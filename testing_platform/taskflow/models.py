"""任务层（26 号文 §1.4）——「一次具体执行」的载体。

与 25 号文的关系（D-1 已裁：**两层，不冲突、不泛化**）：

* **25 号文 = 环节/方法层**：生成方案的方法 + 具体执行过程（怎么分析、怎么过门、
  怎么推进）。落点是 ``generation/state.py`` 的 13 态 ``mc_test_gen_run``，**不改**。
* **本文 = 任务层**：梳理关系、跟用户对齐（交互方式、实现状态、进度可见、
  什么时候叫人）。落点 ``mc_test_task_run``，五段生命周期。

两者是**包含关系**：一个 ``executing`` 的任务内部可以正跑着生成链的
``analyzing → points_proposed → …``。任务层不关心环节内部怎么推进，
只关心「用户现在看到什么、该谁动手」。

对外表面（``__all__``）
----------------------

================================  =========================================
:data:`TASK_TYPES`                五类任务（explore/gen_case/gen_scenario/req_analysis/execution）
:data:`TASK_PHASES`               五段（create/plan/confirm/execute/apply）
:data:`TASK_RUN_STATUSES`         任务层状态机取值域（含 blocked = 缺依赖等人）
:data:`PHASE_OF_STATUS`           状态 → 阶段（面板分组渲染用）
:func:`can_transition`            状态流转判定
:class:`TestTaskRun`              ``mc_test_task_run`` 表
================================  =========================================

依赖纪律
--------
**不 import ``generation`` / ``assets`` / ``tasks``**（并发热区，见记忆
「concurrent-sessions-overwrite-worktree」「shared-worktree-no-branch-switch」）。
需要环节产物时由调用方传 dict 进来。

歧义记录
--------
1. **``blocked`` vs ``paused``**：``blocked`` = agent 撞到缺依赖（凭据/环境/客户端）
   **自动**停下等人；``paused`` = **人**手动暂停。恢复语义不同：blocked 补齐依赖后
   回 ``executing``，paused 由人显式 resume 回暂停前态（记 ``resume_from``）。
2. **plan 存哪**：方案存本行 ``plan`` JSONB（不是新表）。理由：方案是任务的一个
   属性、随任务生死；版本化靠 ``plan_revision`` + ``mc_test_change_record`` 现有惯例。
3. **产物索引**：不建中间表。产物（用例/需求/测试点）自带
   ``task_run_id`` + ``staged`` 两个标识列（26 号文 §1.8，D-6 已裁：
   **共用正常数据表 + 额外字段标识**，不落独立暂存分区）。
4. **``type=execution`` 与既有 ``mc_test_task``/``mc_test_run``**：那两张是
   04 号文的**执行域**（配置 + 跑批结果），本表是**任务层**（交互/状态）。
   执行类任务用 ``refs.execution_task_id`` 指过去，不重复建模。
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

__all__ = [
    "DDL",
    "TASK_TYPES",
    "TASK_TYPE_LABELS",
    "TASK_PHASES",
    "TASK_PHASE_LABELS",
    "TASK_RUN_STATUSES",
    "TASK_RUN_STATUS_LABELS",
    "TERMINAL_TASK_STATUSES",
    "WAITING_TASK_STATUSES",
    "PHASE_OF_STATUS",
    "STATUS_LABELS_BY_TYPE",
    "explore_scope_required",
    "can_transition",
    "is_terminal",
    "is_waiting_on_human",
    "phase_of",
    "TestTaskRun",
    "create_tables_via_pg",
]

# --------------------------------------------------------------------------
# 取值域
# --------------------------------------------------------------------------

#: 五类任务（26 号文 §1.1，用户口径原文顺序）。
TASK_TYPES: tuple[str, ...] = (
    "explore",        # 探索（入口任务：可无需求输入，可**产出**需求）
    "gen_case",       # 生成用例
    "gen_scenario",   # 生成场景
    "req_analysis",   # 需求分析
    "execution",      # 测试执行
)

#: 人读标签（面板 tab / 徽章）。
TASK_TYPE_LABELS: dict[str, str] = {
    "explore": "探索",
    "gen_case": "生成用例",
    "gen_scenario": "生成场景",
    "req_analysis": "需求分析",
    "execution": "测试执行",
}

#: 五段生命周期（26 号文 §1.2）。
TASK_PHASES: tuple[str, ...] = (
    "create",    # 创建
    "plan",      # 生成方案
    "confirm",   # 确定方案
    "execute",   # 执行方案
    "apply",     # 应用方案
)

#: 阶段 → 中文（面板步骤条）。
TASK_PHASE_LABELS: dict[str, str] = {
    "create": "创建",
    "plan": "生成方案",
    "confirm": "确定方案",
    "execute": "执行方案",
    "apply": "应用方案",
}

#: ``mc_test_task_run.status`` 取值域（26 号文 §1.4 任务层状态机）。
#: 主链 6 态 + 停顿 2 态（planned/blocked 等人）+ 旁路 3 态。
TASK_RUN_STATUSES: tuple[str, ...] = (
    "draft",       # 已创建，未开始规划
    "planning",    # agent 在生成方案（可多轮）
    "planned",     # 方案已出，**等人确定**
    "executing",   # 执行中，进度可见
    "blocked",     # **缺依赖等人**（凭据/环境/客户端不具备）——不是人手动停的
    "applying",    # 结果应用（staged → 正式）
    "completed",   # 终态：已应用
    "failed",      # 终态：异常收口
    "cancelled",   # 终态：人为中止
)

#: 状态 → 中文标签（面板徽章；与 25 号文 RUN_STATUS_LABELS 无关，各层各的）。
TASK_RUN_STATUS_LABELS: dict[str, str] = {
    "draft": "草稿",
    "planning": "规划中",
    "planned": "待确定方案",
    "executing": "执行中",
    "blocked": "缺依赖待处理",
    "applying": "应用中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

#: 状态 → 五段阶段（面板步骤条高亮哪一段）。
PHASE_OF_STATUS: dict[str, str] = {
    "draft": "create",
    "planning": "plan",
    "planned": "confirm",
    "executing": "execute",
    "blocked": "execute",     # 阻塞是执行段里的一次停顿
    "applying": "apply",
    "completed": "apply",
    "failed": "execute",      # 失败可能发生在任意段，看 fail_from_phase
    "cancelled": "create",
}

#: 终态（无出边）。
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

#: 等人态（面板要提示"该你动手了"，不轮询进度）。
WAITING_TASK_STATUSES: frozenset[str] = frozenset({"planned", "blocked"})

#: 合法转换边（主链 + 停顿/恢复 + 任意非终态→失败/取消）。
#: planning→planning 允许（多轮迭代改方案）。
#: draft→planned 允许：一次性生成方案（编排器 ``plan_explore`` 单调用即出方案），
#: 跳过可见的 planning 态——省一跳但 UI 不显示"规划中"瞬时态，权衡可接受。
TASK_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("draft", "planning"),
    ("draft", "planned"),
    ("planning", "planning"),      # 方案迭代
    ("planning", "planned"),       # 方案出好
    ("planned", "planning"),       # 打回重规划
    ("planned", "executing"),      # 确定方案 → 执行
    ("executing", "blocked"),      # 撞到缺依赖
    ("blocked", "executing"),      # 补齐依赖后继续
    ("blocked", "planning"),       # 依赖影响方案 → 重新规划
    ("executing", "applying"),     # 跑完 → 应用
    ("executing", "failed"),
    ("blocked", "failed"),
    ("applying", "completed"),
    ("applying", "failed"),
})

#: 取消边：任意非终态 → cancelled（人中止）。
CANCEL_EDGES: frozenset[tuple[str, str]] = frozenset(
    (s, "cancelled") for s in TASK_RUN_STATUSES if s not in TERMINAL_TASK_STATUSES
)

#: 各任务类型「创建」表单必填字段（26 号文 §1.6，用户点名的维度）。
#: 空集 = 无必填维度（如需求分析可先只选范围）。
STATUS_LABELS_BY_TYPE: dict[str, dict[str, str]] = {
    "explore": {
        "purpose": "探索目的",       # 补齐用例/补齐需求/探索测试/其他
        "direction": "探索方向",     # api/web/app 多选
        "client": "探索客户端",     # 用什么手段探（浏览器/设备/HTTP 客户端）
        "target": "被探索的配置",   # 探哪个目标（环境/URL/接口集/应用包）
        "scope_mode": "范围模式",    # full 全量 / incremental 增量
        "required_env_vars": "必需环境变量",  # 键名列表；缺任一 → blocked 等人
        "note": "说明",
    },
    "gen_case": {
        "surfaces": "哪些客户端",
        "modules": "哪些模块",
        "features": "哪些功能",
    },
    "gen_scenario": {
        "surfaces": "哪些客户端",
        "case_ids": "哪些用例",
    },
    "req_analysis": {
        "requirement_ids": "分析哪些需求",
    },
    "execution": {
        "scope": "执行哪一些",
        "plan": "执行方案",
    },
}

#: 探索范围模式（D-4 裁决：全量 / 增量按字段定位）。
#: 探索范围模式（D-4 裁决：全量 / 增量按字段定位）。
EXPLORE_SCOPE_MODES: tuple[str, ...] = ("full", "incremental")

#: 探索方向取值域（26 号文 §1.6「探索方向」——与端 surface 同名，便于映射）。
#: ``security`` 是第四端（见 explore_gate.SECURITY_SURFACE），探索方向同样覆盖。
EXPLORE_DIRECTIONS: tuple[str, ...] = ("api", "web", "app", "security")

#: 探索目的取值域（D-4 裁决：目的不同 → 方案关注点不同）。
EXPLORE_PURPOSES: tuple[str, ...] = (
    "fill_cases",        # 补齐用例
    "fill_requirements", # 补齐需求
    "exploratory_test",  # 探索测试（charter 驱动找缺陷）
    "other",             # 其他
)

#: 探索目的 → 中文（前端下拉/面板展示）。
EXPLORE_PURPOSE_LABELS: dict[str, str] = {
    "fill_cases": "补齐用例",
    "fill_requirements": "补齐需求",
    "exploratory_test": "探索测试",
    "other": "其他",
}


def explore_scope_required() -> tuple[str, ...]:
    """探索任务创建时的必填维度（26 号文 §1.6）。"""
    return ("purpose", "direction", "client", "target")


def can_transition(current: str, to: str) -> bool:
    """任务层状态流转是否允许（纯判定，不查库）。"""
    if current not in PHASE_OF_STATUS or to not in PHASE_OF_STATUS:
        return False
    if current in TERMINAL_TASK_STATUSES:
        return False
    if (current, to) in TASK_TRANSITIONS or (current, to) in CANCEL_EDGES:
        return True
    # 任意非终态 → failed（异常/预算耗尽）
    return to == "failed"


def is_terminal(status: str) -> bool:
    """是否终态（无出边）。"""
    return status in TERMINAL_TASK_STATUSES


def is_waiting_on_human(status: str) -> bool:
    """是否在等人（planned 等确定方案 / blocked 等补依赖）。"""
    return status in WAITING_TASK_STATUSES


def phase_of(status: str) -> str:
    """状态 → 五段之一（未知态回 ``create``，面板不至于空白）。"""
    return PHASE_OF_STATUS.get(status, "create")


# --------------------------------------------------------------------------
# Raw DDL（PostgresClient.pool 部署用；与模型列一一对应）
# --------------------------------------------------------------------------
DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS mc_test_task_run (
        id                UUID PRIMARY KEY,
        code              VARCHAR(32)  NOT NULL,
        project_id        UUID         NOT NULL,
        type              VARCHAR(32)  NOT NULL,
        status            VARCHAR(32)  NOT NULL DEFAULT 'draft',
        phase_status      VARCHAR(32)  NOT NULL DEFAULT 'create',
        title             VARCHAR(512) NOT NULL,
        note              TEXT,
        scope             JSONB        NOT NULL DEFAULT '{}'::jsonb,
        plan              JSONB        NOT NULL DEFAULT '{}'::jsonb,
        plan_revision     INTEGER      NOT NULL DEFAULT 0,
        resources         JSONB        NOT NULL DEFAULT '{}'::jsonb,
        progress          JSONB        NOT NULL DEFAULT '{}'::jsonb,
        blockers          JSONB        NOT NULL DEFAULT '[]'::jsonb,
        refs              JSONB        NOT NULL DEFAULT '{}'::jsonb,
        resume_from       VARCHAR(32),
        fail_from_phase   VARCHAR(32),
        env_id            UUID,
        created_by        UUID         NOT NULL,
        confirmed_by      UUID,
        confirmed_at      TIMESTAMPTZ,
        started_at        TIMESTAMPTZ,
        finished_at       TIMESTAMPTZ,
        version           INTEGER      NOT NULL DEFAULT 1,
        created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
        CONSTRAINT ck_test_task_run_type
            CHECK (type IN ('explore','gen_case','gen_scenario','req_analysis','execution')),
        CONSTRAINT ck_test_task_run_status
            CHECK (status IN ('draft','planning','planned','executing','blocked',
                              'applying','completed','failed','cancelled'))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_test_task_run_code   ON mc_test_task_run(project_id, code)",
    "CREATE INDEX IF NOT EXISTS idx_test_task_run_project      ON mc_test_task_run(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_task_run_type_status  ON mc_test_task_run(project_id, type, status)",
)


class TestTaskRun(Model):
    """任务层运行实例 —— ``mc_test_task_run``（26 号文 §1.1–§1.5）。

    一行 = 用户认知里的「一个任务」：它是某类任务的一次执行，带五段状态、
    方案、资源计划、进度、阻塞项，以及产物索引（产物靠 ``task_run_id`` 反查）。

    - ``type``：:data:`TASK_TYPES` 五类之一；**决定** ``scope`` 的形状
      （见 :data:`STATUS_LABELS_BY_TYPE`，26 号文理由 5「每个任务维度不一样」）。
    - ``status``：:data:`TASK_RUN_STATUSES`（任务层状态机，与 25 号文环节态无关）。
    - ``phase_status``：五段之一（:func:`phase_of` 的落库冗余，面板按段分组/过滤用，
      **读侧以 status 派生为准**——防两列漂移）。
    - ``scope``：创建时用户选的维度（任务类型专属）。
    - ``plan``：**方案**（角色分工 / 执行步骤 / 资源计划 / 依赖检查 / 目的关注点）。
      生成方案阶段由 agent 写，确定方案阶段用户可编辑后冻结。
    - ``plan_revision``：方案第几版（迭代次数）。打回重规划时 +1。
    - ``resources``：``{agent_count, tokens_limit, tokens_used, ...}``（理由 3：
      不同任务量成本不同，用户要能控制派多少 agent）。
    - ``progress``：执行进度（面板唯一渲染源；``{steps:[{role,state,detail,since}], …}``）。
    - ``blockers``：缺的依赖清单（``[{kind, name, why, how_to_fix}]``）——
      进 ``blocked`` 态时写，理由 4「停下来等用户」的可执行版本。
    - ``refs``：关联既有实体（``{gen_run_id?, execution_task_id?, version_id?}``）。
      ``gen_run_id`` 指向 25 号文 ``mc_test_gen_run``（生成类任务用它跑环节层）——
      **两层的连接点**。
    - ``resume_from``：``paused``/``blocked`` 恢复时要回的原态。
    - ``fail_from_phase``：失败发生在哪一段（排障 + 「怎么回滚」的线索，理由 1）。
    """

    __test__ = False  # 类名以 Test 开头：告知 pytest 这不是测试用例

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)              # TRUN-xxxx（项目内发号）
    project_id = fields.UUIDField()
    type = fields.CharField(max_length=32)              # TASK_TYPES
    status = fields.CharField(max_length=32, default="draft")   # TASK_RUN_STATUSES
    phase_status = fields.CharField(max_length=32, default="create")  # TASK_PHASES（冗余）
    title = fields.CharField(max_length=512)
    note = fields.TextField(null=True)                  # 创建时的「说明」
    scope = fields.JSONField(default=dict)              # 任务类型专属维度
    plan = fields.JSONField(default=dict)               # 方案（生成→可编辑→冻结）
    plan_revision = fields.IntField(default=0)          # 方案第几版
    resources = fields.JSONField(default=dict)          # {agent_count, tokens_limit, ...}
    progress = fields.JSONField(default=dict)           # 面板唯一渲染源
    blockers = fields.JSONField(default=list)           # 缺的依赖清单
    refs = fields.JSONField(default=dict)               # 关联既有实体（gen_run_id 等）
    resume_from = fields.CharField(max_length=32, null=True)
    fail_from_phase = fields.CharField(max_length=32, null=True)
    env_id = fields.UUIDField(null=True)                # 选定的环境（裸 UUID，无 FK）
    created_by = fields.UUIDField()
    confirmed_by = fields.UUIDField(null=True)          # 确定方案的人
    confirmed_at = fields.DatetimeField(null=True)
    started_at = fields.DatetimeField(null=True)
    finished_at = fields.DatetimeField(null=True)
    version = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_task_run"
        unique_together = (("project_id", "code"),)
        indexes = (
            ("project_id",),
            ("project_id", "type"),
            ("project_id", "status"),
            ("env_id",),
        )


async def create_tables_via_pg() -> None:
    """通过平台原生 PG client 建 ``mc_test_task_run``（Tortoise 未初始化的部署路径）。

    与同域其它包一致：``from db import PostgresClient``，全 ``IF NOT EXISTS`` 幂等。
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
