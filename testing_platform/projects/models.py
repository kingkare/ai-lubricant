"""Tortoise models for the **test-project domain** (``mc_test_project*``).

The Test Project is the **independent project entity for the whole-project
dimension** — **not** the SUT, **not** the coding project (which is dev-only).
It owns **no code**, only **git configuration** (1-N repos); code is fetched as
snapshots when needed. Code / requirements / cases / management are all **new
tables here**, zero overlap with the coding project — coding will plug in later
as a subset.

Tables (defined verbatim in ``docs/ai-testing-platform/test-project-definition.md``
§十):

======  ==========================================================
表      说明
======  ==========================================================
``mc_test_project``          测试项目（项目容器，全维度）
``mc_test_project_git``      git 配置（1-N，URL/凭据引用）
``mc_test_project_source``   源配置（A/B 轨适配器 + 同步策略）
``mc_test_project_release``  版本指针快照（**deprecated** → version）
``mc_test_project_version``  版本一等实体（仓库分支选择 + 图谱按版本过滤）
======  ==========================================================

Conventions follow ``testing_platform/knowledge/models.py``:

* ``id`` UUID PK,
* ``mc_test_`` prefix,
* JSONB payloads via ``JSONField``,
* ``credential_ref`` stores a **reference** (never plaintext) — resolution is
  the git-source layer's job (env var / platform git identity / signed URL).
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

# --------------------------------------------------------------------------
# Raw DDL mirroring the models below (for ``PostgresClient.pool`` deployments).
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_project (
        id                UUID PRIMARY KEY,
        code              VARCHAR(32)  NOT NULL,
        name              VARCHAR(256) NOT NULL,
        description       TEXT,
        coding_project_id UUID,
        status            VARCHAR(16)  NOT NULL DEFAULT 'active',
        version           INTEGER      NOT NULL DEFAULT 1,
        testers           JSONB        NOT NULL DEFAULT '[]'::jsonb,
        developers        JSONB        NOT NULL DEFAULT '[]'::jsonb,
        product_managers  JSONB        NOT NULL DEFAULT '[]'::jsonb,
        created_by        UUID,
        created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_project_git (
        id             UUID PRIMARY KEY,
        project_id     UUID         NOT NULL,
        name           VARCHAR(128) NOT NULL,
        repo_url       VARCHAR(1024) NOT NULL,
        ref_type       VARCHAR(16)  NOT NULL DEFAULT 'branch',
        ref_value      VARCHAR(512) NOT NULL,
        credential_ref JSONB        NOT NULL DEFAULT '{}'::jsonb,
        status         VARCHAR(16)  NOT NULL DEFAULT 'active',
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_project_source (
        id             UUID PRIMARY KEY,
        project_id     UUID         NOT NULL,
        kind           VARCHAR(16)  NOT NULL,
        adapter        VARCHAR(16)  NOT NULL DEFAULT 'builtin',
        adapter_type   VARCHAR(32)  NOT NULL DEFAULT 'custom',
        name           VARCHAR(256) NOT NULL,
        config         JSONB        NOT NULL DEFAULT '{}'::jsonb,
        sync_policy    VARCHAR(16)  NOT NULL DEFAULT 'manual',
        schedule       JSONB        NOT NULL DEFAULT '{}'::jsonb,
        last_synced_at TIMESTAMPTZ,
        last_sync_result JSONB      NOT NULL DEFAULT '{}'::jsonb,
        status         VARCHAR(16)  NOT NULL DEFAULT 'active',
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, kind, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_project_release (
        id            UUID PRIMARY KEY,
        project_id    UUID         NOT NULL,
        version_code  VARCHAR(64)  NOT NULL,
        git_tags      JSONB        NOT NULL DEFAULT '[]'::jsonb,
        prd_versions  JSONB        NOT NULL DEFAULT '[]'::jsonb,
        case_versions JSONB        NOT NULL DEFAULT '[]'::jsonb,
        note          TEXT,
        created_by    UUID,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, version_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_project_version (
        id           UUID PRIMARY KEY,
        project_id   UUID         NOT NULL,
        version_code VARCHAR(64)  NOT NULL,
        name         VARCHAR(128),
        repos        JSONB        NOT NULL DEFAULT '[]'::jsonb,
        is_initial   BOOLEAN      NOT NULL DEFAULT false,
        status       VARCHAR(16)  NOT NULL DEFAULT 'draft',
        testers      JSONB        NOT NULL DEFAULT '[]'::jsonb,
        developers   JSONB        NOT NULL DEFAULT '[]'::jsonb,
        product_managers JSONB    NOT NULL DEFAULT '[]'::jsonb,
        note         TEXT,
        created_by   UUID,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, version_code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_project_git_project    ON mc_test_project_git(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_project_source_project ON mc_test_project_source(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_project_source_kind    ON mc_test_project_source(kind)",
    "CREATE INDEX IF NOT EXISTS idx_test_project_release_project ON mc_test_project_release(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_project_version_project ON mc_test_project_version(project_id)",
]

#: ``mc_test_project.status``
PROJECT_STATUSES: tuple[str, ...] = ("active", "archived")

#: ``mc_test_project_git.ref_type``
GIT_REF_TYPES: tuple[str, ...] = ("branch", "tag", "commit")

#: ``mc_test_project_git.status`` / ``mc_test_project_source.status``
GIT_STATUSES: tuple[str, ...] = ("active", "paused", "archived")
SOURCE_STATUSES: tuple[str, ...] = ("active", "paused", "archived")

#: ``mc_test_project_source.kind`` — 源性质（与 mc_knowledge_source.kind 解耦）
SOURCE_KINDS: tuple[str, ...] = ("prd", "case", "code", "openapi")

#: ``mc_test_project_source.adapter`` — A 轨 builtin / B 轨 agent
SOURCE_ADAPTERS: tuple[str, ...] = ("builtin", "agent")

#: ``mc_test_project_source.sync_policy``
SOURCE_SYNC_POLICIES: tuple[str, ...] = ("manual", "scheduled", "webhook")


class TestProject(Model):
    """测试项目（项目容器，全维度，与 coding 零重合）。

    ``coding_project_id`` 是「将来 coding 作为子集接入」的桥（design 二）：测试域
    发现的 bug 复用 ``ProjectIssue(issue_type=bug)``，挂在 coding 项目下，故测试项目
    指向一个 coding 项目做桥。空表示尚未关联（bug 域会拒建）。

    ``testers`` / ``developers`` / ``product_managers`` 是**项目级角色成员**
    （平台用户 UUID 列表，JSONB 不做 FK——前端按用户目录解析姓名）。三者对齐
    禅道团队角色；与策略级执行人分配分工不同：**策略的人是「这一轮谁测」，
    项目的人是「这个项目谁负责哪块」**（用户定版 2026-10-15）。
    """

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32, unique=True)  # TP-xxxx
    name = fields.CharField(max_length=256)
    description = fields.TextField(null=True)
    coding_project_id = fields.UUIDField(null=True)  # → mc_project（design 二：桥）
    status = fields.CharField(max_length=16, default="active")  # active|archived
    version = fields.IntField(default=1)
    #: 项目级角色成员（平台用户 UUID 列表）：测试 / 开发 / 产品三类。
    testers = fields.JSONField(default=list)
    developers = fields.JSONField(default=list)
    product_managers = fields.JSONField(default=list)
    created_by = fields.UUIDField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_project"


class TestProjectGit(Model):
    """git 配置（1-N）。``credential_ref`` 是引用（env/identity id），不存明文。"""

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    name = fields.CharField(max_length=128)  # 仓库名如 backend
    repo_url = fields.CharField(max_length=1024)
    ref_type = fields.CharField(max_length=16, default="branch")  # branch|tag|commit
    ref_value = fields.CharField(max_length=512)  # 分支名 / tag / commit sha
    credential_ref = fields.JSONField(default=dict)  # {"kind":"env","name":"GIT_TOKEN"|...}
    status = fields.CharField(max_length=16, default="active")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_project_git"
        unique_together = (("project_id", "name"),)
        indexes = (("project_id",),)


class TestProjectSource(Model):
    """源配置（A/B 轨 + 同步策略）。"""

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    kind = fields.CharField(max_length=16)  # prd|case|code
    adapter = fields.CharField(max_length=16, default="builtin")  # builtin|agent
    adapter_type = fields.CharField(max_length=32, default="custom")  # excel|word|json|xmind|feishu|...|custom
    name = fields.CharField(max_length=256)
    config = fields.JSONField(default=dict)
    sync_policy = fields.CharField(max_length=16, default="manual")  # manual|scheduled|webhook
    schedule = fields.JSONField(default=dict)
    last_synced_at = fields.DatetimeField(null=True)
    last_sync_result = fields.JSONField(default=dict)
    status = fields.CharField(max_length=16, default="active")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_project_source"
        unique_together = (("project_id", "kind", "name"),)
        indexes = (("project_id",), ("kind",))


class TestProjectRelease(Model):
    """版本指针快照（**deprecated**：新流程用 :class:`TestProjectVersion`）。

    历史语义：打版本时把各源“当前指针”（git 分支名 / 数据源 content_hash）
    冻结成 JSONB 数组，供 ``project.release.diff`` 增量测试用。存的是**分支名
    而非 commit**，不可复现——新版本实体记录每仓 resolved commit，图谱按
    版本过滤。表与方法保留，供存量数据兼容。
    """

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    version_code = fields.CharField(max_length=64)  # v1.2
    git_tags = fields.JSONField(default=list)  # [{git_id, tag}]
    prd_versions = fields.JSONField(default=list)  # [{source_id, version}]
    case_versions = fields.JSONField(default=list)
    note = fields.TextField(null=True)
    created_by = fields.UUIDField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_project_release"
        unique_together = (("project_id", "version_code"),)
        indexes = (("project_id",),)


#: ``mc_test_project_version.status`` 版本状态机
#: ``draft``——只配了仓库/分支，未构建图谱；``graph_built``——已生成版本知识图谱
VERSION_STATUSES: tuple[str, ...] = ("draft", "graph_built")

#: ``mc_test_project_version.repos[]`` 单仓项字段说明
#: ``git_id``（关联 mc_test_project_git）、``ref_type``（branch|tag|commit）、
#: ``ref``（分支名/tag/commit sha）、``is_primary``（主分支标记，最多一项）、
#: ``resolved_commit``（构建图谱时解析出的 commit sha，可空）
VERSION_REPO_FIELDS: tuple[str, ...] = ("git_id", "ref_type", "ref", "is_primary", "resolved_commit")


class TestProjectVersion(Model):
    """测试项目版本（一等实体）——**知识图谱按版本生成与过滤**。

    流程（test-project-definition 修正版）：建项目 → 配可用仓库 → **建版本**。
    项目只管“有哪些仓库”；版本去选仓库的分支（每仓各自 ref，多仓多选），
    形成代码 diff → 生成该版本的知识图谱（``status=graph_built``）。

    - ``repos``      ``[{"git_id","ref_type","ref","is_primary","resolved_commit"}]``
      ``is_primary=true`` 的仓是主分支（master/main，代表该版本的生成状态）。
    - ``is_initial`` 首个版本自动置 true（初始版本必然存在）。
    - 知识表 ``mc_knowledge_anchor/item/edge.version_id`` 指向本表——
      图谱查询按版本过滤，旧版本图谱保留可查。
    - ``testers``/``developers``/``product_managers`` 是**版本级角色成员**
      （平台用户 UUID 列表；可空，缺省继承/为空——版本可覆盖项目级角色，见
      ``service.resolve_version_roles``）。
    """

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    version_code = fields.CharField(max_length=64)  # v1.2
    name = fields.CharField(max_length=128, null=True)
    repos = fields.JSONField(default=list)  # 见 VERSION_REPO_FIELDS
    is_initial = fields.BooleanField(default=False)
    status = fields.CharField(max_length=16, default="draft")
    #: 版本级角色成员（平台用户 UUID 列表）：测试 / 开发 / 产品三类（可空）。
    testers = fields.JSONField(default=list)
    developers = fields.JSONField(default=list)
    product_managers = fields.JSONField(default=list)
    note = fields.TextField(null=True)
    created_by = fields.UUIDField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_project_version"
        unique_together = (("project_id", "version_code"),)
        indexes = (("project_id",),)


async def create_tables_via_pg() -> None:
    """Create ``mc_test_project*`` tables through the platform raw PG client."""
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
