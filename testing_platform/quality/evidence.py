"""证据分级与文本级反幻觉校验（24 号文 §Skill6；外部吸收见 skill-library-research R9）。

出处两份，语义在此统一：

- agentic-qe **ADR-105**：结论必须标 EXECUTED/STATIC/INFERRED/CONJECTURE，
  「质量门只对 EXECUTED/STATIC 放行；INFERRED 转对抗复核；CONJECTURE 永不门控」。
- agentic-qe **evidence-classification**（v3/helpers/quality-criteria）：逐级义务 +
  禁用模式（禁裸百分数、禁模糊影响面、待验证里禁推测语气、直证必须带 `file:line`）+
  推理三段式（做什么 → 为何重要 → 会怎么错）+「Claimed 是一等状态非失败态」。

本模块是**纯函数文本校验**（无 IO、无 LLM），供三处消费：

1. `case.review` / 评审子 agent 产物的落库前机器门（EXECUTED/STATIC 才拦门）；
2. `ai_channel._parse_judge` 的三态裁决（passed / inconclusive / blocked，见该模块）；
3. Agent 提示词的硬规则引用（与 `prompts/case_generation.md` 同源措辞）。

设计红线（07 §1 精神）：**AI 不裁判，框架裁判**——等级由产出方**申报**，
但申报文本必须过本模块的机器校验；申报与文本证据不符即 error 拒入。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from .errors import SEVERITY_ERROR, SEVERITY_WARN, ValidationIssue, ValidationResult

# ── 证据等级（四级；conjecture 折入 claimed 的语义在 GATE 上等价：不门控）──
#: 真执行了命令并附输出（如跑测试贴 stdout）。可拦门/可放行。
EVIDENCE_EXECUTED = "executed"
#: 从真实数据推导且点名数据源（DB 查询/AST/覆盖率文件/schema）。可拦门/可放行。
EVIDENCE_STATIC = "static"
#: 无执行的推理。不可直接过门，**转对抗复核**（第二评审者/重查）。
EVIDENCE_INFERRED = "inferred"
#: 待验证/外推（absorbs ADR-105 的 CONJECTURE 与 evidence-classification 的 CLAIMED）。
#: 一等状态非失败态：允许存在，但必须标「requires verification」且**禁止推测语气**。不门控。
EVIDENCE_CLAIMED = "claimed"

EVIDENCE_CLASSES: tuple[str, ...] = (
    EVIDENCE_EXECUTED, EVIDENCE_STATIC, EVIDENCE_INFERRED, EVIDENCE_CLAIMED,
)

#: 质量门只认这两级（ADR-105 原话的落地）
GATING_CLASSES: frozenset[str] = frozenset({EVIDENCE_EXECUTED, EVIDENCE_STATIC})
#: 必须转对抗复核的等级
ADVERSARIAL_CLASSES: frozenset[str] = frozenset({EVIDENCE_INFERRED})


def normalize_evidence_class(raw: Any) -> Optional[str]:
    """申报值归一（容忍 conjecture/claim/direct/inference 等近邻写法）；未知返回 None。"""
    v = str(raw or "").strip().lower()
    if v in ("executed", "direct-executed", "run"):
        return EVIDENCE_EXECUTED
    if v in ("static", "direct", "data"):
        return EVIDENCE_STATIC
    if v in ("inferred", "inference", "reasoned"):
        return EVIDENCE_INFERRED
    if v in ("claimed", "claim", "conjecture", "to_confirm", "unverified"):
        return EVIDENCE_CLAIMED
    return None


def gates(evidence_class: Optional[str]) -> bool:
    """该申报等级是否有资格作为门禁依据（INFERRED/CLAIMED 不拦门也不放行）。"""
    return evidence_class in GATING_CLASSES


# ── 文本级禁用模式（evidence-classification「forbidden patterns」的机器化）──────
# 每条：(规则号, 正则, 中文说明, hint, 默认严重级)
_PATTERNS: tuple[tuple[str, re.Pattern, str, str, str], ...] = (
    (
        "EV1_no_bare_confidence_pct",
        # 裸置信度百分数：85% confident / 置信度85% / 把握有90%——假精确，禁。
        re.compile(r"(\d{1,3})\s*%\s*(confident|confidence|置信|把握|certainty)"
                   r"|(置信度|把握)(有)?\s*(\d{1,3})\s*%", re.I),
        "禁止裸百分数置信度（「85% confident」是假精确）；只许用枚举级 confident/likely/unlikely",
        "删掉百分数，改为声明 evidence_class（executed/static/inferred/claimed）",
        SEVERITY_ERROR,
    ),
    (
        "EV2_no_vague_blast_radius",
        # 模糊影响面：影响很多/大量用户/affects many——必须写具体数。
        re.compile(r"(影响|波及|涉及)\s*(很多|大量|若干|不少|众多|多个模块)"
                   r"|affects\s+(many|several|numerous|a lot)", re.I),
        "禁止模糊影响面（「影响很多」）；必须写可核查的具体数（影响 N 个用例/接口/页面）",
        "把数量查实写成数字，或降级 evidence_class 为 claimed 并注明待验证范围",
        SEVERITY_ERROR,
    ),
    (
        "EV3_claimed_no_speculation",
        # 推测语气：可能是X或者Y/也许是/估计是/不排除——claimed 要求"待验证"，不许替读者猜。
        re.compile(r"可能(是|为)[^。；;]{0,40}(或|或者|也可能是)"
                   r"|(也许|估计|猜测|大概率是)[^。；;]{0,24}(原因|问题|导致)"
                   r"|\bcould be [A-Za-z0-9_]+ or\b"),
        "「待验证」结论禁止推测具体原因（「可能是X或Y」）；只许陈述观察与验证路径",
        "改为：观察到 <现象>，requires verification via <命令/查询>；不要在 claimed 里猜因",
        SEVERITY_ERROR,
    ),
)

#: 直证（executed/static）必须携带的来源引用：file:line / 表名 / 命令 / 查询。
#: 宽松匹配——出现其一即认为有点名来源（文本校验，宁松勿苛；假引用另有复核）。
_SOURCE_HINT = re.compile(
    r"([A-Za-z0-9_./-]+[.](py|ts|tsx|js|json|md|sql|yml|yaml)[:#]?\d?[\d,\-]*)"
    r"|\bmc_test_\w+|\bSELECT\b|\bselect\b"
    r"|```|命令[:：]|查询[:：]|输出[:：]|stdout|覆盖率|schema",
    re.I,
)

#: 行号区间宽度检查（narrow：max 15 行；>30 判过宽——evidence-classification 的
#: "narrow (max 10-15 lines)" 义务，超限降 warn 而非 error：引用仍可能真）。
_LINE_RANGE = re.compile(r"[.](py|ts|tsx|js|json|md)[:#]?(\d+)\s*[-,]\s*(\d+)")
_LINE_RANGE_WARN = 30


def validate_evidence_claim(
    text: str,
    *,
    evidence_class: Optional[str] = None,
    path: str = "$.claim",
) -> ValidationResult:
    """对单条 AI 结论文本跑文本级证据校验（纯函数）。

    :param text: 结论文本（评审 finding、ai_judge reason、提案 intent 等）。
    :param evidence_class: 申报等级（归一见 :func:`normalize_evidence_class`；
        非法/缺失记 error——等级必须申报，这是纪律的根）。
    :returns: `ValidationResult`（复用 07 的结构化 issue 形态）。
    """
    res = ValidationResult()
    cls = normalize_evidence_class(evidence_class)
    if cls is None:
        res.issues.append(ValidationIssue(
            path=path, rule="EV0_class_required",
            msg=f"证据等级必须申报且只能是 {list(EVIDENCE_CLASSES)} 之一，收到 {evidence_class!r}",
            severity=SEVERITY_ERROR,
            hint="给每条结论标 executed/static/inferred/claimed",
        ))
    body = text or ""
    for rule, pattern, msg, hint, sev in _PATTERNS:
        m = pattern.search(body)
        if m:
            res.issues.append(ValidationIssue(
                path=path, rule=rule,
                msg=f"{msg}（命中片段：{m.group(0)[:60]!r}）",
                severity=sev, hint=hint,
            ))
    # 直证必须点名来源；claimed 明确免此义务（一等状态非失败态）。
    if cls in (EVIDENCE_EXECUTED, EVIDENCE_STATIC) and not _SOURCE_HINT.search(body):
        res.issues.append(ValidationIssue(
            path=path, rule="EV4_direct_needs_source",
            msg=f"{cls} 级结论必须携带可核查来源（file:line / 表+查询 / 命令+输出 / 覆盖率文件）",
            severity=SEVERITY_ERROR,
            hint="附上出处，或如实降级为 inferred/claimed",
        ))
    # 行号区间过宽（narrow 义务）
    for lm in _LINE_RANGE.finditer(body):
        lo, hi = int(lm.group(2)), int(lm.group(3))
        if hi - lo > _LINE_RANGE_WARN:
            res.issues.append(ValidationIssue(
                path=path, rule="EV5_reference_too_wide",
                msg=f"引用行区间 {lo}-{hi} 过宽（{hi - lo} 行）——窄引 10-15 行，宽了说明没真读",
                severity=SEVERITY_WARN,
                hint="缩窄到结论真正依据的行段",
            ))
    return res


def enforce_three_part_reasoning(reasoning: Mapping[str, Any], *, path: str = "$.reasoning") -> ValidationResult:
    """推理三段式完整性（observation「代码/数据做什么」→ implication「对质量意味什么」
    → risk「会怎么错」）——ai_judge 输出契约的机器面。缺段=warn（提示词负责教，
    校验器只查形状）。"""
    res = ValidationResult()
    for key, label in (("observation", "做什么"), ("implication", "为何重要"), ("risk", "会怎么错")):
        val = reasoning.get(key)
        if not (isinstance(val, str) and val.strip()):
            res.issues.append(ValidationIssue(
                path=f"{path}.{key}", rule="EV6_three_part_incomplete",
                msg=f"推理三段式缺「{label}」段（{key}）",
                severity=SEVERITY_WARN,
                hint="格式：{What the code does} → {Why that matters for quality} → {What could go wrong}",
            ))
    return res


__all__ = [
    "EVIDENCE_CLASSES", "EVIDENCE_EXECUTED", "EVIDENCE_STATIC",
    "EVIDENCE_INFERRED", "EVIDENCE_CLAIMED",
    "GATING_CLASSES", "ADVERSARIAL_CLASSES",
    "normalize_evidence_class", "gates",
    "validate_evidence_claim", "enforce_three_part_reasoning",
]
