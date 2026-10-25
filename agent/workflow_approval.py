"""一次性审批协调器：模型对工作流定义的写操作（create / update / delete）。

与 :class:`agent.code_run_approval.CodeRunApprovalBatch` /
:class:`agent.file_write_approval.FileWriteApprovalBatch` 同契约：agent 循环把一轮的
全部工具调用先交给 ``prepare`` 预扫，命中的 ``capability_call(name="workflow.*")``
合并成**一张**审批卡；工具执行时用 ``decision_for`` 消费已裁决的结果，不再逐次弹卡。

为什么写定义要审批：一张工作流图能执行 code_run、写文件、驱动浏览器，还能挂 cron
无人值守自跑——改图等于授权一段持久自动化。读与运行态（list/get/run/runs/
run_status/cancel）不经过这里，只有会**落库改变定义**的三个动作进审批：

- ``create`` — 新建 v1；
- ``update`` — 保存新版本（老版本留档）；
- ``delete`` — 删除整串版本（运行历史级联清掉）。

fail closed：没挂协调器（无人值守的定时任务、没有事件出口的调用方）时
``decision_for`` 一律返回 deny，写不落库，与 code_run 同口径。
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

# 进审批的写动作；list/get/run/runs/run_status/cancel 是读或运行态，不进卡。
WRITE_ACTIONS = ("create", "update", "delete")

# 卡片里回显的定义预览上限。完整定义仍参与哈希（digest 覆盖全文），预览只是给人
# 看的抽样——一张大图不能整段塞进 SSE 事件。
_PREVIEW_CHARS = 4000

_ACTION_LABELS = {"create": "新建", "update": "保存新版本", "delete": "删除"}


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def mutation_digest(action: str, args: dict) -> str:
    """一次写操作的防偷换摘要：canonical args 的 sha256。

    审批卡的正文行、canonical 身份、审计行共用它——模型在裁决后换掉 name /
    definition / def_id，摘要就变、``decision_for`` 对不上即拒。
    """
    payload = {"action": action}
    for key in ("def_id", "name", "label", "description", "retry_on_reboot", "definition"):
        if key in args:
            payload[key] = args.get(key)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return _sha256(canonical)


def _args_digest(args: dict) -> str:
    return _sha256(json.dumps(args, ensure_ascii=False, sort_keys=True, default=str))


def _node_summary(definition: Any) -> str:
    """把节点图压成一行人话：``3 步: fetch(web) → clean(code_run) → out(cache)``。"""
    nodes = (definition or {}).get("nodes") if isinstance(definition, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return "0 步"
    parts = []
    for node in nodes[:8]:
        if isinstance(node, dict):
            parts.append(f"{node.get('id') or '?'}({node.get('kind') or '?'})")
        else:
            parts.append("?")
    suffix = f" …共 {len(nodes)} 步" if len(nodes) > 8 else ""
    return f"{len(nodes)} 步: " + " → ".join(parts) + suffix


def _canonical(action: str, args: dict) -> str:
    """一行式 canonical 身份；畸形参数返回空串（由工具层报错，不占审批）。"""
    if action == "delete":
        if args.get("def_id") in (None, ""):
            return ""
    elif not str(args.get("definition") or "") and not isinstance(args.get("definition"), dict):
        return ""
    elif action == "create" and not str(args.get("name") or "").strip():
        return ""
    elif action == "update" and args.get("def_id") in (None, ""):
        return ""
    return json.dumps(
        {"action": action, "digest": mutation_digest(action, args)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


class WorkflowApprovalBatch:
    """Pre-approve every workflow-definition write in one model turn as a batch."""

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
        # 注册表到期时刻与本地 wait_for 必须同值，否则两边各按各的到期。
        self.timeout_seconds = int(timeout_seconds)
        # auto 模式放行开关：用户显式选择「自动」执行模式时预扫直接记 allow、不弹卡。
        # 传 callable 时**每次 prepare 现取**（读会话活状态），让回合中途切 auto
        # 立即生效；传 bool 时用快照。与 code_run / file_write 同口径。
        self._auto_approve = auto_approve
        self._decisions: list[tuple[str, str, str | None]] = []

    def _is_auto(self) -> bool:
        aa = self._auto_approve
        return bool(aa() if callable(aa) else aa)

    def _timeout(self) -> float | None:
        """wait_for 的 timeout：恰好 0 表示不过期（None＝永久等待）。"""
        return None if self.timeout_seconds == 0 else self.timeout_seconds

    @staticmethod
    def _extract(call: dict) -> tuple[str, dict] | None:
        """从一条 tool_call 里挑出 workflow 写动作；非目标调用返回 None。"""
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get("name") != "capability_call":
            return None
        raw = function.get("arguments") or {}
        try:
            outer = raw if isinstance(raw, dict) else json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(outer, dict):
            return None
        name = str(outer.get("name") or "")
        if not name.startswith("workflow."):
            return None
        action = name.split(".", 1)[1].lower()
        if action not in WRITE_ACTIONS:
            return None
        inner = outer.get("args")
        if not isinstance(inner, dict):
            return None
        return action, inner

    async def _describe(self, action: str, args: dict) -> dict[str, Any]:
        """一条写操作的卡片元数据；update/delete best-effort 补全现有行的名称。"""
        entry: dict[str, Any] = {
            "action": action,
            "action_label": _ACTION_LABELS.get(action, action),
            "digest": mutation_digest(action, args),
        }
        definition = args.get("definition") if isinstance(args.get("definition"), dict) else None
        if action in ("create", "update"):
            entry["name"] = str(args.get("name") or "")
            entry["label"] = str(args.get("label") or "")
        if action == "update" or action == "delete":
            def_id = args.get("def_id")
            try:
                entry["def_id"] = int(def_id)
            except (TypeError, ValueError):
                entry["def_id"] = def_id
        # 现有行（update/delete 的目标、update 缺 label 时的兜底）：查不到不阻断建卡。
        if action in ("update", "delete"):
            try:
                from agent import workflow_store

                row = await workflow_store.get_def(int(entry.get("def_id")))
                if row:
                    entry.setdefault("name", str(row.get("name") or ""))
                    if not entry.get("label"):
                        entry["label"] = str(row.get("label") or "")
                    if definition is None:
                        row_defn = row.get("definition")
                        if isinstance(row_defn, str):
                            try:
                                row_defn = json.loads(row_defn)
                            except ValueError:
                                row_defn = None
                        if isinstance(row_defn, dict):
                            definition = row_defn
            except Exception:  # noqa: BLE001 — 展示字段查不到不该拦审批
                pass
        entry["node_summary"] = _node_summary(definition)
        if definition is not None:
            text = json.dumps(definition, ensure_ascii=False, default=str)
            entry["preview"] = text[:_PREVIEW_CHARS]
            entry["truncated"] = len(text) > _PREVIEW_CHARS
        return entry

    @staticmethod
    def _summary_line(entry: dict[str, Any]) -> str:
        """一行摘要，同时是审批哈希依据（registry 哈希 join 后的 command 文本）。

        摘要必须带 digest：前端回显 command_hash 对账，且换掉任一字段（name /
        definition / def_id）摘要即变。
        """
        target = entry.get("label") or entry.get("name") or ""
        if entry["action"] == "delete":
            target = f"#{entry.get('def_id')} {target}".strip()
        elif entry.get("def_id") is not None and entry["action"] == "update":
            target = f"#{entry.get('def_id')} {target}".strip()
        return (
            f"workflow.{entry['action']} {target}（{entry['node_summary']}） "
            f"sha256:{entry['digest']} — {entry['action_label']}"
        )

    async def prepare(self, tool_calls: list[dict]) -> None:
        self._decisions = []
        calls: list[tuple[str, dict, str]] = []
        for call in tool_calls or []:
            extracted = self._extract(call)
            if extracted is None:
                continue
            action, inner = extracted
            canonical = _canonical(action, inner)
            if not canonical:
                continue  # 畸形参数由工具层报错，不必占用审批
            calls.append((action, inner, canonical))
        if not calls:
            return
        # auto 模式：全部自动放行，不建审批卡也不等裁决。emit=None（无人值守）
        # 时仍 fail closed，与 code_run / file_write 同口径。
        if self._is_auto() and self.emit is not None:
            self._decisions = [(canonical, "allow", None) for _a, _i, canonical in calls]
            return
        if self.emit is None:
            self._decisions = [(canonical, "deny", None) for _a, _i, canonical in calls]
            return

        entries = [await self._describe(action, inner) for action, inner, _c in calls]
        summaries = [self._summary_line(entry) for entry in entries]
        action_card = approval_registry.create(
            conversation_id=self.conversation_id,
            node_id="",
            tool_name="workflow",
            command="\n".join(summaries),
            commands=summaries,
            requester=self.caller,
            shell_flavor="workflow-mutation",
            timeout_seconds=self.timeout_seconds,
            metadata={"agent_id": self.agent_id, "workflow_ops": entries},
        )
        await emit_confirmation_required(
            self.emit,
            action_card,
            "Agent 将修改工作流定义（可执行 code_run / 写文件 / 驱动浏览器的持久自动化）。"
            "请核对下面的图；本次授权仅写入一次。",
        )
        try:
            decision = await asyncio.wait_for(action_card.future, timeout=self._timeout())
        except asyncio.TimeoutError:
            # 超时不降级成 deny 让循环继续跑：用户看到的是「审批过期了还在调模型」。
            action_card.resolved = "timeout"
            raise ApprovalTimeout(action_card.confirmation_id, "workflow") from None
        finally:
            if action_card.resolved is None:
                action_card.resolved = "interrupted"
        if decision == "allow":
            self._decisions = [
                (canonical, "allow", action_card.confirmation_id)
                for _a, _i, canonical in calls
            ]
            return
        # 用户显式拒绝：中止本轮，而不是把 denied 工具结果喂回模型让它继续跑。
        raise ApprovalDenied(action_card.confirmation_id, "workflow") from None

    def decision_for(self, action: str, args: dict) -> tuple[str, str | None]:
        """Pop the decision matching this exact write; (outcome, confirmation_id)."""
        canonical = _canonical(action, args)
        if not canonical:
            return "deny", None
        for index, (stored, outcome, confirmation_id) in enumerate(self._decisions):
            if stored == canonical:
                self._decisions.pop(index)
                return outcome, confirmation_id
        # 没有预扫审批（未挂协调器、非本轮调用、裁决后偷换载荷）→ fail closed。
        return "deny", None


__all__ = ["WorkflowApprovalBatch", "WRITE_ACTIONS", "mutation_digest"]
