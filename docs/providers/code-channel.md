# 代码渠道：先选场景，再写钩子

> 这份文件就是管理端「使用说明」弹框的内容。修改本文件后，管理端重新打开弹框即可看到；前端不再另存一份 JSX 说明。
>
> **最重要的一句话：** 代码渠道不是让你重写 Provider，而是让你贴一个普通的 spec 类。只写你要改变的部分，其他部分继续使用「自定义渠道」的配置驱动实现。

## 0. 先判断：你属于哪一种情况？

| 你的情况 | 先不要写什么 | 你应该写 | 直接看哪一节 |
|---|---|---|---|
| 上游是 OpenAI / Anthropic / Responses / Gemini 兼容接口 | 不需要写聊天代码 | **零钩子**，只填渠道地址、协议行、路径 | [场景 A](#场景-a纯兼容接口什么代码都不用写) |
| 只需要多加一个签名头、租户头 | 不要重写 `stream_chat` | `headers` | [场景 B](#场景-b只加一个请求头) |
| 请求体只比标准协议多一个字段 | 不要自己拼 URL 和完整 body | `payload` | [场景 C](#场景-c只改请求体一个字段) |
| 上游 URL 不是配置里的标准路径 | 不要复制整套聊天逻辑 | `build_url` | [场景 D](#场景-d只改聊天-url) |
| 上游 SSE 字段不是 `choices[].delta` | 不要重写 HTTP 请求 | `parse_chunk` | [场景 E](#场景-e只改-sse-解析) |
| 有 OAuth 设备码登录 | 不要在前端硬编码字段 | `account_schema` + `begin_device_flow` + `poll_device_flow` | [场景 F](#场景-f设备码授权) |
| 有 OAuth 回调登录 | 不要自己做授权页面 | `account_schema` + `build_auth_start` + `handle_auth_callback` | [场景 G](#场景-g回调式-oauth) |
| token 需要定时刷新 | 不要另写后台定时任务 | `refresh_auth` + `SCHEDULED_REFRESH = True` | [场景 H](#场景-h刷新-token) |
| 有每日签到 / 额度同步这类「作用于账号」的周期动作 | 不要在前端硬编码按钮 | `account_schema()["account_actions"]` + `perform_action`（`refresh_auth` 作复合入口顺带也做） | [场景 P](#场景-p账号动作签到同步额度等周期动作) |
| 账号密码先换 ticket / Cookie | 不要继承 `BaseProvider` | `init_auth` + 需要的聊天/模型钩子 | [场景 I](#场景-i账密登录和非标聊天) |
| 只想拒绝某些模型或消息 | 不要在聊天函数里偷偷判断 | `check_message` | [场景 J](#场景-j请求前拦截) |
| 上游不认 function calling（模型把工具调用当正文吐） | 不要自己写文本→工具解析 | `p.build_tools_prompt` + `p.parse_tool_calls` | [场景 K](#场景-k上游不认-function-calling文本转工具调用) |

### 你必须记住的 7 条规则

1. 写 `class MyChannel:`，**不要继承** `BaseProvider` 或 `CustomProvider`。
2. 方法推荐写 `@staticmethod`，第一个参数 `p` 是渠道实例。
3. **渠道地址由管理端「渠道配置 → 渠道地址」填写，并且是必填项。** spec 通过 `p.base_url` 读取它；不要把用户可配置的地址写死在代码里。确实不打外网的 spec（例如本地 Echo）在类上写 `REQUIRES_BASE_URL = False` 才可以留空。
4. **usage 不用你算。** 上游没回 token 统计时，框架会按实际内容自动估算。只有拿到上游真实 usage 才需要覆盖：流式 `yield {"usage": {...}}`，非流式在响应体里放 `usage`。
5. **不要 `import aiohttp` 自己发请求。** 用 `p.send_sse_request(...)` / `p._make_session()`，出站代理、连接复用、超时、请求留痕才会生效。
6. 不要写 `PROVIDER_NAME`，加载器会用渠道 id 覆盖它。贴入的代码拥有服务进程权限，等同于部署一个 Python 文件，只允许可信管理员编辑。
7. OAuth / 多字段账号在 `account_schema()["fields"]` 里声明表单字段；schema 之外的运行时字段写到 `ACCOUNT_FIELDS`。两者都会自动挂成 `p.access_token` / `p.enterprise_id` 这类实例属性，缺值也会挂空串，热更新能就地同步。

---

## 场景 A：纯兼容接口，什么代码都不用写

例如上游提供 `/v1/chat/completions`，接受标准 Bearer token。你只需要在渠道配置里填：

- `base_url`：例如 `https://api.example.com`
- 协议：OpenAI
- 聊天路径：`/v1/chat/completions`
- 模型路径：`/v1/models`
- 账号里的 key / token

源码可以保持为空吗？不能，代码渠道至少要有一个钩子。此时贴最小的认证钩子即可：

```python
class CompatibleChannel:
    @staticmethod
    async def init_auth(p, is_check=False):
        # p.api_key、p.base_url 都由渠道配置派生。
        # 不需要自定义认证逻辑时，返回 True 即可。
        return bool(p.api_key and p.base_url)
```

`stream_chat`、`non_stream_chat`、模型列表、错误处理全部回落到配置驱动的 `CustomProvider`。

---

## 场景 B：只加一个请求头

**什么时候用：** 上游是兼容接口，只额外要求 `X-Tenant-Id`、签名头或自定义 User-Agent。

`headers` 收到的是框架已经按真实协议生成的请求头。只改你需要的字段，然后返回它；返回 `None` 则保留原头。

```python
class TenantChannel:
    @staticmethod
    async def init_auth(p, is_check=False):
        return bool(p.api_key and p.base_url)

    @staticmethod
    def headers(p, base_headers, kwargs):
        # p.username 是当前账号；p._channel 是渠道配置对象。
        base_headers["X-Tenant-Id"] = p.username
        base_headers["User-Agent"] = "my-gateway/1.0"
        return base_headers
```

不要在这里重新拼 `Authorization`，除非上游确实要求特殊认证；框架已经按活跃协议处理了标准认证头。

---

## 场景 C：只改请求体一个字段

**什么时候用：** 上游兼容 OpenAI，但要求额外的 `vendor_mode`，或要求把 `temperature` 改成自己的字段。

`payload` 收到框架按协议构造好的 body。它是**后处理钩子**，不是让你从零构造请求。

```python
class VendorPayloadChannel:
    @staticmethod
    async def init_auth(p, is_check=False):
        return bool(p.api_key and p.base_url)

    @staticmethod
    def payload(p, endpoint, model_id, messages, stream, kwargs):
        # _base_payload 是框架按当前协议生成好的请求体；复制后只改需要的字段。
        body = dict(kwargs["_base_payload"])
        body["vendor_mode"] = "fast"
        return body
```

如果只是标准字段改名、协议本身已经有专用支持，优先在渠道配置中选择正确的协议行，不要用代码钩子绕过配置。

---

## 场景 D：只改聊天 URL

**什么时候用：** 上游的聊天地址不是按标准 `chat_path` 拼出来的，例如路径中必须带租户或模型名。

```python
class UrlChannel:
    @staticmethod
    async def init_auth(p, is_check=False):
        return bool(p.api_key and p.base_url)

    @staticmethod
    def build_url(p, kwargs):
        model_id = str(kwargs.get("model_id") or "")
        # p.base_url 是前端渠道配置中的地址，不要写死域名。
        return f"{p.base_url.rstrip('/')}/tenant/{p.username}/models/{model_id}/chat"
```

只改 URL 时不要同时重写 `stream_chat`；这样 headers、payload、重试、限流和请求留痕仍由框架负责。

---

## 场景 E：只改 SSE 解析

**什么时候用：** 上游仍然是 SSE，但数据不是标准的 `choices[0].delta.content`。

返回统一帧字段：`content`、`thinking`、`tool_calls`。返回 `None` 会回落标准解析。

```python
import json

class SseChannel:
    @staticmethod
    async def init_auth(p, is_check=False):
        return bool(p.api_key and p.base_url)

    @staticmethod
    def parse_chunk(p, event_str):
        # event_str 是一帧原始 SSE 文本。
        if event_str.strip() == "[DONE]":
            return {"content": "", "thinking": "", "tool_calls": []}
        try:
            data = json.loads(event_str.removeprefix("data:").strip())
        except (TypeError, ValueError):
            return None  # 交给框架的标准解析器
        return {
            "content": data.get("answer", ""),
            "thinking": data.get("analysis", ""),
            "tool_calls": data.get("tools", []),
        }
```

不要在 `parse_chunk` 里发网络请求、写 Redis 或创建后台任务；它应该只做一帧到统一帧的转换。

---

## 场景 F：设备码授权

**什么时候用：** 上游要求用户打开网页、输入 `user_code`，然后后台轮询直到授权完成。

> **统一后**：设备码被框架自动包装成「异步步骤」——前端只认步骤一种形态（显示 user_code /
> 授权地址 + 后台轮询）。spec 仍按下面写 `begin_device_flow` + `poll_device_flow` 即可，
> 框架自动套壳；如要主动声明步骤化（迁移后的推荐写法），按本节末尾的「步骤化写法」包一层。

这里有 3 个关键部分：

1. `account_schema()` 告诉前端要显示哪些账号字段和授权按钮。
2. `begin_device_flow()` 返回前端展示信息和轮询参数。
3. `poll_device_flow()` 返回 `pending` 或 `authorized`；授权成功的字段放在 `account_data`。

`begin_device_flow(p, auth_context=None)` 与 `poll_device_flow(p, poll_params, auth_context=None)`
都能收到框架的**授权上下文**（老写法只收 `p` / `p, poll_params` 也照常工作，框架按签名传参）。
上下文回答「浏览器回调该回到哪」——服务端地址前端知道、渠道不知道：

```python
{
    "provider": "my-channel",        # 渠道 id
    "state": "...",                  # 框架授权会话标识
    "origin": "https://panel.example.com",   # 管理员浏览器地址（scheme://host[:port]）
    "scheme": "https", "host": "...", "port": 443,
    "callback_path": "/admin/providers/my-channel/accounts/auth/callback",
    "callback_url": origin + callback_path,   # 回调式授权用的完整回调地址
    "loopback_callback_path": "/oauth/callback",   # 本机回调根路由（见下）
}
```

授权成功的落库由服务端中央扫描器在轮询到 `authorized` 时完成，不依赖管理页面继续开着；
spec 不要自己写 `while True`。

> **统一后变化**：`authorized` **不再自动落库**——扫描器标记 `completed` + 存 `account_data`，
> 前端轮询拿到后回填账号表单，用户核对后手动点「保存」才建账号（与步骤认证一致）。

### 步骤化写法（迁移后的推荐）

存量 spec 只声明 `begin_device_flow` + `poll_device_flow` 就能被框架自动包装。迁移后的
推荐写法是显式声明 `begin_step_auth` / `submit_step_auth`，把设备码流包成一个异步步骤：

```python
@staticmethod
async def begin_step_auth(p, auth_context=None) -> dict:
    """把设备码流包装成异步步骤：显示 user_code/地址 + 声明 poll（后台照旧轮询）。"""
    flow = await DeviceChannel.begin_device_flow(p, auth_context)
    return {
        "task_type": "steps",
        "step": {
            "id": "device_authorize",
            "title": "设备码授权",
            "description": "请在授权页面完成登录，完成后自动继续。",
            "fields": [],
            "user_code": str(flow.get("user_code") or ""),
            "verification_uri": str(flow.get("verification_uri") or ""),
            "auth_url": str(flow.get("auth_url") or flow.get("login_url") or ""),
            "poll": {"interval": int(flow.get("interval") or 5),
                     "expires_in": int(flow.get("expires_in") or 900)},
        },
        "step_state": {"poll_params": flow.get("poll_params") or {}},
        "expires_in": int(flow.get("expires_in") or 900),
    }

@staticmethod
async def submit_step_auth(p, step_id, values, step_state, action=None, auth_context=None) -> dict:
    """异步步骤无表单输入：提交即等待，后台轮询推进。"""
    return {"status": "pending", "message": "等待授权完成…", "step_state": step_state or {}}
```

`poll_device_flow` 保留不动——后台扫描器仍调它轮询。这样存量流零改动，spec 只是显式表达了
「这是异步步骤」，前端走统一的步骤渲染。

### 声明「授权怎么才算完成」：`auth_start.completion`

上游的完成方式有三类，渠道在 `account_schema()` 里声明，**前端据此决定 UI 形态**：

| `completion` | 上游行为 | 前端表现 | 需要的钩子 |
|---|---|---|---|
| `poll`（默认） | 用户在浏览器登录完，服务端轮询能查到结果 | 只显示「授权进行中」，自动完成 | `begin_device_flow` + `poll_device_flow` |
| `callback` | 上游回调服务端的 `redirect_uri` | 只显示跳转提示，回调落地即完成 | `build_auth_start` + `handle_auth_callback` |
| `loopback` | 上游只回调 `127.0.0.1:{port}`，不收我们的地址 | 额外显示输入框：跨机时把地址栏 URL 粘回来补投 | `begin_device_flow` + `poll_device_flow` + `handle_loopback_callback` |

不声明 `completion` 时框架按 `mode` 推导：`oauth_callback`/`popup` → `callback`；
`device_code` 且声明了 `loopback` → `loopback`；其余 → `poll`。**存量渠道零改动。**

```python
"auth_start": {
    "enabled": True,
    "mode": "device_code",
    "completion": "loopback",     # 显式声明；或写 "loopback": {"enabled": True}
    "label": "网页登录授权",
}
```

```python
import time

class DeviceChannel:
    SUPPORTS_TOKEN_AUTO_REFRESH = True
    SCHEDULED_REFRESH = True

    @staticmethod
    def account_schema():
        return {
            "display_name": "我的设备码渠道",
            "fields": [
                {"key": "access_token", "label": "Access Token",
                 "type": "password", "secret": True, "section": "凭据"},
                {"key": "refresh_token", "label": "Refresh Token",
                 "type": "password", "secret": True, "section": "凭据"},
            ],
            "auth_start": {
                "enabled": True,
                "mode": "device_code",
                "label": "设备码授权",
            },
        }

    @staticmethod
    async def begin_device_flow(p):
        async with p._make_session() as session:
            async with session.post(
                f"{p.base_url.rstrip('/')}/oauth/device/code",
                data={"client_id": "your-client-id"},
                proxy=p.proxy,
            ) as response:
                result = await response.json()
        interval = max(int(result.get("interval", 5)), 1)
        return {
            "task_type": "device_code",
            "auth_url": result["verification_uri"],
            "verification_uri": result["verification_uri"],
            "user_code": result["user_code"],
            "expires_in": int(result.get("expires_in", 900)),
            "interval": interval,
            "poll_params": {"device_code": result["device_code"]},
        }

    @staticmethod
    async def poll_device_flow(p, poll_params):
        async with p._make_session() as session:
            async with session.post(
                f"{p.base_url.rstrip('/')}/oauth/token",
                data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                      "device_code": poll_params["device_code"]},
                proxy=p.proxy,
            ) as response:
                result = await response.json()
        if result.get("error") in ("authorization_pending", "slow_down"):
            return {"status": "pending"}
        if result.get("error"):
            return {"status": "error", "error": result["error"]}
        return {
            "status": "authorized",
            "account_data": {
                "access_token": result["access_token"],
                "refresh_token": result.get("refresh_token", ""),
            },
        }
```

完整可运行骨架见「可用样例」里的 `DeviceAuthChannel`。

### 本机回调（上游只认 127.0.0.1 那类桌面客户端 OAuth）

华为 CodeArts 这类上游不收 `redirect_uri`，只收一个 `port` 参数、登录后自己回调
`http://127.0.0.1:{port}/oauth/callback`。本服务在根路径提供了这个落点（`GET /oauth/callback`），
spec 只需声明 `handle_loopback_callback(p, params, poll_params)` 认领回调：

```python
@staticmethod
async def begin_device_flow(p, auth_context=None):
    # port 用框架给的（浏览器 origin 的端口），本机部署时回调能命中服务端
    port = (auth_context or {}).get("port") or 0
    return {
        "task_type": "device_code", "auth_url": "...",
        "user_code": "",              # 没有真验证码就留空，别塞说明文字
        "interval": 3, "expires_in": 300,
        "poll_params": {"ticket_id": "...", "secret": "...", "port": port},
    }

@staticmethod
async def handle_loopback_callback(p, params, poll_params, callback_url=""):
    """认领框架 /oauth/callback 的请求。返回 None = 不是我的回调，框架试下一个会话。

    ``callback_url`` 是手工补投时的整条地址（含 fragment），按签名可选接收；浏览器直打
    的真实回调只有 query 参数、``callback_url`` 为空串。需要 token 在 fragment 里的
    上游自己用 ``urlsplit(callback_url)`` 解析。
    """
    if params.get("code"):
        return {"status": "authorized", "account_data": {}}   # 换码完成
    if params.get("secret") and params.get("redirect"):
        return {"status": "claimed",
                "poll_params": {"secret": params["secret"]},  # 回填进 state，下一轮轮询生效
                "redirect": params["redirect"]}               # 框架 307 到这里
    return None
```

这是 best-effort 通道：跨机部署时浏览器回调打不到服务端，授权照常由 `poll_device_flow`
轮询完成，spec 两头都要能跑。回调地址、`state`、会话认领全部是框架的事，spec 只判断
「这个回调是不是我的」并给出处理结果。

**跨机也能用回调：手工补投。** 浏览器最终会停在 `http://127.0.0.1:{port}/oauth/callback?code=…`
这个打不开的地址上，但**地址栏里的参数是完整的**。管理端授权区有一个输入框，把整条地址
粘回来即可——服务端走 `POST /admin/providers/{id}/accounts/auth/replay`，用与
`GET /oauth/callback` **完全相同**的认领逻辑补投那次回调。spec 侧不需要为此写任何额外代码，
`handle_loopback_callback` 一份实现同时服务两条入口。宽松接受完整 URL、`?code=…`、
裸 `code=…` 三种粘贴形态。

**非 URL 载荷（让用户从控制台复制凭证）。** 有些上游（如 AutoClaw 海外区）没有可编程的
OAuth 接口，只能让用户在官网登录后在浏览器控制台复制凭证块。这种粘贴不是 URL，管理端
按 `{`/`[` 开头识别为 JSON 载荷：**跳过 query 解析**（JSON 里的 base64 `=` 会被 `parse_qs`
切碎成垃圾键），以空 `params` + 原文 `callback_url` 交给钩子自行识别。spec 侧在
`handle_loopback_callback` 里读 `callback_url` 原文即可（裸 token、JS 对象字面量等非 JSON
形态也只能从这里拿到）；别忘了同时把 `poll_params` 里的会话标记写成独立值（如
`login="credential"`），这样「本会话在等什么」一目了然，不必靠粘贴内容猜。

---

## 场景 G：回调式 OAuth

**什么时候用：** 用户点击按钮后跳到上游授权页，上游再回调服务端。

这两个钩子没有 `p`，因为启动和 callback 阶段还没有渠道实例：

```python
class CallbackChannel:
    @staticmethod
    def account_schema():
        return {
            "fields": [{"key": "access_token", "label": "Token",
                        "type": "password", "secret": True}],
            "auth_start": {"enabled": True, "mode": "oauth_callback",
                            "label": "网页登录授权"},
        }

    @staticmethod
    def build_auth_start(name, account, redirect_uri, state, cfg, auth_context=None):
        # redirect_uri 由框架用「管理员浏览器的 origin + 框架回调路径」拼好传入：
        # 反代、容器、多域名访问下都保证是浏览器可达的地址。渠道只消费、不自拼；
        # auth_context 里另有 origin/host/port 等字段（见场景 F 的上下文表）。
        return {
            "auth_url": (
                "https://login.example.com/authorize?client_id=my-client"
                f"&redirect_uri={redirect_uri}&state={state}"
            )
        }

    @staticmethod
    async def handle_auth_callback(name, params, state_data, cfg):
        # 用 params['code'] 换 token；返回的 key 会合并到账号配置。
        # 换 token 时 redirect_uri 必须与登录时一致——原样取 state_data['redirect_uri']。
        code = params.get("code")
        if not code:
            return {"ok": False, "error": "缺少授权 code"}
        return {"ok": True, "account_data": {"access_token": code}}
```

真实项目中请使用 URL 编码工具，不要直接拼接未经编码的 query 参数。注意：`auth_start.mode`
现在也是后端的分发依据（`oauth_callback` 只走这两个钩子，`device_code` 只走设备码流，
两者都没声明才按「先试回调、501 回退设备码」的老方式探测）。

---

## 场景 H：刷新 token

**什么时候用：** 账号已有 `refresh_token`，access token 会过期。

只实现 `refresh_auth`，并在类上写 `SCHEDULED_REFRESH = True`，账号页「刷新授权」按钮和**账号动作调度器**的 `refresh_auth` 动作（默认每天 10:00，时刻/频率改 spec 声明的 `account_actions` 里的 `refresh_auth` 条目）都会调用它：

```python
class RefreshableChannel:
    SCHEDULED_REFRESH = True
    SUPPORTS_TOKEN_AUTO_REFRESH = True

    @staticmethod
    async def refresh_auth(p, account, cfg):
        refresh_token = account.get("refresh_token")
        if not refresh_token:
            raise ValueError("缺少 refresh_token，请重新授权")
        async with p._make_session() as session:
            async with session.post(
                f"{p.base_url.rstrip('/')}/oauth/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                proxy=p.proxy,
            ) as response:
                result = await response.json()
        return {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token", refresh_token),
        }
```

返回字典中的字段会窄写回该账号；不要自己改整份渠道配置。

> `refresh_auth` 是「刷新授权」**复合入口**：换 token 的同时**顺带**签到/额度等周期动作（与 `perform_action` 同实现，幂等），调度默认每天 10:00。每日签到、额度同步**另有独立动作**（`perform_action` 的 checkin / sync_quota）与独立的「签到」按钮——按 spec 是否在 `account_actions` 声明决定显隐（`checkin` 默认 `enabled=False` 仅手动，每日签到已由 `refresh_auth` 带上，要独立定时签到把 `default.enabled` 改 `True`）。

---

## 场景 I：账密登录和非标聊天

**什么时候用：** 上游没有 API key 登录，而是账号密码登录网页、换 ticket/Cookie，或者聊天 SSE 需要完整自定义处理。

这时才需要 `init_auth`、`stream_chat`、`non_stream_chat` 等较大钩子。认证状态挂在 `p` 上，持久化登录态可用注入的 `JdbcClient.redis`：

```python
class PasswordChannel:
    SUPPORTS_TOKEN_AUTO_REFRESH = True

    @staticmethod
    async def init_auth(p, is_check=False):
        if not getattr(p, "_token", None):
            cached = await JdbcClient.redis.get(f"{p.redis_prefix}:token")
            p._token = cached or await login_and_get_token(p.username, p.password)
            await JdbcClient.redis.set(f"{p.redis_prefix}:token", p._token, ex=86400)
        if is_check:
            return await check_token(p._token)
        return True

    @staticmethod
    async def fetch_models(p):
        await PasswordChannel.init_auth(p)
        # 返回至少 id；name 可选。
        return [{"id": "my-model", "name": "My Model"}]

    @staticmethod
    async def stream_chat(p, model_id, messages, **kwargs):
        await PasswordChannel.init_auth(p)
        yield {}  # 第一帧表示上游已接通
        async for text in call_upstream_sse(p._token, p.base_url, messages):
            yield {"content": text, "thinking": "", "tool_calls": []}
```

上面的 `login_and_get_token` / `check_token` / `call_upstream_sse` 是示意函数；完整真实实现请直接看可用样例中的 `EaiChatChannel`。不要复制框架已有的错误归一、限流、冻结和代理逻辑；通过 `p._raise_upstream_error()`、`p.send_sse_request()` 等能力复用框架。

---

## 场景 J：请求前拦截

**什么时候用：** 某账号不支持某模型，或渠道有内容 / 消息数量限制，需要在真正发请求前拒绝。

```python
class RestrictedChannel:
    @staticmethod
    async def init_auth(p, is_check=False):
        return True

    @staticmethod
    def check_message(p, model_id, messages):
        if model_id.startswith("vision-") and not p.username.startswith("vision-"):
            return False
        if len(messages) > 50:
            return False
        return True
```

返回 `False` 会让该候选被拒绝；不要把它当作上游错误重试逻辑。

---

## 场景 K：上游不认 function calling（文本转工具调用）

**什么时候用：** 上游没有 OpenAI `tools` 字段，模型只能把工具调用当正文文本吐出来。你需要：把工具说明塞进 system、解析模型吐的文本工具调用、把历史轮的 `tool_calls`/`tool_result` 反向序列化回 content 让上下文自洽。

**好消息：文本→工具调用的解析不用你写。** 框架已经内置，且流式 / 非流式同源：

- **流式**：请求带 `tools` 时，`BaseProvider.chat` 自动缓冲 `content`，把完整工具块切成标准 `tool_calls` delta 下发、工具文本不下发，半截块留 buffer 等补全。spec 只要正常 `yield {"content": ...}` 即可。
- **非流式**：spec 在 `non_stream_chat` 末尾对累积正文调一次 `p.parse_tool_calls(full_content)` 兜底即可。

三种文本围栏都认（流式非流式同源），选一种教给模型即可：

| `format_type` | 模型该吐的形态 | 什么时候选 |
|---|---|---|
| `"xml"`（默认） | ` <function=name><parameter=k>v</parameter></function>` | 旧渠道、已适配 XML 的场景 |
| `"json"` | ` ```json\n{"name":..,"arguments":{..}}\n``` ` | 上游 system 噪音大，围栏边界要更硬 |
| `"hermes"` | ` <tool_call>{"name":..,"arguments":{..}}</tool_call>` | 上游是 Qwen-Instruct / Mistral / Hermes 等 open-weight 模型（这是它们的原生训练格式，指令遵循最好） |

模型吐的 JSON 就算缺引号、带尾随逗号、被流截断，也会被 `json_repair` 容错还原——你不用管上游 JSON 有多脏。

`p.build_tools_prompt` / `p.prepare_provider_messages` / `p.parse_tool_calls` 都是挂在渠道实例上的方法（继承自框架基类，不是钩子），你在 `stream_chat`/`non_stream_chat` 里直接调；`render_tool_call_json` 等历史渲染函数已注入命名空间。

### 最小写法

```python
import json

class TextToolChannel:
    @staticmethod
    async def init_auth(p, is_check=False):
        return bool(p.api_key and p.base_url)

    @staticmethod
    async def stream_chat(p, model_id, messages, **kwargs):
        # 1) 工具说明塞 system：xml / json / hermes 任选（见上表）。
        #    上游是 Qwen-Instruct / Mistral 等 open-weight 模型时优先 "hermes"。
        tools = kwargs.get("tools") or []
        tools_prompt = p.build_tools_prompt(tools, format_type="json") if tools else ""
        msgs = p.prepare_provider_messages(messages, tools_prompt)
        # 2) 发请求、yield 帧。模型把工具调用当正文吐也没关系——
        #    请求带 tools 时框架自动从 content 里切出 tool_calls delta。
        async for frame in _call_upstream_stream(p, model_id, msgs, kwargs):
            yield frame

    @staticmethod
    async def non_stream_chat(p, model_id, messages, **kwargs) -> dict:
        tools = kwargs.get("tools") or []
        tools_prompt = p.build_tools_prompt(tools, format_type="json") if tools else ""
        msgs = p.prepare_provider_messages(messages, tools_prompt)
        full_content, full_tool_calls = "", []
        async for frame in _call_upstream_stream(p, model_id, msgs, kwargs):
            full_content += frame.get("content") or ""
            full_tool_calls.extend(frame.get("tool_calls") or [])
        # 3) 非流式兜底：从正文里抠文本形态的工具调用（与流式出口同源）。
        cleaned, parsed = p.parse_tool_calls(full_content)
        if parsed:
            full_tool_calls.extend(parsed)
        return p.build_openai_response(
            generate_completion_id(), model_id, cleaned,
            tool_calls=full_tool_calls or None,
        )
```

> 上游认 OpenAI `tools` 字段时（如 eaichat），**不要**走 prompt 注入——直接把 `tools` 原样塞请求体，让上游做原生 function calling；文本形态解析只是模型违规时的兜底，框架照常兜。

### 历史轮反向序列化（仅 prompt 注入型需要）

上游不认 function calling 时，历史 assistant 轮的 `tool_calls` 和 `role=tool` 结果要拍回 content，模型才认得出自己上一轮的调用。在拼 `msgs` 前过一遍：

```python
    @staticmethod
    def _normalize_history(messages, tools_prompt=""):
        normalized = []
        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                tc_json = "".join(
                    render_tool_call_json(tc) for tc in msg["tool_calls"]
                    if isinstance(tc, dict) and (tc.get("function") or {}).get("name")
                )
                new_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                new_msg["content"] = (msg.get("content") or "") + tc_json
                normalized.append(new_msg)
            elif role == "tool":
                normalized.append({"role": "user", "content": render_tool_result_json(
                    msg.get("tool_call_id") or "", msg.get("name") or "",
                    str(msg.get("content") or ""))})
            else:
                normalized.append(msg)
        return normalized  # 再交给 p.prepare_provider_messages(normalized, tools_prompt)
```

`render_tool_call_json` / `render_tool_result_json`（JSON 形态）和 `render_tool_call_xml` / `render_tool_result_xml`（XML 形态）已注入，选与 `build_tools_prompt` 同形态的一组即可。解析器覆盖：

| 模型吐的形态 | 例子 |
|---|---|
| JSON 代码块 | ` ```json\n{"name":"get_weather","arguments":{"city":"北京"}}\n``` ` |
| Hermes 围栏 | `<tool_call>{"name":"get_weather","arguments":{"city":"北京"}}</tool_call>` |
| 裸 JSON（漏写围栏） | ` {"name":"file_read","arguments":{"path":"main.py"}} ` |
| XML 标准块 | ` <function=get_weather><parameter=city>北京</parameter></function>` |
| GLM 畸形标签 | `<function name>` / `function=name>` / 孤儿 `</invoke>` 收尾 |
| 脏 JSON | 缺引号 `{name: "x"}` / 尾随逗号 / 被流截断——`json_repair` 兜底还原 |

普通正文（无 `name` 键、无闭合标签、无围栏）走快速短路，零解析成本原样返回。

---

## 场景 L：上游返回动态接入地址

**什么时候用：** 上游的接入地址不是用户在渠道配置里填的那个固定域名，而是登录后上游下发一个动态 endpoint（GitHub Copilot 的 `copilot_internal/user.endpoints.api`、企业租户的动态网关都是这个形态）。这时所有出站路径——聊天、模型列表、图片/视频/语音——都要落到那个动态地址上。

`base_url` 钩子比 `build_url` 更靠底层：`_url` / `_chat_url` / `_image_url` / `_video_url` / `_speech_url` 和 `fetch_models` 里读到的 `p.base_url` 全部经过它，一个钩子覆盖所有路径。

```python
class DynamicBaseChannel:
    # 动态地址通常不是表单字段，而是登录后存在账号里的运行时字段。
    ACCOUNT_FIELDS = ("dynamic_api_base",)

    SUPPORTS_TOKEN_AUTO_REFRESH = True

    @staticmethod
    def base_url(p):
        # 先用登录时拿到的动态地址；没有就回落用户填的渠道地址。
        # 别在这里写死上游官方域名——那是 fallback 的事，地址由用户填。
        return getattr(p, "dynamic_api_base", "") or p._channel.get("base_url", "") or ""

    @staticmethod
    async def init_auth(p, is_check=False):
        return bool(p.dynamic_api_base or p._channel.get("base_url"))

    @staticmethod
    def headers(p, base, kwargs):
        base["Authorization"] = f"Bearer {getattr(p, 'access_token', '')}"
        return base
```

只在还没拿到动态地址时 `base_url(p)` 返回空串（或 `None`），框架会回落到渠道配置里用户填的地址，登录拿到后再下发。**不要**在 `base_url` 钩子里发网络请求——它每次出站都会被调用，做 IO 会把请求放大。

---

## 场景 M：运行时刷新 token 要落库

**什么时候用：** access_token 在请求途中过期，spec 自己刷新了新 token，想把它写回账号配置，进程重启后还在。注意区分两个时机：

- **定时 / 账号页「刷新授权」按钮** → 写 `refresh_auth` 钩子，返回字典，admin 统一落库，spec 不用自己写。
- **请求途中自愈刷新**（401 重试、token 临过期自动换）→ 走 `await p.persist_account_fields({...})`，框架按 `(渠道, username)` 窄写单账号行 + 广播 + 同步池内同名账号的其它实例。

```python
class AutoRefreshChannel:
    SUPPORTS_TOKEN_AUTO_REFRESH = True
    SCHEDULED_REFRESH = True
    ACCOUNT_FIELDS = ("access_token", "refresh_token", "access_token_expires_at_ms")

    @staticmethod
    async def refresh_auth(p, account, cfg):
        # 定时/手动刷新走这里：返回字典，admin 落库，spec 不用自己写。
        new = await _do_refresh(p, account["refresh_token"])
        return new  # {access_token, refresh_token, access_token_expires_at_ms}

    @staticmethod
    async def stream_chat(p, model_id, messages, **kwargs):
        await _ensure_token(p)  # 过期就刷新，拿到新 token
        # 关键：运行时刷新出的 token 要落库，否则重启就丢。
        await p.persist_account_fields({
            "access_token": p.access_token,
            "refresh_token": p.refresh_token,
            "access_token_expires_at_ms": p.access_token_expires_at_ms,
        })
        async for chunk in CustomProvider._do_stream_chat(p, model_id, messages, **kwargs):
            yield chunk
```

`persist_account_fields` 只写传入的字段，不动账号其它字段、不重写整份渠道配置，多账号渠道也不会误伤兄弟账号。

---

## 场景 N：步骤认证（多步交互式登录）

**什么时候用：** 上游登录是多步交互——手机号 → 收短信码 → 邮箱 → 收邮箱码…… 既不是一次设备码轮询，也不是一次 OAuth 回调。每提交一步，服务端立刻执行该步动作并返回下一步。

与设备码的根本区别是**同步交互**：不需要中央扫描器轮询。`account_auth_states` 表里 `task_type='steps'`，扫描器查询天然忽略它，`delete_expired_account_auth_states` 照常清理过期行。spec 只声明两个钩子，框架负责状态存储 + 通用步骤 UI 渲染 + 提交分发。

### 钩子

```python
async def begin_step_auth(p, auth_context=None) -> dict
async def submit_step_auth(p, step_id, values, step_state, action=None, auth_context=None) -> dict
```

`begin_step_auth` 返回第一步 + 框架不解释的 opaque 状态（每步原样回传）。**渠道不需要交互步骤时返回 `exit`**：

```python
{
    "task_type": "steps",
    "step": <StepDescriptor>,     # 第一步
    "step_state": {"internal": "..."},   # 框架不解释，每步原样回传
    "expires_in": 900,            # 可选
}

# 渠道纯手填、无需任何交互步骤 → 直接进账号表单：
{
    "task_type": "steps",
    "exit": True,                 # 框架跳过授权阶段，直接显示账号表单
    "account_data": {...},        # 可选：预填账号字段
}
```

`submit_step_auth` 按签名传参（老写法少声明尾参也能跑，框架按签名探测，绝不 call-and-retry）。返回四选一：

| `status` | 含义 | 前端 |
|---|---|---|
| `"next"` | 进入下一步 | 换渲染下一步（必须附 `step` + `step_state`） |
| `"pending"` | 不推进，原地提示（如「验证码已发送」） | 显示 `message`，保留当前步 |
| `"authorized"` | 完成 | 把 `account_data` 回填进账号表单，用户核对后**手动点保存**才建账号（框架不自动落库） |
| `"error"` | 失败 | 显示 `error`（400） |

`StepDescriptor` 框架只透传、不解释，`fields` 复用 `AccountSchemaField` 形态：

```python
{
    "id": "sms_code",
    "title": "输入短信验证码",
    "description": "...",            # 可选
    "hint": "...",                   # 可选，常驻提示
    "fields": [
        {"key": "code", "label": "验证码", "type": "code",
         "required": True, "placeholder": "6 位数字", "span": 1}
    ],
    "submit_label": "下一步",         # 可选，默认「下一步」
    "actions": [{"id": "resend", "label": "重新发送"}]   # 可选，走 submit 的 action 参数
}
```

字段 `type` 复用 `AccountSchemaFieldType`；`tel`/`email`/`code` 是文本输入变体，仅影响 `inputMode`/`autoComplete`，渲染仍走同一套。

### 声明

`account_schema()` 里把 `auth_start.mode` 写成 `"steps"`，`add_methods` 加 `"steps"`：

```python
@staticmethod
def account_schema():
    return {
        "fields": [
            {"key": "username", "label": "用户名", "type": "text", "required": True},
            {"key": "password", "label": "API Key", "type": "password", "required": True},
        ],
        "auth_start": {"enabled": True, "mode": "steps", "label": "多步登录"},
        "add_methods": ["manual_form", "steps"],
    }
```

### 样例：手机号 → 短信码 → 完成

```python
class StepAuthChannel:
    @staticmethod
    async def begin_step_auth(p, auth_context=None):
        return {
            "task_type": "steps",
            "step": {
                "id": "phone",
                "title": "输入手机号",
                "fields": [{"key": "phone", "label": "手机号", "type": "tel", "required": True}],
                "actions": [],
            },
            "step_state": {"ticket": await _create_ticket()},
            "expires_in": 600,
        }

    @staticmethod
    async def submit_step_auth(p, step_id, values, step_state, action=None, auth_context=None):
        ticket = step_state["ticket"]
        if step_id == "phone":
            if action == "resend":
                return {"status": "pending", "message": "验证码已重新发送"}
            await _send_sms(ticket, values["phone"])
            return {
                "status": "next",
                "step": {
                    "id": "sms_code",
                    "title": "输入短信验证码",
                    "fields": [{"key": "code", "label": "验证码", "type": "code", "required": True}],
                    "actions": [{"id": "resend", "label": "重新发送"}],
                },
                "step_state": {"ticket": ticket, "phone": values["phone"]},
            }
        if step_id == "sms_code":
            token = await _verify_sms(ticket, values["code"])
            if not token:
                return {"status": "error", "error": "验证码错误"}
            return {"status": "authorized",
                    "account_data": {"username": step_state["phone"], "password": token}}
        return {"status": "error", "error": f"未知步骤 {step_id}"}
```

**关键不变式：**

- 步骤认证是**逐步收集数据**的向导：进行中只显示步骤面板（这一步要填什么），**不显示账号创建表单**——步骤与「回填创建」是两件事。
- `authorized` **不自动落库**：框架把 `account_data` 回填进账号表单，用户核对后手动点「保存」才建账号。spec 只返回 `account_data`，不要自己写库。
- 钩子签名的可选 `auth_context` 由框架按签名探测转发，与 `begin_device_flow` 同源；`action` 同理（老 spec 不声明 `action` 参数也照常工作）。
- 页面刷新后，`/auth/status` 返回当前 `step` + `step_state`，前端据此恢复 UI（不需要前端存 step_state，服务端持久化）。
- **设备码 / 跳转登录已被框架包装成「步骤」**：存量 spec 声明 `begin_device_flow`/`build_account_auth_start` 后零改动——框架把它们包成带 `poll` 的异步步骤（显示 user_code / 授权地址，后台照旧轮询），前端只认步骤一种形态。

### 条件表达式（字段显隐 / 动态选项 / 动态必填）

字段和步骤可声明**表达式**（JS 语法子集），前端实时求值——不用写任何 UI 代码就能做「选了 A 才显示 B」：

| 属性 | 作用 | 示例 |
|---|---|---|
| `visible_when` | 为假则隐藏该字段（**提交时一并剔除**，不会写入账号） | `"values.method == 'sms'"` |
| `required_when` | 为真则该字段必填（覆盖静态 `required`） | `"values.phone.length == 11"` |
| `disabled_when` | 为真则禁用该字段 | `"values.locked == true"` |
| `options_when` | 求值结果为数组时替换静态 `options`（动态选项） | `"values.kind == 'a' ? [{'value':'x','label':'X'}] : []"` |
| `skip_if`（步骤级） | 为真则跳过该步 | `"values.method != 'sms'"` |

```python
{
    "key": "region", "label": "区域", "type": "select",
    "options": [{"label": "国内", "value": "cn"}, {"label": "海外", "value": "os"}],
},
{
    "key": "cn_phone", "label": "手机号", "type": "tel",
    "visible_when": "values.region == 'cn'",     # 只在选「国内」时出现
    "required_when": "values.region == 'cn'",    # 出现时必填
},
```

可用 `values.<字段key>` 读同表单/同步骤的其它字段，`state.<key>` 读步骤间 opaque 状态。

**支持的语法子集**（前端求值器手写、**不用 `eval`**，越界即返回 undefined 并按「可见」兜底）：

- 字面量：数字、单/双引号字符串、`true`/`false`/`null`/`undefined`
- 比较：`== != === !== > >= < <=`；逻辑：`&& || !`（短路语义同 JS）
- 成员：`.length`、`.includes(x)`、`.startsWith(x)`、`.endsWith(x)`、`.indexOf(x)`、`.toLowerCase()`、`.toUpperCase()`、`.trim()`
- 下标 `a[i]`、三元 `cond ? a : b`、数组/对象字面量、括号分组

> 表达式求值失败（写错语法 / 访问未定义属性）**不抛错**：显隐类按「可见」处理，避免一个写错的表达式把用户永久卡在空步骤里。`values.x` 只在同一步/同表单内可见——跨步骤的数据请放 `step_state` 由 spec 自己管理。

---

## 场景 O：导入/上传凭据文件识别

**什么时候用：** 上游没有可编程的登录接口，用户只能从浏览器导出凭据（cookie / token / HAR 抓包）再导入。添加账号时上传文件，服务端识别出字段自动回填表单，用户确认后走正常 add 流程——**不落库**。

### 通用解析器（不写钩子也能用）

框架内置 `providers/credential_import`，认三种格式：

| 格式 | 识别来源 | 识别出的槽位 |
|---|---|---|
| JSON | 递归按键名找 `access_token`/`token`/`api_key`/`key`/`refresh_token`/`cookie(s)`/`authorization`/`user_name`/`user_email`/`account_id` 等 | access_token / refresh_token / cookie / username / email / account_id / session_id / csrf_token |
| HAR | `log.entries[].request.headers[]`，取命中渠道 `base_url` host 的（或最新一条）请求 | Cookie 头 → cookie；`Authorization` 去 `Bearer ` 前缀 → access_token |
| cookies.txt | Netscape 7 字段制表符格式（`#HttpOnly_` 前缀是真实行不是注释） | 拼 `cookie` 头 |

识别结果按 `account_schema().fields` 的 key 名分到 `fields`（前端可安全回填）和 `extras`（额外字段区）；`notes` 给人读的来源说明。

槽位到表单字段的映射是双向的：`access_token` 槽位匹配 schema 里 `password` / `api_key` / `key` / `access_token` 任一字段名；`refresh_token` 匹配 `refresh_token`；以此类推。同一槽位只填第一个命中的字段（凭据字段单写，避免 `api_key > key > password` 的 init 优先级遮蔽出陈旧值）。

### 声明

`account_schema()` 的 `add_methods` 加 `"import"`：

```python
"add_methods": ["manual_form", "import"],
```

不写钩子时，上传文件走通用解析器。渠道有非标格式时声明 `parse_credential_file` 钩子完全接管：

```python
async def parse_credential_file(p, filename, content_bytes, cfg=None) -> dict
```

返回与通用解析器同形态：`{"fields": {...}, "extras": {...}, "notes": [...]}`。钩子抛错时框架回落通用解析器（标格式用户不受非标渠道影响）。

### 端点

- `POST /admin/providers/{name}/accounts/import-file`（multipart `UploadFile`）
- 大小护栏 5MB；返回 `{ok, format, fields, extras, notes}`，**不落库**
- 前端拿到 `fields` 合并进表单（凭据字段仍只写单一 `schemaPasswordField.key`）、`extras` 进额外字段、`notes` 显示为一行摘要
- 用户确认后走正常 `PUT /accounts/add`

> 这是 admin 侧**第一个** multipart 端点。前端 `importProviderAccountCredentials` 用 FormData + 显式 `Content-Type: undefined`——共享 axios 默认 JSON，axios 1.x 会把 FormData 静默转 JSON（坑见 `@admin-port/api/nodes.ts`）。

### JSON 批量导入（一批账号一次贴）

**什么时候用：** 手里有一批账号（上游批量注册导出、别的系统迁移），要一次全贴进来。渠道**什么都不用写**——`account_schema()["fields"]` 声明了哪些字段，导入就按那些字段名落，所以代码渠道天然支持。

- `POST /admin/providers/{name}/accounts/import-json`，body `{"text": "<json>"}`，**同样不落库**
- 接受的形态：顶层账号数组 `[ {...}, {...} ]`，或 `{"accounts": [...]}` / `data` / `list` / `items` / `result` 包裹
- 每条 dict 的键走与单账号导入**同一套**映射（`credential_import.map_account_entry`）：
  - 键名先归一（camelCase→snake_case、去分隔符、大小写不敏感），schema 的 **key 和 label 都是匹配入口**——`accessToken` / `Access_Token` / 界面上的 `UIN` 标签都认
  - 上游原始叫法也认：`access_token`/`token`/`authorization`（自动去 `Bearer `）→ schema 的 `password`/`api_key`/`key`/`access_token` 任一；`user_name`/`user_email` → `username`。schema 声明但没有槽位的字段（`user_id`/`enterprise_id`/`uin`…）按同名键直取
  - **同一凭据槽位只写一个字段**，避免 `api_key > key > password` 的 init 优先级把刚导入的凭据遮蔽掉
  - 值按字段类型校正：`number`/`checkbox` 留原值，其余一律转字符串——spec 里 `p.uin.strip()` / `p.user_id.startswith(...)` 是常态，JSON 给数字会 `AttributeError`
- 逐条校验并回报（不整批失败）：缺用户名 / 与已有账号重名 / 批内重名 / 缺 `required` 字段 / 条目不是对象 →
  该行标红进不了 `accounts`，其余好行照常可导入；`username` 缺失时按
  `username`→`user_name`→`user_email`→`account_id`→`user_id`→`name` 兜底
- schema 没声明的键**不丢**：与单账号导入把 `extras` 合并回表单同一口径，一并写进账号行
- 返回 `{ok, schema_fields, rows[], accounts[], total, importable, failed}`：`rows` 是预览行
  （凭据已按 `_mask_secret` 脱敏，明文凭据只在顶层 `accounts` 出现一次），`accounts` 是确认后
  原样发回 `POST /accounts/batch-add` 的完整账号行
- 入口在账号 tab「批量添加」弹框的 **JSON 导入** 页签：粘贴 → 解析预览（表格 + 可导入/跳过计数）→
  确认导入。大小护栏 5MB。

---

## 场景 P：账号动作（签到 / 同步额度等周期动作）

**什么时候用：** 上游有「作用于账号但不属于聊天」的动作——每日签到、同步积分/余额、领福利…… 它们要有**独立的结果展示 + 独立的手动按钮**，并能各自配置调度。

**两个按钮的分工**（这是设计的核心，别搞混）：

| 按钮 | 后端 | 作用 | 显隐 |
|---|---|---|---|
| 「刷新授权」（操作列） | `refresh_auth` 钩子 | **复合入口**：换 token 的同时顺带签到/额度（与 `perform_action` 同实现，幂等）。适合"我现在要把这个账号全刷一遍" | 有 `refresh_auth` 钩子就显示 |
| 「签到」（账号行「额度·动作」列） | `perform_action` 的 `checkin` | **只做签到**这一件事，不碰 token/沙箱 | spec 在 `account_actions` 声明了 `checkin` 才显示 |

`refresh_auth` 与 `perform_action` 共用同一套 `_checkin_fields` / `_fetch_points` 之类的实现，重复调用是幂等的（当日已签则跳过），所以复合入口 + 独立按钮并存不会打乱上游。

### 声明

`account_schema()` 里加 `account_actions`（每个动作独立：独立调度、独立按钮、独立状态，互不牵连）：

```python
def account_schema():
    return {
        "fields": [...],
        "auth_start": {...},
        "account_actions": [
            {"id": "checkin", "label": "每日签到", "manual": True,
             "display": {"format": "badge", "label": "签到",
                         "badge_field": "last_checkin",
                         "status_field": "last_checkin_status"},
             # refresh_auth 复合入口已每天带一次签到（幂等）；
             # checkin 只做「签到」按钮、默认不单独调度，避免重复打上游。
             "default": {"enabled": False, "frequency_unit": "daily", "frequency_value": 1}},
            {"id": "sync_quota", "label": "同步积分", "manual": True,
             "display": {"format": "value", "label": "积分", "value_field": "quota_text"},
             "default": {"enabled": True, "frequency_unit": "hours", "frequency_value": 6}},
        ],
    }
```

- `display.format`：`value` = 账号行直接显示字段值（额度）；`badge` = 显示为徽标（按 `status_field` 的 `日期|状态|+金额` 着色：ok/already 绿、fail/error 红）；`none`（缺省）= 只进「账号动作」面板，不出账号行。
- `default`：**调度配置的真相源就在渠道代码里**（enabled/频率/daily_time/账号范围）。管理端「账号动作」面板只读展示解析后的生效配置 + 提供「立即执行」，**不提供编辑**——要改频率/时刻/范围就改这里的 `default` 重新保存源码。
- `manual`：账号行是否出手动执行按钮（默认 True）。

### 实现

```python
@staticmethod
async def perform_action(p, action_id, account, cfg):
    """执行一个账号动作，返回要窄写回账号的字段（admin 统一落库）。"""
    if action_id == "sync_quota":
        return {"quota_text": f"积分 {await _fetch_points(p):,}"}
    if action_id != "checkin":
        raise ValueError(f"未知动作 {action_id}")
    # 签到：只回「状态 + 拿到了多少」，徽标展示串由框架统一格式化（见下）
    ok, reward = await _do_checkin(p)          # 你自己的上游调用
    if not ok:
        return {"_checkin_status": "failed", "_checkin_detail": "上游 500"}
    return {"_checkin_status": "ok", "_checkin_detail": f"+{reward}"}


@staticmethod
async def checkin_condition(p, action_id, account, cfg):
    """（可选）签到前置条件门：框架在调 perform_action 前先问一句要不要签。"""
    if action_id != "checkin":
        return {"ok": True}
    if _already_signed_today(account):          # 读账号行 / JdbcClient.redis 都行
        return {"ok": False, "reason": "今日已签到"}
    return {"ok": True}
```

- 返回字典窄写回该账号（与 `refresh_auth` 同语义）；**不要**在钩子里自己 `persist_account_fields`——admin 随后会用它自己的旧快照 + 返回值整行回写，中间窄写的字段会被冲掉。
- 动作**执行失败**（如签到一个都没成）：徽标字段照写（前端能看到「签到失败」），再带一个 `_error` 键——admin 把它记为「本次执行失败」（悬浮可见），**不落库**该键。
- **签到类动作改用结构化状态返回**（框架弹出、不落库原样键）：

  | 返回键 | 取值 | 含义 |
  | --- | --- | --- |
  | `_checkin_status` | `ok` | **真签到了**，`_checkin_detail` = 拿到了多少（如 `+80`）→ **框架解冻该账号** |
  | | `failed` | 签到失败，`_checkin_detail` = 失败原因 → 不解冻 |
  | | `skipped` | 没签（今日已签/无活动/条件不满足），`_checkin_detail` = 跳过原因 → **不解冻** |

  框架据此把状态格式化进本动作 `display.badge_field` / `status_field` 声明的字段（缺省
  `last_checkin` / `last_checkin_status`），并把「最近一次签到状态 + 拿到了多少/原因 + 时间」
  统一记进账号动作结果供页面悬浮展示——**spec 不再自己拼「签到成功 +80」这类展示串**。
  「跳过」与「成功」必须分开：只有 `ok` 能证明账号拿到了新额度，才配解冻；把
  「今日已签/无活动」也当成功解冻，等于给一个额度耗尽的号放行流量。
- **解冻只认 `ok`**：额度不足类冻结（冻结周期设「今天/本周/本月」）在签到成功那一刻解除，
  不用干等到期；走的是与「测试成功自动解冻」同一条链路（内存桶 + Redis + 跨实例广播）。
- **条件门 `checkin_condition`**（可选钩子，不声明就没有门、直接执行）：逻辑归 spec，可读
  账号行、也可用注入的 `JdbcClient.redis` 记跨重启的当日标记。返回 `{"ok": False, "reason": ...}`
  时框架**不调 perform_action**、直接记 `skipped`（同样不解冻）——省掉重复的上游请求。
- 幂等：有了条件门就不必再靠 `status_field` 自查「当日已签」；两者可并存（门做廉价本地判断，
  `perform_action` 内的上游复核兜最终一致性）。调度器重启当天会补跑一次，幂等兜住。

### 调度与手动触发

- **自动**：账号动作调度器每 60s 扫描到点的 `(渠道, 动作)`，候选账号入队、worker 串行消费。`daily` 按**每日时刻**执行——`default.daily_time`（"HH:MM"，缺省 "10:00"）决定当天几点跑，当天到点才跑、重启当天补跑一次（spec 的当日幂等兜住）；`minutes`/`hours` 按间隔滚动。手动「立即执行」**不**推迟下一次自动调度。
- **手动**：账号行每动作一个按钮（只跑本动作）；渠道「账号动作」面板每动作一个「立即执行」（按 `default.accounts` 范围异步入队）。
- **端点**：`POST /admin/providers/{name}/accounts/{username}/actions/{action_id}`（单账号）、`POST /admin/providers/{name}/actions/{action_id}/run-all`（批量入队）。
- `refresh_auth` 是框架**自动合成**的动作：声明了 `refresh_auth` 钩子的渠道自动有它（默认 `daily 10:00`）；`SCHEDULED_REFRESH = True` 时默认纳入自动调度，否则只留手动按钮。**想改它的默认频率/时刻**，直接在 `account_actions` 里声明一个 `id="refresh_auth"` 的条目覆盖即可。

---

## 场景 Q：浏览器操作（驱动 CDP）与渠道定时任务

**什么时候用：** 上游不是纯 HTTP 能搞定的——反爬挑战头要页面内 JS 求解（如 duck.ai 的 `X-Vqd-Hash-1`）、验证码必须页面内跑 SDK（阿里云无痕）、登录要真实 SSO 页面交互。这些操作由 spec **直接驱动 CDP 浏览器**完成（原「站点服务」的能力已并入本框架，站点服务已删除）。

### 浏览器操作门（`p` 上常驻，无需声明）

框架给每个渠道实例常驻挂了浏览器操作方法（薄委托 `SiteServer`，与 CDP 桥共用连接）：

| 方法 | 说明 |
|---|---|
| `await p.run_js(client_id, code, *, tab_id="")` | 在浏览器 tab 里执行 JS，返回值 |
| `await p.open_tab(client_id, url="", *, active=False)` | 开 tab（默认记为常驻 tab），返回 session_key |
| `await p.find_tab(client_id, url_contains)` | 找 URL 含关键字的已有 tab |
| `await p.set_default_tab(client_id, session_key)` | 把已有 tab 认作常驻 tab |
| `await p.navigate(client_id, url, *, tab_id="")` | 跳转 |
| `await p.capture_start(client_id, url_pattern="")` | 开始监听网络，返回 capture_id |
| `await p.wait_capture(client_id, capture_id, *, timeout=30)` | 等捕获帧 |
| `await p.capture_stop(client_id, capture_id)` | 停止监听 |
| `await p.list_clients()` | 列**渠道归属者**名下的在线 CDP 浏览器 |

- `client_id` 是「设备工具 → 浏览器」资源的 id；`p.list_clients()` 按**渠道 creator**（`owner_user_id`）收窄，操作不了别人的浏览器。
- 同一方法里**不能同时挂两个 listen**（扩展按 tabId 只维持一份抓包）——要监听多个接口就 `capture_start → wait_capture → capture_start → wait_capture` 依次来。
- 渠道自己的常驻 tab / 浏览器池状态放 **spec 模块级 dict**（namespace 进程内单例，跨账号存活，源码一改才失效；先例见 `_ASK_LOCKS` 的用法）。

### 渠道定时任务（`SCHEDULED_TASKS` 类属性）

原站点 `register_task` 的移植。spec 类上声明：

```python
SCHEDULED_TASKS = [
    {"id": "keepalive", "frequency_unit": "minutes", "frequency_value": 10},
    # {"id": "sync", "frequency_unit": "daily", "daily_time": "10:30", "enabled": False},
]

@staticmethod
async def keepalive(p):
    """渠道级定时任务：p 是渠道级单例实例（能 p.run_js 驱动浏览器）。"""
    ...
```

- 调度词汇与账号动作一致：`frequency_unit` = `daily`（带 `daily_time` "HH:MM"，缺省 10:00）/ `minutes` / `hours`（带正整数 `frequency_value`）；`enabled` 缺省 True。
- 由账号动作调度器统一驱动（同一循环，独立于账号动作——纯保活渠道没有账号也会跑）；重启后首轮即跑（替代原站点开机拉起池的行为）。
- 每个 `id` 对应 spec 里一个同名 `@staticmethod def id(p)`；**声明了没写方法，加载即报错**（防静默不跑）。
- 打外网的渠道该开关无额外要求；纯保活型渠道（无任何钩子）写 `REQUIRES_BASE_URL = False` 豁免渠道地址必填。

### 站点 → spec 移植对照（原站点服务用户的迁移配方）

| 站点现在 | 渠道 spec 之后 |
|---|---|
| `class X(SiteRuntimeBase)` 独立 code 行 | spec 同一源码里的普通静态方法 |
| `self.run_js(cid, js)`（继承基类） | `await p.run_js(cid, js)`（框架常驻门） |
| `self.register_api("ask", ...)` | `async def _ask(p, ...)` 普通方法，spec 内部直调 |
| `self.register_task("keepalive", minutes=10)` | `SCHEDULED_TASKS=[{"id":"keepalive","frequency_unit":"minutes","frequency_value":10}]` + `@staticmethod def keepalive(p)` |
| 池/常驻 tab 状态 | spec 模块级 dict（namespace 单例，跨账号存活） |
| `login_site_id/login_site_token` 账号字段 | 删；`login_client_id` 保留（账号↔浏览器钉位） |
| 跨渠道借别的渠道的能力 | 该能力上提为 `providers/` 下的共享模块，或经渠道类直调 |

---

## 类级开关（spec 类属性）

类级开关写在 spec 类上，框架复制到适配器。已在前述场景分散出现，这里集中列：

| 开关 | 默认 | 作用 |
|---|---|---|
| `SUPPORTS_TOKEN_AUTO_REFRESH` | False | 账号凭据过期后能否自愈；True 时前端标「已过期（等待自动刷新）」 |
| `SCHEDULED_REFRESH` | False | 自动合成的 `refresh_auth` 动作默认纳入自动调度（需同时写 `refresh_auth`；未声明则只有手动按钮） |
| `SUPPORTS_MULTI_MESSAGES` | False | 是否把多轮 messages 整批发给上游 |
| `NEEDS_STREAM_TOOL_FALLBACK` | False | 流式工具调用降级开关 |
| `TOOLS_AS_PROMPT` | False | 上游不认 OpenAI function calling 时把工具说明进 system（见场景 K） |
| `TOOL_PARSE_IN_CHANNEL` | False | 围栏解析（含「调用名是否在入参 tools 里」校验）由渠道自己做；框架流式/非流式出口不再对 content 二次解析围栏 |
| `TOOLS_PROMPT_FORMAT` | "xml" | 文本工具形态：`xml`/`json`/`hermes` |
| `REQUIRES_BASE_URL` | True | 渠道地址是否必填；不打外网的 spec（Echo、本地 mock）写 False 豁免 |
| `ACCOUNT_FIELDS` | () | 账号字段名清单，挂实例属性；与 `account_schema` 字段求并集 |
| `APPLY_CLIENT_PRESET` | True | 是否套 opencode/claude-code 那套伪装头；上游校验自己 CLI 头的写 False |

> `APPLY_CLIENT_PRESET=False` 是唯一能真正关掉伪装头的开关。在渠道配置里写 `client_preset="none"` **关不掉**——`_effective_client_preset` 会按协议兜到默认 preset（openai → opencode、anthropic → claude-code）。上游一旦校验它自家 CLI 的请求头，伪装头就会被拒，必须用这个开关硬关。

---

## 所有钩子速查

下面是完整名称。上面的场景已经给了最常用的写法；没写的钩子就回落到 `CustomProvider`。

### 认证、账号和授权

| 钩子 | 签名 | 用途 |
|---|---|---|
| `init_auth` | `(p, is_check=False) -> bool` | 首次使用、失效自愈、体检 |
| `check_auth` | `(p) -> bool` | 检查已有登录态 |
| `is_init` | `(p) -> bool` | 判断凭据是否就绪 |
| `health_check` | `(p) -> bool` | 自定义健康检查 |
| `account_schema` | `() -> dict` | 声明前端账号字段和授权入口 |
| `begin_device_flow` | `(p) -> dict` | 启动设备码授权 |
| `poll_device_flow` | `(p, poll_params) -> dict` | 设备码单步轮询 |
| `begin_step_auth` | `(p) -> dict` | 启动步骤认证，返回第一步（见[场景 N](#场景-n步骤认证多步交互式登录)） |
| `submit_step_auth` | `(p, step_id, values, step_state, action=None) -> dict` | 提交步骤认证的一步输入 |
| `build_auth_start` | `(name, account, redirect_uri, state, cfg)` | 生成 OAuth 跳转 |
| `handle_auth_callback` | `(name, params, state_data, cfg)` | 处理 OAuth 回调 |
| `refresh_auth` | `(p, account, cfg) -> dict` | 刷新 token 并返回窄写字段（框架自动合成 `refresh_auth` 动作，见[场景 H](#场景-h刷新-token)） |
| `perform_action` | `(p, action_id, account, cfg) -> dict` | 执行一个账号动作（签到/额度等），返回窄写字段 + 签到类动作的 `_checkin_status`/`_checkin_detail`（见[场景 P](#场景-p账号动作签到同步额度等周期动作)） |
| `checkin_condition` | `(p, action_id, account, cfg) -> {ok, reason}` | （可选）签到前置条件门：`ok=False` 时框架跳过本次签到并记 skipped、不解冻账号（见[场景 P](#场景-p账号动作签到同步额度等周期动作)） |
| `parse_credential_file` | `(p, filename, content_bytes, cfg=None) -> dict` | 自定义凭据文件解析（见[场景 O](#场景-o导入上传凭据文件识别)） |

### 聊天、模型和请求

| 钩子 | 签名 | 用途 |
|---|---|---|
| `fetch_models` | `(p) -> list[dict]` | 自定义模型列表 |
| `stream_chat` | `(p, model_id, messages, **kwargs)` | 自定义流式聊天，`yield` 统一帧 |
| `non_stream_chat` | `(p, model_id, messages, **kwargs) -> dict` | 自定义非流式响应 |
| `headers` | `(p, base_headers, kwargs) -> dict` | 在标准请求头上后处理 |
| `payload` | `(p, endpoint, model_id, messages, stream, kwargs) -> dict` | 后处理请求体 |
| `build_url` | `(p, kwargs) -> str` | 覆盖聊天 URL |
| `base_url` | `(p) -> str \| None` | 覆盖渠道地址，影响全部出站路径（见[场景 L](#场景-l上游返回动态接入地址)） |
| `parse_chunk` | `(p, event_str) -> dict \| None` | 覆盖一帧 SSE 解析 |

### 回调、配额和生命周期

| 钩子 | 签名 | 用途 |
|---|---|---|
| `on_response_headers` | `(p, headers, model_id, ctx)` | 响应头回调 |
| `update_quota` | `(p, headers, model_id)` | 从响应头更新配额 |
| `check_message` | `(p, model_id, messages) -> bool` | 请求前放行判定 |
| `record_message` | `(p, model_id, messages)` | 自定义记账 |
| `on_channel_attached` | `(p)` | 配置注入后初始化派生状态 |
| `clear_conversations` | `(p, max_age_hours) -> int` | 清理上游会话 |
| `close` | `(p)` | 释放自定义资源 |

## `p` 上最常用的东西

| 属性 / 方法 | 含义 |
|---|---|
| `p.base_url` | **管理端渠道配置中用户填写的渠道地址**；不要在 spec 里写死 |
| `p.api_key` | 渠道 / 账号配置派生的 key |
| `p.username` / `p.password` | 当前账号凭据 |
| `p.proxy` / `p.proxy_config_id` | 统一出口代理配置 |
| `p.timeout_seconds` | 请求超时 |
| `p.redis_prefix` | `model-api:{渠道id}:{username}`，用于隔离账号运行态 |
| `p._channel` | 渠道领域对象，可读 `.base_url`、`.protocol`、`.chat_protocols`、`.billing_mode` |
| `p._make_session()` | 创建遵循渠道代理设置的 HTTP session（不要自己 `import aiohttp`） |
| `p.send_sse_request(...)` | 发 SSE 请求，并接入请求留痕 |
| `p.build_openai_response(id, model, content, thinking=, tool_calls=)` | 构造统一 OpenAI 响应；`id` 传 `generate_completion_id()` |
| `generate_completion_id()` | 生成 `chatcmpl-xxx` 响应 id，作为 `build_openai_response` 的第一个参数（已注入，无需 import） |
| `p._raise_upstream_error(error)` | 复用框架错误归一，不要自己复制一份 |
| `p.build_tools_prompt(tools, format_type="xml"\|"json"\|"hermes")` | 生成工具说明 prompt（注入 system 用）；`json` 围栏更硬、`hermes` 对齐 Qwen-Instruct/Mistral 训练格式 |
| `p.prepare_provider_messages(messages, tools_prompt="")` | 把 tools_prompt 作为首条 system 消息拼到 messages 前面 |
| `p.parse_tool_calls(content)` | 文本→工具调用解析，返回 `(去掉工具块后的正文, OpenAI tool_calls 列表)`；与框架流式出口同源 |
| `p.parse_sse_type(event_str)` | 解析一帧 SSE，返回 `(event_type, event_data)`；非标 SSE 渠道常用 |
| `await p.persist_account_fields({...})` | 把运行时刷新出的字段窄写回本账号并同步池内实例（见[场景 M](#场景-m运行时刷新-token-要落库)） |
| `render_tool_call_json` / `render_tool_result_json` | 历史轮 `tool_calls`/`tool_result` 反向序列化（JSON 形态，已注入） |
| `render_tool_call_xml` / `render_tool_result_xml` | 同上（XML 形态，已注入）；选与 `build_tools_prompt` 同形态的一组 |

> `generate_completion_id()` 只是生成一个 OpenAI 风格的响应 ID（形如 `chatcmpl-4f...`）。写 `non_stream_chat` 手工构造响应体时，把它作为 `build_openai_response` 的第一个参数传入即可；不写非流式、或用框架聚合时根本不需要它。

## usage（token 统计）怎么处理

**默认不用管。** 上游响应里没有有效 usage 时，框架会按请求内容和返回内容自动估算，日志不会记 0。

只有当上游**确实返回了真实 token 统计**、你想用它覆盖估算时才需要主动给：

- 流式：额外 `yield {"usage": {"prompt_tokens": 123, "completion_tokens": 456, "total_tokens": 579}}`。
- 非流式：在返回的响应体里放 `response["usage"] = {...}`。

不要再手写 `estimate_usage` / `normalize_usage` 之类的兜底逻辑——它们已不再注入，框架统一负责。

## 流式帧格式

`stream_chat` 每次 `yield` 一个字典：

```python
yield {}  # 可选但推荐：先通知主链路上游已接通
yield {"content": "正文增量", "thinking": "", "tool_calls": []}
yield {"content": "", "thinking": "思考增量", "tool_calls": []}
yield {"content": "", "thinking": "", "tool_calls": [{
    "index": 0,
    "id": "call_1",
    "type": "function",
    "function": {"name": "search", "arguments": {"q": "天气"}},
}]}
```

也可以带 `usage`、`finish_reason`、`done`。`messages` 是 OpenAI 格式；`kwargs` 中常见的有 `tools`、`thinking_enabled`、`auto_search`、`max_tokens` 和请求留痕回调。

## 完整样例在哪里？

最小可跑样例就是目录预设中的 `EchoChannel`——新建代码渠道时它会回填进编辑器，先用它验证「贴代码→出渠道」这条链路是通的。

更完整的认证与协议实现（OAuth 设备码、账密登录换 Cookie、非标 SSE 等组合场景）不随产品分发——按上文各场景与钩子说明组合即可。产品文档站「自定义代码渠道」页另附 `TenantChannel` / `DeviceChannel` / `SseChannel` 等说明性片段（不是完整实现）。

## 常见错误

- **渠道地址留空就保存**：保存会被拒绝（`渠道地址（base_url）不能为空`）。填上地址，或在 spec 上写 `REQUIRES_BASE_URL = False` 明确声明本渠道不打外网。
- **继承 BaseProvider 报错**：删掉继承，写普通类；框架能力通过 `p.xxx` 使用。
- **写了 `stream_chat` 但没有 yield**：流式请求不会产生内容；至少 yield 统一帧。
- **`account_schema` 字段名和 `account_data` 对不上**：设备码授权成功后字段不会正确回填；两边 key 必须一致。
- **把 `base_url` 写在代码里**：管理端填写的地址不会生效；应使用 `p.base_url`。
- **自己 `import aiohttp` 发请求**：会绕开出站代理、连接复用和请求留痕；用 `p._make_session()` / `p.send_sse_request()`。
- **自己拼 usage**：不需要；框架自动估算。要覆盖就 `yield {"usage": {...}}`。
- **复制 `_raise_error_json` / 自己做冻结重试**：会和框架状态机分叉；使用 `p._raise_upstream_error` 和框架提供的方法。
- **每个钩子都重写**：越容易漏掉协议、工具调用、日志和重试；优先只写一个最小钩子。

## 保存、热更新与安全

保存 `code` 后，后端会校验语法、扫描唯一 spec 类并热更新适配器；成功后进程内立即生效，不需要重启。语法错、无 spec、钩子拼错、误继承基类、渠道地址缺失都会返回 HTTP 400，不落库。

代码在服务进程权限下执行，能导入已安装的 Python 包（包括 aiohttp——但如上所述，发请求请走框架）。代码渠道只应由可信管理员编辑；它不是沙箱。
