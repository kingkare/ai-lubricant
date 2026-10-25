"""OpenAPI tag 加工：英文 tag → 「一级域 / 二级模块」中文 tag（共用模块）。

两个消费方：

* ``scripts/export_openapi.py`` —— 离线导出 ``docs/openapi.json``；
* ``main.py`` —— 覆盖 ``app.openapi``，让线上 ``GET /openapi.json``（Swagger UI
  与 Apifox URL 导入）与离线文件**同一份 tag 结构**，避免两处维护漂移。

结构：路由文件 ``APIRouter(tags=[...])`` 声明的原始英文 tag 经 :data:`TAG_NAMES`
翻成中文；粗粒度 tag（``admin`` / ``agent`` / ``mcp`` / ``model-proxy``）按
:data:`PATH_SPLIT_TAGS` 的路径规则再切细（第一个命中生效，具体前缀排在宽泛
前缀之前）。Apifox 按 tag 里的 ``/`` 生成嵌套目录，得到「域 → 模块」两层。
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# tag 映射：原始（或细分后）tag → (中文名, 模块说明)
#
# 二级模块名尽量用界面上的叫法（控制台/管理端用户看得懂），不用代码技术词。
# 说明写「这块管什么」，Apifox 里作为目录描述展示。
# ─────────────────────────────────────────────────────────────────────────────
TAG_NAMES: dict[str, tuple[str, str]] = {
    # ── 模型代理（一级：模型代理）────────────────────────────────────────────
    "model-proxy/chat": ("模型代理/对话", "OpenAI 兼容对话补全 /chat/completions 与关联入口。"),
    "model-proxy/responses": ("模型代理/Responses", "OpenAI Responses API。"),
    "model-proxy/messages": ("模型代理/Messages", "Anthropic 兼容的 /messages 接口。"),
    "model-proxy/images": ("模型代理/图像生成", "OpenAI 兼容 /images/generations。"),
    "model-proxy/videos": ("模型代理/视频生成", "OpenAI 兼容视频生成。"),
    "model-proxy/audio": ("模型代理/语音合成", "OpenAI 兼容 TTS /audio/speech。"),
    "model-proxy/models": ("模型代理/模型清单", "列出可用模型。"),
    "model-proxy/billing": ("模型代理/用量计费", "用量与计费查询。"),
    "model-proxy/token": ("模型代理/Token", "token 计数。"),
    "model-proxy": ("模型代理/其他", "未细分的模型推理入口。"),

    # ── 认证（一级：认证）──────────────────────────────────────────────────
    "auth/oauth": ("认证/OAuth 回调", "第三方 OAuth/授权回环回调。"),

    # ── 管理端（一级：管理端）──────────────────────────────────────────────
    "admin": ("管理端/基础", "管理端登录、页面入口与杂项。"),
    "admin/providers": ("管理端/渠道与账号", "供应商（渠道）与其账号的增删改查、启停、健康检查、测试与配置。"),
    "admin/config": ("管理端/主配置", "config.json 主配置。"),
    "admin/config/api-keys": ("管理端/API Key", "对外 API Key 的新建、启停、限流与用量查询。"),
    "admin/notifications": ("管理端/通知管理", "管理端侧的通知配置与发送记录。"),
    "admin/model-metadata": ("管理端/模型元数据", "模型的说明、倍率、上下文长度等元信息维护。"),
    "admin/global-config": ("管理端/全局配置", "节点公网 IP 探测等全局运行时配置。"),
    "admin/browser-proxy": ("管理端/浏览器代理", "CDP 浏览器代理池的配置与启停。"),
    "admin/security": ("管理端/安全", "管理端密码、会话与安全策略。"),
    "admin/model-groups": ("管理端/模型分组", "模型分组的增删改查与成员模型。"),
    "admin/custom-providers": ("管理端/自定义渠道", "自定义（code）渠道的创建与管理。"),
    "admin/model-routing": ("管理端/模型路由", "模型到渠道的路由规则。"),
    "admin/channel-catalog": ("管理端/渠道目录", "市场渠道模板目录的浏览与同步。"),
    "admin/agent": ("管理端/Agent 管理", "管理端侧的 Agent 实例管理。"),
    "admin/chat": ("管理端/对话管理", "管理端侧的会话查看与管理。"),
    "admin/request-logs": ("管理端/请求日志", "模型请求日志的查询。"),
    "admin/header-templates": ("管理端/Headers 模版", "渠道请求头模版的维护。"),
    "admin/model-rule-templates": ("管理端/模型规则模版", "模型 ID 改写规则模版。"),
    "channel-importer": ("管理端/渠道导入", "从外部平台预览并导入渠道。"),

    # ── Agent（一级：Agent）───────────────────────────────────────────────
    "agent": ("Agent/基础", "Agent 服务状态、工具清单与杂项。"),
    "agent/agents": ("Agent/Agent 管理", "Agent 实体的增删改查与启停。"),
    "agent/conversations": ("Agent/会话", "Agent 会话（conversation）的创建、消息、审批、中止与删除。"),
    "agent/chat": ("Agent/CDP 网页对话", "基于浏览器的网页版对话（client 侧）。"),
    "agent/workflows": ("Agent/工作流", "Agent 工作流定义与运行记录。"),
    "agent/sops": ("Agent/SOP", "标准作业流程（SOP）的增删改查。"),
    "agent/attachments": ("Agent/附件", "会话附件的上传与下载。"),
    "agent/task": ("Agent/任务", "一次性 Agent 任务的创建、查询与中止。"),
    "agent/config": ("Agent/配置", "Agent 服务侧配置。"),
    "agent-client": ("Agent/客户端接口", "CDP 网页对话客户端专用接口。"),
    "agent-llm-config": ("Agent/LLM 配置", "Agent 可选的 LLM 配置项。"),

    # ── MCP（一级：MCP）───────────────────────────────────────────────────
    "mcp": ("MCP/服务目录", "MCP 服务的登记、清单与详情。"),
    "mcp/services": ("MCP/服务管理", "MCP 服务的安装、部署与配置。"),
    "mcp/users": ("MCP/授权用户", "MCP 服务的授权用户与令牌。"),
    "mcp/runtime": ("MCP/运行时", "MCP 运行时的启停与状态。"),
    "mcp/my-services": ("MCP/我的服务", "当前用户名下的 MCP 服务。"),
    "mcp/builtin-tools": ("MCP/内置工具", "内置 MCP 工具的配置。"),
    "mcp-runtime-admin": ("MCP/运行时管理", "MCP 运行时实例的启停与管理。"),
    "mcp-runtime-sse": ("MCP/运行时 SSE", "MCP 运行时的 SSE 通道与消息。"),

    # ── 市场一级：市场 ─────────────────────────────────────────────────────
    "marketplace": ("市场/消费端", "面向用户的市场浏览、版本查询与安装包下载。"),
    "marketplace-admin": ("市场/管理端", "条目增删改查、导入导出、索引重建与各发行线的版本发布。"),

    # ── 节点（一级：节点）─────────────────────────────────────────────────
    "user-platform-nodes-admin": ("节点/管理端", "节点的上线审批、配置、升级、编辑器安装与全局控制。"),
    "user-platform-nodes-team": ("节点/团队", "团队侧节点的查看、绑定与自助管理。"),
    "user-platform-nodes-public": ("节点/安装与公开", "新节点安装脚本与公开探测端点。"),
    "user-platform-carrier": ("节点/载体", "节点载体的规格与绑定管理。"),
    "user-platform-studio": ("节点/Studio", "节点上 Studio 的部署与销毁。"),
    "ios-management": ("节点/iOS 管理", "iOS 签名描述文件、证书与设备管理。"),

    # ── 用户平台（一级：用户平台）─────────────────────────────────────────
    "user-platform": ("用户平台/账户", "注册、登录、令牌与账户状态。"),
    "user-platform-projects": ("用户平台/项目", "项目与协作者的增删改查、技术栈扫描、关联与子模块。"),
    "user-platform-tasks": ("用户平台/任务", "任务的创建、查询、分支模式与生命周期管理。"),
    "user-platform-task-workspace": ("用户平台/任务工作区", "任务的文件浏览、差异、端口与附件。"),
    "user-platform-editors": ("用户平台/编辑器", "编辑器实例与会话的管理。"),
    "user-platform-editor-workspace": ("用户平台/编辑器工作区", "编辑器的文件、端口与终端。"),
    "user-platform-editor-admin": ("用户平台/编辑器管理", "团队侧编辑器与会话的管理。"),
    "user-platform-team-users": ("用户平台/团队成员", "团队成员、用户组与仪表盘。"),
    "user-platform-team-models": ("用户平台/团队模型", "团队自定义模型。"),
    "user-platform-team-notify": ("用户平台/团队通知", "团队通知渠道配置。"),
    "user-platform-environments": ("用户平台/环境", "团队环境及其变量管理。"),
    "user-platform-user-notifications": ("用户平台/通知", "用户通知的列表、已读与事件类型。"),
    "user-platform-notify": ("用户平台/订阅规则", "通知订阅规则的配置。"),
    "user-platform-notify-devices": ("用户平台/推送设备", "推送设备的注册与心跳。"),
    "user-platform-skills": ("用户平台/技能", "技能的导入与管理。"),
    "user-platform-plugins": ("用户平台/插件", "插件的导入与管理。"),
    "user-platform-builtin-tools": ("用户平台/内置工具", "内置工具资源（CDP、浏览器密码等）。"),
    "user-platform-browser-passwords": ("用户平台/浏览器密码", "浏览器密码的保存与查看。"),
    "user-platform-totp": ("用户平台/TOTP", "TOTP 动态口令凭据的管理。"),
    "user-platform-user-shell-approval": ("用户平台/Shell 审批", "Shell 命令审批目录与策略。"),
    "user-platform-mcp-principals": ("用户平台/MCP 身份", "MCP principals 与实例授权。"),
    "user-platform-git-identities": ("用户平台/Git 身份", "Git 提交身份配置。"),
    "user-platform-git-bots": ("用户平台/Git 机器人", "Git 机器人凭据配置。"),
    "user-platform-project-webhooks": ("用户平台/项目 Webhook", "项目 Webhook 配置。"),
    "user-platform-tunnels": ("用户平台/内网穿透", "隧道方案与隧道的增删改查、启停。"),
    "user-platform-ddns": ("用户平台/DDNS", "动态域名与解析记录同步。"),
    "user-platform-user-models": ("用户平台/我的模型", "用户名下的自定义模型。"),
    "user-platform-notes": ("用户平台/便签", "个人便签的增删改查。"),
    "user-platform-server": ("用户平台/服务端信息", "服务端配置、版本与客户端 IP。"),
    "user-platform-captcha": ("用户平台/验证码", "公开验证码的挑战与兑换。"),

    # ── 资源中心（一级：资源中心）─────────────────────────────────────────
    "resource-references": ("资源中心/引用", "资源引用的增删改查。"),
    "resources": ("资源中心/镜像", "资源镜像的创建、刷新与删除。"),
    "system-env": ("资源中心/系统环境", "系统环境变量与文件上传。"),

    # ── 项目（一级：项目）───────────────────────────────────────────────
    "project-build": ("项目/构建", "项目构建任务的创建、查询与取消。"),
    "project-prompts-admin": ("项目/提示词管理", "管理端的项目提示词配置。"),
    "project-prompts-user": ("项目/提示词", "用户侧的项目提示词配置。"),
    "project-webhook-receive": ("项目/Webhook 接收", "外部 Webhook 接收入口。"),
    "github-recognize": ("项目/GitHub 识别", "GitHub 仓库的识别与文件读取。"),

    # ── 测试平台（一级：测试平台）────────────────────────────────────────
    "testing-surface": ("测试平台/方法面", "测试平台的方法执行、工具与 MCP。"),
}

#: 原始 tag 过粗时的**路径细分规则**：``(原始tag, 路径前缀, 细分tag)``。
#: 按列表顺序匹配、第一个命中生效——更具体的前缀必须排在宽泛前缀**之前**。
PATH_SPLIT_TAGS: tuple[tuple[str, str, str], ...] = (
    # model-proxy（根级别名 + /v1 前缀两套路径都要切）
    ("model-proxy", "/v1/chat", "model-proxy/chat"),
    ("model-proxy", "/v1/responses", "model-proxy/responses"),
    ("model-proxy", "/v1/messages", "model-proxy/messages"),
    ("model-proxy", "/v1/images", "model-proxy/images"),
    ("model-proxy", "/v1/videos", "model-proxy/videos"),
    ("model-proxy", "/v1/audio", "model-proxy/audio"),
    ("model-proxy", "/v1/models", "model-proxy/models"),
    ("model-proxy", "/v1/dashboard", "model-proxy/billing"),
    ("model-proxy", "/v1/token", "model-proxy/token"),
    ("model-proxy", "/chat", "model-proxy/chat"),
    ("model-proxy", "/responses", "model-proxy/responses"),
    ("model-proxy", "/messages", "model-proxy/messages"),
    ("model-proxy", "/images", "model-proxy/images"),
    ("model-proxy", "/videos", "model-proxy/videos"),
    ("model-proxy", "/audio", "model-proxy/audio"),
    ("model-proxy", "/models", "model-proxy/models"),
    ("model-proxy", "/dashboard", "model-proxy/billing"),
    ("model-proxy", "/token", "model-proxy/token"),
    # admin：先切二级业务域
    ("admin", "/admin/config/api-keys", "admin/config/api-keys"),
    ("admin", "/admin/config", "admin/config"),
    ("admin", "/admin/providers", "admin/providers"),
    ("admin", "/admin/notifications", "admin/notifications"),
    ("admin", "/admin/model-metadata", "admin/model-metadata"),
    ("admin", "/admin/global-config", "admin/global-config"),
    ("admin", "/admin/browser-proxy", "admin/browser-proxy"),
    ("admin", "/admin/security", "admin/security"),
    ("admin", "/admin/model-groups", "admin/model-groups"),
    ("admin", "/admin/custom-providers", "admin/custom-providers"),
    ("admin", "/admin/model-routing", "admin/model-routing"),
    ("admin", "/admin/channel-catalog", "admin/channel-catalog"),
    ("admin", "/admin/agent", "admin/agent"),
    ("admin", "/admin/chat", "admin/chat"),
    ("admin", "/admin/request-logs", "admin/request-logs"),
    ("admin", "/admin/header-templates", "admin/header-templates"),
    ("admin", "/admin/model-rule-templates", "admin/model-rule-templates"),
    ("admin", "/admin/channel-importers", "channel-importer"),
    ("admin", "/admin", "admin"),
    # agent
    ("agent", "/agent/agents", "agent/agents"),
    ("agent", "/agent/conversations", "agent/conversations"),
    ("agent", "/agent/chat", "agent/chat"),
    ("agent", "/agent/workflows", "agent/workflows"),
    ("agent", "/agent/sops", "agent/sops"),
    ("agent", "/agent/attachments", "agent/attachments"),
    ("agent", "/agent/task", "agent/task"),
    ("agent", "/agent/config", "agent/config"),
    ("agent", "/agent", "agent"),
    # mcp
    ("mcp", "/mcp/admin", "mcp-runtime-admin"),
    ("mcp", "/mcp/runtime", "mcp-runtime-admin"),
    ("mcp", "/mcp/services", "mcp/services"),
    ("mcp", "/mcp/users", "mcp/users"),
    ("mcp", "/mcp/my-services", "mcp/my-services"),
    ("mcp", "/mcp/openapi.json", "mcp"),
    ("mcp", "/mcp/builtin-tools", "mcp/builtin-tools"),
    ("mcp", "/mcp/runtime-settings", "mcp/runtime"),
    ("mcp", "/mcp/market-settings", "mcp/runtime"),
    ("mcp", "/mcp/catalog", "mcp"),
    ("mcp", "/mcp/node-hosted", "mcp-runtime-sse"),
    ("mcp", "/mcp", "mcp"),
)

#: 路径首段 → base tag：用于「路由没声明 tags」的模块（如 server/admin.py 的全部端点）。
#: 派生出 base 后交给 :data:`PATH_SPLIT_TAGS` 细分，逻辑与已声明 tag 的路由一致。
UNTAGGED_BASE_BY_SEGMENT: dict[str, str] = {
    "admin": "admin",
    "agent": "agent",
    "mcp": "mcp",
    "v1": "model-proxy",
}

#: 够不到首段 base 的根级别名端点 → 按前缀精确补 tag。
UNTAGGED_TAG_RULES: tuple[tuple[str, str], ...] = (
    ("/authorize", "auth/oauth"),
    ("/auth", "auth/oauth"),
    ("/oauth", "auth/oauth"),
    ("/chat", "model-proxy/chat"),
    ("/responses", "model-proxy/responses"),
    ("/messages", "model-proxy/messages"),
    ("/images", "model-proxy/images"),
    ("/videos", "model-proxy/videos"),
    ("/audio", "model-proxy/audio"),
    ("/models", "model-proxy/models"),
    ("/dashboard", "model-proxy/billing"),
    ("/token", "model-proxy/token"),
)

#: 未命中任何中文映射的 tag 保留原始英文名，避免丢 tag。
INFO_TITLE = "AI Lubricant 接口文档"
INFO_DESCRIPTION = (
    "多模型代理与 Agent 平台。接口按「一级域 / 二级模块」两级目录组织，"
    "例如 `用户平台/项目`、`模型代理/对话`。\n\n"
    "各接口说明来自源码 docstring；连接、鉴权与公共头由环境配置提供，不在接口里表达。"
)


def resolve_tag(tag: str, path: str) -> str:
    """把路由上的原始 tag 按路径规则细分（未命中则原样返回）。"""
    for rule_tag, prefix, fine in PATH_SPLIT_TAGS:
        if tag == rule_tag and path.startswith(prefix):
            return fine
    return tag


def decorate_spec(spec: dict) -> dict:
    """就地加工 OpenAPI spec：tag 细分 + 中文名 + tags 声明 + info 覆盖。

    供导出脚本与 ``main.py`` 的 ``app.openapi`` 覆盖共用——两处产物一致。
    """
    # 第一遍：给所有操作确定「细分后的英文 tag」。
    for path, item in spec.get("paths", {}).items():
        for op in item.values():
            if not isinstance(op, dict):
                continue
            tags = op.get("tags")
            if not tags:
                base = UNTAGGED_BASE_BY_SEGMENT.get(path.lstrip("/").split("/", 1)[0], "")
                if base:
                    op["tags"] = [resolve_tag(base, path)]
                    continue
                op["tags"] = [
                    next(
                        (fine for prefix, fine in UNTAGGED_TAG_RULES if path.startswith(prefix)),
                        "未分类",
                    )
                ]
                continue
            op["tags"] = [resolve_tag(tags[0], path)]

    # 第二遍：英文 tag → 中文名，并收集 tags 声明（按中文名稳定排序）。
    names: dict[str, str] = {}
    for path, item in spec.get("paths", {}).items():
        for op in item.values():
            if not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            if not tags:
                continue
            raw = tags[0]
            cn, desc = TAG_NAMES.get(raw, (raw, "未归类的接口。"))
            op["tags"] = [cn]
            names[cn] = desc

    spec["tags"] = [
        {"name": name, "description": desc}
        for name, desc in sorted(names.items(), key=lambda kv: kv[0])
    ]
    info = dict(spec.get("info") or {})
    # 直接覆盖而非 setdefault：FastAPI 的 app.title 是占位的 "Multi-Model Proxy"。
    info["title"] = INFO_TITLE
    info["description"] = INFO_DESCRIPTION
    spec["info"] = info
    return spec
