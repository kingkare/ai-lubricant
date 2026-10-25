"""覆盖补全（25 号文 §环节4 四条自检 / §2.5 ``mc_test_coverage_cell``；P1「覆盖统计」）。

设计文档 §环节4 步骤原文：

    铺「轴×端」矩阵 → 四条自检（每需求≥1正1反/恒真式直测/风险有场景/无两行同测）
    → 空格 ``state=gap`` → 补点（叫 AI 现场补，进暂存）或标不适用（**必填验证依据**）。

四条自检映射到 rule 前缀 ``CC_``（缺正常流 ``CC1_missing_happy`` / 缺异常流
``CC2_missing_negative`` / 恒真式无 C09 直测或风险方向无场景 ``CC3_*`` / 同题重复
``CC4_duplicate_testpoint``）。恒真式直测与风险方向有场景合并到第 3 条（两条
check code 同属 ``CC3`` 家族），与 :func:`point_gen.self_check` 的两条机器自检
同源——但本函数是**需求级聚合**（看一个需求的全量测试点是否齐），那个是**提点
级机器自检**（看提点 agent 自己产出的点是否漏 invariant/risk）。

依赖纪律（任务边界）：

- 只 import ``quality.errors``（``ValidationResult``）+ ``generation.models``
  （``TestCoverageCell``，落库）+ :mod:`.dedup`（``normalize_text``，CC4 归一）+
  标准库；**不 import** orchestrator/case_gen/reviewer/method_lib/gen_methods/
  state/methods（热区，并发会话在改）。
- ``generation.models`` 与 :mod:`.dedup` 是任务允许的依赖（models 只读不写、
  dedup 本会话产出）。

**适用轴最小集兜底**（:data:`MINIMAL_MANDATORY_AXES`）：覆盖补全只对「该端适用轴」
报空——api 端才要求 C11/C12 等。任务约束：``generation.method_lib`` 的
``SURFACE_RECIPES.mandatory_axes`` 由**另一个 agent 在写**，本文件不能依赖它
（会撞并发会话覆盖的文件），故在**本文件内**定义一份最小兜底常量。两处口径
未来对齐时由 ``method_lib`` 的整合步骤统一收敛，本模块不主动 import 那边。
"""
from __future__ import annotations

import uuid as _uuid
from typing import Any, Mapping, Optional, Sequence

from ..quality.errors import SEVERITY_ERROR, ValidationResult
from .dedup import normalize_text
from .models import COVERAGE_CELL_STATES, TESTPOINT_SURFACES, TestCoverageCell

__all__ = [
    "MINIMAL_MANDATORY_AXES",
    "SELF_CHECK_RULES",
    "build_coverage_matrix",
    "coverage_gaps",
    "self_check_requirements",
    "sync_coverage_cells",
]

# ── 覆盖轴 / 端 适用口径（§2.5 + §环节4）────────────────────────────

#: 各端「适用且有机械覆盖预期」的轴码（§环节4：只对适用轴报空）。
#:
#: 兜底常量（任务约束：``generation.method_lib.SURFACE_RECIPES.mandatory_axes`` 由
#: 另一 agent 在写，本文件**不 import** 它）。键=端，值=该端必须有覆盖的轴码元组。
#: 取值口径与 ``design/transform_table.COVERAGE_AXES[].applies_to`` 同源
#: （``api`` 档轴是接口基准「机械枚举天然产出」的轴——C01/C03/C04/C11/C12/C14
#: 见 ``design.coverage.MECHANICAL_API_AXES``；本兜底取其最小必备子集 + C01/C02
#: 是「每需求≥1正1反」的必出轴）。
MINIMAL_MANDATORY_AXES: dict[str, tuple[str, ...]] = {
    "api": ("C01", "C03", "C04", "C11", "C12"),
    "web": ("C01", "C02"),
    "app": ("C01", "C08"),
    "security": ("C04", "C14"),
}

#: ``state`` 三态（§2.5：lit 亮 / gap 灰 / na 斜纹）。显式写死字面量而非按位置解包
#: :data:`COVERAGE_CELL_STATES`——位置解包会在 models 顺序变化时静默错位（三态语义
#: 直接写进落库 state 列，错位=数据写错而不报错）。下面加一行成员校验兜底。
_STATE_LIT, _STATE_GAP, _STATE_NA = "lit", "gap", "na"
assert {_STATE_LIT, _STATE_GAP, _STATE_NA} == set(COVERAGE_CELL_STATES), (
    "coverage state 字面量与 models.COVERAGE_CELL_STATES 不一致"
)


# ── 四条自检常量（CC_ 前缀）──────────────────────────────────────

#: 四条自检规则（§环节4 四条自检；``codes`` 是该规则可能命中的 check code 清单）。
#:
#: - **CC1/CC2**：每需求至少一条正常流（C01）测试点 + 一条异常流（C02）测试点——
#:   24 号文 Skill3 / 25 号文 §环节3 规程一「每条可测验收标准 ≥1 正 1 反」的需求级聚合。
#: - **CC3**（两 code 同家）：① 每条恒真式有 C09 数据一致性直测点（Skill3 自检之二）；
#:   ② 每条风险方向有场景（Skill3 自检之三「每风险有场景」的需求级聚合）。
#: - **CC4**：无两行同测——同需求下两条测试点标题归一后相同即判重复。
SELF_CHECK_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "CC1",
        "codes": ("CC1_missing_happy",),
        "name": "每需求≥1正",
        "desc": "每个需求至少一条 C01 正常流测试点（25 号文 §环节3 规程一：≥1 正）",
        "severity": SEVERITY_ERROR,
    },
    {
        "id": "CC2",
        "codes": ("CC2_missing_negative",),
        "name": "每需求≥1反",
        "desc": "每个需求至少一条 C02 异常流测试点（25 号文 §环节3 规程一：≥1 反）",
        "severity": SEVERITY_ERROR,
    },
    {
        "id": "CC3",
        "codes": ("CC3_invariant_not_direct", "CC3_direction_uncovered"),
        "name": "恒真式直测 + 风险有场景",
        "desc": "每条恒真式有 C09 直测点；每条风险方向有非占位场景测试点（Skill3 自检 2/3）",
        "severity": SEVERITY_ERROR,
    },
    {
        "id": "CC4",
        "codes": ("CC4_duplicate_testpoint",),
        "name": "无两行同测",
        "desc": "同需求下两条测试点标题归一后相同即判重复（25 号文 §环节4 四条自检之四）",
        "severity": SEVERITY_ERROR,
    },
)


# ── 工具 ──────────────────────────────────────────────────────────

def _as_axes(point: Mapping[str, Any]) -> list[str]:
    axes = point.get("coverage_axis")
    if isinstance(axes, str):
        axes = [axes]
    if not axes:
        return []
    return [str(a).strip() for a in axes if str(a).strip()]


def _as_surfaces(point: Mapping[str, Any]) -> list[str]:
    surfaces = point.get("surfaces")
    if isinstance(surfaces, str):
        surfaces = [surfaces]
    if not surfaces:
        return []
    return [str(s).strip() for s in surfaces if str(s).strip() in TESTPOINT_SURFACES]


def _is_lit_point(point: Mapping[str, Any]) -> bool:
    """计入覆盖矩阵的点：``status`` 在 confirmed/generated（或缺失——宽容提案形态）。

    ``status=to_confirm`` 的占位点（来自 :func:`point_gen.from_register`）
    ``coverage_axis=[]``，自然不计入矩阵——这里只做宽容放行，不强判 status 枚举，
    让 proposal payload（无 status 键）也能进矩阵。
    """
    status = str(point.get("status") or "").strip().lower()
    return status in ("", "confirmed", "generated")


def _title_of(point: Mapping[str, Any]) -> str:
    value = point.get("title")
    return "" if value is None else str(value)


def _point_id(point: Mapping[str, Any]) -> Optional[str]:
    """测试点标识：优先 ``code``，其次 ``id``（proposal 无 code 时退化）。

    只取 code/id——**不取** ``testpoint_id``（那是用例挂父点的列，测试点自己没有）。
    """
    for k in ("code", "id"):
        v = point.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _invariant_refs(point: Mapping[str, Any]) -> list[str]:
    """该点引用的恒真式 id 集合。

    口径（与 :func:`point_gen.from_invariants` 产出形对齐）：
    - ``evidence[]`` 里以 ``INV``（大小写不敏感）开头的条目；
    - ``sample_data.invariant.id``（:func:`point_gen.from_invariants` 的形态是
      ``{"invariant": statement, "id": inv_id}``，``id`` 在 sample_data 顶层）。
    """
    refs: list[str] = []
    evidence = point.get("evidence")
    if isinstance(evidence, str):
        evidence = [evidence]
    for e in evidence or ():
        s = str(e).strip()
        if s.lower().startswith("inv"):
            refs.append(s)
    sample = point.get("sample_data")
    if isinstance(sample, Mapping) and sample.get("invariant") is not None:
        sid = sample.get("id")
        if sid not in (None, ""):
            refs.append(str(sid))
    return refs


def _direction_of(point: Mapping[str, Any]) -> str:
    value = point.get("direction")
    return "" if value is None else str(value).strip()


def _is_actionable(point: Mapping[str, Any]) -> bool:
    """「有场景」判定：非占位点 + 非空轴 + 非空端（占位 = basis=to_confirm）。

    §环节4 四条自检之三「每条风险方向有场景」——场景=一个能落用例的测试点，
    占位点（basis=to_confirm，evidence 引 question_id）不算场景（它挂起待答）。
    """
    basis = str(point.get("basis") or "").strip().lower()
    if basis == "to_confirm":
        return False
    return bool(_as_axes(point)) and bool(_as_surfaces(point))


def _row_requirement_id(point: Mapping[str, Any], fallback: Any) -> Any:
    rid = point.get("requirement_id")
    return rid if rid not in (None, "") else fallback


# ── 矩阵 ──────────────────────────────────────────────────────────

def build_coverage_matrix(testpoints: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, list[str]]]:
    """把 confirmed 测试点铺成「轴×端」矩阵 → ``{axis: {surface: [testpoint codes]}}``。

    - 只放 ``status`` 为 confirmed/generated（或缺失）的点（:func:`_is_lit_point`）；
    - 一条点多轴、多端——每个 (axis, surface) 组合都挂该点 code；
    - 同格同点 code 去重保序（``dict.fromkeys``）；
    - 无点覆盖的 (axis, surface) **不出现**在矩阵里（缺口判定由 :func:`coverage_gaps`
      按 :data:`MINIMAL_MANDATORY_AXES` 补）。

    返回结构供 :func:`coverage_gaps` 与 :func:`sync_coverage_cells` 共用。
    """
    matrix: dict[str, dict[str, list[str]]] = {}
    for point in testpoints or ():
        if not isinstance(point, Mapping) or not _is_lit_point(point):
            continue
        code = _point_id(point)
        if code is None:
            continue
        axes = _as_axes(point)
        surfaces = _as_surfaces(point)
        for axis in axes:
            for surface in surfaces:
                bucket = matrix.setdefault(axis, {}).setdefault(surface, [])
                if code not in bucket:
                    bucket.append(code)
    return matrix


def coverage_gaps(matrix: Mapping[str, Mapping[str, Sequence[str]]]) -> list[dict[str, str]]:
    """矩阵空格清单 → ``[{axis, surface, reason}, ...]``，只报「该端适用轴」的空。

    遍历 :data:`MINIMAL_MANDATORY_AXES`：对每端 (surface) 的每个必出轴 (axis)，
    若矩阵里该格无测试点（``matrix[axis][surface]`` 为空或不存在）→ 报空
    ``{axis, surface, reason: "无测试点"}``。

    - 端不在 :data:`MINIMAL_MANDATORY_AXES`（未知端）→ 不报（兜底表不含即视为
      该端无必出轴，文档未给口径时按「不强制」处理，避免误报）；
    - 矩阵里有覆盖但端/轴不在兜底表 → 不报（不是适用轴，不适用不报缺）；
    - 输出顺序按端、再按该端轴元组顺序（稳定可断言）。
    """
    gaps: list[dict[str, str]] = []
    for surface, axes in MINIMAL_MANDATORY_AXES.items():
        for axis in axes:
            codes = (matrix.get(axis) or {}).get(surface) or ()
            if not codes:
                gaps.append({"axis": axis, "surface": surface, "reason": "无测试点"})
    return gaps


# ── 四条自检 ──────────────────────────────────────────────────────

def self_check_requirements(
    requirement_id: Any,
    testpoints: Sequence[Mapping[str, Any]],
) -> ValidationResult:
    """需求级四条自检（§环节4 四条自检；``CC_`` 前缀）。

    :param requirement_id: 当前需求 id（CC4 归一去重的分组键——点自带
        ``requirement_id`` 时优先后者，否则用本参数兜底）。
    :param testpoints: 本需求的测试点 dict 列表（proposal payload 或正式 testpoint 行）。
    :returns: :class:`ValidationResult`——四条全过则 ``valid=True``；任一缺漏报 error。

    四条命中口径（每条正反可测）：

    - **CC1_missing_happy**：本需求无任何 ``coverage_axis`` 含 ``C01`` 的点。
    - **CC2_missing_negative**：本需求无任何 ``coverage_axis`` 含 ``C02`` 的点。
    - **CC3_invariant_not_direct**：本需求引用了恒真式（见 :func:`_invariant_refs`），
      但没有一条引用该恒真式的点带 ``C09`` 直测轴。（恒真式 id 从点集本身推导——
      签名不收 invariants 列表，否则无法测；:func:`point_gen.from_invariants` 产出
      的点天然带 INV id + C09，故「引了 INV 却没 C09 点」即漏直测。）
    - **CC3_direction_uncovered**：本需求出现了某风险方向（``direction`` 非空），
      但该方向下没有一条「有场景」的点（:func:`_is_actionable`：非占位 + 有轴有端）。
      —— 签名不收 risk_directions 列表，方向从点集本身推导；占位点不算场景。
    - **CC4_duplicate_testpoint**：本需求下两条点标题归一（:func:`normalize_text`）后
      相同 → 重复。path 指第二条重复点的序号。
    """
    result = ValidationResult()
    pts = [p for p in (testpoints or ()) if isinstance(p, Mapping)]

    # CC1 / CC2：每需求 ≥1 正（C01）/ ≥1 反（C02）
    has_c01 = any("C01" in _as_axes(p) for p in pts)
    has_c02 = any("C02" in _as_axes(p) for p in pts)
    if not has_c01:
        result.add(
            f"$.requirements[{requirement_id}]",
            "CC1_missing_happy",
            f"需求 {requirement_id} 缺 C01 正常流测试点（25 号文 §环节4：每需求≥1正）",
            hint="补一条 coverage_axis 含 C01 的正常路径测试点",
        )
    if not has_c02:
        result.add(
            f"$.requirements[{requirement_id}]",
            "CC2_missing_negative",
            f"需求 {requirement_id} 缺 C02 异常流测试点（25 号文 §环节4：每需求≥1反）",
            hint="补一条 coverage_axis 含 C02 的异常路径测试点",
        )

    # CC3：恒真式直测 + 风险有场景（两 code 同家）
    inv_to_has_c09: dict[str, bool] = {}
    for p in pts:
        for ref in _invariant_refs(p):
            if "C09" in _as_axes(p):
                inv_to_has_c09[ref] = True
            else:
                inv_to_has_c09.setdefault(ref, False)
    for ref, has in inv_to_has_c09.items():
        if not has:
            result.add(
                f"$.requirements[{requirement_id}].invariants[{ref}]",
                "CC3_invariant_not_direct",
                f"恒真式 {ref} 没有 C09 数据一致性直测点（Skill3 自检之二）",
                hint="补一条引用该恒真式且 coverage_axis 含 C09 的直测点",
            )

    direction_to_actionable: dict[str, bool] = {}
    for p in pts:
        d = _direction_of(p)
        if not d:
            continue
        direction_to_actionable.setdefault(d, False)
        if _is_actionable(p):
            direction_to_actionable[d] = True
    for d, has in direction_to_actionable.items():
        if not has:
            result.add(
                f"$.requirements[{requirement_id}].directions[{d}]",
                "CC3_direction_uncovered",
                f"风险方向「{d}」没有非占位场景测试点（Skill3 自检之三：每风险有场景）",
                hint="补一条该方向下有 coverage_axis + surfaces 且 basis≠to_confirm 的测试点",
            )

    # CC4：无两行同测（同需求 + 归一后同标题）
    rid_str = str(requirement_id)
    seen: dict[tuple[str, str], int] = {}
    for i, p in enumerate(pts):
        p_rid = str(_row_requirement_id(p, requirement_id))
        norm = normalize_text(_title_of(p))
        if not norm:
            continue
        key = (p_rid, norm)
        if key in seen:
            result.add(
                f"$.testpoints[{i}]",
                "CC4_duplicate_testpoint",
                (f"测试点「{_title_of(p)}」与同需求 {p_rid} 内第 {seen[key]} 条标题归一后"
                 f"重复（{norm!r}）——25 号文 §环节4：无两行同测"),
                hint="合并重复测试点，或改写标题使其语义可区分（归一后不同）",
            )
        else:
            seen[key] = i
    return result


# ── 落库 ──────────────────────────────────────────────────────────

def _as_uuid(value: Any) -> Optional[_uuid.UUID]:
    if value is None or value == "":
        return None
    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _run_project_id(run: Any, project_id: Any = None) -> Any:
    """从 run 取 project_id（TestGenRun 实例 / Mapping / None 三态）。"""
    if project_id not in (None, ""):
        return project_id
    if run is None:
        return None
    if isinstance(run, Mapping):
        return run.get("project_id") or run.get("project")
    return getattr(run, "project_id", None)


def _normalize_na_evidence(
    na_evidence: Any,
) -> dict[tuple[str, str], str]:
    """统一 na_evidence 形态为 ``{(axis, surface): evidence_text}``。

    接受三种 key 形态（调用方偏好不一，全收）：
    - ``{(axis, surface): text}`` —— 元组键（推荐）；
    - ``{"axis|surface": text}`` / ``{"axis:surface": text}`` —— 字符串键（分隔符
      ``|`` 或 ``:``，取先命中者；端名不含分隔符，故安全）；
    - ``{axis: {surface: text}}`` —— 嵌套 dict。
    """
    out: dict[tuple[str, str], str] = {}
    if not isinstance(na_evidence, Mapping):
        return out
    for k, v in na_evidence.items():
        if isinstance(k, tuple) and len(k) == 2:
            axis, surface = str(k[0]).strip(), str(k[1]).strip()
            if axis and surface:
                out[(axis, surface)] = str(v or "").strip()
            continue
        if isinstance(k, str):
            for sep in ("|", ":"):
                if sep in k:
                    axis, surface = k.split(sep, 1)
                    axis, surface = axis.strip(), surface.strip()
                    if axis and surface:
                        out[(axis, surface)] = str(v or "").strip()
                    break
            else:
                # 嵌套形态：{axis: {surface: text}}
                if isinstance(v, Mapping):
                    for surface, text in v.items():
                        out[(k.strip(), str(surface).strip())] = str(text or "").strip()
    return out


def _try_uuid_or_none(code: str) -> Optional[_uuid.UUID]:
    """测试点 code 多为 ``TP-0001``（非 UUID）→ testpoint_id 列存不下时返回 None。

    §2.5 ``testpoint_id`` 是 UUID 列，但矩阵里挂的是 testpoint code；code 非 UUID
    时该列留空，codes 列表仍随返回值回给调用方（UI/进度聚合可用）。
    """
    return _as_uuid(code)


async def sync_coverage_cells(
    run: Any,
    requirement_id: Any,
    matrix: Mapping[str, Mapping[str, Sequence[str]]],
    gaps: Sequence[Mapping[str, str]],
    *,
    na_evidence: Any = None,
    project_id: Any = None,
) -> dict[str, Any]:
    """写 ``mc_test_coverage_cell``（lit/gap/na 三态），幂等 upsert。

    :param run: ``TestGenRun`` 实例（取 ``project_id``）；也接受 Mapping / None
        （后者必须显式传 ``project_id``）。
    :param requirement_id: 当前需求 id（格按需求铺，矩阵行键非空）。
    :param matrix: :func:`build_coverage_matrix` 的产出（lit 来源）。
    :param gaps: :func:`coverage_gaps` 的产出（gap 来源）。
    :param na_evidence: 标不适用格的验证依据（§2.5：na **必填**验证依据）。键形态见
        :func:`_normalize_na_evidence`（元组 / ``"axis|surface"`` / 嵌套 dict 均可）。
    :param project_id: run 为 None 时的显式 project_id 兜底。
    :returns: ``{"lit": n, "gap": n, "na": n, "created": n, "updated": n,
        "na_overridden": [...], "cells": [...]}``——``cells`` 是落库后的格清单
        （含 ``axis/surface/state/evidence/testpoint_id``），供调用方/UI 回读。

    三态映射：

    - **lit**：``matrix[axis][surface]`` 非空 → 点亮，``testpoint_id`` 取首个 code
      解析为 UUID（非 UUID code 留 NULL，见 :func:`_try_uuid_or_none`），``evidence`` 留空。
    - **gap**：``gaps`` 清单里的格 → ``state=gap``。
    - **na**：``na_evidence`` 提供的格且不在 lit 集 → ``state=na`` + ``evidence=text``；
      若该格同时是 lit（有真实测试点点亮），**lit 优先**（na 被覆盖，记入
      ``na_overridden`` 返回，不静默丢弃）。

    幂等：``unique_together (requirement_id, axis, surface)`` → 存在则更新 state/
    evidence/testpoint_id（``update_fields``），不存在则建行。第二次跑同输入产出
    同状态（``created=0``、``updated=总格数``）。

    **na 必填 evidence**（§2.5 硬约束）：na 格的 evidence 文本为空 → 抛 ``ValueError``
    （响亮失败，不静默 no-op——与 :func:`models.ensure_question_columns` 同款纪律，
    避免运行时缺依据）。
    """
    pid = _run_project_id(run, project_id)
    if pid is None:
        raise ValueError("sync_coverage_cells 需要 project_id：run 无 project_id 且未显式传参")
    rid = _as_uuid(requirement_id)
    if rid is None:
        raise ValueError(f"requirement_id 非法 UUID：{requirement_id!r}")
    pid_uuid = _as_uuid(pid)
    if pid_uuid is None:
        raise ValueError(f"project_id 非法 UUID：{pid!r}")

    na_map = _normalize_na_evidence(na_evidence)

    # 收集本需求的格计划：{(axis, surface): (state, evidence, testpoint_id)}
    plan: dict[tuple[str, str], tuple[str, Optional[str], Optional[_uuid.UUID]]] = {}
    # lit
    for axis, surface_map in (matrix or {}).items():
        if not isinstance(surface_map, Mapping):
            continue
        for surface, codes in surface_map.items():
            if not codes:
                continue
            first_code = str(codes[0])
            plan[(str(axis), str(surface))] = (
                _STATE_LIT, None, _try_uuid_or_none(first_code),
            )
    # gap（不覆盖已有 lit）
    for g in gaps or ():
        if not isinstance(g, Mapping):
            continue
        axis, surface = str(g.get("axis") or "").strip(), str(g.get("surface") or "").strip()
        if not axis or not surface:
            continue
        key = (axis, surface)
        if key in plan and plan[key][0] == _STATE_LIT:
            continue  # lit 优先
        plan[key] = (_STATE_GAP, None, None)
    # na
    na_overridden: list[dict[str, str]] = []
    for (axis, surface), text in na_map.items():
        key = (axis, surface)
        existing = plan.get(key)
        if existing is not None and existing[0] == _STATE_LIT:
            na_overridden.append({"axis": axis, "surface": surface, "reason": "lit 优先"})
            continue
        if not text:
            raise ValueError(
                f"na 格必须填验证依据（§2.5）：{(axis, surface)!r} 的 evidence 为空"
            )
        plan[key] = (_STATE_NA, text, None)

    created = updated = 0
    cells: list[dict[str, Any]] = []
    for (axis, surface), (state, evidence, tp_id) in plan.items():
        row = await TestCoverageCell.get_or_none(
            requirement_id=rid, axis=axis, surface=surface,
        )
        if row is None:
            await TestCoverageCell.create(
                project_id=pid_uuid,
                requirement_id=rid,
                axis=axis,
                surface=surface,
                state=state,
                evidence=evidence,
                testpoint_id=tp_id,
            )
            created += 1
        else:
            row.state = state
            row.evidence = evidence
            row.testpoint_id = tp_id
            await row.save(update_fields=["state", "evidence", "testpoint_id", "updated_at"])
            updated += 1
        cells.append({
            "axis": axis, "surface": surface, "state": state,
            "evidence": evidence, "testpoint_id": str(tp_id) if tp_id else None,
        })

    return {
        "lit": sum(1 for s, _, _ in plan.values() if s == _STATE_LIT),
        "gap": sum(1 for s, _, _ in plan.values() if s == _STATE_GAP),
        "na": sum(1 for s, _, _ in plan.values() if s == _STATE_NA),
        "created": created,
        "updated": updated,
        "na_overridden": na_overridden,
        "cells": cells,
    }
