"""节点载体调度中枢。

职责边界（与 ``studio_service`` 对齐）：本模块**不碰**节点协议本身，只负责
「把节点二进制送进目标环境 + 让它保持活着」。节点拨回来之后的一切（审批、
capabilities、host_exec、终端、文件上传、会话）照旧走 ``node_server``，零特判。

分层：
    路由 (routes_carriers)      ── 管理端点击 / 轮询进度 / 唤醒按钮
              │
        本模块 carrier_service  ── 进度推进、ctx 注入、保活循环、on_demand 唤醒
              │
        carrier_loader          ── exec 贴的源码 → spec 类（sha1 缓存）
              │
        CarrierSpec 钩子        ── provision / bootstrap / keepalive / status / teardown
              │
        目标环境（ModelScope OpenAPI / AutoClaw relay / …）
              └──────────────► node-execution 拨回 NodeConnect
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import gzip
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

from .carrier_loader import (
    CarrierSpecError,
    invalidate_cache,
    load_carrier_class,
)

# 内置载体 spec 源码目录。**在 specs/carriers/**（specs/ 整棵被 .gitignore）：载体 spec
# 与代码渠道 spec 同一口径——都是「本地管理、DB 是真源」，不进 git。DB 没有对应行时
# 启动灌一份出厂默认。
_BUILTIN_DIR = Path(__file__).resolve().parent.parent / "specs" / "carriers"

# 与 models_carrier 保持一致（避免在 service 层反向依赖 model 模块的循环 import）。
KEEPALIVE_OFF = "off"
KEEPALIVE_ON_DEMAND = "on_demand"
KEEPALIVE_TIMED = "timed"

# 等节点拨回的默认预算。创空间要先构建再启动（可到 15 分钟），AI 沙箱只装二进制
# （分钟级）。这里给通用上限，载体可在 MANIFEST 覆盖 ``register_timeout_seconds``。
DEFAULT_REGISTER_TIMEOUT = 600.0
# 保活循环自己的 tick 间隔（扫一次 DB 的节流，不是"每行多久唤醒一次"）。
KEEPALIVE_TICK_SECONDS = 60.0


class CarrierError(RuntimeError):
    """载体操作失败（管理端回 400）。"""


def _utcnow() -> _dt.datetime:
    return _dt.datetime.utcnow()


# ════════════════════════════════════════════════════════════════════════════
# 钩子拿到的上下文
# ════════════════════════════════════════════════════════════════════════════

class CarrierContext:
    """载体钩子的唯一入口。框架能力都挂在这里，spec 里不需要 import 平台内部模块。

    载体 spec 只拿 ``ctx``：凭据、进度、安装脚本、节点在线判定、发 prompt 给 AI。
    这样换一个载体实现，框架侧不用改一行。
    """

    def __init__(
        self,
        *,
        node_id: str,
        secret: str = "",
        server_url: str = "",
        role: str = "execution",
        config: dict | None = None,
        binding: Any = None,
        public_origin: str = "",
    ) -> None:
        self.node_id = node_id
        self.secret = secret
        self.server_url = (server_url or "").strip()
        self.role = role or "execution"
        self.config = dict(config or {})
        self.binding = binding
        self.public_origin = (public_origin or "").rstrip("/")
        self._progress_cb = None

    # ── 节点凭据 / 地址 ────────────────────────────────────────────────────────
    async def control_plane_secret(self) -> str:
        """从控制面台账（``mc_ac_nodes``）解出入驻凭据。

        复用 ``studio_service._control_plane_secret``：同一把主密钥、同一个库，
        载体无关。入驻时我们可能没拿到 secret（走 reload/重新部署的场景必须走这条）。
        """
        if self.secret:
            return self.secret
        try:
            from .studio_service import _control_plane_secret

            self.secret = await _control_plane_secret(self.node_id) or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("[carrier] control_plane_secret failed: {}", exc)
        return self.secret

    async def get_install_script(self) -> str:
        """服务端渲染 standalone 安装脚本全文（base64 后交给 AI 执行）。

        **不依赖数据面公网**：standalone 模板只用 GitHub 直链 + SERVER/NODE_ID/
        SECRET，不引用 ``$BASE``（``$BASE`` 只在 docker 分支）。所以只要拨回地址
        可达就能装，管理台无需对外暴露。
        """
        from . import nodes_service
        from .node_client.client import get_local_node_client

        bootstrap = await nodes_service.get_node_bootstrap(self.node_id) or {}
        if not bootstrap:
            # bootstrap 记录 30 分钟过期（重新部署场景必然过期）→ 用台账凭据拼一份。
            bootstrap = {}
        bootstrap = {
            "node_id": self.node_id,
            "secret": await self.control_plane_secret(),
            "server_url": self.server_url or str(bootstrap.get("server_url") or ""),
            "role": str(bootstrap.get("role") or self.role),
            "startup_method": "standalone",
            "proxy_config_id": str(bootstrap.get("proxy_config_id") or ""),
        }
        if not bootstrap["secret"]:
            raise CarrierError(f"节点 {self.node_id} 拿不到入驻凭据（secret），无法渲染安装脚本")
        if not bootstrap["server_url"]:
            raise CarrierError(
                f"节点 {self.node_id} 没有拨号地址；请在入驻时填写可达的 server_address"
            )
        install_assets = await nodes_service._collect_install_assets()
        role = bootstrap["role"]
        role_assets = (
            install_assets["ios_host"] if role == "ios_host"
            else install_assets["management"] if role in ("management", "passive_management")
            else install_assets["execution"]
        )
        # ⚠ nodes_service 模块里有个**同名单例实例**（模块末尾 `nodes_service = NodesService()`）：
        # 模块级函数直接调，方法必须走那个实例。
        from .nodes_service import nodes_service as _ns

        proxy_fields = await _ns.resolved_proxy_fields_for(
            bootstrap.get("proxy_config_id") or "")
        try:
            from server import config as _scfg
            agent_image = _scfg.settings.agent_compose_agent_image
            mirror_fields = _scfg.settings.mirror_settings()
        except Exception:  # noqa: BLE001 - 单测/无 config 时给空，脚本里镜像段会省略
            agent_image = ""
            mirror_fields = {}

        # public_base_url 只被 docker 分支用；standalone 不引用。给控制面地址占位即可。
        return nodes_service.render_install_script(
            bootstrap,
            public_base_url=self.server_url or self.public_origin,
            agent_image=agent_image,
            assets=role_assets,
            execution_assets=install_assets["execution"],
            management_assets=install_assets["management"],
            ios_host_assets=install_assets["ios_host"],
            runtime_assets=install_assets["runtime"],
            proxy_fields=proxy_fields,
            mirror_fields=mirror_fields,
        )

    async def get_install_prompt(self) -> tuple[str, str]:
        """(第 1 发命令, 第 2 发命令) —— gzip+base64 两发式安装。

        为什么两发（Phase 0 实测）：
        · 一次发一条**简单命令**才过沙箱安全闸——多动作串烧（set -e + curl + nohup）
          会被 ``DANGEROUS_ACTIVITY_BLOCKED`` 整条拒掉，且没有 tool_use 可证据化。
        · 脚本本身不贴进 prompt：避免 agent 逐字抄长脚本（实测会回显我们的话而非
          执行），也免去 shell 转义地狱。
        · 第 1 发附带 ``wc -c`` 输出，服务端可与本地渲染的字节数对账自校验。

        为什么 gzip（E2E 实测）：12KB 脚本裸 base64 是 16012 字符的单条命令，云端
        agent 90s 内一个内容帧都没吐（首字超时）；gzip+base64 后 6732 字符。对账
        口径不变：``wc -c`` 报的是**解压后**字节数，与 ``expected_install_bytes()``
        同一口径。
        """
        script = await self.get_install_script()
        gz = gzip.compress(script.encode("utf-8"), compresslevel=9)
        b64 = base64.b64encode(gz).decode("ascii")
        nbytes = len(script.encode("utf-8"))
        write_cmd = (
            f"printf '%s' '{b64}' | base64 -d | gunzip > /tmp/node-install.sh && "
            f"wc -c /tmp/node-install.sh"
        )
        run_cmd = "bash /tmp/node-install.sh"
        logger.info(
            "[carrier] install script rendered node_id={} bytes={} gz={} b64_chars={}",
            self.node_id, nbytes, len(gz), len(b64),
        )
        return write_cmd, run_cmd

    async def expected_install_bytes(self) -> int:
        return len((await self.get_install_script()).encode("utf-8"))

    # ── 节点在线判定 ──────────────────────────────────────────────────────────
    async def is_node_online(self) -> bool:
        """节点是否已拨回并被控制面认作在线。这是 bootstrap 的**真正成功判据**——
        永远不采信 agent 的自述（Phase 0 实测：会回显 prompt、会被安全闸拒）。"""
        return await is_node_online(self.node_id)

    async def wait_for_node_online(self, timeout: float = 60.0, interval: float = 3.0) -> bool:
        """轮询等节点拨回，返回是否在预算内连上。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.is_node_online():
                return True
            await asyncio.sleep(interval)
        return await self.is_node_online()

    # ── 调本平台代码渠道（AI 载体用）──────────────────────────────────────────
    async def call_channel_chat(
        self,
        provider: str,
        message: str,
        *,
        model: str = "",
        username: str = "",
        timeout: float = 300.0,
    ) -> str:
        """通过本平台的代码渠道实例发一条自然语言，聚合返回它的正文。

        AI 载体用它「让 agent 跑命令」，而**不重写 relay 协议**——复用渠道侧已有的
        选路/凭证/流式解析（规格同 ``dispatch``：这里只取正文，工具调用忽略）。
        """
        from rate_limiter import ModelClientPool

        pool = ModelClientPool.get_provider_pool(provider)
        if pool is None:
            raise CarrierError(f"代码渠道 {provider} 不存在或未加载")

        target = None
        for c in list(getattr(pool, "clients", []) or []):
            prov = getattr(c, "provider", None)
            if prov is None:
                continue
            if username and str(getattr(prov, "username", "")) != username:
                continue
            try:
                if prov.is_init():
                    target = prov
                    break
            except Exception:  # noqa: BLE001 - 单个账号 init 探测失败就换下一个
                continue
        if target is None:
            raise CarrierError(
                f"代码渠道 {provider} 没有可用账号"
                + (f"（指定账号 {username}）" if username else "")
            )
        if not model:
            raise CarrierError("未指定模型（config.model）")

        texts: list[str] = []
        thinking: list[str] = []

        async def _run():
            async for frame in target._do_stream_chat(
                model, [{"role": "user", "content": message}]
            ):
                if not isinstance(frame, dict):
                    continue
                if frame.get("content"):
                    texts.append(str(frame["content"]))
                if frame.get("thinking"):
                    thinking.append(str(frame["thinking"]))

        try:
            await asyncio.wait_for(_run(), timeout=timeout)
        except asyncio.TimeoutError:
            raise CarrierError(f"AI 渠道 {provider} 超时（{timeout:.0f}s）未返回") from None
        except CarrierError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CarrierError(f"AI 渠道 {provider} 调用失败: {type(exc).__name__}: {exc}") from exc

        text = "".join(texts).strip()
        if not text:
            hint = "".join(thinking).strip()
            raise CarrierError(
                "AI 渠道没返回正文"
                + (f"（仅有进度帧：{hint[:160]}）" if hint else "（空响应）")
            )
        return text

    # ── 进度 / 探针落库 ───────────────────────────────────────────────────────
    def set_progress_sink(self, sink) -> None:
        """由 carrier_service 注入：把进度写回 ``NodeCarrierBinding``。"""
        self._progress_cb = sink

    async def set_progress(self, step: str, status: str, message: str = "") -> None:
        if self._progress_cb is None:
            return
        try:
            await self._progress_cb(step, status, message)
        except Exception as exc:  # noqa: BLE001 - 进度写失败不打断部署
            logger.warning("[carrier] progress sink failed: {}", exc)

    async def mark_keepalive(self, ok: bool, note: str = "") -> None:
        if self._progress_cb is None:
            return
        try:
            await self._progress_cb(None, ok, note)  # None = 探针记录而非步骤
        except Exception:  # noqa: BLE001
            pass


# ════════════════════════════════════════════════════════════════════════════
# 节点在线判定（载体系无关）
# ════════════════════════════════════════════════════════════════════════════

async def is_node_online(node_id: str) -> bool:
    """查控制面：该节点当前是否 online。控制面不可用返回 False（保守）。"""
    try:
        from .node_client.client import get_local_node_client

        nodes = await get_local_node_client().list_nodes()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[carrier] list_nodes unavailable: {}", exc)
        return False
    for n in nodes or []:
        if str(n.get("node_id") or "") == str(node_id):
            return bool(n.get("online") or n.get("connected"))
    return False


async def resolve_server_url(node_id: str, explicit: str = "") -> str:
    """该节点的拨回地址，沿用创空间的三段优先级（不问第二遍）。"""
    if (explicit or "").strip():
        return explicit.strip().rstrip("/")
    try:
        from . import studio_service as _ss

        return await _ss._resolve_server_url(node_id, None, "")
    except Exception:  # noqa: BLE001
        return ""


# ════════════════════════════════════════════════════════════════════════════
# spec 载入（DB 为真源，出厂文件为默认）
# ════════════════════════════════════════════════════════════════════════════

async def seed_builtin_specs() -> int:
    """从 ``specs/carriers/`` 灌出厂 spec。

    两种情况会动 DB 行：
    ① **缺行** → 插入（新载体上线）。
    ② 行还在且 ``builtin=True``，但源码 sha1 与文件不一致 → **更新**：出厂文件的
       bug 修复必须能随重启生效，否则「文件改了、DB 还是老代码」——生产跑的和
       仓里读的对不上（2026-09-30 实测：重试改动在文件里 12474B，DB 还是 9004B）。
    ③ ``builtin=False`` → **绝不覆盖**（运营在管理端 put_carrier_spec 改过，重启
       不能把他的改动冲掉）。

    返回被新建/更新的行数。
    """
    if not _BUILTIN_DIR.is_dir():
        return 0
    from .models_carrier import NodeCarrierSpec

    seeded = 0
    for path in sorted(_BUILTIN_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = path.stem
        try:
            source = path.read_text(encoding="utf-8")
            load_carrier_class(name, source)  # 出厂文件坏了就别灌，避免一启动就废
            digest = _sha1(source)
            row = await NodeCarrierSpec.get_or_none(name=name)
            if row is None:
                await NodeCarrierSpec.create(
                    name=name, label="", source=source, sha1=digest,
                    builtin=True, enabled=True,
                )
                seeded += 1
                continue
            if not row.builtin:
                continue  # 运营改过的，保持「不覆盖」
            if str(row.sha1 or "") == digest:
                continue  # 没变，不动
            row.source, row.sha1, row.load_error = source, digest, ""
            # label 留着运营填的；出厂行 label 为空则从 MANIFEST 补
            manifest = dict(getattr(load_carrier_class(name, source), "MANIFEST", {}) or {})
            if not str(row.label or "").strip() and manifest.get("label"):
                row.label = str(manifest["label"])
            await row.save()
            from .carrier_loader import invalidate_cache

            invalidate_cache(name)
            seeded += 1
            logger.info("[carrier] builtin {} updated sha1={}", name, digest[:12])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[carrier] seed builtin {} failed: {}", name, exc)
    if seeded:
        logger.info("[carrier] seeded/updated {} builtin spec(s)", seeded)
    return seeded


def _sha1(source: str) -> str:
    import hashlib

    return hashlib.sha1((source or "").encode("utf-8")).hexdigest()


async def sync_studio_bindings() -> int:
    """Phase 3 迁移：给**已有创空间部署**补挂载体绑定（缺行才补，不覆盖）。

    ``StudioDeployment`` 有行但 ``NodeCarrierBinding`` 没有 → 补一条
    ``carrier_name=modelscope_studio``、``keepalive_policy=timed``（对齐旧循环的
    5 分钟节奏）。补挂之后旧保活循环跳过这些行（互斥，见 ``_keepalive_tick``），
    由载体共享循环用 ``modelscope_studio`` spec 的 ``keepalive`` 接管——同一份
    ``keepalive_one`` 实现，行为不变。

    只补**台账里还存在的节点**：孤儿（节点已删、创空间仓库还在）不补绑定——
    补了载体循环也会因节点不在台账而跳过，白占一行。返回补挂数量。
    """
    from .models_carrier import NodeCarrierBinding
    from .models_studio import StudioDeployment
    from .node_client.client import get_local_node_client

    studio_rows = await StudioDeployment.filter().exclude(repo_url="").all()
    if not studio_rows:
        return 0
    try:
        live = await get_local_node_client().list_nodes()
    except Exception as exc:  # noqa: BLE001 - 控制面不可用则无从判 known，本轮不补
        logger.info("[carrier] sync_studio_bindings: node list unavailable, skip: {}", exc)
        return 0
    known = {str(n.get("node_id") or "") for n in (live or [])}

    created = 0
    for row in studio_rows:
        if row.node_id not in known:
            continue
        if await NodeCarrierBinding.get_or_none(node_id=row.node_id) is not None:
            continue
        await NodeCarrierBinding.create(
            node_id=row.node_id,
            carrier_name="modelscope_studio",
            config={},
            keepalive_policy=KEEPALIVE_TIMED,
            keepalive_interval_minutes=5,
        )
        created += 1
        logger.info("[carrier] backfilled studio binding for {}", row.node_id)
    if created:
        logger.info("[carrier] backfilled {} studio binding(s)", created)
    return created


async def ensure_studio_binding(node_id: str) -> None:
    """创空间部署成功后补挂一条载体绑定（幂等，已有则不动）。

    这样新部署的创空间节点立刻进入载体统一定标（保活由载体共享循环接管，
    旧循环因互斥自动跳过它）。
    """
    from .models_carrier import NodeCarrierBinding

    row = await NodeCarrierBinding.get_or_none(node_id=node_id)
    if row is not None:
        return
    await NodeCarrierBinding.create(
        node_id=node_id,
        carrier_name="modelscope_studio",
        config={},
        keepalive_policy=KEEPALIVE_TIMED,
        keepalive_interval_minutes=5,
    )


async def remove_studio_binding(node_id: str) -> None:
    """删除创空间部署账本时同步摘掉载体绑定（缺行不报错）。"""
    from .models_carrier import NodeCarrierBinding

    await NodeCarrierBinding.filter(node_id=node_id).delete()


async def load_spec(name: str) -> type:
    """从 DB 取 spec 源码并加载（sha1 未变则走 loader 缓存，不重复 exec）。"""
    from .models_carrier import NodeCarrierSpec

    row = await NodeCarrierSpec.get_or_none(name=name)
    if row is None:
        raise CarrierError(f"载体 {name} 不存在（未内置、也未自定义）")
    if not row.enabled:
        raise CarrierError(f"载体 {name} 已停用")
    try:
        cls = load_carrier_class(name, row.source)
    except CarrierSpecError as exc:
        raise CarrierError(str(exc)) from exc
    return cls


async def load_spec_for_binding(binding) -> type:
    """解析这个绑定**实际用**的载体 spec（见 :func:`resolve_spec` 的优先级）。"""
    return await resolve_spec(
        source=str(getattr(binding, "source", "") or ""),
        manager_node_id=str(getattr(binding, "manager_node_id", "") or ""),
        carrier_name=getattr(binding, "carrier_name", "") or "",
        node_id=getattr(binding, "node_id", "") or "",
    )


async def resolve_spec(
    *, source: str = "", manager_node_id: str = "",
    carrier_name: str = "", node_id: str = "",
) -> type:
    """解析载体 spec 源码 → 可执行类。三档优先级：

    1. ``source`` 非空 —— **自定义管理节点**（type=custom）贴的代码就存在自己的
       绑定行上，直接用（sha1 缓存键带 node_id，改代码即热更新）。
    2. ``manager_node_id`` 非空 —— 自定义管理节点名下的**子执行节点**：代码与参数
       都归管理节点管，顺着回指去找管理节点绑定上的源码。
    3. 其余 —— 按 ``carrier_name`` 查 NodeCarrierSpec（出厂内置载体，如 AutoClaw
       沙箱）。

    为什么代码放管理节点行、不放 spec 表：载体代码是**这个管理节点的属性**——
    改它只影响该管理节点名下的子节点，不会串到别的管理节点；也不必为一台管理
    节点在 spec 表里占一个全局名字。
    """
    source = (source or "").strip()
    if source:
        key = f"custom:{node_id or manager_node_id}"
        try:
            return load_carrier_class(key, source)
        except CarrierSpecError as exc:
            raise CarrierError(f"管理节点 {node_id or manager_node_id} 的载体代码加载失败: {exc}") from exc

    manager_node_id = (manager_node_id or "").strip()
    if manager_node_id:
        from .models_carrier import NodeCarrierBinding

        mgr = await NodeCarrierBinding.get_or_none(node_id=manager_node_id)
        if mgr is None:
            raise CarrierError(f"管理节点 {manager_node_id} 不存在（子节点 {node_id} 无法 provisioning）")
        mgr_source = str(getattr(mgr, "source", "") or "").strip()
        if mgr_source:
            # 自定义管理节点：贴的代码就在管理节点行上
            try:
                return load_carrier_class(f"custom:{manager_node_id}", mgr_source)
            except CarrierSpecError as exc:
                raise CarrierError(
                    f"管理节点 {manager_node_id} 的载体代码加载失败: {exc}"
                ) from exc
        # 内置载体的管理节点（创空间 / 沙箱…）：source 为空，按 carrier_name 查出厂 spec
        mgr_carrier = str(getattr(mgr, "carrier_name", "") or "").strip()
        if mgr_carrier and mgr_carrier != "custom":
            return await load_spec(mgr_carrier)
        raise CarrierError(
            f"管理节点 {manager_node_id} 既没有载体代码也没有载体名（子节点 {node_id} 无法 provisioning）"
        )

    return await load_spec(carrier_name)


async def list_carriers() -> list[dict]:
    """前端渲染 Tab 用：返回每个载体的 MANIFEST + 实现了哪些钩子。"""
    from .carrier_loader import hook_names
    from .models_carrier import NodeCarrierSpec

    out: list[dict] = []
    for row in await NodeCarrierSpec.filter(enabled=True).order_by("name"):
        try:
            cls = load_carrier_class(row.name, row.source)
            manifest = dict(cls.MANIFEST)
            manifest["hooks"] = list(hook_names(cls))
            manifest["spec_sha1"] = row.sha1[:12]
            manifest["load_error"] = ""
            out.append(manifest)
        except Exception as exc:  # noqa: BLE001 - 一个坏 spec 不该让列表打不开
            out.append({
                "name": row.name, "label": row.label or row.name,
                "hooks": [], "spec_sha1": row.sha1[:12],
                "load_error": str(exc)[:300],
            })
    return out


def _build_ctx(binding, *, secret: str = "", server_url: str = "",
               role: str = "execution") -> CarrierContext:
    ctx = CarrierContext(
        node_id=binding.node_id,
        secret=secret,
        server_url=server_url,
        role=role,
        config=dict(binding.config or {}),
        binding=binding,
    )
    return ctx


async def _progress_sink_for(binding, node_id: str):
    """返回 (step_cb, keepalive_cb) 两个回调，写回 binding 行。"""
    from .models_carrier import NodeCarrierBinding

    async def step(step_name, status, message=""):
        # step=None 语义下 status 承载 ok(bool)，message 是探针结论
        if step_name is None:
            binding.last_keepalive_at = _utcnow()
            binding.last_keepalive_ok = bool(status)
            binding.last_keepalive_note = str(message or "")[:512]
            await NodeCarrierBinding.filter(pk=node_id).update(
                last_keepalive_at=binding.last_keepalive_at,
                last_keepalive_ok=binding.last_keepalive_ok,
                last_keepalive_note=binding.last_keepalive_note,
                updated_at=_utcnow(),
            )
            return
        progress = list(binding.progress or [])
        found = False
        for item in progress:
            if item.get("step") == step_name:
                item.update({"step": step_name, "status": status, "message": message})
                found = True
                break
        if not found:
            progress.append({"step": step_name, "status": status, "message": message})
        binding.progress = progress
        await NodeCarrierBinding.filter(pk=node_id).update(
            progress=progress, updated_at=_utcnow(),
        )

    return step


# ════════════════════════════════════════════════════════════════════════════
# 部署 / 保活 / 状态 / 销毁（路由层只调这些）
# ════════════════════════════════════════════════════════════════════════════

async def deploy_carrier_node(
    node_id: str,
    carrier_name: str,
    *,
    config: dict | None = None,
    server_url: str = "",
    role: str = "execution",
    keepalive_policy: str = KEEPALIVE_ON_DEMAND,
    keepalive_interval_minutes: int = 8,
    source: str = "",
    manager_node_id: str = "",
    config_from_manager: bool = False,
) -> dict:
    """部署一个节点到指定载体（provision → bootstrap → 等拨回）。

    幂等：同一节点重复调用覆盖 binding 行并重跑（对齐 StudioDeployment 的
    「重新部署就地覆盖」语义，不做历史版本）。

    ``source`` / ``manager_node_id`` 用于**自定义管理节点**路径：子执行节点的
    代码与参数都归管理节点（``source`` 留空、只回指 ``manager_node_id``，
    ``config_from_manager=True`` 让子节点继承管理节点的参数）。
    """
    from .carrier_loader import call_hook
    from .models_carrier import NodeCarrierBinding

    spec = await resolve_spec(
        source=source, manager_node_id=manager_node_id,
        carrier_name=carrier_name, node_id=node_id,
    )
    manifest = dict(getattr(spec, "MANIFEST", {}) or {})
    register_timeout = float(manifest.get("register_timeout_seconds") or DEFAULT_REGISTER_TIMEOUT)

    # 自定义管理节点：参数在管理节点上，子节点继承（子节点自己的 config 只做覆盖）。
    effective_config = dict(config or {})
    if config_from_manager and manager_node_id:
        mgr = await NodeCarrierBinding.get_or_none(node_id=manager_node_id)
        if mgr:
            effective_config = {**dict(mgr.config or {}), **effective_config}
            if not keepalive_interval_minutes:
                keepalive_interval_minutes = int(mgr.keepalive_interval_minutes or 8)
            keepalive_policy = mgr.keepalive_policy or keepalive_policy

    binding, _ = await NodeCarrierBinding.get_or_create(
        node_id=node_id,
        defaults={
            "carrier_name": carrier_name,
            "config": dict(effective_config),
            "keepalive_policy": keepalive_policy,
            "keepalive_interval_minutes": keepalive_interval_minutes,
            "source": source,
            "manager_node_id": manager_node_id,
        },
    )
    binding.carrier_name = carrier_name
    binding.config = dict(effective_config)
    binding.source = source
    binding.manager_node_id = manager_node_id
    binding.keepalive_policy = keepalive_policy
    binding.keepalive_interval_minutes = keepalive_interval_minutes
    binding.deploy_status = "provisioning"
    binding.progress = []
    binding.bootstrap_attempts = 0
    binding.bootstrap_last_error = ""
    await binding.save()

    ctx = _build_ctx(binding, server_url=server_url, role=role)
    ctx.set_progress_sink(await _progress_sink_for(binding, node_id))

    async def _fail(stage: str, exc: Exception) -> dict:
        msg = str(exc)[:500]
        logger.warning("[carrier] {} {} failed: {}", node_id, stage, msg)
        await ctx.set_progress(stage, "failed", msg)
        binding.deploy_status = "failed"
        binding.bootstrap_last_error = msg
        await binding.save(update_fields=["deploy_status", "bootstrap_last_error", "updated_at"])
        return {"node_id": node_id, "carrier": carrier_name, "ok": False,
                "stage": stage, "error": msg}

    await ctx.set_progress("provision", "running", "")
    try:
        await call_hook(spec, "provision", ctx)
    except Exception as exc:  # noqa: BLE001
        return await _fail("provision", exc)
    await ctx.set_progress("provision", "done", "")

    binding.deploy_status = "bootstrapping"
    await binding.save(update_fields=["deploy_status", "updated_at"])
    await ctx.set_progress("bootstrap", "running", "")
    try:
        result = await call_hook(spec, "bootstrap", ctx) or {}
    except Exception as exc:  # noqa: BLE001
        return await _fail("bootstrap", exc)
    await ctx.set_progress("bootstrap", "done", str(result.get("note") or ""))

    # ③ 等拨回：真正成功判据（绝不采信 agent 自述 —— Phase 0 实测会回显 prompt、会被安全闸拒）
    binding.deploy_status = "waiting_node"
    await binding.save(update_fields=["deploy_status", "updated_at"])
    await ctx.set_progress("waiting_node", "running", "")
    online = await ctx.wait_for_node_online(timeout=register_timeout, interval=10.0)
    if not online:
        return await _fail(
            "waiting_node",
            CarrierError(f"节点 {node_id} 在 {register_timeout:.0f}s 内未拨回控制面"),
        )
    await ctx.set_progress("waiting_node", "done", "节点已拨回")
    binding.deploy_status = "ready"
    binding.bootstrap_attempts = 0
    binding.bootstrap_last_error = ""
    await binding.save(update_fields=["deploy_status", "bootstrap_attempts",
                                      "bootstrap_last_error", "updated_at"])
    return {"node_id": node_id, "carrier": carrier_name, "ok": True,
            "stage": "ready", "note": str(result.get("note") or "")}


async def keepalive_now(node_id: str) -> dict:
    """手动 / 定时 / 按需统一入口：对该节点的载体跑一次 keepalive。

    对齐 studio 范式：节点在线只更新探针时间；Sleeping/Stopped 唤醒；Running 但
    离线由各载体 spec 自己决定（studio 只告警不重建，AI 沙箱可重跑 bootstrap）。

    **管理节点行本身不跑 keepalive**：它没有宿主（passive，不拨号），保活对象是它
    名下的子执行节点。传管理节点 id 进来直接回 note，不误把它当节点去装。
    """
    from .carrier_loader import call_hook
    from .models_carrier import NodeCarrierBinding

    binding = await NodeCarrierBinding.get_or_none(node_id=node_id)
    if binding is None:
        raise CarrierError(f"节点 {node_id} 没有载体绑定")
    if bool(getattr(binding, "is_manager", False)):
        return {"node_id": node_id, "carrier": binding.carrier_name, "ok": True,
                "note": "该节点是管理节点（无宿主），保活对象是它名下的子节点"}
    spec = await load_spec_for_binding(binding)
    ctx = _build_ctx(binding)
    ctx.set_progress_sink(await _progress_sink_for(binding, node_id))

    try:
        result = await call_hook(spec, "keepalive", ctx) or {}
    except Exception as exc:  # noqa: BLE001
        await ctx.mark_keepalive(False, str(exc)[:300])
        raise CarrierError(f"保活失败: {exc}") from exc
    ok = bool(result.get("ok", True))
    await ctx.mark_keepalive(ok, str(result.get("note") or ""))
    return {"node_id": node_id, "carrier": binding.carrier_name, "ok": ok,
            "note": str(result.get("note") or "")}


async def get_carrier_status(node_id: str) -> dict:
    """宿主运行时状态 + 节点在线态 + 保活策略，一次返回给面板。"""
    from .carrier_loader import call_hook
    from .models_carrier import NodeCarrierBinding

    binding = await NodeCarrierBinding.get_or_none(node_id=node_id)
    if binding is None:
        return {"node_id": node_id, "bound": False}
    spec = await load_spec_for_binding(binding)
    ctx = _build_ctx(binding)
    try:
        status = await call_hook(spec, "status", ctx) or {}
    except Exception as exc:  # noqa: BLE001
        status = {"ok": False, "error": str(exc)[:300]}
    return {
        "node_id": node_id, "bound": True,
        "carrier": binding.carrier_name,
        "deploy_status": binding.deploy_status,
        "progress": binding.progress or [],
        "keepalive_policy": binding.keepalive_policy,
        "keepalive_interval_minutes": binding.keepalive_interval_minutes,
        "last_keepalive_at": binding.last_keepalive_at.isoformat() if binding.last_keepalive_at else "",
        "last_keepalive_ok": binding.last_keepalive_ok,
        "last_keepalive_note": binding.last_keepalive_note or "",
        "node_online": await is_node_online(node_id),
        "host": status,
    }


async def teardown_carrier(node_id: str) -> dict:
    """解绑载体。**不清理远端资产**——创空间仓库 / AI 沙箱都是用户资产，
    删除由用户在各自平台手动做；这里只解平台对它的管辖。"""
    from .carrier_loader import call_hook
    from .models_carrier import NodeCarrierBinding

    binding = await NodeCarrierBinding.get_or_none(node_id=node_id)
    if binding is None:
        return {"node_id": node_id, "ok": True, "note": "未绑定"}
    spec = await load_spec_for_binding(binding)
    ctx = _build_ctx(binding)
    try:
        await call_hook(spec, "teardown", ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[carrier] {} teardown hook failed: {}", node_id, exc)
    await binding.delete()
    return {"node_id": node_id, "ok": True, "note": "已解绑"}


# ════════════════════════════════════════════════════════════════════════════
# 共享保活循环（timed 策略）+ on_demand 唤醒钩子
# ════════════════════════════════════════════════════════════════════════════

async def keepalive_loop() -> None:
    """后台循环：每 KEEPALIVE_TICK_SECONDS 扫一次 timed 绑定并唤醒。

    替代 ``studio_deploy.keepalive_loop``（其逻辑搬进 modelscope_studio spec 的
    keepalive，由本循环统一驱动）。无 timed 绑定时只是一次 DB 查询，代价可忽略。
    """
    from .models_carrier import NodeCarrierBinding

    logger.info("[carrier] keepalive loop started (tick={}s)", KEEPALIVE_TICK_SECONDS)
    while True:
        try:
            rows = await NodeCarrierBinding.filter(keepalive_policy=KEEPALIVE_TIMED)
            now = _utcnow()
            for row in rows:
                # 管理节点自己没有宿主（is_manager=True）——保活对象是它名下的
                # 子节点，对它跑 keepalive 无意义。
                if bool(getattr(row, "is_manager", False)):
                    continue
                last = row.last_keepalive_at
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=None)
                due = last is None or (now - last).total_seconds() >= max(
                    int(row.keepalive_interval_minutes or 8), 1) * 60
                if not due:
                    continue
                try:
                    result = await keepalive_now(row.node_id)
                    logger.info("[carrier] timed keepalive {} ok={} {}",
                                row.node_id, result.get("ok"), result.get("note") or "")
                except Exception as exc:  # noqa: BLE001 - 单节点失败不影响整轮
                    logger.warning("[carrier] timed keepalive {} failed: {}", row.node_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("[carrier] keepalive tick failed", exc_info=True)
        await asyncio.sleep(KEEPALIVE_TICK_SECONDS)


async def ensure_node_awake(node_id: str) -> bool:
    """on_demand 唤醒钩子：host_exec / 会话派发**之前**调用。

    没绑载体的普通节点 → 直接 True（一次 DB 主键查询，零 RPC）。
    绑了载体的节点在线 → 直接 True（一次 ListNodes RPC，确认没死）。
    离线 + on_demand 绑定 → 跑一次 keepalive 再判在线；冷却窗口内不重复打
    （一轮 prompt 10~30s，不能每次派发都付这个代价）。
    返回 True 才允许往下下发。
    """
    from .models_carrier import NodeCarrierBinding

    # 普通节点（绝大多数）：没绑定载体 → 0 RPC，直接放行
    binding = await NodeCarrierBinding.get_or_none(node_id=node_id)
    if binding is None or binding.keepalive_policy != KEEPALIVE_ON_DEMAND:
        return True
    # 载体节点在线：放行
    if await is_node_online(node_id):
        return True
    # 离线 → 冷却窗口内不再打 prompt（上次唤醒刚覆盖的 10 分钟还没过）
    if binding.last_keepalive_at is not None:
        elapsed = (_utcnow() - binding.last_keepalive_at.replace(tzinfo=None)).total_seconds()
        if elapsed < max(int(binding.on_demand_cooldown_seconds or 300), 60):
            return await is_node_online(node_id)
    # 真正唤醒：载体 keepalive 会先 wake 沙箱再判节点，可能重跑 bootstrap
    try:
        await keepalive_now(node_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[carrier] on-demand wake {} failed: {}", node_id, exc)
        return False
    return await is_node_online(node_id)


async def ensure_node_awake_maybe(node_id: str) -> bool:
    """``NodeClient.host_exec`` 的入口钩子：不阻断非载体节点，失败也不卡命令下发。

    用于低层 RPC 客户端 —— 那里不该硬抛「载体唤醒失败」（普通节点也走这条路）：
    普通节点直接 True；载体节点唤醒失败返回 True 让上层看到节点真离线时的 UNAVAILABLE，
    而不是把载体的异常冒泡成 host_exec 异常。
    """
    try:
        return await ensure_node_awake(node_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[carrier] ensure_node_awake soft-fail {}: {}", node_id, exc)
        return True


# ════════════════════════════════════════════════════════════════════════════
# 虚拟管理节点：增删改查 + 子节点管理 + 动态选项
# ════════════════════════════════════════════════════════════════════════════

async def list_node_managers() -> list[dict]:
    """列出所有虚拟管理节点（is_manager=True），带 spec label 与子节点在线态。"""
    from .models_carrier import NodeCarrierBinding

    rows = await NodeCarrierBinding.filter(is_manager=True).order_by("created_at")
    children = await NodeCarrierBinding.filter(is_manager=False, manager_node_id__not="")
    children_by_mgr: dict[str, list] = {}
    for c in children:
        children_by_mgr.setdefault(c.manager_node_id, []).append(c.node_id)
    out: list[dict] = []
    for m in rows:
        out.append({
            "manager_id": m.node_id,
            "name": m.name,
            "carrier_name": m.carrier_name,
            "config": dict(m.config or {}),
            "keepalive_policy": m.keepalive_policy,
            "keepalive_interval_minutes": m.keepalive_interval_minutes,
            "children_count": len(children_by_mgr.get(m.node_id, [])),
            "child_node_ids": children_by_mgr.get(m.node_id, []),
            "created_at": m.created_at.isoformat() if m.created_at else "",
        })
    return out


async def create_node_manager(
    *,
    name: str = "",
    spec_name: str = "",
    config: dict | None = None,
    keepalive_policy: str = KEEPALIVE_ON_DEMAND,
    keepalive_interval_minutes: int = 8,
    source: str = "",
) -> dict:
    """创建虚拟管理节点（合成 id ``mgr-<uuid>``，**不进控制面节点台账**）。

    ``spec_name`` 决定它的 spec（入参/子节点入驻 tab/详情页 tab）；``source`` 非空
    表示自定义载体（贴码），此时 spec_name 固定 "custom"。

    前置：spec 能加载；``requires_client=True`` 的 spec（manageable）拒绝——那种走
    真实节点 onboard，不在这里建虚拟行。
    """
    import uuid

    from .models_carrier import NodeCarrierBinding

    if keepalive_policy not in (KEEPALIVE_OFF, KEEPALIVE_ON_DEMAND, KEEPALIVE_TIMED):
        raise CarrierError("keepalive_policy 取值非法")

    if (source or "").strip():
        # 自定义贴码：先加载校验，carrier_name 固定 custom
        try:
            load_carrier_class("custom:preview", source)
        except CarrierSpecError as exc:
            raise CarrierError(f"载体代码加载失败: {exc}") from exc
        manifest = {}
        carrier_name = "custom"
    else:
        spec = await load_spec(spec_name)
        manifest = dict(getattr(spec, "MANIFEST", {}) or {})
        carrier_name = spec_name
        if manifest.get("requires_client"):
            raise CarrierError(
                f"载体 {spec_name} 需要真实客户端（requires_client），不能建成虚拟管理节点")

    mgr_id = f"mgr-{uuid.uuid4().hex}"
    await NodeCarrierBinding.create(
        node_id=mgr_id,
        carrier_name=carrier_name,
        config=dict(config or {}),
        is_manager=True,
        name=(name or "").strip() or str(manifest.get("label") or carrier_name),
        source=(source or "").strip(),
        manager_node_id="",
        keepalive_policy=keepalive_policy,
        keepalive_interval_minutes=keepalive_interval_minutes,
    )
    return {"manager_id": mgr_id, "carrier_name": carrier_name, "ok": True}


async def get_node_manager(manager_id: str) -> dict:
    from .models_carrier import NodeCarrierBinding

    m = await NodeCarrierBinding.get_or_none(node_id=manager_id, is_manager=True)
    if m is None:
        raise CarrierError(f"管理节点 {manager_id} 不存在")
    return {
        "manager_id": m.node_id, "name": m.name, "carrier_name": m.carrier_name,
        "config": dict(m.config or {}), "source": m.source or "",
        "keepalive_policy": m.keepalive_policy,
        "keepalive_interval_minutes": m.keepalive_interval_minutes,
        "created_at": m.created_at.isoformat() if m.created_at else "",
    }


async def update_node_manager(
    manager_id: str,
    *,
    name: str | None = None,
    config: dict | None = None,
    source: str | None = None,
    keepalive_policy: str | None = None,
    keepalive_interval_minutes: int | None = None,
) -> dict:
    from .models_carrier import NodeCarrierBinding

    m = await NodeCarrierBinding.get_or_none(node_id=manager_id, is_manager=True)
    if m is None:
        raise CarrierError(f"管理节点 {manager_id} 不存在")
    if source is not None:
        if (source or "").strip():
            try:
                load_carrier_class(f"custom:{manager_id}", source)
            except CarrierSpecError as exc:
                raise CarrierError(f"载体代码加载失败: {exc}") from exc
            m.carrier_name = "custom"
        m.source = (source or "").strip()
    if name is not None:
        m.name = (name or "").strip() or m.name
    if config is not None:
        m.config = dict(config)
    if keepalive_policy is not None:
        if keepalive_policy not in (KEEPALIVE_OFF, KEEPALIVE_ON_DEMAND, KEEPALIVE_TIMED):
            raise CarrierError("keepalive_policy 取值非法")
        m.keepalive_policy = keepalive_policy
    if keepalive_interval_minutes is not None:
        m.keepalive_interval_minutes = max(1, min(int(keepalive_interval_minutes), 1440))
    await m.save()
    return {"manager_id": manager_id, "ok": True}


async def delete_node_manager(manager_id: str) -> dict:
    """删虚拟管理节点。有子节点时拒绝（报子节点数），不级联。"""
    from .models_carrier import NodeCarrierBinding

    m = await NodeCarrierBinding.get_or_none(node_id=manager_id, is_manager=True)
    if m is None:
        raise CarrierError(f"管理节点 {manager_id} 不存在")
    children = await NodeCarrierBinding.filter(is_manager=False, manager_node_id=manager_id)
    if children:
        raise CarrierError(
            f"管理节点 {manager_id} 名下有 {len(children)} 个子节点，先删子节点再删管理节点")
    await m.delete()
    return {"manager_id": manager_id, "ok": True, "deleted": True}


async def list_manager_children(manager_id: str) -> list[dict]:
    """子节点列表 + 监控摘要：在线态（控制面）+ 各子节点的保活记录。"""
    from .models_carrier import NodeCarrierBinding
    from .node_client.client import get_local_node_client

    m = await NodeCarrierBinding.get_or_none(node_id=manager_id, is_manager=True)
    if m is None:
        raise CarrierError(f"管理节点 {manager_id} 不存在")
    children = await NodeCarrierBinding.filter(manager_node_id=manager_id, is_manager=False)
    try:
        live = await get_local_node_client().list_nodes()
    except Exception:  # noqa: BLE001 - 控制面不可用在线态全 False
        live = []
    by_id = {str(n.get("node_id")): n for n in (live or [])}
    out: list[dict] = []
    for c in children:
        live_row = by_id.get(c.node_id)
        out.append({
            "node_id": c.node_id,
            "carrier_name": c.carrier_name,
            "online": bool((live_row or {}).get("online") or (live_row or {}).get("connected")),
            "node_name": (live_row or {}).get("node_name") or "",
            "status": (live_row or {}).get("status") or "",
            "deploy_status": c.deploy_status,
            "last_keepalive_at": c.last_keepalive_at.isoformat() if c.last_keepalive_at else "",
            "last_keepalive_ok": c.last_keepalive_ok,
            "last_keepalive_note": c.last_keepalive_note or "",
        })
    return out


async def add_manager_child(
    manager_id: str,
    *,
    node_name: str = "",
    server_address: str = "",
    config: dict | None = None,
) -> dict:
    """在虚拟管理节点名下加一个执行节点：onboard → 写 bootstrap → 用管理节点代码
    部署（后台等拨回）。立即返回，进度看 ``GET /node-managers/{id}/children``。"""
    from .models_carrier import NodeCarrierBinding
    from .node_client.client import get_local_node_client
    from .nodes_service import save_node_bootstrap

    m = await NodeCarrierBinding.get_or_none(node_id=manager_id, is_manager=True)
    if m is None:
        raise CarrierError(f"管理节点 {manager_id} 不存在")

    client = get_local_node_client()
    onboarded = await client.onboard_node(
        role="execution",
        startup_method="standalone",
        node_name=(node_name or "").strip() or None,
        manager_node_id=manager_id,
    )
    child_id = str(onboarded.get("node_id") or "")
    secret = str(onboarded.get("secret") or "")
    if not child_id or not secret:
        raise CarrierError(f"入驻未返回 node_id/secret：{list(onboarded)}")

    server_url = (server_address or "").strip() or str((m.config or {}).get("server_address") or "")
    if server_url:
        try:
            await save_node_bootstrap(
                child_id, secret=secret, server_url=server_url,
                role="execution", startup_method="standalone")
        except Exception:  # noqa: BLE001 - Redis 不可用走台账兜底
            logger.debug("[carrier] child bootstrap record skipped (redis unavailable)")

    async def _run():
        try:
            result = await deploy_child_node(
                child_id, manager_id, config=config, server_url=server_url, role="execution")
            if not result.get("ok"):
                logger.warning("[carrier] child {} deploy failed at {}: {}",
                               child_id, result.get("stage"), result.get("error"))
        except Exception:  # noqa: BLE001
            logger.exception("[carrier] child {} deploy crashed", child_id)

    asyncio.create_task(_run(), name=f"carrier-child-{child_id}")
    return {"node_id": child_id, "manager_id": manager_id, "started": True}


async def delete_manager_child(manager_id: str, child_id: str) -> dict:
    """删子节点：撤销控制面节点 + 清载体绑定 + 清创空间账本（若有）。"""
    from .models_carrier import NodeCarrierBinding
    from .node_client.client import get_local_node_client

    child = await NodeCarrierBinding.get_or_none(node_id=child_id, manager_node_id=manager_id)
    if child is not None:
        await child.delete()
    # 清创空间账本（若这个子节点是创空间部署）
    try:
        from .models_studio import StudioDeployment

        sd = await StudioDeployment.get_or_none(node_id=child_id)
        if sd is not None:
            await sd.delete()
    except Exception:  # noqa: BLE001
        pass
    # 控制面撤销（节点行可能 pending 或已审批）
    try:
        await get_local_node_client().delete_node(child_id)
    except Exception:  # noqa: BLE001 - 控制面不可用/已删，都算成功
        logger.debug("[carrier] delete child {} from control plane failed: ignoring", child_id)
    return {"node_id": child_id, "manager_id": manager_id, "ok": True, "deleted": True}


# ── options_source 注册表：动态下拉选项 → 统一 {label, value} ───────────────────

def _iter_manifest_fields(manifest: dict):
    """遍历 MANIFEST 里所有字段声明所在的位置。

    ``options_source`` 不只在顶层 ``fields``——管理节点入参（``manager_fields``）与
    子节点入驻字段（``child_tabs[].fields``）都可能带动态选项（如创空间的规格/镜像
    在 child_tabs 里）。只查顶层会让这些字段的下拉必然报「没有字段 xxx」。
    """
    yield from (manifest.get("fields") or [])
    yield from (manifest.get("manager_fields") or [])
    for tab in (manifest.get("child_tabs") or []):
        if isinstance(tab, dict):
            yield from (tab.get("fields") or [])


async def resolve_spec_options(
    spec_name: str, key: str, *, config: dict | None = None,
    options_param: dict | None = None,
) -> dict:
    """按 options_source 解析某字段的可选项，返回 ``{options, error}``。

    前端只关心 ``[{label, value}]``；``error`` 非空说明这组选项拉取失败（前端
    显示为禁用 + 提示，不炸整个表单——对齐 studio 的 list_studio_options 容错）。
    """
    spec = await load_spec(spec_name)
    manifest = dict(getattr(spec, "MANIFEST", {}) or {})
    for f in _iter_manifest_fields(manifest):
        if not isinstance(f, dict) or str(f.get("key") or "") != key:
            continue
        src = str(f.get("options_source") or "").strip()
        if not src:
            return {"options": (f.get("options") or []), "error": ""}
        handler = _OPTIONS_SOURCES.get(src)
        if handler is None:
            return {"options": [], "error": f"未注册的选项源 {src!r}"}
        try:
            return await handler(dict(config or {}), dict(options_param or f.get("options_param") or {}))
        except Exception as exc:  # noqa: BLE001
            return {"options": [], "error": f"选项源 {src} 解析失败: {exc}"}
    return {"options": [], "error": f"spec {spec_name} 没有字段 {key!r}"}


async def _studio_hardware_options(config: dict, params: dict) -> dict:
    from . import studio_service

    data = await studio_service.list_studio_options(
        token=str(config.get("access_token") or ""),
        sdk_type=str(params.get("sdk_type") or "docker"))
    return {
        "options": [{"label": h.get("name") or "", "value": h.get("name") or ""}
                    for h in (data.get("hardware") or [])],
        "error": "；".join(data.get("errors") or []),
    }


async def _studio_base_image_options(config: dict, params: dict) -> dict:
    from . import studio_service

    data = await studio_service.list_studio_options(
        token=str(config.get("access_token") or ""))
    return {
        "options": [{"label": b.get("name") or "", "value": b.get("name") or ""}
                    for b in (data.get("base_images") or [])],
        "error": "；".join(data.get("errors") or []),
    }


async def _studio_sdk_version_options(config: dict, params: dict) -> dict:
    from . import studio_service

    data = await studio_service.list_studio_options(
        token=str(config.get("access_token") or ""))
    return {
        "options": [{"label": v.get("version") or "", "value": v.get("version") or ""}
                    for v in (data.get("sdk_versions") or [])],
        "error": "；".join(data.get("errors") or []),
    }


async def _studio_sdk_mode_options(config: dict, params: dict) -> dict:
    from . import studio_service

    data = await studio_service.list_studio_options(
        token=str(config.get("access_token") or ""))
    return {
        "options": [{"label": str(m.get("label") or m.get("sdk_type") or ""),
                     "value": str(m.get("sdk_type") or "")}
                    for m in (data.get("sdk_modes") or [])],
        "error": "；".join(data.get("errors") or []),
    }


_OPTIONS_SOURCES = {
    "studio.hardware": _studio_hardware_options,
    "studio.base_images": _studio_base_image_options,
    "studio.sdk_versions": _studio_sdk_version_options,
    "studio.sdk_modes": _studio_sdk_mode_options,
}


async def deploy_child_node(
    node_id: str,
    manager_node_id: str,
    *,
    config: dict | None = None,
    server_url: str = "",
    role: str = "execution",
) -> dict:
    """在管理节点下部署一个子执行节点。

    代码与参数（保活策略/间隔）都从管理节点继承；子节点自己的 config 只做覆盖。
    管理节点可以是：① 贴了代码的自定义管理节点（source 非空）；② 绑了内置载体的
    管理节点（如创空间，carrier_name=modelscope_studio）。两者的 spec 都由
    ``resolve_spec`` 顺回指解析。
    """
    from .models_carrier import NodeCarrierBinding

    mgr = await NodeCarrierBinding.get_or_none(node_id=manager_node_id)
    if mgr is None:
        raise CarrierError(f"管理节点 {manager_node_id} 不存在")
    has_source = bool(str(getattr(mgr, "source", "") or "").strip())
    has_builtin = bool(str(getattr(mgr, "carrier_name", "") or "").strip()) \
        and str(mgr.carrier_name) != "custom"
    if not has_source and not has_builtin:
        raise CarrierError(f"管理节点 {manager_node_id} 没有载体（既无代码也无内置载体名）")
    return await deploy_carrier_node(
        node_id, str(mgr.carrier_name or "custom"),
        config=config, server_url=server_url, role=role,
        keepalive_policy=mgr.keepalive_policy,
        keepalive_interval_minutes=int(mgr.keepalive_interval_minutes or 8),
        manager_node_id=manager_node_id,
        config_from_manager=True,
    )
