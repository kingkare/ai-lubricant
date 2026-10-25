"""Agent tool registry and sandboxed file/code tools.

Design: all tool methods can be sync or async. The execute() method
auto-detects and handles both transparently — AI tools don't need to
know the underlying implementation.

Sync tools (file_read, memory_*, scheduler_*): work with blocking I/O directly.
Async tools (code_run, web): run subprocesses/network on the event loop so
a conversation abort (task.cancel) can land at a real await point and kill children.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import mimetypes
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from agent.config import AgentConfig
from agent.jobs import JobRegistry
from agent.tool_spill import SPILL_DIRNAME, spill_result

logger = logging.getLogger(__name__)

# No grace period for abort: a stop-button press must take the child down
# immediately, not politely. The tree-kill helper handles both POSIX (killpg)
# and Windows (taskkill /T) to reach grandchildren that hold pipe handles.

async def _kill_subprocess_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the child **and its descendants**. Never raises, never blocks the loop.

    Why the whole tree: the foreground path wraps shell commands as
    ``cmd /c …`` (Windows) / ``/bin/sh -c …`` (POSIX), and the real work runs
    in a *grandchild*. Killing only the direct child leaves that grandchild
    holding the inherited stdout/stderr pipe handles open — asyncio's
    ``proc.wait()`` then never sees EOF and the abort blocks for the full grace
    period. This is the difference between a stop button that lands instantly
    and one that appears to do nothing.

    POSIX: the child is spawned with ``start_new_session=True``, so its pgid ==
    its pid and ``killpg`` reaches every descendant atomically and instantly.
    Windows: ``taskkill /T`` walks the tree (``/F`` forces; SIGKILL-equivalent)
    and takes ~1s wall-clock. It must complete **before** the direct
    ``proc.kill()`` below — killing the parent first orphans the grandchild and
    taskkill would then have no tree to walk. It runs as its own async subprocess
    (not ``subprocess.run``) so that ~1s never freezes other conversations.
    """
    try:
        if proc.returncode is not None:
            return  # already reaped — nothing to kill
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Belt-and-suspenders direct kill: after the tree-walk above it usually no-ops.
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


class WorkspaceSecurity:
    """Resolve and validate file paths against the configured workspace boundary."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._workspace_root = Path(config.workspace_root).resolve()
        self._allowed_roots = [Path(r).resolve() for r in config.allowed_roots]
        self._denied_patterns = [re.compile(p) for p in config.config.denied_patterns] if hasattr(config, 'config') else [re.compile(p) for p in config.denied_patterns]

    def memory_root(self) -> Path:
        """The Agent's read-only memory subtree (L1/L2/L3).

        ``file_write``/``file_patch`` must never touch this subtree; long-term
        memory is written only by ``start_long_term_update`` through file_memory.
        """
        return self._workspace_root / "memory"

    def is_memory_path(self, resolved: Path) -> bool:
        """True when ``resolved`` falls inside the read-only memory/ subtree."""
        try:
            return resolved.is_relative_to(self.memory_root().resolve())
        except (OSError, ValueError):
            return False

    def classify(self, rel_or_abs: str) -> tuple[Path, str]:
        """Resolve a path and report why it breaks the sandbox, without raising.

        Returns ``(resolved, reason)``; ``reason`` is ``""`` when the path is
        inside the sandbox. The reason strings are the ones ``resolve_path``
        embeds in its ``PermissionError``, so the two can never disagree about
        what counts as a boundary crossing.

        The non-raising form exists for the approval pre-scan: it has to ask
        "would this write cross the boundary?" for every call in a turn without
        tripping the exception path, and the answer must be exactly the one the
        tool gets at execution time.
        """
        p = Path(rel_or_abs)
        if not p.is_absolute():
            p = self._workspace_root / p
        resolved = p.resolve()

        path_str = str(resolved).replace("\\", "/")
        for pattern in self._denied_patterns:
            if pattern.search(path_str):
                return resolved, f"matches denied pattern '{pattern.pattern}'"

        if not any(resolved.is_relative_to(root) for root in self._allowed_roots):
            return resolved, "is outside allowed roots"

        return resolved, ""

    def resolve_path(self, rel_or_abs: str) -> Path:
        resolved, reason = self.classify(rel_or_abs)
        if reason:
            raise PermissionError(f"Path '{rel_or_abs}' {reason}")
        return resolved


ToolResult = dict[str, Any]

# 单个工具结果回灌进上下文的字符预算（对齐 GA _get_tool_maxlen 的量级）。
# 一轮里并发调 N 个工具时按 N 均分，否则 5 个 file_read 就能把窗口占满。
_TOOL_RESULT_MAXLEN = 15000
_TOOL_RESULT_MIN_MAXLEN = 2000
# 单行上限：压缩后的 JS bundle、base64、无换行日志会用一行吃掉整个预算。
_TOOL_RESULT_LINE_MAXLEN = 8000


def _elide_middle(text: str, max_len: int) -> str:
    """按头尾保留裁剪。

    退出码、异常栈、命令结尾的失败原因都在尾部，只留头部等于把最关键的证据丢掉。
    """
    if len(text) <= max_len:
        return text
    head = int(max_len * 0.6)
    tail = max_len - head
    dropped = len(text) - max_len
    return (
        f"{text[:head]}\n...[elided {dropped} chars; head+tail kept]...\n{text[-tail:]}"
    )


def _clamp_text(text: str, max_len: int) -> str:
    """先压超长单行，再对整体做头尾裁剪。"""
    if len(text) > _TOOL_RESULT_LINE_MAXLEN:
        lines = text.split("\n")
        if any(len(line) > _TOOL_RESULT_LINE_MAXLEN for line in lines):
            text = "\n".join(
                _elide_middle(line, _TOOL_RESULT_LINE_MAXLEN) for line in lines
            )
    return _elide_middle(text, max_len)


def _clamp_result(result: Any, max_len: int) -> Any:
    """把工具结果里的长字符串裁到预算内，保持结构不变。"""
    if isinstance(result, str):
        return _clamp_text(result, max_len)
    if isinstance(result, dict):
        # 逐个长字段裁剪：stdout/stderr/content 各自受限，而不是整体 JSON 一刀切，
        # 这样 status / exit_code 这类短字段永远不会被截掉。
        clamped = {}
        for key, value in result.items():
            clamped[key] = _clamp_result(value, max_len) if isinstance(
                value, (str, dict, list)
            ) else value
        return clamped
    if isinstance(result, list):
        if not result:
            return result
        per_item = max(_TOOL_RESULT_MIN_MAXLEN // 4, max_len // len(result))
        return [_clamp_result(item, per_item) for item in result]
    return result


class ToolContext:
    """Runtime context shared by all tools."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.security = WorkspaceSecurity(config)
        # 会话级停止信号（协作式取消）：abort 端点置位后，agent loop 在每次 LLM
        # 调用前/工具分发前检查它，code_run 在等待子进程时与它赛跑——置位即杀
        # 子进程树并 raise。这让 task.cancel() 不必靠 await 点投递 CancelledError
        # 才能停（被中间层吞掉或卡在同步子进程里时 cancel 永远等不到）。
        # None 表示无停止通道（后台任务、子 agent 测试桩），相应检查直接跳过。
        self.stop_event: asyncio.Event | None = None
        self.conversation_id: str | None = None

    def run_dir(self) -> Path:
        """code_run 的执行目录：直接用 Agent 自己的 workspace_root。

        相对路径与 file_read/file_write 同根解析，跑出来的产物 Agent 下一步
        就能读到，且天然按 Agent 隔离（不再共用全局 agent/temp）。
        """
        directory = Path(self.config.workspace_root or "agent/workspace").resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory


class ToolRegistry:
    """Register, describe and execute sandboxed agent tools.

    Supports both sync and async tool methods transparently.
    """

    def __init__(
        self,
        context: ToolContext,
        *,
        agent_id: int | None = None,
        owner_user_id: str | None = None,
        is_subagent: bool = False,
    ) -> None:
        self.context = context
        self.agent_id = agent_id
        # 子 agent 标记：_register_phase1 据此跳过 start_long_term_update（子只汇报，
        # 父蒸馏）；spawn_subagent 由主 agent 在 _ensure_tools 里注册，子不注册。
        self._is_subagent = bool(is_subagent)
        # 当前发起者（C 端用户 uid）：决定 AI 经 capability_call 建的定时任务等
        # 副作用产物的归属。对话路径传 caller，CDP 传客户端 owner，后台任务为 None
        # （由 add_job 兜底到 Agent owner）。
        self.owner_user_id = owner_user_id
        self._code_run_approval = None
        # 子 agent 权限模式回调（is_subagent 时由 _run_subagent_task 注入）：
        # 返回 "auto"（父 agent 自动批）/ "interactive"（上抛父对话流人工弹卡）。
        # 主 agent 此字段为 None——主 agent 直接用 _code_run_approval 协调器。
        self._subagent_permission_mode = None
        # Agent 级开关未开时的拒绝文案（CDP 网页对话读 agents.browser_code_run_enabled，
        # 未开就不挂审批协调器）。空串=用默认「需要交互审批」文案。
        self._code_run_denied_msg: str = ""
        # 越出沙箱的 file_write/file_patch 的一次性审批协调器（撞 allowed_roots /
        # denied_patterns 才用得上，工作区内写入不经过它）。
        self._file_write_approval = None
        self._file_write_denied_msg: str = ""
        # 工作流定义写操作（create/update/delete）的一次性审批协调器（图能执行
        # code_run / 写文件 / 驱动浏览器，改图=授权持久自动化）。list/get/run/runs/
        # run_status/cancel 是读或运行态，不经过它。
        self._workflow_approval = None
        self._mcp_runtime = None
        # Capability index produced by GenericAgent._attach_mcp_tools: each entry
        # describes one MCP service (name, methods, browser flag, sop_ref).
        self._capability_index: list[dict] = []
        # Name of the browser-class MCP service, if any (dynamic detection).
        self._browser_service: str | None = None
        # Background-job registry for code_run(background=true). Scoped to this
        # registry's run; closed in agent_runner_loop teardown. None only when a
        # caller explicitly disables jobs (e.g. some test fixtures) — code_run's
        # background branch then returns an error instead of spawning.
        self.jobs: JobRegistry | None = JobRegistry()
        self.tools: dict[str, Callable] = {}
        self._schemas: dict[str, dict] = {}
        # Turn counter mirrored from the loop (dispatch sets it). Lets turn-gated
        # tools like start_long_term_update reject calls made too early.
        self.current_turn: int = 0
        # 本轮 LLM 请求了几个工具（dispatch 设置）。结果预算按这个数均分。
        self.current_tool_num: int = 1
        self._register_phase1()

    def set_code_run_approval(self, approval) -> None:
        self._code_run_approval = approval

    def set_code_run_denial(self, msg: str) -> None:
        """设置 code_run 的禁用文案：不挂审批协调器时，denied 里带引导用户开配置的提示。"""
        self._code_run_denied_msg = msg

    def set_file_write_approval(self, approval) -> None:
        """挂上越界写入的一次性审批协调器（撞沙箱边界的 file_write/file_patch）。"""
        self._file_write_approval = approval

    def set_file_write_denial(self, msg: str) -> None:
        """设置越界写入的拒绝文案：不挂协调器时（无人值守）denied 里的提示。"""
        self._file_write_denied_msg = msg

    def set_workflow_approval(self, approval) -> None:
        """挂上工作流定义写操作（create/update/delete）的一次性审批协调器。

        没挂时写操作 fail closed（与 code_run / 越界写入同口径）；读与运行态
        （list/get/run/runs/run_status/cancel）不受影响。
        """
        self._workflow_approval = approval

    def set_mcp_runtime(
        self,
        mcp_runtime,
        *,
        capability_index: list[dict] | None = None,
        browser_service: str | None = None,
    ) -> None:
        """Inject the MCP manager plus the capability index used by web/capability_call."""
        self._mcp_runtime = mcp_runtime
        if capability_index is not None:
            self._capability_index = capability_index
        if browser_service is not None:
            self._browser_service = browser_service

    def register(
        self,
        name: str,
        func: Callable,
        schema: dict,
    ) -> None:
        self.tools[name] = func
        self._schemas[name] = schema

    async def execute(self, name: str, args: dict | None = None) -> ToolResult:
        """Run a tool by name. Auto-detects sync/async and handles both.

        - coroutine → await
        - async generator → collect all chunks
        - sync value → return directly
        """
        if name not in self.tools:
            return {"status": "error", "msg": f"Unknown tool: {name}"}
        try:
            result = self.tools[name](args or {})
            if inspect.iscoroutine(result) or inspect.isawaitable(result):
                result = await result
            elif inspect.isasyncgen(result):
                chunks = []
                async for chunk in result:
                    chunks.append(chunk)
                result = chunks
        except PermissionError as exc:
            return {"status": "error", "msg": str(exc)}
        except Exception as exc:
            logger.exception(f"Tool {name!r} raised {type(exc).__name__}")
            return {"status": "error", "msg": f"{type(exc).__name__}: {exc}"}
        # 每个结果都过预算裁剪：一个 20MB 的日志文件或一次全量 MCP 返回，
        # 在被回灌进 messages 之前就必须瘦身，否则 trim_messages 只能靠丢
        # 整条消息来救场。
        return self._clamp(result, tool_name=name)

    def _clamp(self, result: Any, tool_name: str = "") -> Any:
        budget = max(
            _TOOL_RESULT_MIN_MAXLEN,
            _TOOL_RESULT_MAXLEN // max(1, self.current_tool_num),
        )
        # Spill before eliding: oversized strings are saved whole to the agent
        # workspace and replaced with a preview + locator, so the model can
        # file_read the elided middle instead of losing it. Excluded tools
        # (file_read) and failed spills fall through to plain clamping.
        spill_dir = self._spill_dir()
        if spill_dir is not None:
            result = spill_result(
                result,
                spill_dir=spill_dir,
                tool_name=tool_name,
                budget=budget,
            )
        return _clamp_result(result, budget)

    def _spill_dir(self) -> str | None:
        """Workspace-adjacent spill directory, or None when unavailable.

        Uses the ToolContext workspace root (allowed_roots[0] when it exists), so
        spilled files stay inside the same sandbox boundary the model can read.
        """
        try:
            roots = self.context.config.allowed_roots
            root = roots[0] if roots else self.context.config.workspace_root
            if not root:
                return None
            return str(Path(root) / SPILL_DIRNAME)
        except Exception:  # noqa: BLE001 - best-effort
            return None

    def get_schema(self) -> list[dict]:
        return [self._schemas[name] for name in self.tools]

    def _register_phase1(self) -> None:
        # GA's atomic tools (names aligned with GA ch9). ``memory_read`` is gone:
        # the Agent's memory/ subtree is real files, read with file_read.
        # 子 agent（is_subagent=True）少注册 start_long_term_update：子只汇报、父蒸馏，
        # 子不应直接写共享 agent memory。spawn_subagent 由主 agent 在 _ensure_tools
        # 里注册（子也不注册）——子想委派只能写进结果上报父，父决定。
        self.register("code_run", self._code_run, _CODE_RUN_SCHEMA)
        self.register("file_read", self._file_read, _FILE_READ_SCHEMA)
        self.register("file_write", self._file_write, _FILE_WRITE_SCHEMA)
        self.register("file_patch", self._file_patch, _FILE_PATCH_SCHEMA)
        # web aggregates web_scan + web_execute_js (platform convergence; GA
        # exposes them as two independent tools).
        self.register("web", self._web, _WEB_SCHEMA)
        self.register("update_working_checkpoint", self._update_working_checkpoint, _UPDATE_WORKING_CHECKPOINT_SCHEMA)
        if not self._is_subagent:
            self.register("start_long_term_update", self._start_long_term_update, _START_LONG_TERM_UPDATE_SCHEMA)
        self.register("ask_user", self._ask_user, _ASK_USER_SCHEMA)
        # Platform extension: one entry for dynamic MCP + scheduler.
        self.register("capability_call", self._capability_call, _CAPABILITY_CALL_SCHEMA)
        # NOTE: background-process management is NOT a separate tool — it is
        # code_run itself, operation=list/output/kill (same one-tool-plus-
        # operation-enum convention as web). A standalone management tool would
        # collide with the scheduler's job_id vocabulary.
        # NOTE: Ralph is NOT a tool either — it is a driver-loop mode like goal
        # (agent/guardian.py next_ralph_round + agent/ralph.py round helpers);
        # a model-invoked tool would run its rounds inside one turn.
        # NOTE: spawn_subagent 由 GenericAgent._ensure_tools 注册（需回调 agent），
        # 不在此处注册；子 agent 永不注册它。

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _code_run(self, args: dict) -> ToolResult:
        """Execute approved code, or manage background processes it started.

        Two modes, chosen by ``operation``:
        - omitted (default) → run ``code`` (foreground blocks; ``background=true``
          spawns a tracked process and returns immediately). Requires approval.
        - ``list`` / ``output`` / ``kill`` → manage this run's background
          processes. These do NOT execute new code, so they skip the approval
          gate (approving an already-approved process's control is meaningless).

        Async so both branches are cancellable: the ``background=true`` branch can
        await the spawn, and the foreground path runs an async subprocess whose
        loop has real await points — a conversation stop-button (``task.cancel``)
        can land there and kill the child instead of blocking on a pinned event
        loop.
        """
        operation = str(args.get("operation") or "").strip().lower()
        if operation:
            return await self._code_run_manage(operation, args)

        if self._code_run_approval is None:
            return {
                "status": "denied",
                "code": "approval_required",
                "msg": self._code_run_denied_msg
                or "code_run requires an interactive one-time approval",
            }
        outcome, confirmation_id = self._code_run_approval.decision_for(args)
        if outcome != "allow":
            self._schedule_code_run_audit(args, confirmation_id, "denied")
            return {
                "status": "denied",
                "code": "approval_required",
                "confirmation_id": confirmation_id,
                "msg": "code_run was not approved",
            }
        code: str = args.get("code") or ""
        if not code:
            return {"status": "error", "msg": "code is required when running (omit operation, or pass a non-empty code)"}
        code_type: str = args.get("type", "python")
        timeout: int = args.get("timeout", 60)
        max_output: int = args.get("max_output_chars", 10000)
        background: bool = bool(args.get("background"))

        run_dir = self.context.run_dir()
        started = time.monotonic()

        # shell 类型：宿主系统原生 shell 跑一条命令（POSIX 用 /bin/sh，Windows 用
        # cmd）。不像 python/powershell 走临时脚本文件——命令就是代码本身。给 agent
        # 一个「一行命令」的最短路径，避免为了查文件存在/大小这类小事还写一整段 Python。
        if code_type == "shell":
            if sys.platform == "win32":
                cmd = ["cmd", "/c", code]
            else:
                cmd = ["/bin/sh", "-c", code]
            if background:
                return await self._start_background(cmd, args, confirmation_id)
            return await self._run_code_process(
                cmd, args, confirmation_id, started, timeout, max_output,
            )

        if code_type == "python":
            suffix = ".py"

            def build_cmd(filepath: Path) -> list[str]:
                return [sys.executable, "-X", "utf8", "-u", str(filepath)]

        elif code_type == "powershell":
            suffix = ".ps1"

            def build_cmd(filepath: Path) -> list[str]:
                import shutil
                pwsh = shutil.which("pwsh") or "powershell"
                return [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code]
        else:
            return {"status": "error", "msg": f"Unsupported code type: {code_type}"}

        # Background python/powershell: the interpreter must be able to read the
        # script after this call returns, so the temp file is NOT unlinked — the
        # JobRegistry owns it for the process's lifetime (it lives in the Agent's
        # workspace; foreground runs unlink theirs in the finally below).
        if background and code_type in ("python", "powershell"):
            fd, tmp_path_str = tempfile.mkstemp(suffix=suffix, dir=str(run_dir))
            os.close(fd)
            Path(tmp_path_str).write_text(code, encoding="utf-8")
            return await self._start_background(build_cmd(Path(tmp_path_str)), args, confirmation_id)

        fd, tmp_path_str = tempfile.mkstemp(suffix=suffix, dir=str(run_dir))
        os.close(fd)
        tmp_path = Path(tmp_path_str)

        try:
            if code_type == "python":
                tmp_path.write_text(code, encoding="utf-8")
                cmd = build_cmd(tmp_path)
            else:
                cmd = build_cmd(tmp_path)

            return await self._run_code_process(
                cmd, args, confirmation_id, started, timeout, max_output,
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    async def _start_background(
        self, cmd: list[str], args: dict, confirmation_id: str | None,
    ) -> dict:
        """Spawn an approved command as a background job (no timeout)."""
        if self.jobs is None:
            return {"status": "error", "msg": "background jobs are not available in this context"}
        try:
            job = await self.jobs.start(cmd, cwd=str(self.context.run_dir()))
        except Exception as exc:  # noqa: BLE001 - surface spawn failure as a tool error
            self._schedule_code_run_audit(args, confirmation_id, "error")
            return {"status": "error", "msg": f"failed to start background job: {exc}"}
        result = {
            "status": "ok",
            "background": True,
            "process_id": job.job_id,
            "cmd": " ".join(cmd)[:200],
            "msg": (
                "Background process started. Read its output with "
                f"code_run(operation='output', process_id='{job.job_id}'), list "
                "processes with code_run(operation='list'), and stop it with "
                "code_run(operation='kill')."
            ),
            "confirmation_id": confirmation_id,
        }
        self._schedule_code_run_audit(args, confirmation_id, "executed", result)
        return result

    async def _run_code_process(
        self, cmd: list[str], args: dict, confirmation_id: str | None,
        started: float, timeout: int, max_output: int,
    ) -> dict:
        """Run an approved code_run subprocess and return a uniform result dict.

        Shared by shell/python/powershell paths — the exact invocation differs per
        interpreter, but the run/audit/timeout handling is identical. On timeout the
        result is still audited so the approval registry can close out the turn.

        Async rather than ``subprocess.run``: a synchronous subprocess run pins the
        event loop for up to ``timeout`` seconds, during which the conversation's
        abort request cannot even be dispatched — let alone deliver its cancel to
        this task. Running the child on the loop keeps a stop-button press
        answerable at a real await point, and lets this coroutine take the child
        down with it instead of orphaning it.

        The child is spawned with ``start_new_session=True`` (POSIX) or wrapped in
        ``cmd /c`` (Windows). On POSIX we can kill the whole process group; on
        Windows we spawn ``taskkill /T /F`` to reach descendants. Either way the
        grandchild that holds the pipe handles dies instantly, so ``proc.wait()``
        returns and the stop button feels snappy.
        """
        # Spawn as a new session (POSIX) so we can killpg the whole tree.
        # Windows path doesn't need this because taskkill walks the tree anyway.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.context.run_dir()),
            start_new_session=sys.platform != "win32",
        )
        try:
            stdout_b, stderr_b = await self._communicate_with_stop_check(
                proc, timeout, confirmation_id, args
            )
        except asyncio.TimeoutError:
            # subprocess.run(timeout=...) semantics: kill the tree (awaiting it —
            # the turn is still healthy, correctness beats latency here) and
            # report the timeout shape so the approval registry can close out.
            await _kill_subprocess_tree(proc)
            duration_ms = int((time.monotonic() - started) * 1000)
            result = {
                "status": "timeout",
                "stdout": "",
                "stderr": f"Process timed out after {timeout}s",
                "exit_code": None,
                "duration_ms": duration_ms,
                "confirmation_id": confirmation_id,
            }
            self._schedule_code_run_audit(args, confirmation_id, "timeout", result)
            return result
        except asyncio.CancelledError:
            # Stop/abort mid-execution. Kill the tree WITHOUT awaiting taskkill:
            # taskkill takes ~1s and blocking the abort on it makes the stop
            # button feel sluggish. It runs detached and finishes the kill in the
            # background (no orphan leak); the sync proc.kill() below takes down
            # the direct child immediately. Then re-raise so run_agent marks the
            # message 已中止 and publishes aborted/end.
            await _kill_subprocess_tree(proc)
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")[:max_output]
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")[:max_output]
        result = {
            "status": "ok",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "confirmation_id": confirmation_id,
        }
        self._schedule_code_run_audit(args, confirmation_id, "executed", result)
        return result

    async def _communicate_with_stop_check(
        self, proc: asyncio.subprocess.Process, timeout: int,
        confirmation_id: str | None, args: dict,
    ) -> tuple[bytes, bytes]:
        """Wait for communicate(), racing against stop_event if set and timeout.

        Returns (stdout, stderr) on success. Raises TimeoutError on timeout,
        CancelledError when stop_event fires. Teardown (tree-kill) is left to
        the caller's exception handlers — killing here too would double-kill.
        The ``confirmation_id``/``args`` params are accepted for signature
        symmetry with the caller but unused (audit runs at the caller).
        """
        del confirmation_id, args
        # Race communicate() against stop_event if available. This is the
        # "cooperative stop" part: the user hits stop → event.set() → we wake
        # up and raise CancelledError → the caller kills the tree → run_agent
        # marks 已中止. Without this race the stop only lands when the current
        # subprocess finishes on its own (up to ``timeout`` seconds).
        wait_tasks: list[asyncio.Task] = []
        stop_task: asyncio.Task | None = None
        if self.context.stop_event is not None:
            stop_task = asyncio.create_task(self.context.stop_event.wait())
            wait_tasks.append(stop_task)
        comm_task = asyncio.create_task(proc.communicate())
        wait_tasks.append(comm_task)
        done, pending = await asyncio.wait(
            wait_tasks, return_when=asyncio.FIRST_COMPLETED, timeout=timeout
        )
        for t in pending:
            t.cancel()
        if not done:
            # Timeout (mirrors subprocess.run(timeout=...) → TimeoutExpired).
            raise asyncio.TimeoutError("code_run subprocess timed out")
        if stop_task is not None and stop_task in done:
            # stop_event fired: signal abort; caller tree-kills.
            raise asyncio.CancelledError("stop requested via cooperative event")
        # communicate() won: unpack results
        stdout_b, stderr_b = comm_task.result()
        return stdout_b, stderr_b

    async def _code_run_manage(self, operation: str, args: dict) -> ToolResult:
        """Manage this run's background processes (code_run operation=list/output/kill).

        These control processes code_run already approved and started, so they do
        not re-enter the approval gate — approving the act of reading a running
        process's output or stopping it would be meaningless.
        """
        if self.jobs is None:
            return {"status": "error", "msg": "background processes are not available in this context"}
        if operation == "list":
            return self.jobs.list()
        process_id = str(args.get("process_id") or "").strip()
        if not process_id:
            return {"status": "error", "msg": f"process_id is required for operation={operation}"}
        if operation == "output":
            return self.jobs.read(process_id, str(args.get("stream") or "stdout"))
        if operation == "kill":
            return await self.jobs.kill(process_id)
        return {"status": "error", "msg": f"Unsupported code_run operation: {operation}"}

    def _schedule_code_run_audit(
        self, args: dict, confirmation_id: str | None, outcome: str,
        result: dict | None = None,
    ) -> None:
        try:
            from agent.tool_audit import record_tool_audit

            asyncio.get_running_loop().create_task(record_tool_audit(
                agent_id=self.agent_id,
                conversation_id=getattr(self._code_run_approval, "conversation_id", ""),
                caller=getattr(self._code_run_approval, "caller", None),
                tool_name="code_run",
                payload={"type": args.get("type", "python"), "code": args.get("code", ""), "timeout": args.get("timeout", 60)},
                approval_id=confirmation_id,
                outcome=outcome,
                result=result,
            ))
        except RuntimeError:
            pass

    @staticmethod
    def _memory_read_hint(path_arg: str) -> str:
        """Nudge the model to pin down what it just read from memory/ or a SOP.

        Reading a SOP is worthless if the content scrolls out of context a few turns
        later and the model then works from a half-remembered version. Anchoring the
        key points (and the SOP's own path in ``related_files``) is what makes the
        turn-13 "re-read the relevant SOP" escalation actionable.
        """
        normalized = str(path_arg or "").replace("\\", "/").lower()
        if "memory/" not in normalized and "sop" not in normalized:
            return ""
        return (
            "You just read a memory/SOP file. If you intend to follow it, extract its "
            "key points (especially the later sections) into update_working_checkpoint "
            "and set related_files to this path, so the steps survive context "
            "compression instead of being recalled from memory."
        )

    def _file_read(self, args: dict) -> ToolResult:
        path = self.context.security.resolve_path(args["path"])
        start: int = args.get("start", 1)
        count: int = args.get("count", 200)
        keyword: str = str(args.get("keyword") or "").strip()
        hint = self._memory_read_hint(args.get("path", ""))

        if not path.exists():
            return {"status": "error", "msg": f"File not found: {path}"}

        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        total = len(lines)
        if keyword:
            # GA keyword anchor: jump to the first matching line so large files
            # (e.g. SOP bodies, logs) are read around the relevant section.
            hit = next(
                (i for i, line in enumerate(lines) if keyword.casefold() in line.casefold()),
                None,
            )
            if hit is None:
                return {
                    "status": "ok",
                    "content": "",
                    "lines": 0,
                    "total_lines": total,
                    "truncated": False,
                    "keyword": keyword,
                    "msg": f"keyword '{keyword}' not found",
                }
            begin = hit
            end = begin + count
            selected = lines[begin:end]
            result = {
                "status": "ok",
                "content": "".join(selected),
                "lines": len(selected),
                "total_lines": total,
                "truncated": end < total,
                "keyword": keyword,
                "start_line": begin + 1,
            }
            if hint:
                result["hint"] = hint
            return result
        begin = max(0, start - 1)
        end = begin + count
        selected = lines[begin:end]
        result = {
            "status": "ok",
            "content": "".join(selected),
            "lines": len(selected),
            "total_lines": total,
            "truncated": end < total,
        }
        if hint:
            result["hint"] = hint
        return result

    def _file_write(self, args: dict) -> ToolResult:
        # 用 classify 而不是 resolve_path：撞边界时不抛，而是把 resolved 和越界理由
        # 都拿回来，交由审批协调器裁决（挂了就弹一张卡，没挂就 fail closed）。
        path_str = str(args.get("path") or "")
        resolved, reason = self.context.security.classify(path_str)
        # memory/ 是结构性只读区，无视审批一律拒绝：长期记忆只能由 start_long_term_update
        # 经 file_memory 写入，放开它会绕过「No Execution, No Memory」的护栏。
        if self.context.security.is_memory_path(resolved):
            return {
                "status": "error",
                "msg": "memory/ 是只读区，请用 start_long_term_update 沉淀长期记忆",
            }
        confirmation_id: str | None = None
        if reason:
            outcome, confirmation_id = self._check_write_approval("file_write", args, reason)
            if outcome is not None:
                return outcome
        path = resolved
        content: str = args["content"]
        mode: str = args.get("mode", "overwrite")

        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        if mode == "append":
            with path.open("ab") as handle:
                handle.write(data)
        elif mode == "overwrite":
            path.write_bytes(data)
        else:
            return {"status": "error", "msg": f"Invalid write mode: {mode}"}
        if confirmation_id is not None:
            self._schedule_write_audit("file_write", args, confirmation_id, "executed")
        return {"status": "ok", "bytes_written": len(data)}

    def _file_patch(self, args: dict) -> ToolResult:
        path_str = str(args.get("path") or "")
        resolved, reason = self.context.security.classify(path_str)
        if self.context.security.is_memory_path(resolved):
            return {
                "status": "error",
                "msg": "memory/ 是只读区，请用 start_long_term_update 沉淀长期记忆",
                "replacements": 0,
            }
        confirmation_id: str | None = None
        if reason:
            outcome, confirmation_id = self._check_write_approval("file_patch", args, reason)
            if outcome is not None:
                return outcome
        path = resolved
        old_content: str = args["old_content"]
        new_content: str = args["new_content"]

        if not path.exists():
            return {"status": "error", "msg": f"File not found: {path}", "replacements": 0}

        text = path.read_text(encoding="utf-8")
        count = text.count(old_content)
        if count != 1:
            return {
                "status": "error",
                "msg": f"old_content is not unique (found {count} occurrences)",
                "replacements": count,
            }

        path.write_text(text.replace(old_content, new_content), encoding="utf-8")
        if confirmation_id is not None:
            self._schedule_write_audit("file_patch", args, confirmation_id, "executed")
        return {"status": "ok", "replacements": 1}

    def _check_write_approval(
        self, tool_name: str, args: dict, reason: str
    ) -> tuple[ToolResult | None, str | None]:
        """越界写入的审批闸门。

        返回 ``(拒绝结果, None)`` 表示工具应直接回这个结果、不落盘；``(None, cid)``
        表示本次一次性授权放行（``cid`` 供执行后补审计；工作区内写入没有审批则为
        ``(None, None)``）。

        挂了协调器：``decision_for`` 命中 allow 才放行（本次一次性授权，由 agent_loop 的
        on_tool_batch 预扫弹卡裁决）；deny 或 fail closed 都返回 denied 工具结果，不落盘。
        没挂协调器（定时任务、没有事件出口的调用方）：fail closed，与 code_run 同口径。
        """
        approval = self._file_write_approval
        if approval is None:
            return {
                "status": "denied",
                "code": "approval_required",
                "msg": self._file_write_denied_msg or (
                    f"写入越出 Agent 工作区沙箱（{reason}），需要交互式审批后才能执行"
                ),
            }, None
        outcome, confirmation_id = approval.decision_for(tool_name, args)
        if outcome == "allow":
            return None, confirmation_id
        self._schedule_write_audit(tool_name, args, confirmation_id, "denied")
        return {
            "status": "denied",
            "code": "approval_required",
            "confirmation_id": confirmation_id,
            "msg": f"写入越出 Agent 工作区沙箱（{reason}），本次未获授权",
        }, None

    def _schedule_write_audit(
        self, tool_name: str, args: dict, confirmation_id: str | None, outcome: str
    ) -> None:
        """Record an audit row for a sandbox-crossing write (executed or denied).

        越出 allowed_roots / 命中 denied_patterns 的写入是安全敏感动作，值得和 code_run
        同等的可追溯性：谁授权的、写了什么路径、裁决了什么。正文不入库，只存摘要哈希
        （与审批卡上回显给用户的一致，便于对账）。
        """
        try:
            from agent.tool_audit import record_tool_audit

            asyncio.get_running_loop().create_task(record_tool_audit(
                agent_id=self.agent_id,
                conversation_id=getattr(self._file_write_approval, "conversation_id", ""),
                caller=getattr(self._file_write_approval, "caller", None),
                tool_name=tool_name,
                payload={
                    "path": str(args.get("path") or ""),
                    "mode": str(args.get("mode") or "overwrite") if tool_name == "file_write" else "patch",
                },
                approval_id=confirmation_id,
                outcome=outcome,
            ))
        except RuntimeError:
            pass

    async def _present_attachment(self, item: dict) -> ToolResult:
        """Register a workspace file or public URL for ``ask_user`` display.

        Files only, by design — no inline base64. Tools that produce binary
        (CDP screenshots) already spill it to a workspace file and hand back a
        ``path``; see ``_persist_binary_result``. Keeping base64 out of tool
        results is what stops a screenshot from costing tens of thousands of
        tokens on every replayed turn.
        """
        path = str(item.get("path") or "").strip()
        url = str(item.get("url") or "").strip()
        name = str(item.get("name") or "").strip() or None
        mime_type = str(item.get("mime_type") or "").strip() or None

        if not path and not url:
            return {"status": "error", "msg": "path 或 url 至少传一个"}
        if url:
            if not url.lower().startswith(("http://", "https://")):
                return {"status": "error", "msg": "url 必须是 http(s) 公网地址"}
            return {
                "status": "ok", "kind": "url", "url": url,
                "name": name or url.rstrip("/").split("/")[-1] or "file",
                "mime_type": mime_type or mimetypes.guess_type(url)[0] or "application/octet-stream",
            }
        try:
            resolved = self.context.security.resolve_path(path)
        except PermissionError as exc:
            return {"status": "error", "msg": str(exc)}
        if not resolved.is_file():
            return {"status": "error", "msg": f"文件不存在：{path}"}
        display_name = name or resolved.name
        mime = mime_type or mimetypes.guess_type(display_name)[0] or "application/octet-stream"
        try:
            import attachment_store
            row = await attachment_store.register_attachment(
                source_path=str(resolved), name=display_name, mime_type=mime,
                owner_user_id=None, created_by_agent_id=self.agent_id,
                context_ref=None, persist=False,
            )
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            return {"status": "error", "msg": str(exc)}
        result = attachment_store.to_public_dict(row)
        result["kind"] = "attachment"
        return result

    # ------------------------------------------------------------------
    # Web tool (async) — aggregates scan + browser execution
    # ------------------------------------------------------------------

    # Browser primitives that identify a browser-class MCP service (GA's web
    # tools run against a real browser). Detection is by capability, not by a
    # hardcoded service name.
    _BROWSER_PRIMITIVES = frozenset({
        "evaluate_script", "navigate", "capture_screenshot",
        "capturescreenshot", "click", "get_url", "set_url",
        "list_tabs", "get_tabs", "screenshot",
        # Platform CDP bridge naming.
        "browser_execute_js", "browser_navigate", "browser_scan",
        "browser_get_tabs", "browser_screenshot", "browser_switch_tab",
    })

    async def _web(self, args: dict) -> ToolResult:
        """Unified web tool.

        - ``read_page`` (alias ``scan``): take a page's readable text. Default path
          is plain HTTP + text extraction. When a ``tab`` is given, route through
          the browser-class MCP service's DOM/text extraction instead (rendered,
          authenticated state).
        - every other operation (``execute``/``tabs``/``screenshot``/``navigate``/
          ``open_tab``/``close_tab``/``switch_tab``/``focus_tab``/``batch``/``wait``/
          ``watch_traffic``/``poll_traffic``/``unwatch_traffic``/``fill_credential``/
          ``totp_code``):
          requires a browser-class MCP service and is routed by
          :data:`_BROWSER_OPERATIONS`.
        """
        operation = str(args.get("operation") or "read_page").strip().lower()
        # ``scan`` was the original name and is a misnomer: it reads one page's
        # text, neither searching by keyword nor monitoring traffic — every alias
        # below keeps already-written prompts and saved workflows resolving.
        if operation in ("read_page", "scan"):
            # Public/static read stays on the plain HTTP path by default even
            # when a browser service happens to be bound. Supplying ``tab`` is
            # the explicit request for rendered/authenticated browser state.
            tab = str(args.get("tab") or "").strip()
            if tab:
                return await self._web_browser_scan(args, operation=operation)
            return await self._web_http_scan(args, operation=operation)
        if operation in _BROWSER_OPERATIONS:
            return await self._web_browser_call(operation, args)
        return {"status": "error", "msg": f"Unsupported web operation: {operation}"}

    async def _web_http_scan(self, args: dict, *, operation: str = "read_page") -> ToolResult:
        """Fetch a URL and return compressed semantic text (no browser).

        Mirrors GA's web_scan intent: never return raw HTML. Strip scripts,
        styles and tags, keep link anchors and visible text, cap output size.

        Uses aiohttp rather than a blocking ``urllib.urlopen``: the fetch can sit
        on the loop for up to ``timeout`` seconds, and a pinned loop would keep a
        conversation stop-button from landing anywhere. Cancelling the task tears
        the request down instead.
        """
        import aiohttp
        import html as html_mod

        url = str(args.get("url") or "").strip()
        if not url:
            return {"status": "error", "msg": "url 不能为空"}
        if not url.lower().startswith(("http://", "https://")):
            return {"status": "error", "msg": "url 必须是 http(s) 地址"}
        max_chars = int(args.get("max_chars") or 8000)
        timeout = int(args.get("timeout") or 20)
        try:
            session_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(
                timeout=session_timeout,
                headers={"User-Agent": "GenericAgent/1.0 web_scan"},
            ) as sess:
                async with sess.get(url) as resp:
                    charset = resp.charset or "utf-8"
                    # 2MB source cap — read only what we can surface.
                    raw = await resp.content.read(2 * 1024 * 1024)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"status": "error", "msg": f"请求失败：{type(exc).__name__}: {exc}"}
        raw = raw or b""
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
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
        truncated = len(text) > max_chars
        body = text[:max_chars]
        return {
            "status": "ok",
            "operation": operation,
            "url": url,
            "title": title[:200],
            "content": body,
            "chars": len(body),
            "truncated": truncated,
        }

    def _resolve_browser_service(self) -> dict | None:
        """Return the capability index entry for the browser-class MCP service."""
        if not self._capability_index:
            return None
        # Prefer an explicitly-tagged browser service, else one whose tool set
        # exposes a browser primitive.
        for entry in self._capability_index:
            if entry.get("browser"):
                return entry
        for entry in self._capability_index:
            names = {str(n).lower() for n in entry.get("method_names", [])}
            if names & self._BROWSER_PRIMITIVES:
                return entry
        return None

    async def _web_browser_scan(self, args: dict, *, operation: str = "read_page") -> ToolResult:
        """Read a live browser tab's rendered DOM/text (the ``tab`` branch of read_page)."""
        entry = self._resolve_browser_service()
        if entry is None:
            return {
                "status": "error",
                "code": "browser_mcp_not_configured",
                "msg": "未配置浏览器 MCP 服务，请在 Agent 配置中绑定 CDP 类 MCP",
            }
        url = str(args.get("url") or "").strip()
        tab = str(args.get("tab") or "").strip()
        # Prefer a dedicated text/dom tool if the service advertises one, else
        # fall back to evaluate_script returning document body text.
        advertised = {str(n).lower(): str(n) for n in entry.get("method_names", [])}
        preferred_order = ("browser_scan", "get_text", "scan", "extract_text", "get_dom")
        preferred = next((advertised[key] for key in preferred_order if key in advertised), None)
        tool = preferred or advertised.get("evaluate_script") or advertised.get("browser_execute_js")
        if tool is None:
            return {
                "status": "error",
                "code": "browser_mcp_not_configured",
                "msg": "浏览器 MCP 不提供 browser_scan/get_text/evaluate_script，无法扫描当前 tab",
            }
        call_args: dict = {}
        tool_key = str(tool).lower()
        if tool_key == "browser_scan":
            call_args["text_only"] = True
            if tab:
                call_args["switch_tab_id"] = tab
        else:
            if url:
                call_args["url"] = url
            if tab:
                # Platform CDP bridge calls this switch_tab_id; generic browser
                # services generally accept tab.
                call_args["switch_tab_id" if tool_key == "browser_execute_js" else "tab"] = tab
            if tool_key in {"evaluate_script", "browser_execute_js"}:
                call_args["script"] = "document.body.innerText"
        namespaced = f"{entry['service']}__{tool}"
        try:
            result = await self._mcp_runtime.call_tool(namespaced, call_args)
        except Exception as exc:
            return {"status": "error", "msg": f"{type(exc).__name__}: {exc}"}
        if isinstance(result, dict):
            # The tab branch reads the given tab only — a co-supplied ``url``
            # is not fetched (browser_scan has no url parameter). Surface that
            # instead of silently swallowing it.
            if url and tab:
                result.setdefault(
                    "warning",
                    f"url {url!r} ignored: a tab was given, so the tab's rendered DOM was read. "
                    "To fetch a URL, omit tab; to read an open tab, omit url.",
                )
            result.setdefault("operation", operation)
        return result

    # Result keys under which an MCP tool may hand back raw base64 bytes.
    _BINARY_RESULT_KEYS = ("base64", "data", "screenshot", "image")

    def _spill_binary_dict(
        self, result: dict, *, path_hint: str = "", default_stem: str = "shot",
    ) -> dict | None:
        """Persist a decoded binary result dict and return its path-only result.

        ``None`` means the dict did not contain a decodable binary payload. This
        helper deliberately owns only the binary conversion; MCP envelope
        unwrapping stays in ``_persist_binary_result``.
        """
        # device-control get_screen_state (spec §8.3) hands the screenshot back as
        # an OBJECT ``{format, w, h, b64}`` — the generic key scan below only
        # matches top-level STRING keys, so without this normalization the JPEG
        # bytes stayed nested and reached the model as text (no file, no
        # ask_user hint, page described from the tree instead of shown).
        shot = result.get("screenshot")
        if (
            isinstance(shot, dict)
            and isinstance(shot.get("b64"), str)
            and len(shot["b64"]) > 256
        ):
            result = {
                **{k: v for k, v in result.items() if k != "screenshot"},
                "format": str(shot.get("format") or "").strip().lower() or "jpeg",
                "base64": shot["b64"],
                "screenshot_w": shot.get("w"),
                "screenshot_h": shot.get("h"),
            }

        key = next(
            (k for k in self._BINARY_RESULT_KEYS
             if isinstance(result.get(k), str) and len(result[k]) > 256),
            None,
        )
        if key is None:
            return None
        payload = result[key]
        fmt = str(result.get("format") or "").strip().lower() or "png"
        try:
            import base64 as _b64
            raw = _b64.b64decode(payload, validate=True)
        except Exception:
            return None  # Not base64 after all — leave the original result intact.

        rel = str(path_hint or "").strip().replace("\\", "/")
        if not rel:
            rel = f"workspace/screenshots/{default_stem}_{int(time.time() * 1000)}.{fmt}"
        try:
            target = self.context.security.resolve_path(rel)
        except PermissionError as exc:
            return {"status": "error", "msg": str(exc)}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        except OSError as exc:
            return {"status": "error", "msg": f"写入截图失败: {exc}"}

        out = {k: v for k, v in result.items() if k != key}
        out["path"] = rel
        out["bytes"] = len(raw)
        out["mime_type"] = f"image/{'jpeg' if fmt in {'jpg', 'jpeg'} else fmt}"
        if "screenshot_w" in out or "screenshot_h" in out:
            out["screenshot_size"] = {
                "w": out.pop("screenshot_w", None),
                "h": out.pop("screenshot_h", None),
            }
        out.setdefault("status", "ok")
        out["hint"] = (
            f"Saved to {rel}. To show it to the user, call ask_user with "
            f"attachments=[{{\"path\": \"{rel}\"}}]."
        )
        return out

    def _persist_binary_result(
        self, result: Any, *, path_hint: str = "", default_stem: str = "shot",
    ) -> Any:
        """Land base64 bytes from an MCP result on disk; return path, not bytes.

        MCP transports may return the producer's dict directly, a JSON string,
        or an MCP ``content[].text`` envelope. Normalize all three before
        spilling, so a CDP screenshot's base64 cannot hide inside the envelope
        and reach the model context.
        """
        import json as _json

        # The in-process SSE gateway wraps ordinary tool results as
        # {content: [{type: "text", text: "{...}"}]}. If one content item is a
        # binary result, return the path-only result instead of the outer envelope.
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            for item in result["content"]:
                if not isinstance(item, dict):
                    continue
                value = item.get("text")
                if isinstance(value, str):
                    try:
                        parsed = _json.loads(value)
                    except Exception:
                        continue
                elif isinstance(value, dict):
                    parsed = value
                else:
                    continue
                if isinstance(parsed, dict):
                    spilled = self._spill_binary_dict(
                        parsed, path_hint=path_hint, default_stem=default_stem,
                    )
                    if spilled is not None:
                        return spilled
            return result

        # MCP handlers may hand back a JSON string instead of a dict. Parse it
        # here too, or a stringified screenshot would sail through untouched.
        if isinstance(result, str):
            stripped = result.lstrip()
            if not stripped.startswith("{"):
                return result
            try:
                result = _json.loads(result)
            except Exception:
                return result
            if not isinstance(result, dict):
                return result

        if not isinstance(result, dict):
            return result
        spilled = self._spill_binary_dict(
            result, path_hint=path_hint, default_stem=default_stem,
        )
        return spilled if spilled is not None else result

    async def _web_browser_call(self, operation: str, args: dict) -> ToolResult:
        """execute / tabs / screenshot — always via the browser MCP."""
        entry = self._resolve_browser_service()
        if entry is None:
            return {
                "status": "error",
                "code": "browser_mcp_not_configured",
                "msg": "未配置浏览器 MCP 服务，请在 Agent 配置中绑定 CDP 类 MCP",
            }
        advertised = {str(n).lower(): str(n) for n in entry.get("method_names", [])}
        spec = _BROWSER_OPERATIONS.get(operation)
        if spec is None:
            return {"status": "error", "msg": f"Unsupported web operation: {operation}"}
        canonical, alias_names, fallback_tab_arg = spec
        tool = advertised.get(canonical)
        if tool is None:
            # Tolerate minor naming variants across browser MCPs while preserving
            # the service's exact advertised method spelling.
            tool = next((advertised[n] for n in alias_names if n in advertised), None)
            if tool is None:
                return {
                    "status": "error",
                    "code": "browser_mcp_not_configured",
                    "msg": f"浏览器 MCP 不提供 {canonical}，请绑定支持该能力的浏览器 MCP",
                }
        namespaced = f"{entry['service']}__{tool}"
        tool_key = str(tool).lower()
        forward: dict = {}
        # Pass-through whitelist: only arguments the caller actually supplied are
        # forwarded, so the MCP method's own defaults survive. `path` is deliberately
        # absent — it addresses the local spill file, not the remote method.
        for key in _WEB_FORWARD_ARGS:
            if args.get(key) is not None:
                forward[key] = args[key]
        tab = str(args.get("tab") or "").strip()
        if tab:
            # Platform CDP names some tab arguments switch_tab_id, others tab_id.
            forward[_WEB_TAB_ARGS.get(tool_key, fallback_tab_arg) or "tab"] = tab
        try:
            result = await self._mcp_runtime.call_tool(namespaced, forward)
            if operation == "screenshot":
                result = self._persist_binary_result(
                    result, path_hint=str(args.get("path") or ""), default_stem="shot",
                )
            if isinstance(result, dict):
                result.setdefault("operation", operation)
            return result
        except Exception as exc:
            return {"status": "error", "msg": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------
    # Long-term memory tool (async) — GA start_long_term_update
    # ------------------------------------------------------------------

    async def _start_long_term_update(self, args: dict) -> ToolResult:
        """Trigger long-term memory distillation. The model never picks storage.

        Enforces GA's "No Execution, No Memory": ``verified_by`` must cite the
        tool result that confirmed the experience. The backend decides the
        landing layer — stable facts go to L2 ``global_mem.txt``; reusable
        procedures go to an L3 SOP file under ``memory/sop/``. Either way a short
        existence pointer is added to L1.

        GA also rejects distillation early in a task — a couple of turns in there
        is nothing yet worth pinning, and writing it would just pollute memory.
        """
        if self.agent_id is None:
            return {"status": "error", "msg": "start_long_term_update 需要在 Agent 上下文中调用"}
        if self.current_turn and self.current_turn < 10:
            return {
                "status": "error",
                "msg": (
                    "start_long_term_update 只用于完成一段较长任务（≥10 轮）后有长期价值的经验沉淀。"
                    "当前任务刚开始，还没有值得写进记忆的内容；继续执行，等确实摸出可复用的坑或事实再来。"
                ),
            }
        from agent import file_memory as fm

        summary = str(args.get("summary") or "").strip()
        if not summary:
            return {"status": "error", "msg": "summary 不能为空"}
        verified_by = str(args.get("verified_by") or "").strip()
        if not verified_by:
            return {"status": "error", "msg": "只有验证过的经验才能沉淀；请提供 verified_by（引用验证该经验的工具结果）"}
        reuse_reason = str(args.get("reuse_reason") or "").strip()
        evidence = args.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {}
        steps = evidence.get("steps") if isinstance(evidence.get("steps"), list) else []

        try:
            layer, ref = self._route_long_term_memory(summary, reuse_reason, steps, verified_by)
            if layer == "L3":
                title = reuse_reason or summary[:60]
                slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", title).strip(".-") or "sop"
                file_ref = f"{slug}.md"
                body_lines = [
                    f"# {title}",
                    "",
                    f"- verified_by: {verified_by}",
                    "- source: agent",
                    f"- reuse_reason: {reuse_reason or 'n/a'}",
                    "",
                    "## Summary",
                    "",
                    summary,
                ]
                if steps:
                    body_lines += ["", "## Steps", ""] + [f"{i+1}. {s}" for i, s in enumerate(steps)]
                fm.write_sop(self.agent_id, file_ref, "\n".join(body_lines) + "\n")
                fm.upsert_l1_pointer(self.agent_id, slug, f"memory/sop/{file_ref}")
                return {
                    "status": "ok",
                    "layer": "L3",
                    "ref": f"memory/sop/{file_ref}",
                    "msg": "已作为可复用流程写入 L3 SOP 并更新 L1 索引",
                }
            # L2 fact
            key = reuse_reason or summary[:60]
            fm.append_l2_fact(
                self.agent_id,
                key=key,
                value=summary,
                source="agent",
                verified=True,
            )
            fm.upsert_l1_pointer(self.agent_id, key, summary[:160])
            return {
                "status": "ok",
                "layer": "L2",
                "ref": "memory/global_mem.txt",
                "msg": "已作为稳定事实写入 L2 并更新 L1 索引",
            }
        except Exception as exc:
            logger.exception("start_long_term_update failed")
            return {"status": "error", "msg": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _route_long_term_memory(summary: str, reuse_reason: str, steps: list, verified_by: str) -> tuple[str, str]:
        """Backend decides L2 vs L3 (GA ch12). Heuristics:

        - explicit reuse_reason or ≥2 ordered steps → reusable procedure (L3);
        - otherwise → stable fact (L2).
        """
        if reuse_reason or len(steps) >= 2:
            return "L3", "memory/sop/"
        return "L2", "memory/global_mem.txt"

    # ------------------------------------------------------------------
    # Working memory tool — GA anchor (goal/completed/current_state/next_steps/...)
    # ------------------------------------------------------------------

    def _update_working_checkpoint(self, args: dict) -> ToolResult:
        """Update the Agent's working memory anchor (temporary, per-task).

        Fields mirror GA ch10's anchor: goal, completed, current_state,
        next_steps, key_info, related_files. ``related_files`` are file_read
        paths (e.g. ``memory/sop/x.md``) the loop re-reads after compression.
        """
        related_files = args.get("related_files")
        if not isinstance(related_files, list):
            related_files = []
        # Keep only string paths; they are file_read targets, not arbitrary text.
        related_files = [str(p).replace("\\", "/").strip() for p in related_files if str(p).strip()]
        return {
            "status": "ok",
            "goal": str(args.get("goal") or "").strip(),
            "completed": args.get("completed") if isinstance(args.get("completed"), list) else [],
            "current_state": str(args.get("current_state") or "").strip(),
            "next_steps": args.get("next_steps") if isinstance(args.get("next_steps"), list) else [],
            "key_info": str(args.get("key_info") or "").strip(),
            "related_files": related_files,
            "msg": "Working checkpoint updated. It will be injected into subsequent turns.",
        }

    # ------------------------------------------------------------------
    # capability_call — single entry for dynamic MCP + scheduler (async)
    # ------------------------------------------------------------------

    async def _capability_call(self, args: dict) -> ToolResult:
        """Route a capability by dotted name.

        - ``workflow.list|get|run|runs|run_status|cancel`` → workflow store/service
          (free to read and drive runs); ``workflow.create|update|delete`` manage
          the graph itself but require a one-time approval card — a workflow is a
          user-owned artifact that can run code, write files and drive browsers.
        - ``<service>.<method>`` → the Agent's bound MCP service (the service
          index is in the system prompt). On first use of an MCP service, a
          skeleton ``memory/sop/mcp/<service>_sop.md`` is generated and an L1
          pointer added so the experience can accumulate.
        """
        name = str(args.get("name") or "").strip()
        if not name:
            return {"status": "error", "msg": "capability_call 需要 name=<scope>.<method>"}
        call_args = args.get("args") or {}
        if not isinstance(call_args, dict):
            return {"status": "error", "msg": "capability_call 的 args 必须是对象"}

        scope = name.split(".", 1)[0].lower()
        if scope == "workflow":
            return await self._workflow_dispatch(name, call_args)

        # MCP service.method
        if "." not in name:
            return {"status": "error", "msg": f"未知的 capability: {name}（应为 <service>.<method> / workflow.<action>）"}
        service, method = name.split(".", 1)
        entry = next((e for e in self._capability_index if e.get("service") == service), None)
        if entry is None:
            return {"status": "error", "code": "capability_not_found", "msg": f"未绑定 MCP 服务: {service}"}
        if self._mcp_runtime is None:
            return {"status": "error", "msg": "MCP runtime 未挂载"}
        await self._maybe_seed_mcp_sop(service, entry)
        # 按方法 schema 先做 required 缺参校验：直接放行会换来一句 Python 的
        # ``fn() missing 1 required positional argument: 'script'``——参数名在消息
        # 末尾，模型（尤其中文提示词下）常把它当上游异常重发同样的空 args 死循环
        # （CDP 网页对话曾连发 5 次 {"args":{},"name":"cdp-bridge.browser_execute_js"}）。
        # 校验消息直接给出「缺哪些参数 + 该方法的完整参数清单」，一轮即可纠正。
        missing = self._missing_required_params(entry, method, call_args)
        if missing:
            params_hint = self._format_mcp_params(self._method_schema(entry, method)) or "    - (无参数)"
            return {
                "status": "error",
                "code": "missing_required_params",
                "msg": (
                    f"{service}.{method} 缺少必填参数: {', '.join(missing)}。"
                    "这不是上游故障，不要原样重试；把缺的参数补进 args 后再调用。"
                    f"该方法全部参数：\n{params_hint}"
                ),
            }
        namespaced = f"{service}__{method}"
        try:
            result = await self._mcp_runtime.call_tool(namespaced, call_args)
        except TypeError as exc:
            # 防御性兜底：schema 没声明 required 但目标函数签名必填（或模型传了
            # 不存在的形参）。TypeError 原文是 Python 视角，翻成模型可行动的口径。
            return {
                "status": "error",
                "code": "bad_arguments",
                "msg": f"{service}.{method} 参数不符: {exc}。请对照方法参数清单修正 args，不要原样重试。",
            }
        except Exception as exc:
            return {"status": "error", "msg": f"{type(exc).__name__}: {exc}"}
        # Any MCP method may hand back inline binary (CDP screenshots do). Spill it
        # to a file here so the base64 never reaches the model's context — the same
        # guard as the web(screenshot) path, since both funnel through call_tool.
        return self._persist_binary_result(
            result, path_hint=str(call_args.get("path") or ""), default_stem=method,
        )
    async def _workflow_dispatch(self, name: str, args: dict) -> ToolResult:
        """workflow.* —— 模型对工作流的**管理面**（读/运行态自由，写定义走审批）。

        工作流是**用户资产**：怎么执行由图自己的步骤决定（每步自带浏览器/MCP/
        目标 Agent 的选择），模型不参与编排。但建模可以交给模型——一条图的落库
        要经过用户的一次性审批（图能执行 code_run / 写文件 / 驱动浏览器，改图
        = 授权一段持久自动化）：

        - 读 / 启动：``list`` / ``get`` / ``run``；
        - 运行态：``runs``（历史）/ ``run_status``（单 run 全字段）/ ``cancel``；
        - 写定义（走审批卡）：``create`` / ``update`` / ``delete``。

        审批闸门：挂了协调器时 ``decision_for`` 命中 allow 才落库（一次性授权，
        由 agent_loop 的 on_tool_batch 预扫弹卡裁决）；没挂（无人值守）或对不上
        → denied，与 code_run / 越界 file_write 同口径 fail closed。
        """
        from agent import workflow_store

        action = name.split(".", 1)[1].lower() if "." in name else ""
        owner = self.owner_user_id

        async def _visible(row: dict | None) -> bool:
            """def 是否对该用户可见：owner 匹配（管理员 owner=None 全可见）。"""
            if not row:
                return False
            if owner is None:
                return True
            row_owner = row.get("user_id")
            return row_owner is None or str(row_owner) == str(owner)

        if action == "list":
            rows = await workflow_store.list_defs(owner)
            return {"status": "ok", "workflows": [
                {"id": r["id"], "name": r["name"], "label": r.get("label", ""),
                 "description": r.get("description", ""),
                 "version": r["version"], "node_count": len((r.get("definition") or {}).get("nodes") or [])}
                for r in rows
            ]}
        if action == "get":
            try:
                def_id = int(args.get("def_id"))
            except (TypeError, ValueError):
                return {"status": "error", "msg": "workflow.get 需要 def_id"}
            row = await workflow_store.get_def(def_id, version=args.get("version"))
            if not await _visible(row):
                return {"status": "error", "msg": f"workflow {args.get('def_id')} not found"}
            # PG 行带 datetime（created_at/updated_at）：不落成字符串的话，下游
            # json.dumps 工具结果时会炸 "Object of type datetime is not JSON
            # serializable"。这里先转成 JSON 安全的纯 dict。
            safe = json.loads(json.dumps(dict(row), ensure_ascii=False, default=str))
            return {"status": "ok", "workflow": safe}

        if action in ("runs", "run_status", "cancel"):
            return await self._workflow_run_state(action, args, _visible)

        if action in ("create", "update", "delete"):
            return await self._workflow_mutate(action, args, _visible)

        if action == "run":
            try:
                def_id = int(args.get("def_id"))
            except (TypeError, ValueError):
                return {"status": "error", "msg": "workflow.run 需要 def_id"}
            row = await workflow_store.get_def(def_id)
            if not await _visible(row):
                return {"status": "error", "msg": f"workflow {args.get('def_id')} not found"}
            from agent import workflow_service

            try:
                run_id = await workflow_service.start_and_run(
                    def_id,
                    args=args.get("args") or {},
                    version=args.get("version"),
                    trigger="agent",
                    caller=self.owner_user_id,
                    # 命名一个 cron 启动方式：其 params（含 account_list 扇出）参与供值。
                    # SOP 早就写了 trigger_id，这里漏传=按 agent 隐式跑（不扇出、不解析账号列表）。
                    trigger_id=args.get("trigger_id"),
                )
            except ValueError as exc:
                return {"status": "error", "msg": str(exc)}
            return {
                "status": "ok", "run_id": run_id,
                "msg": (
                    f"Workflow run {run_id} started in the background. Poll it with "
                    f"workflow.run_status(run_id={run_id}) or wait for the completion "
                    "message; do not re-run it."
                ),
            }
        return {"status": "error", "msg": f"未知的 workflow 动作: {action}"}

    async def _workflow_run_state(self, action: str, args: dict, _visible) -> ToolResult:
        """运行态三动作：runs / run_status / cancel（读+控制，不进审批卡）。

        cancel 类比 code_run 的进程管理操作（kill/output）：取消一个已经批准启动
        的 run 不扩大授权面，故不弹卡。
        """
        from agent import workflow_store

        if action == "runs":
            try:
                def_id = int(args.get("def_id"))
            except (TypeError, ValueError):
                return {"status": "error", "msg": "workflow.runs 需要 def_id"}
            if not await _visible(await workflow_store.get_def(def_id)):
                return {"status": "error", "msg": f"workflow {args.get('def_id')} not found"}
            try:
                limit = max(1, min(50, int(args.get("limit") or 10)))
            except (TypeError, ValueError):
                limit = 10
            rows = await workflow_store.list_runs(def_id, limit=limit)
            # datetime 字段 JSON-safe（与 workflow.get 同口径）。
            safe = json.loads(json.dumps({"runs": rows}, ensure_ascii=False, default=str))["runs"]
            return {"status": "ok", "def_id": def_id, "runs": safe}

        try:
            run_id = int(args.get("run_id"))
        except (TypeError, ValueError):
            return {"status": "error", "msg": f"workflow.{action} 需要 run_id"}
        run = await workflow_store.get_run(run_id)
        if not run:
            return {"status": "error", "msg": f"workflow run {args.get('run_id')} not found"}
        # 可见性按 run 所属的 def 判（家族口径 get_run 已给 def_id 版本行 id）。
        if not await _visible(await workflow_store.get_def(int(run["def_id"]))):
            return {"status": "error", "msg": f"workflow run {args.get('run_id')} not found"}

        if action == "run_status":
            safe = json.loads(json.dumps(dict(run), ensure_ascii=False, default=str))
            return {"status": "ok", "run": safe}

        # cancel：本进程在跑的 asyncio task；已结束/跨进程返回 cancelled=false。
        from agent import workflow_service

        cancelled = workflow_service.cancel_run(run_id)
        return {"status": "ok", "run_id": run_id, "cancelled": bool(cancelled)}

    async def _workflow_mutate(self, action: str, args: dict, _visible) -> ToolResult:
        """create / update / delete —— 定义写操作：先验参，再过审批闸门落库。

        参数畸形（缺 name / 空图 / 缺 def_id）直接回 error——模型能修，不该烧一
        张审批卡；修好重发会生成新卡（digest 变了）。
        """
        from agent import workflow_store

        def_id: int | None = None
        if action in ("create", "update"):
            definition = args.get("definition")
            if not isinstance(definition, dict) or not (definition.get("nodes") or []):
                return {"status": "error", "msg": f"workflow.{action} 的 definition 必须是非空图（含 nodes）"}
            if action == "create":
                if not str(args.get("name") or "").strip():
                    return {"status": "error", "msg": "workflow.create 需要 name（机器名，ASCII，owner 内唯一）"}
            else:
                try:
                    def_id = int(args.get("def_id"))
                except (TypeError, ValueError):
                    return {"status": "error", "msg": "workflow.update 需要 def_id"}
        else:  # delete
            try:
                def_id = int(args.get("def_id"))
            except (TypeError, ValueError):
                return {"status": "error", "msg": "workflow.delete 需要 def_id"}

        # 归属先于审批：别人的 def 直接 error（与 get/run 同口径，不弹卡）。
        if def_id is not None and not await _visible(await workflow_store.get_def(def_id)):
            return {"status": "error", "msg": f"workflow {def_id} not found"}

        outcome, confirmation_id = self._check_workflow_approval(action, args)
        if outcome is not None:
            return outcome

        try:
            if action == "create":
                # agent 建图不给空壳：空壳只服务 UI「先建壳、再填节点」的建表单流程。
                row = await workflow_store.create_def(
                    str(args.get("name")).strip(),
                    str(args.get("description") or ""),
                    args["definition"],
                    user_id=self.owner_user_id,
                    label=str(args.get("label") or ""),
                    retry_on_reboot=bool(args.get("retry_on_reboot", True)),
                )
                self._schedule_workflow_audit(action, args, confirmation_id, "executed")
                return {
                    "status": "ok", "id": row["id"], "version": row["version"],
                    "name": row["name"], "label": row.get("label", ""),
                    "msg": "工作流已创建（用户已授权）。可继续用 workflow.update 改图。",
                }

            if action == "update":
                row = await workflow_store.update_def(
                    def_id, args["definition"],
                    description=args.get("description"),
                    label=args.get("label"),
                    retry_on_reboot=args.get("retry_on_reboot"),
                )
                # 定时（triggers）随定义保存：幂等重载该 def 的 job，避免「编辑完
                # 定时没生效」。重载失败不影响保存本身。
                try:
                    from agent.workflow_scheduler import get_workflow_scheduler

                    sched = get_workflow_scheduler()
                    sched.start()
                    await sched.reload_def(def_id)
                except Exception:  # noqa: BLE001 — 重载失败不影响保存
                    pass
                self._schedule_workflow_audit(action, args, confirmation_id, "executed")
                return {
                    "status": "ok", "id": row["id"], "version": row["version"],
                    "name": row["name"], "label": row.get("label", ""),
                    "msg": "工作流已保存为新版本。",
                }

            # delete：删整串版本行（运行历史级联走）。调度器里的 cron job 也要摘掉，
            # 否则「删掉的工作流继续定时点火」。
            ok = await workflow_store.delete_def(def_id)
            try:
                from agent.workflow_scheduler import get_workflow_scheduler

                await get_workflow_scheduler().remove(def_id)
            except Exception:  # noqa: BLE001 — 摘 job 失败不影响删除本身
                pass
            self._schedule_workflow_audit(action, args, confirmation_id, "executed")
            return {"status": "ok", "def_id": def_id, "deleted": bool(ok)}
        except ValueError as exc:
            # 定义静态校验失败 / name 撞车：把原文回给模型，它能据此修图重交
            # （重新提交会生成新的一次性审批卡）。
            self._schedule_workflow_audit(action, args, confirmation_id, "error")
            return {"status": "error", "msg": str(exc)}

    def _check_workflow_approval(self, action: str, args: dict) -> tuple[ToolResult | None, str | None]:
        """定义写操作的审批闸门（照 ``_check_write_approval``）。

        返回 ``(拒绝结果, None)`` 表示直接回这个结果、不落库；``(None, cid)`` 表示
        本次一次性授权放行（``cid`` 供执行后补审计）。
        """
        approval = self._workflow_approval
        if approval is None:
            return {
                "status": "denied",
                "code": "approval_required",
                "msg": (
                    f"workflow.{action} 需要交互式审批（无人值守时不可用）。"
                    "工作流图能执行 code_run / 写文件 / 驱动浏览器，改图须由用户授权。"
                ),
            }, None
        outcome, confirmation_id = approval.decision_for(action, args)
        if outcome == "allow":
            return None, confirmation_id
        self._schedule_workflow_audit(action, args, confirmation_id, "denied")
        return {
            "status": "denied",
            "code": "approval_required",
            "confirmation_id": confirmation_id,
            "msg": f"workflow.{action} 本次未获授权",
        }, None

    def _schedule_workflow_audit(
        self, action: str, args: dict, confirmation_id: str | None, outcome: str,
    ) -> None:
        """记一条工作流写操作审计（executed / denied / error）。

        只存摘要哈希（与审批卡回显一致，便于对账），definition 全文不入库。
        """
        try:
            from agent.workflow_approval import mutation_digest
            from agent.tool_audit import record_tool_audit

            payload = {
                "action": action,
                "def_id": args.get("def_id"),
                "name": str(args.get("name") or ""),
                "digest": mutation_digest(action, args),
            }
            asyncio.get_running_loop().create_task(record_tool_audit(
                agent_id=self.agent_id,
                conversation_id=getattr(self._workflow_approval, "conversation_id", ""),
                caller=getattr(self._workflow_approval, "caller", None),
                tool_name=f"workflow.{action}",
                payload=payload,
                approval_id=confirmation_id,
                outcome=outcome,
            ))
        except Exception:  # noqa: BLE001 — 审计失败绝不改工具结果
            logger.debug("[agent] workflow audit skipped", exc_info=True)


    @staticmethod
    def _method_schema(entry: dict, method: str) -> Any:
        """Return the input_schema advertised for one method, or None."""
        for m in entry.get("methods") or []:
            if isinstance(m, dict) and str(m.get("name") or "") == method:
                return m.get("input_schema")
        return None

    @classmethod
    def _missing_required_params(cls, entry: dict, method: str, call_args: dict) -> list[str]:
        """List required-but-absent params per the method's advertised schema.

        空值（``""``/``None``）不算缺：有些方法允许显式传空串。schema 缺失或没有
        required 时返回空列表——校验是尽力而为，不替代真正的 schema 验证。
        """
        schema = cls._method_schema(entry, method)
        if not isinstance(schema, dict):
            return []
        required = schema.get("required")
        if not isinstance(required, list):
            return []
        return [str(r) for r in required if str(r) not in call_args]

    @staticmethod
    def _format_mcp_params(schema: Any) -> str:
        """Render an MCP inputSchema as compact per-parameter lines.

        Without this the SOP lists only method names, so the model has to guess
        argument names on a first call and learn them from error replies. The
        schema is the one piece of information it cannot derive on its own.
        """
        if not isinstance(schema, dict):
            return "    - (no parameter schema advertised)"
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return "    - (no parameters)"
        required = schema.get("required")
        required_set = {str(r) for r in required} if isinstance(required, list) else set()
        lines: list[str] = []
        for key, spec in properties.items():
            spec = spec if isinstance(spec, dict) else {}
            kind = str(spec.get("type") or "any")
            flag = "required" if str(key) in required_set else "optional"
            desc = str(spec.get("description") or "").strip().splitlines()
            tail = f" — {desc[0]}" if desc and desc[0] else ""
            enum = spec.get("enum")
            if isinstance(enum, list) and enum:
                tail += f" (one of: {', '.join(str(v) for v in enum[:8])})"
            lines.append(f"    - `{key}` ({kind}, {flag}){tail}")
        return "\n".join(lines)

    async def seed_mcp_sops(self) -> None:
        """Seed a usage SOP for every bound MCP service, before any of them is called.

        The system prompt advertises each service with an ``(SOP: ...)`` pointer; if the
        file only appeared after the first call, that pointer dangled exactly when the
        model most needed it — on the call it had no parameter information for.

        Browser-class services are skipped: they are intentionally unadvertised (every
        method lives behind a ``web`` operation), and an L1 ``mcp.<service>`` pointer
        would lead the model straight back to the capability_call spelling this skips.
        """
        for entry in list(self._capability_index or []):
            service = entry.get("service")
            if not service:
                continue
            if entry.get("browser"):
                self._retire_browser_service_sop(str(service))
                continue
            await self._maybe_seed_mcp_sop(str(service), entry)

    def _retire_browser_service_sop(self, service: str) -> None:
        """Drop the seeded usage SOP of an unadvertised browser-class service.

        Browser services are hidden from every capability display, so an L1
        ``mcp.<service>`` pointer would route the model straight back to the
        capability_call spelling this hides. Deleting the file is enough: L1
        rebuilt by ``ensure_agent_memory`` drops pointers whose target is gone.

        Only the auto-generated skeleton is deleted. If the agent has distilled
        real experience into it, the file (and its pointer) stay — accumulated
        knowledge beats display tidiness, and the service remains callable.
        """
        if self.agent_id is None:
            return
        try:
            from agent import file_memory as fm

            path = fm.agent_sop_root(self.agent_id) / "mcp" / f"{service}_sop.md"
            if not path.is_file():
                return
            body = path.read_text(encoding="utf-8", errors="replace")
            experience = body.split("## Experience", 1)[-1].strip().lower() if "## Experience" in body else ""
            if experience.startswith(("none yet", "暂无")) or not experience:
                path.unlink()
        except Exception as exc:  # noqa: BLE001 — retirement must never block the Agent
            logger.debug("[sop] retire browser sop failed service=%s: %s", service, exc)

    async def _maybe_seed_mcp_sop(self, service: str, entry: dict) -> None:
        """Generate a skeleton MCP usage SOP with the service's parameter schemas.

        Seeded before the first call (from ``set_mcp_runtime``) rather than after it,
        so the L1 pointer the model routes on leads to something executable. The
        Experience section stays empty until a verified run distils into it.

        已存在但缺参数块（早期版本只写方法名+描述，没写 ``_format_mcp_params`` 的
        行）时**补写**：场景段把本文件整体内联进系统提示，没有参数清单的 SOP 会
        让模型只能拿方法名猜 args——CDP 网页对话曾因此连发 5 次空
        ``args={"script":...}`` 缺参调用。补写只动 methods 区，Experience 区
        （人工/AI 沉淀的经验）原样保留。
        """
        if self.agent_id is None:
            return
        if entry.get("browser"):
            # Unadvertised service: never (re)seed a pointer to it, even when the
            # model reaches it through capability_call by an old habit.
            self._retire_browser_service_sop(service)
            return
        try:
            from agent import file_memory as fm
            ref = f"mcp/{service}_sop.md"
            sop_root = fm.agent_sop_root(self.agent_id)
            target = sop_root / "mcp" / f"{service}_sop.md"
            method_lines = self._render_sop_methods(service, entry)
            if target.exists():
                self._backfill_sop_params(target, method_lines)
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            body = (
                f"# {service} MCP SOP\n\n"
                f"## Service\n\n{service}\n\n"
                "## Invocation\n\n"
                "MCP methods are not first-class tools. Call them through "
                f"`capability_call(name=\"{service}.<method>\", args={{...}})`.\n\n"
                f"## Available methods\n\n{method_lines}\n\n"
                "## Experience\n\n"
                "None yet. After a verified successful call, use start_long_term_update "
                "to distil what mattered here (required argument combinations, "
                "preconditions, pitfalls) — not a restatement of the schema above.\n"
            )
            target.write_text(body, encoding="utf-8")
            fm.upsert_l1_pointer(self.agent_id, f"mcp.{service}", f"memory/sop/{ref}")
        except Exception as exc:  # noqa: BLE001 — SOP seeding must never block the call
            logger.debug("[sop] seed mcp sop failed service=%s: %s", service, exc)

    def _render_sop_methods(self, service: str, entry: dict) -> str:
        """Render the SOP's method list block: one bullet per method + params."""
        methods = entry.get("methods") or []
        blocks: list[str] = []
        for m in methods:
            name = m.get("name")
            raw_desc = (m.get("description") or "").strip().splitlines()
            summary = raw_desc[0] if raw_desc else ""
            blocks.append(
                f"- `capability_call(name=\"{service}.{name}\", args={{...}})`"
                + (f": {summary}" if summary else "")
                + "\n"
                + self._format_mcp_params(m.get("input_schema"))
            )
        return "\n".join(blocks) or "- (no methods discovered)"

    def _backfill_sop_params(self, target, method_lines: str) -> None:
        """Patch a stale seeded SOP that predates parameter blocks.

        早期 seed 版本只写方法名+描述。文件已存在时 ``_maybe_seed_mcp_sop``
        直接 return，于是老 Agent 的 SOP 永远没有参数清单，而场景段把它整体
        内联进系统提示——模型只能拿方法名猜 args。这里检测「methods 区缺少
        参数行」并原位补齐；Experience 区不动。
        """
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return
        # 新版 seed 的 methods 区每个方法下面有 "    - `param`" 参数行；老版没有。
        if re.search(r"^- `capability_call\(name=.*\n    - `", text, flags=re.MULTILINE | re.DOTALL):
            return
        # 只重写 methods 区：## Available methods 到下一个 ## 之间。
        patched = re.sub(
            r"(## Available methods\n\n)([\s\S]*?)(\n\n## )",
            lambda m: m.group(1) + method_lines + m.group(3),
            text,
            count=1,
        )
        if patched == text:
            return
        try:
            target.write_text(patched, encoding="utf-8")
        except OSError:
            return

    async def _ask_user(self, args: dict) -> "StepOutcome":
        """Interrupt execution to ask the user questions, optionally with
        attachments (merged from the old ``show_file`` tool).

        Accepts the new multi-question shape (``questions``: [{question, options,
        multi_select, allow_custom, recommended, reason}]) and normalizes the legacy
        single-question shape (``question`` + ``candidates``) into one entry — a
        model still calling the old shape must not break history or the panel.
        Attachments register media the client renders inline next to the questions.
        """
        from agent.agent_loop import StepOutcome

        attachments = args.get("attachments") or []
        media: list[dict] = []
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                result = await self._present_attachment(item)
                # Success is "has a kind", NOT status=="ok": a registered
                # attachment carries the attachment_store lifecycle status
                # ("active"/"expired"/"purged") in that field, so filtering on
                # "ok" silently dropped every workspace-file and base64 image
                # and only ever let public URLs through.
                if result.get("kind"):
                    media.append(result)

        questions = self._normalize_questions(args)
        return StepOutcome(
            data={
                "status": "question",
                # 多题（新形状）：questions[] 承载题干/选项/多选/推荐。
                "questions": questions,
                # 单题兼容字段：取第一题的题干与选项，旧面板/历史回放仍读它。
                "question": questions[0].get("question") if questions else str(args.get("question") or ""),
                "candidates": [o["label"] for o in questions[0].get("options", [])] if questions else [],
                "candidates_raw": [dict(o) for o in questions[0].get("options", [])] if questions else [],
                "media": media or None,
            },
            should_exit=True,
        )

    @staticmethod
    def _normalize_questions(args: dict) -> list[dict]:
        """归一化 question/questions 两种形状 → ``questions`` 列表（每项至少含 question）。

        兼容旧调用：只传 ``question`` + ``candidates`` 时合成单题；新调用传
        ``questions``（每项可带 options/multi_select/allow_custom/recommended/reason）。
        选项统一成 ``[{label, description?}]``（裸字符串也接受）。
        """
        def _opts(raw: Any) -> list[dict]:
            out: list[dict] = []
            if isinstance(raw, list):
                for o in raw:
                    if isinstance(o, str):
                        out.append({"label": o})
                    elif isinstance(o, dict) and str(o.get("label") or "").strip():
                        entry = {"label": str(o["label"])}
                        if o.get("description"):
                            entry["description"] = str(o["description"])
                        out.append(entry)
            return out

        raw_qs = args.get("questions")
        out: list[dict] = []
        if isinstance(raw_qs, list):
            for item in raw_qs:
                if not isinstance(item, dict):
                    continue
                q = str(item.get("question") or "").strip()
                if not q:
                    continue
                entry: dict = {"question": q}
                opts = _opts(item.get("options"))
                if opts:
                    entry["options"] = opts
                if item.get("multi_select") is not None:
                    entry["multi_select"] = bool(item["multi_select"])
                if item.get("allow_custom") is not None:
                    entry["allow_custom"] = bool(item["allow_custom"])
                if item.get("recommended"):
                    entry["recommended"] = str(item["recommended"])
                if item.get("reason"):
                    entry["reason"] = str(item["reason"])
                out.append(entry)
        if out:
            return out
        # 旧形状：question + candidates → 单题。
        legacy_q = str(args.get("question") or "").strip()
        if legacy_q:
            entry = {"question": legacy_q}
            opts = _opts(args.get("candidates"))
            if opts:
                entry["options"] = opts
            out.append(entry)
        return out


# ---------------------------------------------------------------------------
# OpenAI function-calling schemas
# ---------------------------------------------------------------------------

_CODE_RUN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "code_run",
        "description": (
            "Execute a shell command, Python or PowerShell in a subprocess — and manage "
            "the background processes it started. This is the only way to observe real "
            "state: use it to probe the environment, run commands, verify a change "
            "actually took effect, and produce the evidence that start_long_term_update "
            "requires. Prefer it over asserting an outcome. Running new code requires a "
            "one-time interactive approval; a denied call returns status=denied instead "
            "of running. Never use it to write files under memory/ — that path is "
            "reserved for start_long_term_update.\n"
            "Pick the smallest form that does the job: a one-line command (check a file, "
            "list a directory, probe a port) → type=shell; multi-step logic → type=python; "
            "Windows-only shell work → type=powershell. The host OS is in "
            "[Runtime Environment] in your system prompt — read it instead of probing it.\n"
            "Long-running work: pass background=true to start a process without waiting "
            "for it (returns a process_id immediately), then manage it with the operation "
            "parameter — operation=list shows every background process in this run, "
            "operation=output returns the NEW output since your last read of one process "
            "(poll it in a loop while waiting), operation=kill stops one. Management "
            "operations need no approval. Always kill processes you no longer need."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "output", "kill"],
                    "description": (
                        "Manage background processes started earlier with "
                        "background=true. list = all processes this run started; output = "
                        "new output of one process (incremental, needs process_id); kill = "
                        "terminate one process (needs process_id). Omit this parameter to "
                        "run code instead."
                    ),
                },
                "code": {
                    "type": "string",
                    "description": (
                        "The command (type=shell) or complete, self-contained source "
                        "(type=python/powershell). Print what you need to see: "
                        "stdout/stderr are the only things returned. Required unless "
                        "operation is set."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": ["shell", "python", "powershell"],
                    "default": "python",
                    "description": (
                        "shell = one-line command run with the host's native shell "
                        "(/bin/sh on POSIX, cmd on Windows); python = Python script "
                        "(default); powershell = PowerShell. Use shell for simple "
                        "commands; keep the default only when you actually wrote Python."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "default": 60,
                    "description": (
                        "Seconds before the process is killed and status=timeout is "
                        "returned. Raise it for builds or installs. Ignored when "
                        "background=true (background processes have no timeout — stop "
                        "them with operation=kill)."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Start the command as a background process instead of waiting for "
                        "it. Returns immediately with a process_id; manage it with the "
                        "operation parameter. Use for long-running processes (servers, "
                        "watchers, big installs) that would otherwise block the turn or "
                        "hit the timeout."
                    ),
                },
                "process_id": {
                    "type": "string",
                    "description": (
                        "Target background process id (required for operation=output/kill). "
                        "Returned by the background=true call that started it; "
                        "operation=list shows all of them."
                    ),
                },
                "stream": {
                    "type": "string",
                    "enum": ["stdout", "stderr"],
                    "default": "stdout",
                    "description": "Which stream to read for operation=output.",
                },
            },
            # No globally-required field: `code` is required unless `operation`
            # is set (the two modes are mutually exclusive), and
            # `_code_run` enforces that contract explicitly.
        },
    },
}

_FILE_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_read",
        "description": (
            "Read a file. Call this before editing anything, and call it to pull a SOP "
            "body: the L1 index only gives you SOP names, so the actual steps must be "
            "read from memory/sop/<name>.md every time you need them — never work from "
            "a remembered version. Large files come back truncated; use keyword to jump "
            "to the relevant section or start/count to page through."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path, e.g. memory/sop/browser_sop.md or workspace/notes.txt"},
                "start": {
                    "type": "integer",
                    "default": 1,
                    "description": "1-based first line to return. Use with count to page a long file.",
                },
                "count": {
                    "type": "integer",
                    "default": 200,
                    "description": (
                        "How many lines to return. The reply's truncated flag tells you "
                        "whether more remains past start+count."
                    ),
                },
                "keyword": {"type": "string", "description": "Optional anchor: start reading from the first line containing this substring"},
            },
            "required": ["path"],
        },
    },
}

_FILE_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_write",
        "description": (
            "Create a new file or replace one wholesale. Use this for new files and for "
            "generated output; use file_patch to change part of an existing file, so an "
            "overwrite never silently drops content you never read. memory/ is rejected "
            "here — long-term memory is written only by start_long_term_update."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path under the working directory, e.g. workspace/report.md",
                },
                "content": {"type": "string", "description": "Full file content to write (UTF-8)"},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "default": "overwrite",
                    "description": "overwrite replaces the whole file; append adds to the end",
                },
            },
            "required": ["path", "content"],
        },
    },
}

_FILE_PATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_patch",
        "description": (
            "Preferred way to edit an existing file: swap one exact block for another. "
            "old_content must appear exactly once — read the file with file_read first "
            "and copy the block verbatim (indentation and full-width characters "
            "included), otherwise the patch is rejected rather than applied to the "
            "wrong place."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path of the file to edit"},
                "old_content": {
                    "type": "string",
                    "description": "Exact block to replace; must be unique in the file",
                },
                "new_content": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_content", "new_content"],
        },
    },
}

# One browser-class MCP method set is reachable through the `web` tool: every
# operation the browser SOP describes maps to exactly one advertised method. The
# entries are (canonical primitive, tolerated aliases, fallback tab argument name)
# — aliases exist so a browser MCP that spells the primitive differently (GA-style
# `evaluate_script` vs platform `browser_execute_js`) still resolves.
_BROWSER_OPERATIONS: dict[str, tuple[str, tuple[str, ...], str]] = {
    # 列浏览器：授权域可见性出口（模型据此知道能操作哪些浏览器、哪台是本会话绑定的）。
    "clients": ("browser_clients", ("list_clients", "get_clients"), ""),
    "execute": ("evaluate_script", ("browser_execute_js", "eval", "run_script", "evaluate"), "switch_tab_id"),
    "tabs": ("list_tabs", ("browser_get_tabs", "get_tabs", "tabs"), ""),
    "screenshot": ("capture_screenshot", ("browser_screenshot", "screenshot", "take_screenshot", "capturescreenshot"), "tab_id"),
    "navigate": ("navigate", ("browser_navigate", "goto", "set_url"), ""),
    "open_tab": ("open_tab", ("browser_open_tab", "new_tab"), ""),
    "close_tab": ("close_tab", ("browser_close_tab",), "tab_id"),
    "switch_tab": ("switch_tab", ("browser_switch_tab",), "tab_id"),
    "focus_tab": ("focus_tab", ("browser_focus_tab", "activate_tab"), "tab_id"),
    "batch": ("batch", ("browser_batch",), "tab_id"),
    "wait": ("wait", ("browser_wait",), "switch_tab_id"),
    # 流量三件套：canonical 是 watch/poll/unwatch_traffic。「network」太虚（读起来像
    # 整台机器的网络），而这套实际只看**这一个 tab 的收发**——HTTP 请求 + WebSocket
    # 帧 + SSE 消息；「read_network」又与 read_page 抢 read 动词、还容易被读成「用网络
    # 去读」。poll 才是这一步的真语义：从 watch 开着的缓冲里反复取增量。
    # watch/read/unwatch_network 与 network_start/get/stop 全部保留为别名，已写的
    # 提示词和已发布的工作流照常解析。
    "watch_traffic": ("watch_network", ("browser_watch_network", "browser_network_start", "watch_network"), "tab_id"),
    "poll_traffic": ("read_network", ("browser_read_network", "browser_network_get", "read_network"), "tab_id"),
    "unwatch_traffic": ("unwatch_network", ("browser_unwatch_network", "browser_network_stop", "unwatch_network"), "tab_id"),
    # 旧 operation 名保留为同义入口（提示词/历史消息里可能仍出现），路由到同一实现。
    "watch_network": ("watch_network", ("browser_watch_network", "browser_network_start"), "tab_id"),
    "read_network": ("read_network", ("browser_read_network", "browser_network_get"), "tab_id"),
    "unwatch_network": ("unwatch_network", ("browser_unwatch_network", "browser_network_stop"), "tab_id"),
    "network_start": ("watch_network", ("browser_watch_network", "browser_network_start"), "tab_id"),
    "network_get": ("read_network", ("browser_read_network", "browser_network_get"), "tab_id"),
    "network_stop": ("unwatch_network", ("browser_unwatch_network", "browser_network_stop"), "tab_id"),
    # 2FA 取码：按登录页 url / 域名匹配用户验证器的关联网站，回当前码 + 剩余秒。
    # canonical 即 advertised 名（server.py 的函数名）；走 web(totp_code)。
    "totp_code": ("browser_totp_code", ("browser_totp_code", "totp", "get_totp"), ""),
    # 凭据填充：把已保存的 2FA 码 / 密码**直接敲进页面**——值走「服务端→扩展→DOM」，
    # **绝不回给模型**（结果只有回执，无 code/password）。这是登录表单要密码/验证码时的
    # 首选；totp_code 只在「你必须自己读到码」时用。canonical 即 server.py 的函数名。
    "fill_credential": ("browser_fill_credential", ("browser_fill_credential", "fill_credential", "autofill"), "tab_id"),
}

# Arguments forwarded verbatim to the browser MCP method when present. `path` is
# absent on purpose: it names the local file the screenshot spills to, and the
# platform CDP `browser_screenshot` does not accept it.
# `no_monitor` is included: `execute` defaults to a full before/after HTML diff plus
# a 450ms DOM-change monitor, which is pure overhead for a read-only probe (reading
# location.href, a value, a node's text). Without this the model could not turn the
# monitor off through `web` at all, so every trivial probe paid for a whole-page
# snapshot + diff.
_WEB_FORWARD_ARGS: tuple[str, ...] = (
    "script", "url", "timeout", "new_window", "active",
    "commands", "condition_js", "interval", "url_pattern", "no_monitor",
    # client_id：在授权域内**选择**浏览器（web(clients) 列出的 id）。它不是凭据——
    # 服务端按授权边界校验，模型传越界的 id 会被拒；不传则由会话绑定兜底。
    "client_id",
    # 监听读取的按需钻取：概要 → 精确取某条请求全量 / 某帧 payload / 按 url 过滤。
    "search", "request_id", "frame_index",
    # 2FA 取码：url（登录页）/ domain（裸域名）匹配用户验证器的关联网站。
    "domain",
    # 凭据填充：kind（otp/password）/ account（多条目时按账户名选）/ selector（钉死字段）。
    # 注意 value/密码**不在这里**——那些值从不经过模型。
    "kind", "account", "selector",
)

# Platform CDP tab argument names differ per method; the operation table's
# fallback covers generic browser MCPs (which take plain `tab`).
_WEB_TAB_ARGS: dict[str, str] = {
    "browser_execute_js": "switch_tab_id",
    "browser_scan": "switch_tab_id",
    "browser_wait": "switch_tab_id",
    "browser_screenshot": "tab_id",
    "browser_switch_tab": "tab_id",
    "browser_focus_tab": "tab_id",
    "browser_close_tab": "tab_id",
    "browser_batch": "tab_id",
    "browser_watch_network": "tab_id",
    "browser_read_network": "tab_id",
    "browser_unwatch_network": "tab_id",
    "browser_network_start": "tab_id",
    "browser_network_get": "tab_id",
    "browser_network_stop": "tab_id",
    "browser_fill_credential": "tab_id",
}

_WEB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web",
        "description": (
            "Read or drive the web. Pick the operation by WHAT YOU WANT, not by habit — "
            "`execute` runs arbitrary JS and is the escape hatch, not the default:\n"
            "- Which browsers can I operate / which one is this bound to → clients\n"
            "- What tabs are open / what page am I on (URL, title) → tabs\n"
            "- What does this page say (clean readable text) → read_page [no browser "
            "needed for a plain URL; it neither searches by keyword nor monitors traffic]\n"
            "- What does this page SEND (requests, WebSocket frames, SSE) → "
            "watch_traffic → poll_traffic → unwatch_traffic\n"
            "- Click / type / fill / scroll / read a live DOM value → execute\n"
            "- A login form asks for a password / 2FA code → fill_credential (the value is "
            "typed into the page and never returned to you)\n"
            "- Go to another page → navigate (same site) | open_tab (only a new site/goal "
            "the user asked for)\n"
            "- Done with a tab you opened → close_tab (pass `tab`; never close the tab this "
            "conversation is bound to unless the user asked)\n"
            "- Wait for a condition → wait (server-side; better than a JS poll loop)\n"
            "- See it → screenshot.\n"
            "`client_id` selects a browser within your authorised set when this conversation "
            "is not bound to a single one; `web(operation=clients)` lists the ids you may pass. "
            "The bound browser is the default target; pass client_id only to switch.\n"
            "reach for execute only when no dedicated operation fits: every execute pays a "
            "full-page HTML snapshot + DOM diff unless you pass no_monitor=true, whereas "
            "tabs/read_page/watch_traffic are cheap and purpose-built. `read_page` on a plain "
            "URL needs no browser at all; every other operation needs a bound browser-class "
            "MCP service (else it returns browser_mcp_not_configured). Pass `tab` to target a "
            "tab other than the one this conversation is bound to. Before scripting a live "
            "page, read memory/sop/browser_sop.md — the element/click/navigation rules there "
            "are not guessable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "read_page", "execute", "tabs", "screenshot",
                        "navigate", "open_tab", "close_tab", "switch_tab", "focus_tab",
                        "batch", "wait",
                        "watch_traffic", "poll_traffic", "unwatch_traffic",
                        "clients", "fill_credential", "totp_code",
                    ],
                    "default": "read_page",
                    "description": (
                        "Choose by intent:\n"
                        "clients — which browsers you may operate + which is bound (read-only; "
                        "lists client_id, name, tabs, connection state)\n"
                        "tabs — which tabs exist / which is active / its url+title (cheapest "
                        "way to know where you are; never run JS just to read location.href)\n"
                        "read_page — take a page's readable text: fetch a `url` (plain HTTP, "
                        "no browser) or read a given `tab`'s rendered DOM; HTML noise "
                        "(scripts/styles/comments/invisible elements) is stripped, anchors "
                        "kept as `text (url)`, output truncated at `max_chars`. It NEVER "
                        "searches by keyword and NEVER navigates; give both url and tab and "
                        "it reads the tab, ignoring the url\n"
                        "watch_traffic / poll_traffic / unwatch_traffic — subscribe to ONE "
                        "tab's sends and receives: HTTP requests AND WebSocket frames AND "
                        "SSE messages. watch starts (keeps the debugger attached + a banner "
                        "on the page), poll repeatedly extracts what has arrived (summary "
                        "first; drill in with search/request_id/frame_index), unwatch stops "
                        "and returns everything. This is the way to see what a page sends; "
                        "do NOT hand-write fetch/XHR/WebSocket hooks in execute (page hooks "
                        "miss sockets already open and cross-origin traffic)\n"
                        "execute — run JS in a tab: click, type, fill, scroll, read a live DOM "
                        "value. The escape hatch, not the default; pass no_monitor=true for a "
                        "read-only probe\n"
                        "navigate — load a URL in the CURRENT tab (same site); open_tab — only "
                        "when the user named a new site/goal; close_tab — shut a tab you opened "
                        "(pass `tab`); do NOT close the tab this conversation is bound to unless "
                        "asked; switch_tab / focus_tab — retarget "
                        "vs. bring to front; screenshot / batch / wait — capture / many CDP "
                        "commands in one session / poll a condition server-side.\n"
                        "totp_code — the current 2FA verification code for a login page, from "
                        "the user's saved authenticator (matched by page url / `domain`). Use "
                        "when a form asks for a 2FA / verification / authenticator code; empty "
                        "result means the user hasn't linked that site — ask them to add it, "
                        "never guess a code.\n"
                        "fill_credential — type a saved credential (2FA code OR password) "
                        "STRAIGHT INTO the page: pass `kind` ('otp' / 'password') + `url` (or "
                        "`domain`) + `account` when several entries exist. The value is resolved "
                        "server-side, handed to the browser, and typed into the field — it is "
                        "NEVER returned to you (the result is a receipt only). Prefer this "
                        "whenever a login form asks for a password or a 2FA code; use totp_code "
                        "only when you must *read* a code yourself.\n"
                        "Aliases: scan (→read_page), watch_network/read_network/"
                        "unwatch_network and network_start/network_get/network_stop "
                        "(→the *_traffic trio) still resolve."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": "Target URL (read_page / navigate / open_tab)",
                },
                "tab": {
                    "type": "string",
                    "description": (
                        "Tab to act on (all browser operations). Defaults to the tab "
                        "this conversation is bound to"
                    ),
                },
                "client_id": {
                    "type": "string",
                    "description": (
                        "Browser client ID, to select within your authorised set when several "
                        "browsers are available. Defaults to the bound browser. Use "
                        "operation=clients to list the ids you may pass here; an id outside "
                        "your authorisation domain is rejected."
                    ),
                },
                "script": {
                    "type": "string",
                    "description": "JavaScript to execute (operation=execute); `return` a value to get it back",
                },
                "no_monitor": {
                    "type": "boolean",
                    "default": False,
                    "description": "operation=execute: skip the DOM-change monitor for a read-only probe",
                },
                "new_window": {
                    "type": "boolean",
                    "description": "Open in a new window (operation=open_tab)",
                },
                "active": {
                    "type": "boolean",
                    "description": "Open the new tab in the foreground (operation=open_tab)",
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "CDP commands to run in order, sharing one session (operation=batch)",
                },
                "condition_js": {
                    "type": "string",
                    "description": "JavaScript condition to poll (operation=wait)",
                },
                "interval": {
                    "type": "number",
                    "description": "Poll interval in seconds (operation=wait)",
                },
                "url_pattern": {
                    "type": "string",
                    "description": "Only capture requests matching this substring (operation=watch_traffic)",
                },
                "domain": {
                    "type": "string",
                    "description": (
                        "Bare site domain to match against an authenticator's linked sites "
                        "(operation=totp_code / fill_credential), e.g. github.com. Use instead "
                        "of url when no page URL is at hand."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["otp", "password"],
                    "description": (
                        "operation=fill_credential: which credential to fill — 'otp' for a "
                        "2FA / verification code, 'password' for a login password."
                    ),
                },
                "account": {
                    "type": "string",
                    "description": (
                        "operation=fill_credential: account / username to pick the right entry "
                        "when the site has several saved (the result lists the options if you "
                        "omit it and there is more than one)."
                    ),
                },
                "selector": {
                    "type": "string",
                    "description": (
                        "operation=fill_credential: optional CSS selector to pin the target "
                        "field explicitly, when the auto-detection would pick the wrong one."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": (
                        "operation=poll_traffic: return only rows whose URL contains this "
                        "substring (filter before reading, instead of pulling everything)."
                    ),
                },
                "request_id": {
                    "type": "string",
                    "description": (
                        "operation=poll_traffic: fetch the FULL record (headers + response "
                        "body) of this one request, by its requestId from the summary. Use "
                        "this to drill into a single interesting request instead of pulling "
                        "every body."
                    ),
                },
                "frame_index": {
                    "type": "integer",
                    "description": (
                        "operation=poll_traffic: fetch the full payload of this WS/SSE frame "
                        "(index from the summary's wsFrames)."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Where to save the image (operation=screenshot), e.g. "
                        "workspace/shots/login.png. Defaults to a timestamped file under "
                        "workspace/screenshots/. The result carries this path — pass it to "
                        "ask_user to show the user; the image bytes are never returned inline."
                    ),
                },
                "max_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "Cap on returned page text; raise it when the part you need is cut off",
                },
                "timeout": {"type": "integer", "default": 20, "description": "Seconds to wait for the page or script"},
            },
            "required": ["operation"],
        },
    },
}

_SHOW_FILE_REMOVED = None  # merged into ask_user

_START_LONG_TERM_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "start_long_term_update",
        "description": (
            "Distill a verified experience into long-term memory. The backend decides "
            "whether it lands as a stable fact (L2) or a reusable procedure (L3) and "
            "adds a short L1 pointer. Call it only near the end of a task, only for "
            "something reusable by a *future* task, and only after a tool result proved "
            "it — no execution, no memory. Per-task state belongs in "
            "update_working_checkpoint instead; read memory/sop/memory_management_sop.md "
            "before the first write."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "The reusable lesson in one or two sentences, written so a future "
                        "task can act on it without this conversation"
                    ),
                },
                "verified_by": {
                    "type": "string",
                    "description": "The concrete tool result that proved it (command run, output seen, file checked)",
                },
                "reuse_reason": {"type": "string", "description": "Why reusable across tasks"},
                "evidence": {
                    "type": "object",
                    "description": "Optional supporting detail backing verified_by",
                    "properties": {
                        "files": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Paths that were read or changed",
                        },
                        "steps": {
                            "type": "array", "items": {"type": "string"},
                            "description": "The steps that were actually executed",
                        },
                    },
                },
            },
            "required": ["summary", "verified_by"],
        },
    },
}

_MEMORY_READ_REMOVED = None  # memory/ files are read with file_read

_CAPABILITY_CALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "capability_call",
        "description": (
            "Invoke dynamic MCP or scheduler capabilities by dotted name — they are not "
            "separate tools, they all go through here. Examples: "
            "capability_call(name='mail.search_messages', args={...}) or "
            "capability_call(name='scheduler.create', args={...}). Only names listed "
            "under [Available Capabilities] in the system prompt exist; if a service has "
            "a (SOP: ...) pointer, read that SOP with file_read before the first call "
            "instead of guessing the arguments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Dotted capability name, '<service>.<method>' or 'scheduler.<action>'",
                },
                "args": {
                    "type": "object",
                    "description": "Argument object for that method, per its schema in the capability index",
                },
            },
            "required": ["name"],
        },
    },
}

_UPDATE_WORKING_CHECKPOINT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_working_checkpoint",
        "description": (
            "Rewrite the per-task working anchor, which is re-injected every turn and is "
            "the only context guaranteed to survive history compression. Call it after "
            "learning something you must not forget, before a risky step, and whenever "
            "the plan changes. Fields are replaced, not merged — resend the whole state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The task's end state in one sentence"},
                "completed": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Steps already verified done",
                },
                "current_state": {
                    "type": "string",
                    "description": "Where the work stands right now, including the last failure",
                },
                "next_steps": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Concrete remaining steps in order",
                },
                "key_info": {
                    "type": "string",
                    "description": "Facts discovered this task (ids, paths, commands, quirks) that would be expensive to rediscover",
                },
                "related_files": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Paths to re-read with file_read, e.g. the governing SOP",
                },
            },
            "required": [],
        },
    },
}

_SCHEDULER_SCHEMAS_REMOVED = None  # collapsed into capability_call

_ASK_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "The single interaction point between you and the user for two purposes: "
            "(1) clarify a requirement when key information is missing, the request is "
            "ambiguous, or there are several reasonable implementation directions; and "
            "(2) show a deliverable (a file, a screenshot, an image) by attaching it "
            "next to the question.\n"
            "Use it actively — this call ends the turn, so batch every question and "
            "attachment you want to surface into one call.\n\n"
            "Core rules\n"
            "- Ask only when it matters: only ask when missing information would "
            "materially change the outcome; never re-confirm what is already clear.\n"
            "- Resolve the critical ambiguity first: prioritize questions that affect "
            "architecture, business logic, implementation direction, or the final "
            "result.\n"
            "- One decision per question: each entry in `questions` focuses on one "
            "core decision.\n"
            "- Offer options: when several reasonable choices exist, give 2-5 mutually "
            "exclusive options via `options`, each optionally described in "
            "`description`. Allow a custom answer (`allow_custom`) unless a closed "
            "set is truly required.\n"
            "- Stay neutral: do not assume the user's preference and do not word "
            "questions to steer them toward one option.\n"
            "- Recommended defaults are not confirmation: for non-critical choices "
            "you may mark a recommended option with `recommended` and explain in "
            "`reason`, but a recommendation is not the user's decision — never treat "
            "it as already chosen.\n"
            "- Do not block work that can proceed: if the missing information does "
            "not affect the parts you can do now, do those first, then ask.\n"
            "- Respect the user's decision: once the user picks, follow it; never "
            "re-ask the same question.\n\n"
            "Showing a deliverable\n"
            "- When you produced a file/screenshot/image the user should see, pass it "
            "in `attachments` (a workspace `path` a tool returned, or a public `url`); "
            "the client renders it inline next to the question.\n"
            "- Only pass a `path`/`url` you have verified exists; never invent one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "One or more questions to ask in a single call. "
                                    "Batch related decisions here to avoid multi-round back-and-forth.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "The question text — concise, specific, directly answerable."},
                            "options": {
                                "type": "array",
                                "description": "2-5 mutually exclusive suggested answers. Omit only for genuinely open-ended questions.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "A short option label the user picks."},
                                        "description": {"type": "string", "description": "Optional one-line difference/reason this option matters."},
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multi_select": {"type": "boolean", "description": "Whether the user may pick more than one option. Default false."},
                            "allow_custom": {"type": "boolean", "description": "Whether the user may type a custom answer instead of picking an option. Default true."},
                            "recommended": {"type": "string", "description": "Optional: the label of the recommended option. A recommendation is not a pre-confirmation."},
                            "reason": {"type": "string", "description": "Optional: why this option is recommended (shown with the recommendation)."},
                        },
                        "required": ["question"],
                    },
                },
                "attachments": {
                    "type": "array",
                    "description": "Deliverables to show inline next to the questions (files/screenshots/images). Shared across all questions in this call.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Workspace file path (e.g. the path a screenshot tool returned)"},
                            "url": {"type": "string", "description": "Public http(s) URL"},
                            "name": {"type": "string"},
                            "mime_type": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["questions"],
        },
    },
}

# Spawn a fresh sub-agent. Registered only on the PARENT agent's registry
# (GenericAgent._ensure_tools); a sub-agent's registry never registers it, so a
# child cannot delegate further — it reports the need in its result and the
# parent decides. The tool awaits the child's final answer (one-shot/foreground).
_SPAWN_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_subagent",
        "description": (
            "Delegate a self-contained piece of work to a fresh sub-agent and wait "
            "for its final answer. The child shares your tools, bound capabilities "
            "(browser/MCP) and workspace rules, but starts with an EMPTY conversation "
            "— it does not see this conversation, so pass a self-contained task (goal + "
            "constraints + absolute file paths). Use it when: (a) a search or scan "
            "would dump a lot of noise into THIS conversation (you keep the conclusion, "
            "not the file dumps); (b) an independent verification is needed (the child "
            "has no anchoring bias — better than checking your own work); (c) a sub-task "
            "genuinely cannot see this conversation. Pass `role` to say what the child "
            "is for (explore/verify/build) — it also picks the child's model and persona. "
            "Pass `category` only to override the model tier (quick/deep/ultrabrain). "
            "Never split one coherent task into sub-checks (that is one job in several "
            "costumes); if you could do it faster with one file_read, do it yourself. "
            "In supervisor mode delegation is the default action, not a last resort. "
            "Returns the child's final text as the tool result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The self-contained task for the child: the goal in one or two "
                        "sentences, the constraints it must respect, and absolute paths "
                        "for any files it must read/write. It cannot see this conversation."
                    ),
                },
                "role": {
                    "type": "string",
                    "enum": ["explore", "verify", "build", "general"],
                    "description": (
                        "What the child is for. explore = search/locate/read only, bring "
                        "back findings with file:line (cheap-fast model). verify = "
                        "independent adversarial review, output PASS/FAIL/PARTIAL with "
                        "evidence (strong model). build = implement the change and verify "
                        "it (mid model). general = no specialization (follows configured "
                        "sub-agent model). Picks the child's model AND persona; omit "
                        "only when none fits."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": ["quick", "deep", "ultrabrain"],
                    "description": (
                        "Difficulty override for the child's model tier, used when no "
                        "specific role fits or you want to bump the tier up/down for "
                        "this one task. quick = trivial, deep = needs thought, "
                        "ultrabrain = hardest. Takes precedence over role for model "
                        "selection."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Explicit model id override (escape hatch). Prefer role/category "
                        "instead — the model list is deliberately kept out of your context."
                    ),
                },
            },
            "required": ["task"],
        },
    },
}
