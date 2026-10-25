"""Isolated Tortoise ORM bootstrap for the compatibility layer.

This connection is fully separate from the main asyncpg ``PostgresClient`` pool
and must only host compatibility tables (``mc_*``). It never runs against the
main service tables and never runs Aerich against the main database.
"""
from __future__ import annotations

from loguru import logger

from .config import settings

# Compatibility models live in their own namespace to avoid clashing with the
# main service tables. Only additive ``mc_*`` tables are generated here.
MODEL_MODULES = [
    "user_platform.models",
    "user_platform.models_task",
    "user_platform.models_project",
    "user_platform.models_git",
    "user_platform.models_note",
    "user_platform.models_skill",
    "user_platform.models_notify",
    "user_platform.models_team_admin",
    "user_platform.models_resources",
    "user_platform.models_webhook",
    "user_platform.models_webhook_event",
    "user_platform.models_review",
    "user_platform.models_tunnel",
    "user_platform.models_ddns",
    "user_platform.models_environment",
    "user_platform.models_studio",
    "user_platform.models_carrier",
    # The node ledger (mc_ac_nodes / mc_ac_node_sessions) is owned by the
    # separate node control service (``node_server.store``) and is deliberately
    # NOT registered here: the data process must not create or migrate it.
]
APP_LABEL = "user_platform"

# AI 测试平台模型（design §14.1：测试是本服务内的独立能力域，共用 PG/Redis/通知
# 基础设施，但模型独立 app label——FK 引用走 "testing_platform.X"，与 user_platform
# 解耦）。表前缀 mc_test_* / mc_knowledge_*，与既有 mc_* 表同库共存。
TESTING_MODEL_MODULES = [
    "testing_platform.projects.models",
    "testing_platform.assets.models",
    "testing_platform.tasks.models",
    "testing_platform.reports.models",
    "testing_platform.workflow.models",
    "testing_platform.defects.models",
    "testing_platform.strategy.models",
    "testing_platform.knowledge.models",
    "testing_platform.quality.models",
    "testing_platform.chat.models",
    "testing_platform.generation.models",
    "testing_platform.taskflow.models",
    "testing_platform.comments.models",
]
TESTING_APP_LABEL = "testing_platform"

_TORTOISE_CONFIG = {
    "connections": {"user_platform": settings.database_url},
    "apps": {
        APP_LABEL: {
            "models": MODEL_MODULES,
            "default_connection": "user_platform",
        },
        # 测试域共用 user_platform 的 PG 连接（default_connection 指向同库），
        # 但 app label 独立——FK 在本 app 内自洽，跨 app 只用裸 UUID 列
        # （如 defects.issue_id 指 mc_project_issues 不做 FK 关联）。
        TESTING_APP_LABEL: {
            "models": TESTING_MODEL_MODULES,
            "default_connection": "user_platform",
        },
    },
}


async def init_database() -> None:
    """Initialize the isolated Tortoise connection and create ``mc_*`` tables."""
    from tortoise import Tortoise

    config = dict(_TORTOISE_CONFIG)
    config["connections"] = {"user_platform": settings.database_url}
    # _enable_global_fallback: FastAPI runs lifespan (where init() runs) in a
    # background task, but request handlers run in a different task with no
    # ContextVar link. The global fallback lets the compat layers' Tortoise
    # queries find the connection across that task boundary.
    # use_tz=False：历史上 mc_* 表的 datetime 列建为朴素 TIMESTAMP（当年 Tortoise
    # use_tz 默认 False）。新版 Tortoise use_tz 默认 True 会生成带时区 datetime，写进
    # 朴素列时 asyncpg 报 "can't subtract offset-naive and offset-aware"。显式关掉，
    # 让 ORM 生成朴素 UTC datetime 与既有列匹配；读出的朴素值各 service 的 _unix()
    # 已按 UTC 兜底，语义一致。
    await Tortoise.init(config=config, use_tz=False, _enable_global_fallback=True)
    # Drop the retired Agent↔Skill binding table before schema creation. Its
    # model is intentionally no longer registered, so the table would otherwise
    # survive forever on upgraded installations. The Skill catalog stays.
    await _drop_retired_agent_skill_storage()
    # Same treatment for the retired task-domain zombie tables (VM binding,
    # exclusive node occupation, model-switch log, usage aggregate). Their
    # models are no longer registered, so without this the tables linger on
    # upgraded installs with stale rows nobody reads.
    await _drop_retired_task_occupation_storage()
    # Retired mc_tasks columns (dead storage the ORM no longer declares). Runs
    # before generate_schemas like the table drops above.
    await _drop_retired_task_columns()
    # §5.1 method-library reshape: mc_test_method_module.kind (base/project) was
    # dropped — one script = one class now. The old column is NOT NULL, so an
    # upgraded install rejects every INSERT until it is gone. Runs before
    # generate_schemas (which never ALTERs existing tables).
    await _drop_method_module_kind()
    # DDNS was reshaped from one table (credentials inside the record) into the
    # provider → record two-level split. Runs BEFORE the additive pass, or that
    # pass would add a nullable provider_id to the old table and the shape check
    # below could no longer recognize it.
    await _migrate_ddns_two_level()
    # Tortoise's generate_schemas never ALTERs existing tables, so columns added
    # to a model after the table was first created are missing. generate_schemas
    # would then abort the whole script on "column ... does not exist" when it
    # tries to build an index over such a column on a pre-existing table.
    # Reconcile additive columns FIRST, then create schemas.
    await _ensure_additive_columns()
    # Only creates tables that do not yet exist; safe to call repeatedly.
    await Tortoise.generate_schemas(safe=True)
    # Must run AFTER generate_schemas too: on a fresh database the table did not
    # exist during additive reconciliation, so only now can the partial unique
    # index be created. Existing DBs are idempotent via IF NOT EXISTS.
    await _ensure_task_event_message_index()
    await _ensure_task_event_seq_sequence()
    await _migrate_system_env_identity()
    await _migrate_project_issue_workflow()
    await _migrate_task_bootstrap()
    await _migrate_tunnel_runtime()
    await _migrate_environment_defaults()
    await _ensure_node_session_ledger_columns()
    # 禅道对齐：mc_test_bug.status 的 fixed → resolved + 新增列默认值回填
    # （须在 _ensure_additive_columns 之后——新列已存在才有回填对象）
    await _migrate_bug_status_rename()
    # Knowledge tables: partial unique indexes for version scoping (version_id NULL-safe).
    # Runs after generate_schemas + additive columns, so version_id exists on both fresh
    # and existing DBs; ensure_version_indexes is idempotent (IF NOT EXISTS).
    try:
        from tortoise import Tortoise as _T
        from testing_platform.knowledge import ensure_version_indexes

        dropped = await ensure_version_indexes(_T.get_connection("user_platform"))
        if dropped:
            logger.info("[user-platform] knowledge version indexes migrated, dropped: {}", dropped)
    except Exception:
        logger.exception("[user-platform] knowledge version index migration skipped")
    # 生成环节（25 号文 v4.1）：六张新表由 generate_schemas 建（模块已注册），
    # 这里补 DDL 里的增量索引（PG 方言，全 IF NOT EXISTS 幂等）+ mc_test_question 扩列。
    # 失败（如方言不匹配/存量库形态不同）只记日志不阻断 boot——表本体已由 ORM 建。
    try:
        from tortoise import Tortoise as _T

        conn = _T.get_connection("user_platform")
        from testing_platform.generation import models as _gen_models

        for _stmt in getattr(_gen_models, "DDL", ()):
            await conn.execute_script(_stmt)
        await _gen_models.ensure_question_columns(conn)
        logger.info("[user-platform] generation schema ready (DDL indexes + question columns)")
    except Exception:
        logger.exception("[user-platform] generation schema migration skipped")
    # 任务层（26 号文 D-6）：正式表加「暂存标识」两列（task_run_id / staged），
    # 全 IF NOT EXISTS 幂等、列可空带默认值→存量行零影响。表不存在时跳过
    # （建表由 ORM 负责），失败不阻断 boot。
    try:
        from testing_platform.taskflow.staging import ensure_stage_columns

        added = await ensure_stage_columns()
        if added:
            logger.info("[user-platform] task staging columns added: {}", added)
    except Exception:
        logger.exception("[user-platform] task staging column migration skipped")
    logger.debug("[user-platform] tortoise schemas ready")


async def _drop_retired_agent_skill_storage() -> None:
    """Drop the retired Agent↔Skill binding table and its data.

    GenericAgent resource delivery goes through AgentSop and never installs
    Skills, so ``mc_agent_skill_bindings`` is dead storage. The Skill catalog
    itself (``mc_agent_skills`` / ``_versions`` / ``_repos``) stays: it powers
    the resource center's Skill management. Runs after Tortoise.init (needs the
    connection) but before generate_schemas so the table is never recreated.
    The guard query keeps fresh databases from paying a DROP on every boot;
    existing installs are cleaned exactly once. Idempotent via IF EXISTS.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name = 'mc_agent_skill_bindings'"
    )
    if not rows:
        return
    await conn.execute_script("DROP TABLE IF EXISTS mc_agent_skill_bindings CASCADE;")
    logger.info("[user-platform] dropped retired table mc_agent_skill_bindings")


# Zombie task-domain tables: every one had no writer (or only a legacy cleanup
# delete) and no live reader once the VM/occupation/usage-aggregate models were
# retired. ``mc_task_model_switches`` (switch_task_model writes models_snapshot,
# never this), ``mc_task_usage_stats`` (gateway never wrote it; the dashboard now
# reads request_logs), ``mc_task_virtual_machines`` / ``mc_task_node_bindings``
# (the exclusive-occupation model is gone; the node server enforces capacity).
# The task_id columns are plain UUIDs with no inbound FK, so dropping cascades
# nothing a live table depends on.
_RETIRED_TASK_TABLES = (
    "mc_task_model_switches",
    "mc_task_usage_stats",
    "mc_task_virtual_machines",
    "mc_task_node_bindings",
)


async def _drop_retired_task_occupation_storage() -> None:
    """Drop the retired task-domain zombie tables and their data.

    Their models are no longer registered, so ``generate_schemas`` would never
    recreate them — but on upgraded installations the old tables (and any stale
    rows migrated in) would survive forever. Runs after Tortoise.init (needs the
    connection) but before generate_schemas. The guard query keeps fresh
    databases from paying a DROP on every boot; existing installs are cleaned
    exactly once. Idempotent via IF EXISTS.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    placeholders = ", ".join(f"${i + 1}" for i in range(len(_RETIRED_TASK_TABLES)))
    rows = await conn.execute_query_dict(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_name IN ({placeholders})",
        list(_RETIRED_TASK_TABLES),
    )
    present = {row["table_name"] for row in rows}
    if not present:
        return
    for table in _RETIRED_TASK_TABLES:
        if table in present:
            await conn.execute_script(f"DROP TABLE IF EXISTS {table} CASCADE;")
    logger.info("[user-platform] dropped retired task tables: {}", ", ".join(sorted(present)))


# Retired Task columns: dead storage the ORM no longer declares. Every one had
# no live writer/reader at removal time (see models_task.Task for what stayed —
# notably first_request_id / last_request_at, which the codex bootstrap binding
# still writes, were deliberately KEPT).
_RETIRED_TASK_COLUMNS = (
    "workspace_source_task_id",
    "workspace_metadata",
    "environment_snapshot",
    "mode_capability_snapshot",
    "log_store",
    "mcp_config",
    "skill_config",
    "plugin_config",
)


async def _drop_retired_task_columns() -> None:
    """Drop retired mc_tasks columns that the ORM no longer declares.

    ``generate_schemas`` never drops columns, so an upgraded install keeps them
    forever as dead TOAST-bearing storage. Runs after Tortoise.init but before
    generate_schemas — dropping here also means a fresh-then-migrated database
    never pays a second DROP (idempotent via the information_schema guard).
    The columns are plain NULLable fields with no constraint depending on them.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    table_rows = await conn.execute_query_dict(
        "SELECT table_name FROM information_schema.tables WHERE table_name = 'mc_tasks'"
    )
    if not table_rows:
        return
    placeholders = ", ".join(f"${i + 1}" for i in range(len(_RETIRED_TASK_COLUMNS)))
    col_rows = await conn.execute_query_dict(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'mc_tasks' "
        f"AND column_name IN ({placeholders})",
        list(_RETIRED_TASK_COLUMNS),
    )
    present = {row["column_name"] for row in col_rows}
    if not present:
        return
    for column in _RETIRED_TASK_COLUMNS:
        if column in present:
            await conn.execute_script(f'ALTER TABLE "mc_tasks" DROP COLUMN IF EXISTS "{column}";')
    logger.info("[user-platform] dropped retired task columns: {}", ", ".join(sorted(present)))


async def _drop_method_module_kind() -> None:
    """Drop the retired ``mc_test_method_module.kind`` column (§5.1 reshape).

    The method library was reshaped so that **one script = one class**; the
    old ``kind`` (base/project) classifier is gone and the ORM no longer
    declares it. ``generate_schemas`` never drops columns, so an upgraded
    install keeps the old column — and because it was ``NOT NULL``, every
    INSERT from the new code (which omits it) fails with an IntegrityError.
    ``DROP COLUMN`` also removes the dependent ``(project_id, kind)`` index.
    Existing rows survive; the old ``base`` rows already had ``project_id``
    NULL, matching the new "NULL = global" semantics, so no data migration is
    needed. Idempotent via the information_schema guard + IF EXISTS.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'mc_test_method_module' AND column_name = 'kind'"
    )
    if not rows:
        return
    await conn.execute_script(
        'ALTER TABLE "mc_test_method_module" DROP COLUMN IF EXISTS "kind";'
    )
    logger.info("[user-platform] dropped retired column mc_test_method_module.kind")


async def _migrate_ddns_two_level() -> None:
    """Reshape the pre-release single-table DDNS schema into provider → record.

    ``mc_ddns_records`` first shipped (unreleased) with credentials inside the
    record's ``config`` and no ``provider_id``. The model now splits credentials
    onto :class:`DdnsProvider`. Neither ``generate_schemas`` nor the additive
    pass can express that reshape — additive would only bolt on a *nullable*
    ``provider_id`` and leave a half-migrated table whose rows carry credentials
    in the wrong place.

    So the old shape is detected (table present, ``provider_id`` absent) and
    dropped; ``generate_schemas`` recreates it from the current model. No
    released deployment ever had this table populated, so nothing user-created
    is lost; a warning is logged when rows are actually discarded.

    Runs BEFORE :func:`_ensure_additive_columns` (otherwise the additive pass
    hides the old shape by adding the column first) and is idempotent via the
    information_schema guard. Also called from ``tunnel_server`` so whichever
    process starts first against a stale table reshapes it.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    columns = await conn.execute_query_dict(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'mc_ddns_records'"
    )
    if not columns:
        return  # table absent → fresh database; generate_schemas builds the new shape
    names = {row["column_name"] for row in columns}
    if "provider_id" in names:
        return  # already the two-level schema

    rows = await conn.execute_query_dict("SELECT COUNT(*) AS n FROM mc_ddns_records")
    count = int((rows or [{"n": 0}])[0]["n"] or 0)
    await conn.execute_script("DROP TABLE IF EXISTS mc_ddns_records CASCADE;")
    if count:
        logger.warning(
            "[user-platform] mc_ddns_records reshaped to provider→record; "
            "discarded {} pre-release row(s)", count,
        )
    else:
        logger.info("[user-platform] mc_ddns_records reshaped to the provider→record schema")


async def _ensure_additive_columns() -> None:
    """Reconcile every compat table with its model, adding any missing column.

    Tortoise ``generate_schemas(safe=True)`` only creates missing *tables*; it
    never ALTERs an existing table to append columns added to the model after
    the table was first created. That mismatch surfaces as ``OperationalError:
    column "..." does not exist`` the first time we query the new field
    (``last_active_at``, ``updated_at`` etc.).

    This walks every model registered in the compat app, compares its declared
    DB columns against ``information_schema.columns`` for that table, and runs a
    plain ``ALTER TABLE ... ADD COLUMN`` for each absent column. It is:

    * Generic — no hardcoded column list, so newly added model fields are
      picked up automatically on the next startup.
    * Additive only — never drops, renames, or retypes an existing column.
    * Safe on populated tables — every added column is made NULLable regardless
      of the model's ``null`` flag, because ``ADD COLUMN ... NOT NULL`` without a
      default fails when rows already exist. Tortoise still supplies values on
      insert; reads simply stop 500-ing. Idempotent across restarts.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    # Tortoise.apps is an ``Apps`` object (not a plain dict); iterate via
    # ``.items()`` and pick our compat app's model map. **两个 app 都要补**：
    # user_platform（mc_*）与 testing_platform（mc_test_*/mc_knowledge_*）——
    # 测试域模型后加列同样不会被 generate_schemas 补上（曾漏：mc_test_project
    # 加 coding_project_id 后建项目报 column does not exist）。
    wanted_labels = {APP_LABEL, TESTING_APP_LABEL}
    app_models: dict = {}
    for label, model_map in Tortoise.apps.items():
        if label in wanted_labels:
            app_models.update(model_map)
    for model in app_models.values():
        meta = model._meta
        table = meta.db_table
        # Skip if the table itself does not exist yet (generate_schemas will
        # have created it; a missing table here means a transient race — leave
        # it to the next startup rather than ALTERing a non-existent table).
        table_rows = await conn.execute_query_dict(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            [table],
        )
        if not table_rows:
            continue
        existing = {row["column_name"] for row in table_rows}
        # fields_db_projection maps model field name -> actual DB column name;
        # this excludes reverse relations and other non-column pseudo-fields.
        for field_name, column in meta.fields_db_projection.items():
            if column in existing:
                continue
            field = meta.fields_map[field_name]
            sql_type = getattr(field, "SQL_TYPE", None)
            if not sql_type:
                # Unknown/relational field with no direct column type — skip.
                continue
            await conn.execute_script(
                f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type} NULL;'
            )
            logger.info(
                "[user-platform] added missing column {}.{} ({})",
                table,
                column,
                sql_type,
            )


async def _ensure_task_event_message_index() -> None:
    """Add the (task_id, client_message_id) partial unique index for idempotent sends.

    Column reconciliation runs first, so ``client_message_id`` exists by the time
    this runs. The partial ``WHERE client_message_id IS NOT NULL`` keeps legacy
    rows (which never carried an id) outside the constraint, so backfilling the
    index onto a populated table cannot fail on duplicates. Idempotent via
    ``IF NOT EXISTS``.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'mc_task_events'"
    )
    if not rows:
        return  # table not created yet; generate_schemas will build it with the model's indexes
    columns = {row["column_name"] for row in rows}
    if "client_message_id" not in columns:
        return
    await conn.execute_script(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_mc_task_events_client_message_id "
        "ON mc_task_events (task_id, client_message_id) "
        "WHERE client_message_id IS NOT NULL;"
    )


async def _migrate_system_env_identity() -> None:
    """Widen the system-env snapshot's unique key to include ``provider``.

    The node reports MCP entries keyed (provider, name) on purpose — the same
    server name in two editors' configs is two capabilities, because each
    runtime loads its own definition — but the table's old constraint was
    UNIQUE (node_id, kind, name). An operator with e.g. a mis-nested
    ``mcpServers`` key in two editors' configs therefore failed refresh with a
    duplicate-key 500. Swap the stale 3-column constraint for the model's
    4-column one. No cleanup pass is needed: rows unique on the narrower key
    are trivially unique on the wider one. Idempotent by matching constraint
    *definitions*, not names — Tortoise generates a different hash per field
    set, and fresh databases already get the 4-column one from the model.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    table = "mc_node_system_env_entries"
    exists = await conn.execute_query_dict(
        "SELECT 1 FROM information_schema.tables WHERE table_name = $1", [table]
    )
    if not exists:
        return  # table not created yet; generate_schemas will build it with the model's key
    constraints = await conn.execute_query_dict(
        "SELECT conname AS name, pg_get_constraintdef(oid) AS def "
        "FROM pg_constraint "
        f"WHERE conrelid = to_regclass('{table}') AND contype = 'u'"
    )

    def columns(defn: str) -> set[str]:
        inner = defn[defn.find("(") + 1 : defn.rfind(")")]
        return {c.strip().strip('"') for c in inner.split(",")}

    for row in constraints:
        if columns(row["def"]) == {"node_id", "kind", "name"}:
            await conn.execute_script(f'ALTER TABLE {table} DROP CONSTRAINT "{row["name"]}";')
            logger.info(
                "[user-platform] dropped stale system-env unique constraint {}",
                row["name"],
            )
    if not any(
        columns(row["def"]) == {"node_id", "kind", "provider", "name"}
        for row in constraints
    ):
        await conn.execute_script(
            f"ALTER TABLE {table} ADD CONSTRAINT uid_mc_node_sys_env_provider "
            "UNIQUE (node_id, kind, provider, name);"
        )
        logger.info("[user-platform] system-env snapshot key widened to include provider")


async def _migrate_bug_status_rename() -> None:
    """禅道对齐：``mc_test_bug.status`` 的 ``fixed`` → ``resolved``（2026-10-16）。

    禅道 ``fixed`` 是 **resolution**（解决方案），不是 status；我们早期的
    ``BUG_STATUSES`` 误把 ``fixed`` 当状态。新版状态机用 ``resolved``
    （design plans/23-defect-zentao-parity.md），历史行在此归一。

    幂等：无匹配行即 no-op（跑第二次 UPDATE 影响 0 行）。同时回填禅道对齐
    新增的 NOT NULL 列默认值（``activated_count`` / ``confirmed`` / ``found_in``）
    ——``_ensure_additive_columns`` 把它们加为 NULL，存量行必须补默认值，
    否则 service 读侧与筛选用 ``or 0`` 兜底虽不崩，报表计数会漏。
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'mc_test_bug'"
    )
    if not rows:
        return  # table absent → fresh database; generate_schemas builds the new shape
    existing = {row["column_name"] for row in rows}
    if "activated_count" not in existing:
        return  # additive pass has not run yet — nothing to backfill or rename

    await conn.execute_script(
        "UPDATE mc_test_bug SET activated_count = 0 "
        "WHERE activated_count IS NULL;"
        "UPDATE mc_test_bug SET confirmed = false "
        "WHERE confirmed IS NULL;"
        "UPDATE mc_test_bug SET found_in = 'test' "
        "WHERE found_in IS NULL AND status IN ('open','triaged','verified','closed','rejected','fixed');"
        "UPDATE mc_test_bug SET status = 'resolved' WHERE status = 'fixed';"
    )
    logger.info("[user-platform] bug status normalized to zentao parity (fixed → resolved)")


async def _ensure_task_event_seq_sequence() -> None:
    """Create the shared ``mc_task_events_seq`` and seat it past the current max.

    ``seq`` was previously allocated as ``SELECT MAX(seq)+1`` per task by both
    the gateway (``_next_event_seq``) and node_server (``task_event_log`` /
    ``task_stage``). Two writers doing ``MAX+1`` on separate connections is a
    race: both read the same max and both insert that value +1, producing
    duplicate seqs. The ``(seq, id)`` cursor in ``list_task_events`` then
    repeats or skips rows at a page boundary that lands inside a tie.

    A single PG SEQUENCE makes ``nextval`` atomic across every writer, so seq
    is globally unique and monotonic (per-task values are not contiguous once
    other tasks consume seqs, but the ``seq < before`` cursor only needs
    monotonic + unique, which holds). Created here on the gateway because
    node_server deliberately does not create domain tables (see
    ``node_server/shared_store.py``); the SEQUENCE object lives in the shared
    DB, so any connection can ``nextval`` it. ``setval`` past the current max
    keeps existing rows' seqs below the next allocation, so old and new rows
    never collide. Idempotent via ``IF NOT EXISTS``.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'mc_task_events'"
    )
    if not rows:
        return  # table not created yet; generate_schemas will build it
    if "seq" not in {row["column_name"] for row in rows}:
        return
    await conn.execute_script("CREATE SEQUENCE IF NOT EXISTS mc_task_events_seq;")
    # Seat past the current max so the first nextval is strictly greater than
    # every existing seq. is_called=false (setval(..., ..., true) is the
    # default) means the next nextval returns exactly the set value — so use
    # max+1 semantics by seating at GREATEST(max, 1) and letting nextval start
    # from the value after it.
    await conn.execute_script(
        "SELECT setval('mc_task_events_seq', "
        "GREATEST((SELECT COALESCE(MAX(seq), 0) FROM mc_task_events), 1));"
    )


async def _migrate_project_issue_workflow() -> None:
    """Migrate legacy issue rows to the requirement/bug state machine.

    The table used to store ``open | completed | closed`` and had no type. New
    rows always start ``unassigned``; existing rows are requirements because no
    discriminator existed when they were created. The statements are idempotent
    and run after additive columns are present.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'mc_project_issues'"
    )
    columns = {row["column_name"] for row in rows}
    required = {"type", "status", "pending_items"}
    if not required.issubset(columns):
        return
    # Additive reconciliation creates missing columns nullable, regardless of
    # model defaults, so materialize defaults for existing rows explicitly.
    await conn.execute_script(
        """
        UPDATE mc_project_issues
           SET type = 'requirement'
         WHERE type IS NULL OR BTRIM(type) = '';
        UPDATE mc_project_issues
           SET pending_items = '[]'::jsonb
         WHERE pending_items IS NULL;
        UPDATE mc_project_issues SET status = 'unassigned' WHERE status = 'open';
        """
    )
    logger.debug("[user-platform] project issue workflow migration ready")


async def _migrate_task_bootstrap() -> None:
    """Normalize additive Task bootstrap fields and enforce thread uniqueness.

    ``_ensure_additive_columns`` intentionally adds fields nullable on populated
    tables. The gateway treats NULL booleans as false during rollout; this step
    materializes the defaults and adds the partial unique index that makes a
    provider-native thread belong to exactly one canonical Task.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'mc_tasks'"
    )
    columns = {row["column_name"] for row in rows}
    required = {"provider_thread_id", "first_request_seen", "bootstrap_consumed"}
    if not required.issubset(columns):
        return
    await conn.execute_script(
        """
        UPDATE mc_tasks SET first_request_seen = false WHERE first_request_seen IS NULL;
        UPDATE mc_tasks SET bootstrap_consumed = false WHERE bootstrap_consumed IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_mc_tasks_provider_thread
            ON mc_tasks(provider_thread_id)
            WHERE provider_thread_id IS NOT NULL;
        """
    )
    logger.debug("[user-platform] task bootstrap migration ready")


async def _migrate_tunnel_runtime() -> None:
    """Backfill desired state and runtime rows for pre-runtime tunnel bindings."""
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    tables = await conn.execute_query_dict(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ('mc_tunnel_bindings','mc_tunnel_runtimes')"
    )
    if {row["table_name"] for row in tables} != {
        "mc_tunnel_bindings", "mc_tunnel_runtimes"
    }:
        return
    # Failed/stopped legacy rows remain stopped to avoid a boot-time retry storm;
    # pending/running rows preserve intent and are reconciled after startup.
    await conn.execute_script(
        """
        UPDATE mc_tunnel_bindings
           SET desired_state = CASE
               WHEN client_status IN ('pending','running') THEN 'running'
               ELSE 'stopped'
           END
         WHERE desired_state IS NULL OR BTRIM(desired_state) = '';
        UPDATE mc_tunnel_bindings
           SET delete_requested = false
         WHERE delete_requested IS NULL;
        """
    )
    logger.debug("[user-platform] tunnel runtime migration ready")


async def _migrate_environment_defaults() -> None:
    """Backfill environment columns that predate their rows, and drop the old
    ``(user_id, node_id, name)`` unique index.

    ``workspace_mode`` / ``enabled`` were added to a populated table, so every
    pre-existing environment holds NULL for them. ``not None`` is True in Python,
    which made those rows report as *disabled* (dispatch then refused them), and
    an empty mode made the frontend treat a shared environment as the system
    tier — which cleared the user's selection the moment they picked it.

    The unique index is dropped because a name is a label, not an identity: the
    environment's id is what names its node directory. Keeping it meant a user
    could be blocked by "已有同名环境" for an environment they cannot see (a
    legacy row with no project_id never shows on any project page).

    Idempotent: the backfill only touches NULLs, and the drop uses IF EXISTS.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name = 'mc_task_environments'"
    )
    if not rows:
        return
    await conn.execute_script(
        """
        UPDATE mc_task_environments SET enabled = true WHERE enabled IS NULL;
        UPDATE mc_task_environments
           SET workspace_mode = 'shared'
         WHERE workspace_mode IS NULL OR BTRIM(workspace_mode) = '';
        UPDATE mc_task_environments SET isolated = true WHERE isolated IS NULL;
        """
    )
    # provision_state（列后加，NULL=老行）：external/shared 没有可准备的 clone，
    # 直接 ready；worktree/fresh 标成 provisioning——服务启动的 recover_pending_provisions
    # 会对它们做一次幂等验证/补 clone（节点侧目录已在就秒回）。不回填 ready：
    # 「ready」是句实话，没验证过的老行不配。
    await conn.execute_script(
        """
        UPDATE mc_task_environments SET provision_state = 'ready'
         WHERE provision_state IS NULL
           AND (workspace_mode IS NULL OR BTRIM(workspace_mode) = ''
                OR workspace_mode IN ('shared', 'external'));
        UPDATE mc_task_environments SET provision_state = 'provisioning'
         WHERE provision_state IS NULL
           AND workspace_mode IN ('worktree', 'fresh');
        """
    )
    # 按定义找唯一的那条再删，而不是硬编码名字：Tortoise 生成的名字带哈希，跨
    # 版本/跨库不稳定。同一约束可能是「表约束」（pg_constraint，得 ALTER DROP
    # CONSTRAINT）也可能是「裸唯一索引」（pg_indexes，得 DROP INDEX），且
    # DROP INDEX 作用在受约束支撑的索引上会直接报错——所以两种都查、各走各的。
    wanted = ("user_id", "node_id", "name")
    constraints = await conn.execute_query_dict(
        "SELECT conname, pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conrelid = 'mc_task_environments'::regclass AND contype = 'u'"
    )
    for con in constraints:
        definition = (con.get("d") or "").lower()
        if all(col in definition for col in wanted):
            await conn.execute_script(
                f'ALTER TABLE mc_task_environments DROP CONSTRAINT IF EXISTS "{con["conname"]}"'
            )
            logger.info(
                "[user-platform] dropped env name-unique constraint {}", con["conname"]
            )
    indexes = await conn.execute_query_dict(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename = 'mc_task_environments' "
        "AND indexdef ILIKE 'CREATE UNIQUE INDEX%'"
    )
    for idx in indexes:
        definition = (idx.get("indexdef") or "").lower()
        if all(col in definition for col in wanted):
            await conn.execute_script(f'DROP INDEX IF EXISTS "{idx["indexname"]}"')
            logger.info(
                "[user-platform] dropped env name-unique index {}", idx["indexname"]
            )
    logger.debug("[user-platform] environment defaults migration ready")


async def _ensure_node_session_ledger_columns() -> None:
    """Add the data service's own columns to the node session ledger.

    Today that's ``env_id`` (the environment's session tab filter),
    ``title``/``platform_name`` (display names), ``last_activity_at`` (when the
    session last talked on the node) and ``transcript`` (the session's imported
    conversation — see below). ``mc_ac_node_sessions`` belongs
    to the control service — the data process deliberately does NOT register its
    Tortoise model (see the note in ``MODEL_MODULES``), so the generic
    additive-column pass skips it. The data service still reads/writes that
    table directly, so the columns it owns are ensured here too.

    ``transcript`` is deliberately **not** mirrored into the control plane's
    ``ACNodeSession`` model. The conversation content is the display copy of
    what the editor writes on the node, and only the data service reads or
    writes it; keeping it out of the control plane's model means its
    ``row.save()`` (which rewrites every field the model knows) can never
    blank it, and its session fetches never drag a multi-MB JSON blob along.
    Shape: ``{"items": [NormalizedItem…], "head": bool, "window_lines": int}``.

    ``IF NOT EXISTS`` keeps this safe alongside the control service's own
    reconciliation: whichever process starts first creates it, the other's is a
    no-op. Without it the session tab would 500 until the control plane
    restarted, which is an avoidable deployment-order hazard.

    The index is created only when the column has none: the control plane's own
    ``env_id`` index (``idx_mc_ac_node__env_id_*``, named by Tortoise) is usually
    already there, and a second index on the same column would just cost a write
    on every session row. An earlier version of this migration created one
    unconditionally, so this also drops that stray when it is what we find.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("user_platform")
    rows = await conn.execute_query_dict(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name = 'mc_ac_node_sessions'"
    )
    if not rows:
        return
    for column, sql_type in (
        ("env_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
        # 会话标题来自节点对账；platform_name 是用户在平台改的名字，二者只进这
        # 一列各自的写者（对账写 title、改名接口写 platform_name），互不覆盖。
        ("title", "VARCHAR(512) NOT NULL DEFAULT ''"),
        ("platform_name", "VARCHAR(512) NOT NULL DEFAULT ''"),
        # 会话在节点上**最后一次活动**的时间（编辑器记录里最后一条的时间）。
        # 与 updated_at 不同：那列是账本行被碰的时间（派发/对账都会写），
        # 会话列表要展示的是用户关心的"这个会话什么时候还在聊"，可为空
        # （控制面派发建的那批行没有节点扫描结果）。
        ("last_activity_at", "TIMESTAMPTZ NULL"),
        # 思考等级（low/medium/high/xhigh）——从节点会话记录里读出来的（claude
        # 的 effort / codex 的 turn_context.effort）。同步为任务时用它预填任务参数，
        # 不让用户重选。opencode 读不到 → 空串（前端显示「默认」）。
        # 权限档复用已有的 mode 列（控制面派发时写、对账时非空才覆盖）。
        ("effort", "VARCHAR(16) NOT NULL DEFAULT ''"),
        # 会话历史的**显示副本**：导入过哪些条目、读到节点文件的哪一行、有没有到顶。
        # 节点是内容的唯一来源，这列只是同步/导入时留下的副本——详情页"永远先读库"
        # 靠的就是它。绝不进控制面模型（见 docstring），写者只有导入接口。
        # 存 TEXT 而不是 JSONB：asyncpg 对 JSONB 会自动 json 编解码、sqlite 根本没有
        # 这个类型，同一句 SQL 在两边读出的 Python 类型不一样。显式 dumps/loads 才能
        # 让测试里那段 SQL 真跑通。
        ("transcript", "TEXT NULL"),
    ):
        await conn.execute_script(
            f"ALTER TABLE mc_ac_node_sessions ADD COLUMN IF NOT EXISTS "
            f"{column} {sql_type};"
        )
    indexed = await conn.execute_query_dict(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename = 'mc_ac_node_sessions' AND indexdef LIKE '%(env_id)%'"
    )
    names = {str(row.get("indexname") or "") for row in indexed}
    if "idx_mc_ac_node_sessions_env" in names:
        # 我们自己的那份重复索引；控制面已经建过一个覆盖同列的，留着只多付写入。
        await conn.execute_script("DROP INDEX IF EXISTS idx_mc_ac_node_sessions_env;")
        names.discard("idx_mc_ac_node_sessions_env")
    if not names:
        await conn.execute_script(
            "CREATE INDEX IF NOT EXISTS idx_mc_ac_node_sessions_env ON mc_ac_node_sessions(env_id);"
        )
    logger.debug("[user-platform] node session ledger columns ready")


async def close_database() -> None:
    from tortoise import Tortoise

    await Tortoise.close_connections()
