"""覆盖矩阵（18 号文 §3.4 / 治 17 号文断点 5：coverage_gaps 零写入、漏没漏无法度量）。

**纯函数**——输入「用例的 axis 记录」（从 ``mc_test_case.confirm_ref`` 抽出的
``{module, axis}``），输出 ``module × C01-C14`` 矩阵 + 缺口清单。不落库、不查 DB：
查询在能力面方法（``coverage.report``）做，聚合在这里，跨库（PG/SQLite）一致。

适用轴判定：``COVERAGE_AXES[].applies_to`` 含 ``api`` 的轴，接口基准下「应有」；
骨架枚举天然覆盖 api 基准的机械轴（C01/C02/C03/C04/C09/C10/C11/C12/C14），
其余（并发 C05/重试 C06/审计 C07/会话 C08/状态 C13）多为业务流轴，
非接口机械枚举能覆盖——它们算「应有未覆盖」时会误报，故按 ``applies_to`` 分档：
- ``api`` 档：接口基准必须有（缺=真漏）
- 仅 ``flow``/``rule`` 档：接口矩阵不判缺（那是流程/规则基准的事）
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .transform_table import COVERAGE_AXES

#: 全部轴码
AXIS_CODES: tuple[str, ...] = tuple(a["code"] for a in COVERAGE_AXES)

#: 接口基准「适用且有机械覆盖预期」的轴——applies_to 含 api 者。
#: 只有这些轴在「缺」时才算真缺口；纯 flow/rule 轴不因接口矩阵没覆盖而报缺。
API_AXES: tuple[str, ...] = tuple(
    a["code"] for a in COVERAGE_AXES if "api" in a["applies_to"]
)

#: 机械枚举天然产出的轴（骨架里必然出现；出现不了=枚举器 bug 或该接口无参数）。
#: 用于区分「枚举器该覆盖但没覆盖」(漏) vs 「本就需要业务流」(不适用接口)。
MECHANICAL_API_AXES: tuple[str, ...] = ("C01", "C03", "C04", "C11", "C12", "C14")

#: 轴码 → 中文名（报表展示用）
AXIS_NAME: dict[str, str] = {a["code"]: a["name"] for a in COVERAGE_AXES}


def build_matrix(
    axis_records: Iterable[Mapping[str, Any]],
    *,
    modules: Iterable[str] | None = None,
) -> dict[str, Any]:
    """把 ``[{module, axis}]`` 聚合成 ``module × axis`` 覆盖矩阵 + 缺口。

    :param axis_records: 每条含 ``module``（分组）+ ``axis``（C01..C14）。
        来自 ``coverage.report`` 从 ``mc_test_case`` 抽出的记录。
    :param modules: 参与统计的模块全集（不传=仅从记录里出现的模块）。
        传了则「某个模块一条用例都没」也能显式报全缺——否则空模块会隐身。
    :returns: ``{matrix, gaps, covered_rate, modules}``——
        ``matrix[module][axis]`` = 该模块该轴的用例数；
        ``gaps`` = 接口基准适用但计数为 0 的 ``(module, axis)`` 清单；
        ``covered_rate`` = 已覆盖的接口适用格 / 总适用格。
    """
    matrix: dict[str, dict[str, int]] = {}
    seen_modules: set[str] = set()

    for rec in axis_records:
        module = str(rec.get("module") or "") or "（未分组）"
        axis = str(rec.get("axis") or "")
        if axis not in AXIS_CODES:
            continue
        seen_modules.add(module)
        bucket = matrix.setdefault(module, dict.fromkeys(AXIS_CODES, 0))
        bucket[axis] += 1

    # 显式补全传入但未出现的模块（全 0，让「一条都没」也可见）
    for m in modules or []:
        matrix.setdefault(str(m), dict.fromkeys(AXIS_CODES, 0))
        seen_modules.add(str(m))

    # 缺口：接口适用轴（API_AXES）里计数为 0 的格。
    # 但只对「机械枚举该覆盖」的轴判缺（MECHANICAL_API_AXES）——
    # C05 并发/C09 一致性等虽 applies_to 含 api，机械枚举产不出，不该报缺。
    gap_axes = [a for a in API_AXES if a in MECHANICAL_API_AXES]
    gaps: list[dict[str, str]] = []
    total_cells = 0
    covered_cells = 0
    for module in sorted(matrix):
        for axis in gap_axes:
            total_cells += 1
            if matrix[module][axis] > 0:
                covered_cells += 1
            else:
                gaps.append({"module": module, "axis": axis, "axis_name": AXIS_NAME[axis]})

    return {
        "matrix": matrix,
        "gaps": gaps,
        "covered_rate": round(covered_cells / total_cells, 4) if total_cells else 1.0,
        "modules": sorted(matrix),
        "gap_axes": gap_axes,
    }


__all__ = ["AXIS_CODES", "API_AXES", "MECHANICAL_API_AXES", "AXIS_NAME", "build_matrix"]
