"""骨架用例落库（17 号文 L1：机械枚举 → ``mc_test_case`` 批量写入）。

**L1 的职责边界**：读 ``mc_test_api`` 行 → :func:`design.transform_table.enumerate_api`
产 slot → 批量落 ``mc_test_case``（``slot_key`` 幂等，冲突即跳过）。**0 token**——
不调 LLM、不调 AI 语义补全（那是 L2 的批队列，另行接线）。

幂等三保险：
1. ``slot_key`` 部分唯一索引（新库走 models DDL；存量库 :func:`ensure_slot_index` 惰性补）；
2. ``bulk_create(ignore_conflicts=True)``——重跑生成 = created=0 全 skip；
3. 批量发号（号段一次取 max + 内存自增），不逐条捞全表（治 17 号文断点 3 的 O(n²)）。

本模块**只 import models 与 design**，不 import service/methods——那两个文件
正被并发会话改（2026-10-13），保持零接触。
"""
from __future__ import annotations

import uuid as _uuid
from typing import Any, Iterable, Mapping

from ..assets.models import TestCase
from . import transform_table

#: 骨架用例的固定取值（机械点：依据=接口契约本身，confirm_source=doc）
_SKELETON_CONFIRM_SOURCE = "doc"
_SKELETON_ORIGIN = "ai_generated"
_SKELETON_STATUS = "active"  # 机械点不经 AI 确认门（18 号文 §8：自动 active，进抽检队列）
_SKELETON_AUTOMATION = "not_automated"


async def _column_exists(conn: Any, table: str, column: str) -> bool:
    """跨库探测列是否存在（照 projects/service.py 模式：PG information_schema，SQLite PRAGMA）。"""
    try:
        rows = await conn.execute_query_dict(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            [table, column],
        )
        return bool(rows)
    except Exception:  # noqa: BLE001 — SQLite 没有 information_schema
        rows = await conn.execute_query_dict(f"PRAGMA table_info({table})")
        return any(r.get("name") == column for r in rows)


async def ensure_slot_index(conn: Any) -> None:
    """存量库补 ``slot_key`` 列 + 部分唯一索引（幂等）。

    新库由 models DDL 直接建齐；这里管「表已存在、列/索引后补」的存量路径。
    启动路径的 ``_ensure_additive_columns`` 会按模型补列（通用扫描），本函数
    在枚举调用前再兜一次底（自包含，不依赖启动顺序）。
    """
    if not await _column_exists(conn, "mc_test_case", "slot_key"):
        await conn.execute_script("ALTER TABLE mc_test_case ADD COLUMN slot_key VARCHAR(256);")
    await conn.execute_script(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_test_case_slot_key "
        "ON mc_test_case (project_id, slot_key) WHERE slot_key IS NOT NULL;"
    )


def _slot_to_case_row(
    *,
    project_id: Any,
    api: Mapping[str, Any],
    slot: Mapping[str, Any],
    code: str,
    module: str | None,
) -> dict[str, Any]:
    """一个 slot → mc_test_case 插入行（骨架字段预填，语义差量留给 L2）。"""
    api_id = str(api.get("id"))
    method = api.get("method", "")
    path = api.get("path", "")
    param = slot.get("param") or ""
    is_universal = param in ("__auth__", "__idem__")

    title = f"{method} {path} · {slot['label']}" + ("" if is_universal else f"（{param}）")
    expected = {
        "status_code": slot["expect_status"],   # 三层断言第 1 层（机器可判）
        "biz_code": None,                       # 第 2/3 层留 L2/AI/规则补
        "key_fields": [],
    }
    return {
        "code": code,
        "project_id": project_id,
        "title": title[:512],
        "intent": (
            f"机械枚举（变换表）：{method} {path} 参数 {param} 变体 {slot['variant']}"
            if not is_universal
            else f"机械枚举（通用轴）：{method} {path} {slot['label']}"
        ),
        "preconditions": {},
        "expected": expected,
        "steps": [],
        "confirm_source": _SKELETON_CONFIRM_SOURCE,
        "confirm_ref": {
            "api_id": api_id,
            "slot": slot["variant"],
            "axis": slot["axis"],
            "basis": "transform_table",
        },
        "origin": _SKELETON_ORIGIN,
        "priority": "P0" if slot["axis"] in ("C04", "C14") else None,
        "module": module or api.get("name") or None,
        "test_type": _axis_to_test_type(slot["axis"]),
        "slot_key": slot["key"],
        "automation_status": _SKELETON_AUTOMATION,
        "status": _SKELETON_STATUS,
    }


def _axis_to_test_type(axis: str) -> str:
    """覆盖轴 → 用例 test_type（对齐 CASE_TEST_TYPES 枚举）。"""
    return {
        "C01": "functional", "C02": "exception", "C03": "boundary",
        "C04": "permission", "C11": "functional", "C12": "boundary",
        "C14": "security",
    }.get(axis, "functional")


async def _next_code_batch(project_id: Any, count: int) -> list[str]:
    """批量发号：一次 SELECT max + 内存自增（治 O(n²)）；TC- 格式。

    并发竞态由 (project_id, code) 唯一约束兜底——撞号行随 ignore_conflicts 丢弃，
    下次枚举重补（slot_key 幂等保证不丢 slot）。
    """
    rows = await TestCase.filter(project_id=project_id).order_by("-code").limit(1).values("code")
    base = 0
    if rows:
        try:
            base = int(rows[0]["code"].replace("TC-", "") or 0)
        except ValueError:
            base = 0
    return [f"TC-{base + i + 1:04d}" for i in range(count)]


async def enumerate_skeleton_cases(
    *,
    project_id: Any,
    api_rows: Iterable[Mapping[str, Any]],
    created_by: Any,
) -> dict[str, Any]:
    """把一组 ``mc_test_api`` 行枚举成骨架用例并幂等落库。

    :param api_rows: 已查询好的接口行（dict 形状；调用方负责查询与过滤——
        本模块不 import methods/service，保持零接触）。
    :returns: ``{created, skipped, axes, per_api: [{api_id, slots}]}``。

    重跑验收（17 号文 §6 P0）：第二次调用 ``created=0, skipped=全部``。
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await ensure_slot_index(conn)  # 存量库自愈（幂等）

    # ① 全量枚举 + 查已存在的 slot_key（一次查，避免逐条 exists）
    plan: list[dict[str, Any]] = []
    axes_total: dict[str, int] = {}
    per_api_counts: list[tuple[Mapping[str, Any], int]] = []
    for api in api_rows:
        result = transform_table.enumerate_api(api)
        per_api_counts.append((api, len(result["slots"])))
        module = api.get("folder_name") or api.get("name")
        for slot in result["slots"]:
            plan.append(
                _slot_to_case_row(
                    project_id=project_id, api=api, slot=slot,
                    code="", module=module,  # code 批量后补
                )
            )
            axes_total[slot["axis"]] = axes_total.get(slot["axis"], 0) + 1

    if not plan:
        return {"created": 0, "skipped": 0, "axes": {}, "per_api": []}

    keys = [row["slot_key"] for row in plan]
    existing = set(
        await TestCase.filter(project_id=project_id, slot_key__in=keys).values_list("slot_key", flat=True)
    )

    # ② 过滤已有 → 只插新的（ignore_conflicts 双保险：并发枚举撞索引也不炸）
    fresh = [row for row in plan if row["slot_key"] not in existing]
    skipped = len(plan) - len(fresh)

    created = 0
    if fresh:
        codes = await _next_code_batch(project_id, len(fresh))
        for row, code in zip(fresh, codes):
            row["code"] = code
            row.setdefault("created_by", created_by)
        # 约束冲突（并发撞号/撞 slot）静默跳过——幂等语义的一部分
        await TestCase.bulk_create(
            [TestCase(**row) for row in fresh], ignore_conflicts=True, batch_size=200
        )
        created = len(fresh)

    return {
        "created": created,
        "skipped": skipped,
        "axes": axes_total,
        "per_api": [{"api_id": str(api.get("id")), "slots": n} for api, n in per_api_counts],
    }


__all__ = ["ensure_slot_index", "enumerate_skeleton_cases"]
