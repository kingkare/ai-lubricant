"""创空间节点的服务层：部署、重新部署、保活状态、清理。

与 ``nodes_service`` 的关系
---------------------------
``nodes_service`` 管节点的控制面生命周期（onboard/approve/revoke），本模块管
**这台节点跑在哪、怎么把它重新放上去**。两者共享 ``StudioDeployment`` 表里的
``node_id``，不共享任何控制面状态——删节点时本模块只清自己的账本行（控制面的
删除由 ``nodes_service.delete_node`` 负责）。

放在 ``user_platform`` 而不是 ``node_server``：本模块要跑 git、要读 release
catalog（都在数据服务侧），而 ``node_server`` 是独立进程且有「不 import 数据
服务代码」的自包含约束（见 ``node_hosted_reconcile`` 的模块注释）。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from . import studio_api as sa
from . import studio_deploy as sd
from .models_studio import StudioDeployment


class StudioDeployError(RuntimeError):
    """部署失败。message 面向运维，已脱敏。"""


async def _resolve_exec_urls(arch: str) -> tuple[str, str, str]:
    """按架构取执行节点二进制 + 通用 runtime 的下载地址。

    与 ``nodes_service.render_install_script`` 同源（release catalog 的最新登记
    版本），只是这里烘焙给创空间的 Dockerfile ARG 而不是 shell 脚本。返回
    ``(exec_url, runtime_url, runtime_sha)``；缺任何一个都抛错——创空间构建期
    拿不到产物会构建失败，早失败比让用户在创空间日志里找原因好。
    """
    import node_release_catalog as catalog

    latest = await catalog.get_latest_release()
    assets = [a for a in (latest.get("assets") or []) if isinstance(a, dict)]
    if not assets:
        raise StudioDeployError(
            "尚未登记任何节点发行版本。请先在「节点市场 → 版本管理」上传节点运行程序与 runtime。"
        )

    arch = (arch or "amd64").strip() or "amd64"
    exec_url = ""
    for asset in assets:
        if (
            str(asset.get("role") or "") == "execution"
            and str(asset.get("platform") or "") == "linux"
            and str(asset.get("arch") or "") == arch
        ):
            exec_url = str(asset.get("download_url") or "")
            break
    if not exec_url:
        raise StudioDeployError(
            f"发行版本里没有 linux/{arch} 的执行节点二进制。"
            f"请在上传时补上 node-execution-linux-{arch}。"
        )

    # runtime 是平台无关的通用包（role=runtime, platform/arch = any/any）。
    runtime_url = ""
    runtime_sha = ""
    for asset in assets:
        if str(asset.get("role") or "") != "runtime":
            continue
        runtime_url = str(asset.get("download_url") or "")
        digest = str(asset.get("digest") or "")
        runtime_sha = digest.split(":", 1)[1] if ":" in digest else digest
        if runtime_url:
            break
    if not runtime_url:
        raise StudioDeployError(
            "发行版本里没有 agent-compose runtime（node-runtime.tar.gz）。"
            "缺它节点注册后过不了审批门禁（runtime 平时靠审批后推送，形成死锁）。"
        )
    return exec_url, runtime_url, runtime_sha


async def _verify_urls_reachable(urls: dict[str, str]) -> None:
    """预检发行产物地址可匿名下载（只读首块，不下载整个文件）。

    为什么值得单独一步：这两个地址会被烘进创空间的 Dockerfile，由 **ModelScope
    的构建机**去下载——那是我们控制不到、也看不到日志的一台机器。若地址不可达
    （发行仓转私有、raw CDN 抽风、文件名不对），失败表现为创空间构建报错，用户
    拿着一个「curl: 404」的日志来找我们，而根因在我们这边。这里在推送前先自己
    探一次，把远端构建失败变成眼前一条可行动的报错。

    **不能整读**：runtime 是 189MB，``async with session.get(url)`` 会把整个
    响应体拉下来——每次部署白等几十秒到几分钟，正是「部署很慢」的来源之一。
    这里用 ``Range: bytes=0-0`` 只取 1 字节，并在拿到响应头后立刻关闭连接；
    服务端不支持 Range 时（回 200 而非 206）也照样在首块之后断开。

    为什么不直接用 HEAD：GitHub raw 对 HEAD 的支持不稳定（历史上返回 403/405），
    会把可达的地址误判成不可达。
    """
    import asyncio

    import aiohttp

    async def _probe(label: str, url: str) -> None:
        if not url:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=20, sock_read=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"Range": "bytes=0-0"}) as resp:
                    if resp.status >= 400:
                        raise StudioDeployError(
                            f"{label} 地址不可匿名下载（HTTP {resp.status}）：{url}\n"
                            "创空间的构建机会以匿名身份拉取它。请确认该发行仓是公开的，"
                            "或在节点市场重新上传一版。"
                        )
                    # 读一次就够——只为确认连接能建立、首块能拿到。
                    await resp.content.read(1)
                    # 显式关闭：不关的话 aiohttp 会继续把剩余响应体读完（189MB）。
                    resp.close()
        except StudioDeployError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StudioDeployError(
                f"{label} 地址无法访问：{url}\n（{type(exc).__name__}: {exc}）\n"
                "创空间的构建机需要能匿名拉取它。"
            ) from exc

    # 两个地址并行探：串行会让部署在预检上多等一个 RTT 轮次。
    await asyncio.gather(*(_probe(label, url) for label, url in urls.items()))


async def _resolve_server_url(
    node_id: str,
    existing: StudioDeployment | None,
    request_base: str = "",
) -> str:
    """该节点拨号的服务端地址。

    **不问第二遍**：地址在节点入驻时已由用户确认（``routes_nodes`` 的
    ``server_address``），这里按「本次账本 > 入驻 bootstrap 记录 > 全局配置/请求
    origin」的顺序沿用。

    为什么不接受前端再传一个：同一个节点只有一个正确的拨号地址，让用户在创空间
    面板重填既冗余、又可能与入驻时填的互相矛盾（两个值都"看起来对"，实际只有一个
    可达）。创空间面板只展示、不编辑。
    """
    from .config import settings

    # 1) 本账本已存的（重新部署的主路径）。
    if existing is not None and (existing.server_address or "").strip():
        return _normalize_server_url(existing.server_address)

    # 2) 入驻时的 bootstrap 记录（首次部署）。地址与凭据同源，一起取。
    from .nodes_service import get_node_bootstrap

    bootstrap = await get_node_bootstrap(node_id)
    if bootstrap and (bootstrap.get("server_url") or "").strip():
        return _normalize_server_url(str(bootstrap["server_url"]))

    # 3) 全局配置 / 请求 origin 兜底。
    configured = (settings.node_server_public_url or "").strip()
    if configured:
        return _normalize_server_url(configured)
    if (request_base or "").strip():
        return _normalize_server_url(request_base)

    raise StudioDeployError(
        "拿不到该节点拨号的服务端地址。请重新入驻该节点（入驻时可填地址），"
        "或配置 AGENT_COMPOSE_NODE_SERVER_PUBLIC_URL 作为默认值。"
    )


def _normalize_server_url(raw: str) -> str:
    """补协议、剥尾斜杠。用户可能填 ``192.168.1.5:8003`` 或裸域名。"""
    url = (raw or "").strip().rstrip("/")
    if url and not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    return url


def _address_warning(url: str) -> str:
    """地址看起来公网不可达时给一条提示（**不阻断**部署）。

    只提示不拦截：内网地址对局域网节点是合法的，我们无法在服务端判断这个节点
    究竟在哪。把判断交给用户，但把话说在前面——否则他会在创空间日志里找半天。
    """
    reason = _unroutable_reason(url)
    if not reason:
        return ""
    return (
        f"控制面地址 {url} 是{reason}。如果这个节点不在同一内网，它将无法连回；"
        "公网环境（如创空间）需要公网可达的地址。"
    )


def _unroutable_reason(url: str) -> str:
    """判断地址是否为公网不可达的段，返回原因描述（空串=可达）。

    用 ``ipaddress`` 而不是字符串前缀匹配：``10.1.2.3`` 与 ``100.1.2.3`` 用
    ``startswith("10.")`` 会误判，而 ``172.16-31`` 是区间、不是前缀。
    """
    import ipaddress
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url if "://" in url else f"http://{url}").hostname or "").strip()
    except ValueError:
        return ""
    if not host:
        return ""
    lowered = host.lower()
    if lowered in ("localhost",):
        return "回环地址"
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        # 域名：是否可达由 DNS 决定，这里不判断（``foo.local`` 之类的特例留给运维）。
        return ""
    if ip.is_loopback:
        return "回环地址"
    if ip.is_private:
        return "内网地址"
    if ip.is_link_local:
        return "链路本地地址"
    if ip.is_unspecified:
        return "未指定地址"
    return ""


# ── 可选规格目录（给前端「选择运行模式」用）────────────────────────────────
#
# 创空间能建成什么样，取决于平台当前开放的 SDK 类型 / 硬件档位 / 基础镜像。这三组
# 都从 OpenAPI 实时拉——**不缓存**：平台会增删档位（尤其付费 GPU 档位随库存上下），
# 缓存只会让用户选到一个已经没了的东西，然后部署时失败。
#
# 这三个接口匿名可读（见 studio_api._call 的 anonymous_ok），所以用户在还没填令牌
# 时就能看到有哪些选择——这是「先选规格再填令牌」这条交互的前提。

# 本平台节点只支持 Docker 形态：我们推的是自带 Dockerfile 的仓库，容器里要跑
# node-execution。Gradio / Streamlit / 静态页是「平台托管应用」的形态，跑不了我们
# 的节点——所以那几种在界面上标为不可用（而不是藏起来：让用户看见「有这些模式，
# 但当前节点形态用不了」比让他自己猜为什么没有要好）。
NODE_SDK_TYPE = "docker"

SDK_MODES: list[dict[str, Any]] = [
    {
        "sdk_type": "docker",
        "label": "Docker（节点）",
        "description": "用仓库里的 Dockerfile 构建镜像并启动节点进程。执行节点必须用这种模式。",
        "available": True,
    },
    {
        "sdk_type": "gradio",
        "label": "Gradio 应用",
        "description": "平台托管 Gradio 应用，端口与启动命令由平台接管，跑不了自定义进程。",
        "available": False,
    },
    {
        "sdk_type": "streamlit",
        "label": "Streamlit 应用",
        "description": "平台托管 Streamlit 应用，端口与启动命令由平台接管，跑不了自定义进程。",
        "available": False,
    },
    {
        "sdk_type": "static",
        "label": "静态页面",
        "description": "纯静态站点，只伺服文件，没有后端进程。",
        "available": False,
    },
]


async def list_studio_options(*, token: str = "", sdk_type: str = NODE_SDK_TYPE) -> dict[str, Any]:
    """列出创建/重建创空间时可选的模式、硬件档位、基础镜像。

    任何一项拉取失败都**不整体失败**：返回该项空列表 + 该条 ``errors`` 说明。理由：
    这三组是「给用户挑的选项」，少一组顶多是少一个选择，不该让整个面板打不开；
    而且用户在没填令牌时也能看到（接口匿名可读），此时若某项要令牌就会失败。
    """
    from . import studio_api as sa

    hardware: list[dict[str, Any]] = []
    base_images: list[dict[str, Any]] = []
    sdk_versions: list[dict[str, Any]] = []
    errors: list[str] = []

    # 三组并行拉——串行会让面板多等两个 RTT 轮次（每个接口约 1s）。
    import asyncio

    results = await asyncio.gather(
        sa.list_hardware(token=token, sdk_type=sdk_type),
        sa.list_base_images(token=token),
        sa.list_sdk_versions(token=token, sdk_type="gradio"),
        return_exceptions=True,
    )
    labels = ["硬件档位", "基础镜像", "SDK 版本"]
    for label, result in zip(labels, results):
        if isinstance(result, BaseException):
            errors.append(f"{label}列表获取失败：{result}")
            continue
        if label == "硬件档位":
            hardware = [sa.describe_hardware(it) for it in result]
        elif label == "基础镜像":
            base_images = [sa.describe_base_image(it) for it in result]
        else:
            sdk_versions = result

    # 免费档排前面：绝大多数用户会用免费档，付费档（真花钱）放后面且显式标价。
    hardware.sort(key=lambda h: (not h.get("free"), h.get("name") or ""))

    return {
        "sdk_modes": SDK_MODES,
        "sdk_type": sdk_type,
        "hardware": hardware,
        "base_images": base_images,
        "sdk_versions": sdk_versions,
        "errors": errors,
    }


def _resolve_spec(
    *,
    sdk_type: str = "",
    hardware: str = "",
    base_image: str = "",
    sdk_version: str = "",
    existing: StudioDeployment | None = None,
) -> dict[str, str]:
    """把「本次请求的规格」与「账本里已存的规格」合并成一套最终值。

    优先级：本次显式传入 > 账本已存 > 默认。

    为什么要合并而不是各取各的：重部署时用户往往只改一项（比如只换硬件档位），
    其余三项留空。若把空值当作「清空」，用户换个硬件就会把之前选的镜像一起抹掉。
    """
    def _pick(incoming: str, stored: str, fallback: str = "") -> str:
        value = (incoming or "").strip()
        if value:
            return value
        value = (stored or "").strip()
        return value or fallback

    return {
        "sdk_type": _pick(sdk_type, existing.sdk_type if existing else "", NODE_SDK_TYPE),
        # Docker 类型不支持 base_image（平台会忽略/报错），所以这一项固定留空——
        # 只在非 Docker 类型时才带上用户选的值。
        "base_image": "" if _pick(sdk_type, existing.sdk_type if existing else "", NODE_SDK_TYPE) == "docker"
                      else _pick(base_image, existing.base_image if existing else ""),
        "hardware": _pick(hardware, existing.hardware if existing else ""),
        "sdk_version": _pick(sdk_version, existing.sdk_version if existing else ""),
    }


def _spec_changed(wanted: dict[str, str], actual: dict[str, Any]) -> bool:
    """平台侧当前规格是否与期望不一致（不一致才需要 PATCH settings）。

    比对的是 ``GET /studios/{owner}/{repo}`` 返回的顶层 ``hardware`` /
    ``base_image`` / ``sdk_type``（spec 里那是「期望值」，即数据库里存的），而不是
    ``runtime.active_config``（那是**当前容器**实际跑的）。两者可能不同——改了设置
    但没重新部署时就是这样，而重新部署正是我们接下来要做的事。

    为什么值得比：每次重部署都发一次 PATCH 是多余的，而 PATCH 在平台侧可能触发
    校验（实名、付费账号、库存），对一个「什么都没改」的部署造成莫名其妙的失败。
    """
    if not actual:
        # 详情拿不到（首次创建时 ensure_studio 已带上规格，不必再 PATCH）。
        return False
    for key in ("sdk_type", "hardware", "base_image"):
        want = (wanted.get(key) or "").strip()
        if not want:
            continue
        have = str(actual.get(key) or "").strip()
        if want != have:
            return True
    return False


def _derive_studio_name(node_id: str, wanted: str = "") -> str:
    """把用户给的（或从节点 id 推的）创空间名规范化成 ModelScope 能接受的形状。

    ModelScope ``repo_name`` 约束：小写字母 / 数字 / ``-`` / ``_``，maxLength 64。
    用户填 ``My Bot`` 这种带空格大写的名字会被平台拒，而拒回的文案未必说清是名字
    格式问题——所以在这里先归一。

    全空（比如用户只填了标点）时抛错而不是兜一个随机名——静默改名会让用户下次
    找不到自己的创空间。
    """
    import re

    source = (wanted or "").strip()
    if not source:
        # 没给名字就用节点 id 生成：node-a9f30f75-… → ailub-node-a9f30f75
        short = node_id.replace("node-", "").split("-")[0][:12] or node_id[:12]
        source = f"ailub-node-{short}"
    name = re.sub(r"[^a-z0-9_-]+", "-", source.lower()).strip("-_")[:60].strip("-_")
    if not name:
        raise StudioDeployError(
            "创空间名规范化后为空（只剩特殊字符）。请填一个含字母或数字的名字。"
        )
    return name


# 部署步骤的固定顺序与文案。前端按这个顺序铺进度条；服务端每完成一步把状态
# 写进账本（``StudioDeployment.progress``），前端轮询即可看到实时进度。
#
# 为什么要有这个：整条链路要跑几十秒到几分钟，用户点完只能干等，既不知道在
# 做哪一步、也不知道卡在哪。步骤名同时充当失败定位（哪一步红的就是哪一步挂了）。
DEPLOY_STEPS: list[tuple[str, str]] = [
    ("resolve", "解析账号与地址"),
    ("ensure_studio", "创建 / 检查创空间"),
    ("configure", "应用运行模式与硬件规格"),
    ("render", "渲染构建文件"),
    ("push", "推送代码到创空间仓库"),
    ("variables", "写入节点凭据（环境变量）"),
    ("deploy", "触发上线"),
    # ── 以下三步**不由本函数同步完成** ─────────────────────────────────────
    # 它们是**平台侧**的进度（构建镜像 / 拉起容器），只能靠轮询 runtime.status
    # 回填，见 :func:`refresh_deploy_progress`。
    #
    # 为什么也放进这个列表：只到「触发上线」为止的话，用户点完之后界面就停在
    # 「全部完成」，而实际上平台还要构建十几分钟——那段最需要知道「在干嘛、卡没卡」
    # 的时间反而是空白的。放进同一个列表，前端铺进度条时天然就有这几行，
    # 状态由轮询逐格点亮。
    ("build", "平台构建镜像"),
    ("start", "容器启动"),
    ("ready", "容器运行中"),
]

# 平台 runtime.status → 上面三步的状态。
#
# 为什么要做这个映射而不是直接把 status 显示出来：状态名是平台术语
# （Building/Deploying/Initialized…），而进度条是「步骤」语义——用户要看的是
# 「走到哪一步了」，不是一串英文枚举值。
_STATUS_TO_STEPS: dict[str, list[tuple[str, str, str]]] = {
    # 刚开始 / 正在构建。Initialized = 空间已建但还没上线过。
    "Initialized": [("build", "running", "")],
    "Building": [("build", "running", "")],
    "Duplicating": [("build", "running", "平台正在复制空间")],
    "BuildFailed": [("build", "failed", "")],
    # 构建完成，正在拉起容器。
    "Deploying": [("build", "done", ""), ("start", "running", "")],
    "DeployFailed": [("build", "done", ""), ("start", "failed", "")],
    # 跑起来了。
    "Running": [("build", "done", ""), ("start", "done", ""), ("ready", "done", "")],
    # 能休眠/停止说明容器**起来过**——休眠是免费档的正常形态，不是失败。
    "Sleeping": [("build", "done", ""), ("start", "done", ""), ("ready", "done", "容器已休眠（闲置自动休眠）")],
    "Stopping": [("build", "done", ""), ("start", "done", ""), ("ready", "done", "正在停止")],
    "Stopped": [("build", "done", ""), ("start", "done", ""), ("ready", "done", "容器已停止")],
}


async def _set_progress_many(
    node_id: str, entries: list[tuple[str, str, str]]
) -> list[dict[str, Any]]:
    """批量写多步进度，返回写后的完整列表。

    **值没变就不写库**：这个函数会被面板轮询高频调用（每 1.5s 一次），而平台状态
    几十秒才变一次。无脑写会让 ``progress`` 与 ``updated_at`` 一直跳，既浪费
    DB 往返，也让「进度真的变了」这件事失去信号。

    与 :func:`_set_progress` 同口径：任何失败只记日志不抛——进度是观测信息。
    """
    import datetime as _dt

    try:
        row = await StudioDeployment.get_or_none(node_id=node_id)
        if row is None:
            return []
        items = list(row.progress or [])
        changed = False
        for step, state, detail in entries:
            detail = detail or ""
            prev = next(
                (it for it in items if isinstance(it, dict) and it.get("step") == step), None
            )
            if prev is not None and prev.get("state") == state and (prev.get("detail") or "") == detail:
                continue
            label = next((lb for key, lb in DEPLOY_STEPS if key == step), step)
            items = [it for it in items if not (isinstance(it, dict) and it.get("step") == step)]
            items.append({
                "step": step,
                "label": label,
                "state": state,
                "detail": detail[:400],
                "at": _dt.datetime.utcnow().isoformat(),
            })
            changed = True
        if changed:
            row.progress = items
            await row.save(update_fields=["progress", "updated_at"])
        return items
    except Exception:  # noqa: BLE001 — 进度写不进去不该影响调用方
        logger.warning("[studio] batch write progress failed for {}", node_id, exc_info=True)
        return []


def _platform_step_entries(status: str, error: str = "") -> list[tuple[str, str, str]]:
    """平台状态 → 三步进度的写入项。未知状态返回空（保持现状，不乱猜）。"""
    entries = list(_STATUS_TO_STEPS.get((status or "").strip(), []))
    if not entries:
        return []
    # 失败态把平台给的原因挂上去——这正是用户点开要看的。
    if entries[-1][1] == "failed" and error:
        entries[-1] = (entries[-1][0], "failed", error)
    return entries


# 面板每 1.5s 轮一次进度，但平台状态几十秒才变一次。服务端按节点节流，避免
# 把轮询放大成对 ModelScope 的洪水（那不但慢，还可能触发限流）。
_PROGRESS_PROBE_INTERVAL = 8.0
_progress_probed_at: dict[str, float] = {}


async def refresh_deploy_progress(
    node_id: str, *, force: bool = False
) -> list[dict[str, Any]]:
    """按平台运行时状态回填「构建 / 启动 / 运行」三步，返回最新进度列表。

    **由前端轮询驱动**，不起后台任务：面板打开时才需要这份进度，面板关了就没人看；
    而 keepalive 循环（默认 300s）也在跑同一套映射，所以关掉面板后进度照样会推进，
    重新打开时是最新的。

    ``force=True`` 绕过节流（部署刚返回时用一次，让用户立刻看到「构建中」）。
    """
    import time as _time

    now = _time.monotonic()
    if not force:
        last = _progress_probed_at.get(node_id, 0.0)
        if now - last < _PROGRESS_PROBE_INTERVAL:
            row = await StudioDeployment.get_or_none(node_id=node_id)
            return list(row.progress or []) if row is not None else []
    _progress_probed_at[node_id] = now

    row = await StudioDeployment.get_or_none(node_id=(node_id or "").strip())
    if row is None:
        return []
    token = sd.open_token(row.access_token_enc)
    if not token:
        return list(row.progress or [])
    probe = await sd.probe_studio_status(repo_url=row.repo_url, token=token)
    if not probe.get("ok"):
        # 探针失败：不清空已有进度，也不编造新状态——留着上一次的结论。
        return list(row.progress or [])
    entries = _platform_step_entries(probe.get("status") or "", probe.get("error") or "")
    if not entries:
        return list(row.progress or [])
    return await _set_progress_many(node_id, entries)



async def _set_progress(
    node_id: str, *, step: str, state: str, detail: str = ""
) -> None:
    """把某一步的状态写进账本，供前端轮询。

    状态：``running``（进行中）/ ``done`` / ``failed``。**任何写入失败都只记日志
    不抛**——进度是观测信息，不能因为它写不进去而让部署本身失败。
    """
    import datetime as _dt

    try:
        row = await StudioDeployment.get_or_none(node_id=node_id)
        if row is None:
            return
        label = next((lb for key, lb in DEPLOY_STEPS if key == step), step)
        items = list(row.progress or [])
        # 同一步重跑（比如重试）时替换旧记录，不留两份。
        items = [it for it in items if not (isinstance(it, dict) and it.get("step") == step)]
        items.append({
            "step": step,
            "label": label,
            "state": state,
            "detail": (detail or "")[:400],
            "at": _dt.datetime.utcnow().isoformat(),
        })
        row.progress = items
        await row.save(update_fields=["progress", "updated_at"])
    except Exception:  # noqa: BLE001 — 进度写不进去不该让部署失败
        logger.warning("[studio] write progress failed for {}", node_id, exc_info=True)


async def _reset_progress(node_id: str) -> None:
    """开新一轮部署前清空上一次的进度。"""
    try:
        row = await StudioDeployment.get_or_none(node_id=node_id)
        if row is not None and row.progress:
            row.progress = []
            await row.save(update_fields=["progress", "updated_at"])
    except Exception:  # noqa: BLE001
        logger.warning("[studio] reset progress failed for {}", node_id, exc_info=True)


async def deploy_node(
    *,
    node_id: str,
    repo_url: str = "",
    studio_name: str = "",
    access_token: str,
    public_url: str = "",
    arch: str = "amd64",
    request_base: str = "",
    node_name: str = "",
    node_secret: str = "",
    sdk_type: str = "",
    hardware: str = "",
    base_image: str = "",
    sdk_version: str = "",
) -> dict[str, Any]:
    """把一个 execution 节点部署到创空间。

    **首次部署只需要令牌**（+ 可选的创空间名）：服务端调 ``GET /users/me`` 拿 owner，
    推导仓库地址，建创空间，推码，写环境变量，上线。用户全程不接触仓库地址与
    创空间网页。

    重部署：``repo_url`` 和 ``studio_name`` 都留空时，从账本取已存的仓库地址。

    幂等：重复调用就是把同一套文件再推一次（内容没变时 git 层直接跳过），并重新上线。

    ``access_token`` 传空表示「沿用已保存的令牌」——重新部署时前端不回显令牌，
    留空是常态，不该因此报错。

    凭据来源优先级：``node_secret`` 显式传入（入驻弹窗刚生成、手上就有）> 本账本
    已存 > 入驻时的 Redis bootstrap 记录。显式传入这条最关键：入驻结果页是部署
    最自然的时机，那时 secret 就在前端手里，走它就不必依赖 Redis 的 30 分钟 TTL。

    **服务端地址不问第二遍**：它在节点入驻时就由用户填过（见 ``routes_nodes`` 的
    ``server_address``），存在 bootstrap 记录与节点台账里。这里直接沿用——同一个
    节点只有一个正确的拨号地址，让用户在创空间面板再填一次既冗余又容易前后矛盾。

    规格四项（``sdk_type`` / ``hardware`` / ``base_image`` / ``sdk_version``）留空
    即沿用账本里已存的，见 :func:`_resolve_spec`。本平台节点固定 Docker 形态，
    另外三项只在用户显式切换模式时才有意义。
    """
    from .nodes_service import get_node_bootstrap

    node_id = (node_id or "").strip()
    if not node_id:
        raise StudioDeployError("node_id 为空")

    # ── 架构前置校验：创空间容器是 x86_64，只接受 amd64 ─────────────────────
    #
    # 实测依据：平台的硬件档位只有 ``platform/*`` 一种形态，基础镜像全是
    # ``ubuntu22.04-*``（含 cuda/rocm 变体）——都是 x86_64，没有 aarch64 形态。
    #
    # 为什么必须在**部署之前**拦：把 arm64 二进制烘进 x86 镜像，构建期一切正常
    # （下载、npm 安装都过），容器启动才在 ``exec node-execution`` 那一步报
    # ``Exec format error``。而失败发生在 ModelScope 侧：本平台看到的是
    # 「部署中 → 部署失败」中间隔了半小时，真因要去翻创空间运行日志才看得到
    # （这条就是这么查出来的）。拦在这里，用户得到一条当场可行动的报错。
    arch = (arch or "amd64").strip() or "amd64"
    if arch != "amd64":
        raise StudioDeployError(
            f"创空间容器是 x86_64，不支持 {arch} 架构。"
            "把 ARM 二进制放进 x86 容器，启动时会报 exec format error"
            "（而且这个错在 ModelScope 侧，本地日志看不到）。"
            "请把「容器架构」改成 x86_64（amd64）后重新部署。"
        )

    # 开新一轮部署：清掉上一次的进度，否则前端会把旧步骤当成本次结果。
    await _reset_progress(node_id)

    existing = await StudioDeployment.get_or_none(node_id=node_id)
    secret = (node_secret or "").strip()
    if not secret:
        secret = sd.open_token(existing.node_secret_enc) if existing is not None else ""
    if not secret:
        # 入驻时控制面自己也存了一份凭据（``mc_ac_nodes.credential_secret_enc``，
        # 与本表**同一把主密钥、同一个库**——见 models_studio 的注释）。从这里兜底：
        # 账本行可能被用户删掉（点了「删除创空间」）、Redis bootstrap 30 分钟就过期，
        # 但只要节点还在本平台台账里，凭据就永远找得回来——否则「重新部署」在账本被
        # 清后就成了死路。
        secret = await _control_plane_secret(node_id)
    if not secret:
        bootstrap = await get_node_bootstrap(node_id)
        if not bootstrap:
            raise StudioDeployError(
                "找不到该节点的入驻凭据（首次部署需在入驻后 30 分钟内完成）。"
                "请在「节点」页重新入驻该节点后再试。"
            )
        secret = str(bootstrap.get("secret") or "")
        if not secret:
            raise StudioDeployError("节点入驻凭据不完整（缺 secret），请重新入驻。")

    # 令牌同理：留空 = 沿用已存的那份。首次部署（无账本）留空才是真错误。
    if not (access_token or "").strip():
        access_token = sd.open_token(existing.access_token_enc) if existing is not None else ""
        if not access_token:
            raise StudioDeployError("首次部署需要提供 ModelScope 访问令牌。")

    # 规格合并（本次显式传入 > 账本 > 默认）。放在这里是为了下面 ensure_studio 能用上。
    spec = _resolve_spec(
        sdk_type=sdk_type, hardware=hardware,
        base_image=base_image, sdk_version=sdk_version,
        existing=existing,
    )

    # ── 推导仓库地址 ─────────────────────────────────────────────────────────
    # 优先级：已有的账本值 > 显式 repo_url > 从令牌解出 owner + studio_name 推导。
    #
    # 首次部署用户不给仓库地址——他只填了令牌和（可选的）创空间名。令牌能解出
    # owner，仓库地址就是 ``https://www.modelscope.cn/studios/<owner>/<name>.git``。
    #
    # 重部署：账本里有 repo_url，直接沿用。
    repo_url = (repo_url or "").strip()
    studio_name = (studio_name or "").strip()

    if not repo_url and existing is not None and (existing.repo_url or "").strip():
        # 账本里的仓库地址是上次部署时推导/规范化过的，权威。
        repo_url = existing.repo_url.strip()
        # 沿用账本值时「解析」这一步同样发生了（读的是上次的解析结果），要标完成——
        # 否则前端会永远把它显示成「待执行」，看着像卡住了。
        await _set_progress(node_id, step="resolve", state="done", detail=repo_url)

    if not repo_url:
        # 没有仓库地址 → 必须从令牌推导 owner。
        # studio_name 为空时按节点 id 生成一个（ModelScope repo_name maxLength 64）。
        from . import studio_api as sa

        await _set_progress(node_id, step="resolve", state="running")
        try:
            owner = await sa.resolve_owner(token=access_token)
        except sa.StudioApiError as exc:
            await _set_progress(node_id, step="resolve", state="failed", detail=str(exc))
            raise StudioDeployError(str(exc)) from exc

        if not studio_name:
            # 没给名字时从节点 id 生成
            studio_name = _derive_studio_name(node_id)
        else:
            # 用户填了名字 → 规范化
            studio_name = _derive_studio_name(node_id, studio_name)

        repo_url = f"https://www.modelscope.cn/studios/{owner}/{studio_name}.git"
        await _set_progress(
            node_id, step="resolve", state="done",
            detail=f"{owner}/{studio_name}",
        )

    # 地址规范化：用户复制的是页面地址、官方文档给的是带凭据的 clone 地址、有人给
    # SSH 形式——三种都收敛成 clone 用的 HTTPS 地址，并剥掉可能嵌在里面的凭据。
    try:
        repo_url = sd.normalize_repo_url(repo_url)
        public_url = sd.normalize_public_url(public_url)
    except ValueError as exc:
        raise StudioDeployError(str(exc)) from exc

    server_url = await _resolve_server_url(node_id, existing, request_base)
    address_warning = _address_warning(server_url)
    exec_url, runtime_url, runtime_sha = await _resolve_exec_urls(arch)
    # 先自己确认这两个地址匿名可拉，再去推仓库——否则失败会在 ModelScope 的
    # 构建日志里出现，离根因很远。
    await _verify_urls_reachable({
        "执行节点二进制": exec_url,
        "agent-compose runtime": runtime_url,
    })

    # 解析 owner/repo_name（从 repo_url），用于 OpenAPI 调用
    try:
        owner, repo_name = sd.parse_studio_ref(repo_url)
    except ValueError as exc:
        raise StudioDeployError(str(exc)) from exc

    # ── ① 确保创空间存在（不存在就建）──────────────────────────────────────
    # 这是「真一键」的关键：用户只需要给一个仓库路径和令牌，不必先去网页建创空间。
    from . import studio_api as sa

    created_studio = False
    await _set_progress(node_id, step="ensure_studio", state="running")
    try:
        studio, created_studio = await sa.ensure_studio(
            owner, repo_name,
            token=access_token,
            display_name=node_name or repo_name,
            description=f"Ai Lubricant 执行节点 {node_id}",
            # 私有：节点凭据经过它，且没有公开的必要。仓库里已无密钥（凭据走
            # 环境变量），但创空间本身也没有对外展示的理由。
            visibility="private",
            # 规格在创建时一并带上（少一次 PATCH）。已存在时下面单独判断要不要改。
            sdk_type=spec["sdk_type"],
            sdk_version=spec["sdk_version"],
            base_image=spec["base_image"],
            hardware=spec["hardware"],
        )
    except sa.StudioApiError as exc:
        await _set_progress(node_id, step="ensure_studio", state="failed", detail=str(exc))
        raise StudioDeployError(str(exc)) from exc
    if created_studio:
        logger.info("[studio-api] created studio {}/{}", owner, repo_name)
    await _set_progress(
        node_id, step="ensure_studio", state="done",
        detail=("已创建" if created_studio else "已存在") + f" {owner}/{repo_name}",
    )

    # ── ② 应用运行模式与规格 ───────────────────────────────────────────────
    # 创空间已存在时，改规格要走 PATCH settings（创建时带的那次已经生效了，不必重复）。
    #
    # **只在真的有差异时才 PATCH**：平台的 settings 校验会查实名/付费账号/库存，
    # 对一次「什么都没改」的部署白发 PATCH 可能引入莫名其妙的失败。
    #
    # PATCH 之后**必须重新部署才生效**——平台只改了数据库里的期望值，正在跑的容器
    # 不会自动换规格。下面第 ⑥ 步的 deploy 正好承担这件事，所以这里不必额外触发。
    if not created_studio and _spec_changed(spec, studio or {}):
        await _set_progress(node_id, step="configure", state="running")
        try:
            await sa.update_settings(
                owner, repo_name, token=access_token,
                sdk_type=spec["sdk_type"],
                hardware=spec["hardware"],
                base_image=spec["base_image"],
                sdk_version=spec["sdk_version"],
            )
            detail = "、".join(
                f"{k}={v}" for k, v in (
                    ("模式", spec["sdk_type"]),
                    ("硬件", spec["hardware"]),
                    ("镜像", spec["base_image"]),
                ) if v
            )
            await _set_progress(node_id, step="configure", state="done", detail=detail)
            logger.info("[studio-api] settings updated for {}/{}: {}", owner, repo_name, detail)
        except sa.StudioApiError as exc:
            await _set_progress(node_id, step="configure", state="failed", detail=str(exc))
            raise StudioDeployError(str(exc)) from exc
    else:
        # 规格没变（或刚创建时已带上）→ 标完成，避免前端把它显示成「待执行」。
        detail = "、".join(
            f"{k}={v}" for k, v in (
                ("模式", spec["sdk_type"]),
                ("硬件", spec["hardware"]),
            ) if v
        )
        await _set_progress(
            node_id, step="configure", state="done",
            detail=("已创建时带规格" if created_studio else "规格未变") + (f"（{detail}）" if detail else ""),
        )

    # 公开地址优先用平台给的 host（详情接口返回，权威），用户没填时自动补上——
    # 保活探针需要它，让用户去创空间页面复制纯属多余。
    if not (public_url or "").strip():
        host = str((studio or {}).get("host") or "").strip()
        if host:
            public_url = sd.normalize_public_url(host)

    await _set_progress(node_id, step="render", state="running")
    files = sd.build_artifacts(
        node_id=node_id,
        server=server_url,
        exec_url=exec_url,
        runtime_url=runtime_url,
        runtime_sha=runtime_sha,
        base=request_base,
        **sd.default_build_mirrors(),
    )
    await _set_progress(
        node_id, step="render", state="done",
        detail="、".join(sorted(files.keys())),
    )

    # ── ② 推代码（不含任何密钥）────────────────────────────────────────────
    await _set_progress(node_id, step="push", state="running")
    commit_message = f"chore(node): 部署 Ai Lubricant 节点 {node_name or node_id}"
    try:
        await sd.push_files(repo_url=repo_url, token=access_token, files=files, commit_message=commit_message)
    except sd.StudioPushError as exc:
        # 失败也记账：推送失败最常见的原因是网络/令牌，用户改完重试时不该再被「凭据过期」卡一次。
        await _set_progress(node_id, step="push", state="failed", detail=str(exc))
        await _record_push(
            node_id, repo_url=repo_url, token=access_token, public_url=public_url,
            arch=arch, error=str(exc), node_secret=secret, server_address=server_url,
            **spec,
        )
        raise StudioDeployError(str(exc)) from exc
    await _set_progress(node_id, step="push", state="done", detail=repo_url)

    row = await _record_push(
        node_id, repo_url=repo_url, token=access_token, public_url=public_url,
        arch=arch, error="", node_secret=secret, server_address=server_url,
        **spec,
    )
    logger.info("[studio] pushed node {} → {}", node_id, repo_url)

    # ── ③ 写环境变量（节点凭据的落点）──────────────────────────────────────
    # 不再烘进仓库文件：这样仓库可以公开，轮换凭据也不必重新部署。
    # 失败不阻断——仓库已推，用户可手工补；但要明确告诉他不补就连不上。
    variables_ok = True
    variables_error = ""
    await _set_progress(node_id, step="variables", state="running")
    try:
        await sa.set_variables(
            owner, repo_name,
            {
                "AGENT_COMPOSE_SERVER": server_url,
                "AGENT_COMPOSE_NODE_ID": node_id,
                "AGENT_COMPOSE_NODE_SECRET": secret,
            },
            token=access_token,
        )
        logger.info("[studio-api] wrote environment variables to {}/{}", owner, repo_name)
        await _set_progress(
            node_id, step="variables", state="done",
            detail="AGENT_COMPOSE_SERVER / _NODE_ID / _NODE_SECRET",
        )
    except Exception as exc:  # noqa: BLE001
        variables_ok = False
        variables_error = str(exc)
        logger.warning("[studio-service] set_variables failed: {}", exc, exc_info=True)
        await _set_progress(node_id, step="variables", state="failed", detail=str(exc))

    # ── ④ 上线（触发构建 + 启动）───────────────────────────────────────────
    # 这就是网页上的「上线」按钮。spec 说它「停止的容器进行均可调用」，即可重复调。
    deployed = False
    deploy_error = ""
    await _set_progress(node_id, step="deploy", state="running")
    try:
        runtime = await sa.deploy(owner, repo_name, token=access_token)
        deployed = True
        logger.info("[studio-api] deploy triggered for {}/{}", owner, repo_name)
        await _set_progress(
            node_id, step="deploy", state="done",
            detail="已触发构建，平台正在拉起容器",
        )
        # deploy 的返回值就是运行时（见 studio_api.deploy）——顺手记下状态，节点
        # 列表立刻显示「构建中」而不是「未获取」。不然得等保活循环跑一轮（默认
        # 300s）列表才有东西看，用户会觉得状态没接上。
        await sd.record_status(row, {
            "ok": True,
            "status": str((runtime or {}).get("status") or ""),
            "error": str((runtime or {}).get("error_message") or ""),
        })
        # 同一份 runtime 现在就把「平台构建镜像 / 容器启动 / 容器运行中」三步铺出来
        # （见 _STATUS_TO_STEPS）。这一调让进度条在**本请求返回那一刻**就有了平台侧
        # 的第一步，用户不必先等面板轮询一轮（那样会先看到三行「待执行」的空白）。
        entries = _platform_step_entries(
            str((runtime or {}).get("status") or ""),
            str((runtime or {}).get("error_message") or ""),
        )
        if entries:
            await _set_progress_many(node_id, entries)
    except Exception as exc:  # noqa: BLE001 — 代码已推、变量已写，用户可手点上线
        deploy_error = str(exc)
        logger.warning("[studio-service] deploy trigger failed: {}", exc, exc_info=True)
        await _set_progress(node_id, step="deploy", state="failed", detail=str(exc))

    # ── 汇总下一步指引与警告 ────────────────────────────────────────────────
    warnings = [w for w in (address_warning,) if w]
    if not variables_ok:
        warnings.append(
            f"创空间环境变量写入失败（{variables_error}）。节点会启动，但缺少凭据将连不上"
            "控制面。请在创空间「设置 → 环境变量」手动补齐 "
            "AGENT_COMPOSE_SERVER / AGENT_COMPOSE_NODE_ID / AGENT_COMPOSE_NODE_SECRET。"
        )
    if not deployed:
        warnings.append(
            f"自动上线失败（{deploy_error}）。代码与环境变量都已就绪，"
            "请到创空间「设置」页手动点「上线」。"
        )

    if deployed:
        next_step = "已在创空间触发上线，平台正在构建镜像并启动容器。稍候可在节点列表看到该节点上线。"
    else:
        next_step = "请打开该创空间的「设置」页点击「上线」，平台会构建镜像并启动容器。"

    return {
        "node_id": node_id,
        "repo_url": repo_url,
        "public_url": public_url,
        "arch": arch,
        # 本次生效的规格（含从账本沿用的）。前端拿它更新表单预填。
        "spec": dict(spec),
        "files": sorted(files.keys()),
        "deployment": _to_dict(row),
        "studio_owner": owner,
        "studio_repo_name": repo_name,
        "deployed": deployed,
        # 控制面地址看起来公网不可达时的提示（不阻断）。用户按节点网络自行判断。
        "warning": "\n\n".join(warnings),
        "next_step": next_step,
    }


async def _control_plane_secret(node_id: str) -> str:
    """从控制面台账（``mc_ac_nodes``）解出节点凭据。

    为什么能跨表：入驻时控制面把凭据封存在自己的台账里，与本表**同一把主密钥、
    同一个库**（见 ``models_studio`` 顶部注释——这正是当初选这个口径的原因）。
    解不开/查不到一律返回空串，由调用方走下一级兜底。
    """
    from tortoise import Tortoise

    try:
        conn = Tortoise.get_connection("user_platform")
        rows = await conn.execute_query_dict(
            "SELECT credential_secret_enc FROM mc_ac_nodes WHERE id = $1", [node_id]
        )
    except Exception:  # noqa: BLE001 — 兜底路径失败不该掩盖真正要报的错
        logger.warning(
            "[studio] control-plane credential lookup failed for {}", node_id, exc_info=True,
        )
        return ""
    if not rows or not rows[0].get("credential_secret_enc"):
        return ""
    return sd.open_token(rows[0]["credential_secret_enc"])


async def _record_push(
    node_id: str,
    *,
    repo_url: str,
    token: str,
    public_url: str,
    arch: str,
    error: str,
    node_secret: str = "",
    server_address: str = "",
    sdk_type: str = "",
    hardware: str = "",
    base_image: str = "",
    sdk_version: str = "",
) -> StudioDeployment:
    """写部署账本。成功与失败都记，失败时保留上一份成功配置（便于重试）。

    ``node_secret`` 只在首次部署（凭据来自 Redis bootstrap）时非空，封存后落库
    供后续「重新部署」复用——Redis 那份 30 分钟就过期。

    ``server_address`` 总是覆盖（含清空）：前端把已存值预填进表单，空值即「用户
    改回默认」，保留旧值会让这个意图表达不出来。

    ``sdk_type`` / ``hardware`` / ``base_image`` / ``sdk_version`` 同样总是覆盖：
    它们经 :func:`_resolve_spec` 合并过（空值已填上账本里的旧值），所以传进来的
    就是「最终生效值」，直接落库即可。
    """
    import datetime as _dt

    row = await StudioDeployment.get_or_none(node_id=node_id)
    if row is None:
        row = StudioDeployment(node_id=node_id, repo_url=repo_url)
    row.repo_url = repo_url
    # 总是覆盖（含清空）：前端把已存值预填进表单，所以空值就是「用户清掉了」，
    # 保留旧值会让「不想要保活了」这个意图无法表达。
    row.public_url = public_url
    row.server_address = server_address
    if token:
        row.access_token_enc = sd.seal_token(token)
    if node_secret:
        row.node_secret_enc = sd.seal_token(node_secret)
    row.arch = (arch or "amd64").strip() or "amd64"
    # 规格四项：调用方已合并过（见 _resolve_spec），落库即最终值。
    row.sdk_type = (sdk_type or "").strip() or NODE_SDK_TYPE
    row.hardware = (hardware or "").strip()
    row.base_image = (base_image or "").strip()
    row.sdk_version = (sdk_version or "").strip()
    row.last_push_error = error
    if not error:
        row.last_push_at = _dt.datetime.utcnow()
    await row.save()
    return row


async def get_deployment(node_id: str) -> dict[str, Any] | None:
    """读部署账本（令牌掩码）。未部署过返回 None。"""
    row = await StudioDeployment.get_or_none(node_id=(node_id or "").strip())
    return _to_dict(row) if row is not None else None


async def list_deployments() -> list[dict[str, Any]]:
    rows = await StudioDeployment.all().order_by("-created_at")
    return [_to_dict(r) for r in rows]


async def delete_deployment(node_id: str) -> bool:
    """删除部署账本。**不动创空间仓库**——那是用户的资产，删文件由用户在创空间侧操作。"""
    deleted = await StudioDeployment.filter(node_id=(node_id or "").strip()).delete()
    if deleted:
        logger.info("[studio] removed deployment ledger for {}", node_id)
    return bool(deleted)


async def keepalive_now(node_id: str) -> dict[str, Any]:
    """立即查一次创空间状态，休眠/停止时顺手唤醒（管理端「唤醒」按钮）。"""
    row = await StudioDeployment.get_or_none(node_id=(node_id or "").strip())
    if row is None:
        raise StudioDeployError("该节点没有创空间部署记录")

    token = sd.open_token(row.access_token_enc)
    if not token:
        raise StudioDeployError("缺少 ModelScope 访问令牌，请重新部署一次")
    try:
        owner, repo_name = sd.parse_studio_ref(row.repo_url)
    except ValueError as exc:
        raise StudioDeployError(str(exc)) from exc

    probe = await sd.probe_studio_status(repo_url=row.repo_url, token=token)
    # 走 studio_deploy 统一的落库函数（与后台保活循环同一套口径）。
    await sd.record_status(row, probe)

    status = probe.get("status") or ""
    if not probe.get("ok"):
        return {
            "node_id": row.node_id,
            "status": "",
            "status_label": "",
            "ok": False,
            "message": f"查不到创空间状态：{probe.get('error')}",
        }
    if status in ("Sleeping", "Stopped"):
        # 顺手唤醒——这正是「唤醒」按钮的意义。
        try:
            from . import studio_api as sa

            await sa.deploy(owner, repo_name, token=token)
            return {
                "node_id": row.node_id, "status": status,
                "status_label": probe.get("label") or status, "ok": True,
                "message": f"创空间{probe.get('label')}，已触发上线唤醒",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "node_id": row.node_id, "status": status,
                "status_label": probe.get("label") or status, "ok": False,
                "message": f"创空间{probe.get('label')}，唤醒失败：{exc}",
            }
    return {
        "node_id": row.node_id, "status": status,
        "status_label": probe.get("label") or status, "ok": True,
        "message": f"创空间状态：{probe.get('label')}",
    }


async def _load_deployment_with_token(node_id: str) -> tuple[StudioDeployment, str, str, str]:
    """取账本 + 解出的令牌 + ``(owner, repo_name)``。

    三个需要 OpenAPI 的动作（stop/logs/status）共用，避免各写一遍取址与解密的
    样板，也避免某处漏了「令牌缺失」的提示。
    """
    row = await StudioDeployment.get_or_none(node_id=(node_id or "").strip())
    if row is None:
        raise StudioDeployError("该节点没有创空间部署记录")
    token = sd.open_token(row.access_token_enc)
    if not token:
        raise StudioDeployError("缺少 ModelScope 访问令牌，请重新部署一次")
    try:
        owner, repo_name = sd.parse_studio_ref(row.repo_url)
    except ValueError as exc:
        raise StudioDeployError(str(exc)) from exc
    return row, token, owner, repo_name


async def stop_studio(node_id: str, *, access_token: str = "") -> dict[str, Any]:
    """停止创空间容器（释放算力）。下次部署/唤醒时自动拉起。"""
    from . import studio_api as sa

    row, token, owner, repo_name = await _load_deployment_with_token(node_id)
    if (access_token or "").strip():
        token = access_token.strip()
    try:
        await sa.stop(owner, repo_name, token=token)
    except sa.StudioApiError as exc:
        raise StudioDeployError(str(exc)) from exc
    logger.info("[studio] stopped {}/{}", owner, repo_name)
    return {
        "node_id": row.node_id, "ok": True,
        "message": "已停止创空间（下次部署或唤醒时自动拉起）",
    }


# 节点凭据在创空间环境变量里的键名。与 deploy_node 写入的那组必须严格一致——
# teardown 清的就是这三个；清多了会把用户自己加的变量一起抹掉（仓库他要留着）。
_NODE_VARIABLE_KEYS = (
    "AGENT_COMPOSE_SERVER",
    "AGENT_COMPOSE_NODE_ID",
    "AGENT_COMPOSE_NODE_SECRET",
)


async def teardown_studio(node_id: str, *, access_token: str = "") -> dict[str, Any]:
    """下线并清理一个创空间节点：停容器 → 清凭据 → 删部署账本。

    **删不掉仓库本身**——这是平台限制，不是我们偷懒：ModelScope OpenAPI 没有
    删除创空间的端点。spec 里 studio 相关的 DELETE 只有两个（secrets / variables），
    对 ``DELETE /studios/{owner}/{repo_name}`` 实测返回 ``404 page not found``
    （作为对照，已存在的 ``DELETE .../variables`` 返回的是 401 认证错）——即该路由
    根本没注册。所以仓库只能用户自己在网页上删，本函数把能做的都做掉，并把剩下
    这一步如实告诉用户。

    为什么清环境变量是这里**最有价值**的一步：部署时把节点的 TOTP 凭据
    （``AGENT_COMPOSE_NODE_SECRET``）写进了创空间的 variables。如果只删账本不
    清变量，这份凭据就一直躺在用户的创空间里——仓库他可能转作他用、可能改公开、
    可能共享给协作者，而节点凭据是长期有效的身份凭据。下线时清掉它，才叫真的
    「删除」。

    各步**尽力而为**：停容器失败（比如已停止）不该挡住清凭据，清凭据失败更不该
    让账本留着（那样用户重试时账本还在，反而能再试一次）。每一步的结果都如实体
    返回，前端据此告诉用户哪些做成了、哪些要他手动补。
    """
    from . import studio_api as sa

    # 先取账本（令牌 / owner / repo 都从这来），最后一步才删它。
    row, token, owner, repo_name = await _load_deployment_with_token(node_id)
    if (access_token or "").strip():
        token = access_token.strip()

    stopped: bool | None = None
    stop_error = ""
    try:
        await sa.stop(owner, repo_name, token=token)
        stopped = True
        logger.info("[studio] teardown: stopped {}/{}", owner, repo_name)
    except sa.StudioApiError as exc:
        # 已停止/休眠的平台可能报错——那正是我们想要的结果，不算失败。
        stopped = None
        stop_error = str(exc)
        logger.info("[studio] teardown: stop returned error (tolerated): {}", exc)

    variables_cleared = False
    variables_error = ""
    try:
        await sa.delete_variables(owner, repo_name, list(_NODE_VARIABLE_KEYS), token=token)
        variables_cleared = True
        logger.info("[studio] teardown: cleared node credentials from {}/{}", owner, repo_name)
    except sa.StudioApiError as exc:
        variables_error = str(exc)
        logger.warning("[studio] teardown: clear variables failed: {}", exc)

    ledger_deleted = await delete_deployment(node_id)

    manual_steps: list[str] = []
    if stopped is None:
        manual_steps.append(f"停止容器未能确认（{stop_error}）。请到创空间「设置」页确认容器已停止。")
    if not variables_cleared:
        manual_steps.append(
            f"节点凭据未能从创空间环境变量中清除（{variables_error}）。"
            "请到创空间「设置 → 环境变量」手动删除 AGENT_COMPOSE_NODE_SECRET 等三项——"
            "那是节点的长期身份凭据，留在那里有泄露风险。"
        )
    # 平台限制，永远要提醒：仓库删不掉（OpenAPI 没这个端点）。
    manual_steps.append(
        "创空间仓库本身需要你在 ModelScope 网页上手动删除"
        "（官方接口不提供删除创空间的端点）：打开创空间「设置」页 → 删除空间。"
    )

    # 前面那几条（停容器 / 清凭据）任意一条没成功，就是「部分清理」。
    partly = (stopped is None) or (not variables_cleared)
    return {
        "node_id": node_id,
        "repo_url": row.repo_url,
        "stopped": stopped,
        "variables_cleared": variables_cleared,
        "ledger_deleted": ledger_deleted,
        # 剩余需要用户手动做的步骤（含「仓库要手动删」这条平台限制）。
        "manual_steps": manual_steps,
        "partly": partly,
        "message": (
            "已停止容器、清除节点凭据并删除部署记录。"
            if not partly else "已尽力清理，部分步骤需要你手动确认。"
        ),
    }


async def get_studio_logs(node_id: str, *, log_type: str = "run") -> dict[str, Any]:
    """拉创空间日志（``run`` 运行 / ``build`` 构建）。排障用。"""
    from . import studio_api as sa

    row, token, owner, repo_name = await _load_deployment_with_token(node_id)
    kind = (log_type or "run").strip() or "run"
    try:
        lines = await sa.get_logs(owner, repo_name, kind, token=token)
    except sa.StudioApiError as exc:
        raise StudioDeployError(str(exc)) from exc
    return {
        "node_id": row.node_id,
        "log_type": kind,
        "lines": lines,
        "count": len(lines),
    }


async def get_studio_runtime(node_id: str) -> dict[str, Any]:
    """查创空间运行时状态（Running/Sleeping/BuildFailed…）。

    **顺带落库**：这是面板「查看状态」按钮与打开面板时走的路径。不落库的话，
    用户在面板里明明看到了「休眠中」，节点列表却还显示上一次的结论——两处口径
    分裂比都旧一点更难解释。
    """
    row, token, owner, _repo = await _load_deployment_with_token(node_id)
    probe = await sd.probe_studio_status(repo_url=row.repo_url, token=token)
    # 落库：面板「查看状态」与后台保活循环同一套口径，节点列表才不会显示旧值。
    await sd.record_status(row, probe)
    return {
        "node_id": row.node_id,
        "ok": bool(probe.get("ok")),
        "status": probe.get("status") or "",
        "status_label": probe.get("label") or "",
        "error": probe.get("error") or "",
    }


def _mask_server_address(raw: str) -> str:
    """控制面地址脱敏：只留协议与端口，主机名掩掉。

    ``http://8.148.8.185:26330`` → ``http://***:26330``

    为什么留端口：重新部署时用户需要核对端口是否正确（填错端口是常见错误），
    而主机名（公网 IP / 域名）属于内部拓扑，不该出现在可被截图、共享的页面上。
    """
    import re

    text = (raw or "").strip()
    if not text:
        return ""
    match = re.match(r"^(https?://)([^/:]+)(:\d+)?", text)
    if not match:
        return "***"
    return f"{match.group(1)}***{match.group(3) or ''}"


def _to_dict(row: StudioDeployment) -> dict[str, Any]:
    """账本 → API 形状。令牌只回掩码，绝不回明文。"""
    token = sd.open_token(row.access_token_enc)
    return {
        "node_id": row.node_id,
        "repo_url": row.repo_url,
        # 创空间名：从 repo_url 里取最后一段。重新部署时预填回表单。
        "studio_name": (row.repo_url.split("/")[-1].removesuffix(".git") if row.repo_url else ""),
        "public_url": row.public_url,
        # 控制面地址**脱敏后**返回：只留协议与端口，主机名掩掉。
        #
        # 为什么还回：重新部署时要让用户核对「端口对不对」，完全不给反而难排障。
        # 为什么不回原文：地址含内网拓扑，管理端页面可能被截图/共享。
        "server_address_masked": _mask_server_address(row.server_address),
        "arch": row.arch,
        # 创空间规格（用户上次选了什么）。重新部署时预填回表单。
        "sdk_type": row.sdk_type,
        "hardware": row.hardware,
        "base_image": row.base_image,
        "sdk_version": row.sdk_version,
        "access_token_masked": sd.mask_token(token),
        "has_token": bool(token),
        # 最近一次部署的分步进度（前端轮询展示）。空 = 还没部署过或已清空。
        "progress": list(row.progress or []),
        "last_push_at": row.last_push_at.isoformat() if row.last_push_at else "",
        "last_push_error": row.last_push_error or "",
        "last_keepalive_at": row.last_keepalive_at.isoformat() if row.last_keepalive_at else "",
        "last_keepalive_ok": bool(row.last_keepalive_ok),
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }
