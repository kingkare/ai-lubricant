"""用户规则机制（25 号文第七部分 + 2.6 ``mc_test_rule``）——纯函数层。

四环节生效（25 §7）::

    生成期  render_rules_block() 注入子 agent 提示词尾部 + 回执 applied_rules（未声明=违规）
    校验期  hard 代码判（本模块不涉；调用方拿 rules_for 的结果跑自己的校验器）
    评审期  is_waived() 逐条豁免带过期日（不是整条关规则）
    报表期  bump_counters() 聚合 hits/violations 增量供调用方写库

设计约束：

- **纯函数**：无 IO、无 DB、无 LLM。DB 查询走调用方注入的 callable（本模块连
  tortoise 都不 import），输入一律是行 dict 列表。
- **shadow 语义**（GitHub rulesets 影子模式）：不拦只记，``shadow_log_entry()``
  产出留痕条目，``would_block`` 表示"若 active 会不会拦"。
- **新规则默认 shadow**（25 §7：跑一轮看违反率再 active）——``new_rule()``
  构造的行 enforcement 固定 "shadow"。
- 三坑规避（rules-research §7.4）：规则必有服务端执行点（本模块即其可判定
  数据面）/ 未声明=违规（render_rules_block 的英文指令写死）/ 不退化自由文本
  （每条规则带 rule_id 编号，回执按 ID 逐条点名）。

对外表面::

    RULE_KINDS / SCOPES / ENFORCEMENTS          常量枚举（2.6 表三列）
    rules_for(rules, module=, requirement_id=, surface=)  作用域筛选
    render_rules_block(rules)                   生成期提示词块
    shadow_log_entry(rule, case_payload, violated)        影子留痕条目
    bump_counters(rules, executions)            hits/violations 增量
    new_rule(content, kind, scope, ...)         构造行（默认 shadow）
    waive_entry(...) / is_waived(...)           逐条豁免 + 过期判定
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

# ── 常量枚举（2.6 mc_test_rule 三列的字面量）─────────────────────

#: kind：hard=硬规则（校验期代码判，违反可拦）；soft=软规则（评审期提示/打分）
RULE_KINDS: tuple[str, ...] = ("hard", "soft")

#: scope：作用域层。global 恒生效；module/requirement/surface 按 target 定位
SCOPES: tuple[str, ...] = ("global", "module", "requirement", "surface")

#: enforcement：active=生效可拦；shadow=只记不拦（新规则默认）；disabled=停用
ENFORCEMENTS: tuple[str, ...] = ("active", "shadow", "disabled")

#: 新规则默认 enforcement（25 §7「新规则默认 shadow 跑一轮看违反率再 active」）
DEFAULT_ENFORCEMENT = "shadow"

#: 生成期提示词里的英文指令（未声明=违规——rules-research §7.4 坑 3：
#: promptfoo guardrails「缺证据≠通过」的镜像，缺回执必须判违规而非合规）
DECLARATION_INSTRUCTION = (
    "Follow these project rules; you MUST report per-rule disposition in "
    'applied_rules[] as {rule_id, followed|not_applicable, why}. '
    "Not declaring = violation."
)

# ── 作用域筛选 ────────────────────────────────────────────────


def _present(value: Any) -> bool:
    """None / 空串视为"未提供"（调用方没按这个维度查）。"""
    return value is not None and value != ""


def _rule_matches_context(
    rule: Mapping[str, Any],
    *,
    module: Optional[str],
    requirement_id: Optional[str],
    surface: Optional[str],
) -> bool:
    """单条规则对当前上下文是否命中（2.6：scope + target_type/target_id）。

    - global：恒中；
    - module/requirement/surface：对应上下文参数未提供 → 不中；提供了 →
      target_id 精确匹配，target_id 为空视为"该作用域全域"（如挂在 module
      层但不限定哪个模块）；target_type 若填了必须与 scope 一致（防御行写歪）。
    """
    scope = rule.get("scope")
    if scope == "global":
        return True
    if scope not in ("module", "requirement", "surface"):
        return False  # 未知 scope 不放行（不退化自由文本：行必须可判定）

    # 对应维度没查 → 该层规则不在本次上下文里
    ctx = {"module": module, "requirement": requirement_id, "surface": surface}.get(scope)
    if not _present(ctx):
        return False

    target_type = rule.get("target_type")
    if _present(target_type) and target_type != scope:
        return False

    target_id = rule.get("target_id")
    if not _present(target_id):
        return True  # 只挂了作用域层、没点名实体 → 该层全域生效
    return target_id == ctx


def rules_for(
    rules: Sequence[Mapping[str, Any]],
    *,
    module: Optional[str] = None,
    requirement_id: Optional[str] = None,
    surface: Optional[str] = None,
) -> list[dict]:
    """按作用域筛选当前上下文生效的规则（多作用域并集，保持输入顺序）。

    - global 恒中；module/requirement/surface 按 target_type+target_id 匹配
      （target_id 空视为该层全域）；
    - active 与 shadow 都返回（调用方区别处置：active 拦，shadow 只记）；
    - disabled 剔除（25 §2.6 enforcement 三态）。
    纯函数：输入是行 dict 列表，不改输入、不查库。
    """
    selected: list[dict] = []
    for rule in rules:
        if rule.get("enforcement") == "disabled":
            continue
        if _rule_matches_context(
            rule, module=module, requirement_id=requirement_id, surface=surface
        ):
            selected.append(dict(rule))
    return selected


# ── 生成期提示词块 ────────────────────────────────────────────


def _format_rule_line(index: int, rule: Mapping[str, Any]) -> str:
    rule_id = rule.get("id") or rule.get("rule_id") or ""
    content = str(rule.get("content") or "").strip()
    if rule_id:
        return f"{index}. [{rule_id}] {content}"
    return f"{index}. {content}"


def render_rules_block(rules: Sequence[Mapping[str, Any]]) -> str:
    """渲染注入子 agent 提示词尾部的"本项目规则"块。

    - 英文指令写死"未声明=违规"（applied_rules 回执契约）；
    - 每条规则 content 原文编号列出，hard 与 soft 分节（25 §7）；
    - 生成期只注入 active+shadow（调用方先过 rules_for）；这里不重复过滤，
      传入什么渲染什么——但 disabled 理论上不该出现在入参里。
    """
    hard = [r for r in rules if r.get("kind") == "hard"]
    soft = [r for r in rules if r.get("kind") == "soft"]

    lines: list[str] = ["## Project rules", "", DECLARATION_INSTRUCTION, ""]

    if not hard and not soft:
        lines.append("No project rules are currently in effect for this scope.")
        return "\n".join(lines).rstrip() + "\n"

    if hard:
        lines.append("### Hard rules (violations block the proposal)")
        for i, rule in enumerate(hard, start=1):
            lines.append(_format_rule_line(i, rule))
        lines.append("")
    if soft:
        lines.append("### Soft rules (violations lower review score)")
        for i, rule in enumerate(soft, start=1):
            lines.append(_format_rule_line(i, rule))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── shadow 留痕 ───────────────────────────────────────────────


def _case_ref_of(case_payload: Mapping[str, Any]) -> Any:
    """从用例载荷里取引用字段（首个命中），供留痕条目溯源。"""
    for key in ("case_ref", "ref", "slot_key", "case_id", "proposal_id", "id"):
        if case_payload.get(key) not in (None, ""):
            return case_payload[key]
    return None


def shadow_log_entry(
    rule: Mapping[str, Any],
    case_payload: Mapping[str, Any],
    violated: bool,
) -> dict:
    """shadow 模式留痕条目：不拦只记（GitHub rulesets 影子语义）。

    ``would_block`` = 若 enforcement 是 active 会不会拦——hard 且违反才为
    True；soft 永远不拦。调用方把条目落到留痕存储后按规则聚合转 active。
    """
    return {
        "rule_id": rule.get("id") or rule.get("rule_id"),
        "mode": "shadow",
        "violated": bool(violated),
        "would_block": bool(violated and rule.get("kind") == "hard"),
        "case_ref": _case_ref_of(case_payload),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ── 计数聚合（报表期）────────────────────────────────────────


#: 算"命中"的执行状态（RuleExecution.status，rules-research §7.3）：
#: pass/violated/error = 规则真的跑过这次；skipped = 没跑，不计命中。
_HIT_STATUSES = frozenset({"pass", "violated", "error"})
#: 算"违反"的执行状态
_VIOLATION_STATUSES = frozenset({"violated"})


def bump_counters(
    rules: Sequence[Mapping[str, Any]],
    executions: Iterable[Mapping[str, Any]],
) -> list[tuple[str, int, int]]:
    """按执行记录聚合每条规则的 hits/violations 增量。

    返回 ``(rule_id, hits_delta, violations_delta)`` 元组列表（只含本次执行
    记录涉及的、且在 ``rules`` 里存在的规则；rule_id 升序保证确定性），
    供调用方对 ``mc_test_rule.hits/violations`` 做增量写库。

    - hits：status ∈ {pass, violated, error}（规则实际评估过；skipped 不计）；
    - violations：status == "violated"。
    """
    known_ids = {
        str(r.get("id") or r.get("rule_id"))
        for r in rules
        if r.get("id") is not None or r.get("rule_id") is not None
    }
    hits: dict[str, int] = {}
    violations: dict[str, int] = {}
    for execution in executions:
        rule_id = execution.get("rule_id")
        if rule_id is None:
            continue
        rule_id = str(rule_id)
        if rule_id not in known_ids:
            continue  # 执行记录指向已删除/未知规则：不聚合（防脏写库）
        status = execution.get("status")
        if status in _HIT_STATUSES:
            hits[rule_id] = hits.get(rule_id, 0) + 1
        if status in _VIOLATION_STATUSES:
            violations[rule_id] = violations.get(rule_id, 0) + 1

    touched = set(hits) | set(violations)
    return [(rid, hits.get(rid, 0), violations.get(rid, 0)) for rid in sorted(touched)]


# ── 规则行构造 ────────────────────────────────────────────────


def new_rule(
    content: str,
    kind: str,
    scope: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
) -> dict:
    """构造一条 ``mc_test_rule`` 行 dict——**enforcement 默认 shadow**。

    （25 §7：新规则默认 shadow 跑一轮看违反率再 active；调用方落库时按
    此行 INSERT。）非法 kind/scope/enforcement 直接 raise，不让脏行进库。
    """
    if kind not in RULE_KINDS:
        raise ValueError(f"rule kind must be one of {RULE_KINDS}, got {kind!r}")
    if scope not in SCOPES:
        raise ValueError(f"rule scope must be one of {SCOPES}, got {scope!r}")
    if _present(target_type) and target_type not in SCOPES:
        raise ValueError(
            f"rule target_type must be one of {SCOPES} (mirrors scope), got {target_type!r}"
        )

    resolved_target_type = target_type
    if scope != "global" and not _present(resolved_target_type):
        resolved_target_type = scope  # 缺省与 scope 同名（module/requirement/surface）

    return {
        "content": content,
        "kind": kind,
        "scope": scope,
        "target_type": resolved_target_type,
        "target_id": target_id,
        "enforcement": DEFAULT_ENFORCEMENT,
        "hits": 0,
        "violations": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 逐条豁免（评审期）────────────────────────────────────────


def waive_entry(
    rule_id: str,
    case_ref: Any,
    who: str,
    reason: str,
    expires: Any,
) -> dict:
    """构造一条"逐条豁免"条目——带过期日，不是整条关规则。

    （rules-research §7.2 启示 7：Schemathesis baseline waiver——按"违规
    身份"逐条接受技术债；``expires`` 到期后 ``is_waived`` 自动失效，
    ``who``/``reason`` 留审计痕迹。）
    """
    return {
        "rule_id": rule_id,
        "case_ref": case_ref,
        "waived_by": who,
        "reason": reason,
        "expires": expires,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _to_dt(value: Any) -> Optional[datetime]:
    """宽松时间归一：datetime / ISO 字符串 / epoch 秒 → aware UTC datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # naive 按 UTC（库内惯例全 UTC）
    return dt


def is_waived(
    waivers: Sequence[Mapping[str, Any]],
    rule_id: str,
    case_ref: Any,
    now: Any = None,
) -> bool:
    """该 (rule_id, case_ref) 是否存在未过期的豁免。

    - 豁免按 ``rule_id + case_ref`` 逐条匹配（不是整条规则关掉）；
    - ``expires`` 为空 = 永久豁免；到期（expires < now）自动失效；
    - status ∈ {revoked, expired} 的条目不再生效；
    - ``now`` 缺省取当前 UTC 时间。
    """
    now_dt = _to_dt(now) or datetime.now(timezone.utc)
    for waiver in waivers or ():
        if waiver.get("rule_id") != rule_id:
            continue
        if waiver.get("case_ref") != case_ref:
            continue
        if waiver.get("status") in ("revoked", "expired"):
            continue
        expires_dt = _to_dt(waiver.get("expires"))
        if expires_dt is not None and expires_dt < now_dt:
            continue  # 过期 → 自动失效
        return True
    return False


__all__ = [
    "RULE_KINDS",
    "SCOPES",
    "ENFORCEMENTS",
    "DEFAULT_ENFORCEMENT",
    "DECLARATION_INSTRUCTION",
    "rules_for",
    "render_rules_block",
    "shadow_log_entry",
    "bump_counters",
    "new_rule",
    "waive_entry",
    "is_waived",
]
