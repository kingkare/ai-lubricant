"""ModelScope 创空间（Studio）节点部署账本。

一个创空间节点 = 一个 execution 节点 + 一份「怎么把它重新部署上去」的账本。
账本单独建表而不是塞进节点的 capability labels：控制面在每次节点注册时用
``_capability_labels(caps)`` **整体重建** labels（见 ``node_server/service.py``
的 ``_refreshed_record``），onboard 时塞进去的自定义键在节点第一次拨号后就丢
了——``guest_image`` 能吃住只是因为它在 onboard 同一次请求里就被消费。创空间
要长期记住仓库地址与保活地址，必须有自己的持久化。

为什么 token 独立存而不复用 ``GitIdentity``
--------------------------------------------
``GitIdentity`` 是「用户级 git 凭据」，带仓库列举/分支/树/建仓一整套 REST
客户端（``git_clients.py``），而创空间只需要一条 HTTPS push。把 ModelScope 塞
进 ``_SUPPORTED_PLATFORMS`` 会连带打开一条没有实现的 API 分支，收益为零。这里
自存自用，口径与 ``GitIdentity`` 一致：落库前加密、出站前解密、API 响应掩码。
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class StudioDeployment(Model):
    """一个 execution 节点在 ModelScope 创空间上的部署记录。

    ``node_id`` 同时是主键与外键语义：一个节点最多对应一份创空间部署，重新部
    署（换仓库、换 token）就地覆盖同一行，不做历史版本——创空间侧的 Git 历史
    才是真正的版本记录，这里只是「当前该往哪推」。
    """

    node_id = fields.CharField(max_length=64, pk=True)
    # 创空间 Git 仓库地址（不含凭据）。形如
    # ``https://www.modelscope.cn/studios/<owner>/<name>.git``。凭据在 push 时
    # 以 ``oauth2:<token>`` 的形式临时注入 git 配置，绝不写进本列。
    repo_url = fields.CharField(max_length=512)
    # 创空间公开访问地址（保活探针的目标）。与 repo_url 不同：前者是 git 端点，
    # 后者是应用对外域名。留空则该节点不参与保活（用户没填就只做部署）。
    public_url = fields.CharField(max_length=512, default="")
    # ModelScope 访问令牌，AESGCM 封存（见 studio_deploy.seal_token）。密钥缺失
    # 时降级为明文（带 ``plain:`` 前缀）并在 API 层掩码。比 ``GitIdentity`` 更严：
    # 那张表的 access_token 是明文列，这里默认就加密，因为令牌能写用户的仓库。
    access_token_enc = fields.TextField(default="")
    # 节点 TOTP 凭据，同样封存。**必须自己存一份**：入驻时下发的凭据只在 Redis
    # 里活 30 分钟（``_NODE_BOOTSTRAP_TTL_SECONDS``），过期后「重新部署」就拿不
    # 到了——而重新部署恰恰是换架构、改保活地址、仓库被清空后重推的常规操作。
    # 与 ``mc_ac_nodes.credential_secret_enc`` 同一把主密钥、同一张库，不引入新的
    # 暴露面。
    node_secret_enc = fields.TextField(default="")
    # 该节点二进制对应的目标架构（linux 下的 amd64/arm64）。创空间构建期按它
    # 拉取 node-execution，选错的表现是容器起不来（exec format error）。
    arch = fields.CharField(max_length=16, default="amd64")
    # ── 创空间规格三件套（创建时带上、重部署时按需 PATCH settings）──────────
    # SDK 类型。本平台节点固定 docker（我们推 Dockerfile）；保留成字段而不是
    # 常量，是为了「让用户看见创空间能选哪几种模式」——界面上展示 gradio /
    # streamlit / 静态页 这些可选项时，用户选了哪个就在这里记下来，即便实际
    # 部署仍走 docker（节点只能用 docker 跑），也尊重了他的选择意图。
    sdk_type = fields.CharField(max_length=16, default="docker")
    # 硬件档位名（如 ``platform/2v-cpu-8g-mem``）。空 = 用平台默认（免费档）。
    # 改了要重新部署才生效，所以这里只记「用户上次选了什么」，真正生效靠重新部署
    # 时的 PATCH settings。
    hardware = fields.CharField(max_length=128, default="")
    # 基础镜像名（仅 gradio/streamlit/static 生效；docker 不用——我们自带 Dockerfile）。
    # 同样只记意图，生效靠 PATCH + 重新部署。
    base_image = fields.CharField(max_length=128, default="")
    # SDK 版本（仅 gradio 生效）。空 = 用平台默认（latest）。
    sdk_version = fields.CharField(max_length=32, default="")
    # 该节点拨号的服务端地址（含端口）。空 = 用服务端全局配置/请求 origin 兜底。
    #
    # 存这里而不是只靠入驻时的 Redis bootstrap：那份 30 分钟就过期，而「重新部署」
    # 是常规操作（换架构、改配置、仓库被清空后重推），届时必须还能拿到地址——否则
    # 每次重推都要用户重填，或者更糟：静默用了一个对他不可达的兜底地址。
    server_address = fields.CharField(max_length=512, default="")
    # 最近一次推送结果，供前端展示「上次部署成功/失败」。
    last_push_at = fields.DatetimeField(null=True)
    last_push_error = fields.TextField(default="")
    # 最近一次部署的分步进度：``[{step, label, state, detail, at}]``。
    #
    # 为什么要落库而不是只在响应里返回：整条链路（建空间→推码→写变量→上线）
    # 要跑几十秒到几分钟，用户点完只能干等。每完成一步写一次库，前端轮询就能
    # 实时显示「现在在做哪一步、哪步失败了」。这是一次部署的运行时状态，
    # 下次部署时整体覆盖。
    progress = fields.JSONField(default=list)
    # 最近一次保活探针时间与结果，供前端展示「创空间是否醒着」。
    last_keepalive_at = fields.DatetimeField(null=True)
    last_keepalive_ok = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mc_studio_deployments"
        indexes = (("public_url",),)
