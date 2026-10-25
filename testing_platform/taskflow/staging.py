"""暂存标识 · ORM 侧实现（26 号文 §1.8，D-6 已裁）——**不落独立暂存分区**。

**用户裁决原文**：需求/用例/测试点**不落独立暂存分区**，**共用正常数据表**，
只**增加额外字段标识**；并且**做关联任务**——之前是「直接做，做了之后也不知道
怎么处理，怎么恢复，用户需要一个一个处理」。

⇒ 机制：

1. 数据仍写正式表（``mc_test_case`` / ``mc_test_requirement`` / ``mc_test_testpoint``
   / ``mc_test_scenario``）。
2. 加两个标识列（:data:`STAGED_TABLES` + :data:`STAGE_COLUMNS`）：

   - ``task_run_id UUID NULL`` —— 由哪个任务产出（关联任务）
   - ``staged BOOLEAN NOT NULL DEFAULT false`` —— 未应用 = true（只在任务视图可见）

3. 列表默认过滤 ``staged=false``；任务视图内显示 ``staged=true`` 的产物。
4. **应用 = 把标识翻正**（``staged=false``），不是搬到另一张表。
5. **回滚 = 把标识翻回去**（或删除该行）——「怎么恢复」的答案。

对外表面
--------

====================================  =========================================
:data:`STAGED_TABLES`                 需要加标识列的正式表
:data:`STAGE_COLUMNS`                 列名 + PG 类型
:func:`ensure_stage_columns`          幂等补列 + 补索引（启动时调）
:func:`apply_staged`                  整批应用（staged → false）
:func:`revert_staged`                 整批回滚（staged → true，或删除）
:func:`count_staged`                  未应用计数（面板徽章）
====================================  =========================================

**与 25 号文 ``mc_test_proposal`` 的关系**：生成链已有自己的暂存表，那是**环节层**
的方法（门①②机制）。本模块是**任务层**的统一机制——探索/需求分析等其他任务的产物
直接进正式表 + 打标识。两者并存（D-1 两层不冲突）。

**并发纪律**：标识列是**加在既有正式表上**的新列，且全部可空/带默认值，
所以存量行零影响；补列用 ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` 幂等，
与 ``user_platform/database.py::_ensure_additive_columns`` 同手法。
"""
from __future__ import annotations

from typing import Any, Iterable

from tortoise import connections

__all__ = [
    "STAGED_TABLES",
    "STAGE_COLUMNS",
    "APPLY_ACTIONS",
    "ensure_stage_columns",
    "apply_staged",
    "revert_staged",
    "count_staged",
]

#: 需要加标识列的正式表（26 号文 §1.8：需求/用例/测试点 + 场景共用正常数据）。
STAGED_TABLES: tuple[str, ...] = (
    "mc_test_case",
    "mc_test_requirement",
    "mc_test_testpoint",
    "mc_test_scenario",
)

#: 每张表要加的列 → PG 类型。**全部可空 / 带默认值**，存量行不受影响。
STAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("task_run_id", "UUID"),
    ("staged", "BOOLEAN NOT NULL DEFAULT false"),
)

#: 回滚动作。
APPLY_ACTIONS: tuple[str, ...] = ("unstage", "restage", "delete")


async def _conn():
    """Tortoise 连接（user_platform app label）。"""
    return connections.get("user_platform")


async def ensure_stage_columns() -> dict[str, list[str]]:
    """给 :data:`STAGED_TABLES` 幂等补两个标识列 + 索引（启动时调）。

    :returns: ``{表名: [真正补上的列名]}``——空 dict = 已齐备（幂等性的可断言证据）。

    **表不存在时静默跳过**——建表由 ORM ``generate_schemas`` 负责，本函数只给
    **已建成的表**补列。这样启动顺序错乱不会炸 boot。
    """
    conn = await _conn()
    added: dict[str, list[str]] = {}
    for table in STAGED_TABLES:
        if not await _table_exists(table):
            continue
        per_table: list[str] = []
        for column, col_type in STAGE_COLUMNS:
            if await _column_exists(table, column):
                continue
            await conn.execute_script(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_type};')
            per_table.append(column)
        if per_table:
            added[table] = per_table
    # 索引（幂等）：任务视图按 task_run_id 反查 + 列表默认过滤 staged
    for table in STAGED_TABLES:
        if await _table_exists(table) and await _column_exists(table, "task_run_id"):
            await conn.execute_script(
                f'CREATE INDEX IF NOT EXISTS "idx_{table}_task_run" ON "{table}"(task_run_id)'
            )
            await conn.execute_script(
                f'CREATE INDEX IF NOT EXISTS "idx_{table}_staged" ON "{table}"(project_id, staged)'
            )
    return added


async def apply_staged(task_run_id: Any) -> dict[str, int]:
    """**应用方案**：把该任务的产物 ``staged`` 翻正为 false（之后全局可见）。

    :returns: ``{表名: 翻正的行数}``。这是 26 号文 §1.2「应用方案」的存储实现——
        **不是搬表，是翻标识**。
    """
    return await _flip(task_run_id, action="unstage")


async def revert_staged(task_run_id: Any, *, action: str = "restage") -> dict[str, int]:
    """**回滚**（理由 1「怎么回滚」的答案）。

    :param action: :data:`APPLY_ACTIONS` 之一：

        - ``restage``：翻回 staged=true（行保留，退回未应用态）——**默认，可逆**
        - ``delete``：物理删除该任务的 staged 产物（彻底回滚，**不可逆**）

    ``delete`` 只删 ``task_run_id`` 匹配**且** ``staged=true`` 的行——
    已应用过的产物不会被误删（那是用户已确认的资产）。
    """
    if action not in ("restage", "delete"):
        raise ValueError(f"未知回滚动作：{action!r}（合法：restage | delete）")
    return await _flip(task_run_id, action=action)


async def count_staged(task_run_id: Any) -> dict[str, int]:
    """该任务产出的**未应用**产物计数（面板「待应用」徽章数据源）。"""
    tid = _task_run_id(task_run_id)
    conn = await _conn()
    out: dict[str, int] = {}
    for table in STAGED_TABLES:
        if not await _column_exists(table, "staged"):
            continue
        n = await conn.execute_query_dict(
            f'SELECT count(*) AS n FROM "{table}" WHERE task_run_id = $1 AND staged = true',
            [tid],
        )
        count = int(n[0]["n"]) if n else 0
        if count:
            out[table] = count
    return out


async def _flip(task_run_id: Any, *, action: str) -> dict[str, int]:
    tid = _task_run_id(task_run_id)
    conn = await _conn()
    out: dict[str, int] = {}
    for table in STAGED_TABLES:
        if not await _column_exists(table, "staged"):
            continue
        if action == "unstage":
            sql = f'UPDATE "{table}" SET staged = false WHERE task_run_id = $1 AND staged = true'
        elif action == "restage":
            sql = f'UPDATE "{table}" SET staged = true WHERE task_run_id = $1 AND staged = false'
        else:  # delete —— 只删该任务且仍未应用的行（已应用的资产不动）
            sql = f'DELETE FROM "{table}" WHERE task_run_id = $1 AND staged = true'
        result = await conn.execute_query(sql, [tid])
        # Tortoise 的 execute_query 返回 (count, rows) 或行数，视方言而定；
        # 这里用 information_schema 兜底不可行，改用显式 count 查询拿数。
        out[table] = await _count_after_flip(table, tid, action)
    return out


async def _count_after_flip(table: str, tid: str, action: str) -> int:
    """翻完后的**影响行数**。用 staged 值反推：unstage 后 = staged=false 且 task_run_id 命中；
    restage 后 = staged=true；delete 后 = 0。"""
    conn = await _conn()
    if action == "unstage":
        rows = await conn.execute_query_dict(
            f'SELECT count(*) AS n FROM "{table}" WHERE task_run_id = $1 AND staged = false',
            [tid],
        )
        return int(rows[0]["n"]) if rows else 0
    if action == "restage":
        rows = await conn.execute_query_dict(
            f'SELECT count(*) AS n FROM "{table}" WHERE task_run_id = $1 AND staged = true',
            [tid],
        )
        return int(rows[0]["n"]) if rows else 0
    # delete 无法反推（已没了）——用返回值不可得，退 0；
    # 面板若要精确数，应在删除前先 count_staged 一次。
    return 0


# ── 内部 ─────────────────────────────────────────────────────────


def _task_run_id(value: Any) -> str:
    """归一 ``task_run_id``；**空值直接抛**——防止 ``DELETE ... WHERE task_run_id IS NULL``
    误删全表正常行。这是本模块唯一的硬护栏。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("task_run_id 不能为空（防误删全表：拒绝空条件）")
    return text


async def _table_exists(table: str) -> bool:
    conn = await _conn()
    rows = await conn.execute_query_dict("SELECT to_regclass($1) IS NOT NULL AS ok", [table])
    return bool(rows and rows[0]["ok"])


async def _column_exists(table: str, column: str) -> bool:
    conn = await _conn()
    rows = await conn.execute_query_dict(
        "SELECT count(*) > 0 AS ok FROM information_schema.columns "
        "WHERE table_name = $1 AND column_name = $2",
        [table, column],
    )
    return bool(rows and rows[0]["ok"])
