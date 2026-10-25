"""GA 配置模块"""
from dataclasses import dataclass, field

# 单个 Agent 一轮任务的轮数上限默认值。唯一真相源：所有调用点（交互态 / CDP /
# 无人值守）取「请求级 max_turns → agents 行 max_turns → 本默认」，不再各自写
# 字面量（历史上散落 80/50/40，导致同一轮 handler 与 loop 上限不一致）。
DEFAULT_MAX_TURNS = 200


@dataclass
class AgentConfig:
    """GenericAgent 子系统配置。"""

    enabled: bool = False

    # LLM 配置（核心）
    api_key: str = ""
    model: str = ""

    # 关联 agent_llm_configs.id（旧：agent 服务专用 LLM 配置）。已停用，保留仅为
    # 兼容历史数据/序列化，新链路不再读取。LLM 走网关秘钥（下方 *_api_key_id）。
    llm_config_id: int | None = None

    # Subagent 默认 LLM（可选覆盖）——旧字段，保留兼容。
    subagent_api_key: str = ""
    subagent_model: str = ""

    # ── 网关秘钥绑定（新）：agent LLM 走本系统网关 dispatch_entry，形成计费/限流/归属闭环 ──
    # 主 Agent：绑定的网关 api_keys.id + 模型名（模型必须是网关 providers 提供的）。
    main_api_key_id: int | None = None
    main_model: str = ""
    # 子 Agent：空 api_key_id 表示跟随主 Agent（同 key，可另选模型）。
    subagent_api_key_id: int | None = None
    # 子 Agent 岗位/难度 → 模型映射（OMO 两张表思想，粒度自定）：
    #   {"roles": {"explore"|"verify"|"build"|"general": "<model id>"},
    #    "categories": {"quick"|"deep"|"ultrabrain": "<model id>"}}
    # 值全部是网关模型 id 字符串；空值/缺键 = 未配 → 逐层回落（见
    # GenericAgent._resolve_subagent_model）。key 仍沿用 subagent_api_key_id/主 key
    # （fork 只能换模型换不了 key），所以这里只存模型名、不存 key。
    subagent_model_map: dict = field(default_factory=dict)
    # 定时任务默认绑定：定时态是无人值守场景，通常想用更便宜/更稳的模型跑，
    # 与交互态主 Agent 分开。两者都空 → 定时任务回退到主 Agent 的 key/模型。
    # 任务行上的 api_key_id/model 优先于这里的 Agent 级默认。
    scheduled_api_key_id: int | None = None
    scheduled_model: str = ""

    # 运行控制
    max_turns: int = DEFAULT_MAX_TURNS
    max_concurrent_tasks: int = 5

    # 功能开关
    memory_enabled: bool = True
    skill_auto_learn: bool = True
    browser_enabled: bool = False
    subagent_enabled: bool = True
    # scheduler_enabled 已删：原主配置总开关没有任何 UI 入口、默认 false，
    # 只会让定时任务静默失效。调度闸门 = 任一 Agent 的 scheduler_enabled（agents 表）
    # 或库里有 enabled 的定时任务（agent/runner.scheduler_wanted）。from_dict 对
    # 历史配置里残留的该键按未知字段忽略，无需迁移。
    # 思考模式（agent 级策略；注入 agent LLM 请求体，独立于主系统）
    thinking_enabled: bool = False
    reasoning_effort: str = ""

    # agent 层 429 自动重试次数：仅对瞬时可重试业务码（rate_limit_exceeded/
    # service_busy/concurrent_limit_exceeded 等）做带退避重试；配置类
    # no_available_account（白名单/禁用）不重试。0=关闭。注入 GatewayLLMBridge.max_retries。
    llm_retry_429: int = 2

    # 需确认的工具（code_run / node_shell_exec）挂起等人裁决的上限，单位秒。
    # 超时中止本轮而非自动拒绝后继续下一轮。0 = 不过期，一直等到有人裁决。
    approval_timeout_seconds: int = 24 * 60 * 60

    # ── 上下文压缩 ──
    # auto_compact：每轮 LLM 调用前是否自动压缩历史（compress_history +
    #   trim_messages_async 截断+摘要）。默认开 = 历史行为；关掉则只在收到手动
    #   /compact 或用户点压缩按钮时压缩。由 agent_loop 读取。
    # compact_threshold：上下文占用比例阈值（0~1），前端 token 卡/用量环占比达到
    #   此值时主动弹框提醒压缩（纯展示提醒，不改后端压缩时机）。
    auto_compact: bool = True
    compact_threshold: float = 0.8

    # 浏览器（可选）
    cdp_bridge_mode: str = "stdio"
    cdp_bridge_token: str = ""

    # 绑定的 MCP 用户（cdp-bridge 客户端）id；空表示未绑定。
    # 绑定后：agent 按该 principal 的 params/服务授权挂载 MCP 并调用。
    mcp_user_id: int | None = None

    # 安全
    workspace_root: str = "agent/workspace"
    allowed_roots: list[str] = field(default_factory=lambda: ["agent/workspace"])
    denied_patterns: list[str] = field(
        default_factory=lambda: [
            r"^/etc/",
            r"^/var/",
            r"config\.json$",
            r"db\.py$",
            r"\.env$",
            r"\.git/",
            r"node_modules/",
        ]
    )

    @classmethod
    def from_dict(cls, data: dict) -> "AgentConfig":
        """从 dict 加载配置，忽略未知字段"""
        if not isinstance(data, dict):
            return cls()
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid_keys})

    def to_dict(self) -> dict:
        """导出为 dict"""
        return {
            "enabled": self.enabled,
            "api_key": self.api_key,
            "model": self.model,
            "llm_config_id": self.llm_config_id,
            "subagent_api_key": self.subagent_api_key,
            "subagent_model": self.subagent_model,
            "main_api_key_id": self.main_api_key_id,
            "main_model": self.main_model,
            "subagent_api_key_id": self.subagent_api_key_id,
            "subagent_model_map": self.subagent_model_map,
            "scheduled_api_key_id": self.scheduled_api_key_id,
            "scheduled_model": self.scheduled_model,
            "max_turns": self.max_turns,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "memory_enabled": self.memory_enabled,
            "skill_auto_learn": self.skill_auto_learn,
            "browser_enabled": self.browser_enabled,
            "subagent_enabled": self.subagent_enabled,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_effort": self.reasoning_effort,
            "llm_retry_429": self.llm_retry_429,
            "auto_compact": self.auto_compact,
            "compact_threshold": self.compact_threshold,
            "cdp_bridge_mode": self.cdp_bridge_mode,
            "cdp_bridge_token": self.cdp_bridge_token,
            "mcp_user_id": self.mcp_user_id,
            "workspace_root": self.workspace_root,
            "allowed_roots": self.allowed_roots,
            "denied_patterns": self.denied_patterns,
        }
