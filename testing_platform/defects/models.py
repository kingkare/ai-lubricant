"""Tortoise models for the testing-platform **defect domain**.

⚠️ **解耦改版（用户定版）**：测试缺陷与 coding 平台彻底无关——
``mc_test_bug`` 是测试域**自己的缺陷实体**，`project_id` 直接是**测试项目 id**，
**不需要也不再有任何 coding 项目桥**（``coding_project_id``）。

（旧设计 design §21.3「bug=ProjectIssue(issue_type=bug)，唯一实体」已被推翻：
测试缺陷不与 coding 项目共享 issue 流。见本会话对齐。）

⚠️ **禅道对齐（用户定版 2026-10-16）**：缺陷域补上禅道的核心机制——
``resolution`` / ``bug_type`` / ``confirmed`` / ``activated_count`` / 双 build /
``deadline`` / ``escaped`` 等列，以及**动作守卫表** :data:`BUG_ACTIONS`
（对标禅道 ``isClickable``）。状态机保留我们更细的 7 态（见 :data:`BUG_STATUSES`）。
实证与落地差异见 ``plans/23-defect-zentao-parity.md``。

======  ==========================================================
表      说明
======  ==========================================================
``mc_test_bug``             测试缺陷主表（测试项目自持，独立于 coding）
``mc_test_bug_root_cause``  根因分析（1:N 挂 bug，可迭代；过根因门）
``mc_test_bug_pattern``     缺陷模式（沉淀回流，独立实体）
``mc_test_bug_log``         缺陷活动流（动作/状态变更逐行留痕）
======  ==========================================================

Conventions follow the sibling ``testing_platform/assets/models.py``:

* ``id`` is a UUID primary key,
* ``mc_`` table prefix,
* JSONB payloads use ``JSONField``,
* 无跨包 FK —— 由 service 层保证引用一致。

The same tables are exposed to the raw asyncpg path used by the main service:
``DDL`` + :func:`create_tables_via_pg` keep a ``PostgresClient``-only
deployment able to create the schema without Tortoise being initialized.

**迁移**：``mc_test_bug`` / ``bug_id`` 列为新增；``_ensure_additive_columns``
（user_platform/database.py）在启动时幂等补列。历史 coding 侧 bug 不迁移（清空重建）。
``mc_test_bug_ext`` 旧表退役，字段并入 ``mc_test_bug``。禅道对齐新增列同样走
``_ensure_additive_columns``；``fixed`` → ``resolved`` 状态改名走
``_migrate_bug_status_rename``（同文件一次性迁移）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from tortoise import fields
from tortoise.models import Model

# --------------------------------------------------------------------------
# 领域异常（base 放在最底层模型包，triage/service 共用，避免循环导入）。
# --------------------------------------------------------------------------
class DefectError(Exception):
    """缺陷域错误基类。"""


class DefectValidationError(DefectError, ValueError):
    """入参不合法（枚举越界、缺必填、引用不存在）。继承 ``ValueError``。"""


class RootCauseEvidenceRequiredError(DefectValidationError):
    """根因门未过：``evidence_chain`` 为空（design §18「根因→根因门」）。

    结构化错误：每条根因结论必须带证据引用，否则拒绝写入。
    """

    code = "root_cause_evidence_required"

    def __init__(self, message: str = "根因门未过：evidence_chain 为空，每条结论必须带证据引用",
                 *, field: str = "evidence_chain") -> None:
        super().__init__(message)
        self.field = field
        self.details = {"code": self.code, "field": field, "message": message}


class DefectNotFoundError(DefectError, LookupError):
    """bug/根因/模式不存在。"""


class BugTransitionNotAllowedError(DefectError, ValueError):
    """动作守卫未过：当前状态不允许该动作（对标禅道 ``isClickable``）。

    结构化错误：``details`` 带 ``action`` / ``from_status`` / ``allowed_actions``，
    前端据此渲染可用动作、后端据此给机器码。
    """

    code = "bug_transition_not_allowed"

    def __init__(
        self,
        message: str,
        *,
        action: str,
        from_status: str,
        allowed_actions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.action = action
        self.from_status = from_status
        self.details = {
            "code": self.code,
            "action": action,
            "from_status": from_status,
            "allowed_actions": allowed_actions or [],
            "message": message,
        }


class BugFieldRequiredError(DefectValidationError):
    """条件必填未满足（禅道实证：``resolve`` 必填 ``resolution``，且
    ``resolution=fixed`` 必填 ``resolved_build``、``duplicate`` 必填 ``duplicate_bug_id``）。
    """

    code = "bug_field_required"

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field
        self.details = {"code": self.code, "field": field, "message": message}


# --------------------------------------------------------------------------
# 枚举与中文标签（禅道对齐 §4.3；实证见 plans/23-defect-zentao-parity.md）
# --------------------------------------------------------------------------
#: ``mc_test_bug_root_cause.classification`` 根因分类（design §15.8 固定十类）
ROOT_CAUSE_CLASSIFICATIONS: tuple[str, ...] = (
    "logic",
    "boundary",
    "concurrency",
    "null",
    "config",
    "data",
    "integration",
    "requirement_ambiguity",
    "regression",
    "flaky",
)

#: ``mc_test_bug.status`` 状态机（测试域自有；保留比禅道更细的 7 态）。
#: open→confirmed→triaged→resolved→verified→closed；rejected = resolution(willnotfix/bydesign) 终态。
#: **不设 ``reopened`` 态**——reopen 是动作，用 ``activated_count`` 记次数（对齐禅道）。
BUG_STATUSES: tuple[str, ...] = (
    "open",
    "confirmed",
    "triaged",
    "resolved",
    "verified",
    "closed",
    "rejected",
)

#: 旧值别名（``fixed`` 在禅道是 resolution，不是 status；历史误写进 status 的行读侧归一）。
BUG_STATUS_ALIASES: dict[str, str] = {"fixed": "resolved"}

BUG_STATUS_LABELS: dict[str, str] = {
    "open": "新建",
    "confirmed": "已确认",
    "triaged": "已分流",
    "resolved": "已解决",
    "verified": "已验证",
    "closed": "已关闭",
    "rejected": "已驳回",
}

#: ``mc_test_bug.severity`` 严重度（保留我们的 5 级，比禅道 1-4 更细，不改）
BUG_SEVERITIES: tuple[str, ...] = ("blocker", "critical", "major", "minor", "trivial")
BUG_SEVERITY_LABELS: dict[str, str] = {
    "blocker": "致命",
    "critical": "严重",
    "major": "一般",
    "minor": "轻微",
    "trivial": "微小",
}

#: ``mc_test_bug.priority`` 优先级（对齐禅道 pri 1-4，默认 2）
BUG_PRIORITIES: tuple[int, ...] = (1, 2, 3, 4)

#: ``mc_test_bug.resolution`` 解决方案（照搬禅道 ``resolutionList`` 8 值）
BUG_RESOLUTIONS: tuple[str, ...] = (
    "fixed",
    "bydesign",
    "duplicate",
    "external",
    "notrepro",
    "postponed",
    "willnotfix",
    "tostory",
)
BUG_RESOLUTION_LABELS: dict[str, str] = {
    "fixed": "已解决",
    "bydesign": "设计如此",
    "duplicate": "重复Bug",
    "external": "外部原因",
    "notrepro": "无法重现",
    "postponed": "延期处理",
    "willnotfix": "不予解决",
    "tostory": "转需求",
}

#: ``resolution`` 取值后导致 bug 进入 ``rejected`` 终态的集合（willnotfix/bydesign）
BUG_REJECT_RESOLUTIONS: frozenset[str] = frozenset({"willnotfix", "bydesign"})

#: ``mc_test_bug.bug_type`` 缺陷类型（照搬禅道 ``typeList`` 10 值；discardedTypes 不引入）
BUG_TYPES: tuple[str, ...] = (
    "codeerror",
    "config",
    "install",
    "security",
    "performance",
    "standard",
    "automation",
    "designdefect",
    "codeimprovement",
    "others",
)
BUG_TYPE_LABELS: dict[str, str] = {
    "codeerror": "代码错误",
    "config": "配置相关",
    "install": "安装部署",
    "security": "安全相关",
    "performance": "性能问题",
    "standard": "标准规范",
    "automation": "测试脚本",
    "designdefect": "设计缺陷",
    "codeimprovement": "代码改进",
    "others": "其他",
}

#: ``mc_test_bug.found_in`` 发现阶段（我们自己的口径；禅道 ``found`` 无枚举）
BUG_FOUND_INS: tuple[str, ...] = ("test", "production", "feedback")
BUG_FOUND_IN_LABELS: dict[str, str] = {
    "test": "测试期",
    "production": "生产期",
    "feedback": "用户反馈",
}

#: ``mc_test_bug.os`` 操作系统（照搬禅道 ``osList``）
BUG_OS_LIST: tuple[str, ...] = (
    "all", "windows", "win11", "win10", "win8", "win7", "winxp",
    "osx", "android", "ios", "linux", "ubuntu", "chromeos", "fedora",
    "unix", "others",
)

#: ``mc_test_bug.browser`` 浏览器（照搬禅道 ``browserList``）
BUG_BROWSER_LIST: tuple[str, ...] = (
    "all", "chrome", "edge", "ie", "ie11", "ie10", "ie9", "ie8",
    "firefox", "opera", "safari", "360", "qq", "other",
)

#: 超长未解决阈值（对标 ``$config->bug->longlife = 7`` 天）
BUG_LONG_LIFE_DAYS: int = 7

#: closed 态不可编辑（禅道 ``bug_model.php:412`` 守卫）
TERMINAL_NOT_EDITABLE: frozenset[str] = frozenset({"closed"})


# --------------------------------------------------------------------------
# 动作守卫表（对标禅道 ``isClickable``，集中成表、不散落在各方法里）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BugAction:
    """一个状态流转动作的守卫定义（见 :data:`BUG_ACTIONS`）。

    ``to_status`` 是字符串或依 ``resolution`` 分流的 callable；
    ``conditional`` 形如 ``(("resolution", "fixed", ("resolved_build",)),)``——
    "字段等于该值时，这些字段必填"。``stamps`` 是写入时要打时间戳的字段名集合
    （实际填值在 service 层，dataclass 只声明意图，避免引入 datetime 依赖）。
    """

    name: str
    label: str
    from_statuses: frozenset[str]
    to_status: str | Callable[[Mapping[str, Any]], str]
    required: tuple[str, ...] = ()
    conditional: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    clears: tuple[str, ...] = ()
    sets_flags: tuple[tuple[str, Any], ...] = ()
    bumps: str | None = None  # 自增字段（activated_count）
    stamps: tuple[str, ...] = ()  # 要打时间戳的字段（resolved_at/closed_at/...）


def _resolve_to_status(action: BugAction, payload: Mapping[str, Any]) -> str:
    """``to_status`` 是 callable 时按 payload 分流（resolve 依 resolution）。"""
    ts = action.to_status
    return ts(payload) if callable(ts) else ts


def _resolve_target(payload: Mapping[str, Any]) -> str:
    """``resolve`` 动作的分流：willnotfix/bydesign → rejected，其余 → resolved。"""
    resolution = payload.get("resolution")
    return "rejected" if resolution in BUG_REJECT_RESOLUTIONS else "resolved"


#: 动作 → 守卫。**单入口** :func:`~testing_platform.defects.service.apply_bug_action`
#: 据此校验 from-status / required / conditional，再应用 sets_flags/stamps/clears/bumps。
BUG_ACTIONS: dict[str, BugAction] = {
    "confirm": BugAction(
        name="confirm",
        label="确认",
        from_statuses=frozenset({"open"}),
        to_status="confirmed",
        sets_flags=(("confirmed", True),),
        stamps=("confirmed_at",),
    ),
    "assign": BugAction(
        name="assign",
        label="指派",
        from_statuses=frozenset({"open", "confirmed", "triaged", "resolved"}),
        to_status="",  # 保持原状态，service 层不覆写 status
        required=("assignee_id",),
        stamps=("assigned_at",),
    ),
    "triage": BugAction(
        name="triage",
        label="分流",
        from_statuses=frozenset({"open", "confirmed"}),
        to_status="triaged",
    ),
    "resolve": BugAction(
        name="resolve",
        label="解决",
        from_statuses=frozenset({"open", "confirmed", "triaged"}),
        to_status=_resolve_target,
        required=("resolution",),
        conditional=(
            ("resolution", "fixed", ("resolved_build",)),
            ("resolution", "duplicate", ("duplicate_bug_id",)),
        ),
        stamps=("resolved_at",),
    ),
    "verify": BugAction(
        name="verify",
        label="验证",
        from_statuses=frozenset({"resolved"}),
        to_status="verified",
        stamps=("verified_at",),
    ),
    "close": BugAction(
        name="close",
        label="关闭",
        from_statuses=frozenset({"resolved", "verified", "rejected"}),
        to_status="closed",
        sets_flags=(("assignee_id", None),),  # 禅道 closed 后 assignee 置占位；这里清空
        stamps=("closed_at",),
    ),
    "reopen": BugAction(
        name="reopen",
        label="重开",
        from_statuses=frozenset({"confirmed", "triaged", "resolved", "verified", "closed", "rejected"}),
        to_status="",  # service 层按 confirmed 布尔位决定回 open 还是 confirmed
        clears=(
            "resolution", "resolved_build", "resolved_at",
            "verified_at", "closed_at", "closed_by", "resolved_by",
        ),
        bumps="activated_count",
        stamps=("activated_at",),
    ),
}


def available_actions(status: str, *, confirmed: bool = False) -> list[str]:
    """当前 status 下合法的动作名列表（前端/``available_actions`` 字段共用的单一真相）。

    ``reopen`` 的 to_status 依赖运行时 confirmed 位，这里不预先决定目标态，
    只判断 ``status`` 是否在该动作的 ``from_statuses`` 内。
    """
    result: list[str] = []
    for name, action in BUG_ACTIONS.items():
        if status in action.from_statuses:
            result.append(name)
    return result


def reopen_target(*, confirmed: bool) -> str:
    """重开回到哪个态：已确认过（confirmed 位真）→ confirmed，否则 open。"""
    return "confirmed" if confirmed else "open"


# --------------------------------------------------------------------------
# Raw DDL mirroring the models below (for ``PostgresClient.pool`` deployments).
# order: root_cause 依赖 bug（逻辑上，无硬 FK）。
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_bug (
        id                   UUID PRIMARY KEY,
        code                 VARCHAR(32)  NOT NULL,
        project_id           UUID         NOT NULL,
        title                VARCHAR(512) NOT NULL,
        symptom              TEXT,
        status               VARCHAR(32)  NOT NULL DEFAULT 'open',
        severity             VARCHAR(32)  NOT NULL DEFAULT 'major',
        priority             INTEGER      NOT NULL DEFAULT 2,
        summary              TEXT,
        resolution_note      TEXT,
        fingerprint          VARCHAR(64),
        repro                JSONB        NOT NULL DEFAULT '{}'::jsonb,
        case_refs            JSONB        NOT NULL DEFAULT '[]'::jsonb,
        scenario_refs        JSONB        NOT NULL DEFAULT '[]'::jsonb,
        scenario_id          UUID,
        from_run_scenario_id UUID,
        from_run_id          UUID,
        assignee_id          UUID,
        created_by           UUID,
        module               VARCHAR(128),
        version_id           UUID,
        strategy_id          UUID,
        requirement_id       UUID,
        tags                 JSONB        NOT NULL DEFAULT '[]'::jsonb,
        -- 禅道对齐新增列（2026-10-16；见 plans/23-defect-zentao-parity.md）
        resolution           VARCHAR(32),
        bug_type             VARCHAR(32),
        confirmed            BOOLEAN      NOT NULL DEFAULT false,
        confirmed_at         TIMESTAMPTZ,
        activated_count      INTEGER      NOT NULL DEFAULT 0,
        activated_at         TIMESTAMPTZ,
        opened_build         VARCHAR(128),
        resolved_build       VARCHAR(128),
        duplicate_bug_id     UUID,
        related_bug_ids      JSONB        NOT NULL DEFAULT '[]'::jsonb,
        deadline             TIMESTAMPTZ,
        resolved_at          TIMESTAMPTZ,
        resolved_by          UUID,
        verified_at          TIMESTAMPTZ,
        verified_by          UUID,
        closed_by            UUID,
        found_in             VARCHAR(32),
        escaped              BOOLEAN      NOT NULL DEFAULT false,
        os                   VARCHAR(32),
        browser              VARCHAR(32),
        hardware             VARCHAR(128),
        keywords             VARCHAR(255),
        mailto               JSONB        NOT NULL DEFAULT '[]'::jsonb,
        feedback_by          VARCHAR(128),
        sub_status           VARCHAR(32),
        color                VARCHAR(16),
        assigned_at          TIMESTAMPTZ,
        last_edited_by       UUID,
        folder_id            UUID,
        created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
        closed_at            TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_bug_root_cause (
        id                  UUID PRIMARY KEY,
        bug_id              UUID,
        issue_id            UUID,  -- legacy（旧 coding issue 桥）；本表已不用
        location            VARCHAR(512),
        classification      VARCHAR(32),
        attributed_commit   VARCHAR(64),
        hypothesis          TEXT,
        confidence          REAL,
        fix_suggestion      TEXT,
        healing_suggestion  TEXT,
        blast_radius        JSONB        NOT NULL DEFAULT '[]'::jsonb,
        evidence_chain      JSONB        NOT NULL DEFAULT '[]'::jsonb,
        created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_bug_log (
        id           UUID PRIMARY KEY,
        bug_id       UUID         NOT NULL,
        action       VARCHAR(32)  NOT NULL,
        from_status  VARCHAR(32),
        to_status    VARCHAR(32),
        actor        UUID,
        comment      TEXT,
        extra        JSONB        NOT NULL DEFAULT '{}'::jsonb,
        occurred_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_bug_pattern (
        id                   UUID PRIMARY KEY,
        symptom_pattern      VARCHAR(512) NOT NULL UNIQUE,
        root_cause_kind      VARCHAR(32)  NOT NULL,
        prevention           TEXT,
        regression_template  JSONB        NOT NULL DEFAULT '{}'::jsonb,
        hit_count            INTEGER      NOT NULL DEFAULT 0,
        created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_bug_project   ON mc_test_bug(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_version     ON mc_test_bug(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_strategy    ON mc_test_bug(strategy_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_requirement ON mc_test_bug(requirement_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_fingerprint ON mc_test_bug(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_status     ON mc_test_bug(status)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_project_status ON mc_test_bug(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_assignee    ON mc_test_bug(assignee_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_resolution  ON mc_test_bug(resolution)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_type        ON mc_test_bug(bug_type)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_dup         ON mc_test_bug(duplicate_bug_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_found_in    ON mc_test_bug(found_in)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_deadline    ON mc_test_bug(deadline)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_folder      ON mc_test_bug(folder_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_rc_bug     ON mc_test_bug_root_cause(bug_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_rc_class   ON mc_test_bug_root_cause(classification)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_pattern_kind ON mc_test_bug_pattern(root_cause_kind)",
    "CREATE INDEX IF NOT EXISTS idx_test_bug_log_bug     ON mc_test_bug_log(bug_id)",
]


class TestBug(Model):
    """测试缺陷主表（**测试项目自持，独立于 coding**）。

    ``project_id`` 是**测试项目 id**（不再桥接 coding 项目）；``code`` 形如
    ``BUG-0001``（按 project 内 max+1，service 层生成）。``fingerprint`` 是失败指纹
    （design §04 §4），跨 run 去重——同签名归并到同一 bug。
    ``repro`` 存复现维度（steps/env/data/rate）；``case_refs``/``scenario_refs`` 记
    关联用例/场景；``scenario_id``/``from_run_scenario_id``/``from_run_id`` 记来源执行。
    ``root_causes`` 由 :class:`TestBugRootCause` 1:N 关联。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)  # BUG-0001
    project_id = fields.UUIDField()  # → mc_test_project.id（测试项目，非 coding）
    title = fields.CharField(max_length=512)
    symptom = fields.TextField(null=True)  # 症状描述
    status = fields.CharField(max_length=32, default="open")
    severity = fields.CharField(max_length=32, default="major")
    priority = fields.IntField(default=2)  # 1/2/3/4（对齐禅道 pri）
    summary = fields.TextField(null=True)
    resolution_note = fields.TextField(null=True)
    fingerprint = fields.CharField(max_length=64, null=True)  # 失败指纹·去重
    repro = fields.JSONField(default=dict)  # 复现维度 steps/env/data/rate
    case_refs = fields.JSONField(default=list)
    scenario_refs = fields.JSONField(default=list)
    scenario_id = fields.UUIDField(null=True)  # 关联场景
    from_run_scenario_id = fields.UUIDField(null=True)  # 来源执行
    from_run_id = fields.UUIDField(null=True)  # 来源运行
    assignee_id = fields.UUIDField(null=True)
    created_by = fields.UUIDField(null=True)
    module = fields.CharField(max_length=128, null=True)  # 所属功能模块（分组维度）
    version_id = fields.UUIDField(null=True)  # → mc_test_project_version（缺陷关联版本）
    strategy_id = fields.UUIDField(null=True)  # → mc_test_strategy（缺陷第二维度：版本×策略）
    requirement_id = fields.UUIDField(null=True)  # → mc_test_requirement（挂到需求，主脊）
    tags = fields.JSONField(default=list)
    # ── 禅道对齐新增列（2026-10-16；见 plans/23-defect-zentao-parity.md）──
    resolution = fields.CharField(max_length=32, null=True)  # 解决方案（BUG_RESOLUTIONS）
    bug_type = fields.CharField(max_length=32, null=True)  # 缺陷类型（BUG_TYPES）
    confirmed = fields.BooleanField(default=False)  # 确认位（正交于状态；一旦为真不回退）
    confirmed_at = fields.DatetimeField(null=True)
    activated_count = fields.IntField(default=0)  # 重开次数（返工率信号，对齐禅道 activatedCount）
    activated_at = fields.DatetimeField(null=True)
    opened_build = fields.CharField(max_length=128, null=True)  # 影响版本（禅道 openedBuild）
    resolved_build = fields.CharField(max_length=128, null=True)  # 解决版本（禅道 resolvedBuild）
    duplicate_bug_id = fields.UUIDField(null=True)  # 重复 bug（resolution=duplicate 时必填）
    related_bug_ids = fields.JSONField(default=list)  # 相关 bug（禅道 relatedBug）
    deadline = fields.DatetimeField(null=True)  # 处理期限（禅道 deadline）
    resolved_at = fields.DatetimeField(null=True)  # 解决时刻（MTTR 计算）
    resolved_by = fields.UUIDField(null=True)
    verified_at = fields.DatetimeField(null=True)  # 验证时刻
    verified_by = fields.UUIDField(null=True)
    closed_by = fields.UUIDField(null=True)
    found_in = fields.CharField(max_length=32, null=True)  # 发现阶段（BUG_FOUND_INS）
    escaped = fields.BooleanField(default=False)  # 逃逸 bug（上线后才发现）
    os = fields.CharField(max_length=32, null=True)  # 操作系统（BUG_OS_LIST）
    browser = fields.CharField(max_length=32, null=True)  # 浏览器（BUG_BROWSER_LIST）
    hardware = fields.CharField(max_length=128, null=True)
    keywords = fields.CharField(max_length=255, null=True)
    mailto = fields.JSONField(default=list)  # 抄送（禅道 mailto）
    feedback_by = fields.CharField(max_length=128, null=True)  # 反馈者
    sub_status = fields.CharField(max_length=32, null=True)  # 子状态（禅道 subStatus）
    color = fields.CharField(max_length=16, null=True)  # 标题色标（禅道 color）
    assigned_at = fields.DatetimeField(null=True)
    last_edited_by = fields.UUIDField(null=True)
    folder_id = fields.UUIDField(null=True)  # → mc_test_catalog.id（type=bug 目录树；裸 UUID 无 FK）
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    closed_at = fields.DatetimeField(null=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_bug"
        unique_together = (("project_id", "code"),)
        indexes = (
            ("project_id",),
            ("fingerprint",),
            ("status",),
            ("version_id",),
            ("strategy_id",),
            ("resolution",),
            ("bug_type",),
            ("duplicate_bug_id",),
            ("found_in",),
            ("folder_id",),
        )


class TestBugRootCause(Model):
    """根因分析（1:N 挂 bug，可迭代——每次诊断追加一行）。

    **根因门**（design §18）：``evidence_chain`` 为空则拒绝写入（service 层强制）。
    ``bug_id`` 指向 :class:`TestBug.id`。``issue_id`` 为 legacy 列（旧 coding 桥），
    本版已不使用，留列避免破坏既有表。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    bug_id = fields.UUIDField()  # → mc_test_bug.id（1:N）
    issue_id = fields.UUIDField(null=True)  # legacy（弃用）
    location = fields.CharField(max_length=512, null=True)
    classification = fields.CharField(max_length=32, null=True)
    attributed_commit = fields.CharField(max_length=64, null=True)
    hypothesis = fields.TextField(null=True)
    confidence = fields.FloatField(null=True)
    fix_suggestion = fields.TextField(null=True)
    healing_suggestion = fields.TextField(null=True)
    blast_radius = fields.JSONField(default=list)
    evidence_chain = fields.JSONField(default=list)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_bug_root_cause"
        indexes = (("bug_id",), ("classification",))


class TestBugPattern(Model):
    """缺陷模式（沉淀回流，独立实体——不挂在具体 bug 上）。

    ``symptom_pattern`` 是症状签名；``hit_count`` 累计命中次数。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    symptom_pattern = fields.CharField(max_length=512)
    root_cause_kind = fields.CharField(max_length=32)
    prevention = fields.TextField(null=True)
    regression_template = fields.JSONField(default=dict)
    hit_count = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_bug_pattern"
        unique_together = (("symptom_pattern",),)
        indexes = (("root_cause_kind",),)


class TestBugLog(Model):
    """缺陷活动流（1:N 挂 bug）——每次动作/状态变更记一行。

    命名对齐 21 号文 §3.2 的 ``mc_test_version_log``（同一形状：action /
    from_value / to_value / actor / reason / occurred_at）。消费方三处：
    详情页时间线、MTTR/重开时间线的复算、复盘的事实源。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    bug_id = fields.UUIDField()  # → mc_test_bug.id（1:N）
    action = fields.CharField(max_length=32)  # confirm/assign/resolve/.../update/create
    from_status = fields.CharField(max_length=32, null=True)
    to_status = fields.CharField(max_length=32, null=True)
    actor = fields.UUIDField(null=True)  # 操作人
    comment = fields.TextField(null=True)
    extra = fields.JSONField(default=dict)  # 动作附带字段（resolution/build/...）
    occurred_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_bug_log"
        indexes = (("bug_id",),)


async def create_tables_via_pg() -> None:
    """Create ``mc_test_bug*`` tables through the platform raw PG client."""
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
