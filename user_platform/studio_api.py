"""ModelScope 创空间 OpenAPI 客户端。

权威依据
--------
官方 spec：``https://www.modelscope.cn/.well-known/openapi.json``
（OpenAPI 3.1.1，42 个端点）。创空间相关端点：

    GET    /studios                                  列表
    POST   /studios                                  创建
    GET    /studios/{owner}/{repo_name}              详情（含 runtime.status / host）
    POST   /studios/{owner}/{repo_name}/deploy       上线（拉最新代码重建，可重复调）
    POST   /studios/{owner}/{repo_name}/stop         停止
    GET    /studios/{owner}/{repo_name}/logs/{type}  日志（排障）
    GET/POST/PUT/DELETE /studios/{owner}/{repo_name}/variables   环境变量
    PATCH  /studios/{owner}/{repo_name}/settings     改设置

认证：``Authorization: Bearer <ModelScope API Token>``（spec 的 ``bearerAuth``）。
base：``https://modelscope.cn/openapi/v1``

**注意前缀**：是 ``/openapi/v1``，不是网页前端用的 ``/api/v1``。后者是站点内部
接口（只认浏览器会话 Cookie），对着它调令牌永远 401 —— 这条路踩过，记在这里。

为什么单独一个模块
------------------
``studio_deploy`` 管「怎么把节点装进容器」（渲染工件、git push），本模块只管
「怎么跟 ModelScope 说话」。两者独立：换托管平台只需换本模块，工件渲染不受影响；
且 OpenAPI 客户端可以单独测（见 script/test_modelscope_token.py）。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

# spec 里 servers[0].url。不带尾部斜杠，拼接时自己加。
OPENAPI_BASE = "https://modelscope.cn/openapi/v1"

# 单次请求超时。deploy/stop 是异步动作（返回当前状态，不等构建完成），
# 所以不需要长超时；build 进度靠轮询 GET 详情看 runtime.status。
_TIMEOUT = 30

# StudioRuntime.status 的全部取值（spec 的 enum）。
STUDIO_STATUSES = (
    "Initialized", "Building", "BuildFailed", "Deploying", "DeployFailed",
    "Running", "Stopping", "Stopped", "Duplicating", "Sleeping",
)

# 处于这些状态时容器不会接活：需要唤醒。
_SLEEPING_STATES = frozenset({"Sleeping", "Stopped"})
# 处于这些状态时正在跑一个动作：不要再叠加 deploy（避免重复构建）。
_IN_FLIGHT_STATES = frozenset({"Building", "Deploying", "Stopping", "Duplicating"})


class StudioApiError(RuntimeError):
    """调用 ModelScope OpenAPI 失败。message 面向用户，已脱敏（绝不含令牌）。"""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _redact(text: str, token: str) -> str:
    """抹掉可能出现在响应里的令牌（防御性：理论上不该出现）。"""
    out = (text or "").strip()
    if token:
        out = out.replace(token, "***")
    return out[:400]


async def _call(
    method: str,
    path: str,
    *,
    token: str = "",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    anonymous_ok: bool = False,
) -> dict[str, Any]:
    """调一次 OpenAPI。返回响应 JSON 的 ``data`` 部分（成功时）。

    错误一律抛 :class:`StudioApiError`，消息里带 ModelScope 的 code/message ——
    那些文案（``InvalidAuthentication``、``NotFound`` 等）比我们自己编的准确。

    ``anonymous_ok``：少数端点（硬件档位 / 基础镜像 / SDK 版本这三个目录）在 spec 里
    的 security 是 ``[{bearerAuth: []}, {}]``——**匿名也能读**。这些是「平台有哪些
    规格可选」的公开目录，用户还没填令牌时就该能看见（否则选规格这一步要么空白、
    要么逼人先填令牌），所以单独放行。
    """
    import aiohttp

    token = (token or "").strip()
    if not token and not anonymous_ok:
        raise StudioApiError("缺少 ModelScope 访问令牌")

    url = OPENAPI_BASE + path
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, url, headers=headers, params=params, json=body
            ) as resp:
                raw = await resp.text()
                status = resp.status
    except Exception as exc:  # noqa: BLE001 — 网络类错误统一转成 StudioApiError
        raise StudioApiError(
            f"调用 ModelScope 失败（{type(exc).__name__}: {exc}）。"
            "请检查服务器到 modelscope.cn 的网络。"
        ) from exc

    # ModelScope 统一信封：{success, data, request_id} 或 {success:false, code, message}
    try:
        import json as _json

        payload = _json.loads(raw)
    except (ValueError, TypeError):
        raise StudioApiError(
            f"ModelScope 返回了非 JSON 响应（HTTP {status}）：{_redact(raw, token)}"
        )

    if not isinstance(payload, dict):
        raise StudioApiError(f"ModelScope 返回了意外的响应形状（HTTP {status}）")

    if payload.get("success") is False or status >= 400:
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or "").strip()
        detail = _redact(message or raw, token)
        # 认证类错误单独给一句人话：这是最常见的失败，且用户多半不知道去哪查。
        if code == "InvalidAuthentication" or status == 401:
            raise StudioApiError(
                f"ModelScope 拒绝了这个访问令牌（{detail}）。"
                "请在「个人中心 → 访问令牌」确认令牌有效且未过期。",
                status=status, code=code,
            )
        if status == 404 or code in ("NotFound", "StudioNotFound"):
            raise StudioApiError(
                f"创空间不存在（{detail}）。请确认仓库地址里的用户名/创空间名正确。",
                status=status, code=code,
            )
        raise StudioApiError(
            f"ModelScope 返回错误（HTTP {status} {code}）：{detail}",
            status=status, code=code,
        )

    data = payload.get("data")
    return data if isinstance(data, dict) else {}


# ── 账号 ────────────────────────────────────────────────────────────────────

async def whoami(*, token: str) -> dict[str, Any]:
    """取当前令牌对应的账号信息。返回 ``{username, nickname, avatar_url, email}``。

    ``username`` 是**创空间的 owner**——所以用户只需要给一个令牌，仓库地址的
    ``<owner>`` 段能推出来，不必让他填整个仓库地址。
    """
    return await _call("GET", "/users/me", token=token)


async def resolve_owner(*, token: str) -> str:
    """解出该令牌对应的用户名（创空间 owner）。

    取不到就报错而不是猜——owner 错了会去建/推一个别人的命名空间下的空间，
    平台多半拒绝，但错误信息会很难懂。
    """
    me = await whoami(token=token)
    username = str(me.get("username") or "").strip()
    if not username:
        raise StudioApiError(
            "无法从访问令牌解析出用户名（创空间 owner）。请确认令牌有效。"
        )
    return username


# ── 创空间 CRUD ─────────────────────────────────────────────────────────────

async def get_studio(owner: str, repo_name: str, *, token: str) -> dict[str, Any]:
    """取创空间详情。返回 ``{id, repo_name, owner, host, runtime:{status,...}, ...}``。

    ``runtime.status`` 是保活判断的真相源（含 ``Sleeping``）；``host`` 是公开
    访问地址——不必再让用户填。
    """
    return await _call("GET", f"/studios/{owner}/{repo_name}", token=token)


async def create_studio(
    owner: str,
    repo_name: str,
    *,
    token: str,
    display_name: str = "",
    description: str = "",
    visibility: str = "private",
    sdk_type: str = "docker",
    sdk_version: str = "",
    base_image: str = "",
    hardware: str = "",
) -> dict[str, Any]:
    """创建一个创空间。返回 ``{id, repo_name, owner, url}``。

    默认 ``sdk_type=docker``（我们推的是 Dockerfile）、``visibility=private``
    （节点凭据会经过它，且没有公开的必要）。

    ``hardware`` / ``base_image`` / ``sdk_version``：仅在创建时一次性带上——
    spec 里 ``CreateStudioRequest`` 接受这三项。**注意**：``base_image`` 仅对
    非 Docker 类型生效（spec 原文「仅 Docker 类型不支持」），Docker 类型传了
    会被平台忽略——所以本平台节点（Docker）不传 base_image。

    平台侧可能要求账号已实名并绑定阿里云——那是 ModelScope 的校验，失败时它返回
    的 message 会说明原因，这里原样透出。
    """
    body: dict[str, Any] = {
        "owner": owner,
        "repo_name": repo_name,
        "sdk_type": sdk_type,
        "visibility": visibility,
    }
    if display_name:
        body["display_name"] = display_name
    if description:
        body["description"] = description
    # 硬件在创建时就指定，避免建好后再改一遍 settings（少一次 API 调用）。
    if hardware:
        body["hardware"] = hardware
    # base_image / sdk_version 对 Docker 类型无效，传了也是噪音——只在非 Docker
    # 类型时带，避免平台因「Docker 类型不支持 base_image」返回校验错误。
    if sdk_type != "docker":
        if base_image:
            body["base_image"] = base_image
        if sdk_version:
            body["sdk_version"] = sdk_version
    return await _call("POST", "/studios", token=token, body=body)


async def update_settings(
    owner: str,
    repo_name: str,
    *,
    token: str,
    sdk_type: str = "",
    sdk_version: str = "",
    base_image: str = "",
    hardware: str = "",
    display_name: str = "",
    description: str = "",
    visibility: str = "",
) -> dict[str, Any]:
    """PATCH 创空间设置。

    spec 的 ``UpdateStudioSettingsRequest`` 是「传哪个字段改哪个」——所以空值一律
    不带（否则会把字段清空）。

    **关键口径**：改设置后需重新部署才能生效——平台数据库里改了，但**正在运行的
    容器不会自动换规格**。所以调用方改完设置后必须紧接着调 :func:`deploy`，否则
    用户会看到「设置改了但规格没变」的假象。
    """
    body: dict[str, Any] = {}
    if sdk_type:
        body["sdk_type"] = sdk_type
    if sdk_version:
        body["sdk_version"] = sdk_version
    if base_image:
        body["base_image"] = base_image
    if hardware:
        body["hardware"] = hardware
    if display_name:
        body["display_name"] = display_name
    if description:
        body["description"] = description
    if visibility:
        body["visibility"] = visibility
    if not body:
        return {}
    return await _call("PATCH", f"/studios/{owner}/{repo_name}/settings", token=token, body=body)


async def studio_exists(owner: str, repo_name: str, *, token: str) -> bool:
    """创空间是否已存在。任何错误（含认证失败）都向上抛，不吞成 False——
    把「令牌无效」误判成「不存在」会导致去建一个重复的，或给出错误指引。"""
    try:
        await get_studio(owner, repo_name, token=token)
        return True
    except StudioApiError as exc:
        if exc.status == 404 or exc.code in ("NotFound", "StudioNotFound"):
            return False
        raise


async def ensure_studio(
    owner: str,
    repo_name: str,
    *,
    token: str,
    display_name: str = "",
    description: str = "",
    visibility: str = "private",
    sdk_type: str = "docker",
    sdk_version: str = "",
    base_image: str = "",
    hardware: str = "",
) -> tuple[dict[str, Any], bool]:
    """确保创空间存在，不存在就建。返回 ``(studio, created)``。

    幂等：已存在时直接返回详情，不重复创建。

    **已存在时不动设置**：硬件/镜像的变更走 :func:`update_settings`（由调用方在
    「已存在但规格不同」时显式调用）。这里不做「顺手对齐」——那会让每次重新部署都
    多发一次 PATCH，而绝大多数重部署的规格是没变的。
    """
    try:
        existing = await get_studio(owner, repo_name, token=token)
        return existing, False
    except StudioApiError as exc:
        if not (exc.status == 404 or exc.code in ("NotFound", "StudioNotFound")):
            raise
    created = await create_studio(
        owner, repo_name, token=token,
        display_name=display_name, description=description, visibility=visibility,
        sdk_type=sdk_type, sdk_version=sdk_version,
        base_image=base_image, hardware=hardware,
    )
    logger.info("[studio-api] created studio {}/{}", owner, repo_name)
    return created, True


# ── 生命周期 ────────────────────────────────────────────────────────────────

async def deploy(owner: str, repo_name: str, *, token: str) -> dict[str, Any]:
    """上线/重新上线：拉最新代码重建容器。

    spec 原文「拉取最新代码并重建，返回当前状态；停止的容器进行均可调用」——
    即**可重复调用**，也正是「唤醒」的手段（Sleeping/Stopped 时调它拉起）。

    返回值就是 :class:`StudioRuntime`（``{status, active_config, created_at}``），
    调用方可以拿它直接记账，不必再打一次详情接口。
    """
    return await _call("POST", f"/studios/{owner}/{repo_name}/deploy", token=token)


async def stop(owner: str, repo_name: str, *, token: str) -> dict[str, Any]:
    """停止创空间（释放算力）。"""
    return await _call("POST", f"/studios/{owner}/{repo_name}/stop", token=token)


async def get_runtime(owner: str, repo_name: str, *, token: str) -> dict[str, Any]:
    """取运行时状态：``{status, error_message, active_config, created_at}``。

    ``status`` 取值见 :data:`STUDIO_STATUSES`。
    """
    studio = await get_studio(owner, repo_name, token=token)
    runtime = studio.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


async def get_logs(
    owner: str,
    repo_name: str,
    log_type: str = "run",
    *,
    token: str,
    page_num: int = 1,
    page_size: int = 200,
    keyword: str = "",
) -> list[str]:
    """取创空间日志（构建/运行）。返回日志行列表（最新的在前，由平台决定）。

    这是排障的主力：节点连不上时，创空间侧的日志能直接看出是构建失败、还是容器里
    的进程报错。``log_type`` 常见取值 ``build`` / ``run``。
    """
    params: dict[str, Any] = {"page_num": page_num, "page_size": min(page_size, 500)}
    if keyword:
        params["keyword"] = keyword
    data = await _call(
        "GET", f"/studios/{owner}/{repo_name}/logs/{log_type}",
        token=token, params=params,
    )
    logs = data.get("logs")
    return [str(line) for line in logs] if isinstance(logs, list) else []


# ── 环境变量（节点凭据的落点）──────────────────────────────────────────────

async def list_variables(owner: str, repo_name: str, *, token: str) -> dict[str, str]:
    """列环境变量，返回 ``{key: value}``。"""
    data = await _call("GET", f"/studios/{owner}/{repo_name}/variables", token=token)
    items = data.get("variables")
    out: dict[str, str] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("key"):
                out[str(item["key"])] = str(item.get("value") or "")
    return out


async def set_variables(
    owner: str, repo_name: str, values: dict[str, str], *, token: str
) -> None:
    """写入/更新一组环境变量（幂等）。

    spec 里新增是 POST、更新是 PUT，**没有 upsert 语义**——同一个 key 重复 POST
    会失败。所以先拉一遍现有 key，决定每个走 POST 还是 PUT。

    为什么把节点凭据放这里而不是烘进仓库文件：仓库里不留任何密钥（可以公开），
    轮换凭据也不必重新部署。``start.sh`` 读同名环境变量，两边天然对齐。
    """
    if not values:
        return
    try:
        existing = await list_variables(owner, repo_name, token=token)
    except StudioApiError as exc:
        # 列表失败（权限/接口不可用）时不能盲试 POST——可能创建出重复 key。
        raise StudioApiError(
            f"读取创空间环境变量失败，无法安全写入：{exc}"
        ) from exc

    for key, value in values.items():
        body = {"key": key, "value": value}
        method = "PUT" if key in existing else "POST"
        await _call(
            method, f"/studios/{owner}/{repo_name}/variables", token=token, body=body,
        )
    logger.info(
        "[studio-api] wrote {} variable(s) to {}/{}", len(values), owner, repo_name,
    )


async def delete_variables(
    owner: str, repo_name: str, keys: list[str], *, token: str
) -> None:
    """删除环境变量。key 不存在时平台可能报错——逐个删并容忍 NotFound。"""
    for key in keys:
        try:
            await _call(
                "DELETE", f"/studios/{owner}/{repo_name}/variables",
                token=token, body={"key": key},
            )
        except StudioApiError as exc:
            if exc.status == 404 or exc.code in ("NotFound",):
                continue
            raise


# ── 可选规格目录（硬件 / 基础镜像 / SDK 版本）──────────────────────────────
# 这三组是「创空间能建成什么样」的选项来源。都不需要令牌（spec 的 security 是
# ``[{bearerAuth: []}, {}]``），所以在用户填令牌之前就能展示——否则「选规格」这一步
# 要么空白、要么逼人先填令牌再看。传令牌也不亏：账号不同可见的付费档位可能不同。
#
# **付费档位的含义**：``resource_type=paid`` 表示「用你自己的阿里云账号付费部署」，
# 不是平台代付。所以界面上必须把价格与库存显式标出来——用户点下去是真花钱。

# SDK 类型的中文说明。用户在「Docker / Gradio / Streamlit / 静态页」之间选时，
# 光看英文名不知道选哪个——本平台只推 Docker（我们自己提供 Dockerfile）。
SDK_TYPE_LABELS: dict[str, str] = {
    "docker": "Docker",
    "gradio": "Gradio",
    "streamlit": "Streamlit",
    "static": "静态页面",
}

SDK_TYPE_HINTS: dict[str, str] = {
    "docker": "用仓库里的 Dockerfile 构建（本平台节点用这个）",
    "gradio": "平台托管 Gradio 应用，端口由平台接管",
    "streamlit": "平台托管 Streamlit 应用，端口由平台接管",
    "static": "纯静态站点，无需后端进程",
}


async def list_hardware(
    *, token: str = "", sdk_type: str = "", studio: str = ""
) -> list[dict[str, Any]]:
    """列可用硬件档位。返回 ``HardwareItem`` 列表。

    ``sdk_type`` 过滤「支持该 SDK 的档位」；``studio`` 传 ``owner/repo_name`` 时
    返回**该创空间**可选的档位（已建空间换规格时的口径）。

    免费档位只回 ``name`` / ``resource_type`` / ``supported_sdk_types``——平台不给
    CPU/内存明细（实测：``platform/2v-cpu-8g-mem`` 只回这三个字段）。规格得从
    ``name`` 里解析，见 :func:`describe_hardware`。
    """
    params: dict[str, Any] = {}
    if sdk_type:
        params["sdk_type"] = sdk_type
    if studio:
        params["studio"] = studio
    data = await _call("GET", "/studios/hardware", token=token, params=params, anonymous_ok=True)
    items = data.get("hardware")
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


async def list_base_images(*, token: str = "") -> list[dict[str, Any]]:
    """列可用基础镜像。返回 ``BaseImageItem`` 列表（``{name, tag}``）。

    仅对 Gradio / Streamlit / 静态页生效——**Docker 类型不用它**（spec 原文：
    「仅 Docker 类型不支持」）。我们推的是 Dockerfile，所以这一项只用于展示
    「平台有哪些环境」给用户做参考，或在用户显式切到非 Docker 类型时才带上。
    """
    data = await _call("GET", "/studios/base-images", token=token, anonymous_ok=True)
    items = data.get("base_images")
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


async def list_sdk_versions(*, token: str = "", sdk_type: str = "gradio") -> list[dict[str, Any]]:
    """列 SDK 版本。仅 ``sdk_type=gradio`` 有内容，其余类型平台回空数组。"""
    data = await _call(
        "GET", "/studios/sdk-versions",
        token=token, params={"sdk_type": sdk_type}, anonymous_ok=True,
    )
    items = data.get("sdk_versions")
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


# 硬件档位名的形状：``<厂商>/<规格>``，规格段里带 ``2v``(vCPU) / ``8g-mem``(内存)
# / ``a10``(GPU 型号) 之类的标记。免费档位平台不回 cpu/memory 字段，所以规格只能
# 从名字里读——不解析的话界面上就是一串 ``platform/2v-cpu-8g-mem``，用户看不懂。


def describe_hardware(item: dict[str, Any]) -> dict[str, Any]:
    """把 ``HardwareItem`` 归一成前端直接能渲染的形状。

    为什么要有这一层：平台的字段是**残缺**的——免费档位只回 ``name`` 与
    ``resource_type``，CPU / 内存 / GPU 全都不给（只有付费档位才回）。若前端直接
    渲染原始字段，免费档位就是一行光秃秃的 ``platform/2v-cpu-8g-mem``。

    所以：**优先用平台给的结构化字段，缺失时从 ``name`` 里解析**。解析是启发式的，
    拿不准就不猜（宁可少显示一个数字，也不要显示错的规格——用户按这个选机器）。

    返回::

        {
          "name": "platform/2v-cpu-8g-mem",   # 提交给 API 的原值
          "resource_type": "free" | "paid",
          "free": True,                        # 前端分支用的布尔
          "cpu": 2, "memory": 8,               # 可能为 None
          "gpu": 0, "gpu_type": "", "gpu_memory": None,
          "instance_type": "",
          "has_stock": None, "stock": None,
          "cost_after_discount": None, "original_cost": None,
          "supported_sdk_types": ["docker", ...],
          "spec_label": "2 vCPU · 8 GB 内存",
          "vendor": "platform",
        }
    """
    import re

    name = str(item.get("name") or "").strip()
    resource_type = str(item.get("resource_type") or "").strip().lower()
    vendor = name.split("/", 1)[0] if "/" in name else ""

    def _int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    cpu = _int(item.get("cpu"))
    memory = _int(item.get("memory"))
    gpu = _int(item.get("gpu"))

    # 免费档位缺字段 → 从 name 里补。规格段形如 ``2v-cpu-8g-mem``、``1v-cpu-4g-mem``，
    # GPU 档位形如 ``xgpu-a10-24g`` / ``gpu-1-a10``。只认能确定匹配的模式。
    if cpu is None:
        match = re.search(r"(\d+)v(?=-|$)", name)
        if match:
            cpu = int(match.group(1))
    if memory is None:
        match = re.search(r"(\d+)g-mem", name)
        if match:
            memory = int(match.group(1))

    gpu_type = str(item.get("gpu_type") or "").strip()
    if gpu is None and "gpu" in name.lower():
        # 只有名字里确实提到 GPU 才认，避免把 ``platform/2v-cpu-8g-mem`` 误判成有卡。
        gpu = 1

    # 规格摘要。只列确实知道的项，未知就跳过——宁可短，不可错。
    parts: list[str] = []
    if cpu:
        parts.append(f"{cpu} vCPU")
    if memory:
        parts.append(f"{memory} GB 内存")
    if gpu:
        gpu_text = f"{gpu} × {gpu_type}" if gpu_type else f"{gpu} 卡"
        gpu_memory = _int(item.get("gpu_memory"))
        if gpu_memory:
            gpu_text += f" {gpu_memory} GB 显存"
        parts.append(gpu_text)
    spec_label = " · ".join(parts)

    return {
        "name": name,
        "resource_type": resource_type or ("free" if not item.get("instance_type") else "paid"),
        "free": resource_type != "paid",
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "gpu_type": gpu_type,
        "gpu_memory": _int(item.get("gpu_memory")),
        "instance_type": str(item.get("instance_type") or "").strip(),
        "has_stock": item.get("has_stock") if isinstance(item.get("has_stock"), bool) else None,
        "stock": _int(item.get("stock")),
        "cost_after_discount": _float(item.get("cost_after_discount")),
        "original_cost": _float(item.get("original_cost")),
        "supported_sdk_types": [
            str(s) for s in (item.get("supported_sdk_types") or []) if isinstance(s, str)
        ],
        "spec_label": spec_label,
        "vendor": vendor,
    }


def describe_base_image(item: dict[str, Any]) -> dict[str, Any]:
    """把 ``BaseImageItem`` 归一成前端可渲染形状（``name`` 拆成可读的版本标签）。

    平台给的只是 ``ubuntu22.04-py312-torch2.10.0-modelscope1.37.0`` 这种连字符串。
    拆出 Python / Torch / CUDA / ROCm 版本给用户看——选镜像时真正影响可用性的就是
    这几个，而它们在原始串里要人肉找。
    """
    import re

    name = str(item.get("name") or "").strip()
    tag = str(item.get("tag") or "").strip()

    def _find(pattern: str) -> str:
        match = re.search(pattern, name, re.IGNORECASE)
        return match.group(1) if match and match.groups() else ""

    # Python 版本有两种写法：``py312``（平台现有镜像全用这种紧凑形式）与
    # ``py3.12``。统一归一到 ``3.12``，否则界面上会显示成「Python 312」。
    py_raw = _find(r"py(\d{2,3})(?![\d.])")
    python = f"{py_raw[0]}.{py_raw[1:]}" if len(py_raw) >= 2 else _find(r"py(\d+\.\d+)")

    return {
        "name": name,
        "tag": tag,
        "is_default": tag == "default",
        "os": _find(r"^(ubuntu\d+\.\d+)"),
        "python": python,
        "torch": _find(r"torch(\d+\.\d+\.\d+)"),
        "cuda": _find(r"cuda(\d+\.\d+\.\d+)"),
        "rocm": _find(r"rocm(\d+\.\d+\.\d+)"),
        "modelscope": _find(r"modelscope(\d+\.\d+\.\d+)"),
        # 有没有 GPU 运行时：镜像名里带 cuda/rocm 或 LLM 标记。注意这里不能用
        # _find——那两个词后面没有版本号可捕获，_find 的模式都带捕获组。
        "has_gpu": bool(re.search(r"cuda|rocm", name, re.IGNORECASE)) or "LLM" in name.upper(),
    }


# ── 状态判定辅助 ────────────────────────────────────────────────────────────

def is_sleeping(runtime: dict[str, Any]) -> bool:
    """容器是否处于「不接活」状态，需要唤醒。"""
    return str((runtime or {}).get("status") or "") in _SLEEPING_STATES


def is_in_flight(runtime: dict[str, Any]) -> bool:
    """是否正在构建/部署/停止中——此时不该再叠加动作。"""
    return str((runtime or {}).get("status") or "") in _IN_FLIGHT_STATES


def status_label(status: str) -> str:
    """状态 → 中文展示。未知值原样返回。"""
    return {
        "Initialized": "已初始化",
        "Building": "构建中",
        "BuildFailed": "构建失败",
        "Deploying": "部署中",
        "DeployFailed": "部署失败",
        "Running": "运行中",
        "Stopping": "停止中",
        "Stopped": "已停止",
        "Duplicating": "复制中",
        "Sleeping": "休眠中",
    }.get((status or "").strip(), status or "未知")
