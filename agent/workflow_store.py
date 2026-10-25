"""Workflow definitions + runs persistence (Postgres).

A workflow belongs to a **user** (not an agent): it is an independent artifact
the user builds, imports/exports as JSON, and starts from an agent — the agent
only *triggers* a run, it is not the run host.

Two tables:

- ``workflow_defs`` — versioned graph definitions owned by ``user_id``. Every
  save inserts a NEW version row (old versions stay); runs point at
  ``(def_id, version)`` so a template edit never rewrites history. ``name`` is
  unique per owner, not globally.
- ``workflow_runs`` — one row per execution, with per-node structured state
  (status/output/error/decision/duration) written as each node finishes, so a
  crash never loses more than the in-flight node.

Large payloads are bounded here (:data:`agent.workflow.MAX_NODE_OUTPUT_CHARS`)
rather than trusted to callers.

**Restart recovery.** A run whose process died mid-flight stays ``running``
forever (no one writes its terminal state). On startup :func:`recover_interrupted_runs`
marks every such run ``interrupted`` and — when the def opted in via
``retry_on_reboot`` (default true) — re-launches it from the top with the same
args as a fresh run.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from loguru import logger

from db import PostgresClient


def _loads(value: Any) -> Any:
    """asyncpg returns JSONB as str unless a codec is registered — accept both."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# ── 家族寻址（family addressing）──────────────────────────────────────────────
#
# ``workflow_defs.id`` 是**版本行**主键：每次保存 INSERT 一行新 version（新 id），
# 同一个工作流 = (user_id, name) 的一串版本行。但 def_id 对外是「这个工作流」
# 的稳定把手（列表给最新行 id，详情页保存后行 id 又变了）。所以所有按 def_id 的
# 读写都先解析家族再取最新版本——否则 GET /workflows/{old_id} 读回旧版本行
# = 用户看到的「一点保存又回去了」。显式传 version（导出/跑指定版本）才落某行。
#
# 锚点行若已被删除，子查询返回空集，对应读返回 None / 空列表（404）。

def _family_ids_sql(anchor_ph: str) -> str:
    """同家族（user_id + name 一致）所有版本行的 id 子查询；
    ``anchor_ph`` 是取锚点行的占位符（如 ``$1``）。"""
    return (
        "SELECT d.id FROM workflow_defs d "
        "JOIN (SELECT user_id, name FROM workflow_defs WHERE id=" + anchor_ph + ") f "
        "ON d.user_id IS NOT DISTINCT FROM f.user_id AND d.name = f.name"
    )


def _latest_family_sql(anchor_ph: str) -> str:
    """同家族里**最新版本**那一行的 id 子查询；anchor_ph 是占位符。"""
    return (
        "SELECT d.id FROM workflow_defs d "
        "JOIN (SELECT user_id, name FROM workflow_defs WHERE id=" + anchor_ph + ") f "
        "ON d.user_id IS NOT DISTINCT FROM f.user_id AND d.name = f.name "
        "ORDER BY d.version DESC LIMIT 1"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Definitions
# ──────────────────────────────────────────────────────────────────────────────

async def create_def(
    name: str,
    description: str,
    definition: dict,
    user_id: Optional[str],
    label: str = "",
    retry_on_reboot: bool = True,
) -> dict:
    """Insert version 1 of a workflow owned by ``user_id``.

    Name uniqueness is scoped to the owner: two users may each own a workflow
    named the same. ``label`` is the Chinese display name (required by callers;
    stored as given).
    """
    from agent.workflow import validate_def
    # create 允许空壳（{nodes: []}）：新建表单只收名称，节点图进详情页再填。
    # 保存新版本（update_def）与运行（Runner.__init__）仍全量校验，空壳跑不起来。
    validate_def(definition, allow_empty=True)
    if not PostgresClient.pool:
        raise RuntimeError("Database not available")
    async with PostgresClient.pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM workflow_defs WHERE user_id IS NOT DISTINCT FROM $1 AND name=$2",
            user_id, name,
        )
        if exists:
            raise ValueError(f"workflow name already taken for this owner: {name!r}")
        row = await conn.fetchrow(
            "INSERT INTO workflow_defs(name, label, description, user_id, version, definition, retry_on_reboot) "
            "VALUES($1,$2,$3,$4,1,$5::jsonb,$6) RETURNING *",
            name, label, description, user_id,
            json.dumps(definition, ensure_ascii=False), bool(retry_on_reboot),
        )
    return dict(row)


async def update_def(
    def_id: int,
    definition: dict,
    description: Optional[str] = None,
    label: Optional[str] = None,
    retry_on_reboot: Optional[bool] = None,
) -> dict:
    """Save a new version (old rows stay; runs keep pointing at their version).

    新版本号按**唯一索引分组** ``MAX(version)+1``（子查询算），不能用「传入行的
    version+1」——每个版本是独立 id 的行，若前端拿着旧版本行的 id 连续点保存，会
    各自算出同一个 ``version`` 撞唯一索引。并发同理，冲突即重试一次。
    """
    from agent.workflow import validate_def
    validate_def(definition)
    if not PostgresClient.pool:
        raise RuntimeError("Database not available")
    payload = json.dumps(definition, ensure_ascii=False)
    # 新版本基于**家族最新行**派生（不基于可能已过期的传入行）：取到的 name/label/
    # description/user_id/retry_on_reboot 才是当前真相，历史版本行只留档不改。
    _SQL = (
        "INSERT INTO workflow_defs(name, label, description, user_id, version, definition, retry_on_reboot) "
        "SELECT name, COALESCE($2, label), COALESCE($3, description), user_id, "
        "       (SELECT MAX(version) FROM workflow_defs WHERE name = t.name "
        "         AND user_id IS NOT DISTINCT FROM t.user_id) + 1, $4::jsonb, "
        "       COALESCE($5, t.retry_on_reboot) "
        "FROM (SELECT * FROM workflow_defs WHERE id = (" + _latest_family_sql("$1") + ")) t "
        "RETURNING *"
    )
    last_exc: Exception | None = None
    for _attempt in range(3):
        try:
            async with PostgresClient.pool.acquire() as conn:
                exists = await conn.fetchval("SELECT 1 FROM workflow_defs WHERE id=$1", def_id)
                if not exists:
                    raise ValueError(f"workflow not found: {def_id}")
                row = await conn.fetchrow(_SQL, def_id, label, description, payload, retry_on_reboot)
            return dict(row)
        except Exception as exc:  # noqa: BLE001 - 只对唯一冲突重试，其余照抛
            from asyncpg.exceptions import UniqueViolationError
            if not isinstance(exc, UniqueViolationError):
                raise
            last_exc = exc
            await asyncio.sleep(0.05)
    raise ValueError(f"保存冲突，请重试：{last_exc}")


async def get_def(def_id: int, version: Optional[int] = None) -> Optional[dict]:
    """读一个工作流的定义——**家族寻址**。

    默认（``version=None``）返回 def_id 所属工作流的**最新版本**行：保存会 INSERT
    新行新 id，页面/调度器手里的旧 id 必须仍能读到刚存进去的定义，否则
    「保存成功但界面回退到旧数据」。显式传 ``version`` 才落到那一个版本
    （导出历史版本、跑指定版本）。
    """
    if not PostgresClient.pool:
        return None
    async with PostgresClient.pool.acquire() as conn:
        if version is None:
            row = await conn.fetchrow(
                "SELECT * FROM workflow_defs WHERE id IN (" + _family_ids_sql("$1") + ") "
                "ORDER BY version DESC LIMIT 1",
                def_id,
            )
        else:
            row = await conn.fetchrow(
                "SELECT d.* FROM workflow_defs d WHERE d.id IN (" + _family_ids_sql("$1") + ") AND d.version = $2",
                def_id, version,
            )
    if not row:
        return None
    d = dict(row)
    d["definition"] = _loads(d.get("definition"))
    return d


async def list_defs(user_id: Optional[str], limit: int = 100) -> list[dict]:
    """Latest version of every workflow **owned by one user**.

    A workflow is an independent artifact owned by the user; the agent only
    triggers runs. The list is scoped by ``user_id`` and the "latest version per
    workflow" grouping is by ``(user_id, name)`` — two users may each own a
    workflow of the same name.
    """
    if not PostgresClient.pool:
        return []
    async with PostgresClient.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.* FROM workflow_defs d
            JOIN (
                SELECT user_id, name, MAX(version) AS version
                FROM workflow_defs WHERE user_id IS NOT DISTINCT FROM $1 GROUP BY user_id, name
            ) m ON m.user_id IS NOT DISTINCT FROM d.user_id AND m.name = d.name AND m.version = d.version
            WHERE d.user_id IS NOT DISTINCT FROM $1
            ORDER BY d.updated_at DESC LIMIT $2
            """,
            user_id, limit,
        )
    out = []
    for r in rows:
        d = dict(r)
        d["definition"] = _loads(d.get("definition"))
        out.append(d)
    return out


async def delete_def(def_id: int) -> bool:
    """Delete ALL versions of a workflow (runs cascade with it).

    家族寻址：传任何一个版本行 id 都要删干净整串版本——旧实现只删 ``id=$1``
    那一行，其余版本仍被 list_defs（按家族取最新）列出来，表现为「删了又回来」。
    workflow_runs.def_id 是 FK ON DELETE CASCADE，整串版本行删掉即历史一起走。
    """
    if not PostgresClient.pool:
        return False
    async with PostgresClient.pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM workflow_defs WHERE id IN (" + _family_ids_sql("$1") + ")", def_id)
    return not result.endswith(" 0")


# ──────────────────────────────────────────────────────────────────────────────
# Runs
# ──────────────────────────────────────────────────────────────────────────────

async def start_run(def_id: int, version: int, trigger: str, args: dict) -> int:
    if not PostgresClient.pool:
        raise RuntimeError("Database not available")
    async with PostgresClient.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO workflow_runs(def_id, version, trigger, args) "
            "VALUES($1,$2,$3,$4::jsonb) RETURNING id",
            def_id, version, trigger, json.dumps(args, ensure_ascii=False),
        )


async def record_node(run_id: int, node_id: str, result: dict) -> None:
    """Merge one finished node into the run's nodes JSONB (node-level logging).

    ``result`` is the engine's per-node dict; the engine also passes a
    ``_context`` key carrying the whole-run variable snapshot, which we lift
    into the run's ``context`` column (not into the node entry).
    """
    if not PostgresClient.pool:
        return
    payload = dict(result)
    context = payload.pop("_context", None)
    async with PostgresClient.pool.acquire() as conn:
        if context is not None:
            await conn.execute(
                "UPDATE workflow_runs SET nodes = nodes || $2::jsonb, context = $3::jsonb WHERE id=$1",
                run_id,
                json.dumps({node_id: payload}, ensure_ascii=False, default=str),
                json.dumps(context, ensure_ascii=False, default=str),
            )
        else:
            await conn.execute(
                "UPDATE workflow_runs SET nodes = nodes || $2::jsonb WHERE id=$1",
                run_id,
                json.dumps({node_id: payload}, ensure_ascii=False, default=str),
            )


async def finish_run(run_id: int, status: str, error: str = "") -> None:
    if not PostgresClient.pool:
        return
    async with PostgresClient.pool.acquire() as conn:
        await conn.execute(
            "UPDATE workflow_runs SET status=$2, error=$3, finished_at=now() WHERE id=$1",
            run_id, status, error,
        )


async def get_run(run_id: int, family_of: Optional[int] = None) -> Optional[dict]:
    """一条运行记录全字段。

    ``family_of`` 给了 = 额外校验该 run 属于这个工作流——**家族口径**（run 落库
    时记的是当时最新版本行的 id，而调用方手里的 def_id 可能是旧版本行，逐行
    相等会误判 404）。
    """
    if not PostgresClient.pool:
        return None
    async with PostgresClient.pool.acquire() as conn:
        if family_of is None:
            row = await conn.fetchrow("SELECT * FROM workflow_runs WHERE id=$1", run_id)
        else:
            row = await conn.fetchrow(
                "SELECT r.* FROM workflow_runs r WHERE r.id=$1 AND r.def_id IN (" + _family_ids_sql("$2") + ")",
                run_id, family_of,
            )
    if not row:
        return None
    d = dict(row)
    for key in ("args", "context", "nodes"):
        d[key] = _loads(d.get(key))
    return d


async def list_runs(def_id: int, limit: int = 50) -> list[dict]:
    """一个工作流的运行历史——**家族寻址**。

    保存会 INSERT 新版本行（新 id）；旧版本行的 id 也要能查到它这个工作流的
    历史，否则详情页保存后运行历史「清零」。runs 按 (家族内所有版本行 id) 聚合。
    """
    if not PostgresClient.pool:
        return []
    async with PostgresClient.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, def_id, version, trigger, status, error, started_at, finished_at "
            "FROM workflow_runs WHERE def_id IN (" + _family_ids_sql("$1") + ") "
            "ORDER BY started_at DESC LIMIT $2",
            def_id, limit,
        )
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────────────────────
# Restart recovery
# ──────────────────────────────────────────────────────────────────────────────

async def recover_interrupted_runs() -> int:
    """Mark every still-``running`` run ``interrupted`` and re-run the opted-in ones.

    Called once at startup. A run left ``running`` means the process died
    mid-flight; nothing will ever finish it. We mark it terminal and, for defs
    with ``retry_on_reboot`` (default true), relaunch from the top with the same
    args as a fresh run. Returns the number of runs relaunched.

    Best-effort: any failure is logged, never raised — startup must not be held
    hostage by a wedged workflow row.
    """
    if not PostgresClient.pool:
        return 0
    try:
        async with PostgresClient.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.id, r.def_id, r.args, d.retry_on_reboot
                FROM workflow_runs r
                JOIN workflow_defs d ON d.id = r.def_id
                WHERE r.status = 'running'
                ORDER BY r.id
                """
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workflow] recover: scan failed: %s", exc)
        return 0

    relaunched = 0
    for row in rows:
        run_id = int(row["id"])
        def_id = int(row["def_id"])
        try:
            await finish_run(run_id, "interrupted", error="process restarted mid-run")
        except Exception:  # noqa: BLE001
            logger.warning("[workflow] recover: could not interrupt run %s", run_id, exc_info=True)
            continue
        if not row["retry_on_reboot"]:
            continue
        try:
            from agent import workflow_service

            await workflow_service.start_and_run(
                def_id,
                args=_loads(row["args"]) or {},
                trigger="reboot",
                caller=None,
            )
            relaunched += 1
        except Exception:  # noqa: BLE001 — one bad def must not stop recovery
            logger.warning("[workflow] recover: relaunch run for def %s failed", def_id, exc_info=True)
    if rows:
        logger.info("[workflow] restart recovery: %d interrupted, %d relaunched", len(rows), relaunched)
    return relaunched


__all__ = [
    "create_def", "update_def", "get_def", "list_defs", "delete_def",
    "start_run", "record_node", "finish_run", "get_run", "list_runs",
    "recover_interrupted_runs",
]
