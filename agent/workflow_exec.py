"""Concrete workflow step executors — bridge engine steps to platform abilities.

The engine stays dependency-free; this module wires each step kind to the real
platform. A workflow is **agent-independent** — everything here resolves from
the run's owner identity (a user), never from an agent binding:

- ``code_run`` / ``file_read`` / ``file_write`` → the sandbox: subprocess run in
  the workflow's own workspace dir, file IO guarded by :class:`WorkspaceSecurity`.
  No approval coordinator — a workflow IS the pre-approved human artifact (the
  user built and saved this graph deliberately); unattended scheduled runs are
  the same picture.
- ``web`` / ``run_mcp`` → the platform MCP stack, called with a **user identity
  token** (``issue_token("user", owner)``): the gateway's identity branch natively
  authorizes user-owned callers (sse/custom via can_use_service, builtin tools
  via plugin owner-path narrowing). The browser a ``web`` step drives is chosen
  by that step's own ``client_id`` — forwarded verbatim, and the plugin rejects
  any id outside the token owner's clients. No agent in the loop.
- ``run_session`` → a fresh ``GenericAgent`` run with the step's ``sop`` text as
  the prompt (an SOP-like task description); ``agent_id`` picks the agent
  (``"any"``/empty = the first enabled agent of the owner). Unattended: there is
  no conversation to answer an approval card, so in-session code_run/越界写入
  fail closed.
- ``cache`` → :data:`JdbcClient.redis` (the platform Redis), keys forced under
  the fixed ``wf:`` prefix (before the global ``prefix_key`` the wrapper adds).
- ``http`` → one outbound HTTP request through the shared proxy manager
  (``get_proxy_manager().request``): ``proxy`` picks the outbound route (empty =
  direct, a proxy-pool id = that entry, ``$input.<key>`` = a proxy-typed input's
  value). Same routing the channel providers use — no new credentials.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from agent.workflow import (
    CACHE_KEY_PREFIX,
    MAX_NODE_OUTPUT_CHARS,
    NodeExecutors,
    WorkflowContext,
)


class WorkflowStepExecutors(NodeExecutors):
    """Concrete executors backed by the platform's sandbox/MCP/agent stack.

    One instance per workflow run. ``owner`` is the user the run is attributed
    to (a C-side user id, or None = admin) — it decides file-sandbox roots,
    the MCP identity token, and run_session's agent pick.
    """

    def __init__(
        self,
        *,
        owner: str | None,
        workspace_root: str = "",
    ) -> None:
        # owner 为 None（管理员）：网关 identity 分支对 user token 的 can_use_service
        # 判定在 user_id=None 时全量放行（mcp_plugin_store.can_use_service）。
        self._owner = owner
        # Every workflow run owns its own workspace dir (data/workflows/<id>/):
        # file_write/code_run land there; never inside an agent's tree.
        self._workspace_root = workspace_root
        # Lazily-built MCP stack (identity token + manager + capability index).
        self._mcp_ready = False
        self._mcp_manager = None
        self._capability_index: list[dict] = []
        self._browser_entry: dict | None = None

    # ── code_run ─────────────────────────────────────────────────────────────

    # python 步骤的包装：`data` = 解析后的 input_mapping（stdin JSON 注入），代码在
    # 模块层执行并给 `result` 赋值，包装器把 result 以 JSON 打到 stdout 的标记后，
    # 由执行器解析回图变量。不在服务进程内 eval——独立解释器让崩溃/死循环都伤不到
    # runner（与旧 code 节点同一契约）。
    _CODE_WRAPPER = '''import json, sys
data = json.loads(sys.stdin.read())
{code}
print("__WF_RESULT__" + json.dumps(result, ensure_ascii=False, default=str))
'''

    async def run_code_run(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        code_type = str(params.get("type") or "python").lower()
        timeout = int(params.get("timeout") or 60)
        started = time.monotonic()
        run_dir = self._ensure_run_dir()
        # input_mapping 深解析过的上游值，作为 `data` 注入 python 步骤。
        inputs = params.get("input_mapping") or {}
        if not isinstance(inputs, dict):
            inputs = {}
        code = str(params.get("code") or "")

        if code_type == "shell":
            import sys
            cmd = ["cmd", "/c", code] if sys.platform == "win32" else ["/bin/sh", "-c", code]
        elif code_type == "powershell":
            import shutil
            pwsh = shutil.which("pwsh") or "powershell"
            cmd = [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code]
        else:
            import sys
            wrapped = self._CODE_WRAPPER.format(code=code)
            cmd = [sys.executable, "-X", "utf8", "-c", wrapped]

        result = await self._run_subprocess(cmd, run_dir, timeout, started, stdin=json.dumps(inputs, ensure_ascii=False))

        # python：解析 __WF_RESULT__ 标记 → result 作为本步输出（下游 $node 引用它）。
        if code_type not in ("shell", "powershell"):
            marker = "__WF_RESULT__"
            stdout = str(result.get("stdout") or "")
            if marker in stdout:
                try:
                    return json.loads(stdout.rsplit(marker, 1)[1])
                except ValueError:
                    raise RuntimeError("code_run step: __WF_RESULT__ 后不是合法 JSON") from None
            if result.get("exit_code") == 0:
                raise RuntimeError(
                    "code_run step produced no __WF_RESULT__ output (python 步骤必须给 `result` 赋值)"
                )
        return result

    def _ensure_run_dir(self):
        from pathlib import Path
        d = Path(self._workspace_root or "data/workflows").resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _run_subprocess(self, cmd: list, run_dir, timeout: int, started: float, stdin: str = "") -> dict:
        """Run a step subprocess (async, loop-friendly) and return a uniform dict."""
        import sys
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin else None,
            cwd=str(run_dir),
            start_new_session=sys.platform != "win32",
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin.encode("utf-8") if stdin else None), timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._kill_tree(proc)
            raise RuntimeError(f"code_run step timed out ({timeout}s)") from None
        except asyncio.CancelledError:
            # Stop/shutdown mid-run: take the child down detached (Windows
            # taskkill takes ~1s; blocking the cancel on it is sluggish).
            await self._kill_tree(proc, await_finish=False)
            raise
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")[:10000]
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")[:10000]
        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": proc.returncode,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    @staticmethod
    async def _kill_tree(proc, await_finish: bool = True) -> None:
        import sys
        if proc.returncode is not None:
            return
        try:
            if sys.platform == "win32":
                # Windows: taskkill walks the tree; it must fire BEFORE proc.kill
                # (once the direct child is gone taskkill cannot find the group).
                detached = asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                if await_finish:
                    try:
                        await asyncio.wait_for(detached.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        pass
            else:
                import os
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        if await_finish:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    def _security(self):
        """Sandbox guard for this workflow's own workspace dir.

        file_read/file_write resolve relative paths here and nowhere else — the
        workflow is not an agent, so it has no data/agents/<id>/ tree; code_run
        runs in the same dir (see _ensure_run_dir), so a step's产物是下一步可读的
        真实文件。
        """
        from agent.config import AgentConfig
        from agent.tools import WorkspaceSecurity

        root = self._workspace_root or "data/workflows"
        return WorkspaceSecurity(AgentConfig(workspace_root=root, allowed_roots=[root]))

    # ── file_read / file_write ────────────────────────────────────────────────

    async def run_file_read(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        path = self._security().resolve_path(str(params.get("path") or ""))
        if not path.exists():
            raise RuntimeError(f"file_read: file not found: {path}")
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        total = len(lines)
        keyword = str(params.get("keyword") or "").strip()
        start = int(params.get("start") or 1)
        count = int(params.get("count") or 200)
        if keyword:
            hit = next((i for i, line in enumerate(lines) if keyword.casefold() in line.casefold()), None)
            if hit is None:
                return {"status": "ok", "content": "", "lines": 0, "total_lines": total, "truncated": False}
            begin, end = hit, hit + count
        else:
            begin, end = max(0, start - 1), max(0, start - 1) + count
        return {
            "status": "ok",
            "content": "".join(lines[begin:end]),
            "lines": len(lines[begin:end]),
            "total_lines": total,
            "truncated": end < total,
        }

    async def run_file_write(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        path = self._security().resolve_path(str(params.get("path") or ""))
        content = str(params.get("content") or "")
        mode = str(params.get("mode") or "overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        if mode == "append":
            with path.open("ab") as handle:
                handle.write(data)
        else:
            path.write_bytes(data)
        return {"status": "ok", "bytes_written": len(data), "path": str(path)}

    # ── web / run_mcp（MCP stack, user identity token）───────────────────────

    async def _ensure_mcp(self) -> None:
        """Build the run's MCP stack once: user identity token + manager + index.

        Token: ``issue_token("user", owner)`` — the gateway's identity branch
        authorizes user-owned callers (can_use_service for sse/custom, builtin
        plugin owner-path for cdp-bridge/mail). Services the owner cannot use
        simply 403 at call time; discovery itself is not permission-gated.

        The browser boundary is the **step's own** ``client_id``: the plugin
        narrows instances to the token owner's clients, and ``client_id`` selects
        within them — exactly how the agent-side ``web`` tool works, with no
        agent in the loop.
        """
        if self._mcp_ready:
            return
        self._mcp_ready = True

        import mcp_plugin_store

        try:
            services = [s for s in await mcp_plugin_store.list_services() if s.get("enabled")]
        except Exception as exc:  # noqa: BLE001 — MCP 不可用不炸 run，步骤自会报错
            logger.warning("[workflow] list_services failed: %s", exc)
            services = []

        BROWSER_PRIMITIVES = {
            "evaluate_script", "navigate", "capture_screenshot",
            "capturescreenshot", "click", "get_url", "set_url",
            "list_tabs", "get_tabs", "screenshot",
            "browser_execute_js", "browser_navigate", "browser_scan",
            "browser_get_tabs", "browser_screenshot", "browser_switch_tab",
        }
        index: list[dict] = []
        for svc in services:
            name = str(svc.get("name") or "")
            if not name:
                continue
            methods = await self._service_methods(svc)
            entry = {
                "service": name,
                "methods": methods,
                "method_names": [m["name"] for m in methods],
                "browser": bool({str(m["name"]).lower() for m in methods} & BROWSER_PRIMITIVES),
            }
            index.append(entry)
            if entry["browser"] and self._browser_entry is None:
                self._browser_entry = entry
        self._capability_index = index

        # 一个用户身份 token 通吃所有服务（网关 identity 分支按用户判权）。
        token = ""
        if self._owner:
            try:
                import builtin_tool_store

                _row, token = await builtin_tool_store.issue_token(
                    "user", str(self._owner), display_token=False,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
                )
            except Exception as exc:  # noqa: BLE001 — token 失败降级为 401，不炸 run
                logger.warning("[workflow] issue user identity token failed: %s", exc)

        from agent.mcp_client import MCPManager

        # 不给 run 级客户端白名单：浏览器由每步自己的 client_id 选，实例范围由插件
        # 按 token owner 收窄（与 agent 链路同口径）。
        self._mcp_manager = MCPManager(
            service_tokens={entry["service"]: token for entry in index if token},
            cdp_session_id="",
        )

    @staticmethod
    async def _service_methods(svc: dict) -> list[dict]:
        """One service's methods: ``tools_cache`` first, gateway ``tools/list`` fallback."""
        cache = svc.get("tools_cache")
        methods: list[dict] = []
        if isinstance(cache, list) and cache:
            for entry in cache:
                if isinstance(entry, dict) and entry.get("name"):
                    methods.append({
                        "name": entry["name"],
                        "description": entry.get("description") or "",
                        "input_schema": entry.get("input_schema") or {},
                    })
            if methods:
                return methods
        # tools_cache 缺失/为空：进程内直调网关 tools/list（与 agent 链路同一入口）。
        try:
            from mcp_runtime.sse_gateway import _handle_rpc

            payload = await _handle_rpc(str(svc.get("name") or ""), {
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
            }, "")
        except Exception:  # noqa: BLE001 — 单服务发现失败只让它不可用，不炸 run
            return []
        tools = (payload or {}).get("result", {}).get("tools") if isinstance(payload, dict) else None
        out: list[dict] = []
        for item in tools or []:
            if isinstance(item, dict) and item.get("name"):
                out.append({
                    "name": item["name"],
                    "description": item.get("description") or "",
                    "input_schema": item.get("inputSchema") or item.get("input_schema") or {},
                })
        return out

    async def run_web(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        await self._ensure_mcp()
        operation = str(params.get("operation") or "read_page").strip().lower()

        # read_page (legacy op name: scan) without a tab: plain HTTP — no browser and
        # no MCP needed. Every other operation (and read_page with a tab) goes through
        # the browser MCP.
        if operation in ("read_page", "scan") and not str(params.get("tab") or "").strip():
            return await self._http_scan(params, operation=operation)

        entry = self._browser_entry
        if entry is None:
            raise RuntimeError("web step: no browser MCP service available")
        from agent.tools import _BROWSER_OPERATIONS, _WEB_FORWARD_ARGS, _WEB_TAB_ARGS

        spec = _BROWSER_OPERATIONS.get(operation)
        if spec is None:
            raise RuntimeError(f"web step: unsupported operation {operation!r}")
        advertised = {str(n).lower(): str(n) for n in entry.get("method_names", [])}
        canonical, alias_names, fallback_tab_arg = spec
        method = advertised.get(canonical) or next((advertised[n] for n in alias_names if n in advertised), None)
        if method is None:
            raise RuntimeError(f"web step: browser MCP does not offer {canonical}")

        # 与 agent 侧 web 工具同一套转发规则：只有显式给了的参数才转发（保留方法
        # 自己的默认值），client_id 在转发列表里——它就是「这一步自选的浏览器」，
        # 服务端按 token owner 的实例范围校验，越界即拒。
        forward: dict[str, Any] = {}
        for key in _WEB_FORWARD_ARGS:
            if params.get(key) is not None:
                forward[key] = params[key]
        tab = str(params.get("tab") or "").strip()
        if tab:
            forward[_WEB_TAB_ARGS.get(method.lower(), fallback_tab_arg) or "tab"] = tab
        # fill_credential 的凭据类型在 MCP 那一侧就叫 `kind`，但这个名字是引擎的
        # **节点类型字段**（node_params 已把它剔除，永远进不了参数包）。图上的写法
        # 是 `credential_kind`，在这里映射回 MCP 的 `kind`。值本身从不经过工作流：
        # password 由扩展按 entryId 自己取，otp 走「服务端→扩展→DOM」，回执无明文。
        if operation == "fill_credential":
            cred_kind = str(params.get("credential_kind") or "").strip().lower()
            if cred_kind not in ("otp", "password"):
                raise RuntimeError(
                    "web step fill_credential: credential_kind must be 'otp' or 'password' "
                    "(the field 'kind' names the step type and cannot carry the credential kind)"
                )
            forward["kind"] = cred_kind
        result = await self._call_mcp(entry["service"], method, forward)
        if isinstance(result, dict):
            result.setdefault("operation", operation)
        return result

    async def _http_scan(self, params: dict, *, operation: str = "read_page") -> Any:
        """read_page without a browser: aiohttp fetch + semantic text extraction."""
        import html as html_mod
        import re

        import aiohttp

        url = str(params.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise RuntimeError(f"web step read_page: bad URL {url!r}")
        max_chars = int(params.get("max_chars") or 8000)
        timeout = int(params.get("timeout") or 20)
        session_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=session_timeout) as sess:
            async with sess.get(url) as resp:
                charset = resp.charset or "utf-8"
                raw = await resp.content.read(2 * 1024 * 1024)
        text = raw.decode(charset, errors="replace")
        title_match = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", text)
        title = title_match.group(1).strip() if title_match else ""
        text = re.sub(r"(?is)<script\b.*?</script>", " ", text)
        text = re.sub(r"(?is)<style\b.*?</style>", " ", text)
        text = re.sub(r"(?is)<noscript\b.*?</noscript>", " ", text)
        text = re.sub(r"(?s)<!--.*?-->", " ", text)
        text = re.sub(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                      lambda m: f" {m.group(2)} ({m.group(1)}) ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html_mod.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\s*\n\s*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return {"status": "ok", "operation": operation, "url": url, "title": title[:200],
                "content": text[:max_chars], "chars": min(len(text), max_chars),
                "truncated": len(text) > max_chars}

    async def run_mcp(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        """Call one MCP method: ``service`` + ``method`` + ``args``."""
        await self._ensure_mcp()
        service = str(params.get("service") or "").strip()
        method = str(params.get("method") or "").strip()
        if not service or not method:
            raise RuntimeError("run_mcp step: service and method are required")
        call_args = params.get("args")
        if not isinstance(call_args, dict):
            call_args = {}
        return await self._call_mcp(service, method, call_args)

    async def _call_mcp(self, service: str, method: str, call_args: dict) -> Any:
        """Call one MCP method through the run's manager.

        The manager already sets the CDP session/client ContextVars per call; a
        workflow run leaves them empty so the plugin resolves instances from the
        token owner, and a ``web`` step's ``client_id`` argument does the picking
        (validated by the plugin against that owner's clients).
        """
        if self._mcp_manager is None:
            raise RuntimeError("MCP stack not ready")
        result = await self._mcp_manager.call_tool(f"{service}__{method}", call_args)
        if isinstance(result, dict) and result.get("status") == "error":
            raise RuntimeError(str(result.get("msg") or f"{service}.{method} failed"))
        return result

    # ── run_session ───────────────────────────────────────────────────────────

    async def run_session(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        """Launch a bounded agent run whose prompt is the step's ``sop`` text.

        ``agent_id`` picks the agent (a number, or ``"any"``/empty = first
        enabled agent of the run's owner); ``max_turns`` bounds the loop.
        ``model`` (optional) overrides the run's model — usually written as
        ``$input.<key>`` so the launch method picks it (a ``model``-typed input,
        a fixed literal, or a per-parameter value). The bridge reuses the
        agent's own key (``fork(model=...)``), exactly like the scheduled-task
        path — no new credentials are introduced.
        Unattended by design: there is no conversation to answer an approval
        card, so code_run/file_write inside the session fail closed (denied) —
        same picture as an unattended scheduled run. The SOP text should stick
        to read/probe/API work, or the workflow's own deterministic steps.
        """
        sop = str(params.get("sop") or "").strip()
        if not sop:
            raise RuntimeError("run_session step: sop is required")
        agent_want = str(params.get("agent_id") or "").strip()
        agent_id = await self._pick_agent(agent_want)
        max_turns = int(params.get("max_turns") or 0) or None
        model_want = str(params.get("model") or "").strip()

        from agent.agent_main import GenericAgent

        agent = GenericAgent(agent_id=agent_id) if agent_id else GenericAgent()
        llm = None
        if model_want:
            # 只换模型、复用该 Agent 自己的 key（fork 的既有语义），失败不让整步崩：
            # 桥建不起来时回落 Agent 主模型继续跑，并在输出里记一句。
            try:
                bridge = await agent._ensure_llm()
                llm = bridge.fork(model=model_want)
            except Exception as exc:  # noqa: BLE001 — 模型解析失败不该静默吞
                logger.warning("[workflow] run_session model=%s 解析失败，回落主模型: %s", model_want, exc)
        outputs = await agent.run_task(sop, max_turns=max_turns, llm=llm)
        return _session_result(outputs, agent_id)

    async def _pick_agent(self, want: str) -> int | None:
        """Resolve the step's agent: explicit id, or the first enabled agent
        visible to the run's owner (``any``)."""
        from db import PostgresClient

        if want and want not in ("any", "*"):
            try:
                return int(want)
            except ValueError:
                raise RuntimeError(f"run_session: bad agent_id {want!r}") from None
        if not PostgresClient.pool:
            raise RuntimeError("run_session: database unavailable, cannot pick an agent")
        async with PostgresClient.pool.acquire() as conn:
            if self._owner:
                row = await conn.fetchrow(
                    "SELECT id FROM agents WHERE user_id=$1 AND enabled=TRUE ORDER BY id LIMIT 1",
                    str(self._owner),
                )
            else:
                row = await conn.fetchrow(
                    "SELECT id FROM agents WHERE enabled=TRUE ORDER BY id LIMIT 1",
                )
        if not row:
            raise RuntimeError("run_session: no enabled agent available")
        return int(row["id"])

    # ── cache ─────────────────────────────────────────────────────────────────

    async def run_cache(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        """Read/write/delete Redis under the fixed ``wf:`` prefix.

        The user-facing key is always the text after ``wf:``; the platform's
        global ``prefix_key`` (REDIS_PREFIX_KEY) is added on top by the Redis
        wrapper itself, so a stored key reads ``<prefix>:wf:<key>``.
        """
        action = str(params.get("action") or "").strip().lower()
        key = str(params.get("key") or "").strip()
        if not key.startswith(CACHE_KEY_PREFIX):
            # Defense in depth — the store layer validated this at save time.
            raise RuntimeError(f"cache step: key must start with the fixed prefix {CACHE_KEY_PREFIX!r}")
        redis_key = key[len(CACHE_KEY_PREFIX):]
        from rd import JdbcClient

        redis = JdbcClient.redis
        if redis is None:
            raise RuntimeError("cache step: Redis is not available")
        if action == "get":
            value = await redis.get(redis_key)
            return {"status": "ok", "key": key, "value": _redis_json(value), "found": value is not None}
        if action == "set":
            value = params.get("value")
            ttl = int(params.get("ttl") or 0)
            payload = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
            if ttl > 0:
                await redis.set(redis_key, payload, ex=ttl)
            else:
                await redis.set(redis_key, payload)
            return {"status": "ok", "key": key, "value": value, "ttl": ttl or None}
        if action == "delete":
            deleted = await redis.delete(redis_key)
            return {"status": "ok", "key": key, "deleted": bool(deleted)}
        raise RuntimeError(f"cache step: unsupported action {action!r}")

    # ── select（分支取值：按 $item/上游值选一组常量）─────────────────────────

    async def run_select(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        """Pick the first case whose ``when`` holds, output its ``set`` object.

        ``when`` is evaluated against the *raw* node cases (same as an edge
        predicate — it references ``$item`` / ``$node.field`` and must not be
        interpolated first), while the chosen ``set`` comes from the already
        interpolated ``params`` so its values may carry ``$ref``s. No case
        matches → ``default`` (validate_def requires a default whenever there
        are cases). Used inside a foreach body to map the current element
        (e.g. an account) to a chosen proxy id / browser id for downstream steps.
        """
        from agent.workflow import eval_predicate

        cases_raw = node.get("cases") or []
        cases_val = params.get("cases") or []
        for i, case in enumerate(cases_raw):
            when = str((case or {}).get("when") or "").strip()
            try:
                hit = eval_predicate(when, ctx) if when else True
            except Exception as exc:  # noqa: BLE001 - 条件不可判定 = 不命中（与边 when 同口径）
                logger.warning("[workflow] select case %d when not evaluable: %s", i, exc)
                hit = False
            if hit and i < len(cases_val):
                return cases_val[i].get("set")
        return params.get("default") or node.get("default") or {}

    # ── http（发送请求，走代理池）────────────────────────────────────────────

    async def run_http(self, node: dict, ctx: WorkflowContext, params: dict) -> Any:
        """One outbound HTTP request through the shared proxy manager.

        ``proxy`` is the outbound route (already interpolated by the engine):
        ``""`` = direct, a proxy-pool id = that entry, ``$input.<key>`` resolved
        to a proxy-typed input's value = that entry. Exactly the routing the
        channel providers use — no new credentials.

        The response body/headers land in the node output as-is (downstream
        steps and the AI may need them); the log line deliberately carries no
        headers/body — only status and size.
        """
        url = str(params.get("url") or "").strip()
        if not url:
            raise RuntimeError("http step: url is required")
        method = str(params.get("method") or "GET").strip().upper()
        headers = params.get("headers")
        if not isinstance(headers, dict) or not headers:
            headers = None
        body = params.get("body")
        body_text = str(body) if body is not None else ""
        timeout_s = int(params.get("timeout") or 0) or 60
        proxy_id = str(params.get("proxy") or "").strip()

        import aiohttp

        from providers.proxy_manager import get_proxy_manager

        manager = get_proxy_manager()
        try:
            resp = await manager.request(
                url=url,
                method=method,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
                proxy_config_id=proxy_id,
                data=body_text if body_text else None,
            )
        except Exception as exc:
            # 网络层失败是节点数据（引擎标 failed），日志只带 status 不带原文——
            # 上游 URL/代理认证可能就在异常文案里。
            logger.warning("[workflow] http step failed status=exception type=%s", type(exc).__name__)
            raise
        async with resp:
            text = await resp.text()
            truncated = len(text) > MAX_NODE_OUTPUT_CHARS
            return {
                "status": resp.status,
                "headers": dict(resp.headers) if resp.headers else {},
                "body": text[:MAX_NODE_OUTPUT_CHARS],
                "truncated": truncated,
                "proxy": proxy_id or "direct",
            }


def _redis_json(value: Any) -> Any:
    """Decode a Redis string back to JSON when it parses; raw text otherwise."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _session_result(outputs: list[dict], agent_id: int | None) -> dict:
    """Uniform run_session output: final text + per-item tail for debugging."""
    final = ""
    for item in reversed(outputs or []):
        if isinstance(item, dict) and item.get("result") == "CURRENT_TASK_DONE":
            data = item.get("data")
            if isinstance(data, str) and data.strip():
                final = data
                break
    if not final and outputs:
        last = outputs[-1]
        final = str(last.get("data") or "") if isinstance(last, dict) else str(last)
    return {"status": "ok", "agent_id": agent_id, "result": final}


__all__ = ["WorkflowStepExecutors", "_session_result"]
