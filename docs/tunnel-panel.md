# 内网穿透：类型 = 服务，抽象面板 = spec

> 本文是「内网穿透」的架构说明。面向运维/开发者，讲清三件事：**服务（原「方案」）**、
> **穿透类型**、以及**抽象面板（panel spec）**如何让每种类型声明自己的能力。

## 1. 术语

| 旧叫法 | 新叫法 | 含义 | 代码标识符（不变） |
|---|---|---|---|
| 方案 | **服务** | 一套可复用的代理服务器配置（连接信息 + 端口段/域名） | `TunnelScheme` / `scheme_id` / `/tunnel-schemes` |
| 代理 / 绑定 | 代理 / 绑定 | 一次暴露：选一个服务 + 目标节点 + 本地端口 → 公网地址 | `TunnelBinding` |

「方案 → 服务」是**纯 UI 文案**改名：页面标题、按钮、toast、字段标签都改成「服务」，
但数据库表名、路由、API 字段名全部不动（`scheme` 一词在代码里保留），零迁移风险。

## 2. 穿透类型（kind）

在 `user_platform/tunnel_service.py` 的 `KINDS` 注册：

| kind | 客户端 | 公网地址来源 |
|---|---|---|
| `frpc` | frpc | 服务端口段自动分配 `server_addr:port` |
| `npc` | npc | 同上 |
| `cloudflared` | cloudflared | quick=运行时随机域名；managed=子域名+根域名 |
| **`chmlfrp`** | **frpc**（面板下发 ini） | **平台调面板建隧道 → 面板下发 ini → 从 ini 取 `server_addr:remote_port`** |

`chmlfrp` 是**面板托管**服务：数据面跑标准 frpc 客户端，但**隧道由平台通过面板 API 创建**。
用户只填面板地址 + token 对；创建穿透时平台拉面板节点列表，用户选**服务节点**（隧道建在该
中继节点上）+ 隧道名 + 端口，平台调 `create_tunnel` 建隧道、拉回面板生成的 frpc ini，
再用 frpc 连接（跑在用户选的执行节点上，和 frpc 一样）。

> **关键区别**：chmlfrp **不是** frpc-family 的「本地拼 config」路径。frps 地址/端口/token
> 全在面板下发的 ini 里，平台**绝不**替用户手填这些。因此：
> - 服务 config 只有 `panel_url` / `access_token` / `refresh_token`（无 server_addr/server_port/token）。
> - 每个 binding 是**独立运行时**（`is_grouped=False`）—— 每条穿透建自己的面板隧道、拿自己的 ini。
> - 面板 ini 缓存在 `runtime.provider_ref`；改隧道定义字段（节点/名/端口）时清缓存，下次
>   reconcile 重建隧道。`public_addr` 从 ini 的 `server_addr` + `remote_port` 解析回填。

## 3. 抽象面板（`tunnel_panel_spec.py`）

每种类型可以声明**自己的面板**：表单字段、面板区块、账号动作、默认服务。前端与后端
都读这份 spec，而不是到处写 `if kind == ...`。新增一个穿透类型 = 在这里加一条。

```python
register_panel_spec("chmlfrp", {
    "kind": "chmlfrp",
    "has_panel": True,
    "panel_url_default": "https://panel.chmlfrp.net/",
    "sections": [账号信息, 积分信息, 定时签到],
    "fields":   [panel_url, access_token, refresh_token],
    "actions":  [checkin(daily), sync_quota(hours/6), sync_account(hours/1)],
    "schemes":  [{ name: "ChmlFrp 官方面板", config: {panel_url: ...} }],
})
```

### 3.1 账号动作（account_actions）

沿用代码渠道那套 `account_schema()["account_actions"]` 的形状（见
`docs/providers/code-channel.md` 场景 P），每条动作有：

- `id` — 与 `perform_action` 的入参一致；`checkin` / `sync_quota` / `sync_account`。
- `label` / `manual` — 手动按钮文案与是否可手动触发。
- `display` — `format=badge`（签到徽标，读 `badge_field`）、`value`（额度，读 `value_field`）、
  `none`（只进面板）。字段名对齐渠道面板，前端渲染口径一致。
- `default` — **调度真相源**：`frequency_unit` / `frequency_value` / `daily_time` / `enabled`。

### 3.2 执行（`tunnel_panel_service.py`）

`execute_action(action_id, config)` 用 vendored SDK（`user_platform/chmlfrp_sdk`，原样移植自
已现网验证的 `chmlfrp-sdk`）驱动面板，返回**窄写回**字段（只回本动作产出的账号字段，
绝不整表覆盖）。面板故障降级为 `{"ok": False, "error": ...}`，不 500。

> **定时签到不做，改跳转面板**：ChmlFrp 的 `POST /qiandao` 要求极验四元组
> （`lot_number` / `captcha_output` / `pass_token` / `gen_time`），服务端拿不到。
> 因此 `checkin` 动作在 spec 里标 `requires_captcha: True` 且 `default.enabled=False`
> —— **调度器跳过它**；前端签到区块给「跳转面板签到」按钮去面板手动签（仍保留
> 「立即签到」尝试，会返回 `captcha_required`）。**定时更新账号信息/积分保留**：
> `sync_account`（每小时）/`sync_quota`（每 6 小时）继续由调度器跑。

### 3.3 定时调度（`tunnel_panel_scheduler.py`）

`tunnel_server` 启动时拉起一个 5 分钟 tick 的循环，遍历所有启用的面板型服务，按 spec
的 `default` 调度判断是否到期（`_due`：daily 走自然日 + `daily_time`；hours/minutes 走滚动间隔），
到期就执行动作并把结果窄写回 `config`。上次执行时间记在 `config["_panel_runs"]`
（`{action_id: epoch}`），重启后不重复签当天的到。

> **关键约束**：面板动作结果只写 `config` 的字段，**不 bump `config_revision`** ——
> 否则每次签到都会重启运行中的 frpc 客户端。见 `routes_tunnel.run_scheme_panel_action`
> 的注释（它直接写 row，不走 `update_scheme`）。

## 4. 配置字段

`chmlfrp` 服务的 `config`：

| key | 必填 | 说明 |
|---|---|---|
| `panel_url` | 否（默认官方面板） | ChmlFrp 面板地址，可指向自建镜像。**添加服务时类型选 chmlfrp 会自动填入 `https://panel.chmlfrp.net/`** |
| `access_token` | 是 | 面板 Bearer JWT（从 localStorage → chmlfrp_auth_tokens.accessToken 复制），用于 账号/积分/签到 + 创建隧道 |
| `refresh_token` | 否 | 配了则平台自动续期 access_token |

> **没有** `server_addr` / `server_port` / `token` —— frps 地址由面板在创建隧道时下发，
> 见 §2。`access_token` / `refresh_token` 在 `_SECRET_KEYS` 里：读取时掩码，编辑时回填掩码即沿用原值。

## 5. API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/users/tunnel-schemes/panel-spec` | 所有类型的面板 spec（前端表单/面板渲染） |
| GET | `/api/v1/users/tunnel-schemes/{id}/panel` | 单个面板型服务的账号/积分/签到快照 |
| POST | `/api/v1/users/tunnel-schemes/{id}/panel/actions/{action_id}` | 手动执行一个动作 |
| GET | `/api/v1/users/tunnel-schemes/{id}/panel/nodes` | ChmlFrp 服务节点列表（创建穿透的「服务节点」下拉） |

创建穿透时 `BindingReq` 带 `panel_node`（chmlfrp 必填）；平台在 dispatch 时调
`tunnel_panel_service.create_tunnel` 建隧道 + 拉 ini，缓存进 `runtime.provider_ref`。

## 6. 测试

`tests/test_tunnel_panel.py`：spec 注册完整性、chmlfrp 配置校验（panel_url 默认值、
access_token 必填）、frp-family 端口分配、调度 due 逻辑。
