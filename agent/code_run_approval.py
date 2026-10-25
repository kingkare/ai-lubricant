"""Approval coordinator for GenericAgent's local ``code_run`` tool."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable

from user_platform.node_client.approvals import (
    CONFIRMATION_TIMEOUT_SECONDS,
    ApprovalDenied,
    ApprovalTimeout,
    approval_registry,
    emit_confirmation_required,
)

EventSink = Callable[[dict], Awaitable[None]]


class CodeRunApprovalBatch:
    """Pre-approve every code_run call in one model turn as a single batch."""

    def __init__(
        self,
        *,
        conversation_id: str,
        agent_id: int,
        caller: str | None,
        emit: EventSink | None,
        timeout_seconds: int = CONFIRMATION_TIMEOUT_SECONDS,
        auto_approve: "bool | Callable[[], bool]" = False,
    ):
        self.conversation_id = conversation_id
        self.agent_id = agent_id
        self.caller = caller
        self.emit = emit
        # 注册表到期时刻与本地 wait_for 必须同值，否则两边各按各的到期：
        # 注册表判过期后 reap 成 deny，而等待方还在等，或者反过来。
        self.timeout_seconds = int(timeout_seconds)
        # auto 模式放行开关。传 callable 时**每次 prepare 现取**（读会话活状态），
        # 传 bool 时用快照——历史 bug 是这里只收 bool，回合开始切走 auto 后同回合
        # 后续审批还拿旧值弹卡。会话状态收进 ConversationState 后，调用方传
        # `lambda: state.auto_approve` 即可让中途切 auto 立即生效。
        self._auto_approve = auto_approve
        self._decisions: list[tuple[str, str, str | None]] = []

    def _is_auto(self) -> bool:
        aa = self._auto_approve
        return bool(aa() if callable(aa) else aa)

    def _timeout(self) -> float | None:
        """wait_for 的 timeout：恰好 0 表示不过期（None＝永久等待）。

        与 ApprovalRegistry.create 同口径——负数不是哨兵，仍是「立即到期」。
        """
        return None if self.timeout_seconds == 0 else self.timeout_seconds

    @staticmethod
    def _canonical(args: dict[str, Any]) -> str:
        return json.dumps({
            "type": str(args.get("type") or "python"),
            "code": str(args.get("code") or ""),
            "timeout": int(args.get("timeout") or 60),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    async def prepare(self, tool_calls: list[dict]) -> None:
        self._decisions = []
        calls: list[tuple[str, dict[str, Any]]] = []
        for call in tool_calls or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or function.get("name") != "code_run":
                continue
            raw = function.get("arguments") or {}
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw)
            except (TypeError, ValueError):
                args = {}
            # Management operations (list/output/kill) execute no new code, so
            # they must not trigger an approval card — the whole batch would
            # otherwise ask the user to approve an empty script.
            if args.get("operation"):
                continue
            canonical = self._canonical(args)
            calls.append((canonical, args))
        if not calls:
            return
        # auto 模式：全部自动放行，不建审批卡也不等裁决（用户显式选择了自动模式）。
        # 但 emit=None（无人值守）时仍 fail closed：auto_approve 只由交互入口传入，
        # 这里双重保险——没有事件出口就没有「用户在场」的前提。
        if self._is_auto() and self.emit is not None:
            self._decisions = [(canonical, "allow", None) for canonical, _args in calls]
            return
        if self.emit is None:
            self._decisions = [(canonical, "deny", None) for canonical, _args in calls]
            return

        canonical_batch = "\n".join(canonical for canonical, _args in calls)
        action = approval_registry.create(
            conversation_id=self.conversation_id,
            node_id="",
            tool_name="code_run",
            command=canonical_batch,
            commands=[canonical for canonical, _args in calls],
            requester=self.caller,
            shell_flavor="shell/python/powershell",
            timeout_seconds=self.timeout_seconds,
            metadata={
                "agent_id": self.agent_id,
                "runs": [
                    {
                        "type": str(args.get("type") or "python"),
                        "timeout": int(args.get("timeout") or 60),
                        "code_hash": hashlib.sha256(str(args.get("code") or "").encode("utf-8")).hexdigest(),
                        "code": str(args.get("code") or ""),
                    }
                    for _canonical, args in calls
                ],
            },
        )
        await emit_confirmation_required(
            self.emit,
            action,
            "code_run 将在服务端执行代码。请核对完整代码和哈希；本次授权仅执行一次。",
        )
        try:
            decision = await asyncio.wait_for(action.future, timeout=self._timeout())
        except asyncio.TimeoutError:
            # 超时不再降级成 deny 让循环继续跑下一轮：那样模型会拿着「被拒绝」继续
            # 折腾，用户看到的是「过期了还在调模型」。中止本轮，等人回来重发。
            action.resolved = "timeout"
            raise ApprovalTimeout(action.confirmation_id, "code_run") from None
        finally:
            # 与 node shell 两处对齐：轮次被取消时留下 resolved=None 会让这条审批
            # 永远以 pending 出现在 GET /approvals 里。
            if action.resolved is None:
                action.resolved = "interrupted"
        if decision == "allow":
            self._decisions = [(canonical, "allow", action.confirmation_id) for canonical, _args in calls]
            return
        # Explicit user rejection aborts the whole turn. Do not return a normal
        # denied tool result: agent_loop would feed it back to the model and let
        # the model continue after the user already cancelled execution.
        raise ApprovalDenied(action.confirmation_id, "code_run") from None

    def decision_for(self, args: dict[str, Any]) -> tuple[str, str | None]:
        canonical = self._canonical(args)
        for index, (stored, outcome, confirmation_id) in enumerate(self._decisions):
            if stored == canonical:
                self._decisions.pop(index)
                return outcome, confirmation_id
        # No pre-scan approval means fail closed. ToolRegistry can also be used by
        # non-conversation callers, which must not silently gain server code exec.
        return "deny", None


__all__ = ["CodeRunApprovalBatch"]
