"""一次性审批协调器：写入撞到沙箱边界时的 ``file_write`` / ``file_patch``。

与 :class:`agent.code_run_approval.CodeRunApprovalBatch` 同契约：agent 循环把一轮的
全部工具调用先交给 ``prepare`` 预扫，命中的调用合并成**一张**审批卡；工具执行时用
``decision_for`` 消费已裁决的结果，不再逐次弹卡。

与 ``code_run`` 的差别只有准入判定：``code_run`` 一律需要审批（能执行任意代码），而
文件写入是 Agent 的日常工作，工作区内的正常写入不该每次都打断用户。所以只有**撞到
沙箱边界**的写入才进审批：

- 解析后落在 ``allowed_roots`` 之外（例如用 ``../`` 跳出工作区），或
- 命中 ``denied_patterns``（``.env`` / ``db.py`` / ``.git/`` 这类显式禁写项）。

``memory/`` 子树是**结构性**只读区（长期记忆只能由 ``start_long_term_update`` 经
``file_memory`` 写入），它不属于「沙箱边界」——即便用户点允许也不能放开，因此这里
直接跳过，由工具层硬拒。

fail closed：没有挂协调器（无人值守的定时任务、没有事件出口的调用方）时
``decision_for`` 一律返回 deny，写入不落盘，与 ``code_run`` 同口径。
"""
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

# 卡片里回显的正文预览上限。完整内容仍参与哈希（哈希覆盖全文），预览只是给人看的
# 抽样——一个 1MB 的写入不能整段塞进 SSE 事件。
_PREVIEW_CHARS = 4000

_FILE_TOOLS = {"file_write", "file_patch"}


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class FileWriteApprovalBatch:
    """Pre-approve every sandbox-crossing file write in one model turn as a batch."""

    def __init__(
        self,
        *,
        conversation_id: str,
        agent_id: int,
        caller: str | None,
        emit: EventSink | None,
        security,
        timeout_seconds: int = CONFIRMATION_TIMEOUT_SECONDS,
        auto_approve: "bool | Callable[[], bool]" = False,
    ):
        self.conversation_id = conversation_id
        self.agent_id = agent_id
        self.caller = caller
        self.emit = emit
        self.security = security
        # 注册表到期时刻与本地 wait_for 必须同值，否则两边各按各的到期。
        self.timeout_seconds = int(timeout_seconds)
        # auto 模式放行开关：用户显式选了「自动」执行模式时预扫直接记 allow、不弹卡。
        # 只影响本批越界写入的审批卡，memory/ 与 denied_patterns 的判定不在这里放开
        # （memory/ 由工具层硬拒，见 _needs_approval / tools.py）。
        # 传 callable 时**每次 prepare 现取**（读会话活状态），让回合中途切 auto
        # 立即生效；传 bool 时用快照（历史行为）。
        self._auto_approve = auto_approve
        self._decisions: list[tuple[str, str, str | None]] = []

    def _is_auto(self) -> bool:
        aa = self._auto_approve
        return bool(aa() if callable(aa) else aa)

    def _timeout(self) -> float | None:
        """wait_for 的 timeout：恰好 0 表示不过期（None＝永久等待）。"""
        return None if self.timeout_seconds == 0 else self.timeout_seconds

    @classmethod
    def _canonical(cls, tool_name: str, args: dict[str, Any]) -> str:
        """Compact tamper-proof identity of one write.

        正文只进**摘要**（sha256 + 字节数）而不是原文：授权绑定的必须是全文，但
        1MB 的正文不该塞进 SSE 事件。模型在裁决后换掉正文，摘要就变、canonical 就
        对不上，``decision_for`` 拿不到 allow —— 与 ``code_run`` 用全文做 canonical
        是同一道防线。
        """
        path = str(args.get("path") or "")
        if tool_name == "file_patch":
            old = str(args.get("old_content") or "")
            new = str(args.get("new_content") or "")
            payload = {
                "tool": "file_patch",
                "path": path,
                "old_sha256": _sha256(old),
                "old_bytes": len(old.encode("utf-8")),
                "new_sha256": _sha256(new),
                "new_bytes": len(new.encode("utf-8")),
            }
        else:
            content = str(args.get("content") or "")
            payload = {
                "tool": "file_write",
                "path": path,
                "mode": str(args.get("mode") or "overwrite"),
                "content_sha256": _sha256(content),
                "content_bytes": len(content.encode("utf-8")),
            }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _describe(
        self, tool_name: str, args: dict[str, Any], reason: str, canonical: str
    ) -> dict[str, Any]:
        """One write's card metadata: what lands where, why it needs approval."""
        path = str(args.get("path") or "")
        resolved, _ = self.security.classify(path)
        entry: dict[str, Any] = {
            "tool": tool_name,
            "path": path,
            "resolved": str(resolved),
            "reason": reason,
            "canonical": canonical,
        }
        if tool_name == "file_patch":
            old = str(args.get("old_content") or "")
            new = str(args.get("new_content") or "")
            entry.update({
                "old_preview": old[:_PREVIEW_CHARS],
                "old_bytes": len(old.encode("utf-8")),
                "old_truncated": len(old) > _PREVIEW_CHARS,
                "old_sha256": _sha256(old),
                "new_preview": new[:_PREVIEW_CHARS],
                "new_bytes": len(new.encode("utf-8")),
                "new_truncated": len(new) > _PREVIEW_CHARS,
                "new_sha256": _sha256(new),
            })
        else:
            content = str(args.get("content") or "")
            entry.update({
                "mode": str(args.get("mode") or "overwrite"),
                "preview": content[:_PREVIEW_CHARS],
                "bytes": len(content.encode("utf-8")),
                "truncated": len(content) > _PREVIEW_CHARS,
                "content_sha256": _sha256(content),
            })
        return entry

    @staticmethod
    def _summary_line(entry: dict[str, Any]) -> str:
        """One line per write, and the approval hash basis.

        The content digest must be *in* this line: the registry hashes the joined
        command text, and the frontend echoes that hash back on resolve. A line
        carrying only the path would let a swapped payload ride on the same card.
        """
        if entry["tool"] == "file_patch":
            digest = f"old {entry['old_sha256']} new {entry['new_sha256']}"
            action = f"patch {entry['old_bytes']}B → {entry['new_bytes']}B"
        else:
            digest = str(entry["content_sha256"])
            action = f"{entry['mode']} {entry['bytes']}B"
        return f"{entry['path']} ({action}) sha256:{digest} — {entry['reason']}"

    def _needs_approval(self, args: dict[str, Any]) -> str:
        """Reason this write crosses the sandbox, or "" when it is fine to run."""
        path = str(args.get("path") or "")
        if not path:
            return ""  # 缺参数由工具自己报错，不必占用审批
        resolved, reason = self.security.classify(path)
        # memory/ 是结构性只读区，不走审批也不放开——交给工具层硬拒。
        if self.security.is_memory_path(resolved):
            return ""
        return reason

    async def prepare(self, tool_calls: list[dict]) -> None:
        self._decisions = []
        calls: list[tuple[str, str, dict[str, Any], str]] = []
        for call in tool_calls or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            tool_name = str(function.get("name") or "")
            if tool_name not in _FILE_TOOLS:
                continue
            raw = function.get("arguments") or {}
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw)
            except (TypeError, ValueError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            reason = self._needs_approval(args)
            if not reason:
                continue
            calls.append((tool_name, self._canonical(tool_name, args), args, reason))
        if not calls:
            return
        # auto 模式：全部自动放行，不建审批卡也不等裁决（用户显式选择「自动」模式）。
        # emit=None（无人值守）时仍 fail closed，见 code_run 同口径。
        if self._is_auto() and self.emit is not None:
            self._decisions = [(canonical, "allow", None) for _t, canonical, _args, _r in calls]
            return
        if self.emit is None:
            self._decisions = [(canonical, "deny", None) for _t, canonical, _args, _r in calls]
            return

        entries = [
            self._describe(tool_name, args, reason, canonical)
            for tool_name, canonical, args, reason in calls
        ]
        summaries = [self._summary_line(entry) for entry in entries]
        action = approval_registry.create(
            conversation_id=self.conversation_id,
            node_id="",
            tool_name="file_write",
            command="\n".join(summaries),
            commands=summaries,
            requester=self.caller,
            shell_flavor="file-write",
            timeout_seconds=self.timeout_seconds,
            metadata={"agent_id": self.agent_id, "writes": entries},
        )
        await emit_confirmation_required(
            self.emit,
            action,
            "以下写入越出了 Agent 工作区沙箱（allowed_roots / denied_patterns），"
            "需要确认后才会落盘；本次授权仅执行一次。",
        )
        try:
            decision = await asyncio.wait_for(action.future, timeout=self._timeout())
        except asyncio.TimeoutError:
            # 超时不降级成 deny 让循环继续跑：用户看到的是「审批过期了还在调模型」。
            action.resolved = "timeout"
            raise ApprovalTimeout(action.confirmation_id, "file_write") from None
        finally:
            if action.resolved is None:
                action.resolved = "interrupted"
        if decision == "allow":
            self._decisions = [
                (canonical, "allow", action.confirmation_id)
                for _t, canonical, _args, _r in calls
            ]
            return
        # 用户显式拒绝：中止本轮，而不是把 denied 工具结果喂回模型让它继续跑。
        raise ApprovalDenied(action.confirmation_id, "file_write") from None

    def decision_for(self, tool_name: str, args: dict[str, Any]) -> tuple[str, str | None]:
        """Pop the decision matching this exact write; (outcome, confirmation_id)."""
        canonical = self._canonical(tool_name, args)
        for index, (stored, outcome, confirmation_id) in enumerate(self._decisions):
            if stored == canonical:
                self._decisions.pop(index)
                return outcome, confirmation_id
        # No pre-scan approval means fail closed. ToolRegistry can also be used by
        # non-conversation callers, which must not silently gain out-of-sandbox writes.
        return "deny", None


__all__ = ["FileWriteApprovalBatch"]