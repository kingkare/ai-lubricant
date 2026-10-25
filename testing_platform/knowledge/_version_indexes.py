"""知识表版本维度的**部分唯一索引**（versioned + legacy 两条）。

为什么不用 ``UNIQUE(...)`` / ``unique_together``：PG 的 UNIQUE 约束把 NULL 当作
互不相等——``version_id`` 可空（存量图谱数据未绑版本），普通复合唯一约束既
管不了「同版本内去重」（version_id 非空时其实可以），也管不了「NULL 版本内去重」
（version_id 为空时约束不生效，等于没有）。两条部分索引各管一段，才是正确语义：

* ``... WHERE version_id IS NOT NULL`` —— 同一版本内身份唯一（按版本过滤图谱的根基）
* ``... WHERE version_id IS NULL``     —— 存量遗留数据仍按老身份唯一

新库建表（DDL）与存量库迁移（:func:`ensure_version_indexes`）共用本模块同一份
索引定义，两条路径收敛到同一 schema。函数幂等：先按定义找到并删除旧的
``(project_id, ...)`` 唯一约束/索引，再 ``CREATE UNIQUE INDEX IF NOT EXISTS``。
"""
from __future__ import annotations

from typing import Any

#: ``{表: (索引名前缀, 版本化唯一列, 遗留唯一列)}``——identity 列不含 version_id。
IDENTITY: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "mc_knowledge_anchor": (
        "uq_knowledge_anchor", ("project_id", "version_id", "kind", "path", "symbol"),
        ("project_id", "kind", "path", "symbol"),
    ),
    "mc_knowledge_item": (
        "uq_knowledge_item", ("project_id", "version_id", "kind", "key"),
        ("project_id", "kind", "key"),
    ),
    "mc_knowledge_edge": (
        "uq_knowledge_edge",
        ("project_id", "version_id", "relation", "from_kind", "from_id", "to_kind", "to_id"),
        ("project_id", "relation", "from_kind", "from_id", "to_kind", "to_id"),
    ),
}

#: 供 raw DDL 追加的建索引语句（新库路径）。
INDEX_DDL: list[str] = []
for _table, (_prefix, _versioned, _legacy) in IDENTITY.items():
    INDEX_DDL.append(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_prefix}_versioned ON {_table} "
        f"({', '.join(_versioned)}) WHERE version_id IS NOT NULL"
    )
    INDEX_DDL.append(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_prefix}_legacy ON {_table} "
        f"({', '.join(_legacy)}) WHERE version_id IS NULL"
    )


def _cols(defn: str) -> set[str]:
    """从约束/索引定义里抽出列名集合（``(a, b, c)`` 括号内部分）。"""
    inner = defn[defn.find("(") + 1 : defn.rfind(")")]
    return {c.strip().strip('"').split("(")[0].strip() for c in inner.split(",")}


async def ensure_version_indexes(conn: Any) -> list[str]:
    """存量库幂等迁移：删掉旧的整表唯一约束/索引，再建两条部分索引。

    只在 ``version_id`` 列已存在时执行（缺列由 ``_ensure_additive_columns``
    先补）。返回本回实际删除的旧约束名（用于日志；空列表=无变更）。
    """
    dropped: list[str] = []
    for table, (prefix, versioned, legacy) in IDENTITY.items():
        has_col = await conn.execute_query_dict(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = 'version_id'",
            [table],
        )
        if not has_col:
            continue
        exists = await conn.execute_query_dict(
            "SELECT 1 FROM information_schema.tables WHERE table_name = $1", [table]
        )
        if not exists:
            continue
        wanted = set(legacy)
        # 1) 表约束（Tortoise 的 unique_together 当年建的就是表约束）
        constraints = await conn.execute_query_dict(
            "SELECT conname AS name, pg_get_constraintdef(oid) AS def FROM pg_constraint "
            f"WHERE conrelid = to_regclass('{table}') AND contype = 'u'"
        )
        for row in constraints:
            defn = (row.get("def") or "").lower()
            # 旧约束=不含 version_id 的那条整表唯一（versioned 约束留着无害，但按
            # 定义匹配只删 legacy 形状的，避免误删迁移后自己建的部分索引之约束形态）
            if "version_id" not in defn and _cols(row.get("def") or "") == wanted:
                await conn.execute_script(
                    f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{row["name"]}"'
                )
                dropped.append(f"{table}:{row['name']}")
        # 2) 裸唯一索引（同样可能是约束之外的遗留索引）
        indexes = await conn.execute_query_dict(
            f"SELECT indexname AS name, indexdef AS def FROM pg_indexes "
            f"WHERE tablename = '{table}' AND indexdef ILIKE 'CREATE UNIQUE INDEX%'"
        )
        for row in indexes:
            defn = (row.get("def") or "")
            if "version_id" in defn.lower():
                continue  # 迁移自己建的部分索引，别删
            if _cols(defn) == wanted:
                await conn.execute_script(f'DROP INDEX IF EXISTS "{row["name"]}"')
                dropped.append(f"{table}:{row['name']}")
        # 3) 幂等建两条部分索引
        for stmt in (
            f"CREATE UNIQUE INDEX IF NOT EXISTS {prefix}_versioned ON {table} "
            f"({', '.join(versioned)}) WHERE version_id IS NOT NULL",
            f"CREATE UNIQUE INDEX IF NOT EXISTS {prefix}_legacy ON {table} "
            f"({', '.join(legacy)}) WHERE version_id IS NULL",
        ):
            await conn.execute_script(stmt)
    return dropped


__all__ = ["IDENTITY", "INDEX_DDL", "ensure_version_indexes"]
