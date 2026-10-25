"""GenericAgent — the top-level agent orchestrator.

Supports two modes:
1. Config-based: pass AgentConfig directly (legacy, for internal use)
2. DB-based: pass agent_id, config loaded from agents table (multi-tenant)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any

from agent.agent_loop import BaseHandler, agent_runner_loop
from agent.config import AgentConfig, DEFAULT_MAX_TURNS
from agent.llm_bridge import GatewayLLMBridge
from agent.tools import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

# Cap on an inlined scene SOP body. Scene services are few (usually one), but a
# service with many methods can produce a long schema dump; the pointer form is
# the fallback for anything larger.
_MAX_SCENE_SOP_CHARS = 6000

# browser_sop.md is the ONE SOP every browser-scene conversation needs on the first
# turn (open-page policy, click sequences, network capture), so it is inlined whole
# instead of behind a file_read pointer. Its body (~11KB) exceeds the generic scene
# cap, so it gets its own, larger one — still bounded so a runaway edit cannot bloat
# every browser turn's prompt.
_MAX_BROWSER_SOP_CHARS = 14000

# Built-in tool capabilities a scene can declare, mapped to the seeded SOP file that
# documents how to use them. A capability that is a built-in tool is already a
# first-class tool the agent calls directly, so nothing is promoted for it — but its
# SOP is inlined into the scene segment so "how to use it" needs no file_read.
# Keyed by the name the agent calls the tool by (see SceneSpec.capabilities).
_BUILTIN_CAPABILITY_SOPS: dict[str, str] = {
    "web": "browser_sop.md",
}

# ── Sub-agent roles & categories (OMO 两张表思想，粒度自定) ──────────────────────
# The parent picks WHO (role) and HOW HARD (category) for a delegated task; the
# server maps each to a gateway model id via config.subagent_model_map, so the
# model list never enters the context. A role also drives the child's persona (see
# _ROLE_PERSONAS) — the delegation is a role, not just a cheaper model.
_SUBAGENT_ROLES = ("explore", "verify", "build", "general")
_SUBAGENT_CATEGORIES = ("quick", "deep", "ultrabrain")

# English personas injected as the child's system prompt, one per role. '' = no
# specialization (general). Independent of model resolution: a role always sets the
# persona even when its model is unset in the map (then the model falls back).
_ROLE_PERSONAS: dict[str, str] = {
    "explore": (
        "You are an explorer. Search, locate and read only — do not modify anything. "
        "Report findings as evidence: file paths, file:line, exact quoted values. "
        "Be fast; do not over-scope beyond the task you were handed."
    ),
    "verify": (
        "You are an independent reviewer. Assume nothing you were told is correct — "
        "re-derive it from evidence. Output a verdict PASS / FAIL / PARTIAL with the "
        "specific checks behind it (files read, commands run, values confirmed). "
        "Call out anything that would make the claimer wrong."
    ),
    "build": (
        "You are an implementer. Make the smallest correct change that satisfies the "
        "task, verify it (run it, check output), and report what you changed with "
        "paths. Stay inside the task's blast radius."
    ),
    "general": "",
}


def _normalize_subagent_model_map(raw: Any) -> dict:
    """Converge a stored/submitted subagent_model_map to ``{roles, categories}``.

    Tolerates asyncpg returning JSONB as text (str) or as dict, drops unknown keys,
    non-canonical role/category names and empty model values, so the runtime lookup
    only ever sees well-formed entries. Missing/blank entries mean 'not configured'
    and fall through to the next layer.
    """
    import json

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        return {"roles": {}, "categories": {}}

    def _sub(name: str, allowed: tuple[str, ...]) -> dict[str, str]:
        block = raw.get(name)
        if not isinstance(block, dict):
            return {}
        return {
            k: v.strip()
            for k, v in block.items()
            if k in allowed and isinstance(v, str) and v.strip()
        }

    return {
        "roles": _sub("roles", _SUBAGENT_ROLES),
        "categories": _sub("categories", _SUBAGENT_CATEGORIES),
    }


# The "## New tab vs. in-page navigation" section of browser_sop.md, dropped from the
# inlined body when the scene already binds one page (capability ``web`` + a tab_id in
# identity). Its advice — start a new task in a new tab — is for a scene-less agent
# that has no bound page; a scene-bound browser agent must operate the page the user
# pointed at. The section stays in the file so the scene-less agent's file_read path
# still delivers it; only the inline copy is trimmed. Matching is on the exact heading.
_NEW_TAB_SECTION = "## New tab vs. in-page navigation"


def _drop_section(body: str, heading: str) -> str:
    """Remove one Markdown section (``heading`` up to the next ``## ``) from a body.

    Returns ``body`` unchanged when the heading is absent, so a SOP without the
    section degrades to "inline it whole" rather than crashing.
    """
    lines = body.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
    if start is None:
        return body
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    kept = lines[:start] + lines[end:]
    return "\n".join(kept).strip()

# Cap on the one-line method description in the capability index. Cutting at a
# raw character offset severs words mid-token ("plus th", "URLs, and ti"), which
# reads as corrupted text rather than an abbreviation, so trim at a word
# boundary and mark the elision.
_MAX_METHOD_DESC_CHARS = 60


def _short_desc(description: object) -> str:
    """First line of a method description, trimmed at a word boundary."""
    text = str(description or "").strip()
    if not text:
        return ""
    text = text.splitlines()[0].strip()
    if len(text) <= _MAX_METHOD_DESC_CHARS:
        return text
    head = text[:_MAX_METHOD_DESC_CHARS]
    cut = head.rfind(" ")
    # Keep the hard cut for scripts without spaces (Chinese descriptions).
    return (head[:cut] if cut > _MAX_METHOD_DESC_CHARS // 2 else head).rstrip(" ,;:.") + "…"


def _runtime_environment_segment() -> str:
    """Stable host facts for the system prompt: OS, arch, Python, default shell.

    ``code_run`` gained a ``shell`` type that runs through the host's native shell
    (``/bin/sh`` on POSIX, ``cmd`` on Windows); without telling the model which host
    it is on, it either guesses the command syntax wrong or burns a turn probing
    (``uname`` / ``platform.system()``). These facts never change for the process, so
    they belong in the system prompt's stable section, not in a per-turn message.
    """
    import platform

    system = platform.system() or ""
    os_label = {
        "Windows": "Windows",
        "Linux": "Linux",
        "Darwin": "macOS",
    }.get(system, system or "unknown")
    shell = "cmd.exe" if system == "Windows" else "/bin/sh"
    py = platform.python_version()
    machine = platform.machine() or ""
    bits = [f"OS: {os_label}"]
    if machine:
        bits.append(machine)
    return (
        "[Runtime Environment]\n"
        f"- {' '.join(bits)}\n"
        f"- Python: {py}\n"
        f"- code_run default shell: {shell}"
    )


def _scene_cdp_client_ids(scene) -> tuple[str, ...]:
    """Return the CDP client ids this run may operate (browser scene = one bound
    client), else empty.

    A browser scene is bound to exactly one browser (its ``client_id``); the
    cdp-bridge plugin treats this as a hard boundary — the model may only pick
    that client, never another. An empty tuple means "not scene-bound": a plain
    agent / node / marketplace turn leaves the choice to the plugin's identity
    grant-list derivation, so a multi-browser agent still sees and selects within
    its authorised set.
    """
    if scene is None:
        return ()
    identity = getattr(scene, "identity", None) or {}
    client_id = str(identity.get("client_id") or "").strip()
    return (client_id,) if client_id else ()


def _scene_cdp_session_key(scene) -> str:
    """Return ``client_id:tab_id`` from a resolved CDP scene, else empty.

    The tab_id in the scene identity is stable within a tab (survives full-page
    navigation), so it is the right key for a tab lease. Empty when there is no
    CDP scene (plain agent / node terminal / marketplace admin).
    """
    if scene is None:
        return ""
    identity = getattr(scene, "identity", None) or {}
    client_id = str(identity.get("client_id") or "").strip()
    tab_id = str(identity.get("tab_id") or "").strip()
    if not client_id or not tab_id:
        return ""
    return f"{client_id}:{tab_id}"


@dataclass(slots=True)
class SubagentHandle:
    task: str
    llm: GatewayLLMBridge
    asyncio_task: asyncio.Task
    # 子 agent 的稳定 id（= _run_subagent_task 的 handle_id，也是 request_logs.subagent_id
    # 和 WS subagent/<id> 频道的 target）。kill/list 动作按它定位子任务。
    subagent_id: str = ""
    handle_name: str = ""
    # 岗位（explore/verify/build/general）；空=不特化。既驱动模型查表，也驱动子
    # agent 的人格（见 _ROLE_PERSONAS），供展示/审计。
    role: str = ""


class _AgentHandler(BaseHandler):
    def __init__(self, tools_registry: ToolRegistry, max_turns: int) -> None:
        super().__init__(tools_registry=tools_registry, max_turns=max_turns)


class GenericAgent:
    def __init__(
        self,
        config: AgentConfig | None = None,
        agent_id: int | None = None,
        llm: GatewayLLMBridge | None = None,
        tools: ToolRegistry | None = None,
        run_as_caller: str | None = None,
        scene=None,
        model_override: str = "",
        conversation_id: str | None = None,
        *,
        parent_subagent_id: str | None = None,
        is_subagent: bool = False,
        mcp_manager=None,
        capability_index: list[dict] | None = None,
        browser_service: str | None = None,
        agent_row: dict | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._llm = llm
        self._tools = tools
        # 配置注入（Phase 2.1）：入口已按 agent_id 读过 agents 行时直接传进来，
        # _ensure_config 用它派生 config，不再每轮重复 SELECT。None = 走 DB 懒加载。
        self._agent_row = agent_row
        # 会话血缘：本 Agent 运行时所属的会话 id（CDP 网页对话传 conv_id，用户端
        # Agent 聊天传同一 conv_id）。透传给 GatewayLLMBridge → request_logs.session_id，
        # 让请求日志页能按会话把一次对话的所有上游请求串起来。与 editor 路径的
        # editor_session_id 同一套模式；不传则日志 session 列为空。
        self._conversation_id = (conversation_id or "").strip()
        # 单次运行级模型覆盖（空=用 Agent 自己绑定的模型）。复用 Agent 绑定的网关
        # key 只换模型——与定时任务「同 key 换模型」同一口径；供批量识别这类
        # 「指定 agent + 指定模型」的调用方使用。
        self._model_override = model_override or ""
        # Resolved SceneSpec (agent/scene_context.py) or None. Drives which
        # capabilities get their full SOP inlined into the first system prompt
        # instead of an (SOP: ...) pointer the model must read first. ``capabilities``
        # names tools/MCP services **as the agent calls them**; the resolution loop
        # below treats a built-in tool name (e.g. ``web``) and an MCP service name
        # (e.g. ``marketplace-status``) through one path — no scene type is special.
        self._scene = scene
        self.sub_agents: list[SubagentHandle] = []
        # 父级事件出口：子 agent spawn 时从这里取 on_event，把子 agent 的
        # content/tool 事件透传回主对话 SSE 流（详见 spawn_subagent_async）。
        self._event_sink = None
        # 子 agent 自增序号，仅用于事件聚合与展示，非持久化。
        self._subagent_seq = 0
        # MCP 工具仅需要附加一次；子 agent 复用同一 ToolRegistry 时保持幂等。
        self._mcp_attached = False
        # MCP manager：自建时由 _attach_mcp_tools 创建并 close()；子 agent 由父
        # 注入（共享父的 MCP 连接，不重连），子不自建也不 close（_owns_mcp_manager
        # = False），否则子的 run_task finally 会把父的连接一起关掉。
        self._mcp_manager = mcp_manager
        self._owns_mcp_manager = mcp_manager is None
        # Capability index produced by _attach_mcp_tools: list of
        # {service, method_names, methods, browser, sop_ref}. Advertised in the
        # system prompt; MCP methods are invoked via capability_call, not as
        # first-class tools.
        self._capability_index: list[dict] = list(capability_index or [])
        # Dynamically-detected browser-class MCP service name (or None).
        self._browser_service: str | None = browser_service
        # 子 agent 标记：registry 不注册 spawn_subagent / start_long_term_update，
        # 隔离其额度，便于权限分档（见 _run_subagent_task）。
        self._is_subagent = bool(is_subagent)
        # 父→子血缘：子 agent 的上游 LLM 请求带这个 id 落 request_logs.subagent_id，
        # 让父子成本可分开归因（session_id 仍是同一 conv_id）。空 = 主 agent。
        self._parent_subagent_id = (parent_subagent_id or "").strip() or None
        # Agent 归属 user_id（_ensure_config 时从 DB 填充，仅供展示/日志）。
        self._agent_owner: str | None = None
        # Agent 自身的 system_prompt（_ensure_config 时从 DB 填充）。AgentConfig 不含
        # 该字段，历史上只有对话链路会从 agents 行读出来显式传给 run_task；无人值守
        # 入口（定时任务、自愈诊断）不传，于是那些场景跑的是空系统提示，Agent 人设
        # 整段失效。这里缓存一份供 run_task 兜底，让「不传」等于「用 Agent 自己的」。
        self._agent_system_prompt: str = ""
        # 运行身份：解析网关 api_key 时按「当前发起请求的用户」校验权限，而非 Agent
        # owner。这样平台 Agent（owner=NULL）对所有用户可见，但每个用户运行时用自己的
        # 身份校验绑定 key —— 无权访问该 key 就发送失败。None = 管理员上下文（全量查找）。
        self._run_as_caller: str | None = run_as_caller

        if config is not None:
            self.config = config
        elif agent_id is not None:
            self.config = None  # Lazy load from DB
        else:
            self.config = AgentConfig()

        if self.config is not None:
            # LLM 延迟解析：运行时按绑定的网关 API Key + 模型构造桥。
            if self._tools is None:
                self._tools = ToolRegistry(ToolContext(self.config), agent_id=self.agent_id)

    async def _ensure_config(self) -> AgentConfig:
        if self.config is not None:
            return self.config
        if self.agent_id is None:
            self.config = AgentConfig()
            return self.config

        # 配置注入路径（Phase 2.1）：入口已按 agent_id 读过 agents 行，直接用它，
        # 不再每轮重复 SELECT。owner/system_prompt/workspace 派生统一走
        # _build_config_from_row，与 DB 路径同一实现，不会漂移。
        if self._agent_row is not None:
            self._apply_agent_row(dict(self._agent_row))
            return self.config

        from db import PostgresClient
        if not PostgresClient.pool:
            self.config = AgentConfig()
            return self.config

        async with PostgresClient.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agents WHERE id=$1", self.agent_id)
        if not row:
            logger.warning("Agent %d not found, using default config", self.agent_id)
            self.config = AgentConfig()
            return self.config

        self._apply_agent_row(dict(row))
        return self.config

    def _apply_agent_row(self, d: dict) -> None:
        """由 agents 行 dict 填充 config + owner + system_prompt（注入/DB 两路共用）。"""
        # 记录 owner，供网关 key 归属校验用（NULL=平台 Agent，走管理员全量查找分支）。
        self._agent_owner = d.get("user_id")
        # system_prompt 不是 AgentConfig 字段（它属于 Agent 行/会话行）。这里留一份，
        # 供 run_task 在调用方没传时兜底 —— 定时任务等无人值守入口没有会话可读。
        self._agent_system_prompt = (d.get("system_prompt") or "").strip()
        # GA-style real working directory: every Agent owns
        # ``data/agents/<agent_id>/`` with ``memory/`` and ``workspace/`` as real
        # subdirectories. Only honour an explicit non-legacy workspace_root from
        # the DB (future node binding); otherwise default to the per-Agent dir so
        # file_read reaches the Agent's real memory/ and workspace/ files.
        workspace_root = (d.get("workspace_root") or "").strip()
        legacy_default = workspace_root in {"", "agent/workspace"}
        if legacy_default:
            from agent.file_memory import agent_root
            workspace_root = str(agent_root(self.agent_id))
            allowed_roots = [workspace_root]
        else:
            allowed_roots = d.get("allowed_roots") or [workspace_root]
        self.config = AgentConfig.from_dict({
            "api_key": d.get("api_key", ""),
            "model": d.get("model", ""),
            "llm_config_id": d.get("llm_config_id"),
            # 网关秘钥绑定（新链路）：主/子 Agent 各自的网关 key + 模型名。
            "main_api_key_id": d.get("main_api_key_id"),
            "main_model": d.get("main_model", ""),
            "subagent_api_key_id": d.get("subagent_api_key_id"),
            "subagent_model": d.get("subagent_model", ""),
            "subagent_model_map": _normalize_subagent_model_map(d.get("subagent_model_map")),
            # 定时任务默认绑定（无人值守场景专用；空则回退主 Agent）。
            "scheduled_api_key_id": d.get("scheduled_api_key_id"),
            "scheduled_model": d.get("scheduled_model", ""),
            # Agent 行没配（NULL）→ 固定默认（单一真相源，见 agent.config）。
            "max_turns": d.get("max_turns") or DEFAULT_MAX_TURNS,
            "memory_enabled": d.get("memory_enabled", True),
            "skill_auto_learn": d.get("skill_auto_learn", True),
            "workspace_root": workspace_root,
            "allowed_roots": allowed_roots,
            "denied_patterns": d.get("denied_patterns", ["/etc/", "/var/", ".env"]),
            "thinking_enabled": d.get("thinking_enabled", False),
            "reasoning_effort": d.get("reasoning_effort", ""),
            "llm_retry_429": d.get("llm_retry_429", 2),
            "approval_timeout_seconds": d.get("approval_timeout_seconds", 24 * 60 * 60),
            "auto_compact": d.get("auto_compact", True),
            "compact_threshold": d.get("compact_threshold", 0.8),
            "mcp_user_id": d.get("mcp_user_id"),
        })
        # API callers may provision a registry before lazy DB config is loaded
        # (node shell registration needs ``agent._tools`` early). Rebind that
        # registry to the resolved per-Agent context; otherwise file_read would
        # silently keep the legacy shared ``agent/workspace`` root.
        if self._tools is not None:
            self._tools.context = ToolContext(self.config)

    async def _ensure_llm(self) -> "GatewayLLMBridge":
        if self._llm is not None:
            return self._llm
        await self._ensure_config()
        self._llm = await self._resolve_gateway_bridge(
            self.config.main_api_key_id,
            self._model_override or self.config.main_model,
            role="main_agent",
        )
        return self._llm

    async def _resolve_gateway_bridge(
        self, api_key_id: int | None, model: str, *, role: str
    ) -> "GatewayLLMBridge":
        """按（网关 api_key_id + 模型名）构造走网关的 LLM 桥。

        - api_key 归属校验按「当前运行用户」（_run_as_caller），而非 Agent owner：
          平台 Agent（owner=NULL）对所有用户可见，但每个用户运行时用自己的身份校验
          绑定 key —— 无权访问该 key（不在自己名下、也不在所在分组授权内）就发送失败。
          _run_as_caller=None 表示管理员上下文，走全量查找。复用 agent.api 的解析器。
        - 无绑定 / 无权 / 已禁用 / 未选模型 → 抛 RuntimeError（Agent 使用失败），
          满足"没有网关 key 权限则不可用/使用失败"的诉求。
        - provider 白/黑名单由该 key 的配置决定（与聊天发送链路口径一致）。
        """
        from agent.llm_bridge import GatewayLLMBridge
        from agent.api import _resolve_caller_api_key
        import config as _config_mod
        from fastapi import HTTPException

        if not api_key_id:
            raise RuntimeError(
                f"Agent 未绑定网关 API Key（role={role}），"
                "请在 Agent 编辑器的「模型」中选择秘钥与模型"
            )
        if not model:
            raise RuntimeError(
                f"Agent 未选择 role={role} 的模型，请在「模型」中选择模型"
            )
        try:
            key_row = await _resolve_caller_api_key(api_key_id, self._run_as_caller)
        except HTTPException as exc:
            # 无权/不存在/已禁用 → 转成 agent 层可读错误（Agent 使用失败）。
            raise RuntimeError(f"Agent 绑定的网关 API Key 不可用：{exc.detail}") from exc
        api_key = key_row["key"]
        api_key_name = key_row.get("name") or ""
        cfg = self.config or AgentConfig()
        provider_whitelist, provider_blacklist = await _config_mod.Config.get_api_key_provider_filter(api_key)
        return GatewayLLMBridge(
            api_key=api_key,
            api_key_name=api_key_name,
            model=model,
            provider_whitelist=provider_whitelist,
            provider_blacklist=provider_blacklist,
            thinking_enabled=cfg.thinking_enabled,
            reasoning_effort=cfg.reasoning_effort,
            # 会话血缘：主/子/定时三种桥都在这里构造，统一带上本 Agent 所属会话 id
            # → request_logs.session_id，请求日志页可据此串起一次对话的全部上游请求。
            session_id=self._conversation_id or None,
            # 子 agent 血缘：子 agent 自建桥（llm=None 路径）时也带上 subagent_id，
            # 让其上游请求在 request_logs 里可与父分开归因。主 agent 该值为 None。
            subagent_id=self._parent_subagent_id,
            max_retries=max(0, int(cfg.llm_retry_429 or 0)),
        )

    async def resolve_scheduled_llm(
        self, api_key_id: int | None = None, model: str = "",
    ) -> "GatewayLLMBridge":
        """定时任务用的 LLM 桥。优先级：任务级 > Agent 的 scheduled_* > 主 Agent。

        每一层各字段独立回退，而不是整层二选一：只给模型（不给 key）时复用上一层的
        key 换模型，这是最常见的诉求（同一把 key 下换个便宜模型跑定时）。

        与 _resolve_subagent_llm 分开：子 agent 是「主 agent 派生的并行子任务」，
        定时任务是「无人值守的独立触发」，两者该配的模型往往不同，混用一个字段会
        让其中一方被迫将就。
        """
        config = self.config or await self._ensure_config()
        # 任务级 > Agent 级；key 与模型各自回退，允许只覆盖其中一个。
        eff_key = api_key_id or config.scheduled_api_key_id or config.main_api_key_id
        eff_model = (model or "").strip() or config.scheduled_model or config.main_model
        return await self._resolve_gateway_bridge(eff_key, eff_model, role="scheduled")

    async def _resolve_subagent_llm(self) -> "GatewayLLMBridge | None":
        """子 agent 网关桥解析；未绑定专用 key/模型则返回 None，由调用方 fallback 到主桥。"""
        config = self.config or await self._ensure_config()
        if not config.subagent_api_key_id and not config.subagent_model:
            return None
        # 子 Agent 有独立 key → 用它；仅有独立模型 → 复用主 key 换模型（在 spawn 侧 fork 处理）。
        if config.subagent_api_key_id:
            return await self._resolve_gateway_bridge(
                config.subagent_api_key_id,
                config.subagent_model or config.main_model,
                role="subagent",
            )
        return None

    async def _resolve_subagent_model(
        self, role: str | None, category: str | None
    ) -> str | None:
        """按「难度 > 岗位」查 config.subagent_model_map，返回网关模型名或 None。

        纯查表（config 已在内存，无 DB I/O）。miss 返回 None，由调用方继续回落到
        subagent_model / 跟随主桥——这是「不配映射时行为逐字不变」的支点。
        """
        config = self.config or await self._ensure_config()
        m = _normalize_subagent_model_map(getattr(config, "subagent_model_map", None))
        if category:
            hit = m["categories"].get(category)
            if hit:
                return hit
        if role:
            hit = m["roles"].get(role)
            if hit:
                return hit
        return None

    async def _ensure_tools(self) -> ToolRegistry:
        if self._tools is None:
            config = await self._ensure_config()
            # is_subagent: registry 不注册 spawn_subagent / start_long_term_update
            # （子禁用此二工具），其余 8 个工具与主 agent 同形。
            self._tools = ToolRegistry(
                ToolContext(config),
                agent_id=self.agent_id,
                is_subagent=self._is_subagent,
            )
        # MCP 工具挂载与 ToolRegistry 是否预置无关：api.py 会预塞内置 registry，
        # 这里仍需把 agent 的有效 MCP 服务注册进去。挂一次即可（幂等标记）。
        if not self._mcp_attached:
            await self._attach_mcp_tools(self._tools)
            self._mcp_attached = True
        # 主 agent 注册 spawn_subagent 工具（子 agent 的 registry 已在构造时跳过）。
        if not self._is_subagent:
            from agent.tools import _SPAWN_SUBAGENT_SCHEMA
            reg = getattr(self._tools, "tools", None)
            if isinstance(reg, dict) and "spawn_subagent" not in reg:
                self._tools.register("spawn_subagent", self._spawn_subagent_tool, _SPAWN_SUBAGENT_SCHEMA)
        return self._tools

    async def _ensure_resources(self) -> tuple[ToolRegistry, str]:
        """Prepare tools plus GA's always-on memory context.

        GenericAgent L1/L2/L3 are Agent-private files under data/agents/<id>/.
        MCP services and the scheduler are advertised in the system prompt via a
        short capability index — they are NOT first-class tools. SOP bodies are
        read on-demand with file_read("memory/sop/...").
        """
        tools = await self._ensure_tools()
        if self.agent_id is None:
            return tools, ""
        try:
            from agent.file_memory import ensure_agent_memory, read_l1
            from agent.meta_memory import META_MEMORY_SUMMARY

            ensure_agent_memory(self.agent_id)
            try:
                from agent.sop_service import sync_agent_sops
                await sync_agent_sops(self.agent_id)
            except Exception as exc:  # noqa: BLE001 - database-backed selection is optional during startup
                logger.debug("[sop] binding sync unavailable agent=%s: %s", self.agent_id, exc)
            index = read_l1(self.agent_id).strip()
            prefix = META_MEMORY_SUMMARY
            # 宿主运行时事实：code_run 的 shell 类型要按宿主 OS 选解释器（/bin/sh vs cmd），
            # 没有这一行模型只能先花一轮跑 uname/platform.system() 猜，或直接猜错。
            # 稳定不变，进 system 一次即可（与 L1 同档），不进每轮消息。
            prefix = f"{prefix}\n\n{_runtime_environment_segment()}"
            if index:
                prefix = f"{prefix}\n\n[L1 Memory Index]\n{index}"
            # Self-improvement log (GA ch12.3.3): always-on cross-task lessons.
            try:
                from agent.self_improvement_log import system_prompt_segment
                segment = system_prompt_segment(self.agent_id)
                if segment:
                    prefix = f"{prefix}\n\n{segment}"
            except Exception as exc:  # noqa: BLE001 — best-effort injection
                logger.debug("[memory] self-improvement log unavailable agent=%s: %s", self.agent_id, exc)
            # Capability index produced by _attach_mcp_tools, split into the
            # scene's own capabilities and everything else the Agent happens to
            # have mounted.
            #
            # ``scene.capabilities`` names each capability **as the agent calls
            # it**: a built-in tool name (the cdp-bridge panel's ``web``) or an
            # MCP service name (``marketplace-status``). Resolution per name:
            #
            # - built-in tool → no promotion (it is already a first-class tool
            #   the agent calls directly); inline its seeded SOP instead, so
            #   "how to use it" is on hand from turn one.
            # - MCP service → promote: full method schema inlined first, right
            #   after the scene intro, and dropped from the pointer index below.
            #
            # A name matching neither is silently skipped (never advertised
            # pointing at a capability_call that would fail).
            capability_index = self._capability_index or []
            scene_caps = set(getattr(self._scene, "capabilities", ()) or ())
            scene_lines: list[str] = []
            scene_blocks: list[str] = []
            other_lines: list[str] = []
            inlined: set[str] = set()
            for entry in capability_index:
                # Browser-class services stay in the index (the `web` tool routes
                # through it) but are never advertised: every method the model
                # could call is reachable as a `web` operation, and a second
                # capability_call spelling of the same thing only teaches the
                # model two inconsistent ways to do one job.
                if entry.get("browser"):
                    continue
                # Method names alone are not enough to route on — the model cannot
                # tell what ``browser_scan`` vs ``browser_execute_js`` want without a
                # one-line description. method_meta carries it; surface it here too.
                method_meta = entry.get("methods") or []
                if method_meta:
                    methods_str = "; ".join(
                        f"{m.get('name')}" + (f": {desc}" if (desc := _short_desc(m.get("description"))) else "")
                        for m in method_meta
                    )
                else:
                    methods_str = ", ".join(entry.get("method_names") or []) or "(no methods)"
                sop = entry.get("sop_ref") or ""
                service_name = str(entry["service"])
                if service_name in scene_caps:
                    # This is the scene's own MCP service: the model will use it
                    # on the first turn, so a pointer it has to file_read first
                    # is pure latency. Inline the body and drop the pointer.
                    inline = self._read_sop_body(sop)
                    if inline:
                        scene_lines.append(f"- {service_name}: {methods_str}")
                        scene_blocks.append(f"[Scene Capability: {service_name}]\n{inline}")
                        inlined.add(service_name)
                        continue
                tail = f" (SOP: {sop})" if sop else ""
                other_lines.append(f"- {service_name}: {methods_str}{tail}")
            # 内置工具能力：工具本身就是一等公民（每轮 schema 里都在），无需提权；
            # 把「怎么用」的种子 SOP 内联进场景段，省掉一次 file_read。能力名不在
            # 本表也不在 MCP 索引里 → 静默跳过。
            for cap in scene_caps - inlined:
                sop_file = _BUILTIN_CAPABILITY_SOPS.get(cap)
                if not sop_file:
                    continue
                body = await self._read_seeded_sop(sop_file)
                if not body:
                    continue
                # 在场景工具行里顺带列出该内置工具的操作名（有则给，没有也不强求）。
                ops = ""
                schema = getattr(self._tools, "_schemas", None) or {}
                schema = schema.get(cap) if isinstance(schema, dict) else None
                if isinstance(schema, dict):
                    function = schema.get("function")
                    if isinstance(function, dict):
                        params = function.get("parameters") or {}
                        enum = params.get("properties", {}).get("operation", {}).get("enum")
                        if isinstance(enum, list):
                            ops = "|".join(str(e) for e in enum)
                scene_lines.append(f"- {cap}" + (f": {ops}" if ops else "") + " (SOP below)")
                # 场景绑定了唯一页面（能力声明了 web，identity 里带着 tab_id），
                # SOP 的「新任务开新 tab」策略就不该一起内联：照做会把工作搬到
                # 用户没在看的新页面上。策略整节留在文件里——无场景的 agent 走
                # L1 指针读全文时它照常生效；剪裁判断在这里做，文件里不写判断。
                body = _drop_section(body, _NEW_TAB_SECTION)
                scene_blocks.append(f"[Scene Capability: {cap}]\n{body}")
                inlined.add(cap)
            other_lines.append("- workflow: list|get|run|runs|run_status|cancel|create|update|delete (create/update/delete manage the graph and need a one-time approval card; runs/run_status/cancel drive an existing run)")

            head = ""
            if scene_lines:
                head = "[Scene Tools]\n" + "\n".join(scene_lines)
                for block in scene_blocks:
                    head = f"{head}\n\n{block}"
            # Heading stays [Available Capabilities]: the constitution, the
            # capability_call error text, mcp_usage_sop / web_setup_sop and the
            # Agent admin page all point the model at that exact name. When a scene
            # service was hoisted out of this index, say so — otherwise the SOP's
            # promise that it "lists every bound MCP service" reads as a gap.
            cap_heading = "[Available Capabilities]"
            if scene_lines:
                cap_heading += "\n（本场景服务已在上方 [Scene Tools] / [Scene Capability] 完整给出，此处不重复）"
            body = f"{prefix}\n\n{cap_heading}\n" + "\n".join(other_lines)
            return tools, f"{head}\n\n{body}".strip() if head else body
        except Exception as exc:  # noqa: BLE001 - memory initialization must not hide the Agent itself
            logger.warning("[memory] initialize resources failed agent=%s: %s", self.agent_id, exc)
            return tools, ""

    def _read_sop_body(self, sop_ref: str) -> str:
        """Read a seeded MCP SOP body for inlining into the system prompt.

        ``sop_ref`` is the pointer form advertised in L1 (``memory/sop/mcp/x_sop.md``),
        resolved against the Agent root. Returns "" when the file is missing or the
        Agent has no id, so the caller falls back to the pointer form.
        """
        if self.agent_id is None or not sop_ref:
            return ""
        try:
            from agent.file_memory import agent_root

            path = agent_root(self.agent_id) / str(sop_ref).replace("\\", "/")
            if not path.is_file():
                return ""
            return path.read_text(encoding="utf-8", errors="replace").strip()[:_MAX_SCENE_SOP_CHARS]
        except OSError as exc:
            logger.debug("[scene] read sop body failed ref=%s: %s", sop_ref, exc)
            return ""

    async def _read_seeded_sop(self, filename: str) -> str:
        """Read a built-in capability SOP whole (not the generic 6000-cap).

        Used for built-in tool capabilities whose SOP is longer than the generic
        scene cap (browser_sop.md ~11KB). Falls back to the source copy in
        ``agent/sop/`` when the seeded agent copy has not landed yet (first run of
        a fresh agent); both paths resolve to the same authoritative text.
        """
        if self.agent_id is None:
            return ""
        try:
            from agent.file_memory import agent_sop_root

            path = agent_sop_root(self.agent_id) / filename
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace").strip()[:_MAX_BROWSER_SOP_CHARS]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[sop] read seeded %s failed: %s", filename, exc)
        # Fallback: the source copy shipped with the repo (works on first run).
        try:
            from agent.file_memory import _BUILTIN_SOP_ROOT

            src = _BUILTIN_SOP_ROOT / filename
            if src.is_file():
                return src.read_text(encoding="utf-8", errors="replace").strip()[:_MAX_BROWSER_SOP_CHARS]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[sop] read source %s failed: %s", filename, exc)
        return ""

    async def _attach_mcp_tools(self, tools: ToolRegistry) -> None:
        """Resolve the Agent's effective MCP services and build a capability index.

        GA alignment: MCP methods are NOT registered as first-class tools. They are
        advertised in the system prompt's [Available Capabilities] index and invoked
        through ``capability_call(name="<service>.<method>", args=...)``. The MCP
        manager (call routing) is still exposed to atomic tools that proxy into it
        (the aggregated ``web`` tool routes browser operations through it).

        - Effective services come from mcp_client.resolve_effective_services.
        - Tool schema is taken from the service's tools_cache first, falling back
          to SSE tools/list.
        - Browser-class services are detected dynamically (by capability tag or by
          exposing a browser primitive), never by a hardcoded service name.
        - Defensive: runtime down / service not loaded / cache miss only skips that
          service and warns; never blocks the Agent.
        """
        # 子 agent 路径：父已把 MCP manager + capability 索引注入（mcp_manager 非空），
        # 直接复用父的连接与索引挂到子 registry，不重连、不重发现。子 agent 的
        # capability_call 因此"无条件继承父"——父能调的 MCP 服务子都能调。
        if not self._owns_mcp_manager and self._mcp_manager is not None:
            self._tools.set_mcp_runtime(
                self._mcp_manager,
                capability_index=self._capability_index,
                browser_service=self._browser_service,
            )
            return

        from agent.mcp_client import MCPManager, resolve_effective_services

        config = await self._ensure_config()
        try:
            services = await resolve_effective_services(config.mcp_user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp] resolve_effective_services failed: %s", exc)
            return
        if not services:
            return

        service_tokens = await self._collect_service_tokens(services)
        manager = MCPManager(
            service_tokens=service_tokens,
            cdp_session_id=_scene_cdp_session_key(self._scene),
            cdp_client_ids=_scene_cdp_client_ids(self._scene),
        )
        self._mcp_manager = manager

        BROWSER_PRIMITIVES = {
            "evaluate_script", "navigate", "capture_screenshot",
            "capturescreenshot", "click", "get_url", "set_url",
            "list_tabs", "get_tabs", "screenshot",
            # Platform CDP bridge naming.
            "browser_execute_js", "browser_navigate", "browser_scan",
            "browser_get_tabs", "browser_screenshot", "browser_switch_tab",
        }
        capability_index: list[dict] = []
        browser_service: str | None = None

        for svc in services:
            service_name = svc.get("name")
            if not service_name:
                continue
            try:
                mcp_tools = await self._tools_for_service(svc)
            except Exception as exc:  # noqa: BLE001 — 单个服务失败不影响其它服务
                logger.warning("[mcp] discover tools failed service=%s: %s", service_name, exc)
                continue
            method_names: list[str] = []
            method_meta: list[dict] = []
            is_browser = False
            for tool in mcp_tools:
                namespaced = f"{service_name}__{tool.name}"
                manager.tools[namespaced] = tool  # available via capability_call routing
                method_names.append(tool.name)
                method_meta.append({
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    # Carry the inputSchema so the seeded MCP SOP can document the
                    # parameters a first-time caller would otherwise have to guess.
                    "input_schema": getattr(tool, "input_schema", None) or {},
                })
                if str(tool.name).lower() in BROWSER_PRIMITIVES:
                    is_browser = True
            if is_browser and browser_service is None:
                browser_service = service_name
            capability_index.append({
                "service": service_name,
                "method_names": method_names,
                "methods": method_meta,
                "browser": is_browser,
                "sop_ref": f"memory/sop/mcp/{service_name}_sop.md" if method_names else "",
            })

        self._capability_index = capability_index
        self._browser_service = browser_service
        # Expose the MCP manager + capability index to atomic tools (web/capability_call).
        tools.set_mcp_runtime(
            manager,
            capability_index=capability_index,
            browser_service=browser_service,
        )
        # Seed each service's usage SOP (with parameter schemas) before any call, so the
        # L1 pointer the prompt advertises resolves to an executable file immediately.
        try:
            await tools.seed_mcp_sops()
        except Exception as exc:  # noqa: BLE001 — seeding must never block the Agent
            logger.warning("[mcp] seed mcp sops failed: %s", exc)
        logger.info(
            "[mcp] indexed %d MCP service(s) (browser=%s); methods reachable via capability_call",
            len(capability_index), browser_service,
        )

    async def _collect_service_tokens(self, services: list[dict]) -> dict[str, str]:
        """为开启鉴权的服务取一个可用 token（SSE ?token= / Bearer 用）。

        取不到 token 的鉴权服务不放进映射——调用时网关会 401，届时降级为该工具报错，
        但不阻断 agent 初始化。

        统一口径：agent 绑定了 mcp_user_id 时，**所有**开启鉴权的 MCP 服务都固定
        使用该用户的 token（前提是该用户已获该服务授权，即 token 在服务
        allowed_tokens 内）。这样 agent 的全部 MCP 请求都以同一个 MCP 用户身份发出；
        对 cdp-bridge 而言，该 token 经 runtime 的 token→client 路由落到对应客户端的
        浏览器会话池。未绑定用户或该用户未获授权时，不从 allowed_tokens 任取，避免
        跨用户串台——该服务的工具调用会 401 并降级为工具报错。
        """
        import mcp_plugin_store

        config = self.config or await self._ensure_config()
        bound_user_id = getattr(config, "mcp_user_id", None)
        bound_token = ""
        if bound_user_id is not None:
            try:
                user = await mcp_plugin_store.get_mcp_user(int(bound_user_id), mask_token=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mcp] get bound mcp_user failed user_id=%s: %s", bound_user_id, exc)
                user = None
            if user and user.get("enabled") and user.get("token"):
                bound_token = user["token"]
            elif user and user.get("enabled") and user.get("token_hash"):
                logger.warning(
                    "[mcp] bound principal user_id=%s has no recoverable plaintext token; "
                    "rotate it or provision the token to this Agent runtime",
                    bound_user_id,
                )

        # principal 的明文 token 不落库。Agent 调用时签发短生命周期 identity token，
        # 网关由 agent_id → agents.mcp_user_id → principal 解析身份；具体操作哪个 CDP
        # 客户端 / 邮箱账户由 driver 读 principal 的 param 定位（见 cdp_bridge_plugin /
        # mail_plugin），主链路不再做 grant scope 判断。
        identity_token = ""
        if bound_user_id is not None and self.agent_id is not None:
            try:
                import builtin_tool_store

                # target_id 必须是 agents 行 id（网关 identity 分支据此
                # get_agent_mcp_principal_id 反查绑定 principal）。AgentConfig 是配置
                # dataclass、不带行 id，这里用 self.agent_id。
                _row, identity_token = await builtin_tool_store.issue_token(
                    "agent", str(self.agent_id), display_token=False,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mcp] issue agent identity token failed agent_id=%s: %s", self.agent_id, exc)

        tokens: dict[str, str] = {}
        # 市场 MCP 以本次对话发起者身份调用。token 只存在于本次 Agent
        # runtime，不返回前端；adapter 会再次核验用户仍为平台管理员。
        # 场景保留：只有市场管理场景（scene.capabilities 含 marketplace-status）才签发
        # 身份 token——普通 Agent即使因历史配置还挂着这个服务，也拿不到可用身份。
        scene_caps = set(getattr(self._scene, "capabilities", ()) or ())
        marketplace_services = [
            svc for svc in services
            if svc.get("name") == "marketplace-status" and "marketplace-status" in scene_caps
        ]
        if marketplace_services:
            try:
                import builtin_tool_store

                target_id = self._run_as_caller if self._run_as_caller is not None else "__admin__"
                _row, marketplace_token = await builtin_tool_store.issue_token(
                    "user", str(target_id), display_token=False,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
                )
                tokens["marketplace-status"] = marketplace_token
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mcp] issue marketplace identity token failed: %s", exc)

        for svc in services:
            if svc.get("name") == "marketplace-status":
                continue
            name = svc.get("name")
            svc_id = svc.get("id")
            if not name or svc_id is None:
                continue
            # 鉴权一律强制：所有 MCP 服务都要 token，不再按服务的 auth_enabled 开关
            # 决定收不收集 token（曾让 admin 关过鉴权的匿名服务两边口径不一致致 401）。
            # identity token 即放行：具体操作哪个 CDP 客户端 / 邮箱账户 / 设备由 driver
            # 读 principal 的 param 定位（cdp_bridge_plugin / mail_plugin / device_control_plugin），
            # custom/sse/stdio 服务的授权由网关按 mcp_service_users 判。
            if identity_token:
                tokens[name] = identity_token
                continue
            # 兼容旧 principal plaintext token + mcp_service_users 授权（identity token 签不出
            # 时的兜底：agent_id 为空 / issue_token 失败的极少数路径）。
            try:
                auth = await mcp_plugin_store.get_service_auth(int(svc_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mcp] get_service_auth failed service=%s: %s", name, exc)
                continue
            allowed = auth.get("allowed_tokens") or set()
            if bound_token and bound_token in allowed:
                tokens[name] = bound_token
                continue
            # 未绑定/未授权：不任取 token，避免跨用户串台。
            logger.warning(
                "[mcp] service=%s 鉴权已强制，但 agent 未绑定获授权的 MCP 用户，调用将 401/403",
                name,
            )
        return tokens

    async def _tools_for_service(self, svc: dict) -> list:
        """取服务工具列表：优先 tools_cache，缺失回退 SSE tools/list。"""
        from agent.mcp_client import MCPServerConfig, MCPTool

        service_name = svc.get("name") or ""
        cache = svc.get("tools_cache")
        tools: list = []
        if isinstance(cache, list) and cache:
            for entry in cache:
                if not isinstance(entry, dict):
                    continue
                tools.append(
                    MCPTool(
                        name=entry.get("name") or "",
                        description=entry.get("description") or "",
                        input_schema=entry.get("input_schema") or {},
                        service_name=service_name,
                    )
                )
            tools = [t for t in tools if t.name]
            if tools:
                return tools

        # tools_cache 缺失/为空：回退 SSE tools/list。
        cfg = MCPServerConfig.from_dict({
            "name": service_name,
            "transport": svc.get("transport") or "stdio",
            "command": svc.get("command"),
            "args": svc.get("args") or [],
            "url": svc.get("url"),
            "builtin": bool(svc.get("builtin")) or svc.get("kind") == "builtin",
        })
        mgr = self._mcp_manager
        if mgr is None:
            from agent.mcp_client import MCPManager
            mgr = MCPManager()
        return await mgr.discover_tools(cfg)

    @property
    def llm(self) -> GatewayLLMBridge | None:
        return self._llm

    @property
    def tools(self) -> ToolRegistry | None:
        return self._tools

    def set_event_sink(self, on_event) -> None:
        """注入父级事件出口，spawn 出的子 agent 会把事件透传到这里。

        主对话流（agent.run_task 直接被调用，或 api.py 直接调 agent_runner_loop）
        在开始前调用本方法，spawn_subagent_async 内部取用。
        """
        self._event_sink = on_event

    def _build_subagent_approval_batch(self, tools, config):
        """为子 agent 构造审批批次：emit=父对话流，auto_approve 读父会话活状态。

        子 agent 不自批（对齐 dsh "child scope fresh-flat"）：code_run / 越界写入
        要么在父会话自动模式下自动放行，要么弹卡到父对话流由用户批。批次是子自己的
        独立实例（独立 _decisions），不与父批次共享额度。
        """
        from agent.code_run_approval import CodeRunApprovalBatch
        from agent.file_write_approval import FileWriteApprovalBatch
        from agent.workflow_approval import WorkflowApprovalBatch

        conv_id = self._conversation_id
        caller = self._run_as_caller

        def _auto_approve() -> bool:
            """读父会话活状态：auto 模式 → 批；交互模式 → 弹卡。

            先 peek（同步热路径）；实例不在注册表（被 sweep/重启后未再触碰）时
            peek 一个临时的兜底——不能 async（回调是同步契约），兜底语义：读不到
            按交互处理（安全侧）。父会话在跑回合时 set_busy 钉住了实例，正常路径
            peek 一定命中。
            """
            try:
                from agent import conversation_state
                st = conversation_state.peek(conv_id) if conv_id else None
                return bool(st.auto_approve) if st is not None else False
            except Exception:  # noqa: BLE001 — 读不到会话状态按交互处理，安全侧
                return False

        timeout = int(getattr(config, "approval_timeout_seconds", 0) or 0)
        code_batch = CodeRunApprovalBatch(
            conversation_id=conv_id,
            agent_id=int(self.agent_id or 0),
            caller=caller,
            emit=self._event_sink,
            timeout_seconds=timeout,
            auto_approve=_auto_approve,
        )
        file_batch = FileWriteApprovalBatch(
            conversation_id=conv_id,
            agent_id=int(self.agent_id or 0),
            caller=caller,
            emit=self._event_sink,
            security=tools.context.security,
            timeout_seconds=timeout,
            auto_approve=_auto_approve,
        )
        tools.set_code_run_approval(code_batch)
        tools.set_file_write_approval(file_batch)
        # 工作流定义写审批：子 agent 自批（对齐 dsh "child scope fresh-flat"）——
        # create/update/delete 弹卡到父对话流，auto 模式下自动放行。读与运行态不进卡。
        workflow_batch = WorkflowApprovalBatch(
            conversation_id=conv_id,
            agent_id=int(self.agent_id or 0),
            caller=caller,
            emit=self._event_sink,
            timeout_seconds=timeout,
            auto_approve=_auto_approve,
        )
        tools.set_workflow_approval(workflow_batch)

        async def _prepare(tool_calls: list[dict]) -> None:
            await code_batch.prepare(tool_calls)
            await file_batch.prepare(tool_calls)
            await workflow_batch.prepare(tool_calls)

        return _prepare

    async def run_task(
        self,
        prompt: str,
        system_prompt: str = "",
        max_turns: int | None = None,
        on_event=None,
        llm: "GatewayLLMBridge | None" = None,
    ) -> list[dict[str, Any]]:
        """跑一轮任务。

        ``system_prompt`` 留空时回落到 Agent 行上的 system_prompt（人设/指令），而不是
        跑成空提示：对话链路会显式传会话级 system_prompt，而无人值守入口（定时任务、
        自愈诊断）没有会话可读，之前不传就把 Agent 人设整段丢了。

        ``llm`` 允许调用方指定本次用的桥（定时任务用 resolve_scheduled_llm 换模型），
        不传则用主 Agent 的桥。传入不会写回 self._llm，只作用于这一次运行。
        """
        config = await self._ensure_config()
        if llm is None:
            llm = await self._ensure_llm()
        tools, skill_index = await self._ensure_resources()
        # 显式传入优先；没传就用 Agent 自己的人设（_ensure_config 已从 DB 读出）。
        effective_system_prompt = (system_prompt or "").strip() or self._agent_system_prompt
        if skill_index:
            effective_system_prompt = f"{effective_system_prompt}\n\n{skill_index}".strip()
        # 记录事件出口给本 agent 内 spawn 出的子 agent 透传用。
        if on_event is not None:
            self._event_sink = on_event

        handler = _AgentHandler(tools, max_turns or config.max_turns)
        outputs: list[dict[str, Any]] = []

        # 子 agent 审批：复用现有 CodeRunApprovalBatch/FileWriteApprovalBatch，
        # 但 emit=on_event（子的 forward → 父对话流）+ auto_approve 读父会话活状态。
        # 自动模式→auto allow（不弹卡）；交互模式→弹卡到父对话流，resolve 经 WS。
        # 这里建的批次是子自己的（独立 _decisions），不与父批次共享额度。
        on_tool_batch = None
        if self._is_subagent and on_event is not None:
            on_tool_batch = self._build_subagent_approval_batch(tools, config)

        loop_kwargs: dict[str, Any] = {
            "max_turns": max_turns or config.max_turns,
            "verbose": False,
            "on_event": on_event,
            "auto_compact": getattr(config, "auto_compact", True),
        }
        # 仅子 agent 路径传 on_tool_batch：主 agent 的审批批次由调用方（api.py）
        # 自己 prepare，不在这里重复挂。测试桩 loop 无此参数时也跳过。
        if on_tool_batch is not None:
            loop_kwargs["on_tool_batch"] = on_tool_batch

        try:
            async for item in agent_runner_loop(
                llm,
                effective_system_prompt,
                prompt,
                handler,
                tools.get_schema(),
                **loop_kwargs,
            ):
                outputs.append(item)
        finally:
            # 父 agent 退出时把所有仍在跑的子 agent 一并 cancel：子是同进程协程，
            # 不清掉会成孤儿继续占资源 / 抢网关额度。崩溃路径由本 finally 兜底。
            for h in list(self.sub_agents):
                if not h.asyncio_task.done():
                    h.asyncio_task.cancel()
            # Release any CDP tab leases this Agent runtime acquired. The per-task
            # MCP manager is created in _attach_mcp_tools; close() releases by holder.
            # 子 agent 共享父的 manager（_owns_mcp_manager=False），不能在这里 close——
            # 否则子的 run_task finally 会把父的 MCP 连接一起关掉。
            # Errors here must not mask the real exception above.
            try:
                manager = self._mcp_manager
                if manager is not None and self._owns_mcp_manager:
                    await manager.close()
            except Exception:
                pass
        return outputs

    async def _spawn_subagent_tool(self, args: dict) -> dict[str, Any]:
        """``spawn_subagent`` 工具实现（仅主 agent 注册）。

        Foreground / one-shot：派生一个 fresh 子 agent 并等它的最终回答，返回文本。
        子 agent 不继承对话上下文（对齐 dsh spawn）；继承父的工具/场景/权限。
        """
        task = str(args.get("task") or "").strip()
        if not task:
            return {"status": "error", "msg": "task 不能为空（子 agent 看不到本对话，任务必须自足）"}
        model = str(args.get("model") or "").strip() or None
        # 岗位/难度：白名单外的值静默降级为「不特化」（不炸父，模型可自纠）。
        role = str(args.get("role") or "").strip()
        role = role if role in _SUBAGENT_ROLES else ""
        category = str(args.get("category") or "").strip()
        category = category if category in _SUBAGENT_CATEGORIES else ""
        handle: SubagentHandle | None = None
        try:
            handle = await self.spawn_subagent_async(
                task=task, model=model, role=role, category=category,
            )
            outputs = await handle.asyncio_task
        except asyncio.CancelledError:
            # 父被 kill 或会话中止：子任务由 _run_subagent_task 的 finally 自清。
            raise
        except Exception as exc:  # noqa: BLE001 — 子崩溃转成工具错误，不炸父
            logger.warning("[subagent] spawn failed: %s", exc)
            return {"status": "error", "msg": f"子 agent 执行失败：{type(exc).__name__}: {exc}"}
        finally:
            if handle is not None:
                self.sub_agents = [h for h in self.sub_agents if h is not handle]
        final = ""
        if outputs:
            last = outputs[-1]
            final = str(last.get("data") or "") if isinstance(last, dict) else str(last)
        # ask_user 两级的第一级：子以提问收尾（ask_user 是 should_exit 工具，loop 把
        # 它的 data 原样 yield 出来，status="question"）。父要么自己答、要么转人工。
        # 结果即汇报，无双向通道。
        asked = False
        if outputs and isinstance(outputs[-1], dict):
            data = outputs[-1].get("data")
            asked = isinstance(data, dict) and data.get("status") == "question"
        result: dict[str, Any] = {
            "status": "ok",
            "subagent_id": handle.subagent_id,
            "result": final,
        }
        if asked:
            result["status"] = "needs_input"
            result["msg"] = "子 agent 提出了问题需要回答（见 result）。自行解答后继续，或转给用户。"
        return result

    async def spawn_subagent_async(
        self,
        task: str,
        model: str | None = None,
        api_key: str | None = None,
        llm_model_id: int | None = None,
        role: str | None = None,
        category: str | None = None,
    ) -> SubagentHandle:
        """异步派生子 agent，选择走网关的 LLM 桥与模型，并注入岗位人格。

        key 与模型正交：
        - key：绑了 subagent_api_key_id 就用它（独立计费/归属），否则复用主桥的 key
          （fork 保留 key——fork 只能换模型换不了 key，per-role 换 key 不支持）。
        - 模型解析顺序：1) 显式 model；2) 岗位/难度查表（subagent_model_map：
          categories[category] > roles[role]）；3) config.subagent_model；4) 专用子
          key 分支下再回落 main_model；都没命中 → 跟随主 agent。
        - map 未命中 → ``resolved`` 为 None → 与旧解析链逐字一致（默认行为不变）。
        - role 另外决定子 agent 的人格（``_ROLE_PERSONAS``），即使该 role 没配模型
          也照样注入人格。

        ``llm_model_id`` 为历史签名保留（已不再走 agent_llm_models 库存），传入时按
        「跟随主桥」处理，避免调用方报错。
        """
        parent_llm = await self._ensure_llm()
        config = self.config or await self._ensure_config()
        role = (role or "").strip()
        category = (category or "").strip()
        resolved = (model or "").strip() or await self._resolve_subagent_model(role, category)
        child_llm: "GatewayLLMBridge"
        if config.subagent_api_key_id:
            # 专用子 key：key 固定，模型取 resolved → subagent_model → main_model。
            child_llm = await self._resolve_gateway_bridge(
                config.subagent_api_key_id,
                resolved or config.subagent_model or config.main_model,
                role="subagent",
            )
        elif resolved:
            child_llm = parent_llm.fork(model=resolved)
        elif config.subagent_model:
            child_llm = parent_llm.fork(model=config.subagent_model)
        else:
            child_llm = parent_llm.fork()
        # 岗位人格：父人设 + role 人格拼接（子没有自己的 agent_system_prompt，见
        # _run_subagent_task）。general/未指定 → 空 → 不注入，行为同现状。
        persona = _ROLE_PERSONAS.get(role, "")
        child_system_prompt = (
            f"{self._agent_system_prompt}\n\n{persona}".strip()
            if (persona and self._agent_system_prompt)
            else (persona or "")
        )
        self._subagent_seq += 1
        handle_id = f"sub-{id(self)}-{self._subagent_seq}"
        handle_name = f"子 Agent {self._subagent_seq}"
        task_handle = asyncio.create_task(
            self._run_subagent_task(
                handle_id, handle_name, task, child_llm,
                role=role, system_prompt=child_system_prompt,
            )
        )
        handle = SubagentHandle(task=task, llm=child_llm, asyncio_task=task_handle, subagent_id=handle_id, handle_name=handle_name, role=role)
        self.sub_agents.append(handle)
        return handle

    def spawn_subagent(
        self,
        task: str,
        model: str | None = None,
        api_key: str | None = None,
    ) -> SubagentHandle:
        """同步派生子 agent（兼容入口）。

        仅支持显式模型覆盖；不读取 subagent 角色映射（同步函数无法做 DB I/O）。
        需要按角色映射派生时请使用 spawn_subagent_async。
        """
        if self._llm is None:
            raise RuntimeError("LLM 尚未初始化，无法派生子 agent（请先调用 _ensure_llm）")
        child_llm = self._llm.fork(model=model)
        self._subagent_seq += 1
        handle_id = f"sub-{id(self)}-{self._subagent_seq}"
        handle_name = f"子 Agent {self._subagent_seq}"
        task_handle = asyncio.create_task(self._run_subagent_task(handle_id, handle_name, task, child_llm))
        handle = SubagentHandle(task=task, llm=child_llm, asyncio_task=task_handle, subagent_id=handle_id, handle_name=handle_name)
        self.sub_agents.append(handle)
        return handle

    async def _run_subagent_task(
        self,
        handle_id: str,
        handle_name: str,
        task: str,
        llm: GatewayLLMBridge | None,
        role: str = "",
        system_prompt: str = "",
    ) -> list[dict[str, Any]]:
        # 子 agent 用独立 ToolRegistry（隔离 current_turn / jobs / 审批额度），但共享
        # 父的 MCP manager + capability 索引（不重连 MCP 服务、不重挂 capability）。
        # tools=None 让 GenericAgent 内部建独立 registry；mcp_manager/capability_index/
        # browser_service 从父继承，_ensure_tools 据此跳过 _attach_mcp_tools 的重连分支。
        # is_subagent=True 让 registry 不注册 spawn_subagent / start_long_term_update。
        # parent_subagent_id 透传到子 LLM 桥，落 request_logs.subagent_id 区分父子成本。
        # 真桥有 fork(subagent_id=)；测试用 stub 桥（SimpleNamespace）无 fork —— 跳过即可，
        # subagent_id 已在 _resolve_gateway_bridge 的 llm=None 路径兜底（主 agent 该值为 None）。
        if llm is not None and hasattr(llm, "fork") and not getattr(llm, "subagent_id", None):
            llm = llm.fork(subagent_id=handle_id)
        child = GenericAgent(
            config=self.config,
            agent_id=self.agent_id,
            llm=llm,
            tools=None,
            run_as_caller=self._run_as_caller,
            scene=self._scene,
            # 会话血缘继承父级：子 agent 的上游请求与父同属一次对话，日志页按同一
            # session_id 串起来。llm 已派生自父桥（含 session_id）时这里等价冗余，
            # 但 llm=None 走 _ensure_llm 重建时靠它兜底。
            conversation_id=self._conversation_id or None,
            parent_subagent_id=handle_id,
            is_subagent=True,
            mcp_manager=self._mcp_manager,
            capability_index=self._capability_index,
            browser_service=self._browser_service,
        )

        # 构造事件转发器：把子 agent 的事件改写为 subagent_* 前缀并带上 handle_id，
        # 先发 subagent_start，结束后发 subagent_end。仅当父级挂了事件出口时透传。
        parent_sink = self._event_sink

        async def forward(event: dict) -> None:
            base_type = event.get("type", "")
            out = {
                "subagent_id": handle_id,
                "subagent_name": handle_name,
                **event,
                "type": f"subagent_{base_type}" if base_type else "subagent_event",
            }
            # 双投：父对话流（agent/<conv>，客户端零感知）+ 子 agent 频道
            # （subagent/<child_id>，单独订阅子流）。WS 订阅者是同一序列的两种视图。
            try:
                from agent import realtime as _rt

                await _rt.publish("subagent", handle_id, out)
            except Exception:  # noqa: BLE001
                pass
            if not parent_sink:
                return
            try:
                await parent_sink(out)
            except Exception:  # noqa: BLE001 — 子 agent 事件出口失败不应阻断其自身执行
                logger.debug("[subagent] failed to forward event %s", base_type, exc_info=True)

        if parent_sink:
            try:
                await parent_sink({
                    "type": "subagent_start",
                    "subagent_id": handle_id,
                    "subagent_name": handle_name,
                    "task": task,
                })
            except Exception:  # noqa: BLE001
                logger.debug("[subagent] failed to forward start event", exc_info=True)
        try:
            from agent import realtime as _rt

            await _rt.publish("subagent", handle_id, {
                "type": "subagent_start",
                "subagent_id": handle_id,
                "subagent_name": handle_name,
                "task": task,
            })
        except Exception:  # noqa: BLE001
            pass

        outputs: list[dict[str, Any]] = []
        try:
            # 登记：WS kill 动作按 child_id 定位子任务；parent_conv 供 authorize 按
            # 父会话归属校验客户端只读订阅。
            from agent import realtime as _rt

            _rt.register_subagent(handle_id, asyncio.current_task(), self._conversation_id or "")
            # role 人格经 run_task(system_prompt=) 注入；未指定 role（persona 空）时
            # 不传该 kwarg——与旧签名调用方（含测试桩）逐字兼容。
            if system_prompt:
                if parent_sink:
                    outputs = await child.run_task(task, system_prompt=system_prompt, on_event=forward)
                else:
                    outputs = await child.run_task(task, system_prompt=system_prompt)
            else:
                if parent_sink:
                    outputs = await child.run_task(task, on_event=forward)
                else:
                    outputs = await child.run_task(task)
        finally:
            from agent import realtime as _rt

            _rt.unregister_subagent(handle_id)
            summary = ""
            if outputs:
                last = outputs[-1]
                summary = str(last.get("data") or "") if isinstance(last, dict) else str(last)
            try:
                await _rt.publish("subagent", handle_id, {
                    "type": "subagent_end",
                    "subagent_id": handle_id,
                    "subagent_name": handle_name,
                    "summary": summary[:500],
                })
            except Exception:  # noqa: BLE001
                pass
            if parent_sink:
                try:
                    await parent_sink({
                        "type": "subagent_end",
                        "subagent_id": handle_id,
                        "subagent_name": handle_name,
                        "summary": summary[:500],
                    })
                except Exception:  # noqa: BLE001
                    logger.debug("[subagent] failed to forward end event", exc_info=True)
        return outputs
