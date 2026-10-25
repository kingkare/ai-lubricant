# 账号 / 渠道 / 模型状态 —— 现状存储梳理

> 状态：现状梳理（as-is），不含设计。梳理时间 2026-10-20。
> 结论性判断与改造方案见后续文档，本文件只回答一个问题：**每个状态实际存在哪里、叫什么、谁写谁读**。

## 1. 一句话结论

同一套「状态」分了三种持久化待遇：

- **人工开关**（启用/停用）→ Postgres 列，可靠；
- **运行期冻结**（临时/永久冷却）→ 内存桶为权威 + Redis 为兜底 + pubsub 跨实例，闭环完整；
- **认证/可用性结论**（`auth_ok` 等）→ **纯内存，无库无缓存无广播**，重启即归零，多实例各算各的。

另有一个历史遗留：账号 JSONB 里的 `is_frozen` 仍被建池时读取，但运行时不再回写，Redis 也不兜底（详见 §6.1）。

## 2. Postgres

### 2.1 标量列（状态真相源）

| 表 | 列 | 含义 | DDL 位置 |
|---|---|---|---|
| `provider_configs` | `enabled BOOLEAN` | 渠道启用/停用 | `server/db.py:145` |
| `provider_accounts` | `switch BOOLEAN` | 账号启用/停用 | `server/db.py:172` |
| `provider_models` | `enabled BOOLEAN NOT NULL DEFAULT TRUE` | 渠道模型行启用/停用 | `server/db.py:739`（迁移 `:747`） |

写入：`upsert_provider_base_config`（`db.py:3558-3577`）、`upsert_provider_account_async`（`db.py:3600` 一带）、`bulk_replace_provider_models` / `upsert_provider_model`（`db.py:8924-8968` / `8859-8871`）。

读出：`list_provider_configs`（`db.py:3710-3757`）、`list_provider_models`。

要点：
- **模型行的 `enabled=False` 不删行**，仍会灌进运行时模型表，由各扫描方按 `enabled` 过滤（`rate_limiter.py:3884-3906` 注释明确）。
- `upsert_provider_model` 的 `ON CONFLICT` **不更新 `enabled` 列**（`db.py:8868-8871`），所以定时同步不会覆盖人工停用。
- `provider_models.account_tpm` 列**已废弃**（建表 `db.py:737` + 迁移 `743-744`），全仓无读写；模型 TPM 的真相源是 `provider_limit_policies.model_tpm`。

### 2.2 JSONB 列

**`provider_configs.config`**（整份渠道配置，一大 JSONB）

| 键 | 是否状态 | 说明 |
|---|---|---|
| `builtin_type` / `code` / `base_url` / `chat_protocols` / `health_check` / `scheduled_test` / `auto_update_models` / `remark` / `tags` … | 否 | 配置项 |
| ~~`accounts`~~ | — | 历史内嵌账号数组，账号态实际在 `provider_accounts` 表 |

**`provider_accounts.account`**（单账号 JSONB）

| 键 | 含义 | 状态性质 |
|---|---|---|
| `username` / `password` / `api_key` / `proxy` / `proxy_id` / `url_prefix` | 凭据与代理 | 配置 |
| `rpm_limit` / `tpm_limit` / `concurrent_limit` / `rpd` / `rph` / `tph` / `tpd` | 账号限额 | 配置 |
| `balance` / `balance_threshold` | 余额与阈值 | **配置**，运行期无回写路径（见 §6.4） |
| `metadata` | 展示用元数据 | 配置 |
| OAuth 系凭据（`refresh_token` / `access_token` / `security_token` / `code_verifier` / `dpop_private_key` / `security_token_expires_at_ms` …） | 各渠道 spec 自定义 | 配置（代码渠道为**整行替换**，schema 外的键静默丢弃） |
| **`is_frozen`** | 永久冻结标记 | **遗留**：建池时被当初始状态读取（`rate_limiter.py:1302`、`admin.py:2387`），运行时解冻**不回写**，Redis 也不兜底 → 僵尸种子（§6.1） |
| `test_config` | 测试配置 | 配置 |

`provider_limit_policies`（一行一渠道的限流/冻结策略）

| 列 | 含义 |
|---|---|
| `enabled` | 策略总开关 |
| `account_rpm` / `account_tpm` / `account_rph` / `account_tph` / `account_rpd` / `account_tpd` / `account_concurrent` / `model_tpm` | 限额阈值（配置，非状态） |
| `balance_enabled` / `balance_method_id` / `balance_min_value` / `balance_cache_ttl_seconds` | 余额门禁配置 |
| `cooldown_policy` JSONB | 状态码→冷却秒数 |
| **`freeze_policy` JSONB** | 冻结规则（`rules[]` + `refresh_freeze_on_failure`），**决定状态怎么产生，本身不是状态** |
| `extra` JSONB | 扩展 |

### 2.3 派生表（非状态）

`model_metadata`（跨渠道模型元数据）、`model_groups`（模型组：成员 + 白/黑名单）、`notifications`（冻结/退登事件流水，`db.py:361-396`）。`notifications` 是状态的**记录**，不是状态的**载体**。

## 3. Redis

### 3.1 键格式

前缀规则：`{REDIS_PREFIX_KEY}:` + 裸键（`limits/backend.py:34-45`、`server/rd.py:44-45`）。本环境实测前缀为 `marsview`（裸键 `limit:cooldown:x` → 实际键 `marsview:limit:cooldown:x`）。

| 用途 | 裸键 | 值 | TTL | 代码 |
|---|---|---|---|---|
| **账号级冻结** | `limit:cooldown:{provider}:{username}` | 冻结原因字符串 | 临时=`ex=seconds`；**永久=无 TTL** | `backend.py:1299-1321` |
| **账号×模型冻结** | `limit:cooldown:{provider}:{username}:model:{model}` | 同上 | 同上 | `backend.py:1474-1500` |
| 冻结索引·渠道集 | `limit:cooldown_index:providers` | SET，成员=provider | — | `backend.py:60-68` |
| 冻结索引·账号 | `limit:cooldown_index:{provider}:accounts` | SET，成员=username | — | `backend.py:72-73` |
| 冻结索引·模型 | `limit:cooldown_index:{provider}:models` | SET，成员=`{username}\x1f{model}` | — | `backend.py:76-77` |
| 模型 TPM 冻结 | `limit:model-tpm-freeze:{model_id}` | — | | `limits/manager.py:496/701/1022` |
| 模型 TPM 窗口 | `limit:window:model:{model_id}:tpm` | 计数 | 窗口期 | `backend.py:329/402` |
| 账号并发租约 | `limit:concurrency:account:{provider}:{username}` | ZSET，score=到期点 | | `backend.py:1290` |
| 账号固定窗口计数 | `account:{provider}:{username}`（rpm/tpm/rpd 维度） | 计数 | | `manager.py:495-497` |
| 账号预留 | `limit:account-reserve:{key}:rpd` | 计数 | | `backend.py:463` |
| 日成功/失败 | `routing:daily:{provider}:{username}:{success\|failure}` | 计数 | | `rate_limiter.py:4339` |

### 3.2 权威与索引

> 冻结索引只加速展示查询，**权威判定始终以精确 cooldown key 的 TTL 为准**（`backend.py:63-64` 注释）。

「永久」的判定是 `ttl == -1`（键存在但无 TTL）：`get_account_cooldown_info`（`backend.py:1324-1378`）、`get_account_model_cooldown_info`（`backend.py:1507-1510`）。

### 3.3 回灌：只加、不清

启动回灌 `ProviderPool.restore_cooldowns()`（`rate_limiter.py:1308-1351`）：

- 账号级：遍历 `pool.clients` 逐个 `GET`+`TTL` → `permanent` 走 `freeze(seconds=None)`，否则 `set_freeze_state_raw(until)`。
- 模型级：读索引成员，再 pipeline 取 TTL+值，同上。

四个不覆盖的场景（事实，不含评价）：

1. `info is None` 时 **`continue`**——不落桶也不清桶（`:1321-1322`）；
2. 只遍历 `self.clients`，池外账号的键不回灌（`:1318`）；
3. 模型级完全依赖索引成员，键在但成员已被 SREM（过期惰性清理 `backend.py:1617-1623`）则不回灌；
4. 模型 TPM 由独立函数 `restore_model_tpm_cooldowns`（`:4066-4073`）恢复，**扫的是窗口键** `limit:window:model:*:tpm` 且条件 `used >= limit`，不读冻结键 `limit:model-tpm-freeze:{id}`。

## 4. 内存（不落任何外部存储的部分）

### 4.1 认证/可用性结论 —— 全在此类字段

`AccountClient`（`server/rate_limiter.py:764`）：

| 字段 | 位置 | 初值 | 持久化 | 广播 |
|---|---|---|---|---|
| `auth_ok: bool \| None` | `:791` | `None` | ❌ 无 Redis / 无 PG | ❌ 无 pubsub |
| `auth_checked_at: float` | `:792` | `0` | ❌ | ❌ |
| `auth_error: str` | `:793` | `""` | ❌ | ❌ |
| `_provider_daily_remaining` | `:796` | `None` | ❌ | ❌ |
| `_provider_quota_disabled_until` | `:798` | `0` | ❌ | ❌ |
| `_stats`（FailureWindow） | `:813` | 空 | ❌ | ❌ |
| `_requests` / `_token_usages` | `:863-864` | 空 | 计数权威在 Redis，内存是窗口副本 | — |
| `_in_flight` | `:777` | `0` | 并发权威是 Redis ZSET | — |
| `_channel_score` / `_ttft_ewma_ms` | `channel.py:495-496` | `1.0` / `None` | ❌ | ❌ |
| `ModelClientPool._model_tpm_cooldowns` | `:1984` | `{}` | 选路**只读这个内存镜像** | pubsub 写入 |
| `ModelClientPool._model_provider_quota` | `:2001` | `{}` | ❌ | ❌ |

`auth_error` 的实际取值（7 种，全是中文串或异常栈，无枚举）：

```
"认证已过期，等待自动刷新"        rate_limiter.py:1923
"认证已失效，需人工更新凭据"      rate_limiter.py:1925
"初始化超时"                      rate_limiter.py:1851
"初始化失败"                      rate_limiter.py:1856
traceback.format_exc()            rate_limiter.py:1931
"初始化返回失败"                  admin.py:9291
f"初始化异常: {e}"                admin.py:9297
```

### 4.2 半内存半 Redis 的

`AccountClient._freeze_state`（`:808`）——**内存为权威，Redis 为兜底**：

```python
{"account": {"until": 0 | None | float, "reason": str},
 "models":   {model_id: {"until": ..., "reason": ...}}}
```

注释声明「唯一入口 `freeze`/`unfreeze`，内部桶不对外暴露」（`:930`）。

`Channel.models`（`channel.py:494`）——渠道模型行运行时全量真相源，由 `Channel.apply()` 从 PG 灌，pg 为真相源。

`ModelCatalogSnapshot`（`model_catalog.py:18`）——**不可变**进程内快照（`frozen dataclass` + `MappingProxyType`），承载 `groups` / `metadata` / `group_metadata` / `generation` / `fingerprint`；DB 为真相源，`reload_from_db` 全量替换。

## 5. 状态归属汇总

| 状态 | 权威存储 | 兜底 | 跨实例传播 |
|---|---|---|---|
| 渠道 `enabled` | PG `provider_configs.enabled` | 内存 `Channel._enabled` | `EVENT_CHANNEL` pubsub（**60s 对账不刷 `Channel` 对象**，`runtime_handlers.py:309-393`） |
| 账号 `switch` | PG `provider_accounts.switch` | 内存 `disabled` | 60s 对账会重写 `disabled`（`admin.py:2311-2312`） |
| 模型行 `enabled` | PG `provider_models.enabled` | 内存 `Channel.models[i]` | `EVENT_MODEL` pubsub |
| 账号冻结 / 冷却 | 内存 `_freeze_state["account"]` | Redis `limit:cooldown:{p}:{u}` | `EVENT_COOLDOWN` pubsub |
| 账号×模型冻结 | 内存 `_freeze_state["models"]` | Redis `...:model:{m}` + 索引 SET | 同上 |
| 模型 TPM 冷却 | 内存 `_model_tpm_cooldowns`（选路只读它） | Redis 窗口键 + 冻结键 | pubsub |
| **认证/可用性结论** | **内存 `auth_ok`** | **无** | **无** |
| 配额门禁态 | 内存 | 无 | 无（且生产无写入源，见 §6.5） |
| 余额 | PG JSONB `balance` | 内存 | 无运行期刷新 |
| 评分/TTFT | 内存 | 无 | 无（设计如此，仅软降权） |
| 冻结历史/退登事件 | PG `notifications` | — | —（记录，非载体） |

## 6. 现状已知问题（只述事实）

### 6.1 DB `is_frozen` 僵尸种子

- 建池/新账号：`is_frozen=bool(acc.get("is_frozen") or False)` → 写内存桶 `until=None`（永久）（`rate_limiter.py:1302`、`admin.py:2387`、`:809-810`）。
- 运行时解冻：`unfreeze` + Redis 删键 + 广播，**不回写 DB `is_frozen=False`**。
- 回灌：Redis 无键时 `continue`，不清内存/不纠正种子（`:1321-1322`）。
- 注释与实现矛盾：`admin.py:2302-2303` 称「冻结态属运行态…新账号的永久冻结由 `restore_cooldowns` 从 Redis 回灌，**不来自 DB `is_frozen`**」，但 `:2387` 与 `rate_limiter.py:1302` 确实在读 DB 该键。
- **现网实测**：`provider_accounts.account` JSONB 含 `is_frozen` 且值为 True 的账号有 3 个（`gorouter/github-*` ×2、`ch-20/18668178362`），而这 3 个在 Redis 中**均无对应 `limit:cooldown` 键**（`exists=0`）。

### 6.2 认证态无持久化、无广播

`auth_ok` 三处皆无（无 Redis / 无 PG / 无 pubsub）。后果（事实描述）：重启后全员归 `None`（`CHECKING`），需等首轮 `init_all`；多实例各跑各的体检，结论互不可见也不收敛。

### 6.3 状态判定口径分裂

- 候选收集（`rate_limiter.py:5674-5692`）读：`disabled` / `is_frozen` / `is_model_frozen`。**不读** `auth_ok`、`state()`、`is_init()`。
- reserve（`limits/manager.py:369-421`）读：`disabled` / `is_frozen` / `channel.enabled` / **`is_init()`** / `is_model_frozen` / 配额字段 / `check_message`。**不读** `auth_ok`。
- 展示层（`state()`、徽章、前端筛选）读：`auth_ok`。
- 旁路规则不同：`is_test` 旁路 disabled/冻结/渠道禁用；`is_probe` 旁路冻结但**不**旁路 disabled/渠道禁用。

### 6.4 余额无运行期刷新

`balance` / `balance_threshold` 从 PG 载入内存后，评分阶段按 `balance < threshold` 判定（`rate_limiter.py:5079-5081`、`5161-5163`），但无请求期回写路径——判定依据可能是建池时刻的值。

### 6.5 配额门禁「读而不写」

`limits/manager.py:401-405` 读 `provider.quotas.dimensions()` 判 `provider_quota_exhausted`；`rate_limiter.py:1139-1153` 的 `sync_provider_quota` 同样依赖它。事实：全仓（`providers/`、`specs/`）无 `provider.quotas =` 赋值，`WindowQuota` / `CountQuota`（`providers/quota.py:60/169`）无生产构造点。`_has_model_provider_quota` 硬编码 `return False`（`rate_limiter.py:4096-4098`，注释「模型级 provider 限额功能已禁用」）。

### 6.6 `enabled` 有两个读取源

后台任务分两派：`init_all` / `check_account` / `scheduled_test` 读内存 `Channel._enabled`（`rate_limiter.py:1832/1895/2782`）；`clean_message` 读 PG 快照 `provider_config.get("enabled")`（`:4015`）。`_reconcile_providers` 只刷 `_providers_cache`、不调 `pool.channel.apply`（`runtime_handlers.py:312-326`），故丢 `EVENT_CHANNEL` 的实例其 `Channel._enabled` 无对账兜底。

### 6.7 disable 动作「内存即时 + 异步落库」竞态

`_apply_disable`（`rate_limiter.py:1604-1655`）先改内存 `disabled` / `channel.enabled`，再用 `create_task` 异步落 PG。60s 对账按 DB 值重写 `client.disabled`（`admin.py:2311-2312`），落库未完成时存在被回滚窗口。

### 6.8 账号级冻结「Redis SET 未 await 就广播」

`_freeze_account`（`:1742-1746`）：`client.freeze` → `create_task(set_account_cooldown)` → `_broadcast_cooldown`，SET 与广播无顺序保证；模型级已显式 await（`:1766-1773`，注释「Redis SET 必须先于广播完成」）。接收端对 `info is None` 选择**不清**（`runtime_handlers.py:218-225`）作为容忍。

### 6.9 死代码（写了没人读 / 有定义无调用）

`FailureWindow.reset_failures` / `reset_streaks` / `clear_all` / `clear_circuit` / `circuit_failure_count`（`rate_limiter.py:712-755`）；`AccountClient.is_available` / `_has_provider_quota` / `is_model_available`（恒 True）/ `set_cooldown` / `_cooldown_until` setter（`:1031/1105-1125/1155`）；`ModelClientPool.update_account_provider_quota` / `update_model_provider_quota`（`:4114-4128`，无调用方）。

### 6.10 同类信息不同口径

`get_stats` 用 `provider.is_init()` 当认证态计数（`admin.py:10485`），`_provider_summary_response` 用真实 `auth_ok`（`admin.py:4771-4773`）；lite 视图把永久冻结显示为 `cooling`（`admin.py:4560-4563`）；`disable_reason` 恒为 `"switch_off"`，冻结规则产生的禁用不带真实原因（`rate_limiter.py:1634-1635`）。

## 7. 附：三套持久化机制对照

| | 人工开关 | 运行期冻结 | 可用性结论 |
|---|---|---|---|
| 权威 | PG 列 | 内存桶 | 内存字段 |
| 兜底 | — | Redis 键（TTL / 无 TTL） | 无 |
| 索引 | — | Redis 3 个 SET | 无 |
| 跨实例 | pubsub（渠道）/ 对账（账号）/ pubsub（模型） | pubsub + 回灌 | 无 |
| 重启后 | 原样恢复 | 原样恢复（含永久） | **归零**（`CHECKING`） |
| 解除方式 | 人工 | 成功自动解除 / 手动清除 / TTL 到期 | 下次体检覆盖 |
