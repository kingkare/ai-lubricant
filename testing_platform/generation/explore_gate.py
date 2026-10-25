"""场景化探索门·按内容分级（25 号文 v4.1 §环节7；P2）。

**纯函数层**——不 import tortoise、不查库、不调 LLM、不发请求。所有「既有数据」
（用例 payload、接口文档行 ``mc_test_api``、探索中挂的段事件）由调用方查库后传
进来，与 :mod:`gates` / :mod:`defect_flow` / :mod:`dedup` 同款纪律，因此单测
不需要 PG。落库（``mc_test_case.verified_by_run``）与真跑（api 试调 / web-app
走引擎 page·device 工具）都在编排器侧，本文件只回答两件事：

1. **这条用例要不要过探索门**（:func:`gate_required`）；
2. **过门时怎么过**（:func:`trial_call_plan` = api 那一侧的试调清单；
   :func:`change_package` = 挂的段攒成的待审变更包）。

对照 §环节7 原文（探索门**按内容分，不按端**）：

===========================  ========  ======================================
内容                          过门      依据（原文）
===========================  ========  ======================================
参数类型/必填/格式/可行性       不过      契约可定，request+check 步骤机械翻译直接可跑
（框架测试）
字段规则/接口逻辑/接口安全      过        断言值是推测的，必须真调验证
web/app 全部                  过        选择器/流程全是推测
===========================  ========  ======================================

步骤原文：api=**试调**一遍（断言字段/业务码真实存在，不存在打回）；web/app=dsh 走
引擎 page/device 工具真跑+每步截图（意图驱动/自愈优先/每步留证三技巧），真实轨迹
替换推测步骤；挂的段攒成事件→**离线批量重写**→**待审变更包**人签（不做运行时自愈）。
状态原文：``case.verified_by_run`` **未探索/已探索/探索失败**；未过门不进波次。

对外符号（``__all__``）
------------------------

======================================  =========================================
:func:`gate_required`                    过门判定 + 可解释 reason + content_class
:data:`CONTENT_CLASSES`                  content_class 五值闭集（framework 免门）
:data:`FRAMEWORK_CLASS`                  免门那一类 ``"framework"`` 的字面量
:data:`SURFACE_ASSERTION_FIELDS`         各端断言字段（镜像 case_gen，字段名不自造）
:data:`VERIFY_STATES` / 中文标签映射       未探索/已探索/探索失败 的三态与跨语归一
:func:`normalize_verify_state`           任意写法（中英/别名/空）→ 三态之一
:func:`next_verify_state`                一次探索结果（pass/fail）→ 该落的状态
:func:`trial_call_plan`                  api 侧试调清单（核验点 + 是否 blocking）
:func:`change_package`                   挂的段事件 → 待审变更包（稳定包号）
======================================  =========================================

依赖纪律（并发热区）
--------------------

- **不 import ``generation`` 内其他模块**（``case_gen`` / ``waves`` /
  ``orchestrator`` / ``models`` 是并发会话热区）。因此本文件**镜像**了三组常量：
  :data:`SURFACE_ASSERTION_FIELDS`（= ``case_gen.SURFACE_ASSERTIONS``）、
  安全类 ``scenario_tag`` 别名（= ``gates._TOKEN_INVALID_TAGS`` +
  ``gates._CROSS_TENANT_TAGS``）、覆盖轴三分类（= ``design.transform_table.COVERAGE_AXES``
  的 C01-C14 语义）。漂移由 ``tests/test_generation_explore_gate.py`` 里的
  「两处常量集合相等」对照断言钉住（与 :mod:`defect_flow` 同一手法）。
- 断言字段名一律取各端既有字段（``status_code``/``biz_code``/``key_fields``/
  ``response_time``、``reject_code``/``audit_trail``/``data_invisible`` 等），
  **不自造字段名**；名词一律取 25 号文原词（探索门/未探索/已探索/探索失败/试调/
  变更包）。

歧义记录（按文档 + 任务口径）
-----------------------------

1. **content_class 只有五个值，但没有「未知」那一格**：surface 既不是 web/app
   也没有任何框架类信号时，判定落在 **``logic``（接口逻辑）且过门**——保守方向
   选「过门」而不是「免门」：免门的代价是推测断言直接进波次真跑，过门的代价只是
   多一次试调。reason 里会明说是保守兜底。
2. **security 端（第四端）不在这张表里**：§环节7 表只写了 api 与 web/app，
   ``surface=security`` 的用例内容本来就是「接口安全」那一行 → 过门、
   ``content_class="security"``（不套 web/app 的 ui_flow）。
3. **框架类要求「正面证据」，不是「没反证就算」**：命中框架类 tag/覆盖轴/文本，
   或 expected 只落 ``status_code`` 一档，才判 ``framework``；否则按最严重的一类
   过门（安全 > 逻辑 > 字段规则 > 框架）。
4. **送错参数类型不构成打回**：非数字/超长/非法枚举本来就是框架类变体的**载荷**
   （见 ``transform_table.TRANSFORMS_BY_TYPE``），所以试调清单里的类型核对项
   ``blocking=False``（只记录，不作打回条件）；参数名在契约里查无此项才打回。
5. **变更包包号与事件到达顺序无关**：包号 = 规范化 item 集合（去掉序号）的
   sha1 前 16 位，重放同一批事件得到同一个包号（幂等）；item 本身保留传入顺序。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "CONTENT_CLASSES",
    "FRAMEWORK_CLASS",
    "SURFACE_ASSERTION_FIELDS",
    "VERIFY_STATES",
    "VERIFY_STATE_LABELS",
    "normalize_verify_state",
    "next_verify_state",
    "gate_required",
    "trial_call_plan",
    "change_package",
]

# ── 端与内容分类（§环节7 表的三行）─────────────────────────────

#: 五种内容分类（任务书给定的闭集；framework=框架测试、field_rule=字段规则、
#: logic=接口逻辑、security=接口安全、ui_flow=web/app 的选择器/流程）。
CONTENT_CLASSES: tuple[str, ...] = (
    "framework", "field_rule", "logic", "security", "ui_flow",
)

#: 探索门免门的那一类（契约可定 → request+check 步骤机械翻译直接可跑）。
FRAMEWORK_CLASS = "framework"

#: 一律过门的两个端（选择器/流程全是推测）。
UI_SURFACES: tuple[str, ...] = ("web", "app")

#: 机械翻译端（内容再细分为 framework/field_rule/logic/security）。
API_SURFACE = "api"

#: 第四端 security：内容本身就是「接口安全」那一行 → 过门。
SECURITY_SURFACE = "security"

#: 各端断言体系字段（**镜像** ``case_gen.SURFACE_ASSERTIONS``，字段名不得自造；
#: 漂移由测试钉住）。键 = surface，值 = 该端断言字段（有序）。
SURFACE_ASSERTION_FIELDS: dict[str, tuple[str, ...]] = {
    "api": ("status_code", "biz_code", "key_fields", "response_time"),
    "web": ("element_visible", "element_text", "url"),
    "app": ("foreground_state", "persisted_state", "crash_log"),
    "security": ("reject_code", "audit_trail", "data_invisible"),
}

#: expected 里出现这些键 = 该用例在断言安全结果（security 端三字段）。
_SECURITY_EXPECTED_KEYS: frozenset[str] = frozenset(
    SURFACE_ASSERTION_FIELDS[SECURITY_SURFACE]
)

#: 探索门三行的依据原文短语（只用于拼可解释 reason，不新增名词）。
_CLASS_BASIS: dict[str, str] = {
    "framework": "参数类型/必填/格式/可行性（框架测试）：契约可定，request+check 步骤机械翻译直接可跑",
    "field_rule": "字段规则：断言值是推测的，必须真调验证",
    "logic": "接口逻辑：断言值是推测的，必须真调验证",
    "security": "接口安全：断言值是推测的，必须真调验证",
    "ui_flow": "web/app 全部：选择器/流程全是推测",
}

# ── 结构化信号词典（scenario_tag / 覆盖轴 / 文本）───────────────

#: 安全类 scenario_tag（**镜像** ``gates._TOKEN_INVALID_TAGS`` ∪
#: ``gates._CROSS_TENANT_TAGS``——25 号文未定义 scenario_tag 枚举，机器门用同一组
#: 宽松别名刻画「Token 失效/越权」两类必覆盖场景，探索门沿用，避免两套口径）。
_SECURITY_TAGS: frozenset[str] = frozenset({
    "token_expired", "token-expired", "token_invalid", "token-invalid",
    "no-token", "expired-token", "invalid-token", "token_failure",
    "auth_failure", "auth-failure", "token失效", "token 失效",
    "cross_tenant", "cross-tenant", "unauthorized", "cross_tenant_access",
    "privilege_escalation", "privilege-escalation", "forbidden", "越权",
    "越权id", "越权 id", "cross-tenant-access",
})

#: 逻辑类 scenario_tag（六策略里的场景法/状态迁移 + 幂等/并发/一致性等轴名）。
_LOGIC_TAGS: frozenset[str] = frozenset({
    "scenario", "state-transition", "state_transition", "flow", "e2e",
    "end-to-end", "consistency", "idempotency", "idem", "concurrency",
    "retry", "依赖", "幂等", "并发", "重试", "一致性", "状态迁移", "场景",
})

#: 框架类 scenario_tag（六策略中参数面那四类 + 变换表变体名）。
_FRAMEWORK_TAGS: frozenset[str] = frozenset({
    "positive", "negative", "boundary", "equivalence-class",
    "equivalence_class", "param", "framework", "valid", "missing", "empty",
    "null", "over-max", "under-max", "under-min", "max+1", "min-1",
    "non-numeric", "special-chars", "injection-probe", "invalid", "malformed",
    "not-found", "true", "false", "string-typed", "over-max-items", "future",
    "past", "合法值", "缺失", "空串", "超长", "过短", "边界", "等价类",
})

#: 覆盖轴 → 内容分类（**镜像** ``design.transform_table.COVERAGE_AXES`` 的
#: C01-C14 语义）：契约可定的三轴（边界值/输入校验/异常流/正常流的参数面）免门。
_AXIS_CLASS: dict[str, str] = {
    "C01": FRAMEWORK_CLASS, "C02": FRAMEWORK_CLASS,
    "C03": FRAMEWORK_CLASS, "C12": FRAMEWORK_CLASS,
    "C04": "security", "C07": "security", "C14": "security",
    "C05": "logic", "C06": "logic", "C08": "logic", "C09": "logic",
    "C10": "logic", "C11": "logic", "C13": "logic",
}

#: 断言文本里判「接口安全」的词（越权/无 token/401/403/脱敏/注入探针…）。
#: ``{{scope.path}}`` 占位符先剥掉再匹配——``{{auth.token}}`` 是数据作用域，
#: 不是安全断言（preconditions 整段也不进语料，见 :func:`_assertion_corpus`）。
_SECURITY_TEXT_RE = re.compile(
    r"越权|未授权|无权访问|无\s*token|token\s*(?:失效|过期|无效|缺失)|"
    r"鉴权|认证|授权|权限|脱敏|敏感数据|数据泄露|泄露|注入|"
    r"unauthorized|forbidden|cross[-_ ]?tenant|privilege|sql\s*inject|"
    r"\b40[13]\b",
    re.I,
)

#: 断言文本里判「接口逻辑」的词（状态迁移/幂等/并发/上下游/落库生效…）。
_LOGIC_TEXT_RE = re.compile(
    r"状态(?:迁移|流转|机|回退)|流转|迁移|幂等|并发|重试|回滚|"
    r"一致性|上下游|依赖|生效|落库|余额|库存|扣减|冻结|解冻|"
    r"审批|流程|之后|然后|再次(?:提交|调用)|重复提交",
    re.I,
)

#: 断言文本里判「字段规则」的词（值层面的业务规则；**不含** 格式/必填/类型/长度
#: ——那四个属于框架那一行「参数类型/必填/格式/可行性」）。
_FIELD_RULE_TEXT_RE = re.compile(
    r"字段规则|计算|精度|小数|舍入|四舍五入|默认值|唯一|有效期|"
    r"换算|映射|汇总|累计|取到|业务字段",
    re.I,
)

#: 断言文本里判「框架测试」的词（参数类型/必填/格式/可行性四类）。
_FRAMEWORK_TEXT_RE = re.compile(
    r"参数类?型|类型错误|必填|缺失|为空|空串|null|格式(错误|不对|不合法)|"
    r"非数字|非法枚举|枚举值|特殊字符|不存在的|不支持的|"
    r"可行性|参数校?验|超长|过短|边界值?",
    re.I,
)

#: 占位符 ``{{scope.path}}``（引擎变量语法，五作用域）——语料匹配前先剥掉。
_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")

# ── 探索门状态（case.verified_by_run：未探索/已探索/探索失败）───

#: 三态（英文 slug，落 ``mc_test_case.verified_by_run`` VARCHAR(32)）。
VERIFY_STATES: tuple[str, ...] = ("unexplored", "explored", "failed")

#: 三态 → 25 号文原词（前端展示/报告文案一律取这张表，别处不再写中文）。
VERIFY_STATE_LABELS: dict[str, str] = {
    "unexplored": "未探索",
    "explored": "已探索",
    "failed": "探索失败",
}

#: 缺省态（未知/空值一律归这里——「未过门不进波次」由缺省态兜住）。
DEFAULT_VERIFY_STATE = "unexplored"

#: 中文与常见英文写法 → 三态（DB 里可能被人写成中文，读侧必须容）。
_VERIFY_ALIASES: dict[str, str] = {
    "unexplored": "unexplored", "not_explored": "unexplored",
    "un_explored": "unexplored", "notexplored": "unexplored",
    "pending": "unexplored", "none": "unexplored", "null": "unexplored",
    "未探索": "unexplored", "待探索": "unexplored", "未过门": "unexplored",
    "explored": "explored", "verified": "explored", "passed": "explored",
    "done": "explored", "已探索": "explored", "已验证": "explored",
    "failed": "failed", "explore_failed": "failed", "failure": "failed",
    "探索失败": "failed",
}

# ── 小工具（纯函数）───────────────────────────────────────────


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _norm(value: Any) -> str:
    """归一成一个可比较的小写短串（去首尾空白、压内部空白）。"""
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _present(value: Any) -> bool:
    """断言值是否「真的设计了」——``_normalize_expected`` 会把没设计的字段补成
    None，空串/空 dict/空 list 同样视为未设计（不据未设计的字段判内容）。"""
    if value is None:
        return False
    if isinstance(value, (str, list, tuple, dict, set)):
        return bool(str(value).strip()) if isinstance(value, str) else bool(value)
    return True


def _iter_strings(obj: Any) -> list[str]:
    """递归收集 payload 片段里的字符串（dict 的值、list 的元素）。"""
    out: list[str] = []
    if isinstance(obj, Mapping):
        for v in obj.values():
            out.extend(_iter_strings(v))
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            out.extend(_iter_strings(v))
    elif isinstance(obj, str) and obj.strip():
        out.append(obj)
    return out


def _strip_placeholders(text: str) -> str:
    """剥掉 ``{{scope.path}}`` 占位符（数据作用域不是断言内容）。"""
    return _PLACEHOLDER_RE.sub(" ", text or "")


def _tags_of(raw: Any) -> set[str]:
    """scenario_tag → 归一 tag 集合（str 或 list 都吃）。"""
    return {t for t in (_norm(x) for x in _as_list(raw)) if t}


def _assertion_corpus(case_payload: Mapping[str, Any]) -> str:
    """判内容用的语料：**只取断言面**（title/intent/steps 的 action+expected/
    expected 的值/evidence），**不含 preconditions**（那是数据前置，写「已登录
    {{auth.user}}」不代表这条用例在测安全）。"""
    parts: list[str] = []
    for key in ("title", "intent", "description"):
        raw = case_payload.get(key)
        if isinstance(raw, str):
            parts.append(raw)
    for step in _as_list(case_payload.get("steps")):
        if isinstance(step, Mapping):
            parts.extend(_iter_strings({k: step.get(k) for k in ("action", "expected")}))
        elif isinstance(step, str):
            parts.append(step)
    parts.extend(_iter_strings(_as_dict(case_payload.get("expected"))))
    parts.extend(_iter_strings(case_payload.get("assertions")))
    parts.extend(str(x) for x in _as_list(case_payload.get("evidence")) if isinstance(x, str))
    return _strip_placeholders(" \n ".join(p for p in parts if p))


def _slot_axis(case_payload: Mapping[str, Any]) -> str:
    """从幂等键里取覆盖轴。

    三种形态（与 ``gates.make_slot_key`` / ``transform_table.slot_key`` 同形）：
    ``api:{api_id}:{param}:{axis}:{variant}``（第 4 段是 C 轴）、
    ``auth:{api_id}:{variant}`` / ``idem:{api_id}:{variant}``（通用轴前缀本身即类型）、
    ``flow:{tp_code}:{surface}:{seq}``（业务流）。
    """
    key = str(case_payload.get("slot_key") or "").strip()
    if not key:
        return ""
    parts = key.split(":")
    head = parts[0].strip().lower()
    if head == "api" and len(parts) >= 4:
        return parts[3].strip().upper()
    if head in ("auth", "idem"):
        return head
    if head == "flow":
        return "flow"
    return ""


def _classify_content(case_payload: Mapping[str, Any]) -> tuple[str, str]:
    """payload 内容 → ``(content_class, 命中的信号说明)``。

    优先级：接口安全 > 接口逻辑 > 字段规则 > 框架测试；四类都没正面命中时落
    ``logic``（歧义记录 1：保守方向选过门）。
    """
    payload = _as_dict(case_payload)
    tags = _tags_of(payload.get("scenario_tag"))
    corpus = _assertion_corpus(payload)
    expected = _as_dict(payload.get("expected"))
    axis = _slot_axis(payload)
    axis_class = _AXIS_CLASS.get(axis) or (
        {"auth": "security", "idem": "logic", "flow": "logic"}.get(axis)
    )
    hits: dict[str, list[str]] = {"security": [], "logic": [], "field_rule": [],
                                  FRAMEWORK_CLASS: []}

    def _hit(cls: str, text: str) -> None:
        if text not in hits[cls]:
            hits[cls].append(text)

    # 1) 接口安全
    if _SECURITY_EXPECTED_KEYS & {str(k).strip().lower() for k in expected}:
        _hit("security", "expected 带 security 端断言字段（reject_code/audit_trail/data_invisible）")
    if tags & _SECURITY_TAGS:
        _hit("security", f"scenario_tag 命中 Token 失效/越权（{sorted(tags & _SECURITY_TAGS)}）")
    if axis_class == "security":
        _hit("security", f"覆盖轴 {axis} 属安全约束")
    m = _SECURITY_TEXT_RE.search(corpus)
    if m:
        _hit("security", f"断言文本命中安全词「{m.group(0)}」")

    # 2) 接口逻辑
    if _present(expected.get("biz_code")):
        _hit("logic", "expected.biz_code 有具体值（业务码是推测的）")
    if _present(expected.get("response_time")):
        _hit("logic", "expected.response_time 有阈值（推测值）")
    if tags & _LOGIC_TAGS:
        _hit("logic", f"scenario_tag 命中场景法/状态迁移/幂等（{sorted(tags & _LOGIC_TAGS)}）")
    if axis_class == "logic":
        _hit("logic", f"覆盖轴 {axis} 属并发/重试/一致性/依赖/状态转换")
    m = _LOGIC_TEXT_RE.search(corpus)
    if m:
        _hit("logic", f"断言文本命中逻辑词「{m.group(0)}」")

    # 3) 字段规则
    if _present(expected.get("key_fields")):
        _hit("field_rule", "expected.key_fields 有具体字段断言（字段值/类型是推测的）")
    if axis_class == "field_rule":
        _hit("field_rule", f"覆盖轴 {axis} 属字段规则")
    m = _FIELD_RULE_TEXT_RE.search(corpus)
    if m:
        _hit("field_rule", f"断言文本命中字段规则词「{m.group(0)}」")

    # 4) 框架测试（要正面证据）
    if tags & _FRAMEWORK_TAGS:
        _hit(FRAMEWORK_CLASS, f"scenario_tag 命中参数面变体（{sorted(tags & _FRAMEWORK_TAGS)}）")
    if axis_class == FRAMEWORK_CLASS:
        _hit(FRAMEWORK_CLASS, f"覆盖轴 {axis} 属参数类型/必填/格式/可行性")
    if expected and not any(
        _present(expected.get(k))
        for k in set(SURFACE_ASSERTION_FIELDS.get(API_SURFACE, ())) - {"status_code"}
    ) and _present(expected.get("status_code")):
        _hit(FRAMEWORK_CLASS, "expected 只落 status_code 一档（契约可定）")
    m = _FRAMEWORK_TEXT_RE.search(corpus)
    if m:
        _hit(FRAMEWORK_CLASS, f"断言文本命中参数面词「{m.group(0)}」")

    for cls in ("security", "logic", "field_rule"):
        if hits[cls]:
            return cls, "；".join(hits[cls])
    if hits[FRAMEWORK_CLASS]:
        return FRAMEWORK_CLASS, "；".join(hits[FRAMEWORK_CLASS])
    return "logic", "无框架类正面证据，按接口逻辑保守过门"


# ── 1) 探索门判定 ─────────────────────────────────────────────


def gate_required(case_payload: Mapping[str, Any], surface: str) -> dict:
    """这条用例要不要过探索门（§环节7「探索门按内容分，不按端」）。

    :param case_payload: 入库用例 / case proposal 的 payload（
        :func:`testing_platform.generation.case_gen.case_to_payload` 的形状：
        ``title/intent/steps/expected/preconditions/scenario_tag/slot_key``）。
    :param surface: 该用例的端（``api|web|app|security``）。传空则回退读
        ``case_payload["surface"]``。

    :returns: ``{"required": bool, "reason": str, "content_class": str}``——
        ``content_class`` ∈ :data:`CONTENT_CLASSES`；``reason`` 是命中的结构化信号
        + 25 号文那三行的依据原文，可直接给人看。

    判定顺序：

    1. ``surface`` ∈ web/app → 一律过门（``ui_flow``，选择器/流程全是推测）；
    2. ``surface`` = security → 过门（内容就是「接口安全」那一行）；
    3. 其余（api，以及认不出端时的保守路径）→ 按 payload **内容** 分：
       命中字段规则/接口逻辑/接口安全 → 过门；只有参数类型/必填/格式/可行性
       （框架测试）→ 不过门（request+check 机械翻译直接可跑）。
    """
    payload = _as_dict(case_payload)
    surf = _norm(surface) or _norm(payload.get("surface"))

    if surf in UI_SURFACES:
        content_class = "ui_flow"
        required = True
        hits = f"surface={surf} 属 web/app"
    elif surf == SECURITY_SURFACE:
        content_class = "security"
        required = True
        hits = "surface=security（用例内容即「接口安全」那一行）"
    else:
        content_class, hits = _classify_content(payload)
        # 免门只对 api 机械翻译成立：端没标明 / 认不出的端一律保守过门
        required = content_class != FRAMEWORK_CLASS or surf != API_SURFACE

    basis = _CLASS_BASIS[content_class]
    if not required:
        reason = f"不过探索门（{content_class}）：{basis}。命中信号：{hits}。"
    else:
        reason = f"过探索门（{content_class}）：{basis}。命中信号：{hits}。"
        if content_class == FRAMEWORK_CLASS:
            reason += (
                "（端未标明，按内容判定后仍保守过门）" if not surf
                else f"（surface={surf} 非 api，免门只对 api 机械翻译成立）"
            )

    return {"required": required, "reason": reason, "content_class": content_class}


# ── 2) 探索门状态 ─────────────────────────────────────────────


def normalize_verify_state(value: Any) -> str:
    """任意写法 → :data:`VERIFY_STATES` 之一（未知/空 → ``unexplored``）。

    容忍中文原词（未探索/已探索/探索失败）与常见近邻写法（verified/passed/
    pending/not_explored），因为 ``mc_test_case.verified_by_run`` 是可空
    VARCHAR(32)，人工与旧数据都会写中文或留空。
    """
    key = _norm(value)
    if not key:
        return DEFAULT_VERIFY_STATE
    if key in VERIFY_STATES:
        return key
    aliased = _VERIFY_ALIASES.get(key)
    if aliased:
        return aliased
    # 中文原词可能带修饰（「已探索（试调通过）」）——按标签前缀再试一次
    for state, label in VERIFY_STATE_LABELS.items():
        if key.startswith(label):
            return state
    return DEFAULT_VERIFY_STATE


def next_verify_state(previous: Any, outcome: str) -> str:
    """一次探索结果 → 该落到哪个 ``verified_by_run``。

    :param previous: 上一次状态（非法/空按 ``unexplored`` 处理——只影响无结论时
        的回填，不改变 pass/fail 的确定性结论）。
    :param outcome: ``pass`` → ``explored``；``fail`` → ``failed``；
        其他（无结论/取消/跳跑）→ 保持 ``previous`` 的归一值。
    """
    prev = normalize_verify_state(previous)
    key = _norm(outcome)
    if key in ("pass", "passed", "ok", "success", "explored", "通过"):
        return "explored"
    if key in ("fail", "failed", "error", "探索失败"):
        return "failed"
    return prev


# ── 3) api 试调清单 ───────────────────────────────────────────

#: 业务码在响应契约里的常见键名（大小写不敏感；只用来从 example/schema 取值）。
_BIZ_CODE_KEYS: frozenset[str] = frozenset({
    "code", "biz_code", "bizcode", "biz_code_no", "errcode", "err_code",
    "error_code", "ret", "retcode", "ret_code", "errno", "result_code",
    "resultcode",
})

#: 请求体在步骤 params 里的挂载键（与 ``gates._REQUEST_BODY_KEYS`` 同口径——
#: transport 级键 method/url/headers 不是参数名，不核）。
_REQUEST_BODY_KEYS: tuple[str, ...] = ("body", "json", "data", "form", "query", "payload")


def _api_param_index(api_doc: Mapping[str, Any]) -> dict[str, str]:
    """接口声明的参数名 → 类型（``params[]`` + ``request_body`` 顶层 properties）。

    request_body 形状兼容两种（与 ``gates._api_param_names`` 同口径）：
    ``{"schema": {"properties": {...}}}`` 优先，退 ``body["properties"]``。
    """
    index: dict[str, str] = {}
    for p in _as_list(api_doc.get("params")):
        if isinstance(p, Mapping):
            name = str(p.get("name") or "").strip()
            if name:
                index[name] = str(p.get("type") or "").strip().lower()
    body = api_doc.get("request_body")
    props: Any = None
    if isinstance(body, Mapping):
        schema = body.get("schema")
        props = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(props, Mapping):
            props = body.get("properties")
    if isinstance(props, Mapping):
        for name, spec in props.items():
            if not name:
                continue
            t = spec.get("type") if isinstance(spec, Mapping) else None
            index.setdefault(str(name), str(t or "").strip().lower())
    return index


def _declared_statuses(api_doc: Mapping[str, Any]) -> set[str]:
    """契约声明的 HTTP 状态码集合（``responses[].status_code``）。"""
    out: set[str] = set()
    for resp in _as_list(api_doc.get("responses")):
        if isinstance(resp, Mapping):
            sc = resp.get("status_code", resp.get("status"))
            if sc is not None and str(sc).strip():
                out.add(str(sc).strip())
    return out


def _walk_schema_keys(node: Any, prefix: str = "", depth: int = 0) -> set[str]:
    """schema/example 树里的字段名（含 ``data.order`` 这种带前缀的嵌套路径）。"""
    keys: set[str] = set()
    if depth > 4:
        return keys
    if isinstance(node, Mapping):
        props = node.get("properties")
        if isinstance(props, Mapping):
            for k, sub in props.items():
                path = f"{prefix}.{k}" if prefix else str(k)
                keys.add(path)
                keys |= _walk_schema_keys(sub, path, depth + 1)
        items = node.get("items")
        if items is not None:
            keys |= _walk_schema_keys(items, prefix, depth + 1)
        for k, v in node.items():
            if k in ("properties", "items", "type", "required", "description",
                     "example", "examples"):
                continue
            if isinstance(v, Mapping):
                path = f"{prefix}.{k}" if prefix else str(k)
                keys.add(path)
                keys |= _walk_schema_keys(v, path, depth + 1)
    return keys


def _declared_response_fields(api_doc: Mapping[str, Any]) -> set[str]:
    """契约声明的响应字段名（各 responses 的 schema + example 顶层/嵌套键）。"""
    out: set[str] = set()
    for resp in _as_list(api_doc.get("responses")):
        if not isinstance(resp, Mapping):
            continue
        out |= _walk_schema_keys(resp.get("schema"))
        example = resp.get("example")
        if isinstance(example, Mapping):
            out |= _walk_schema_keys(example)
            out |= {str(k) for k in example.keys()}
    return out


def _declared_biz_codes(api_doc: Mapping[str, Any]) -> set[str]:
    """契约里出现过的业务码值（responses[].example / schema 的 code 类键取值）。"""
    out: set[str] = set()

    def _collect(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        for k, v in node.items():
            if str(k).strip().lower() in _BIZ_CODE_KEYS:
                if isinstance(v, (list, tuple, set)):
                    out.update(str(x).strip() for x in v if str(x).strip())
                elif isinstance(v, Mapping):
                    for sub in ("enum", "default", "example", "const"):
                        sv = v.get(sub)
                        if isinstance(sv, (list, tuple, set)):
                            out.update(str(x).strip() for x in sv if str(x).strip())
                        elif sv is not None and str(sv).strip():
                            out.add(str(sv).strip())
                elif v is not None and str(v).strip():
                    out.add(str(v).strip())
        props = node.get("properties")
        if isinstance(props, Mapping):
            for sub in props.values():
                _collect(sub)

    for resp in _as_list(api_doc.get("responses")):
        if isinstance(resp, Mapping):
            _collect(resp.get("schema"))
            _collect(resp.get("example"))
    return {o for o in out if o}


def _referenced_params(payload: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    """payload 引用的请求参数 ``(位置, 参数名, 送的值)``（只取显式请求参数，
    口径同 ``gates._collect_referenced_param_names``，另把 body 的值一并带出）。"""
    out: list[tuple[str, str, Any]] = []
    for i, p in enumerate(_as_list(payload.get("params"))):
        if isinstance(p, Mapping):
            name = str(p.get("name") or "").strip()
            if name:
                out.append((f"$.params[{i}].name", name, p.get("value", p.get("example"))))
    for field in ("body", "request_body"):
        b = payload.get(field)
        if isinstance(b, Mapping):
            for k, v in b.items():
                if k:
                    out.append((f"$.{field}.{k}", str(k), v))
    for si, step in enumerate(_as_list(payload.get("steps"))):
        if not isinstance(step, Mapping):
            continue
        sp = step.get("params")
        if not isinstance(sp, Mapping):
            continue
        for bk in _REQUEST_BODY_KEYS:
            nested = sp.get(bk)
            if isinstance(nested, Mapping):
                for k, v in nested.items():
                    if k:
                        out.append((f"$.steps[{si}].params.{bk}.{k}", str(k), v))
    return out


def _field_path_segments(path: str) -> list[str]:
    return [s for s in re.split(r"[.\[\]]+", path or "") if s]


def _item(point: str, kind: str, target: Any, expected: Any, *,
          verifiable: bool, found: bool | None = None, note: str = "") -> dict:
    """试调清单项。``blocking`` 由 ``verifiable`` 且 ``found=False`` 派生——
    契约判得了、又判它查无此项，才是不必真调就打回的硬点。"""
    return {
        "point": point, "kind": kind, "target": target, "expected": expected,
        "found": found, "verifiable": verifiable,
        "blocking": bool(verifiable and found is False), "note": note,
    }


def trial_call_plan(case_payload: Mapping[str, Any], api_doc: Mapping[str, Any] | None) -> dict:
    """api 侧**试调**清单：列出真调时必须核验的断言点（§环节7「api=试调一遍
    （断言字段/业务码真实存在，不存在打回）」）。

    :param case_payload: 用例 payload（同 :func:`gate_required`）。
    :param api_doc: ``mc_test_api`` 行（``params`` / ``request_body`` /
        ``responses``）。**可空**——空则字段/业务码无从核，只列 status_code 类
        清单且 ``blocking=False``。

    :returns: ``{"assertions": [...], "blocking": bool}``。每个 assertion 项 =
        ``{point, kind, target, expected, found, verifiable, blocking, note}``：

        - ``kind`` ∈ ``status_code`` / ``biz_code`` / ``key_field`` /
          ``response_time`` / ``param`` / ``param_type`` / ``expected_text``；
        - ``verifiable``：契约能否判这一点（False=契约没这块信息，只能真调观测，
          不作打回依据）；
        - ``found``：契约判下来这项在不在（契约判不了时为 None）；
        - ``blocking``：静态核验判**查无此项**（``verifiable`` 且 ``found=False``）
          → 不必真调就该**打回**。顶层 ``blocking`` = 任一 item 的 ``blocking``
          为真——api_doc 为空时无从核验字段，全项不 blocking（只列 status_code
          类观测项）。

    不核的东西（免得误伤）：``response_time`` 阈值（契约无耗时口径）、送错的
    参数类型（非数字/超长本就是框架类变体的载荷，见歧义记录 4）。
    """
    payload = _as_dict(case_payload)
    doc = _as_dict(api_doc)
    expected = _as_dict(payload.get("expected"))

    has_responses = bool(_as_list(doc.get("responses")))
    statuses = _declared_statuses(doc) if has_responses else set()
    resp_fields = _declared_response_fields(doc) if has_responses else set()
    resp_leaves = {
        segs[-1] for f in resp_fields
        if (segs := _field_path_segments(f))
    }
    biz_codes = _declared_biz_codes(doc) if has_responses else set()
    param_index = _api_param_index(doc) if doc else {}

    items: list[dict] = []

    # 1) status_code
    sc = expected.get("status_code")
    if _present(sc):
        sc_str = str(sc).strip()
        if not doc:
            items.append(_item("$.expected.status_code", "status_code", "status_code", sc,
                               verifiable=False,
                               note="无 api_doc，状态码只能靠真调观测（不据此打回）"))
        elif statuses:
            hit = sc_str in statuses or f"{sc_str[:1]}xx" in {s.lower() for s in statuses}
            items.append(_item(
                "$.expected.status_code", "status_code", "status_code", sc,
                verifiable=True, found=hit,
                note=("契约已声明该状态码" if hit
                      else f"契约 responses 未声明 {sc_str}（已声明：{sorted(statuses)}）→ 打回"),
            ))
        else:
            items.append(_item("$.expected.status_code", "status_code", "status_code", sc,
                               verifiable=False,
                               note="responses 为空，状态码无法静态核验"))

    # 2) biz_code（业务码真实存在）
    bc = expected.get("biz_code")
    if _present(bc):
        if not doc or not biz_codes:
            items.append(_item("$.expected.biz_code", "biz_code", "biz_code", bc,
                               verifiable=False,
                               note="契约未声明业务码取值，须真调观测该码是否真实存在"))
        else:
            vals = bc if isinstance(bc, (list, tuple, set)) else [bc]
            for i, one in enumerate(vals):
                point = "$.expected.biz_code" if isinstance(bc, str) else f"$.expected.biz_code[{i}]"
                hit = str(one).strip() in biz_codes
                items.append(_item(point, "biz_code", "biz_code", one,
                                   verifiable=True, found=hit,
                                   note=("契约里查得到该业务码" if hit
                                         else "契约里查不到该业务码 → 打回")))

    # 3) key_fields（断言字段真实存在）
    kf = expected.get("key_fields")
    if _present(kf):
        names: list[str] = []
        if isinstance(kf, Mapping):
            names = [str(k) for k in kf.keys() if str(k).strip()]
            nested_vals = [v for v in kf.values() if isinstance(v, Mapping)]
            for nv in nested_vals:
                names.extend(str(k) for k in nv.keys() if str(k).strip())
        elif isinstance(kf, (list, tuple, set)):
            for one in kf:
                if isinstance(one, Mapping):
                    nm = one.get("field") or one.get("name") or one.get("path")
                    if nm:
                        names.append(str(nm))
                elif isinstance(one, str) and one.strip():
                    names.extend(s for s in one.replace("，", ",").split(",") if s.strip())
        elif isinstance(kf, str):
            names = [s.strip() for s in kf.replace("，", ",").split(",") if s.strip()]
        for name in sorted(set(names)):
            segs = _field_path_segments(name)
            point = f"$.expected.key_fields.{name}"
            if not doc or not resp_fields:
                items.append(_item(point, "key_field", name, name,
                                   verifiable=False,
                                   note="无响应契约，字段是否存在须真调观测"))
            else:
                hit = (name in resp_fields
                       or bool(segs) and (segs[-1] in resp_fields
                                          or segs[-1] in resp_leaves))
                items.append(_item(point, "key_field", name, name,
                                   verifiable=True, found=hit,
                                   note=("断言字段在契约响应里存在" if hit
                                         else "断言字段在契约响应里不存在 → 打回")))

    # 4) response_time（契约无耗时口径 → 只记录）
    rt = expected.get("response_time")
    if _present(rt):
        items.append(_item("$.expected.response_time", "response_time", "response_time", rt,
                           verifiable=False,
                           note="耗时阈值非契约可定，真调观测即可，不作打回依据"))

    # 5) security 端断言字段（reject_code/audit_trail/data_invisible）—— 只列不核
    emitted_expected: set[str] = set()
    for key in _SECURITY_EXPECTED_KEYS:
        if _present(expected.get(key)):
            emitted_expected.add(key)
            items.append(_item(f"$.expected.{key}", "expected_text", key, expected.get(key),
                               verifiable=False,
                               note="安全结果断言（拒绝码/审计/不可见），须真调观测"))

    # 6) expected 里的自由文本（非该端契约字段、未在 5) 列过）——只列出，不核
    known = set(SURFACE_ASSERTION_FIELDS.get(_norm(payload.get("surface")) or API_SURFACE, ()))
    for key, val in expected.items():
        k = str(key)
        if k in known or k in emitted_expected or not _present(val):
            continue
        if isinstance(val, str):
            items.append(_item(f"$.expected.{k}", "expected_text", k, val,
                               verifiable=False,
                               note="推测断言原文，试调时逐条比对"))

    # 7) 请求参数：参数名必须在契约里（不存在→打回）；类型只记录
    referenced = _referenced_params(payload)
    for point, name, value in referenced:
        if not doc or not param_index:
            items.append(_item(f"{point}(name)", "param", name, value,
                               verifiable=False,
                               note="无 api_doc，参数名无法核契约"))
        else:
            hit = name in param_index
            items.append(_item(f"{point}(name)", "param", name, None,
                               verifiable=True, found=hit,
                               note=("参数名在契约 params/request_body 里" if hit
                                     else "参数名不在契约 params/request_body 里 → 打回")))
            declared = param_index.get(name) or ""
            if declared and value is not None and not _norm(value).startswith("{{"):
                items.append(_item(f"{point}(type)", "param_type", name, value,
                                   verifiable=True, found=None,
                                   note=f"契约类型 {declared}，实送值 {_norm(value)[:40]}"
                                        "（类型不符属框架变体载荷，不打回）"))

    return {
        "assertions": items,
        "blocking": any(one["blocking"] for one in items),
    }


# ── 4) 待审变更包 ─────────────────────────────────────────────


def _canonical(value: Any) -> str:
    """规范化 JSON（键排序、紧凑分隔）——包号与 item 去重都用它。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _normalize_event(event: Any) -> dict | None:
    """挂的段事件 → 变更包 item（畸形项返回 None，由调用方剔掉）。"""
    if isinstance(event, str):
        event = {"reason": event}
    if not isinstance(event, Mapping):
        return None
    seq = event.get("step_seq", event.get("seq", event.get("step")))
    try:
        step_seq: Any = int(seq)
    except (TypeError, ValueError):
        step_seq = str(seq).strip() if seq not in (None, "") else None
    evidence: list[str] = []
    for key in ("evidence", "screenshot", "screenshots", "trace", "artifacts"):
        for one in _as_list(event.get(key)):
            text = str(one).strip()
            if text and text not in evidence:
                evidence.append(text)
    item = {
        "case_id": str(event.get("case_id") or "").strip(),
        "scenario_id": str(event.get("scenario_id") or "").strip(),
        "step_seq": step_seq,
        "reason": str(event.get("reason") or event.get("message")
                      or event.get("error") or event.get("detail") or "").strip(),
        "failure_kind": str(event.get("failure_kind") or "").strip(),
        "evidence": evidence,
        "suggestion": str(event.get("suggestion") or event.get("proposed_change")
                          or event.get("rewrite") or "").strip(),
    }
    if not any(item.values()):
        return None
    return item


def change_package(events: Sequence[Mapping] | None) -> dict:
    """探索中**挂的段**攒成事件 → **待审变更包**（§环节7：挂的段攒成事件→离线
    批量重写→待审变更包人签；**不做运行时自愈**）。

    :param events: 事件序列（每项认 ``case_id`` / ``scenario_id`` /
        ``step_seq``(``seq``) / ``reason``(``message``/``error``) /
        ``failure_kind`` / ``evidence``(``screenshot``) / ``suggestion``；
        纯字符串事件按 ``reason`` 收）。

    :returns: ``{"package_id": str, "items": [...], "count": int,
        "requires_human_sign": True}``。

    纯函数口径：

    - ``package_id`` = ``"chg-"`` + 规范化 item 集合（**不含序号**）的
      sha1 前 16 位——不生成随机 id；同一批事件无论到达顺序、重放多少次，
      包号一致（幂等），完全重复的事件只留一条；
    - ``items`` 保持传入顺序并补 ``seq``（从 1 连续）供人签时逐条对照；
    - ``requires_human_sign`` 恒为 ``True``：批量重写产物是**待审**变更包，
      落库前人签（0 条也照样置 True，由调用方按 ``count`` 决定是否起弹框）。
    """
    items: list[dict] = []
    seen: set[str] = set()
    for raw in events or ():
        item = _normalize_event(raw)
        if item is None:
            continue
        fingerprint = _canonical(dict(item, seq=None))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        items.append(item)
    for i, item in enumerate(items, 1):
        item["seq"] = i
    digest = hashlib.sha1(
        _canonical(sorted(seen)).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "package_id": f"chg-{digest}",
        "items": items,
        "count": len(items),
        "requires_human_sign": True,
    }
