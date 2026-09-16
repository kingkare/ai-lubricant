# 外部渠道导入（New API 等）

把其它 LLM 网关 / 中转框架（New API、One API 等）的渠道列表整体导进 ai-lubricant，
直接创建为本地渠道（`provider_configs` + `provider_accounts`），而不是逐个重填。

入口：管理端「供应商」页右上「导入外部渠道」按钮。设计为通用适配器框架，New API
是首个适配器，后续接别的框架只需在 `channel_importers/adapters/` 下加一个适配器模块。

## 数据源两条

1. **API 拉取**：填 New API 地址 + 管理员令牌（role ≥ admin 的会话 token 或 PAT），
   服务端走 `providers/proxy_manager` 调对方 `GET /api/channel` 分页拉全量渠道。
2. **粘贴文本**：粘对方渠道接口的 JSON 响应、或每行一个渠道对象的 JSONL。
   **这是唯一能带密钥进来的入口**（见下）。

## 关键约束：列表接口不回传密钥

New API 的渠道列表接口做了 `Omit("key")`，**不返回渠道密钥**；`/search` 同样。
密钥只能逐个 `POST /api/channel/:id/key`，且该接口要 root 会话 + 交互式安全验证
（`X-Security-Proof`，2FA/邮箱），程序化无法批量获取。

所以 **API 拉取路径导入的渠道账号必然为空**——弹框里会明确提示，导入后需要在渠道
详情的「账号管理」里补填密钥。要连密钥一起导入，只能走「粘贴文本」。

## 冲突处理

导入前先勾选要导入的渠道，服务端按 `_normalize_base_url_for_compare` 检测本地是否
已有同地址渠道，命中则在预览里标出冲突。提交时三种模式（用户选）：

- **跳过**：保留本地已有渠道，不动。
- **覆盖**：用导入数据更新本地渠道的基础配置（仅覆盖导入侧显式声明的字段，语义对齐
  `channel_template_service.apply_channel_template`）+ 账号逐条 upsert。
- **合并账号**（默认）：只把导入的账号追加到本地渠道，不碰渠道基础配置。
  适合 New API 里「五个渠道同一 base_url、五个不同 key」的常态。

## 三个映射坑（改动适配器前先读）

1. **`priority` 极性相反**：New API 越大越优先，ai-lubricant 的 `account_priority`
   越小越优先（`server/rate_limiter.py` 用 `1/(1+priority)`）。**不映射**，只告警。
   `weight` 两侧极性一致，可映射。
2. **`model_mapping` 对应 `provider_models` 行**，不是 `model_id_rewrite_rules`。
   New API 的 `{A: B}` = 客户端要 A、上游拿 B →
   `{"upstream_model_id": B, "model_id": A}`（运行时 `Channel.resolve_upstream_id`
   直接读）。`model_id_rewrite_rules` 是反方向（上游 → 对外），用它会搞坏模型名。
3. **`models` 是逗号分隔字符串**。`db._provider_model_payload` 对裸字符串按序列处理，
   `"gpt-4o"` 会被拆成 `{"upstream_model_id": "g", "model_id": "p"}`——所以导入器
   必须产 `{upstream_model_id, model_id}` dict，不能产裸字符串。

## 渠道类型 → 协议映射

详见 `channel_importers/adapters/newapi_types.py`。要点：

- `1` OpenAI / `8` 自定义 / `20` OpenRouter / `43` DeepSeek / `48` xAI / `60` NewAPI
  → `openai` 协议，`/v1/chat/completions`。
- `14` Anthropic → `anthropic`，`/v1/messages`。
- `24` Gemini → `gemini`，路径运行时动态拼（按约定留空）。
- `57` Codex → `responses`，`/v1/responses`。
- `33` AWS Bedrock / `41` VertexAI → **不可导入**（签名鉴权，无法作为静态密钥渠道）。
- 媒体生成类（`2`/`36`/`50`/`51`/`52`/`55`/`56`）→ 预览里默认不勾选。
- 智谱 / 阿里 / 火山 / Cohere 等厂商的 OpenAI 兼容地址内嵌非 `/v1` 版本段，
  追加 `/v1/chat/completions` 大概率不对 → 出 warning，预览里路径可编辑。

每个渠道只写一条协议行：网关已做客户端协议→上游协议转换，单条 openai 行即可服务
Anthropic / Responses 客户端。

## 空 base_url 渠道

**直接过滤掉**，不进预览候选列表（base_url 是运行时唯一出口，空地址导入进来也发不出
请求）。响应里会统计并提示「已过滤 N 个上游未填渠道地址的渠道」。

## 安全

- 密钥与令牌永不落日志 / 回显：审计经 `_scrub_relay_secret`，预览只回掩码后的
  `key_preview` + `has_keys` + `account_usernames`。
- 账号凭据写 `password` 字段（不是 `api_key`）：`CustomProvider` 读
  `api_key > key > password`，写 `api_key` 会复刻「改了密钥重启不生效」的陈旧遮蔽 bug
  （`_demask_custom_account_credentials` 正是为保住 `password` 这条活凭据存在的）。
- 没密钥就不写账号行：空 key 占位账号会进运行池并表现为永久认证失败。
- SSRF：远端 `base_url` 由操作人提供，仅允许 http/https，`allow_redirects=False`，
  内网目标**告警不拦**（从局域网 New API 导入是正当常见用法）。
- 上限：`page_size ≤ 200`、`max_pages ≤ 50`、粘贴 ≤ 2MB、`selection ≤ 50`。

## 后端路由

- `GET /admin/channel-importers` — 可用框架清单
- `POST /admin/channel-importers/preview` — 干跑，零写入
- `POST /admin/channel-importers/commit` — 按勾选建/并渠道（部分失败也 200，逐项报告）

提交时客户端重发源输入、服务端重新派生（不做服务端预览缓存）：多实例部署下进程内
缓存不共享，而缓存密钥又要跨实例复制。重派生无状态且保证「预览所见 == 提交所得」。

## 模块布局

```
channel_importers/
  base.py          ExternalChannel / ExternalChannelAccount / ChannelImporter 协议
  registry.py      适配器注册表
  payload.py       ExternalChannel → 建渠道 payload（纯函数）
  errors.py
  adapters/
    newapi.py          NewApiImporter：fetch + parse + 映射
    newapi_types.py   渠道类型常量表 / 协议映射表
server/channel_importer_api.py   管理端路由（preview + commit）
```

建渠道落在 `admin._create_provider_from_config`（与「添加供应商」向导共用，避免两条
创建路径漂移）。

## 测试

- `tests/test_channel_importer_newapi.py` — 适配器纯映射/解析 + 打桩 fetch
- `tests/test_channel_importer_api.py` — 路由编排（预览零写入、冲突三模式、脱敏）
- `tests/test_channel_importer_ui.py` — 前端接线断言
