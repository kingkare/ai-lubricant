# 渠道语义异常与统一冻结收口 — 设计方案

> 状态：待评审。起草 2026-10-20。
> 关联现状梳理：[account-state-storage.md](./account-state-storage.md)

## 0. 要解决的问题

现在渠道报错转成「冻结动作」的唯一路径是：spec 抛 `HTTPException(status_code, detail)` → 网关拿状态码/响应头去匹配渠道 `freeze_policy` 规则表。三个后果：

1. **状态码语义复用过度**——400 既是「参数错」也是「refresh_token 烧了」也是「额度没了」，规则表没法区分。codearts 的死号报 400，规则表里只有 403/429/exception 三类，**落不进任何规则**，只走 60 秒兜底冷却 → 死号永远留在候选里反复白撞（2026-10-19 事故后半段）。
2. **误伤**——WAF 边缘 403（HTML，非凭证问题）被规则「403→冻到今天」命中，好号停摆一整天（现网抓到 1 例）。spec 只能自己在异常文案里区分，网关看不见。
3. **spec 各自造轮子**——额度耗尽/凭证失效/临时故障的重映射在每个 spec 里写一遍（本次要盘点的存量特殊逻辑即此）。

## 1. 核心：语义异常 + 统一收口

spec 抛**带语义的异常**（可携带冻结参数），网关统一收口翻译成冻结动作。三层优先级：

```
① 异常自带显式冻结动作（供应商指定）→ 直接执行，跳过规则表与时长判断
② 异常只有语义 kind → 查渠道 freeze_policy 的 error_kind 规则 → 命中则按其动作
③ 裸 HTTPException（存量 spec）→ 完全走现有 status_code/headers/exception 规则匹配，行为不变
```

**冻结时长归谁**：供应商在异常里显式给了 freeze 参数 → 以此为准，**不再经过渠道策略的时长判断**（含 `refresh_freeze_on_failure` 也不覆盖——显式意图即终态；见 §5 D2）。只给 kind 不给参数 → 时长归渠道策略配。都没有 → 现状不变。

## 2. 异常类（全部继承 HTTPException）

放 `providers/base.py`（先例：`IncompleteStreamError`/`EmptyNonStreamResponseError` 同处），经 `code_loader._build_namespace()` 注入 spec 命名空间——**spec 直接 `raise CredentialInvalid(...)`，不需要 import**。

```python
class ChannelError(HTTPException):
    """渠道语义异常基类。

    继承 HTTPException 是硬约束，不是风格选择：
    - 顶层出口只注册了 @app.exception_handler(HTTPException)（main.py:1521），
      非 HTTPException 抛到顶 = FastAPI 默认 500，客户端拿到错误状态码；
    - 重试分类/失败收口全部 isinstance(error, HTTPException) 后读
      status_code/detail（main.py:2999-3017）；换成裸 Exception 会误走
      mark_account_exception（网络异常）分支。
    所以新增只加语义标注，不改任何既有读法。
    """
    kind: ClassVar[str] = "channel_error"
    #: 类级默认冻结动作（子类给出建议值）；实例可覆盖
    default_freeze: ClassVar[dict | None] = None

    def __init__(self, detail, *, status_code: int | None = None,
                 freeze: dict | bool | None = None, headers=None):
        # freeze=None → 用 kind 级策略；freeze=False → 明确声明"别冻结我"
        # dict 形态即 freeze_policy 规则的动作字段子集：
        #   {"scope": "account|account_model|channel|channel_model",
        #    "period": "seconds|minutes|hours|days|today|permanent|disabled",
        #    "value": int, "model_id": str|None}
        ...
```

语义子类（首批四类 + 一个通用）：

| 类 | kind | 默认建议冻结 | 默认客户端状态码 | 语义 |
|---|---|---|---|---|
| `CredentialInvalid` | `credential_invalid` | account / **permanent** | 401 | 凭证确死（refresh 烧了/签名被拒/token 吊销），只有重新授权能救 |
| `QuotaExhausted` | `quota_exhausted` | account / today | 403 | 上游额度/积分耗尽，重试恢复不了 |
| `UpstreamTransient` | `upstream_transient` | account / 60s | 502 | 上游临时故障：超时、Backend timeout、CDN/WAF 拦截 |
| `RateLimited` | `rate_limited` | account / seconds=retry_after | 429 | 429 限流，可带 retry_after |
| `InvalidRequest` | `invalid_request` | **不冻结**（freeze=False） | 400 | 参数/上下文错，账号是好的 |

要点：

- **默认状态码是语义正确值**，但迁移时必须逐渠道核对现网可见行为——如 codearts 死号现网回 400，直接换 401 就改了客户端契约。处理：构造时可显式传 `status_code=400` 保持兼容，是否统一切到 401 后续翻代码再定（§5-6）。
- `UpstreamTransient` 承接 WAF 场景：spec 分诊出「HTML 边缘拦截」→ 抛它（502/60s），而不是裸 403。这正是 codearts 本轮手工做的事，机制化后各渠道同构。
- 每个类都允许实例覆盖：`raise RateLimited("...", freeze={"scope":"account_model","period":"seconds","value":90})`。

## 3. 网关收口（一处改动，两条路径合流）

### 3.1 抽公共执行器

现状 `apply_freeze_policy`（rate_limiter.py:1525）= 「匹配规则 → 翻译动作 → 执行」。把**执行段**抽成方法，供规则路径与显式路径共用：

```python
def apply_freeze_action(self, username, model_id, action: dict, reason: str,
                        *, respect_refresh_flag: bool = True) -> str:
    """执行一个已规范化的冻结动作 {scope, seconds/permanent/disable, ...}。"""
```

规则表路径（`apply_freeze_policy`）与异常显式路径都收敛到它——冻结写内存桶 + Redis SET + 广播的既有顺序、通知、日志全部只有一份，不出现两套语义。

### 3.2 失败收口按三层优先级分流

`main.py:3001-3017` 现有 `isinstance(error, HTTPException)` 分支之前插一段：

```python
if isinstance(error, ChannelError):
    action = error.freeze_action()          # ①显式 或 ②按 kind 查渠道 error_kind 规则
    if action is not None:
        pool.apply_freeze_action(username, route_info.get("routed_model") or model,
                                 action, reason=error_text,
                                 respect_refresh_flag=(error.freeze is None))  # 见 D2
        # 之后照常走换候选重试 / 终态归因，不改
```

③ 裸 HTTPException → 原逻辑一字不动。

`error.freeze_action()` 的解析：实例 dict → 直接用；`freeze=False` → 声明不冻；未给 → 读该渠道 `freeze_policy.rules` 里 `condition="error_kind"` 的条目；还没有 → 类默认。

### 3.3 探测/体检路径同权

`check_account` / `init_all`（rate_limiter.py:1892/1827）目前吞异常只写 `auth_ok`。改为：spec 在体检里抛出的 `ChannelError` 同样喂 `apply_freeze_action`（等价"探测失败与请求失败同权"）；`auth_ok` 退成纯诊断字段，展示/通知照旧。**这同时补上今天"探测失败啥也不冻、死号永在候选"的另一半。**

### 3.4 SSE 路径同构

`send_sse_request` 非 200 抛 HTTPException → spec 在 `stream_chat` 里 catch 后按语义重塑（codearts 已手改两处：额度改判 403、WAF 改判 502），机制化后重塑 = `raise QuotaExhausted/UpstreamTransient`，收口在流式失败处与③同源。`_parse_sse_event` 里 spec 直接 `raise CredentialInvalid(...)` 同样被捕获（它本来就是 HTTPException 家族）。

## 4. 规则表扩展：新条件 `error_kind`

`limit_policy_store.py`：`FREEZE_CONDITION_TYPES` 加 `"error_kind"`，`match_freeze_rules` 加一个 `kind` 入参（与 `error_code` 同槽位——`error_code` 从未被网关传入，是死槽，本次让 kind 走通这条道）。动作字段、校验、管理端表单复用现有 `freeze_object × freeze_period` 二维模型。

管理端据此可以给任何渠道配：「`credential_invalid` → 冻到今天而不是永久」——**供应商显式给的优先，管理端策略兜后**（§1 优先级）。

## 5. 决策与待评审（§1-2 已拍板；§3-7 中带 * 的条目为待你确认）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 异常继承 | 全部继承 `HTTPException`。顶层出口、重试分类、失败收口都按它读，客户端协议零变化 |
| 2 | 异常类放哪 | `providers/base.py`，经 `_build_namespace()` 注入 spec 命名空间，spec 直接 raise，无需 import |
| 3 | 异常语义从哪来 | **不发明新概念**——就是已梳理的账号状态：凭证失效（现「认证已失效，需人工更新凭据」态）、额度耗尽、上游临时故障、限流。custom 渠道（138 个）无登录态，不涉及凭证失效类 |
| 4 | 冻结时长归谁 | 异常可自带冻结参数；**自带 = 终态，不经过渠道冻结策略**（含失败刷新覆盖逻辑）；不带 = 走渠道策略（规则表新增 error_kind 条件），策略也没配 = 默认冻本账号 60 秒 |
| 5 | 冻结范围 | 默认且仅冻**抛异常的这个账号**（与现有失败处理同粒度）。账号×模型/渠道级只有显式参数才生效，不新增权限设计 |
| 6 | 兼容旧行为 | 客户端可见状态码/信封**保持各渠道现状**（codearts 死号迁移后仍回 400，只改冻结行为）；之后翻 spec 代码逐个找需要改的点 |
| 7 | 存量 spec | 先盘 31 个代码渠道：已有同类特殊处理的 → 去掉自造轮子换成新异常；没做特殊处理的 → 不动，遇到再改 |

## 6. 兼容性

| 层面 | 结论 |
|---|---|
| 对客户端协议（OpenAI/Anthropic 兼容出口） | 零变化（迁移期显式保持各渠道现状态码） |
| 存量 spec（裸 HTTPException） | 零变化，走③原路径 |
| 顶层 handler / 重试分类 / 冻结规则引擎 | 全部 isinstance 判定天然命中新子类，不改读法 |
| freeze_policy 存量规则 | 不变；新条件向后兼容（normalize 遇未知 condition 现在会拒绝——加枚举即可） |
| auth_ok 体系 | 语义收窄为纯诊断；**选路行为变化只发生在：spec 开始抛语义异常之后** |

## 6.5 账号状态属性与选路过滤（本轮最初目标，补回）

**AccountClient 增加统一状态属性**（property 现算，不新增存储字段，原料就是现有字段）：

```python
@property
def status(self) -> AccountStatus:
    # 正常 NORMAL      = 凭证齐 + 未冻结 + auth_ok 非 False
    # 未初始化 UNINITIALIZED = is_init() False（凭证字段不全）
    # 未认证 UNAUTHENTICATED  = auth_ok is False（诊断态）
    # 已冻结 FROZEN    = is_frozen（动态桶原样，永久/临时由 until 区分）
```

- 「之前判断冻结的地方改为判断这个」：`_collect_candidates` / `account_available_locked` 里的手判收敛为读 `status`。
- **选路过滤规则**：`DISABLED` / `FROZEN` 拦截；`UNINITIALIZED` 由 reserve 现有 `is_init` 拦；`UNAUTHENTICATED` / `CHECKING` 不拦——按「状态由冻结决定」的口径，只有真被冻住才出候选，探测误判（auth_ok 抖动）不饿死好号。
- 冻结时长刷新语义（2s/30s 谁覆盖谁、refresh_freeze_on_failure）原样保留；供应商显式冻结 = 终态（§5-4）。
- **渠道、模型同构**：`Channel` 增加 `status` 属性（正常/停用/冻结中），`AccountClient` 模型级冻结维持现有 `is_model_frozen(m)`（账号×模型二维，不塞进单值枚举）。

状态怎么被改写：**全部由冻结驱动**——请求失败与探测失败同权喂冻结执行器（§3.2/§3.3），spec 通过语义异常声明失败类别；`auth_ok` 仅诊断。

## 7. 分期实施

1. **P0 框架**：异常基类+5 子类、`apply_freeze_action` 抽取、main.py 收口分流、`check_account` 同权、规则表 `error_kind` 条件、spec 命名空间注入、**AccountClient/Channel 状态属性与选路过滤收敛**。单测：三层优先级、裸 HTTPException 回归不变、SSE 重塑、显式冻结不被 refresh 覆盖、状态属性四态判定与过滤行为。
2. **P0 试点迁移**：codearts——死号→`CredentialInvalid`、额度→`QuotaExhausted`、WAF→`UpstreamTransient`，删掉手写的状态码猜谜分支（含本轮 v2 的 403→502 临时写法，被机制取代）。
3. **P1 存量清理**：按盘点结果，**只对已有同类特殊逻辑的 spec** 逐个替换为语义异常（去掉自造轮子）；没做特殊处理的 spec 不动，遇到再改。
4. **P2**：文档（spec 规范 SPEC_GUIDE 增补「错误语义异常」章节）、管理端规则表单加 error_kind 条件。

## 8. 验收标准

- codearts 死号：一次真请求失败 → 账号进 is_frozen（按配置 permanent/today）→ 候选剔除，不再每请求白撞；重授权+测试成功 → 自动解冻。
- WAF 窗口复现时：好号最长 60s 不可选，不再冻到 0 点；不再有 `grant=refresh_token` 误烧。
- 状态属性：四个态判定与现网账号一一对应（正常/未初始化/未认证/已冻结），选路过滤行为=现状 frozen/disabled 拦截 + 死号经冻结后剔除，WAF 抖动不拦号。
- 全量回归：所有既有渠道 spec 测试零修改通过（③路径不变）；60s 对账/重启回灌/多实例广播行为与现状逐项一致。
