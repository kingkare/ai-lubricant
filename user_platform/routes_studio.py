"""创空间（ModelScope Studio）节点部署路由。

挂在管理端 ``/api/v1/admin/nodes/{node_id}/studio`` 下，与节点控制面路由同前缀
（``routes_nodes.admin_router`` 也挂 ``/api/v1/admin``），这样前端「节点列表 →
部署到创空间」弹框的所有调用都在一个资源路径下。

权限与节点控制面一致：``_require_admin``。创空间部署会持有用户的 ModelScope
访问令牌并能向他的仓库推送，属于管理员级操作。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import studio_service
from .deps import get_current_user
from .models import User
admin_router = APIRouter(prefix="/api/v1/admin", tags=["user-platform-studio"])


async def _require_admin(user: User = Depends(get_current_user)) -> User:
    """Gate studio endpoints to platform administrators.

    Same口径 as the node control surface (``routes_nodes._require_admin``):
    创空间部署会持有用户的 ModelScope 访问令牌并能向他的仓库推送，权限必须与
    节点入驻/吊销一致。各路由模块各自定义是本仓约定（见 routes_editors 等），
    避免模块间 import 耦合。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


class StudioDeployReq(BaseModel):
    """部署（或重新部署）一个节点到创空间。

    两种模式：
    - **首次**：只给令牌 + 创空间名（可留空自动生成）。服务端调 ``GET /users/me``
      拿 owner、推导仓库地址、建空间、推码、写 variables、上线——用户全程
      不接触仓库地址、不碰创空间网页。
    - **重部署**：留空令牌和创空间名，服务端从账本里取已存的仓库地址和令牌。
    """

    # 令牌。首次必填；重部署留空 = 沿用账本里的那份。
    access_token: str = ""
    # 创空间名（仓库的 repo_name 段）。首次可留空，用节点 id 自动生成；
    # 重部署留空 = 沿用账本里的仓库地址（此时 repo_url 已落库，不再需要推导）。
    studio_name: str = ""
    # 仓库地址。只在账本已有部署时作为覆盖用；首次留空由服务端推导。
    repo_url: str = ""
    # 创空间公开访问地址，保活探针的目标。留空 = 用平台返回的 host 自动补。
    public_url: str = ""
    # 创空间容器架构。ModelScope 免费档是 x86_64，故默认 amd64。
    arch: str = "amd64"
    # 节点 TOTP 凭据。入驻结果页部署时由前端直接带过来——那时 secret 就在手上，
    # 不必依赖 Redis bootstrap 记录的 30 分钟 TTL。留空则回落到账本/Redis。
    node_secret: str = ""
    node_name: str = ""
    # ── 创空间规格（运行模式 / 硬件 / 镜像 / SDK 版本）─────────────────────
    # 全部留空 = 沿用账本里已存的值（重部署的常态）。首次留空 = 用平台默认
    # （docker 模式 + 免费档硬件）。
    #
    # 本平台节点只跑 Docker 形态（我们推自带 Dockerfile 的仓库），所以 sdk_type
    # 实际只会是 docker；另外三项在 Docker 形态下对平台无意义（base_image 平台
    # 明确说「仅 Docker 类型不支持」），但**仍然落库**：用户的选择意图要留痕，
    # 界面上重新打开面板时才回显得出他上次选了什么。
    sdk_type: str = ""
    hardware: str = ""
    base_image: str = ""
    sdk_version: str = ""


def _guard_error(exc: studio_service.StudioDeployError) -> HTTPException:
    """把服务层的部署错误转成 400。

    这些全是「配置/凭据不对」类的可诊断错误（缺发行版本、令牌无效、地址是回环），
    不是服务端故障，所以用 400 而不是 500——前端直接把 message 展示给运维。

    **同时落一条日志**：400 的响应体前端拿了就丢，服务端日志里若不留底，事后只能
    凭空反推「为什么那次部署失败了」（本次就是这么折腾的——日志里只有一行
    `studio/deploy 400`，具体原因完全查不到）。
    """
    from loguru import logger

    logger.warning("[studio] 部署/管理操作失败：{}", exc)
    return HTTPException(status_code=400, detail=str(exc))


@admin_router.get("/nodes/{node_id}/studio")
async def get_studio_deployment(node_id: str, _: object = Depends(_require_admin)) -> dict:
    """读该节点的创空间部署账本（令牌掩码）。未部署过返回 ``{deployed: false}``。"""
    deployment = await studio_service.get_deployment(node_id)
    if deployment is None:
        return {"deployed": False, "node_id": node_id}
    return {"deployed": True, **deployment}


@admin_router.post("/nodes/{node_id}/studio/deploy")
async def deploy_node_to_studio(
    node_id: str, body: StudioDeployReq, request: Request, _: object = Depends(_require_admin)
) -> dict:
    """部署（或重新部署）一个节点到创空间。

    首次调用只需要令牌（+ 可选的创空间名）：服务端解出 owner、推导仓库地址、
    建空间、推码、写环境变量、上线——用户全程不接触仓库地址与创空间网页。

    幂等：重复调用就是把同一套文件再推一次（内容没变时 git 层面直接跳过），
    并重新上线一次。
    """
    try:
        result = await studio_service.deploy_node(
            node_id=node_id,
            repo_url=body.repo_url,
            studio_name=body.studio_name,
            access_token=body.access_token,
            public_url=body.public_url,
            arch=body.arch,
            node_secret=body.node_secret,
            node_name=body.node_name,
            sdk_type=body.sdk_type,
            hardware=body.hardware,
            base_image=body.base_image,
            sdk_version=body.sdk_version,
            # 创空间容器在公网，拨号地址必须是公网可达的；用请求 origin 兜底
            # （单机部署时管理员用公网域名访问控制台，这个 origin 就是对的）。
            request_base=f"{request.url.scheme}://{request.url.netloc}",
        )
    except studio_service.StudioDeployError as exc:
        raise _guard_error(exc) from exc
    # 部署成功 → 补挂载体绑定（幂等）：进入载体统一定标（保活由载体共享循环
    # 接管，旧循环因互斥自动跳过它）。失败不挂——没有账本行的绑定只会让
    # 保活每轮白报「无创空间部署记录」。
    try:
        from .carrier_service import ensure_studio_binding

        await ensure_studio_binding(node_id)
    except Exception:  # noqa: BLE001 - 绑定补挂失败不影响部署本身
        pass
    return result


@admin_router.get("/studios/options")
async def list_studio_options(request: Request, _: object = Depends(_require_admin)) -> dict:
    """列创空间可用的运行模式 / 硬件档位 / 基础镜像。

    给前端「选择创空间模式」表单的选项来源。这些目录接口在 ModelScope 侧**匿名
    可读**，所以这里也不强制要求本平台侧先有令牌——但**不匿名**：管理端路由，
    权限口径与节点控制面一致。转发时若有已存令牌就带上（账号不同可见的付费
    档位可能不同）。

    任何一组拉取失败都不整体失败（见 ``studio_service.list_studio_options``）：
    该组返回空列表，错误说明放在 ``errors`` 里由前端展示。
    """
    from .models_studio import StudioDeployment
    from .studio_deploy import open_token

    token = ""
    # 节点路径可选：传了就复用该节点已存的令牌（能看见该账号专属的付费档位）。
    node_id = request.query_params.get("node_id", "").strip()
    if node_id:
        row = await StudioDeployment.get_or_none(node_id=node_id)
        if row is not None:
            token = open_token(row.access_token_enc)
    return await studio_service.list_studio_options(token=token)


@admin_router.post("/nodes/{node_id}/studio/keepalive")
async def keepalive_studio_node(node_id: str, _: object = Depends(_require_admin)) -> dict:
    """立即探活一次创空间（「唤醒」按钮）。

    免费档创空间闲置会休眠，节点随之掉线。后台循环只对**离线**节点探活；这个
    接口让运维手动戳一下，不必等下一轮。
    """
    try:
        return await studio_service.keepalive_now(node_id)
    except studio_service.StudioDeployError as exc:
        raise _guard_error(exc) from exc


@admin_router.delete("/nodes/{node_id}/studio")
async def delete_studio_deployment(node_id: str, _: object = Depends(_require_admin)) -> dict:
    """删除部署账本。**不动创空间仓库**——仓库是用户资产，删文件请在创空间侧操作。"""
    removed = await studio_service.delete_deployment(node_id)
    # 摘掉载体绑定（缺行不报错）：节点不再受载体循环管了。
    try:
        from .carrier_service import remove_studio_binding

        await remove_studio_binding(node_id)
    except Exception:  # noqa: BLE001 - 摘绑失败不影响删除本身
        pass
    return {"deleted": removed, "node_id": node_id}


class StopStudioReq(BaseModel):
    """停止创空间（释放算力）。重新部署时服务端会再调 deploy 拉起。"""

    token: str = ""


@admin_router.post("/nodes/{node_id}/studio/stop")
async def stop_studio_node(
    node_id: str, body: StopStudioReq, _: object = Depends(_require_admin)
) -> dict:
    """停止创空间容器（释放算力，下次部署/唤醒时自动拉起）。"""
    try:
        return await studio_service.stop_studio(node_id, access_token=body.token)
    except studio_service.StudioDeployError as exc:
        raise _guard_error(exc) from exc


@admin_router.post("/nodes/{node_id}/studio/teardown")
async def teardown_studio_node(node_id: str, body: StopStudioReq, _: object = Depends(_require_admin)) -> dict:
    """下线并清理创空间节点：停容器 → 清节点凭据 → 删部署账本。

    与 ``DELETE .../studio``（只删账本）的区别：那条会让节点的 TOTP 凭据**继续
    留在创空间的环境变量里**，而仓库他可能转作他用/改公开/共享——等于一个长期
    有效凭据悬在别人能碰的地方。这条会把它清掉。

    **不会删除创空间仓库**：官方 OpenAPI 没有删除创空间的端点（实测
    ``DELETE /studios/{o}/{r}`` 返回 404 page not found）。剩余要手动做的步骤在
    ``manual_steps`` 里如实返回。
    """
    try:
        result = await studio_service.teardown_studio(node_id, access_token=body.token)
    except studio_service.StudioDeployError as exc:
        raise _guard_error(exc) from exc
    # 下线即不再保活：摘掉载体绑定（缺行不报错）。
    try:
        from .carrier_service import remove_studio_binding

        await remove_studio_binding(node_id)
    except Exception:  # noqa: BLE001 - 摘绑失败不影响下线本身
        pass
    return result


@admin_router.get("/nodes/{node_id}/studio/progress")
async def get_studio_progress(node_id: str, _: object = Depends(_require_admin)) -> dict:
    """部署进度（含平台侧那几步：构建镜像 / 容器启动 / 容器运行中）。

    与 ``GET .../studio``（只读账本里存的 progress）的区别：这条会**顺带推进**
    平台侧的三步——按 ``runtime.status`` 回填，所以面板轮询它就能看到进度往前走。

    服务端按节点节流（默认 8s 打一次 ModelScope），面板可以放心地 1.5s 轮一次。
    探针失败/没有账本都返回现有进度（不抛）——进度是观测信息，不该让轮询整条报错。
    """
    progress = await studio_service.refresh_deploy_progress(node_id)
    return {"node_id": node_id, "progress": progress}


@admin_router.get("/nodes/{node_id}/studio/logs")
async def get_studio_logs(
    node_id: str,
    log_type: str = "run",
    _: object = Depends(_require_admin),
) -> dict:
    """拉创空间日志（构建/运行），排障用。

    免费档创空间闲置会休眠、节点随之掉线时，这里能直接看出是「休眠了」还是
    「构建/运行失败」——不必去 ModelScope 网页翻。
    """
    try:
        return await studio_service.get_studio_logs(node_id, log_type=log_type)
    except studio_service.StudioDeployError as exc:
        raise _guard_error(exc) from exc


@admin_router.get("/nodes/{node_id}/studio/status")
async def get_studio_status(node_id: str, _: object = Depends(_require_admin)) -> dict:
    """查创空间运行时状态（Running/Sleeping/BuildFailed…）。

    与 ``/studios/status``（列表用的缓存摘要）不同：这条**现打 ModelScope 接口**，
    拿到的是此刻的真值，并顺带把结论落库供列表展示。
    """
    try:
        return await studio_service.get_studio_runtime(node_id)
    except studio_service.StudioDeployError as exc:
        raise _guard_error(exc) from exc
