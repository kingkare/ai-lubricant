"""会话运行时状态注册表（``ConversationState`` + ``_states`` 内存真相源）。

设计原则：**一个会话 = 一个内存实例，实例放进进程级注册表 ``_states``；运行时
状态的增删改查全部经本模块走这个实例**。DB 只是写穿持久化，不是热路径的读源。
审批批次（``CodeRunApprovalBatch`` / ``FileWriteApprovalBatch``）、子 agent 的
``_auto_approve`` 回调、三端底栏显示都读这里。

## 实例里放什么（刻意只放运行时状态）

只放 5 个**运行时会话状态**字段（+ 派生 ``auto_approve``）：

    mode / model / reasoning_effort / run_status / run_status_info

**身份段（``agent_id``/``user_id``/``cdp_client_id``/``node_id``/...）不进实例**：
身份段有独立且合法的写入口（建会话 ``create_conversation``、换标签页重绑
``_rebind_conv_scene`` 直改 ``chat_settings``），缓存它反而引入第二份真相。身份段
继续从每个 send/retry 入口已读到的 conv 行取。同理，本模块写穿 ``chat_settings``
时**从 DB 现读整块再合并运行时键**，绝不基于本地快照整块覆盖——否则场景键会被
过期快照抹掉。读不到行时跳过 ``chat_settings`` 写（fail-closed：宁可不持久化这次
mode/effort，也不拿局部 blob 毁掉身份段）。

``reasoning_effort`` 三态：``None``=从未设置（跟随 agent 默认）/ ``""``=显式关闭
思考 / 具体值=指定等级。``_state_from_conv`` 保留 ``None``（不 ``str(or "")`` 抹平）；
写穿时 ``None`` 从 ``chat_settings`` pop 掉 key 回到「跟随默认」。

## 生命周期

- **添加**：``register``（建会话后入列）与 ``load``（首次触碰兜底装载：实例不在
  缓存时用传入 conv 行建，缺省查 DB 建）。进程重启后 ``_states`` 空，下一次
  send/retry 自动兜底装载。
- **改**（唯一写入口）：``update`` —— 改内存 → 写穿 DB → WS 推送，三边同一函数
  做完，字段真变化才写/推（去重短路）。**运行时状态不许绕过它直写 DB**；所有
  端点（send/retry/PATCH settings）一律改调它。
- **读**：``peek``（同步、不触发 DB，审批热路径）与 ``load``（异步兜底）。
  ``load`` 对**已存在的实例绝不打穿**——conv 行只在实例首次创建时采信其运行时
  值；实例已在内存就直接返回。这是对旧实现的根本修复：旧实现「传了 conv 就无条件
  重建缓存」，每轮 send/retry 都拿 DB 行覆盖内存，会话中途切 auto 要等下一轮。
- **busy**：``set_busy`` 标记回合进行中（纯内存标记），``sweep`` 跳过 busy 会话。
- **回收**：``evict``（会话删除时调）与 ``sweep``（空闲 TTL + 无订阅者 + 非 busy，
  由 main.py lifespan 周期驱动）。写穿保证 DB 是最新值，实例逐出后下次触碰
  ``load`` 从 DB 装回即收敛。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 运行状态枚举 ────────────────────────────────────────────────────────────
# 进行中（agent 在干活）+ 暂停（等人）+ 终态（需重试按钮）。
# 前端按 run_status 选文案与是否挂重试按钮，见 STATUS_RETRYABLE。
STATUS_SENDING = "sending"                    # 发送中：已受理，请求派发中
STATUS_WAITING_UPSTREAM = "waiting_upstream"  # 等待上游输出：请求已发，等首 token
STATUS_THINKING = "thinking"                  # 思考中：reasoning 增量流式中
STATUS_PROCESSING = "processing"              # 处理中：content 增量流式中
STATUS_EXECUTING = "executing"                # 执行中：工具运行中
STATUS_RETRYING = "retrying"                  # 重试中（带第几次 + 倒计时）
STATUS_WAITING_APPROVAL = "waiting_approval"  # 等待审批（不显示 status_info）
STATUS_APPROVAL_FAILED = "approval_failed"    # 审批失败暂停
STATUS_WAITING_INPUT = "waiting_input"        # 等待输入：本轮结束，球在用户（静息态）
STATUS_ERROR = "error"                        # 异常（需重试按钮）
STATUS_ABORTED = "aborted"                    # 已中止（需重试按钮）

# 中文标签（前端若自带映射可忽略；这里作服务端/日志兜底）。
STATUS_LABELS: dict[str, str] = {
    STATUS_SENDING: "发送中",
    STATUS_WAITING_UPSTREAM: "等待上游输出",
    STATUS_THINKING: "思考中",
    STATUS_PROCESSING: "处理中",
    STATUS_EXECUTING: "执行中",
    STATUS_RETRYING: "重试中",
    STATUS_WAITING_APPROVAL: "等待审批",
    STATUS_APPROVAL_FAILED: "审批失败暂停",
    STATUS_WAITING_INPUT: "等待输入",
    STATUS_ERROR: "异常",
    STATUS_ABORTED: "已中止",
}

# 终态：底栏挂「重试」按钮，点击走现有 retry_message 续跑链路（保留已做的
# tool_calls，不清空）。
STATUS_RETRYABLE: frozenset[str] = frozenset({
    STATUS_ERROR, STATUS_ABORTED, STATUS_APPROVAL_FAILED,
})

# 写放大护栏：``update`` 只在字段**真变化**时写库/推送（``getattr(state,key)!=val``）。
# 因此「每个 content delta 都调 update(processing)」也无害——首个 delta 落一次库，
# 其余全被去重短路。一回合写库次数与阶段切换数同量级（~工具调用数），不是 delta 数。

# 运行时字段集合：只有这些进实例、走 update 唯一写入口。
_RUNTIME_KEYS = ("mode", "model", "reasoning_effort", "run_status", "run_status_info")


@dataclass
class ConversationState:
    """单个会话的运行时状态。经 ``load``/``register`` 获取，改它 = 调 ``update``。"""

    conv_id: str
    mode: str = "interact"
    model: str = ""
    # None = 从未设置（跟随 agent 默认）；"" = 显式关闭思考；其余 = 指定等级。
    reasoning_effort: str | None = None
    run_status: str = ""
    run_status_info: str = ""
    # 最近一次触碰（monotonic 秒），sweep 据此判空闲。peek/load/update 都刷新。
    last_touch: float = field(default_factory=time.monotonic, repr=False)

    @property
    def auto_approve(self) -> bool:
        """是否自动放行审批。= 本会话当前 mode 是否 auto——读活状态，非快照。"""
        return self.mode == "auto"

    def touch(self) -> None:
        """刷新空闲计时（任何读/写触碰都调，防止活跃会话被 sweep 回收）。"""
        self.last_touch = time.monotonic()

    def snapshot(self) -> dict:
        """给 WS 事件与前端恢复用的全量状态。

        ``reasoning_effort`` 对外仍发字符串（``None`` → ``""``），保持与旧事件的
        JSON 契约一致；三态区分只在服务端内部（``update``/写穿）有意义。
        """
        return {
            "conv_id": self.conv_id,
            "mode": self.mode,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort or "",
            "run_status": self.run_status,
            "run_status_info": self.run_status_info,
            "auto_approve": self.auto_approve,
            "retryable": self.run_status in STATUS_RETRYABLE,
        }


# conv_id → ConversationState（进程内注册表：会话列表内存）。
_states: dict[str, ConversationState] = {}

# 回合进行中的会话集合：sweep 跳过它们。纯内存标记，不写库不广播——busy 是
# 生命周期护栏，不是对外状态。turn 启动 set_busy(True)，done 回调 set_busy(False)。
_busy: set[str] = set()


def _chat_settings_of(conv: dict) -> dict:
    """conv 行的 chat_settings 归一成 dict（JSON 串/None/坏值 → {}）。"""
    cs = conv.get("chat_settings")
    if isinstance(cs, str) and cs:
        try:
            import json
            cs = json.loads(cs)
        except (ValueError, TypeError):
            cs = {}
    return cs if isinstance(cs, dict) else {}


def _state_from_conv(conv: dict) -> ConversationState:
    """从 conv 行 dict 建实例。

    ``reasoning_effort`` 保留 ``None``（key 不存在时），不用 ``str(... or "")``——
    否则「从未设置（跟随默认）」被抹成「显式关闭」，下游 ``effective_effort`` 会误 fork。
    """
    cs = _chat_settings_of(conv)
    return ConversationState(
        conv_id=str(conv.get("id") or ""),
        mode=str(cs.get("mode") or "interact"),
        model=str(conv.get("model") or ""),
        # 三态：key 不在 → None；key 在值为空 → ""。
        reasoning_effort=cs["reasoning_effort"] if "reasoning_effort" in cs else None,
        run_status=str(conv.get("run_status") or ""),
        run_status_info=str(conv.get("run_status_info") or ""),
    )


async def load(conv_id: str, conv: dict | None = None) -> ConversationState:
    """兜底装载一个会话的实例。

    - 实例**已在注册表** → 直接返回并刷新触碰，**不看 conv、不打穿**（运行时
      状态的真相源在内存；DB 行可能落后于内存——写穿是异步的、且失败时落后更多）。
    - 实例**不在注册表** → 用传入 ``conv`` 行建（调用方多半刚查过，避免重复查
      DB）；没传则查 DB。查不到会话 → 返回一个**不入缓存**的空实例（不抛——
      显示路径尽力而为；``update`` 会把它缓存起来，见 update）。
    """
    conv_id = str(conv_id or "")
    if not conv_id:
        return ConversationState(conv_id="")
    cached = _states.get(conv_id)
    if cached is not None:
        cached.touch()
        return cached
    if conv is None:
        try:
            from agent import conversation_store
            conv = await conversation_store.get_conversation(conv_id)
        except Exception as exc:  # noqa: BLE001 — 装载失败不阻断对话
            logger.debug("[conv_state] load failed conv=%s: %s", conv_id, exc)
            conv = None
        if not conv:
            return ConversationState(conv_id=conv_id)
    state = _state_from_conv(conv)
    state.touch()
    _states[conv_id] = state
    return state


def register(conv: dict) -> ConversationState:
    """建会话后把该行入列（= 「添加」的显式时机，同步、无 IO）。

    幂等：已存在就**不覆盖**（运行时字段可能是别处 update 改过的，建会话路径
    不该碰它们）；不存在才用 conv 行建。行缺 id 返回空实例。
    """
    conv_id = str(conv.get("id") or "")
    if not conv_id:
        return ConversationState(conv_id="")
    cached = _states.get(conv_id)
    if cached is not None:
        cached.touch()
        return cached
    state = _state_from_conv(conv)
    state.touch()
    _states[conv_id] = state
    return state


def peek(conv_id: str) -> ConversationState | None:
    """同步读注册表，不触发 DB 查。审批批次的 ``is_auto`` 走这里（热路径，必须同步）。"""
    st = _states.get(str(conv_id or ""))
    if st is not None:
        st.touch()
    return st


async def update(conv_id: str, **patch) -> ConversationState | None:
    """**唯一写入口**：改内存 → 写穿 DB → WS 推送，三条边一个函数里做完。

    ``patch`` 可带 ``mode``/``model``/``reasoning_effort``/``run_status``/``run_status_info``。
    只改真有变化的字段；DB 写与 WS 推送只在有变化时发生（避免每个 delta 一次推送）。
    未 load 过的会话先 load 再改。返回最新状态；失败返回 None（不抛）。

    写穿规则：
    - ``model``/``run_status``/``run_status_info`` 是顶层列，直接落。
    - ``mode``/``reasoning_effort`` 在 ``chat_settings`` JSON 里，**从 DB 现读整块
      再合并**（身份段可能有并发的直写，如换标签页重绑）；读不到行就跳过
      ``chat_settings`` 写（fail-closed，绝不拿局部 blob 覆盖身份段）。
    - ``reasoning_effort=None`` 表示「清空设置、回到跟随 agent 默认」：写穿时从
      ``chat_settings`` pop 掉该 key。
    """
    conv_id = str(conv_id or "")
    if not conv_id:
        return None
    state = _states.get(conv_id)
    if state is None:
        state = await load(conv_id)
        # load 在「查不到会话」时返回一个**未入缓存**的临时对象；update 必须把它
        # 缓存起来，否则后续 peek（审批批次的 is_auto 热路径）永远看不到这次改动。
        _states[conv_id] = state
    changed: dict[str, object] = {}
    for key in _RUNTIME_KEYS:
        if key not in patch:
            continue
        val = patch[key]
        if key == "reasoning_effort":
            # 保留三态：None（清空）/ ""（关闭）/ 具体值。不折叠 None→""。
            new_val = None if val is None else str(val)
        else:
            new_val = "" if val is None else str(val)
        if getattr(state, key) != new_val:
            setattr(state, key, new_val)
            changed[key] = new_val
    state.touch()
    if not changed:
        return state

    # ── 2. 写穿 DB ──
    try:
        from agent import conversation_store
        fields: dict = {}
        if "model" in changed:
            fields["model"] = changed["model"]
        if "run_status" in changed:
            fields["run_status"] = changed["run_status"]
        if "run_status_info" in changed:
            fields["run_status_info"] = changed["run_status_info"]
        # 阶段态写穿：去重已保证「只在真变化时」走到这里，故 run_status/info 直接
        # 落库——一回合写库次数 = 阶段切换次数（~工具调用数），非 delta 数。
        if "mode" in changed or "reasoning_effort" in changed:
            row = None
            try:
                row = await conversation_store.get_conversation(conv_id)
            except Exception as exc:  # noqa: BLE001 — 读不到行按无行处理
                logger.debug("[conv_state] cs read failed conv=%s: %s", conv_id, exc)
            if row is not None:
                cs = _chat_settings_of(row)
                if "mode" in changed:
                    cs["mode"] = state.mode
                if "reasoning_effort" in changed:
                    if state.reasoning_effort is None:
                        cs.pop("reasoning_effort", None)
                    else:
                        cs["reasoning_effort"] = state.reasoning_effort
                fields["chat_settings"] = cs
            else:
                # fail-closed：行读不到就不写 chat_settings——内存态保留本次改动，
                # 但绝不拿「只有运行时键」的局部 blob 覆盖 DB 里的身份段。
                logger.debug(
                    "[conv_state] skip chat_settings write (row missing) conv=%s", conv_id)
        if fields:
            await conversation_store.update_conversation(conv_id, **fields)
    except Exception as exc:  # noqa: BLE001 — 写穿失败不影响内存态与推送
        logger.debug("[conv_state] persist failed conv=%s: %s", conv_id, exc)

    # ── 3. WS 推送：三端同步底栏状态与 mode/model/思考等级 ──
    try:
        from agent import conversation_hub
        await conversation_hub.publish(conv_id, {
            "type": "conversation_state",
            "state": state.snapshot(),
            "changed": sorted(changed),
        })
    except Exception as exc:  # noqa: BLE001 — 推送失败不影响主流程
        logger.debug("[conv_state] publish failed conv=%s: %s", conv_id, exc)
    return state


def set_busy(conv_id: str, flag: bool) -> None:
    """标记/清除「回合进行中」。sweep 据此跳过活跃会话。纯内存，不写库不广播。"""
    conv_id = str(conv_id or "")
    if not conv_id:
        return
    if flag:
        _busy.add(conv_id)
    else:
        _busy.discard(conv_id)


def evict(conv_id: str) -> None:
    """会话删除时回收实例（与 conversation_hub 的 unsub 同生命周期点）。"""
    conv_id = str(conv_id or "")
    if not conv_id:
        return
    _states.pop(conv_id, None)
    _busy.discard(conv_id)


def sweep(ttl_seconds: float = 1800.0, extra_busy=None) -> list[str]:
    """空闲回收：逐出「空闲超过 TTL 且非 busy 且无人订阅」的实例，返回逐出的 id。

    - ``ttl_seconds``：空闲阈值（默认 30 分钟）。活跃会话的 ``last_touch`` 被每次
      ``peek``/``load``/``update`` 刷新，永远到不了阈值。
    - ``extra_busy``：可选 ``(conv_id) -> bool`` 谓词，True = 仍活跃（调用方传入
      自己的 ``_CONV_TASKS`` 判断），与本模块 ``_busy`` 取或。
    - 有人订阅（``conversation_hub.is_watching``）→ 前端还开着这个会话，不回收。

    回收安全：写穿保证 DB 里是最新值，逐出后下次触碰 ``load`` 从 DB 装回即收敛。
    """
    if ttl_seconds <= 0:
        return []
    try:
        from agent import conversation_hub
        watching = conversation_hub.is_watching
    except Exception:  # noqa: BLE001 — hub 不可用时退化为「不按订阅判」
        watching = lambda cid: False  # noqa: E731

    now = time.monotonic()
    evicted: list[str] = []
    for conv_id, st in list(_states.items()):
        if conv_id in _busy:
            continue
        if extra_busy is not None:
            try:
                if extra_busy(conv_id):
                    continue
            except Exception:  # noqa: BLE001 — 谓词失败按不 busy 处理
                pass
        if watching(conv_id):
            continue
        if (now - st.last_touch) <= ttl_seconds:
            continue
        _states.pop(conv_id, None)
        evicted.append(conv_id)
    if evicted:
        logger.debug("[conv_state] swept idle instances: %s", evicted)
    return evicted


def reset() -> None:
    """测试用：清空全部注册表与 busy 标记。"""
    _states.clear()
    _busy.clear()


def stats() -> dict:
    """诊断用：注册表规模与 busy 数（不泄露字段值）。"""
    return {"cached": len(_states), "busy": len(_busy)}


__all__ = [
    "ConversationState",
    "STATUS_LABELS", "STATUS_RETRYABLE",
    "STATUS_SENDING", "STATUS_WAITING_UPSTREAM", "STATUS_THINKING",
    "STATUS_PROCESSING", "STATUS_EXECUTING", "STATUS_RETRYING",
    "STATUS_WAITING_APPROVAL", "STATUS_APPROVAL_FAILED",
    "STATUS_WAITING_INPUT", "STATUS_ERROR", "STATUS_ABORTED",
    "load", "register", "peek", "update", "set_busy",
    "evict", "sweep", "reset", "stats",
]
