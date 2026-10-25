"""Tortoise models for the knowledge domain (``mc_knowledge_*``).

Knowledge is a **platform-level asset** (developers read it too), so the
tables carry no ``test`` prefix — only the derived per-anchor test profile
(L4 view) is ``mc_test_`` (design ``docs/ai-testing-platform/design.md``
§15.1 and plan ``09-knowledge-system.md`` §3/§6/§15.1).

Conventions follow ``user_platform/models_project.py``:

* ``id`` is a UUID primary key,
* ``mc_`` table prefix,
* JSONB payloads use ``JSONField``,
* unique constraints carry the *stable identity* that adapters upsert against
  (§4.1 anchor identity = ``(project_id, kind, path, symbol)``; items use
  ``(project_id, kind, key)``).

The same tables are also exposed to the raw asyncpg path used by the main
service: ``DDL`` + ``create_tables_via_pg()`` keep a PostgresClient-only
deployment able to create the schema without Tortoise being initialized.
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

from ._version_indexes import INDEX_DDL  # 部分唯一索引定义（DDL 复用）

# --------------------------------------------------------------------------
# Raw DDL mirroring the models below (for ``PostgresClient.pool`` deployments).
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_knowledge_source (
        id              UUID PRIMARY KEY,
        kind            VARCHAR(16)  NOT NULL,
        name            VARCHAR(256) NOT NULL,
        source_type     VARCHAR(32)  NOT NULL,
        location        JSONB        NOT NULL DEFAULT '{}'::jsonb,
        sync_policy     VARCHAR(16)  NOT NULL DEFAULT 'on_demand',
        project_id      UUID,
        last_synced_at  TIMESTAMPTZ,
        status          VARCHAR(16)  NOT NULL DEFAULT 'active',
        created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_knowledge_item (
        id            UUID PRIMARY KEY,
        project_id    UUID NOT NULL,
        version_id    UUID,
        source_id     UUID,
        kind          VARCHAR(32)  NOT NULL,
        key           VARCHAR(512) NOT NULL,
        title         TEXT,
        content       JSONB NOT NULL DEFAULT '{}'::jsonb,
        status        VARCHAR(16) NOT NULL DEFAULT 'candidate',
        confidence    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        provenance    JSONB NOT NULL DEFAULT '{}'::jsonb,
        version       INTEGER NOT NULL DEFAULT 1,
        anchor_id     UUID,
        source_node_id VARCHAR(255),
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_knowledge_anchor (
        id              UUID PRIMARY KEY,
        project_id      UUID NOT NULL,
        version_id      UUID,
        kind            VARCHAR(16)  NOT NULL,
        path            VARCHAR(1024) NOT NULL,
        symbol          VARCHAR(512) NOT NULL,
        content_hash    VARCHAR(64),
        label           VARCHAR(512),
        source_location VARCHAR(32),
        community       INTEGER,
        provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,
        source_node_id VARCHAR(255),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_knowledge_edge (
        id             UUID PRIMARY KEY,
        project_id       UUID NOT NULL,
        version_id       UUID,
        relation        VARCHAR(32) NOT NULL,
        from_kind       VARCHAR(8) NOT NULL,
        from_id         UUID NOT NULL,
        to_kind         VARCHAR(8) NOT NULL,
        to_id           UUID NOT NULL,
        confidence      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,
        source_file     VARCHAR(1024),
        source_location VARCHAR(32),
        content_hash    VARCHAR(64),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_knowledge_raw (
        id           UUID PRIMARY KEY,
        project_id   UUID         NOT NULL,
        source_id    UUID,
        kind         VARCHAR(16)  NOT NULL DEFAULT 'product',
        adapter      VARCHAR(16)  NOT NULL DEFAULT 'builtin',
        adapter_type VARCHAR(32)  NOT NULL DEFAULT 'custom',
        locator      VARCHAR(512),
        title        VARCHAR(512),
        content      JSONB        NOT NULL DEFAULT '{}'::jsonb,
        content_text TEXT,
        media_type   VARCHAR(128),
        byte_size    INTEGER      NOT NULL DEFAULT 0,
        content_hash VARCHAR(64)  NOT NULL,
        provenance   JSONB        NOT NULL DEFAULT '{}'::jsonb,
        version      INTEGER      NOT NULL DEFAULT 1,
        status       VARCHAR(16)  NOT NULL DEFAULT 'active',
        fetched_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, source_id, content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_profile (
        id               UUID PRIMARY KEY,
        project_id       UUID NOT NULL,
        anchor_id        UUID NOT NULL,
        rule_ids         JSONB NOT NULL DEFAULT '[]'::jsonb,
        component_ids   JSONB NOT NULL DEFAULT '[]'::jsonb,
        bug_pattern_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        template_ids    JSONB NOT NULL DEFAULT '[]'::jsonb,
        coverage_gaps   JSONB NOT NULL DEFAULT '{}'::jsonb,
        last_tested_at  TIMESTAMPTZ,
        test_status     VARCHAR(16) NOT NULL DEFAULT 'green',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (anchor_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_knowledge_item_project ON mc_knowledge_item(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_source_project ON mc_knowledge_source(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_source_kind    ON mc_knowledge_source(kind)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_item_kind     ON mc_knowledge_item(kind)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_anchor_project ON mc_knowledge_anchor(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_anchor_path    ON mc_knowledge_anchor(path)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_edge_project  ON mc_knowledge_edge(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_edge_from     ON mc_knowledge_edge(from_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_edge_to       ON mc_knowledge_edge(to_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_edge_relation ON mc_knowledge_edge(relation)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_raw_project  ON mc_knowledge_raw(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_raw_source   ON mc_knowledge_raw(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_raw_hash     ON mc_knowledge_raw(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_test_profile_project   ON mc_test_profile(project_id)",
] + INDEX_DDL  # 部分唯一索引（versioned + legacy 两条），见 _version_indexes.py

#: ``mc_knowledge_source.kind`` 知识源性质（design §15.1）
KNOWLEDGE_SOURCE_KINDS: tuple[str, ...] = ("product", "tech", "test")

#: ``mc_knowledge_source.source_type`` 源类型（design §15.1）
KNOWLEDGE_SOURCE_TYPES: tuple[str, ...] = (
    "prd",
    "wiki",
    "api_doc",
    "codegraph",
    "db_schema",
    "case_lib",
    "bug_lib",
    "report_lib",
)

#: ``mc_knowledge_source.sync_policy`` 同步策略（design §15.1）
KNOWLEDGE_SYNC_POLICIES: tuple[str, ...] = ("on_demand", "scheduled", "webhook")

#: ``mc_knowledge_source.status`` 生命周期
KNOWLEDGE_SOURCE_STATUSES: tuple[str, ...] = ("active", "paused", "archived")


class KnowledgeSource(Model):
    """L1 knowledge source registry (``design.md`` §15.1 / ``09-knowledge-system.md``).

    A source is a *registered origin* of knowledge (PRD repo, wiki, OpenAPI
    spec, Graphify codegraph, DB schema, case/bug/report library). ``location``
    is a JSON descriptor (repo url / path / endpoint); ``sync_policy`` says how
    it is refreshed (``on_demand`` / ``scheduled`` / ``webhook``).
    ``last_synced_at`` is the freshness watermark downstream drift checks read.

    ``mc_knowledge_item.source_id`` points here. Knowledge is a **platform-level
    asset**, so the table carries no ``test`` prefix (design §15.1 命名修正).
    """

    id = fields.UUIDField(pk=True)
    kind = fields.CharField(max_length=16)  # product|tech|test
    name = fields.CharField(max_length=256)
    source_type = fields.CharField(max_length=32)  # prd|wiki|api_doc|codegraph|db_schema|case_lib|bug_lib|report_lib
    location = fields.JSONField(default=dict)  # 源位置描述（repo/path/endpoint）
    sync_policy = fields.CharField(max_length=16, default="on_demand")  # on_demand|scheduled|webhook
    project_id = fields.UUIDField(null=True)  # 归属项目（平台级源可为空）
    last_synced_at = fields.DatetimeField(null=True)
    status = fields.CharField(max_length=16, default="active")  # active|paused|archived
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_knowledge_source"
        indexes = (("project_id",), ("kind",), ("source_type",))


class KnowledgeRaw(Model):
    """L1 raw layer — immutable source snapshot + provenance (09 §2).

    The *first* layer: whatever a source adapter pulled, verbatim, before any
    parsing. Immutable by design — a new version is a **new row** (keyed on
    ``(project_id, source_id, content_hash)``), so re-fetching identical content
    is idempotent while changed content appends.

    ``content`` holds structured payloads (JSON), ``content_text`` holds plain
    text (markdown/csv/PRD prose). Small text lives in the DB; large binaries
    stay in object storage with ``locator``/``media_type`` describing them —
    the §12 #3 decision, applied per-row rather than as two code paths.
    """

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    source_id = fields.UUIDField(null=True)  # → mc_test_project_source
    kind = fields.CharField(max_length=16, default="product")  # product|tech|test
    adapter = fields.CharField(max_length=16, default="builtin")  # builtin|agent
    adapter_type = fields.CharField(max_length=32, default="custom")
    locator = fields.CharField(max_length=512, null=True)  # file path / URL / doc id
    title = fields.CharField(max_length=512, null=True)
    content = fields.JSONField(default=dict)
    content_text = fields.TextField(null=True)
    media_type = fields.CharField(max_length=128, null=True)
    byte_size = fields.IntField(default=0)
    content_hash = fields.CharField(max_length=64)
    provenance = fields.JSONField(default=dict)
    version = fields.IntField(default=1)
    status = fields.CharField(max_length=16, default="active")  # active|superseded|archived
    fetched_at = fields.DatetimeField(auto_now_add=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_knowledge_raw"
        unique_together = (("project_id", "source_id", "content_hash"),)
        indexes = (("project_id",), ("source_id",), ("content_hash",))


class KnowledgeItem(Model):
    """L2 structured knowledge entry (``09-knowledge-system.md`` §3).

    One polymorphic table, ``kind`` distinguishes the payload shape inside
    ``content``. The stable identity is ``(project_id, kind, key)``; ``key``
    is a human-stable identifier (e.g. ``crypto.py::hash_password::L20`` for
    a docstring rationale) that never embeds extractor line numbers.
    """

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    version_id = fields.UUIDField(null=True)  # → mc_test_project_version（图谱按版本过滤）
    source_id = fields.UUIDField(null=True)  # → mc_knowledge_source (not built yet)
    kind = fields.CharField(max_length=32)  # rationale|concept|rule|component|api|db_table|...
    key = fields.CharField(max_length=512)
    title = fields.TextField(null=True)
    content = fields.JSONField(default=dict)
    status = fields.CharField(max_length=16, default="candidate")  # candidate|confirmed|stale
    confidence = fields.FloatField(default=1.0)
    provenance = fields.JSONField(default=dict)  # extractor/version/mode/graphify_id
    version = fields.IntField(default=1)
    # Owning code anchor for rationale/doc_ref items (single-owner convenience;
    # multi-anchor bindings are expressed through mc_knowledge_edge).
    anchor_id = fields.UUIDField(null=True)
    # Graphify's node id — a *source fingerprint* only, never part of identity.
    source_node_id = fields.CharField(max_length=255, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_knowledge_item"
        # 身份唯一性走**部分唯一索引**（见 knowledge/_version_indexes.py）：
        # PG 的 UNIQUE 视 NULL 互异，version_id 可空（存量遗留），普通
        # unique_together 无法对 NULL 去重。故此处不声明 unique_together，
        # 由启动期迁移建两条部分索引（versioned + legacy）。
        indexes = (("project_id",), ("kind",), ("source_id",), ("anchor_id",), ("version_id",))


class KnowledgeAnchor(Model):
    """Code anchor (alignment layer, ``09-knowledge-system.md`` §4.1).

    Identity is exactly the design's: ``(project_id, kind, path, symbol)``.
    ``content_hash`` changes when the referenced source lines change — that is
    the drift signal downstream freshness checks rely on.
    """

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    version_id = fields.UUIDField(null=True)  # → mc_test_project_version
    kind = fields.CharField(max_length=16)  # function|class|file|endpoint|route|table
    path = fields.CharField(max_length=1024)  # repo-relative path, forward slashes
    symbol = fields.CharField(max_length=512)  # class/function name; "" for module/file anchors
    content_hash = fields.CharField(max_length=64, null=True)
    label = fields.CharField(max_length=512, null=True)  # Graphify display label
    source_location = fields.CharField(max_length=32, null=True)  # "L9"
    community = fields.IntField(null=True)
    provenance = fields.JSONField(default=dict)
    source_node_id = fields.CharField(max_length=255, null=True)  # Graphify fingerprint
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_knowledge_anchor"
        # 身份唯一性走部分唯一索引（NULL 安全的 versioned + legacy 两条），
        # 见 knowledge/_version_indexes.py。故不声明 unique_together。
        indexes = (("project_id",), ("path",), ("kind",), ("version_id",))


class KnowledgeEdge(Model):
    """L3 graph edge (``09-knowledge-system.md`` §6).

    Nodes are either anchors or items (``from_kind``/``to_kind`` disambiguate
    the polymorphic ``from_id``/``to_id`` references). ``relation`` keeps the
    Graphify vocabulary (``calls``/``contains``/``imports``/``references``/
    ``rationale_for``/...) verbatim — the design's enum is a starting point,
    not final DDL, and downstream impact queries read relation as a string.

    Identity = ``(project_id, relation, from_kind, from_id, to_kind, to_id)``
    where ``from_id``/``to_id`` are *our* anchor/item uuids, so re-running the
    same extraction against the same project never duplicates an edge.
    """

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    version_id = fields.UUIDField(null=True)  # → mc_test_project_version
    relation = fields.CharField(max_length=32)
    from_kind = fields.CharField(max_length=8)  # anchor|item
    from_id = fields.UUIDField()
    to_kind = fields.CharField(max_length=8)
    to_id = fields.UUIDField()
    confidence = fields.FloatField(default=1.0)
    provenance = fields.JSONField(default=dict)
    source_file = fields.CharField(max_length=1024, null=True)
    source_location = fields.CharField(max_length=32, null=True)
    content_hash = fields.CharField(max_length=64, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_knowledge_edge"
        # 身份唯一性走部分唯一索引（NULL 安全），见 knowledge/_version_indexes.py。
        indexes = (("project_id",), ("from_id",), ("to_id",), ("relation",), ("version_id",))


class TestProfile(Model):
    """L4 derived per-anchor test profile (``design.md`` §15.1).

    Pure derivation, never hand-maintained: downstream coverage/impact queries
    compute it and upsert here. ``anchor_id`` is unique — one profile per anchor.
    """

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    anchor_id = fields.UUIDField(unique=True)
    rule_ids = fields.JSONField(default=list)
    component_ids = fields.JSONField(default=list)
    bug_pattern_ids = fields.JSONField(default=list)
    template_ids = fields.JSONField(default=list)
    coverage_gaps = fields.JSONField(default=dict)
    last_tested_at = fields.DatetimeField(null=True)
    test_status = fields.CharField(max_length=16, default="green")  # green|flaky|red
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_profile"
        indexes = (("anchor_id",), ("project_id",))


async def create_tables_via_pg() -> None:
    """Create ``mc_knowledge_*`` tables through the platform raw PG client.

    Used by deployments where Tortoise is not initialized (main service path).
    Matches the platform convention ``from db import PostgresClient``.
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)