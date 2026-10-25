"""Tortoise models for the task domain (``mc_*`` tables only).

These mirror the upstream ent schema for the coding-agent task surface so the
ported handlers behave identically. They are storage only: the VM/agent runtime
itself lives in the external ``agent-compose`` service and is not implemented
here. The task rows record orchestration metadata + attribution (``user_id``).

Enums (from upstream consts):
* TaskType:    develop | design | review
* TaskSubType: generate_docs | generate_requirement | generate_design |
               generate_tasklist | execute_task | pr_review | diagnose_bug |
               fix_bug
* TaskStatus:  pending | processing | error | finished
* CliName:     codex | claude | opencode | cursor
* TaskRole:    design | develop | diagnose | fix | manual

``Task.mode`` is intentionally NOT an enum: editor permission/approval modes are
discovered from the node's reported editor capabilities (custom editor agents
can introduce arbitrary ids), so validity is checked against the target node's
advertised mode set rather than a fixed list here.
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class Task(Model):
    """A coding-agent task = the 'conversation' unit of the platform.

    Message history is NOT stored here (it lives in the external log store on
    the upstream side); this row only holds orchestration metadata + summary.
    """

    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField()
    kind = fields.CharField(max_length=32)  # TaskType
    sub_type = fields.CharField(max_length=32, null=True)  # TaskSubType
    # Canonical task-owned execution identity. The task is the product work unit;
    # provider/runtime capabilities may still use "editor" terminology internally.
    provider = fields.CharField(max_length=32, default="claude")
    node_id = fields.CharField(max_length=128, null=True)
    node_session_id = fields.CharField(max_length=128, null=True)
    provider_thread_id = fields.CharField(max_length=512, null=True)
    workspace_key = fields.CharField(max_length=255, null=True, unique=True)
    # pending（未派发）→ dispatching（公开创建已受理、后台派发中；DTO 投影为
    # 「准备中 · 正在连接执行节点」）→ ready（runtime 已就绪）→ dispatch_failed。
    workspace_state = fields.CharField(max_length=32, default="pending")
    # Which bring-up step the node last reported for this task, e.g.
    # ``workspace_prepare`` / ``git_clone`` / ``runtime_preflight`` /
    # ``runtime_start`` / ``running``. Empty until the first stage frame arrives.
    #
    # This is a *column*, not a config_snapshot key, because it is queried and
    # rendered on its own: the task list and detail page both need "is this task
    # still preparing, and where is it?" without loading and parsing a JSON blob,
    # and preparation must be distinguishable from a task that is genuinely
    # running a turn. ``status`` cannot carry it — status is the product-level
    # lifecycle (pending/processing/finished/error) and is what filters and the
    # recovery worker key on; overloading it with setup phases would change the
    # meaning of every existing query.
    runtime_stage = fields.CharField(max_length=32, default="")
    # Human-readable detail for the stage above (branch being cloned, or the
    # reason the step failed). Rendered verbatim, so the node redacts credentials
    # before sending it.
    runtime_stage_detail = fields.TextField(null=True)
    # False once a stage reports ok=false, so the UI can show the failing step
    # without re-deriving it from status + snapshot.
    runtime_stage_ok = fields.BooleanField(default=True)
    models_snapshot = fields.JSONField(null=True)
    # 模型映射（用户在创建/同步弹框里配的「编辑器模型 → 平台模型」表）。统一形状
    # {main, slots}（读取时 _migrate_model_mapping 兼容旧 roles/fallback/catalog 形状）：
    #   main  = 主模型。claude 是档名别名（如 "opus"），下发成 ANTHROPIC_MODEL；
    #           其余 provider 是平台模型 id。
    #   slots = 编辑器原生用途槽 {<槽名>: {model, fallback_models[]}}：
    #     claude   = 档名 opus/sonnet/haiku/fable → ANTHROPIC_DEFAULT_{档}_MODEL
    #     codex    = 目录条目名 → CODEX_MODEL_CATALOG
    #     opencode = OMO agent/category 名 → OPENCODE_MODELS
    #     dsh      = 模型条目名 → DSH_MODELS
    #     gemini / cursor = 无槽，只有 main
    # NULL = 未配映射，派发回落到老的 active_model 解析。值全部是平台模型 id（claude
    # 的 main 除外，它是档名）；只描述编辑器侧的模型菜单/角色，不再当子 Key 白名单用
    # （见 _task_llm_config）。
    model_mapping = fields.JSONField(null=True)
    config_snapshot = fields.JSONField(null=True)
    # Task-owned resource config is retired: skills/plugins are installed by the
    # ENVIRONMENT (mc_task_environment_resources + node sync), not carried by the
    # task. MCP service authorization is projected from the environment onto the
    # task principal's grants at dispatch (the mcp_config column is gone too).
    # Per-task overlays (currently issue-workflow MCP) survive base updates.
    mcp_overlay_json = fields.JSONField(default=list)
    # Task-owned MCP principal. The principal itself remains a normal mcp_users row;
    # this foreign key is owned by the task side, not the MCP module.
    mcp_user_id = fields.IntField(null=True)
    api_key_id = fields.IntField(null=True)
    parent_api_key_id = fields.IntField(null=True)
    usage_limit = fields.JSONField(null=True)
    expires_at = fields.DatetimeField(null=True)
    expected_client_id = fields.CharField(max_length=512, null=True)
    bootstrap_content_hash = fields.CharField(max_length=128, null=True)
    first_request_seen = fields.BooleanField(default=False)
    first_request_id = fields.TextField(null=True)
    bootstrap_consumed = fields.BooleanField(default=False)
    last_request_at = fields.DatetimeField(null=True)
    deleted_at = fields.DatetimeField(null=True)
    source_editor_id = fields.CharField(max_length=255, null=True)
    source_editor_session_id = fields.CharField(max_length=255, null=True)
    # Provider-defined permission/approval mode.
    mode = fields.CharField(max_length=128, null=True)
    mode_label = fields.CharField(max_length=255, null=True)
    content = fields.TextField()
    title = fields.TextField(null=True)
    summary = fields.TextField(null=True)
    status = fields.CharField(max_length=32, default="pending")
    created_at = fields.DatetimeField(auto_now_add=True)
    last_active_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    completed_at = fields.DatetimeField(null=True)

    class Meta:
        table = "mc_tasks"
        indexes = (("user_id",), ("status",), ("created_at",))


class ProjectTask(Model):
    """Binding of a task to a project/model/image/git/branch/cli."""

    id = fields.UUIDField(pk=True)
    task_id = fields.UUIDField()
    model_id = fields.UUIDField()
    image_id = fields.UUIDField()
    git_identity_id = fields.UUIDField(null=True)
    project_id = fields.UUIDField(null=True)
    issue_id = fields.UUIDField(null=True)
    # design | develop | diagnose | fix | manual —— which slot in the issue
    # workflow this task fills. Null for tasks created outside that flow.
    task_role = fields.CharField(max_length=32, null=True)
    repo_url = fields.TextField(null=True)
    repo_filename = fields.TextField(null=True)
    branch = fields.TextField(null=True)
    cli_name = fields.CharField(max_length=32, default="")  # CliName
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mc_project_tasks"
        indexes = (("task_id",), ("project_id",))


class TaskEvent(Model):
    """State + index for a task's conversation; the content lives in ClickHouse.

    The live SSE stream is fire-and-forget; this table is the durable record so a
    client can paginate history on reconnect. It is deliberately *not* where the
    conversation is stored — agent frames are appended to ClickHouse
    ``task_messages`` and this table keeps one thin row per frame
    (``kind='item_ref'``, NULL ``payload``) to carry the ordering (``seq``) and
    the join key (``logical_event_id``). Transport conditions (stage / error /
    message) and ``user_input`` rows with their end-to-end delivery state keep
    their small payload inline: they are state, not conversation.

    Splitting it this way is what lets the frame trail be append-only. Content in
    PostgreSQL forced a read-merge-update per frame — the runtime re-reports an
    item as it advances, so each report meant reading a growing jsonb blob,
    folding the new fields onto it, and writing it back — which cost a read per
    frame and destroyed the trail: a merged row can no longer say what the
    runtime actually reported, or in what order. The delivery state machine stays
    here because only PostgreSQL can do a conditional UPDATE; ClickHouse cannot.

    Raw stdout deltas are not stored anywhere, to keep both sides bounded.
    """

    id = fields.UUIDField(pk=True)
    task_id = fields.UUIDField()
    seq = fields.BigIntField()  # monotonic per-task sequence for cursor paging
    kind = fields.CharField(max_length=32)  # item_ref | item | stage | error | message
    event_type = fields.CharField(max_length=64, default="")
    # Conversation content moved to ClickHouse ``task_messages``; this column is
    # the legacy/fallback home. Non-NULL means "content is here" (rows written
    # before the split, or written while ClickHouse was unreachable); NULL on an
    # item row means "content is in ClickHouse, joined by logical_event_id".
    # Transport rows (stage/error/message) keep their small payload inline —
    # they are state, not conversation, and belong with the rest of the state.
    payload = fields.JSONField(null=True)
    # The runtime's stable id for one logical conversation item (a tool call's
    # tool_use_id, a message's id) — the join key to the ClickHouse frames, and
    # the key the read side merges on. Several rows share it: the runtime
    # re-reports an item as it advances and every report is its own frame.
    logical_event_id = fields.CharField(max_length=128, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    # 消息投递协议：client_message_id 是发送端生成的端到端幂等键，贯穿
    # 网关 → node_server → 节点 → runtime；delivery_status 记录投递状态机
    # pending → received → running → completed/cancelled/failed。仅 user_input
    # 事件携带；旧行与其它事件两列为 NULL，回放按已完成处理。
    client_message_id = fields.CharField(max_length=64, null=True)
    delivery_status = fields.CharField(max_length=16, null=True)
    # Logical message id stays stable across retries; delivery_attempt increments
    # only when a failed/cancelled turn is explicitly retried. Node/runtime
    # dedupe on (client_message_id, delivery_attempt), so transport replays do
    # not execute twice while a real retry still can.
    delivery_attempt = fields.IntField(default=1)
    received_at = fields.DatetimeField(null=True)
    started_at = fields.DatetimeField(null=True)
    completed_at = fields.DatetimeField(null=True)
    failure_reason = fields.TextField(null=True)

    class Meta:
        table = "mc_task_events"
        indexes = (("task_id", "seq"),)


class TaskStatusHistory(Model):
    """Append-only log of every task/message state transition.

    The task surface runs four state machines — ``mc_tasks.status``,
    ``mc_tasks.workspace_state``, ``mc_tasks.runtime_stage`` and
    ``mc_task_events.delivery_status`` — and all four are mutable columns written
    in place by both the gateway and node_server. When a task ended up in a state
    nobody expected, the only forensics available were the surviving timestamps,
    from which the path had to be guessed: a task showing ``error`` gave no answer
    to "did it ever reach processing, and who set error, and why".

    This table is that answer. One row per accepted transition, never updated,
    never deleted with the task: which machine (``field``), where it came from and
    went (``from_val`` → ``to_val``), why (``reason``), and which process wrote it
    (``source``). Writers are best-effort — a failed history insert must never
    abort the state change it describes — so a missing row means "diagnostics
    dropped it", not "the transition did not happen".

    ``message_id`` scopes ``delivery_status`` rows to one ``mc_task_events`` row,
    since a task has many user messages each running its own delivery machine.
    """

    id = fields.UUIDField(pk=True)
    task_id = fields.UUIDField()
    # status | workspace_state | runtime_stage | delivery_status
    field = fields.CharField(max_length=32)
    # Empty (not null) when there was no prior value — a first write, or a writer
    # that could not cheaply read the pre-image. Distinguishing "" from a real
    # state keeps a replay honest about what it does not know.
    from_val = fields.CharField(max_length=64, default="")
    to_val = fields.CharField(max_length=64, default="")
    # Free-text cause: the dispatch error, the stop request's origin, the runtime
    # failure text. Rendered to operators, so writers redact credentials.
    reason = fields.TextField(null=True)
    # gateway | node — which process observed the transition. The two write the
    # same columns by design (node_server must close out a task whose browser
    # never subscribed), so attribution is what separates their trails.
    source = fields.CharField(max_length=16, default="")
    # The mc_task_events row this transition belongs to, for delivery_status.
    # Null for the three task-level machines.
    message_id = fields.CharField(max_length=64, null=True)
    delivery_attempt = fields.IntField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mc_task_status_history"
        # (task_id, created_at) is the replay query: one task's whole trail in
        # order. (task_id, field) narrows it to a single machine.
        indexes = (("task_id", "created_at"), ("task_id", "field"))


class TaskCreateDefaults(Model):
    '''「创建任务」的上次配置缓存，按 (用户, 项目, 编辑器) 各存一份。

    存在的理由：创建任务要选的父 Key / 模型映射 / 思考等级 / 权限方式 / API Key
    限制，对同一个人在同一个项目上用同一个编辑器基本是**不变**的；每建一个任务
    都重填一遍是纯折磨。创建成功后写一次，下次打开弹框自动带入。

    存的是**表单态**而不是任务字段本身，所以整包用一个 JSON 列（形状跟着弹框走，
    加字段不用改表）；唯一键 (user_id, project_id, provider) 让 upsert 有落点。
    ``provider`` 进键是因为模型映射的形状、权限方式、思考等级的取值都按编辑器不同。
    '''

    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField()
    # 空项目（未绑项目）用零 UUID 占位：Postgres 的 UNIQUE 不把 NULL 视为相等，
    # 用 NULL 会让「无项目」这份缓存每次都插新行、永不命中。
    project_id = fields.UUIDField()
    provider = fields.CharField(max_length=32)
    # 表单态整包：{intent, branch_mode, branch, parent_api_key_id, model_mapping,
    # mode, reasoning_effort, editor_mode, selected_editors, rate_limit,
    # usage_limit, expires_at, selection_strategy}
    payload = fields.JSONField(default=dict)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = 'mc_task_create_defaults'
        indexes = (('user_id',),)
        unique_together = (('user_id', 'project_id', 'provider'),)
