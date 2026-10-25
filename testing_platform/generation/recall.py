"""风格锚回灌：golden 用例召回 + 子 agent「相关历史」切片（25 号文 v4.1 §环节11；P2）。

**纯函数层**——不 import tortoise / ORM / LLM，只吃 ``mc_test_case`` 的 dict 行
（``.values()`` 或 ``case_to_payload`` 归一后的 payload 皆可，字段名同名）。
查库（按 project/module 取候选行）与把切片喂给
:func:`.case_gen.build_case_prompt` 的 ``history_slice`` 都在编排器侧；本文件只做
「召回 + 渲染」，可直接单测。

口径（§环节11 原文「评审 keep 且分≥85 的用例打 golden 标记；下一轮同模块生成时
子 agent『相关历史』切片优先召回 golden 用例——先查重再仿写」）：

- golden 标记**不加新列**，落在 ``mc_test_case.confirm_ref.golden=true``
  （由 :mod:`testing_platform.surface.gen_methods` 的 ``case.mark_golden`` 写入），
  故 :func:`is_golden` 读的是 ``confirm_ref``——它可能是 dict，也可能是 JSONB
  取出的 JSON 字符串，两种都要兼容。
- 切片顺序即优先级：**先 golden（仿写风格锚），后 existing（查重清单）**；
  超预算时先截 existing、再截 golden（golden 优先保留）。

命名只用 25 号文既有词：风格锚 / golden / 相关历史 / 查重 / 仿写。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Sequence

__all__ = [
    "DEFAULT_GOLDEN_LIMIT",
    "DEFAULT_MAX_CHARS",
    "is_golden",
    "recall_golden",
    "build_history_slice",
]

#: 召回 golden 用例的默认条数（§环节11「相关历史」切片预算，够定风格即止）。
DEFAULT_GOLDEN_LIMIT = 5

#: 「相关历史」切片渲染的默认字符预算（与调用方提示词体积相称）。
DEFAULT_MAX_CHARS = 4000

#: 切片内每条用例各字段的截断长度（摘要用，不追求全文）。
_TITLE_MAX = 120
_INTENT_MAX = 200
_PRECONDITION_ITEMS = 3
_PRECONDITION_MAX = 60
_STEP_ACTION_MAX = 80
_STEP_EXPECTED_MAX = 60


# ── 内部工具（纯函数，形态归一）────────────────────────────────


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    """任意值 → 单行短文本（None/空 → ""；换行折叠成空格，保证切片一行一条）。"""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return [value]


def _parse_container(value: Any) -> dict:
    """JSONB 容器归一：dict 原样、JSON 字符串解析、解析失败/其它 → {}。"""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes)):
        raw = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _confirm_ref(case_row: Mapping[str, Any]) -> dict:
    """``confirm_ref`` 兼容 dict / JSON 字符串两种形态（解析失败 → {}）。"""
    return _parse_container(case_row.get("confirm_ref"))


# ── is_golden —— 风格锚标记读取 ────────────────────────────────


def is_golden(case_row: Mapping[str, Any]) -> bool:
    """该用例行是否带 golden 风格锚标记（读 ``confirm_ref.golden``）。

    :param case_row: 一条用例 dict（``mc_test_case`` 行或归一后的 payload）。
    :returns: ``confirm_ref`` 存在且其 ``golden`` 为真 → ``True``；缺失、
        ``confirm_ref`` 解析失败、或非 Mapping 行 → ``False``。

    ``confirm_ref`` 可能是 dict，也可能是 JSONB 取出的 JSON 字符串——两种都认。
    撤标（``case.mark_golden`` 的 ``unmark``）会把 ``golden`` 键整个删掉，
    故这里只看键的真值，不看键是否存在。
    """
    if not isinstance(case_row, Mapping):
        return False
    return bool(_confirm_ref(case_row).get("golden"))


# ── recall_golden —— 同模块 golden 用例召回 ────────────────────


def _sort_key_created_desc(index: int, row: Mapping[str, Any]):
    """created_at 倒序排序键；无/坏 created_at 的行排到最后且保持原序（稳定）。"""
    value = row.get("created_at")
    if isinstance(value, datetime):
        stamp = value
    else:
        text = _text(value)
        if not text:
            return (1, 0.0, index)
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return (1, 0.0, index)
    naive = stamp.replace(tzinfo=None) if stamp.tzinfo is not None else stamp
    return (0, -naive.timestamp(), index)


def recall_golden(
    cases: Sequence[Mapping[str, Any]] | None,
    *,
    module: str | None,
    limit: int = DEFAULT_GOLDEN_LIMIT,
) -> list[dict]:
    """从候选用例行里召回可仿写的 golden 用例（§环节11「优先召回 golden」）。

    :param cases: 候选用例行（``mc_test_case`` 的 dict 列表，调用方查库传入）。
    :param module: 本轮目标模块；非空时只保留 ``module`` 字段相等的用例
        （同模块才谈得上仿写风格）；``None`` / 空串 → 不按模块过滤。
    :param limit: 召回条数上限（默认 :data:`DEFAULT_GOLDEN_LIMIT`）；
        ``0`` / 负数 → 空列表；``None`` → 用默认上限。
    :returns: 命中行的 ``dict`` 列表——**原行内容不改**（只浅拷贝成 dict），
        按 ``created_at`` 倒序（无 ``created_at`` 的行保持原相对顺序、排在有时间的之后）。

    过滤口径：origin 不限（golden 既可能来自 ``ai_generated``，也可能来自
    评审手动提升的其它来源），但必须 :func:`is_golden` 为真。
    """
    target = _text(module)
    hits: list[dict] = []
    for row in cases or ():
        if not isinstance(row, Mapping):
            continue
        if not is_golden(row):
            continue
        if target and _text(row.get("module")) != target:
            continue
        hits.append(dict(row))
    # 稳定排序：created_at 倒序，无/坏 created_at 的行沉到最后且保持原相对顺序。
    ordered = [
        row
        for _, row in sorted(
            enumerate(hits),
            key=lambda pair: _sort_key_created_desc(pair[0], pair[1]),
        )
    ]
    if limit is None:
        limit = DEFAULT_GOLDEN_LIMIT
    return ordered[: max(0, int(limit))]


# ── build_history_slice —— 「相关历史」切片渲染 ────────────────


def _steps_summary(case_row: Mapping[str, Any], *, full: bool) -> str:
    """steps → 一行摘要。golden 给每步 action→expected，existing 只给步数。"""
    steps = [s for s in _as_list(case_row.get("steps")) if isinstance(s, Mapping)]
    if not steps:
        return "(no steps)"
    if not full:
        return f"{len(steps)} step(s)"
    parts: list[str] = []
    for step in steps:
        action = _truncate(_text(step.get("action")), _STEP_ACTION_MAX)
        expected = _truncate(_text(step.get("expected")), _STEP_EXPECTED_MAX)
        if action and expected:
            parts.append(f"{action} → {expected}")
        else:
            parts.append(action or expected or "(empty)")
    return " | ".join(parts)


def _expected_keys(case_row: Mapping[str, Any]) -> str:
    """expected 的断言字段名（键），按出现顺序；无则空串。"""
    expected = case_row.get("expected")
    if isinstance(expected, Mapping):
        return ", ".join(str(k) for k in expected.keys())
    return ""


def _render_case_line(case_row: Mapping[str, Any], *, golden: bool) -> str:
    """一条用例 → 切片里的一行（golden 展开风格锚字段，existing 取短摘要）。"""
    code = _text(case_row.get("code")) or "(no code)"
    title = _truncate(_text(case_row.get("title")), _TITLE_MAX) or "(untitled)"
    head = f"- {code} — {title}"
    if not golden:
        summary = _truncate(_text(case_row.get("intent")), _INTENT_MAX)
        extras = [summary, _steps_summary(case_row, full=False)]
        return "\n".join([head] + [f"    {e}" for e in extras if e])

    lines = [head]
    intent = _truncate(_text(case_row.get("intent")), _INTENT_MAX)
    if intent:
        lines.append(f"    intent: {intent}")
    lines.append(f"    steps: {_steps_summary(case_row, full=True)}")
    keys = _expected_keys(case_row)
    if keys:
        lines.append(f"    expected keys: {keys}")
    pre = " | ".join(
        _truncate(_text(p), _PRECONDITION_MAX)
        for p in _as_list(case_row.get("preconditions"))[:_PRECONDITION_ITEMS]
        if _text(p)
    )
    if pre:
        lines.append(f"    preconditions: {pre}")
    tag = _text(case_row.get("scenario_tag"))
    if tag:
        lines.append(f"    scenario_tag: {tag}")
    return "\n".join(lines)


def _render_section(title: str, cases: Sequence[Mapping[str, Any]], *, golden: bool) -> str:
    if not cases:
        return ""
    lines = [title, ""]
    for case_row in cases:
        lines.append(_render_case_line(case_row, golden=golden))
    return "\n".join(lines)


def _join_sections(sections: Sequence[str]) -> str:
    return "\n\n".join(s for s in sections if s.strip())


def build_history_slice(
    golden_cases: Sequence[Mapping[str, Any]] | None,
    existing_cases: Sequence[Mapping[str, Any]] | None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """渲染子 agent「相关历史」切片（喂给 :func:`.case_gen.build_case_prompt` 的
    ``history_slice``；英文正文，项目约定）。

    :param golden_cases: 同模块 golden 用例（一般来自 :func:`recall_golden`）——
        **仿写**的风格锚，展开 code/title、intent、steps 摘要、expected 键、scenario_tag。
    :param existing_cases: 已存在的用例——**查重**清单，只给 code/title + 简短摘要。
    :param max_chars: 切片字符预算（默认 :data:`DEFAULT_MAX_CHARS`）；超预算时
        **先截 existing、再截 golden**（golden 优先保留）。
    :returns: 切片字符串；两者皆空 → 返回空串（让提示词走
        ``"(none — you are first)"`` 兜底）。

    两段顺序即优先级：先 golden 段（"imitate their style, do not copy verbatim"），
    后 existing 段（"do not duplicate — dedup against these"）——先查重再仿写。
    """
    golden = [c for c in (golden_cases or ()) if isinstance(c, Mapping)]
    existing = [c for c in (existing_cases or ()) if isinstance(c, Mapping)]
    if not golden and not existing:
        return ""

    golden_header = "## Golden cases (same module — imitate their style, do not copy verbatim)"
    existing_header = "## Existing cases (do not duplicate — dedup against these)"

    def render(golden_rows: Sequence[Mapping[str, Any]],
               existing_rows: Sequence[Mapping[str, Any]]) -> str:
        return _join_sections([
            _render_section(golden_header, golden_rows, golden=True),
            _render_section(existing_header, existing_rows, golden=False),
        ])

    full = render(golden, existing)
    if max_chars is None or max_chars <= 0 or len(full) <= max_chars:
        return full

    # 超预算：先截 existing（整段丢掉，再逐条丢），golden 段最后才动。
    for keep in range(len(existing), -1, -1):
        candidate = render(golden, existing[:keep])
        if len(candidate) <= max_chars:
            return candidate

    # existing 全丢仍超 → 逐条截 golden（保留最新的在前，尾部先丢）。
    for keep in range(len(golden), -1, -1):
        candidate = render(golden[:keep], ())
        if len(candidate) <= max_chars:
            return candidate
    return ""
