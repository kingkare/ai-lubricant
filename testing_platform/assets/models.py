"""Tortoise models for the testing-platform **asset domain**.

Implements the tables defined in ``docs/ai-testing-platform/plans/01-asset-model.md``
§1–§4/§6 (cross-checked against ``design.md`` §15.3/§15.4/§15.12):

======  ==========================================================
表      说明
======  ==========================================================
``mc_test_requirement``   需求（主脊：版本下挂需求，需求下挂用例/场景/接口/缺陷）
``mc_test_tag``           标签（§1）
``mc_test_case``          用例——业务视角（§2，**不含步骤**）
``mc_test_case_tag``      用例↔标签 多对多（§2.2）
``mc_test_case_scenario`` 用例↔场景 多对多（§2.1）
``mc_test_scenario``      场景——实现视角（§3，含 local_vars/flow）
``mc_test_scenario_step`` 步骤——类型化（§4）
``mc_test_transport``     传输层/接口管理（design §15.4，含鉴权/公共数据）
``mc_test_method_module`` 方法脚本「类」（§5.1 重构：一个脚本 = 一个类，全静态方法）
``mc_test_method``        方法注册表（§5.1）
``mc_test_dataset``       数据集（§6 / design §15.4）
``mc_test_environment``   测试环境（design §15.4 / 05：分层继承）
``mc_test_env_variable``  环境变量（design §15.4 / 05：7 类型 + 密钥脱敏）
``mc_test_custom_tool``   自定义工具（design §15.5：只拥有签名 + 指向）
``mc_test_custom_tool_version`` 自定义工具版本快照（design §15.5）
``mc_test_change_record`` 变更记录（design §15.12，唯一版本机制）
======  ==========================================================

Conventions follow ``user_platform/models_project.py`` and the sibling
``testing_platform/knowledge/models.py``:

* ``id`` is a UUID primary key,
* ``mc_`` table prefix,
* JSONB payloads use ``JSONField``,
* every ``*_id`` that points at a table **not** built in this package
  (``dataset_id`` / ``transport_id`` / ``env_id``) stays a plain ``UUIDField``
  placeholder — see §2/§3 of the plan (``transport_id`` 明确「先留」).

``mc_test_scenario_step.scenario_id`` is a real FK (§4) to ``mc_test_scenario``;
the many-to-many link tables use plain UUID columns, mirroring
``mc_knowledge_edge``'s ``(from_id, to_id)`` style.

The same tables are exposed to the raw asyncpg path used by the main service:
``DDL`` + ``create_tables_via_pg()`` keep a ``PostgresClient``-only deployment
able to create the schema without Tortoise being initialized.
"""
from __future__ import annotations

import json as _json

from tortoise import fields
from tortoise.models import Model


class JSONValueField(fields.JSONField):
    """JSONB 字段，支持任意 JSON 原生值（**含顶层字符串**）。

    Tortoise 内置 ``JSONField`` 把「Python str」一律当 **JSON 文本**处理：
    裸字符串（``"https://x"``）既非法也无法无损存取。本字段把顶层字符串
    编码成 JSON 字符串字面量（``json.dumps`` → ``"\"https://x\""``）——
    存储是合法 JSON、读回解码成原生 ``str``，与 Postgres JSONB 的字符串
    语义一致（环境变量 ``type=string`` 的值就存成带引号的 JSON 字符串）。

    非 str 值（int/float/bool/list/dict）走内置路径（encoder 编码）。
    """

    def to_db_value(self, value, instance):  # type: ignore[override]
        if isinstance(value, str):
            return _json.dumps(value)
        return super().to_db_value(value, instance)

    def to_python_value(self, value):  # type: ignore[override]
        if isinstance(value, str):
            # DB 读回：值是本字段 json.dumps 过的 JSON 字符串字面量；
            # 解码失败则原样返回（Python 侧直接赋的裸字符串）。
            try:
                return self.decoder(value)
            except Exception:  # noqa: BLE001 —— 裸字符串原样保留
                return value
        return super().to_python_value(value)

# --------------------------------------------------------------------------
# Raw DDL mirroring the models below (for ``PostgresClient.pool`` deployments).
# Order matters: parent tables before children (scenario before step).
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_tag (
        id           UUID PRIMARY KEY,
        code         VARCHAR(64)  NOT NULL,
        project_id   UUID         NOT NULL,
        name         VARCHAR(128) NOT NULL,
        category     VARCHAR(32)  NOT NULL,
        description  TEXT,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_case (
        id                UUID PRIMARY KEY,
        code              VARCHAR(32)  NOT NULL,
        project_id        UUID         NOT NULL,
        title             VARCHAR(512) NOT NULL,
        intent            TEXT,
        preconditions     JSONB        NOT NULL DEFAULT '[]'::jsonb,
        expected          JSONB        NOT NULL DEFAULT '{}'::jsonb,
        steps             JSONB        NOT NULL DEFAULT '[]'::jsonb,
        dataset_id        UUID,
        confirm_source    VARCHAR(32)  NOT NULL,
        confirm_ref       JSONB        NOT NULL DEFAULT '{}'::jsonb,
        origin            VARCHAR(32)  NOT NULL DEFAULT 'manual',
        priority          VARCHAR(8),
        module            VARCHAR(128),
        feature           VARCHAR(128),
        version_id        UUID,
        requirement_id     UUID,
        test_type         VARCHAR(32),
        test_types        JSONB        DEFAULT '[]'::jsonb,   -- 多值类型（新增；test_type 旧单值保留兼容）
        slot_key          VARCHAR(256),
        automation_status VARCHAR(32)  NOT NULL DEFAULT 'not_automated',
        status            VARCHAR(32)  NOT NULL DEFAULT 'draft',
        version           INTEGER      NOT NULL DEFAULT 1,
        created_by        UUID         NOT NULL,
        created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
        folder_id         UUID,                                 -- → mc_test_catalog(type=case)；裸 UUID 无 FK
        UNIQUE (project_id, code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_case_folder ON mc_test_case(folder_id)",
    """
    CREATE TABLE IF NOT EXISTS mc_test_case_tag (
        id         UUID PRIMARY KEY,
        case_id    UUID NOT NULL,
        tag_id     UUID NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (case_id, tag_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_case_scenario (
        id           UUID PRIMARY KEY,
        case_id      UUID    NOT NULL,
        scenario_id  UUID    NOT NULL,
        is_primary   BOOLEAN NOT NULL DEFAULT false,
        "order"      INTEGER NOT NULL DEFAULT 0,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (case_id, scenario_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_scenario (
        id             UUID PRIMARY KEY,
        code           VARCHAR(32)  NOT NULL,
        project_id     UUID         NOT NULL,
        name           VARCHAR(512) NOT NULL,
        purpose        TEXT,
        transport_id   UUID,
        env_id         UUID,
        version_id     UUID,
        requirement_id UUID,
        folder_id      UUID,
        local_vars     JSONB NOT NULL DEFAULT '{}'::jsonb,
        flow           JSONB NOT NULL DEFAULT '{}'::jsonb,
        exec_mode      VARCHAR(32)  NOT NULL DEFAULT 'script_only',
        status         VARCHAR(32)  NOT NULL DEFAULT 'draft',
        version        INTEGER      NOT NULL DEFAULT 1,
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_scenario_step (
        id           UUID PRIMARY KEY,
        scenario_id  UUID         NOT NULL REFERENCES mc_test_scenario(id) ON DELETE CASCADE,
        step_key     VARCHAR(128) NOT NULL,
        seq          INTEGER      NOT NULL,
        name         VARCHAR(512),
        description  TEXT,
        type         VARCHAR(32)  NOT NULL,
        params       JSONB        NOT NULL DEFAULT '{}'::jsonb,
        pre_cond     JSONB        NOT NULL DEFAULT '[]'::jsonb,
        post_checks  JSONB        NOT NULL DEFAULT '[]'::jsonb,
        post_cond    JSONB        NOT NULL DEFAULT '[]'::jsonb,
        on_error     VARCHAR(64),
        enabled      BOOLEAN      NOT NULL DEFAULT true,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (scenario_id, step_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_change_record (
        id             UUID PRIMARY KEY,
        entity_type    VARCHAR(32) NOT NULL,
        entity_id      UUID        NOT NULL,
        created_by     UUID,
        change_reason  TEXT        NOT NULL,
        field_changes  JSONB       NOT NULL DEFAULT '{}'::jsonb,
        propagation    JSONB       NOT NULL DEFAULT '{}'::jsonb,
        rollback_ref   JSONB,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_transport (
        id              UUID PRIMARY KEY,
        code            VARCHAR(128) NOT NULL,
        project_id      UUID         NOT NULL,
        kind            VARCHAR(8)   NOT NULL DEFAULT 'http',
        description     VARCHAR(512),
        module          VARCHAR(128),
        requirement_id  UUID,
        auth            JSONB        NOT NULL DEFAULT '{}'::jsonb,
        common_headers  JSONB        NOT NULL DEFAULT '{}'::jsonb,
        common_params   JSONB        NOT NULL DEFAULT '{}'::jsonb,
        path_rewrite    JSONB        NOT NULL DEFAULT '{}'::jsonb,
        status          VARCHAR(32)  NOT NULL DEFAULT 'draft',
        version         INTEGER      NOT NULL DEFAULT 1,
        created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_method_module (
        id            UUID PRIMARY KEY,
        code          VARCHAR(128) NOT NULL,
        project_id    UUID,
        name          VARCHAR(256) NOT NULL,
        description   TEXT,
        source        TEXT         NOT NULL DEFAULT '',
        async_native  BOOLEAN      NOT NULL DEFAULT false,
        status        VARCHAR(32)  NOT NULL DEFAULT 'draft',
        version       INTEGER      NOT NULL DEFAULT 1,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_method (
        id          UUID PRIMARY KEY,
        module_id   UUID         NOT NULL REFERENCES mc_test_method_module(id) ON DELETE CASCADE,
        name        VARCHAR(128) NOT NULL,
        full_ref    VARCHAR(256) NOT NULL,
        signature   JSONB        NOT NULL DEFAULT '{}'::jsonb,
        is_async    BOOLEAN      NOT NULL DEFAULT false,
        description TEXT,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (module_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_dataset (
        id          UUID PRIMARY KEY,
        code        VARCHAR(128) NOT NULL,
        project_id  UUID         NOT NULL,
        "schema"    JSONB        NOT NULL DEFAULT '{}'::jsonb,
        rows        JSONB        NOT NULL DEFAULT '[]'::jsonb,
        version     INTEGER      NOT NULL DEFAULT 1,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_environment (
        id           UUID PRIMARY KEY,
        code         VARCHAR(128) NOT NULL,
        name         VARCHAR(256) NOT NULL,
        project_id   UUID         NOT NULL,
        parent_id    UUID         REFERENCES mc_test_environment(id) ON DELETE SET NULL,
        description  TEXT,
        is_default   BOOLEAN      NOT NULL DEFAULT false,
        status       VARCHAR(32)  NOT NULL DEFAULT 'active',
        version      INTEGER      NOT NULL DEFAULT 1,
        created_by   UUID,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_env_variable (
        id          UUID PRIMARY KEY,
        env_id      UUID         NOT NULL REFERENCES mc_test_environment(id) ON DELETE CASCADE,
        key         VARCHAR(256) NOT NULL,
        value       JSONB        NOT NULL DEFAULT 'null'::jsonb,
        type        VARCHAR(16)  NOT NULL DEFAULT 'string',
        purpose     TEXT,
        is_secret   BOOLEAN      NOT NULL DEFAULT false,
        version     INTEGER      NOT NULL DEFAULT 1,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (env_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_custom_tool (
        id          UUID PRIMARY KEY,
        code        VARCHAR(128) NOT NULL,
        project_id  UUID         NOT NULL,
        scope       VARCHAR(16)  NOT NULL DEFAULT 'project',
        purpose     TEXT,
        signature   JSONB        NOT NULL DEFAULT '{}'::jsonb,
        impl_ref    JSONB        NOT NULL DEFAULT '{}'::jsonb,
        status      VARCHAR(32)  NOT NULL DEFAULT 'draft',
        version     INTEGER      NOT NULL DEFAULT 1,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_custom_tool_version (
        id            UUID PRIMARY KEY,
        tool_id       UUID         NOT NULL REFERENCES mc_test_custom_tool(id) ON DELETE CASCADE,
        version       INTEGER      NOT NULL,
        snapshot      JSONB        NOT NULL DEFAULT '{}'::jsonb,
        change_reason TEXT         NOT NULL,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (tool_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_requirement (
        id                  UUID PRIMARY KEY,
        code                VARCHAR(32)  NOT NULL,
        project_id          UUID         NOT NULL,
        version_id          UUID,
        title               VARCHAR(512) NOT NULL,
        description         TEXT,
        content             JSONB        NOT NULL DEFAULT '{}'::jsonb,
        mindmap             JSONB        NOT NULL DEFAULT '{}'::jsonb,
        acceptance_criteria JSONB        NOT NULL DEFAULT '[]'::jsonb,
        priority            VARCHAR(8),
        module              VARCHAR(128),
        folder_id           UUID,
        origin              VARCHAR(32)  NOT NULL DEFAULT 'manual',
        source_ref          JSONB        NOT NULL DEFAULT '{}'::jsonb,
        status              VARCHAR(32)  NOT NULL DEFAULT 'draft',
        analysis            JSONB        NOT NULL DEFAULT '{}'::jsonb,
        review              JSONB        NOT NULL DEFAULT '{}'::jsonb,
        gen_status          VARCHAR(32)  NOT NULL DEFAULT 'not_started',
        elements            JSONB        NOT NULL DEFAULT '{}'::jsonb,
        version             INTEGER      NOT NULL DEFAULT 1,
        created_by          UUID,
        created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    # 需求附件（需求 = 文档：可挂 Word/思维导图/图片/任意文件）。文件本体在 OSS，
    # 本表只存 URL 与元数据（浏览器直传 OSS 拿 URL，服务端不碰字节）。
    """
    CREATE TABLE IF NOT EXISTS mc_test_requirement_attachment (
        id             UUID PRIMARY KEY,
        requirement_id UUID         NOT NULL,
        project_id     UUID         NOT NULL,
        name           VARCHAR(512) NOT NULL,
        url            TEXT         NOT NULL,
        mime_type      VARCHAR(128),
        size_bytes     BIGINT,
        kind           VARCHAR(32)  NOT NULL DEFAULT 'file',
        source_ref     JSONB        NOT NULL DEFAULT '{}'::jsonb,
        uploaded_by    UUID,
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_case_project     ON mc_test_case(project_id)",
    # slot_key 幂等唯一（17 号文 §3.3）：非空时项目内唯一；存量/人工行（NULL）不受约束。
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_test_case_slot_key ON mc_test_case(project_id, slot_key) WHERE slot_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_test_tag_project      ON mc_test_tag(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_scenario_project ON mc_test_scenario(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_step_scenario    ON mc_test_scenario_step(scenario_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_case_tag_case    ON mc_test_case_tag(case_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_case_tag_tag     ON mc_test_case_tag(tag_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_cs_case          ON mc_test_case_scenario(case_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_cs_scenario      ON mc_test_case_scenario(scenario_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_change_entity    ON mc_test_change_record(entity_type, entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_transport_project ON mc_test_transport(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_requirement_project ON mc_test_requirement(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_requirement_version ON mc_test_requirement(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_requirement_folder  ON mc_test_requirement(folder_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_req_attach_req      ON mc_test_requirement_attachment(requirement_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_case_requirement    ON mc_test_case(requirement_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_scenario_requirement ON mc_test_scenario(requirement_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_scenario_folder      ON mc_test_scenario(folder_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_transport_requirement ON mc_test_transport(requirement_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_module_project   ON mc_test_method_module(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_method_module    ON mc_test_method(module_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_method_fullref   ON mc_test_method(full_ref)",
    "CREATE INDEX IF NOT EXISTS idx_test_dataset_project  ON mc_test_dataset(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_env_project      ON mc_test_environment(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_env_parent       ON mc_test_environment(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_env_var_env      ON mc_test_env_variable(env_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_custom_tool_project ON mc_test_custom_tool(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_custom_tool_ver_tool ON mc_test_custom_tool_version(tool_id)",
    # ── 接口管理（Apifox 式一等实体；design §15.4 的接口侧）──────────
    """
    CREATE TABLE IF NOT EXISTS mc_test_api_folder (
        id         UUID PRIMARY KEY,
        project_id UUID NOT NULL,
        parent_id  UUID REFERENCES mc_test_api_folder(id) ON DELETE CASCADE,
        name       VARCHAR(128) NOT NULL,
        "order"    INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_api (
        id             UUID PRIMARY KEY,
        project_id     UUID NOT NULL,
        code           VARCHAR(64),                 -- API-0001（可读句柄，非唯一标识；身份是 id）
        folder_id      UUID REFERENCES mc_test_api_folder(id) ON DELETE SET NULL,  -- → 目录（删目录归根）
        name           VARCHAR(256) NOT NULL,
        method         VARCHAR(8)   NOT NULL,
        path           VARCHAR(512) NOT NULL,
        protocol       VARCHAR(8)   NOT NULL DEFAULT 'http',  -- http|ws|grpc
        status         VARCHAR(32)  NOT NULL DEFAULT 'designing',
        description    TEXT,
        tags           JSONB NOT NULL DEFAULT '[]'::jsonb,
        owner_id       UUID,
        priority       VARCHAR(8),
        "order"        INTEGER NOT NULL DEFAULT 0,
        params         JSONB NOT NULL DEFAULT '[]'::jsonb,   -- 结构说明（非具体值）
        headers        JSONB NOT NULL DEFAULT '[]'::jsonb,   -- 结构说明
        cookies        JSONB NOT NULL DEFAULT '[]'::jsonb,   -- Cookie 说明（独立于 params/headers）
        body_type      VARCHAR(32)  NOT NULL DEFAULT 'none',
        request_body   JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 结构说明（schema 树 + 示例）
        responses      JSONB NOT NULL DEFAULT '[]'::jsonb,   -- 多响应结构说明
        requirement_id UUID,
        version_id     UUID,                        -- → 发布版本（与生产环境一致；后续跟踪）
        source         VARCHAR(32)  NOT NULL DEFAULT 'manual', -- manual|openapi|swagger|postman|har|repo_scan|ai
        source_ref     JSONB NOT NULL DEFAULT '{}'::jsonb,
        version        INTEGER NOT NULL DEFAULT 1,
        created_by     UUID,
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_by     UUID,
        updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
        -- 身份 = id（UUID），不用 code 做唯一键（用户定版：引用走接口 id）
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_api_project  ON mc_test_api(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_folder   ON mc_test_api(folder_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_method   ON mc_test_api(project_id, method)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_status   ON mc_test_api(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_ver      ON mc_test_api(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_folder_project ON mc_test_api_folder(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_folder_parent  ON mc_test_api_folder(parent_id)",
    # ── 统一目录表（需求/用例/场景/接口共用一张，type 区分；用户定版 2026-10-14）──
    """
    CREATE TABLE IF NOT EXISTS mc_test_catalog (
        id         UUID PRIMARY KEY,
        project_id UUID NOT NULL,
        type       VARCHAR(32) NOT NULL,   -- requirement|case|scenario|api
        parent_id  UUID REFERENCES mc_test_catalog(id) ON DELETE CASCADE,
        name       VARCHAR(128) NOT NULL,
        "order"    INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_catalog_project_type ON mc_test_catalog(project_id, type)",
    "CREATE INDEX IF NOT EXISTS idx_test_catalog_parent       ON mc_test_catalog(parent_id)",
    # 接口↔用例 / 接口↔场景 多对多（P5：直连；裸 UUID 对，无 FK——同 case_scenario 惯例）
    """
    CREATE TABLE IF NOT EXISTS mc_test_api_case (
        id         UUID PRIMARY KEY,
        api_id     UUID    NOT NULL,
        case_id    UUID    NOT NULL,
        "order"    INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (api_id, case_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_api_scenario (
        id          UUID PRIMARY KEY,
        api_id      UUID    NOT NULL,
        scenario_id UUID    NOT NULL,
        "order"     INTEGER NOT NULL DEFAULT 0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (api_id, scenario_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_api_case_api   ON mc_test_api_case(api_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_case_case  ON mc_test_api_case(case_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_scn_api    ON mc_test_api_scenario(api_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_scn_scn    ON mc_test_api_scenario(scenario_id)",
    """
    CREATE TABLE IF NOT EXISTS mc_test_api_import_job (
        id            UUID PRIMARY KEY,
        project_id    UUID NOT NULL,
        name          VARCHAR(128) NOT NULL,
        source_format VARCHAR(16)  NOT NULL DEFAULT 'auto',  -- auto|openapi3|swagger2|postman|apifox|har
        spec_url      TEXT,                                   -- 文档 URL（URL 通道；定时导入只能用它）
        spec_text     TEXT,                                   -- 文档原文（粘贴/上传通道；仅手动）
        schedule      JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {"kind":manual|hourly|daily|weekly,"time":"HH:MM","weekday":0-6}
        enabled       BOOLEAN NOT NULL DEFAULT true,          -- false = 暂停定时（手动仍可跑）
        last_run_at   TIMESTAMPTZ,
        next_run_at   TIMESTAMPTZ,
        last_status   VARCHAR(16),                            -- ok|error|running
        last_message  TEXT,
        last_result   JSONB NOT NULL DEFAULT '{}'::jsonb,     -- 上次导入结果快照（created/updated/…）
        created_by    UUID,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_api_job_project ON mc_test_api_import_job(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_job_due     ON mc_test_api_import_job(enabled, next_run_at)",
    """
    CREATE TABLE IF NOT EXISTS mc_test_api_source (
        id            UUID PRIMARY KEY,
        api_id        UUID NOT NULL,
        project_id    UUID NOT NULL,
        job_id        UUID,                          -- → mc_test_api_import_job（绑定的导入任务；null=孤儿导入）
        source_format VARCHAR(16) NOT NULL,         -- auto|openapi3|swagger2|postman|apifox|har
        spec_url      TEXT,                          -- 文档 URL（同来源时供「同步」重抓）
        method        VARCHAR(8)  NOT NULL,          -- 该来源记录的 method（同一接口跨来源可能不同——保留来源态）
        path          VARCHAR(512) NOT NULL,         -- 该来源记录的 path
        last_synced_at TIMESTAMPTZ,                  -- 上次「同步」成功时间
        last_status   VARCHAR(16),                   -- ok|error
        last_message  TEXT,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_api_source_api    ON mc_test_api_source(api_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_api_source_job    ON mc_test_api_source(job_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_test_api_source_api_job ON mc_test_api_source(api_id, job_id) WHERE job_id IS NOT NULL",
]

#: ``mc_test_tag.category`` 枚举（§1 固定四类，design §15.3 决策 1）
TAG_CATEGORIES: tuple[str, ...] = ("purpose", "priority", "module", "type")

#: ``mc_test_case.confirm_source`` 枚举（§2，创建依据，必填）
CASE_CONFIRM_SOURCES: tuple[str, ...] = ("debug", "doc", "user_confirm")

#: ``mc_test_case.priority`` 枚举（design §20.1）
CASE_PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")

#: ``mc_test_requirement.status`` 状态机
REQUIREMENT_STATUSES: tuple[str, ...] = ("draft", "active", "deprecated", "archived")

#: ``mc_test_requirement.origin`` 来源
REQUIREMENT_ORIGINS: tuple[str, ...] = ("manual", "from_doc", "from_import")

#: ``mc_test_case.test_type`` 测试类型。追加式扩展（原 7 类保留，零迁移）：
#: 新增接口/UI/AI/性能——用户可理解的业务类型，与旧口径并存。
CASE_TEST_TYPES: tuple[str, ...] = (
    "smoke", "functional", "boundary", "exception", "permission",
    "security", "compatibility",
    "api", "ui", "ai", "performance",
)

#: ``mc_test_case.origin`` 来源（用例从哪来）。与 ``confirm_source``（该用例
#: 的成立依据/确认门，debug|doc|user_confirm）语义正交：origin=内容出处。
CASE_ORIGINS: tuple[str, ...] = (
    "ai_generated", "manual", "from_history", "from_requirement",
)

#: ``mc_test_case.status`` 状态机（§2）
CASE_STATUSES: tuple[str, ...] = (
    "draft",
    "pending_confirmation",
    "active",
    "deprecated",
    "archived",
)

#: ``mc_test_case.automation_status`` 枚举（§2）
CASE_AUTOMATION_STATUSES: tuple[str, ...] = (
    "not_automated",
    "in_progress",
    "automated",
    "broken",
)

#: ``mc_test_scenario.exec_mode`` 三模——**已停用**（用户定版：执行语义下沉到步骤类型
#: ai_judge/agent_action/agent_workflow）。列保留（存量行有值），create/update 不再写、
#: 校验器不再查；SCENARIO_EXEC_MODES 仅作历史枚举保留，勿在新代码消费。
SCENARIO_EXEC_MODES: tuple[str, ...] = ("script_only", "hybrid", "ai_only")

#: ``mc_test_catalog.type`` —— 统一目录表的**类型**（需求/用例/场景/接口共用一张表，
#: 彼此无关系，只靠 type 区分各自的目录树）。本轮只有 case 有调用方，其余预留给后续迁移。
CATALOG_TYPES: tuple[str, ...] = ("requirement", "case", "scenario", "api", "bug")


#: ``mc_test_scenario.status`` 状态机（§3 / design 十六「脚本」）
SCENARIO_STATUSES: tuple[str, ...] = (
    "draft",
    "verified",
    "active",
    "broken",
    "archived",
)

#: ``mc_test_scenario_step.type`` 步骤类型（§4.2 + 流程控制扩展）
STEP_TYPES: tuple[str, ...] = (
    "request",
    "page",
    "android",
    "ios",
    "wait",
    "script",
    "check",
    "sql",
    "ws",
    "custom",
    # AI/agent 步骤（执行侧先占位，run 到达时返回结构化 not_implemented）。
    "ai_judge",
    "agent_action",
    "agent_workflow",
    # 流程控制（executor 区域栈解释：判断/跳出/结束场景，无工具、无 post_checks）。
    "if",
    "else_if",
    "else",
    "end_if",
    "break",
    "end_scenario",
    # 引用场景（递归跑子场景、共享 ctx，local 变量父子直传；同 flow.pre_scenarios 机制）。
    "scenario_ref",
)

#: ``mc_test_change_record.entity_type`` 枚举（design §15.12）
#: transport / dataset / method_module 都带 ``version``，其版本变更同走本表
#: （design §15.3「版本机制统一」：一处版本真相，不另建 revision 表）。
CHANGE_ENTITY_TYPES: tuple[str, ...] = (
    "case",
    "scenario",
    "method",
    "method_module",
    "custom_tool",
    "knowledge_item",
    "requirement",
    "task",
    "transport",
    "api",
    "dataset",
    "environment",
    "strategy",
    "bug",
)

#: ``mc_test_transport.kind`` 枚举（design §15.4）
TRANSPORT_KINDS: tuple[str, ...] = ("http", "ws")

#: ``mc_test_api.status`` 状态机（对齐 Apifox「接口状态」；与场景/用例的 draft/active 区分）
API_STATUSES: tuple[str, ...] = (
    "designing",
    "developing",
    "testing",
    "done",
    "deprecated",
)

#: ``mc_test_api.method`` 枚举（HTTP 方法；OpenAPI 全集）
API_METHODS: tuple[str, ...] = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
)

#: ``mc_test_api.protocol`` 枚举
API_PROTOCOLS: tuple[str, ...] = ("http", "ws", "grpc")

#: ``mc_test_api.body_type`` 枚举（请求体类型）
API_BODY_TYPES: tuple[str, ...] = (
    "none",
    "json",
    "form-data",
    "x-www-form-urlencoded",
    "raw",
    "xml",
    "binary",
    "graphql",
)

#: ``mc_test_api.source`` 枚举（接口从哪来）
API_SOURCES: tuple[str, ...] = (
    "manual",
    "openapi",   # OpenAPI 3.x（含 Swagger UI 导出）
    "apifox",    # Apifox 原生格式导出
    "swagger",   # Swagger 2.0（老系统）
    "postman",   # Postman Collection v2.x
    "har",       # HAR 抓包（Chrome / Charles / Fiddler）
    "repo_scan",
    "ai",
)

#: ``mc_test_api_import_job.source_format`` 导入方式枚举。
#: ``auto`` = 按文档特征自动判定；其余 = 显式指定（不猜）。
#: 与 ``spec_normalizer.FORMATS`` 同源 + ``apifox`` + ``auto``。
API_SOURCE_FORMATS: tuple[str, ...] = (
    "auto", "openapi3", "swagger2", "postman", "apifox", "har",
)

#: ``mc_test_api_import_job.schedule.kind`` 节奏（复用任务的 cron/interval/once
#: 纯函数解析器 ``testing_platform.tasks.scheduler.next_fire``）。
#: ``manual``（无 schedule / ``kind=manual``）= 仅手动跑。
API_IMPORT_SCHEDULE_KINDS: tuple[str, ...] = ("manual", "cron", "interval", "once")

#: ``mc_test_transport.status`` 状态机（design §15.4；沿用场景式生命周期）
TRANSPORT_STATUSES: tuple[str, ...] = (
    "draft",
    "verified",
    "active",
    "broken",
    "archived",
)

#: ``mc_test_method_module.status`` 状态机（design §15.5）
METHOD_MODULE_STATUSES: tuple[str, ...] = (
    "draft",
    "verified",
    "active",
    "deprecated",
    "archived",
)

#: ``mc_test_environment.status`` 状态机（05 §2.1：active/archived，归档不删）
ENVIRONMENT_STATUSES: tuple[str, ...] = ("active", "archived")

#: ``mc_test_env_variable.type`` 六类型（05 §2.2/§4：保持原生类型，
#: ``{{env.port}}`` 是 int 就替换成 int）。
#: ``json``＝JSON 对象、``list``＝JSON 数组；**dict 已并入 json**——两者在
#: :func:`assets.service._coerce_env_value` 本就同一分支（都要求 Mapping），
#: 存量 ``type='dict'`` 行由 ``create_tables_via_pg`` 幂等归一到 ``json``。
ENV_VARIABLE_TYPES: tuple[str, ...] = (
    "string",
    "int",
    "float",
    "boolean",
    "json",
    "list",
)

#: ``mc_test_custom_tool.scope`` 枚举（design §15.5）
CUSTOM_TOOL_SCOPES: tuple[str, ...] = ("project", "team")

#: ``mc_test_custom_tool.status`` 状态机（design §15.5：draft|review|active|deprecated）
CUSTOM_TOOL_STATUSES: tuple[str, ...] = ("draft", "review", "active", "deprecated")


class TestRequirement(Model):
    """需求（主脊）—— ``mc_test_requirement``。

    需求是知识图谱的**中心**：版本下挂需求，需求下挂一切产出（用例/场景/接口/
    缺陷/代码）。这样每条数据都知道"服务于哪个需求"，不乱。

    - ``version_id``：需求归属版本（同项目不同版本需求不同）。
    - ``content``：富文本正文（Tiptap/ProseMirror 文档 JSON）——需求的**真相源**。
    - ``mindmap``：思维导图树 JSON（``{root:{id,text,children:[]}}``）。
    - ``description``：``content`` 的**纯文本投影**（AI 提示词/列表预览/向后兼容），
      由服务端在写入 content 时派生，不信任前端直传；老数据（无 content）回退用它。
    - ``acceptance_criteria``：验收标准（JSONB 数组）——需求的可测判据（可测闸要它）。
    - ``folder_id``：归属目录（→ ``mc_test_catalog(type=requirement)``；裸 UUID 无 FK，
      删目录时 service 显式置 NULL 归根）——需求的分组（可嵌套，同用例页目录树）。
      ``module`` 降为业务标签（模块归属），不再当分组用。
    - ``source_ref``：溯源（哪份 PRD/文档/导入文件）。
    - ``elements``：**九要素结构化**（26 号文 §2.1；用户口径：业务规则/背景/
      关联冲突需求/关联历史需求/使用用户与背景/约束 + 未知项）。``content`` 富文本
      仍是正文真相源；本列是**可按字段检索/引用**的结构化补充，两者并存不互斥。
      形状 ``{business_rules, background, related_conflicts, related_history,
      users, constraints, unknown_items:[{id,text,status}]}``——``unknown_items``
      里的**手工未知项**与 ``analysis.register`` 的 ``to_confirm`` 条目在页面上合并展示。
    - 用例/场景/接口/缺陷各自带 ``requirement_id`` 挂到本表（各表加列，非关联表）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)  # REQ-xxxx（项目内自增）
    project_id = fields.UUIDField()
    version_id = fields.UUIDField(null=True)  # → mc_test_project_version
    title = fields.CharField(max_length=512)
    description = fields.TextField(null=True)  # content 的纯文本投影（派生）
    content = fields.JSONField(default=dict)  # Tiptap/ProseMirror 文档（富文本正文）
    mindmap = fields.JSONField(default=dict)  # 思维导图树
    acceptance_criteria = fields.JSONField(default=list)  # [{id?, text}] 验收标准
    priority = fields.CharField(max_length=8, null=True)  # P0-P3
    module = fields.CharField(max_length=128, null=True)  # 业务标签（模块归属，非分组）
    # 归属目录（→ mc_test_catalog(type=requirement)）；裸 UUID，删目录时显式置 NULL。
    folder_id = fields.UUIDField(null=True)
    origin = fields.CharField(max_length=32, default="manual")  # manual|from_doc|from_import
    source_ref = fields.JSONField(default=dict)  # 溯源：{raw_id?, doc?, locator?}
    status = fields.CharField(max_length=32, default="draft")  # draft|active|deprecated|archived
    # ── 生成环节（25 号文 v4.1 §2.1）：需求分析/评审产物 + 该需求的流程进度 ──
    # analysis：环节1 九步产物（inventory/smells/criteria/register/invariants/dependencies）
    analysis = fields.JSONField(default=dict)
    # review：门⓪ 评审报告 + TSDR 决策记录
    review = fields.JSONField(default=dict)
    # gen_status：该需求在生成流程中的位置（state.GEN_STATUSES 12 态；需求节点徽章数据源）
    gen_status = fields.CharField(max_length=32, default="not_started")
    # 九要素结构化（26 号文 §2.1；2026-10-22 加列——_ensure_additive_columns 自动补）
    elements = fields.JSONField(default=dict)
    version = fields.IntField(default=1)
    created_by = fields.UUIDField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_requirement"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("version_id",), ("module",), ("folder_id",))


class TestRequirementAttachment(Model):
    """需求附件—— ``mc_test_requirement_attachment``。

    需求即文档：可挂 Word / 思维导图源文件 / 图片 / 任意文件。文件本体在 OSS
    （浏览器直传拿 URL），本表只存 URL 与元数据。``kind`` 区分来源：
    ``file|image|docx|xmind``（docx/xmind 是"导入为正文/思维导图"的原件留档）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    requirement_id = fields.UUIDField()
    project_id = fields.UUIDField()
    name = fields.CharField(max_length=512)
    url = fields.TextField()  # OSS 访问 URL
    mime_type = fields.CharField(max_length=128, null=True)
    size_bytes = fields.BigIntField(null=True)
    kind = fields.CharField(max_length=32, default="file")  # file|image|docx|xmind
    source_ref = fields.JSONField(default=dict)
    uploaded_by = fields.UUIDField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_requirement_attachment"
        indexes = (("requirement_id",), ("project_id",))


#: ``mc_test_requirement_attachment.kind`` 枚举
REQUIREMENT_ATTACHMENT_KINDS: tuple[str, ...] = ("file", "image", "docx", "xmind")


class TestTag(Model):
    """标签（§1）。任务按标签选例，不直接选用例。

    ``category`` 固定四类（purpose/priority/module/type）以支撑组合筛选，
    ``name`` 自由（smoke/regression/p0/payment/nightly…）。
    """

    __test__ = False  # 类名以 Test 开头：告知 pytest 这不是测试用例

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=64)
    project_id = fields.UUIDField()
    name = fields.CharField(max_length=128)
    category = fields.CharField(max_length=32)  # purpose|priority|module|type
    description = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_tag"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("category",))


class TestCase(Model):
    """用例——业务视角（§2）：**数据 + 场景引用 + 标签，不含步骤**。

    ``code`` 形如 ``TC-0001``，按 ``project_id`` 内 max+1 自增（service 层）。
    步骤属于场景（§2.1 关联表）；标签走 ``mc_test_case_tag``。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)
    project_id = fields.UUIDField()
    title = fields.CharField(max_length=512)
    intent = fields.TextField(null=True)  # 意图/描述：一句话测什么
    preconditions = fields.JSONField(default=list)  # 业务级前置条件（字符串列表，一行一条）
    expected = fields.JSONField(default=dict)  # 业务级预期结果（断言依据）
    #: 测试步骤（业务视角自然语言步骤说明，每项 {seq, action, expected}）。
    #: 与场景的可执行步骤分层：用例描述「怎么算过」，场景描述「怎么跑」。
    steps = fields.JSONField(default=list)
    dataset_id = fields.UUIDField(null=True)  # → mc_test_dataset
    confirm_source = fields.CharField(max_length=32)  # debug|doc|user_confirm（必填）
    confirm_ref = fields.JSONField(default=dict)  # question_id / 文档 / 调试记录
    origin = fields.CharField(max_length=32, default="manual")  # 来源（ai_generated|manual|from_history|from_requirement）
    priority = fields.CharField(max_length=8, null=True)  # P0-P3
    module = fields.CharField(max_length=128, null=True)  # 所属模块（§20.1 用例字段补充）
    feature = fields.CharField(max_length=128, null=True)  # 功能点
    version_id = fields.UUIDField(null=True)  # → mc_test_project_version（用例关联版本）
    requirement_id = fields.UUIDField(null=True)  # → mc_test_requirement（挂到需求，主脊）
    test_type = fields.CharField(max_length=32, null=True)  # 冒烟/功能/边界/异常/权限/安全/兼容
    #: 多值类型（2026-10-14 用户要「用例类型可多选」）：**新增列**，不动上面的旧单值
    #: ``test_type``——旧列继续由 ``case_scan``/``skeleton``/AI 路径写入，读侧 coalesce
    #: （``test_types or ([test_type] if test_type else [])``）。改列类型不会被
    #: additive 迁移生效，故用新列而非 retype（见 database.py:_ensure_additive_columns）。
    test_types = fields.JSONField(default=list)
    #: 归属目录（→ ``mc_test_catalog`` 且 ``type=case``）。**裸 UUID 无 FK 约束**
    #: （已存在表加列只得到 `UUID NULL`，与 TestCaseTag/TestCaseScenario 一致）；
    #: 删目录时由 catalog.delete service 显式把挂的用例 ``folder_id`` 置 NULL 归根。
    #: ``module`` 列保留向后兼容（选目录时同时写 module 全路径）。
    folder_id = fields.UUIDField(null=True)
    #: 机械枚举的幂等键（17 号文 §3.3）：``api:{api_id}:{param}:{axis}:{variant}``，
    #: 通用轴 ``auth:{api_id}:{variant}`` / ``idem:{api_id}:{variant}``。
    #: 非空时项目内唯一（部分唯一索引，见 design/skeleton.py 的 ensure_slot_index）——
    #: 重跑生成 = 冲突即跳过，冗余从存储层根治。人工用例保持 NULL（不受约束）。
    slot_key = fields.CharField(max_length=256, null=True)
    automation_status = fields.CharField(max_length=32, default="not_automated")
    status = fields.CharField(max_length=32, default="draft")
    # ── 生成环节（25 号文 v4.1 §2.1）：端归属 / 挂测试点 / 探索门状态 ──
    # surface：api|web|app|security（用例归属端；导图筛选与端配方回查用）
    surface = fields.CharField(max_length=32, null=True)
    # testpoint_id：挂回测试点（用例是测试点在某个端上的实现；裸 UUID 无 FK）
    testpoint_id = fields.UUIDField(null=True)
    # verified_by_run：探索门状态（未探索|已探索|探索失败）；未过门不进执行波次
    verified_by_run = fields.CharField(max_length=32, null=True)
    version = fields.IntField(default=1)
    created_by = fields.UUIDField()  # 设计者 = 责任人
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_case"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("status",), ("folder_id",))


class TestCaseTag(Model):
    """用例↔标签 多对多（§2.2）。任务「跑所有 smoke 用例」按此表选例。"""

    __test__ = False

    id = fields.UUIDField(pk=True)
    case_id = fields.UUIDField()
    tag_id = fields.UUIDField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_case_tag"
        unique_together = (("case_id", "tag_id"),)
        indexes = (("case_id",), ("tag_id",))


class TestCaseScenario(Model):
    """用例↔场景 多对多（§2.1）。

    一个用例可挂多个场景（正常流/异常流），一个场景可服务多个用例；
    ``is_primary`` 标主场景（默认执行）。``order`` 是关联的排列顺序。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    case_id = fields.UUIDField()
    scenario_id = fields.UUIDField()
    is_primary = fields.BooleanField(default=False)
    order = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_case_scenario"
        unique_together = (("case_id", "scenario_id"),)
        indexes = (("case_id",), ("scenario_id",))


class TestScenario(Model):
    """场景——实现视角（§3）：**步骤集合 + 局部变量管理**。

    ``local_vars`` 存局部变量定义与初值（§3.1，``{{local.xxx}}`` 跨步骤传递）；
    ``flow`` 存编排（条件/分支/提前结束/引用前置场景）。
    ``transport_id`` / ``env_id`` 指向尚未在本包建的表，先留空占位。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)
    project_id = fields.UUIDField()
    name = fields.CharField(max_length=512)
    purpose = fields.TextField(null=True)
    transport_id = fields.UUIDField(null=True)  # → mc_test_transport（先留）
    env_id = fields.UUIDField(null=True)  # → mc_test_environment（先留）
    version_id = fields.UUIDField(null=True)  # → mc_test_project_version
    requirement_id = fields.UUIDField(null=True)  # → mc_test_requirement（主脊）
    #: 归属目录 → mc_test_catalog(type=scenario)；裸 UUID 无 FK（同 case/requirement idiom），
    #: 删目录由 catalog.delete service 显式置 NULL 归根（场景本体不删）。
    folder_id = fields.UUIDField(null=True)
    local_vars = fields.JSONField(default=dict)  # 局部变量定义 + 初值
    flow = fields.JSONField(default=dict)  # 编排：conditions/early_exit/pre_scenarios
    exec_mode = fields.CharField(max_length=32, default="script_only")
    status = fields.CharField(max_length=32, default="draft")
    version = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_scenario"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("status",), ("folder_id",))


class TestScenarioStep(Model):
    """步骤——类型化（§4）。

    ``step_key`` 场景内唯一（被 ``flow`` 引用）；``seq`` 是执行顺序的唯一真相
    （引擎按其排序）；``type`` 决定 ``params`` 结构（§4.2）；``post_checks``
    必填 ≥1 由**校验器**强制（§4.4），模型层不重复该规则。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    # Python 属性是 ``scenario``（FK 关系对象），DB 列名经 ``source_field``
    # 锁定为 ``scenario_id``（§4 字段名）。``step.scenario_id`` 取裸 UUID，
    # ``step.scenario`` 取关联模型 —— 标准 Tortoise idiom。
    scenario = fields.ForeignKeyField(
        "testing_platform.TestScenario",
        related_name="steps",
        on_delete=fields.CASCADE,
        source_field="scenario_id",
    )
    step_key = fields.CharField(max_length=128)
    seq = fields.IntField()
    name = fields.CharField(max_length=512, null=True)
    description = fields.TextField(null=True)  # 人读：这步在干啥
    type = fields.CharField(max_length=32)  # request|page|android|ios|wait|script|check|sql|ws|custom
    params = fields.JSONField(default=dict)  # 结构随 type 变，支持 {{变量}}/method_ref
    pre_cond = fields.JSONField(default=list)  # 前置条件（断言列表）
    post_checks = fields.JSONField(default=list)  # 类型化后置校验
    post_cond = fields.JSONField(default=list)  # 后置赋值→写局部变量（{{local.x}} = ...，§3.1 流转）
    on_error = fields.CharField(max_length=64, null=True)  # retry|reinit|evict|fail|skip|abort
    enabled = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_scenario_step"
        unique_together = (("scenario_id", "step_key"),)
        indexes = (("scenario_id",),)


class TestChangeRecord(Model):
    """变更记录（design §15.12）——**变更必有因 + 唯一的版本机制**。

    用例/场景/方法/工具的版本与变更理由统一走本表；``field_changes`` 即
    字段级 diff，可重建任意时刻状态（design 十八「变更传导」）。``propagation``
    记录受影响的下游与同步状态；``rollback_ref`` 指向可回滚依据。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    entity_type = fields.CharField(max_length=32)  # case|scenario|method|custom_tool|knowledge_item|task
    entity_id = fields.UUIDField()
    created_by = fields.UUIDField(null=True)  # 变更人（操作者 UUID；历史留痕用）
    change_reason = fields.TextField()  # 必填（变更门）
    field_changes = fields.JSONField(default=dict)  # 字段级 diff
    propagation = fields.JSONField(default=dict)  # 受影响下游 + 同步状态
    rollback_ref = fields.JSONField(null=True)  # 可回滚依据
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_change_record"
        indexes = (("entity_type", "entity_id"),)


class TestTransport(Model):
    """传输层 / 接口管理（design §15.4）。

    存连接方式 + 鉴权 + 公共数据，被多个场景复用（``scenario.transport_id``）。
    ``auth`` 是 JSONB 登录流程 + ``re_login_on``（凭据只存引用，不存明文）；
    ``path_rewrite`` 是路径替换规则（``{"/api/v1": "{{env.api_prefix}}/v1"}``）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=128)
    project_id = fields.UUIDField()
    kind = fields.CharField(max_length=8, default="http")  # http|ws
    description = fields.CharField(max_length=512, null=True)  # 接口说明（导入时可为空，AI 可补全）
    module = fields.CharField(max_length=128, null=True)  # 分组维度（思维导图/树）
    requirement_id = fields.UUIDField(null=True)  # → mc_test_requirement（主脊）
    auth = fields.JSONField(default=dict)  # 登录流程 + re_login_on；凭据只存引用
    common_headers = fields.JSONField(default=dict)
    common_params = fields.JSONField(default=dict)
    path_rewrite = fields.JSONField(default=dict)
    status = fields.CharField(max_length=32, default="draft")
    version = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_transport"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("module",))


class TestApiFolder(Model):
    """接口目录树（mc_test_api_folder）——可嵌套分组（对齐 Apifox「目录」）。

    ``parent_id`` 自引用（null = 根目录）；``order`` 是同级排序。
    删除目录**级联删除子目录**，但接口（``mc_test_api.folder_id``）走
    SET NULL 归根——目录只是视图组织，删目录不动接口本体。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    # 自引用父：Python 属性是 ``parent``（FK 关系对象），DB 列名经
    # source_field 锁定为 ``parent_id`` —— 同 TestEnvironment.parent 的 idiom。
    parent = fields.ForeignKeyField(
        "testing_platform.TestApiFolder",
        related_name="children",
        null=True,
        on_delete=fields.CASCADE,
        source_field="parent_id",
    )
    name = fields.CharField(max_length=128)
    order = fields.IntField(default=0)  # 同级排序（拖拽整理）
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_api_folder"
        indexes = (("project_id",), ("parent_id",))


class TestCatalog(Model):
    """统一目录树（``mc_test_catalog``）——需求/用例/场景/接口**共用一张表**，
    靠 ``type``（:data:`CATALOG_TYPES`）区分各自的目录树；四类之间**没有关系**，
    只是恰好同存（用户定版，2026-10-14）。

    照 :class:`TestApiFolder` 的 idiom：``parent_id`` 自引用（null=根）、
    ``order`` 同级排序；删目录**级联删子目录**（``on_delete=CASCADE``）。挂在目录下
    的实体（如 ``mc_test_case.folder_id``）**不设 FK 约束**（裸 UUID），删目录时由
    service 显式把实体的 ``folder_id`` 置 NULL 归根（目录只是视图组织，不动实体本体）。

    与既有 :class:`TestApiFolder`（接口页生产中）并存：本轮只把**用例**迁到本表
    （``type=case``）；接口/需求/场景后续各自迁到对应 ``type`` 分支。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    type = fields.CharField(max_length=32)  # requirement|case|scenario|api
    # 自引用父：Python 属性是 ``parent``（FK 关系对象），DB 列名经 source_field
    # 锁定为 ``parent_id``——同 TestApiFolder / TestEnvironment.parent 的 idiom。
    parent = fields.ForeignKeyField(
        "testing_platform.TestCatalog",
        related_name="children",
        null=True,
        on_delete=fields.CASCADE,
        source_field="parent_id",
    )
    name = fields.CharField(max_length=128)
    order = fields.IntField(default=0)  # 同级排序（拖拽整理留后续）
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_catalog"
        indexes = (("project_id", "type"), ("parent_id",))


class TestApi(Model):
    """接口（mc_test_api）——Apifox 式一等实体：**一份完整接口文档**。

    与 ``mc_test_transport`` 的分工（用户定版，2026-10-13）：

    * **接口 = 文档实体**：method/path/params/headers/body/responses 全是
      **结构说明**（schema：有哪些字段、类型、必填、说明、示例），**不是具体值**
      ——具体数据属用例/场景的输入。
    * **连接配置**（base_url/鉴权/公共头/路径替换）= 归**环境**管，接口不存。

    **身份是 ``id``（UUID），不是 ``code``**——code（API-0001）只是可读句柄，
    不做唯一键（版本关联后同一逻辑接口可跨版本出现多条）。所有引用
    （场景/用例）一律指向 ``id``。

    ``version_id`` 指向发布版本（与生产环境一致）；当前**不自动关联**
    （哪个是线上版本未定），先落列，后续跟踪。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    code = fields.CharField(max_length=64, null=True)  # API-0001（可读句柄，非唯一标识）
    # Python 属性是 ``folder``（FK 关系对象），DB 列名经 source_field 锁定为 folder_id。
    # 删目录 → 接口归根（SET NULL），不级联删接口。
    folder = fields.ForeignKeyField(
        "testing_platform.TestApiFolder",
        related_name="apis",
        null=True,
        on_delete=fields.SET_NULL,
        source_field="folder_id",
    )
    name = fields.CharField(max_length=256)
    method = fields.CharField(max_length=8)  # GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS
    path = fields.CharField(max_length=512)  # /api/v1/orders/{id}
    protocol = fields.CharField(max_length=8, default="http")  # http|ws|grpc
    status = fields.CharField(max_length=32, default="designing")  # API_STATUSES
    description = fields.TextField(null=True)  # 说明/备注（同一字段，不分两栏）
    tags = fields.JSONField(default=list)
    owner_id = fields.UUIDField(null=True)  # 责任人/维护人
    priority = fields.CharField(max_length=8, null=True)  # P0-P3
    order = fields.IntField(default=0)  # 目录内排序
    # ↓ 结构说明（非具体值）：字段名/类型/必填/说明/示例/default
    params = fields.JSONField(default=list)  # [{name,in(query|path),type,required,...}]
    headers = fields.JSONField(default=list)  # [{name,type,required,...}]
    cookies = fields.JSONField(default=list)  # [{name,value,description,required}]（cookie 独立于 params）
    body_type = fields.CharField(max_length=32, default="none")  # API_BODY_TYPES
    request_body = fields.JSONField(default=dict)  # schema 树 + 示例
    responses = fields.JSONField(default=list)  # [{status_code,name,content_type,schema,example}]
    requirement_id = fields.UUIDField(null=True)  # → mc_test_requirement（主脊）
    version_id = fields.UUIDField(null=True)  # → 发布版本（暂不自动关联，后续跟踪）
    source = fields.CharField(max_length=32, default="manual")  # API_SOURCES
    source_ref = fields.JSONField(default=dict)  # {spec?, operationId?, file?, line?}
    version = fields.IntField(default=1)  # 行版本（变更门 +1，走 change_record）
    created_by = fields.UUIDField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_by = fields.UUIDField(null=True)  # 最后更新人
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_api"
        # 身份 = id；code 不做唯一键（版本关联后同一接口可跨版本多条）
        indexes = (
            ("project_id",),
            ("project_id", "method"),
            ("project_id", "status"),
            ("version_id",),
        )


class TestApiCase(Model):
    """接口↔用例 多对多（P5 直连）——一个接口可被多个用例覆盖，一个用例可引用多个接口。

    裸 UUID 对（无 FK），同 ``mc_test_case_scenario`` 惯例；``order`` 是关联排列。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    api_id = fields.UUIDField()
    case_id = fields.UUIDField()
    order = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_api_case"
        unique_together = (("api_id", "case_id"),)
        indexes = (("api_id",), ("case_id",))


class TestApiScenario(Model):
    """接口↔场景 多对多（P5 直连）——场景步骤通过 ``params.api_id`` 引用接口，
    本表存显式关联（供接口详情反查「哪些场景在用我」与影响面）。"""

    __test__ = False

    id = fields.UUIDField(pk=True)
    api_id = fields.UUIDField()
    scenario_id = fields.UUIDField()
    order = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_api_scenario"
        unique_together = (("api_id", "scenario_id"),)
        indexes = (("api_id",), ("scenario_id",))


class TestApiImportJob(Model):
    """接口导入任务（可配置的定时导入）。

    一条任务 = 「从哪导（``spec_url`` 或 ``spec_text``）+ 按什么格式
    （``source_format``，含 apifox）+ 什么节奏（``schedule``：复用任务域
    ``next_fire`` 的 cron/interval/once；manual 只手动跑）。

    * 定时只能走 ``spec_url``（``spec_text`` 是浏览器里粘贴的，服务端无会话）。
    * ``last_*`` 记最近一次运行（状态/消息/结果快照），供列表显示。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    name = fields.CharField(max_length=128)
    source_format = fields.CharField(max_length=16, default="auto")  # API_SOURCE_FORMATS
    spec_url = fields.TextField(null=True)
    spec_text = fields.TextField(null=True)
    schedule = JSONValueField(default=dict)
    enabled = fields.BooleanField(default=True)
    last_run_at = fields.DatetimeField(null=True)
    next_run_at = fields.DatetimeField(null=True)
    last_status = fields.CharField(max_length=16, null=True)  # ok|error|running
    last_message = fields.TextField(null=True)
    last_result = JSONValueField(default=dict)
    created_by = fields.UUIDField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_api_import_job"
        indexes = (("project_id",), ("enabled", "next_run_at"))


class TestApiSource(Model):
    """接口的**来源**（一个接口可来自多个导入方式）。

    每次导入命中一条接口，就登记/刷新一条来源（``api_id`` × ``job_id``）：
    记住它是从哪个导入任务、按什么方式、哪份 URL、什么 method/path 导进来的，
    以及上次「同步」的时间与结果。详情页据此展示来源列表并提供**同步**按钮
    （重抓该来源的文档，把这条接口刷回来源态）。

    ``job_id`` 唯一约束（同一任务对同一接口只留一条来源）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    api_id = fields.UUIDField()
    project_id = fields.UUIDField()
    job_id = fields.UUIDField(null=True)
    source_format = fields.CharField(max_length=16)  # API_SOURCE_FORMATS（不含 auto）
    spec_url = fields.TextField(null=True)
    method = fields.CharField(max_length=8)
    path = fields.CharField(max_length=512)
    last_synced_at = fields.DatetimeField(null=True)
    last_status = fields.CharField(max_length=16, null=True)  # ok|error
    last_message = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_api_source"
        indexes = (("api_id",), ("job_id",), ("project_id",))


class TestMethodModule(Model):
    """方法脚本「类」（§5.1 重构：**一个脚本 = 一个类**）——Python 代码的唯一居所。

    ``code`` 即源码里的**类名**（保存时由 AST 解析同步，同作用域内唯一），
    ``name``/``description`` 分别是类名与类 docstring（保存时自动同步，类说明
    用 Python 注释方式编写）。``project_id=null`` 全局内置类，有值绑项目——
    ``kind``（base/project）已删，列表平铺不再分类。``source`` 存 Python 源码
    （design §15.5 / 01 §8 决策 2：DB text，不落对象存储）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=128)  # = 源码里的类名（如 Crypto），保存时 AST 同步
    project_id = fields.UUIDField(null=True)  # null = 全局内置类；有值 = 项目类
    name = fields.CharField(max_length=256)  # 类名（保存时与 code 一并同步）
    description = fields.TextField(null=True)  # 类备注（= 类 docstring，保存时同步）
    source = fields.TextField(default="")  # Python 源码
    async_native = fields.BooleanField(default=False)  # 是否异步原生
    status = fields.CharField(max_length=32, default="draft")
    version = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_method_module"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",),)


class TestMethod(Model):
    """方法注册表（§5.1）——模块内方法的签名登记。

    ``full_ref`` 形如 ``Crypto.sign``（``<类名>.<方法名>``，被步骤 ``method_ref``
    引用）；``signature`` 是**入参说明**（JSONB：``{params:[{name,type,default,
    description}], returns:{...}}``，保存时 AST 解析同步，供校验器/白名单/前端
    检索展示）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    # Python 属性是 ``module``（FK 关系对象），DB 列名经 ``source_field``
    # 锁定为 ``module_id``（§5.1 字段名）——同 TestScenarioStep.scenario 的 idiom。
    module = fields.ForeignKeyField(
        "testing_platform.TestMethodModule",
        related_name="methods",
        on_delete=fields.CASCADE,
        source_field="module_id",
    )
    name = fields.CharField(max_length=128)  # 方法名，如 sign
    full_ref = fields.CharField(max_length=256)  # Crypto.sign
    signature = fields.JSONField(default=dict)  # 入参说明：{params:[...], returns:{...}}
    is_async = fields.BooleanField(default=False)  # 同步/异步
    description = fields.TextField(null=True)  # 方法说明（= 方法 docstring）
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_method"
        unique_together = (("module_id", "name"),)
        indexes = (("module_id",), ("full_ref",))


class TestDataset(Model):
    """数据集（§6 / design §15.4）——参数化数据，一个场景可跑多组。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=128)
    project_id = fields.UUIDField()
    # ``schema`` 是保留关键字，属性用 ``schema`` 会踩 builtin 提示但合法；
    # 为可读性用 source_field 锁定列名，属性名取 ``schema`` 直接对应 design 字段。
    schema = fields.JSONField(default=dict)  # product_id: string ...
    rows = fields.JSONField(default=list)  # [{...}, {...}]
    version = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_dataset"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",),)


class TestEnvironment(Model):
    """测试环境（design §15.4 / 05 §2.1）——**分层继承**，不是 TaskEnvironment。

    ``parent_id`` 自引用：**基线(base) → 环境组 → 具体环境**（05 §3）。
    ``null`` 表示根基线（无父）。子环境只配差异，公共变量在父级，
    读取时**递归向上合并**（子覆盖父）——合并算法在
    :func:`testing_platform.assets.service.resolve_environment_vars`。
    **防循环继承**由 service 写入时校验（父链不得出现自身）。

    ``is_default``：项目内默认环境（切换默认时 service 会清掉同项目其它默认）。
    ``status`` = active/archived（05 §6：归档不删，标 archived）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=128)  # dev / staging / prod_sim
    name = fields.CharField(max_length=256)
    project_id = fields.UUIDField()
    # 自引用父：Python 属性是 ``parent``（FK 关系对象），DB 列名经
    # source_field 锁定为 ``parent_id``（05 §2.1 字段名）。null = 根基线。
    parent = fields.ForeignKeyField(
        "testing_platform.TestEnvironment",
        related_name="children",
        null=True,
        on_delete=fields.SET_NULL,
        source_field="parent_id",
    )
    description = fields.TextField(null=True)
    is_default = fields.BooleanField(default=False)
    status = fields.CharField(max_length=32, default="active")  # active|archived
    version = fields.IntField(default=1)
    created_by = fields.UUIDField(null=True)  # 人为管理（05 §2.1）
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_environment"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("parent_id",))


class TestEnvVariable(Model):
    """环境变量（design §15.4 / 05 §2.2）——类型化 + 密钥脱敏。

    ``value`` 存 JSONB 原生值（int 就是 int），``type`` 是七类型声明
    （05 §4「保持原生类型」——``port > 8000`` 这类判断不能错）。
    ``is_secret=true`` → 读取接口脱敏（``***`` / 末 4 位）、执行时服务端注入真值、
    日志/报告/trace 一律脱敏（05 §5）。

    ``purpose``：**用途说明**（可选，自由文本）。同一环境里常有 ``base_url`` /
    ``timeout`` 这类同形变量，说明写「指向哪套 SUT / 单位是什么」省得猜；
    页面表格与变量弹框都可编辑（``create_tables_via_pg`` 幂等补列，老库有数据）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    env = fields.ForeignKeyField(
        "testing_platform.TestEnvironment",
        related_name="variables",
        on_delete=fields.CASCADE,
        source_field="env_id",
    )
    key = fields.CharField(max_length=256)
    value = JSONValueField(null=True)  # 原生值（string/int/float/bool/json/list/dict）
    type = fields.CharField(max_length=16, default="string")  # ENV_VARIABLE_TYPES 六类型（dict 已并入 json）
    purpose = fields.TextField(null=True)  # 用途说明（可选，页面/弹框可编辑）
    is_secret = fields.BooleanField(default=False)
    version = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_env_variable"
        unique_together = (("env_id", "key"),)
        indexes = (("env_id",),)


class TestCustomTool(Model):
    """自定义工具（design §15.5）——**只拥有签名 + 指向，不拥有实现**。

    ``impl_ref`` 只能指向公共场景（``mc_test_scenario``）或方法
    （``mc_test_method.full_ref``），杜绝两处实现。
    ``status`` = draft/review/active/deprecated（评审流）。
    版本与变更理由统一走 ``mc_test_change_record`` + 本表 ``version``；
    每次升级同时落 :class:`TestCustomToolVersion` 快照（design §15.5）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=128)
    project_id = fields.UUIDField()
    scope = fields.CharField(max_length=16, default="project")  # project|team
    purpose = fields.TextField(null=True)  # 业务语义（这个工具解决什么问题）
    signature = fields.JSONField(default=dict)  # 输入输出契约（JSONB）
    impl_ref = fields.JSONField(default=dict)  # → scenario id 或 method.full_ref
    status = fields.CharField(max_length=32, default="draft")  # draft|review|active|deprecated
    version = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_custom_tool"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("scope",))


class TestCustomToolVersion(Model):
    """自定义工具版本快照（design §15.5：``id, tool_id, version, snapshot, change_reason``）。

    每次工具升级落一行 —— ``snapshot`` 是升级后的完整工具文档，
    ``change_reason`` 即变更门理由，可据此回溯任意时刻的工具形态。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    tool = fields.ForeignKeyField(
        "testing_platform.TestCustomTool",
        related_name="versions",
        on_delete=fields.CASCADE,
        source_field="tool_id",
    )
    version = fields.IntField()
    snapshot = fields.JSONField(default=dict)
    change_reason = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_custom_tool_version"
        unique_together = (("tool_id", "version"),)
        indexes = (("tool_id",),)


async def create_tables_via_pg() -> None:
    """Create ``mc_test_*`` tables through the platform raw PG client.

    Used by deployments where Tortoise is not initialized (main service path).
    Matches the platform convention ``from db import PostgresClient``.
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
        # §5.1 重构兜底：老库的 mc_test_method_module.kind（base/project 分类）已删——
        # CREATE TABLE IF NOT EXISTS 不会改已存在的表，老表留着 NOT NULL 的 kind 会让
        # 不带该列的 INSERT 直接失败。幂等 DROP（存量行保留；老 base 行 project_id
        # 本就为 NULL，与新模型「null=全局」语义一致，无需数据迁移）。
        await conn.execute("ALTER TABLE mc_test_method_module DROP COLUMN IF EXISTS kind")
        # 场景目录（mc_test_catalog type=scenario）：同理由幂等补列（CREATE TABLE IF NOT
        # EXISTS 不给老库加列），老场景归「未分组」由 service 侧按 NULL 语义处理。
        await conn.execute(
            "ALTER TABLE mc_test_scenario ADD COLUMN IF NOT EXISTS folder_id UUID"
        )
        # 环境变量用途说明（mc_test_env_variable.purpose）：同上，老库有数据不能重建表。
        # nullable 且默认 NULL，补列对存量行零影响（用途说明本就允许为空）。
        await conn.execute(
            "ALTER TABLE mc_test_env_variable ADD COLUMN IF NOT EXISTS purpose TEXT"
        )
        # json/dict 合并（六类型定版）：两者在 _coerce_env_value 本就同一分支（都要求
        # 对象），存量 dict 行归一到 json，避免 UI 类型下拉（已无 dict）选中空值。
        await conn.execute(
            "UPDATE mc_test_env_variable SET type = 'json' WHERE type = 'dict'"
        )
