"""节点载体（Node Carrier）账本与载体 spec 源码表。

一个「载体」= 一种把节点二进制送进目标环境跑起来的方式。本表管两件事：

1. ``NodeCarrierBinding`` — 某个节点绑到哪个载体、载体的配置、保活策略与分步进度。
2. ``NodeCarrierSpec`` — 载体 spec 的 Python 源码（管理端贴代码 → exec 加载，对齐
   代码渠道 ``providers/code_loader.py`` 的「DB 是真源、文件是出厂默认」口径）。

为什么和 ``StudioDeployment`` 分开
--------------------------------
``StudioDeployment`` 是**创空间专属账本**（仓库地址 / ModelScope 令牌 / 规格三件套 /
构建进度），字段强绑定 ModelScope 语义，是创建空间与推送仓库的事实源。本表是**载体
无关**的通用层：哪个载体、怎么保活、最近一次探针结果。两者通过 ``node_id`` 并存，
互不迁移——创空间载体 spec 继续读自己的表，通用状态读这张。这样新载体（AI 沙箱、
未来的云主机）不需要为通用状态再各开一列。

为什么不塞进节点 capability labels
----------------------------------
控制面每次注册都用 ``_capability_labels(caps)`` **整体重建** labels（``node_server/
service.py`` 的 ``_refreshed_record``），onboard 时塞进去的自定义键第一次拨号就丢。
长期记忆必须落自己持久化——这正是 ``StudioDeployment`` 顶部注释记下的坑，本表同源。
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

# 保活策略取值（keepalive_policy）。三选一，不设嵌套枚举类型：路由层校验，
# 存进 CHAR 列便于直接 SELECT 过滤。
KEEPALIVE_OFF = "off"            # 不自动保活，纯手动
KEEPALIVE_ON_DEMAND = "on_demand"  # 掉线且有派发时先唤醒（host_exec/会话路径）
KEEPALIVE_TIMED = "timed"        # 后台循环按 interval_minutes 定期唤醒
KEEPALIVE_POLICIES = (KEEPALIVE_OFF, KEEPALIVE_ON_DEMAND, KEEPALIVE_TIMED)


class NodeCarrierBinding(Model):
    """一个节点与其载体的绑定（一般每节点一行）。

    ``node_id`` 是主键语义：一个节点在任一时刻只被一个载体托管——换载体等于重装，
    是「重新部署」而非并存。解绑（``delete``）不清理远端资产（创空间仓库 / AI 沙箱
    都是用户资产），只解除平台对它的管辖。
    """

    node_id = fields.CharField(max_length=64, pk=True)
    # 载体 spec 名（对应 NodeCarrierSpec.name）。自定义管理节点固定 "custom"。
    carrier_name = fields.CharField(max_length=64)
    # 载体专属配置。创空间：{repo_url, access_token_enc, arch, sdk_type, hardware…}；
    # AI 沙箱：{provider, username, region, model, tunnel_address…}。
    # 凭据类键由载体 spec 自行加密（口径同 StudioDeployment 的 seal_token）。
    #
    # 管理节点行上存的是**管理节点级**参数（如创空间 token）——子节点不存凭据，
    # 部署/保活时从管理节点继承（见 carrier_service.deploy_child_node）。
    config = fields.JSONField(default=dict)

    # ── 虚拟管理节点（is_manager=True）────────────────────────────────────────
    # 管理节点**不进控制面节点台账**（mc_ac_nodes）：它没有客户端、不拨号、无凭据，
    # 真实建一行是纯脏数据。这里用合成 id ``mgr-<uuid>`` 存一行绑定即代表它存在。
    # 子节点的 manager_node_id 直接写这个合成 id——控制面不校验外键，分组授权按
    # id 查也不需要它在节点表里。
    is_manager = fields.BooleanField(default=False)
    # 显示名（虚拟管理节点用；真实节点行的名字在控制面台账里）。
    name = fields.CharField(max_length=64, default="")
    # 贴的载体源码（**只在该管理节点是自定义载体时非空**）。放在绑定行而不是
    # NodeCarrierSpec 表里：一个管理节点一份代码，改它只影响这个管理节点名下的
    # 子节点，不会串到别的管理节点——载体代码是**这个管理节点的**属性。
    source = fields.TextField(default="")
    # 子节点回指其管理节点（虚拟管理节点的合成 id，或老真实管理节点的 node_id）。
    # 空 = 该行是管理节点自己（is_manager=True）或普通载体绑定。
    # keepalive 时要顺着它找到「管理节点的代码 + 参数」来跑，所以子节点行不存代码。
    manager_node_id = fields.CharField(max_length=64, default="")

    # ── 保活策略 ──────────────────────────────────────────────────────────────
    keepalive_policy = fields.CharField(max_length=16, default=KEEPALIVE_ON_DEMAND)
    # timed 模式的间隔（分钟）。默认 8 —— 必须显著小于 AutoClaw 的 10 分钟
    # access_hold 窗口，否则「上一次 wake 的 10 分钟还没过、下一次还没到」的空档里
    # 沙箱睡死、节点二进制随之退出。由**管理节点**定义，子节点继承。
    keepalive_interval_minutes = fields.IntField(default=8)
    # 按需唤醒的冷却：一次唤醒覆盖 10 分钟，窗口内重复派发不必再打一轮 prompt
    # （一轮 ≈ 10~30s，会拖慢派发）。
    on_demand_cooldown_seconds = fields.IntField(default=300)

    # ── 探针记录（与 StudioDeployment 同名同义，便于两表并排查询）──────────────
    last_keepalive_at = fields.DatetimeField(null=True)
    last_keepalive_ok = fields.BooleanField(default=False)
    last_keepalive_note = fields.CharField(max_length=512, default="")

    # ── 部署分步进度 ──────────────────────────────────────────────────────────
    # 形态沿用 StudioDeployment.progress：[{"step": "provision", "status": "done",
    # "message": ""}, …]。状态取值 pending/running/done/failed。
    progress = fields.JSONField(default=list)
    # 部署状态：unset/provisioning/bootstrapping/waiting_node/ready/failed。
    deploy_status = fields.CharField(max_length=32, default="unset")
    # bootstrap 失败计数（AI 载体的「重跑 N 次后交人工」），成功后清零。
    bootstrap_attempts = fields.IntField(default=0)
    bootstrap_last_error = fields.TextField(default="")

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mc_node_carrier_bindings"
        indexes = (("carrier_name",), ("keepalive_policy",), ("deploy_status",))


class NodeCarrierSpec(Model):
    """载体 spec 源码（管理端贴的 Python 类）。

    加载口径与代码渠道完全一致：``sha1(source)`` 缓存、没变不重 exec、变了重 exec。
    ``enabled=False`` 只让路由层不再把该载体挂进前端 Tab，已绑定的历史节点不受影响
    （它们已经跑起来了，只是不能再新部署）。
    """

    name = fields.CharField(max_length=64, pk=True)
    label = fields.CharField(max_length=64, default="")
    source = fields.TextField()
    enabled = fields.BooleanField(default=True)
    sha1 = fields.CharField(max_length=40, default="")
    # 出厂内置 spec（DB 缺行时由文件灌入）与用户改过的区分，便于「恢复出厂」。
    builtin = fields.BooleanField(default=False)
    load_error = fields.TextField(default="")

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mc_node_carrier_specs"
        indexes = (("enabled",),)
