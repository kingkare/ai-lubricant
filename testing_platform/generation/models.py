"""测试平台 **生成环节** 数据模型（25 号文 v4.1 第二部分，唯一事实源）。

======  ==========================================================
表      说明
======  ==========================================================
``mc_test_gen_run``       生成任务（全文所有 "run" 指它；§2.0）
``mc_test_testpoint``     测试点（中枢，挂需求不挂模块；§2.2）
``mc_test_proposal``      暂存区（**AI 唯一写入口**；§2.3）
``mc_test_coverage_cell`` 覆盖矩阵格（轴×端；§2.5）
``mc_test_rule``          用户规则（机制见 §7；与 :mod:`.rules` 纯函数层同表）
======  ==========================================================

**问题队列不新建**（§2.4 终审#1：``mc_test_question`` 现库已上线）——本模块只
提供 :func:`ensure_question_columns` 给存量表补生成流程缺的六列。

铁律（§1.3）：AI 产物先落 ``mc_test_proposal``，人确认（门②）后才进正式表；
门②之前 ``mc_test_case`` 零写入。故本模块**不 import** ``assets``/``quality``
的任何模型，跨表引用一律留**裸 UUID 列**（同 ``assets.models`` 里
``dataset_id``/``transport_id`` 的「先留」惯例）——既不建跨包 FK，也不与
并发会话的 models 改动相互牵制。

风格照 ``testing_platform/assets/models.py``：UUID 主键 + ``mc_test_`` 前缀 +
``JSONField`` + ``Meta.app = "testing_platform"`` + 模块级 ``DDL``；**模型层不做
枚举校验**（枚举以模块级 tuple 常量给出，``_validate`` 由调用方/service 负责）。

``DDL`` 是 **Postgres 方言**（JSONB/TIMESTAMPTZ/UUID），供不走 Tortoise 的
``PostgresClient.pool`` 部署路径与整合步骤使用；Tortoise 侧由 ``generate_schemas``
按模型建表（sqlite/PG 通吃）。两者列集合必须一致——
``tests/test_generation_models.py`` 逐表比对防漂移。
"""
from __future__ import annotations

from typing import Any

from tortoise import fields
from tortoise.models import Model


# ==========================================================================
# 枚举常量（25 号文原文取值；模型层不校验，校验归调用方）
# ==========================================================================

#: ``mc_test_gen_run.run_type``（§2.0 六枚举）。各 run_type 的状态起止见 §3.1 表。
GEN_RUN_TYPES: tuple[str, ...] = (
    "full",              # 设计+用例（pending → done 全链）
    "plan",              # 只到门⓪①，产设计（pending → points_proposed 关闭）
    "exec",              # 从已确认测试点起（generating → done）
    "pre_submit_edit",   # 提测前改草案（generating → cases_proposed，不重跑设计）
    "incremental",       # 提测后增量（analyzing 只增量范围 → done）
    "reopen",            # 门②后 REJECT 派生（parent_run_id=原 run，只重跑该测试点）
)

#: ``mc_test_gen_run.status``（§3.1 状态机，10 态；回边 evaluating→generating）
#: ``mc_test_gen_run.status`` 取值域（§3.1）——主链 10 态 + 旁路 3 态。
#: 旁路三态由 state.py 的 RUN_BYPASS_STATUSES 定义：paused(暂停可恢复)/failed(收口)/
#: cancelled(人取消终态)。**两处必须一致**：本模块是 DB 层取值域（VARCHAR(32) 无 CHECK，
#: 单列自由文本），state.py 是转换合法性（can_transition 只认这里的 13 个值）。
#: 整合约定（2026-10-18）：models.GEN_RUN_STATUSES == state.GEN_RUN_STATUSES（含旁路），
#: 调用方做 status 合法性校验时用 models 版；转换校验用 state 版。两边由
#: tests/test_generation_models.py 的 enum parity 测试钉死。
GEN_RUN_STATUSES: tuple[str, ...] = (
    # 主链（§3.1）
    "pending",
    "analyzing",
    "req_reviewing",      # 内含门⓪
    "points_proposed",    # 内含门①
    "generating",
    "evaluating",
    "cases_proposed",     # 内含门②
    "cases_confirmed",
    "importing",
    "done",
    # 旁路（state.py RUN_BYPASS_STATUSES：不进 gen_status 映射，只进 run 状态）
    "paused",             # 人暂停；记 resume_from，恢复回原态（非终态）
    "failed",             # 异常/预算 Blocking 收口/熔断；终态
    "cancelled",          # 人为中止；终态
)

#: 门态（进入即走通知协议叫人开门，§6.2；实际枚举名，终审#6 修正）
GEN_RUN_GATE_STATUSES: tuple[str, ...] = ("req_reviewing", "points_proposed", "cases_proposed")

#: ``mc_test_gen_run.budget.state``（§2.0 原文三取值；第五部分徽章表写「四档」——
#: 差异见交付说明，未擅自补第四态）
GEN_RUN_BUDGET_STATES: tuple[str, ...] = ("normal", "pressure", "blocking")

#: ``mc_test_testpoint.surfaces`` 数组取值（§2.2）
TESTPOINT_SURFACES: tuple[str, ...] = ("api", "web", "app", "security")

#: ``mc_test_testpoint.surfaces_decided_by``（§2.2：门①人改端记 user，徽章数据源）
TESTPOINT_SURFACES_DECIDED_BY: tuple[str, ...] = ("ai", "user")

#: ``mc_test_testpoint.design_method``——25 §2.2 原文清单「等价类/边界值/判定表/
#: 状态迁移/场景法/错误推测/正交」。本表存**英文 slug**（全库枚举字面量惯例，见
#: ``assets.models.CASE_TEST_TYPES`` 等），中文对照见 :data:`TESTPOINT_DESIGN_METHOD_LABELS`。
#: ※ 17/18 号文同列还多一个「因果图」，25 号文（唯一事实源）没有 → 未收录。
TESTPOINT_DESIGN_METHODS: tuple[str, ...] = (
    "equivalence_class",   # 等价类
    "boundary_value",      # 边界值
    "decision_table",      # 判定表
    "state_transition",    # 状态迁移
    "scenario",            # 场景法
    "error_guessing",      # 错误推测
    "orthogonal",          # 正交
)

#: :data:`TESTPOINT_DESIGN_METHODS` 的中文对照（文档原文措辞，供 UI/提示词渲染）
TESTPOINT_DESIGN_METHOD_LABELS: dict[str, str] = {
    "equivalence_class": "等价类",
    "boundary_value": "边界值",
    "decision_table": "判定表",
    "state_transition": "状态迁移",
    "scenario": "场景法",
    "error_guessing": "错误推测",
    "orthogonal": "正交",
}

#: ``mc_test_testpoint.batch_tier``（§2.2：A 冒烟 / B 重点 / C 扩展）
TESTPOINT_BATCH_TIERS: tuple[str, ...] = ("A", "B", "C")

#: ``mc_test_testpoint.basis``（§2.2：事实/推断/待确认——依据三色徽章 绿/黄/红）
TESTPOINT_BASES: tuple[str, ...] = ("fact", "assumption", "to_confirm")

#: ``mc_test_testpoint.status``（§2.2：门①确认后才建行，初始即 confirmed，
#: **不存在 candidate**；generated = surfaces 每一端都有 confirmed 用例，人改端后自动重算）
TESTPOINT_STATUSES: tuple[str, ...] = ("confirmed", "generated")

#: ``mc_test_testpoint.case_review_state`` 聚合态（§2.2 v4.1 补终审#7：子 case
#: proposal 评审态聚合，TP 节点「用例评审中」徽章的数据源）
TESTPOINT_CASE_REVIEW_STATES: tuple[str, ...] = (
    "machine_pending", "in_duel", "split", "scored", "need_rework", "none",
)

#: ``mc_test_proposal.kind``（§2.3）
PROPOSAL_KINDS: tuple[str, ...] = ("testpoint", "case", "dataset")

#: ``mc_test_proposal.evidence_class``（§2.3；机器门只认 executed/static 可判通过）
PROPOSAL_EVIDENCE_CLASSES: tuple[str, ...] = ("executed", "static", "inferred", "claimed")

#: ``mc_test_proposal.review_state``（§2.3 原文五态）。
#: ※ §3.3 cases 聚合与第五部分徽章还出现 ``split``（双评分歧），但 2.3 的
#: review_state 清单没列它（共识值在 ``reviews.consensus`` 里）——按 2.3 原文实现，
#: 差异见交付说明。列宽留了余量，日后补态不必改表。
PROPOSAL_REVIEW_STATES: tuple[str, ...] = (
    "machine_pending", "machine_rejected", "in_duel", "scored", "need_rework",
)

#: ``mc_test_proposal.status``（§2.3：proposed → keep|modify|reject|defer，门②四判决）
PROPOSAL_STATUSES: tuple[str, ...] = ("proposed", "keep", "modify", "reject", "defer")

#: ``mc_test_proposal.applied_rules[].disposition``（§2.3 回执契约：未声明=违规）
PROPOSAL_RULE_DISPOSITIONS: tuple[str, ...] = ("followed", "not_applicable", "violated")

#: ``mc_test_proposal.reviews.consensus``（§2.3/环节6：弃权=inconclusive 进人，
#: 分歧=split 带两份意见进门②，不设仲裁模型）
PROPOSAL_CONSENSUS: tuple[str, ...] = ("agree", "split", "inconclusive")

#: ``mc_test_coverage_cell.state``（§2.5：亮/灰/斜纹三态；na 必填验证依据）
COVERAGE_CELL_STATES: tuple[str, ...] = ("lit", "gap", "na")

#: ``mc_test_rule.kind``（§2.6；hard 校验期代码判，soft 评审期提示/打分）
RULE_KINDS: tuple[str, ...] = ("hard", "soft")

#: ``mc_test_rule.scope``（§2.6 作用域层）
RULE_SCOPES: tuple[str, ...] = ("global", "module", "requirement", "surface")

#: ``mc_test_rule.enforcement``（§2.6：新规则默认 shadow，跑一轮看违反率再 active）
RULE_ENFORCEMENTS: tuple[str, ...] = ("active", "shadow", "disabled")

#: 新规则的默认 enforcement（与 :mod:`testing_platform.generation.rules` 的
#: ``DEFAULT_ENFORCEMENT`` 同源；此处独立定义保持 models 自包含、零跨模块依赖）
RULE_DEFAULT_ENFORCEMENT = "shadow"

#: ``mc_test_question.gate``（§2.4 扩列：问题出自哪道门）
QUESTION_GATES: tuple[str, ...] = ("req_review", "point_confirm")

#: ``mc_test_question.resolution`` 扩展取值（§2.4：**DB 是 VARCHAR(32) 无 CHECK
#: 约束**，故不加列不改类型——这里只是给调用方的字面量清单）。基础三值
#: debug/doc/user_confirm 见 ``quality.models.QUESTION_RESOLUTIONS``（本模块不 import，
#: 避免与并发会话耦合）；``risk_accepted`` 是门⓪死锁出口（超预算降级：按推荐答案
#: 假设走、测试点标 to_confirm，resolve 时清 blocking_refs 沿用现语义）。
QUESTION_RESOLUTIONS_GENERATION: tuple[str, ...] = (
    "debug", "doc", "user_confirm", "risk_accepted",
)


# ==========================================================================
# Postgres 方言 DDL（新建五表 + 索引）。顺序：表在前、索引在后；IF NOT EXISTS 幂等。
# 与下面模型逐列对齐（tests/test_generation_models.py 做集合比对防漂移）。
# 注：``"trigger"`` 加引号——trigger 是 SQL 关键字，同库 "order" 的既有写法。
# ==========================================================================
DDL: tuple[str, ...] = (
    # ── §2.0 生成任务 mc_test_gen_run ─────────────────────────
    """
    CREATE TABLE IF NOT EXISTS mc_test_gen_run (
        id            UUID PRIMARY KEY,
        project_id    UUID         NOT NULL,
        version_id    UUID,
        run_type      VARCHAR(32)  NOT NULL,
        parent_run_id UUID,
        scope         JSONB        NOT NULL DEFAULT '{}'::jsonb,
        status        VARCHAR(32)  NOT NULL DEFAULT 'pending',
        progress      JSONB        NOT NULL DEFAULT '{}'::jsonb,
        budget        JSONB        NOT NULL DEFAULT '{}'::jsonb,
        decisions     JSONB        NOT NULL DEFAULT '[]'::jsonb,
        config        JSONB        NOT NULL DEFAULT '{}'::jsonb,
        created_by    UUID,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_gen_run_project  ON mc_test_gen_run(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_gen_run_status   ON mc_test_gen_run(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_test_gen_run_version  ON mc_test_gen_run(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_gen_run_parent   ON mc_test_gen_run(parent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_gen_run_type     ON mc_test_gen_run(project_id, run_type)",
    # ── §2.2 测试点 mc_test_testpoint ─────────────────────────
    """
    CREATE TABLE IF NOT EXISTS mc_test_testpoint (
        id                  UUID PRIMARY KEY,
        code                VARCHAR(32)  NOT NULL,
        project_id          UUID         NOT NULL,
        requirement_id      UUID,
        title               VARCHAR(512) NOT NULL,
        "trigger"           TEXT,
        sample_data         TEXT,
        verify_result       TEXT,
        impact              TEXT,
        evidence            TEXT,
        coverage_axis       JSONB        NOT NULL DEFAULT '[]'::jsonb,
        surfaces            JSONB        NOT NULL DEFAULT '[]'::jsonb,
        surfaces_decided_by VARCHAR(16)  NOT NULL DEFAULT 'ai',
        design_method       VARCHAR(32),
        batch_tier          VARCHAR(8),
        basis               VARCHAR(16)  NOT NULL DEFAULT 'assumption',
        status              VARCHAR(32)  NOT NULL DEFAULT 'confirmed',
        case_review_state   VARCHAR(32)  NOT NULL DEFAULT 'none',
        created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_testpoint_project     ON mc_test_testpoint(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_testpoint_requirement ON mc_test_testpoint(requirement_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_testpoint_status      ON mc_test_testpoint(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_test_testpoint_review_state ON mc_test_testpoint(case_review_state)",
    # ── §2.3 暂存区 mc_test_proposal ──────────────────────────
    """
    CREATE TABLE IF NOT EXISTS mc_test_proposal (
        id              UUID PRIMARY KEY,
        run_id          UUID           NOT NULL,
        project_id      UUID           NOT NULL,
        kind            VARCHAR(16)    NOT NULL,
        payload         JSONB          NOT NULL DEFAULT '{}'::jsonb,
        source_agent    VARCHAR(64),
        surface         VARCHAR(16),
        testpoint_id    UUID,
        evidence_class  VARCHAR(16),
        review_state    VARCHAR(32)    NOT NULL DEFAULT 'machine_pending',
        review_round    INTEGER        NOT NULL DEFAULT 0,
        rewrite_of      UUID,
        version         INTEGER        NOT NULL DEFAULT 1,
        score           DOUBLE PRECISION,
        score_breakdown JSONB          NOT NULL DEFAULT '{}'::jsonb,
        reviews         JSONB          NOT NULL DEFAULT '{}'::jsonb,
        dup_of_case_id  UUID,
        dup_score       DOUBLE PRECISION,
        -- 处置三态文档只给中文（并入/另立/维持），未给字面量 → 自由 VARCHAR，不建枚举常量
        dup_verdict     VARCHAR(32),
        applied_rules   JSONB          NOT NULL DEFAULT '[]'::jsonb,
        question_id     UUID,
        slot_key        VARCHAR(256),
        status          VARCHAR(16)    NOT NULL DEFAULT 'proposed',
        provenance      JSONB          NOT NULL DEFAULT '{}'::jsonb,
        created_at      TIMESTAMPTZ    NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ    NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_run       ON mc_test_proposal(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_project   ON mc_test_proposal(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_run_kind  ON mc_test_proposal(run_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_testpoint ON mc_test_proposal(testpoint_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_status    ON mc_test_proposal(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_review    ON mc_test_proposal(review_state)",
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_slot      ON mc_test_proposal(project_id, slot_key)",
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_question  ON mc_test_proposal(question_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_proposal_rewrite   ON mc_test_proposal(rewrite_of)",
    # ── §2.5 覆盖格 mc_test_coverage_cell ─────────────────────
    """
    CREATE TABLE IF NOT EXISTS mc_test_coverage_cell (
        id             UUID PRIMARY KEY,
        project_id     UUID         NOT NULL,
        requirement_id UUID         NOT NULL,
        axis           VARCHAR(8)   NOT NULL,
        surface        VARCHAR(16)  NOT NULL,
        state          VARCHAR(16)  NOT NULL DEFAULT 'gap',
        evidence       TEXT,
        testpoint_id   UUID,
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (requirement_id, axis, surface)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_cov_cell_project     ON mc_test_coverage_cell(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_cov_cell_requirement ON mc_test_coverage_cell(requirement_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_cov_cell_state       ON mc_test_coverage_cell(project_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_test_cov_cell_testpoint   ON mc_test_coverage_cell(testpoint_id)",
    # ── §2.6 用户规则 mc_test_rule ────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS mc_test_rule (
        id           UUID PRIMARY KEY,
        project_id   UUID         NOT NULL,
        content      TEXT         NOT NULL,
        kind         VARCHAR(16)  NOT NULL,
        scope        VARCHAR(16)  NOT NULL,
        target_type  VARCHAR(32),
        target_id    VARCHAR(128),
        enforcement  VARCHAR(16)  NOT NULL DEFAULT 'shadow',
        hits         INTEGER      NOT NULL DEFAULT 0,
        violations   INTEGER      NOT NULL DEFAULT 0,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_rule_project     ON mc_test_rule(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_rule_scope       ON mc_test_rule(project_id, scope)",
    "CREATE INDEX IF NOT EXISTS idx_test_rule_enforcement ON mc_test_rule(enforcement)",
    "CREATE INDEX IF NOT EXISTS idx_test_rule_target      ON mc_test_rule(target_type, target_id)",
)


# ==========================================================================
# 模型
# ==========================================================================
class TestGenRun(Model):
    """生成任务 —— ``mc_test_gen_run``（§2.0，全文所有 "run" 指这张表）。

    与现库执行用的 ``mc_test_run``（pending/running/done/aborted）**分开建模**：
    生成是长事务多阶段，执行是波次跑批，两者状态机不兼容（v3 撞名教训）。

    - ``run_type``：本次编排在链路上的位置（§3.1 起止表）；``plan`` 只到门①产设计，
      ``reopen`` 由门② REJECT 已入库用例派生。
    - ``parent_run_id``：reopen/派生链（裸 UUID，无 FK——一条链的完整历史靠
      ``parent_run_id`` + 各资产外键回放）。
    - ``scope``：``{requirement_ids 默认=版本全部, surfaces?}``。
    - ``status``：13 态取值域（:data:`GEN_RUN_STATUSES`：主链 10 + 旁路 3）；
      主链流转与回边 ``evaluating → generating`` 见 state.py TRANSITIONS；
      旁路 paused/failed/cancelled 语义见 state.py RUN_BYPASS_STATUSES。
    - ``progress``：§3.3 结构，**前端唯一渲染源**。
    - ``budget``：``{tokens_used, tokens_limit, state}``；指挥中心每回合读 state，
      Blocking 只收口。
    - ``decisions``：指挥中心决策回合流水（每条带证据等级，可回放）。
    - ``config``：本次用的模型档/规程版本快照（复现用）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    version_id = fields.UUIDField(null=True)          # → mc_test_project_version（裸 UUID）
    run_type = fields.CharField(max_length=32)        # GEN_RUN_TYPES
    parent_run_id = fields.UUIDField(null=True)       # 派生链（reopen 指原 run）
    scope = fields.JSONField(default=dict)            # {requirement_ids, surfaces?}
    status = fields.CharField(max_length=32, default="pending")  # GEN_RUN_STATUSES
    progress = fields.JSONField(default=dict)         # §3.3 结构（前端唯一渲染源）
    budget = fields.JSONField(default=dict)           # {tokens_used, tokens_limit, state}
    decisions = fields.JSONField(default=list)        # 决策回合流水（可回放）
    config = fields.JSONField(default=dict)           # 模型档/规程版本快照
    created_by = fields.UUIDField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_gen_run"
        indexes = (
            ("project_id",),
            ("project_id", "status"),
            ("version_id",),
            ("parent_run_id",),
            ("project_id", "run_type"),
        )


class TestTestpoint(Model):
    """测试点 —— ``mc_test_testpoint``（§2.2，中枢；挂需求不挂模块）。

    五字段（title/trigger/sample_data/verify_result/impact）+ 依据（evidence）是
    测试点的最小完整体；``coverage_axis`` 与 ``surfaces`` 是**数组**（一个点可多轴、
    可多端），``surfaces`` 的判定即扇出计划（环节3）。

    - ``status``：门①确认后才建行，初始即 ``confirmed``（**无 candidate 态**）；
      ``generated`` = 该点 ``surfaces`` 每一端都有 confirmed 用例，人改端后自动重算。
    - ``surfaces_decided_by``：门①人改端记 ``user``（依据徽章的数据源）。
    - ``basis``：事实 fact（绿）/推断 assumption（黄）/待确认 to_confirm（红）。
    - ``case_review_state``：子 case proposal 评审态的**聚合**（TP 节点「用例评审中」
      徽章数据源，v4.1 终审#7）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)            # TP-0001（项目内发号）
    project_id = fields.UUIDField()
    requirement_id = fields.UUIDField(null=True)      # → mc_test_requirement（主脊）
    title = fields.CharField(max_length=512)
    trigger = fields.TextField(null=True)             # 触发条件
    sample_data = fields.TextField(null=True)         # 样例数据（数据集 schema 由此推导，§2.7）
    verify_result = fields.TextField(null=True)       # 预期/验证结果
    impact = fields.TextField(null=True)              # 影响面
    evidence = fields.TextField(null=True)            # 依据（引原文位置）
    coverage_axis = fields.JSONField(default=list)    # [C01..C14] 数组，一个点可多轴
    surfaces = fields.JSONField(default=list)         # [api|web|app|security] 数组
    surfaces_decided_by = fields.CharField(max_length=16, default="ai")  # ai|user
    design_method = fields.CharField(max_length=32, null=True)  # TESTPOINT_DESIGN_METHODS
    batch_tier = fields.CharField(max_length=8, null=True)      # A|B|C
    basis = fields.CharField(max_length=16, default="assumption")  # fact|assumption|to_confirm
    status = fields.CharField(max_length=32, default="confirmed")  # confirmed|generated
    case_review_state = fields.CharField(max_length=32, default="none")  # 聚合态
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_testpoint"
        unique_together = (("project_id", "code"),)
        indexes = (
            ("project_id",),
            ("requirement_id",),
            ("project_id", "status"),
            ("case_review_state",),
        )


class TestProposal(Model):
    """暂存区 —— ``mc_test_proposal``（§2.3，**AI 唯一写入口**）。

    铁律：AI 产物先落本表，人确认（门②）后才进正式表；门②之前 ``mc_test_case``
    零写入。三种 ``kind``（testpoint/case/dataset）共用一张表，形态差异全在
    ``payload``（JSONB）里。

    - ``slot_key`` 幂等键；``rewrite_of``/``version`` 是重写版本链——**同一 slot_key
      允许多行**（新行引父 proposal），所以 slot_key 只建普通索引，不建唯一索引
      （§2.3 原文「slot_key 允许带版本多行」；回收协议按 rewrite_of 链取最新）。
    - ``review_state`` 评审三道闸的态；``review_round`` 第几轮重写（棘轮比较用）。
    - ``score``/``score_breakdown`` 五维总分+分项分（门②组头「五维迷你条」数据源）；
      P0 无打分（第十部分 终审#4），两列先留空。
    - ``reviews`` ``{a:{model,opinion,verdict,abstain}, b:{...}, consensus}``。
    - ``dup_of_case_id``/``dup_score``/``dup_verdict`` 查重命中与处置。
    - ``applied_rules`` 规则回执 ``[{rule_id, disposition, why}]``——**未声明=违规**。
    - ``question_id`` defer 时关联的问题（答完自动重新入评审）。
    - ``status`` proposed → keep|modify|reject|defer（门②四判决）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    run_id = fields.UUIDField()                       # → mc_test_gen_run
    project_id = fields.UUIDField()
    kind = fields.CharField(max_length=16)            # testpoint|case|dataset
    payload = fields.JSONField(default=dict)          # 形态随 kind
    source_agent = fields.CharField(max_length=64, null=True)  # 哪个角色产的
    surface = fields.CharField(max_length=16, null=True)        # 该提案属于哪一端
    testpoint_id = fields.UUIDField(null=True)        # case 挂父点
    evidence_class = fields.CharField(max_length=16, null=True)  # executed|static|inferred|claimed
    review_state = fields.CharField(max_length=32, default="machine_pending")  # PROPOSAL_REVIEW_STATES
    review_round = fields.IntField(default=0)         # 第几轮重写（棘轮）
    rewrite_of = fields.UUIDField(null=True)          # 父 proposal（裸 UUID，版本链）
    version = fields.IntField(default=1)              # 重写版本号（同 slot_key 多行）
    score = fields.FloatField(null=True)              # 五维总分
    score_breakdown = fields.JSONField(default=dict)  # 分项分 {coverage,assert,exception,executable,maintainable}
    reviews = fields.JSONField(default=dict)          # 双评意见 + consensus
    dup_of_case_id = fields.UUIDField(null=True)      # 查重命中的正式用例
    dup_score = fields.FloatField(null=True)          # 相似度
    dup_verdict = fields.CharField(max_length=32, null=True)  # 并入/另立/维持（文档未给字面量）
    applied_rules = fields.JSONField(default=list)    # 规则回执 [{rule_id, disposition, why}]
    question_id = fields.UUIDField(null=True)         # defer 关联的问题
    slot_key = fields.CharField(max_length=256, null=True)  # 幂等键（可多版本 → 非唯一）
    status = fields.CharField(max_length=16, default="proposed")  # PROPOSAL_STATUSES
    provenance = fields.JSONField(default=dict)       # 模型ID/输入hash/skill_version
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_proposal"
        indexes = (
            ("run_id",),
            ("project_id",),
            ("run_id", "kind"),
            ("testpoint_id",),
            ("project_id", "status"),
            ("review_state",),
            ("project_id", "slot_key"),
            ("question_id",),
            ("rewrite_of",),
        )


class TestCoverageCell(Model):
    """覆盖矩阵格 —— ``mc_test_coverage_cell``（§2.5）。

    一行 = 一个「需求 × 轴 × 端」格。环节4 纯代码铺矩阵：空格 ``state=gap``（灰格），
    补点进暂存或标 ``na``（斜纹，**evidence 必填验证依据**——由调用方强制，模型层不判）。

    唯一键 ``(requirement_id, axis, surface)`` = 一格一行；热力图三态直接读本表
    （§3.3 ``coverage {lit, gap, na}`` 聚合源）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    requirement_id = fields.UUIDField()               # 格按需求铺（矩阵行键，非空）
    axis = fields.CharField(max_length=8)             # C01..C14
    surface = fields.CharField(max_length=16)         # api|web|app|security
    state = fields.CharField(max_length=16, default="gap")  # lit|gap|na
    evidence = fields.TextField(null=True)            # na 必填：验证依据
    testpoint_id = fields.UUIDField(null=True)        # 点亮该格的测试点
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_coverage_cell"
        unique_together = (("requirement_id", "axis", "surface"),)
        indexes = (
            ("project_id",),
            ("requirement_id",),
            ("project_id", "state"),
            ("testpoint_id",),
        )


class TestRule(Model):
    """用户规则 —— ``mc_test_rule``（§2.6；机制/四环节见第七部分，纯函数层在
    :mod:`testing_platform.generation.rules`）。

    - ``content``：人话一条（不退化自由文本：每条规则必有服务端执行点）。
    - ``kind``：hard 校验期代码判 / soft 评审期提示打分。
    - ``scope`` + ``target_type``/``target_id``：定位到哪个模块/需求/端（v4 补，
      否则「支付模块」无处挂）。``target_id`` 存**字符串**：module 是名字、
      requirement 是 UUID 的字符串形式、surface 是端名——故列宽 128、不做 UUID 列。
    - ``enforcement``：active/**shadow**/disabled，新规则默认 shadow。
    - ``hits``/``violations``：报表期统计（增量口径见 ``rules.bump_counters``）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    content = fields.TextField()                      # 人话规则正文
    kind = fields.CharField(max_length=16)            # hard|soft
    scope = fields.CharField(max_length=16)           # global|module|requirement|surface
    target_type = fields.CharField(max_length=32, null=True)   # 与 scope 同名（缺省由 service 补）
    target_id = fields.CharField(max_length=128, null=True)    # 实体标识（模块名/需求 UUID 串/端名）
    enforcement = fields.CharField(max_length=16, default=RULE_DEFAULT_ENFORCEMENT)  # active|shadow|disabled
    hits = fields.IntField(default=0)
    violations = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_rule"
        indexes = (
            ("project_id",),
            ("project_id", "scope"),
            ("enforcement",),
            ("target_type", "target_id"),
        )


# ==========================================================================
# §2.4 问题队列：扩列复用现库 mc_test_question（表已上线，**不新建**）
# ==========================================================================

#: :func:`ensure_question_columns` 要补的列（§2.4 原文六列）。
#: 全部**可空**——存量行（老疑问）没有这些值，NOT NULL 会让 ALTER 在 PG 上炸。
_QUESTION_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("requirement_id", "UUID"),           # 挂哪（需求）
    ("testpoint_id", "UUID"),             # 挂哪（测试点）
    ("recommended_answer", "TEXT"),       # AI 推荐答案（拷问式「每问带推荐」）
    ("answer", "TEXT"),                   # 人答
    ("round_no", "INTEGER"),              # 第几问（门⓪①各 10-15 预算计数）
    ("gate", "VARCHAR(16)"),              # req_review|point_confirm
)


async def _column_exists(conn: Any, table: str, column: str) -> bool:
    """跨库探测列是否存在（照 ``design/skeleton.py`` 与 ``projects/service.py``：
    PG 走 ``information_schema``，SQLite 无此视图 → ``PRAGMA table_info`` 兜底）。"""
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


async def ensure_question_columns(conn: Any) -> list[str]:
    """存量库给 ``mc_test_question`` 补生成流程缺的六列（§2.4，**幂等**）。

    只扩列、不动已有列、不建表：``mc_test_question`` 由
    ``testing_platform/quality/models.py`` 的 DDL/模型创建（本模块不 import 它，
    避免与并发会话耦合）。因此**表不存在时 ALTER 会直接抛错**（响亮失败，
    不静默 no-op——静默会让启动顺序问题变成运行时缺列）。

    ``resolution`` 的 ``risk_accepted``（门⓪死锁出口）**不加列不加约束**：该列是
    ``VARCHAR(32)`` 无 CHECK，扩展取值只需调用方写新值——字面量清单见模块常量
    :data:`QUESTION_RESOLUTIONS_GENERATION`。

    :returns: 本次真正补上的列名（第二次调用返回 ``[]``——幂等性的可断言证据）。
    """
    added: list[str] = []
    for name, col_type in _QUESTION_NEW_COLUMNS:
        if await _column_exists(conn, "mc_test_question", name):
            continue
        await conn.execute_script(f"ALTER TABLE mc_test_question ADD COLUMN {name} {col_type};")
        added.append(name)
    return added


__all__ = [
    # 枚举
    "GEN_RUN_TYPES",
    "GEN_RUN_STATUSES",
    "GEN_RUN_GATE_STATUSES",
    "GEN_RUN_BUDGET_STATES",
    "TESTPOINT_SURFACES",
    "TESTPOINT_SURFACES_DECIDED_BY",
    "TESTPOINT_DESIGN_METHODS",
    "TESTPOINT_DESIGN_METHOD_LABELS",
    "TESTPOINT_BATCH_TIERS",
    "TESTPOINT_BASES",
    "TESTPOINT_STATUSES",
    "TESTPOINT_CASE_REVIEW_STATES",
    "PROPOSAL_KINDS",
    "PROPOSAL_EVIDENCE_CLASSES",
    "PROPOSAL_REVIEW_STATES",
    "PROPOSAL_STATUSES",
    "PROPOSAL_RULE_DISPOSITIONS",
    "PROPOSAL_CONSENSUS",
    "COVERAGE_CELL_STATES",
    "RULE_KINDS",
    "RULE_SCOPES",
    "RULE_ENFORCEMENTS",
    "RULE_DEFAULT_ENFORCEMENT",
    "QUESTION_GATES",
    "QUESTION_RESOLUTIONS_GENERATION",
    # DDL 与模型
    "DDL",
    "TestGenRun",
    "TestTestpoint",
    "TestProposal",
    "TestCoverageCell",
    "TestRule",
    # 存量表扩列
    "ensure_question_columns",
]
