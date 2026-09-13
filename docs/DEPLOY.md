# Ai Lubricant 部署指南

本文件是 Ai Lubricant **部署唯一权威文档**。历史 README 里的「Qwen 反向代理 / config.json」叙事已废弃，以本文件为准。

## 架构概览

服务由**两个独立 Python 进程**组成，共用同一份 `.env`、同一套 PostgreSQL / Redis：

| 进程 | 入口 | 服务器 | 协议 | 端口 | 可达性 |
|---|---|---|---|---|---|
| 数据服务 | `python main.py` | uvicorn | HTTP/1.1 + WebSocket + SSE | `8001` | 对外唯一入口 |
| 控制服务 | `python -m node_server` | Hypercorn + h2c | 先验式 HTTP/2 明文 | `8003` | 仅节点与数据服务内部可达 |

**谁连谁**：

- 浏览器 / App / 终端 / 文件 / 会话客户端 → 数据服务（`:8001`），不直连控制服务。
- 节点从各自机器拨到控制服务（`node_server_public_url`，`:8003`）。
- 数据服务 → 控制服务（`agent_compose_base_url`），用 `node_control_token` 鉴权。

依赖中间件：

| 服务 | 用途 | 必需 |
|---|---|---|
| PostgreSQL | 业务配置（渠道/模型/api_keys 等） | 是 |
| Redis | 运行态/冻结态真相源（账号池、模型级冻结镜像、智能选择评分） | 是 |
| ClickHouse | 请求 payload 双写 | 否（默认关闭） |

## 配置与环境变量

**所有启动依赖配置项与环境变量见** → [ENV_VARS.md](ENV_VARS.md)

优先级铁律：**环境变量 > `.env` 字段 > 内置默认值**。`.env` 含明文密码已 gitignore，用 `.env.example` 作模板复制。

至少要配置：PG 连接（host/port/user/password/database）、Redis 连接（host/port/db/prefix_key/max_connections）、`node_control_token`（两进程同值，缺失会自动生成回写）。

## 部署形态

### 形态 A：Docker Compose 一键（本机 / 测试，推荐）

`docker-compose.yml` 已覆盖两进程 + PG + Redis（ClickHouse 走 profile，按需启）：

```bash
# 1. 复制配置模板并填值（compose 起本机中间件，PG/Redis 地址用环境变量指向容器服务名）
cp .env.example .env
#   至少填 [marketplace] repo_url（可选）、确认 node_control_token（两进程同值即可，留空自动生成）

# 2. 一键起依赖 + 两进程
docker compose up -d                  # postgres + redis + ai-lubricant + node-server

# 3.（可选）启用 ClickHouse
docker compose --profile clickhouse up -d
```

compose 内 `ai-lubricant` / `node-server` / `tunnel-server` 共用同一镜像 `ai-lubricant:<TAG>`：**只由 `ai-lubricant` 服务 `build:` 一次**，另两个服务仅 `image:` 引用（`pull_policy: never`）——避免同 tag 并行构建互踩、以及被当成远程镜像去 pull。三者分别 `CMD python main.py` / `python -m node_server` / `python -m tunnel_server`，PG/Redis 通过环境变量指向容器服务名 `postgres`/`redis`，`node_control_token` 由 .env 传递（容器读同一文件）。

端口映射：宿主 `3006 → 8001`（数据服务）、`8003 → 8003`（控制服务）、`15432 → 5432`（PG）、`6479 → 6379`（Redis）。

#### 国内部署加速（可选）

国内网络直连 Docker Hub / npmjs / Alpine CDN 很慢。在 `.env` 里设：

```bash
MIRROR_MODE=cn                            # 节点镜像 build + 运行时 npm 全走国内源
DOCKER_REGISTRY_PREFIX=docker.1ms.run/    # compose 拉基础镜像走加速器（含结尾 /）
```

- `MIRROR_MODE=cn`：服务端渲染节点安装脚本时自动为节点镜像 build 注入
  `--build-arg`（Alpine 基础镜像 / apk / npm 三层海外源换国内），并给节点进程
  导出 `NPM_CONFIG_REGISTRY`，使其装/升级编辑器 CLI（claude/codex/gemini 等）走
  npmmirror。**无需改节点侧任何配置**。
- `DOCKER_REGISTRY_PREFIX`：compose 的镜像插值不认 `MIRROR_MODE`，所以这一行
  要单独设（主服务 `FROM` + postgres/redis/clickhouse 拉取都吃它）。
- 两行**都留空 = 直连海外**，行为与不开完全一致。

三个地址均可覆盖（指向私有镜像 / 阿里云个人加速器 `<id>.mirror.aliyuncs.com/` /
自建反代）：`DOCKER_REGISTRY_PREFIX` / `NPM_REGISTRY` / `APK_MIRROR`，默认值
`docker.1ms.run/`、`https://registry.npmmirror.com`、`mirrors.aliyun.com`。

> 注意：`docker.1ms.run` 是公共加速器，可用性可能变动；生产建议换成阿里云个人
> 加速器或自建 registry proxy。

### 形态 B：宿主机裸跑（指向远端 PG/Redis）

适合 PG/Redis 已在远端（如内网 `10.x.x.x`）的情况：

```bash
# 1. 装依赖
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 配置 .env 指向远端 PG/Redis
cp .env.example .env
#   [postgres] host=10.x.x.x port=15432 ...
#   [redis]    host=10.x.x.x port=6579 ...

# 3. 两个终端各起一个进程
python main.py            # 终端 1：数据服务 :8001
python -m node_server     # 终端 2：控制服务 :8003
```

中间件也可本机用 compose 起、应用裸跑：`docker compose up -d postgres redis`，再让 `.env` 指向 `127.0.0.1`。

### 形态 C：生产

Linux 容器或 systemd，沿用形态 B 的配置方式（.env 指向生产 PG/Redis）。生产铁律：

- **切换控制服务：先停旧控制进程再启新进程**，绝不让两个 Registry 同时对同一批节点下发命令。
- `node_credential_encryption_key` 一旦生成不得轮换，否则已加密节点凭据全部失效；建议在 .env 显式预置并在两进程同值。
- `node_control_token` 两进程必须同值。
- 数据服务前端产物需单独构建（见下），随镜像/部署包分发。

## 前端产物

数据服务启动时查找 `user-frontend/dist/index.html` 作为用户门户与管理端共用 SPA（挂 `/admin-static`，API 前缀走后端、其余回退 index.html）。没有构建产物时根路径只返回前端构建提示，API 仍可正常运行。

```bash
cd user-frontend
pnpm install && pnpm build    # 产物落到 user-frontend/dist
```

## 启动顺序与切换铁律

1. 起 PG → Redis（或确认远端可达）。
2. 起控制服务 `python -m node_server`（或 compose 的 `node-server`）。
3. 起数据服务 `python main.py`（或 compose 的 `ai-lubricant`）。
4. 切换控制服务版本：**先停旧进程，再启新进程**，禁止双 Registry 同时下发。

## 验证

```bash
# 数据服务健康 + 模型列表
curl http://localhost:8001/v1/models

# 控制服务监听（h2c，curl 普通 HTTP/1.1 会协商失败或返回 426，看端口监听即可）
# Linux:  ss -ltnp | grep 8003
# macOS:  lsof -iTCP:8003 -sTCP:LISTEN
```

数据服务有响应即说明 PG/Redis 连通、主链路就绪；控制服务端口监听即说明 h2c 服务已起。
