"""ModelScope 创空间（Studio）节点部署：渲染工件 + 推送 + 保活。

形态
----
一个创空间节点 = 一个普通 execution 节点，只是它的宿主机是 ModelScope 托管
的容器。平台约束（官方文档 ``studios/docker``）决定了产物形状：

* 容器**必须**有人监听 ``0.0.0.0:7860``（端口写死不可改，8080 被平台占用）。
  节点本体只拨出、不监听，所以镜像里另起一个 node 写的健康页占住 7860。
* ``/mnt/workspace`` 是唯一持久卷，且**构建期不可用**。因此运行时（189MB 的
  agent-compose-runtime）烘进镜像层 ``/opt/agent-compose/state``，而会话工作树
  落 ``/mnt/workspace``——重启保留，重建清空。
* 容器内**没有 docker.sock** → 只能做 execution 节点，management 角色拉不起
  子节点。``RunningInContainer()`` 还会连带关掉 self-upgrade 与 system 模式。
* 环境变量**运行时**注入，构建期不支持 → 凭据烘进仓库文件，同时脚本读同名
  环境变量并让 env 优先，用户想改用创空间设置页覆盖即可。

为什么整条链路要服务端跑 git
-----------------------------
创空间仓库是普通 git 仓库（``https://www.modelscope.cn/studios/<owner>/<name>.git``，
见 ``studios/create`` 文档）。平台没有「上传即部署」的 HTTP API（试过
``/api/v1/studios/.../repo``，404），官方推荐的就是 git push。所以服务端要能
跑 ``git``（见根 Dockerfile 的 apt 安装）。
"""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile
import time as _time

from loguru import logger

# 健康页：平台要求容器里有人监听 7860，节点本体不监听，所以另起一个 node 进程
# 占位。用 node 而不是 python，是因为基础镜像 node:20-slim 自带 node、不带 python。
_HEALTH_JS = """\
// ModelScope 创空间状态页：占住平台约定端口 7860，同时把节点状态显示成人能看的页面。
//
// 为什么需要这个页：节点本体（node-execution）只向控制面拨出、不监听任何端口，
// 而创空间要求容器内有进程监听 0.0.0.0:7860，否则平台判「运行错误」。既然必须
// 占这个端口，就把它做成排障入口——节点连不上时，用户打开创空间页面看到的不该是
// 一串 JSON，而应该是「在连哪个地址、连没连上、上次失败是什么」。
//
// 状态从哪来：node-execution 把日志写进 /opt/agent-compose/logs/。这里读最新那份
// 的尾部，按已知的日志行判定连接状态。不去问节点进程（它没有控制接口），也不额外
// 维护状态文件（多一个可能不同步的真值源）。
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 7860;
// 与 start.sh 导出的 AGENT_COMPOSE_NODE_LOG_DIR 对齐（那边钉死、这边读回来）。
// 兜底值是节点默认推导出的位置，两个都对不上时页面显示「状态未知」而不是崩。
const LOG_DIR = process.env.AGENT_COMPOSE_NODE_LOG_DIR || '/opt/agent-compose/logs';
const SERVER = process.env.AGENT_COMPOSE_SERVER || '';
const NODE_ID = process.env.AGENT_COMPOSE_NODE_ID || '';
const WORK_ROOT = process.env.AGENT_COMPOSE_NODE_WORK_ROOT || '';

/** HTML 转义：值来自环境变量与日志，落进页面必须转义。 */
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 日志读取结果缓存。平台会频繁探活（FC Invoke 每次请求都可能打到这），而日志文件
// 按天累积、可能很大；每次请求都读一遍既慢又浪费。2 秒的窗口足够让状态「跟手」，
// 又能挡住探活风暴。
const TAIL_CACHE_MS = 2000;
let tailCache = { at: 0, lines: [] };

/** 读最新日志文件的尾部若干行。任何失败都返回空数组（页面照常渲染）。 */
function tailLog(maxLines) {
  const now = Date.now();
  if (now - tailCache.at < TAIL_CACHE_MS) return tailCache.lines;
  let lines = [];
  try {
    const files = fs.readdirSync(LOG_DIR)
      .filter((name) => name.startsWith('node-execution') && name.endsWith('.log'))
      .sort();
    if (files.length) {
      const full = path.join(LOG_DIR, files[files.length - 1]);
      const size = fs.statSync(full).size;
      // 只读尾部 128KB：一天的日志可能有几十 MB，整读会把探活拖成秒级。
      // 128KB 足够放下上百行，而我们要的只是最后几条连接状态。
      const CHUNK = 128 * 1024;
      const start = Math.max(0, size - CHUNK);
      const fd = fs.openSync(full, 'r');
      try {
        const buf = Buffer.alloc(Math.min(CHUNK, size));
        fs.readSync(fd, buf, 0, buf.length, start);
        lines = buf.toString('utf8').split('\\n').filter(Boolean);
      } finally {
        fs.closeSync(fd);
      }
    }
  } catch (err) {
    lines = [];
  }
  // 从块中间截断时首行是半行，丢掉——否则可能把半条日志误判成完整状态行。
  if (lines.length) lines = lines.slice(1);
  tailCache = { at: now, lines };
  return lines;
}

/**
 * 从日志尾部判定当前连接状态。
 *
 * 只认最后一条「连接相关」的日志——节点会反复重试，早先的成功不能盖过后来的失败，
 * 反之亦然。识别不到任何一条时状态是 unknown（刚启动、日志还没刷出来）。
 */
function connectionState() {
  const lines = tailLog();
  let state = 'unknown';
  let detail = '';
  for (const line of lines) {
    if (line.includes('msg="node registered"')) {
      state = 'connected';
      detail = '';
    } else if (line.includes('msg="node connection lost"')) {
      state = 'retrying';
      // 日志行形如：... msg="node connection lost; reconnecting" error="await server hello: ..."
      const match = line.match(/error="([^"]*)"/);
      detail = match ? match[1] : '';
    } else if (line.includes('msg="node connection attempt"')) {
      // 只把「尚无结论」的状态推进到 connecting。节点失败后会反复打 attempt，
      // 若让它无条件覆盖，就会把 retrying 和错误原因一起冲掉——而用户打开页面
      // 想看的恰恰是「为什么连不上」。
      if (state === 'unknown' || state === 'starting') state = 'connecting';
    } else if (line.includes('msg="node-execution starting"')) {
      if (state === 'unknown') state = 'starting';
    }
  }
  return { state, detail };
}

const LABELS = {
  connected: ['已连接', '#16a34a'],
  connecting: ['连接中', '#ca8a04'],
  starting: ['启动中', '#ca8a04'],
  retrying: ['连接失败，重试中', '#dc2626'],
  unknown: ['状态未知', '#6b7280'],
};

/** 连接不上时最可能的原因：server 是内网地址，而创空间在公网。 */
function diagnosis() {
  const host = (SERVER.match(/^https?:\\/\\/([^/:]+)/) || [])[1] || '';
  if (/^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|127\\.|localhost$)/.test(host)) {
    // 只描述问题、不回显地址：本页在公网可达，控制面地址（含端口）属于不该
    // 对外暴露的内部拓扑信息。
    return '控制面地址是内网/回环地址，创空间容器在公网拨不到它。' +
      '请在 Ai Lubricant 里把节点拨号地址改为公网可达地址后重新部署。';
  }
  return '';
}

/**
 * 掩掉主机名，只留协议与端口：``http://1.2.3.4:8003`` → ``http://***:8003``。
 *
 * 为什么还留端口：本页在公网可达（创空间公开地址），完整地址会泄露内网拓扑，
 * 但排障时用户需要核对「端口对不对」——只留端口既够用又不暴露主机。
 */
function maskServer(raw) {
  if (!raw) return '（未配置）';
  const match = raw.match(/^(https?:\\/\\/)([^/:]+)(:\\d+)?/);
  if (!match) return '***';
  return match[1] + '***' + (match[3] || '');
}

function renderPage() {
  const { state, detail } = connectionState();
  const [label, color] = LABELS[state] || LABELS.unknown;
  const hint = state === 'connected' ? '' : diagnosis();
  const uptime = Math.floor(process.uptime());
  const rows = [
    ['节点 ID', NODE_ID || '（未配置）'],
    // 地址脱敏：本页公网可达，完整控制面地址不外显（见 maskServer 注释）。
    ['控制面地址', maskServer(SERVER)],
    ['工作目录', WORK_ROOT || '（未配置）'],
    ['健康页运行时长', uptime + ' 秒'],
  ];
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ai Lubricant 节点</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; padding:2rem 1rem; font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
         background:#f8fafc; color:#0f172a; }
  @media (prefers-color-scheme: dark) { body { background:#0b1120; color:#e2e8f0; }
    .card { background:#111827 !important; border-color:#1f2937 !important; }
    dt { color:#94a3b8 !important; } code { background:#1f2937 !important; } }
  .wrap { max-width:44rem; margin:0 auto; }
  h1 { font-size:1.15rem; margin:0 0 .25rem; }
  .sub { color:#64748b; font-size:.85rem; margin:0 0 1.5rem; }
  .card { background:#fff; border:1px solid #e2e8f0; border-radius:.75rem; padding:1.25rem; margin-bottom:1rem; }
  .status { display:flex; align-items:center; gap:.6rem; font-weight:600; font-size:1.05rem; }
  .dot { width:.7rem; height:.7rem; border-radius:50%; flex:none; background:${color}; }
  dl { display:grid; grid-template-columns:auto 1fr; gap:.4rem 1rem; margin:1rem 0 0; font-size:.9rem; }
  dt { color:#64748b; white-space:nowrap; }
  dd { margin:0; word-break:break-all; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  code { background:#f1f5f9; padding:.1rem .35rem; border-radius:.25rem;
         font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em; }
  .alert { border-radius:.5rem; padding:.85rem 1rem; font-size:.9rem;
           background:#fef2f2; border:1px solid #fecaca; color:#991b1b; }
  @media (prefers-color-scheme: dark) { .alert { background:#450a0a; border-color:#7f1d1d; color:#fecaca; } }
  .err { margin-top:.6rem; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
         font-size:.8rem; opacity:.85; word-break:break-all; }
</style></head>
<body><div class="wrap">
  <h1>Ai Lubricant 节点</h1>
  <p class="sub">跑在 ModelScope 创空间里的执行节点</p>

  <div class="card">
    <div class="status"><span class="dot"></span>${esc(label)}</div>
    ${detail ? `<div class="err">${esc(detail)}</div>` : ''}
    <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
  </div>

  ${hint ? `<div class="alert">${esc(hint)}</div>` : ''}

  <div class="card">
    <div style="font-weight:600;margin-bottom:.5rem">这个页面是什么</div>
    <p style="margin:0;font-size:.9rem;color:#64748b">
      创空间要求容器内有人监听 <code>7860</code>，所以这里放了一个状态页。
      节点本身只向控制面拨出，不需要对外提供服务——本页<strong>不代表节点可用</strong>，
      节点是否真正接上要看上面的连接状态。
    </p>
  </div>
</div></body></html>`;
}

http.createServer((req, res) => {
  // 给保活探针留一个纯文本端点，省得它拉整个 HTML。
  if ((req.url || '').startsWith('/healthz')) {
    const { state } = connectionState();
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ ok: true, state, node_id: NODE_ID }));
    return;
  }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(renderPage());
}).listen(PORT, '0.0.0.0', () => {
  console.log('[studio-health] listening on 0.0.0.0:' + PORT);
});
"""

# 启动脚本：先起健康页，再把节点拉成前台进程（容器寿命 = 节点寿命）。
_START_SH = """\
#!/bin/sh
# ModelScope 创空间容器入口。
#
# 顺序：健康页（占 7860）→ node-execution（前台）。
# 节点退出即容器退出——平台会把创空间标成「运行错误」，运维在创空间日志里能
# 直接看到原因，比留一个活着但不干活的容器更可诊断。
set -eu

# 凭据全部来自创空间**环境变量**（由 Ai Lubricant 经 OpenAPI 的 variables 接口
# 写入）。仓库文件里不留任何密钥，所以本仓库可以公开，轮换凭据也不必重新部署。
#
# 缺任何一个就明确报错退出：与其让节点带着空凭据去连（表现为无限重试、日志里
# 一堆超时），不如在创空间的运行日志里直接说清缺了什么。
: "${AGENT_COMPOSE_SERVER:?未设置 AGENT_COMPOSE_SERVER —— 请在创空间「设置 → 环境变量」补齐}"
: "${AGENT_COMPOSE_NODE_ID:?未设置 AGENT_COMPOSE_NODE_ID —— 请在创空间「设置 → 环境变量」补齐}"
: "${AGENT_COMPOSE_NODE_SECRET:?未设置 AGENT_COMPOSE_NODE_SECRET —— 请在创空间「设置 → 环境变量」补齐}"
export AGENT_COMPOSE_SERVER AGENT_COMPOSE_NODE_ID AGENT_COMPOSE_NODE_SECRET

# 托管形态：跳过宿主机单实例锁与持久化配置（凭据来自环境变量），也不装开机自启。
export AGENT_COMPOSE_NODE_MANAGED=1
export AGENT_COMPOSE_NODE_ROLE=execution
# 容器里 system 模式无意义（HOME 是镜像的），显式关掉。
export AGENT_COMPOSE_NODE_ALLOW_SYSTEM_ENV=off
# 让控制台把该节点显示为「创空间」而不是「手动启动」。
export AGENT_COMPOSE_STARTUP_METHOD=studio

# 运行时（agent-compose-runtime）在镜像层里，随镜像走，不进持久卷——持久卷
# 只在运行时可用，构建期写不进去。
export AGENT_COMPOSE_NODE_STATE_DIR=/opt/agent-compose/state
# 日志目录钉死：默认值是从可执行文件所在目录推出来的（/opt/agent-compose/logs），
# 但健康页要读它做状态判定，隐式推导一旦漂移（换镜像布局、换安装路径）页面就会
# 永远显示「状态未知」。显式写死，两边对齐。
export AGENT_COMPOSE_NODE_LOG_DIR=/opt/agent-compose/logs
# 会话工作树落持久卷：创空间重启不丢，重建清空（与节点自举幂等口径一致）。
export AGENT_COMPOSE_NODE_WORK_ROOT=/mnt/workspace/work
mkdir -p "$AGENT_COMPOSE_NODE_WORK_ROOT" "$AGENT_COMPOSE_NODE_LOG_DIR"

# 只打节点 ID，**不打控制面地址**：容器日志在创空间控制台可见，而地址（含端口）
# 属于内部拓扑信息，不该出现在那里。排障要核对地址时看 Ai Lubricant 管理端。
echo "[studio] node_id=$AGENT_COMPOSE_NODE_ID starting"

# 健康页后台运行；挂了不影响节点（平台探活失败才会判错，那时本来也该暴露）。
node /opt/agent-compose/health.js &

exec /opt/agent-compose/node-execution
"""

_DOCKERFILE = """\
# ModelScope 创空间节点镜像（由 ai-lubricant 生成，勿手工编辑）。
#
# 与 nodes/docker/Dockerfile 的关系：同一套节点运行时的两种托管形态。那份面向
# 「宿主机本地 docker build + docker run」，这份面向「创空间按仓库构建、单容器、
# 无 compose、无 docker.sock、必须监听 7860」。
#
# 基础镜像用 node:20-slim（Debian bookworm）：agent-compose-runtime 的
# package.json 要求 node >= 20，Ubuntu 22.04 自带的 nodejs 是 12，装不上。
FROM node:20-slim

ARG APT_MIRROR=@@APT_MIRROR@@
ARG NPM_REGISTRY=@@NPM_REGISTRY@@
# 发行产物地址在渲染时直接烘进 ARG 默认值——创空间按仓库构建，**不能传
# --build-arg**，所以默认值必须自足（写成 ARG 而非直接 curl 是为了手工重建时
# 还能用 --build-arg 换源）。
ARG EXEC_URL=@@EXEC_URL@@
ARG RUNTIME_URL=@@RUNTIME_URL@@
ARG RUNTIME_SHA=@@RUNTIME_SHA@@

# git            — 会话 clone 仓库、fileservice 的 __git_changes__/__git_diff__
# npm            — manageEditor 装 claude/codex/gemini/opencode（已在下方预装）
# openssh-client — git over SSH 克隆
# tini           — PID1 + 信号回收
# ca-certificates — TLS 出站校验
#
# 镜像源默认走国内：**创空间的构建机在阿里云国内**，拉 deb.debian.org 与
# registry.npmjs.org 都要绕一圈国际出口，是构建慢的主因。这里与 MIRROR_MODE 无关
# ——那个开关描述的是「我们的服务端在哪」，而构建机的位置是平台决定的、恒在国内。
# 留 ARG 是为了手工重建时还能覆盖。
RUN if [ -n "$APT_MIRROR" ]; then \\
      sed -i "s#deb.debian.org#$APT_MIRROR#g; s#security.debian.org#$APT_MIRROR#g" \\
        /etc/apt/sources.list.d/debian.sources; \\
    fi \\
 && apt-get update \\
 && apt-get install -y --no-install-recommends \\
      ca-certificates curl git openssh-client tini \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/agent-compose

# 编辑器 CLI 先装（**单独一层**）。
#
# 为什么必须和下载分开：平台**没开构建缓存**（build 日志里 cacheType= 空），
# 每次都是全量构建；但推送按**层 digest**跳过（Layer already exists）。npm 这层
# 的内容只随包版本变，digest 稳定 → 每次构建都跳过推送；下载层烘了 EXEC_URL /
# RUNTIME_URL（发行版本一变就变），digest 必然变 → 每次都全量推。
# 把两者合进一层，npm 那 ~600MB 就被迫跟着下载层一起重推 —— 实测 884s 全花在这。
# 拆开后只有下载层重推，npm 层跳过。
RUN set -eu; \\
    if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi; \\
    npm install -g --no-audit --no-fund \\
      @anthropic-ai/claude-code @openai/codex @google/gemini-cli opencode-ai; \\
    npm cache clean --force

# 发行产物下载（单独一层：digest 随发行版本变，每次都重推是正常的）。
RUN set -eu; \\
    test -n "$EXEC_URL" || { echo "EXEC_URL 未设置：请先在节点市场登记发行版本" >&2; exit 1; }; \\
    test -n "$RUNTIME_URL" || { echo "RUNTIME_URL 未设置：请先在节点市场登记 runtime" >&2; exit 1; }; \\
    mkdir -p /opt/agent-compose/state /tmp/dl; \\
    curl -fsSL --parallel --parallel-max 2 \\
      -o /opt/agent-compose/node-execution "$EXEC_URL" \\
      -o /tmp/dl/runtime.tar.gz "$RUNTIME_URL"; \\
    chmod +x /opt/agent-compose/node-execution; \\
    { [ -z "$RUNTIME_SHA" ] || echo "$RUNTIME_SHA  /tmp/dl/runtime.tar.gz" | sha256sum -c -; }; \\
    tar -xzf /tmp/dl/runtime.tar.gz -C /opt/agent-compose/state; \\
    test -f /opt/agent-compose/state/runtime/dist/cli.js || { echo "runtime 解压后缺少 dist/cli.js" >&2; exit 1; }; \\
    rm -rf /tmp/dl

# 会话工作树在持久卷上，容器内以 root 运行；git 对非本用户所有的仓库会拒绝操作。
RUN git config --system --add safe.directory '*'

COPY health.js /opt/agent-compose/health.js
COPY start.sh /opt/agent-compose/start.sh
RUN chmod +x /opt/agent-compose/start.sh

# 平台约定端口，写死不可改（deploy_schema.json: const 7860）。
EXPOSE 7860

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agent-compose/start.sh"]
"""

# 创空间卡片（README.md 的 YAML 头）。平台解析它做展示；缺文件不影响部署，
# 但创空间页面会没有说明，所以一并写入。
_README = """\
---
license: Apache License 2.0
tags:
- ai-lubricant
- agent
---

# Ai Lubricant 节点

本创空间由 [Ai Lubricant](@@BASE@@) 自动生成，用于托管一个执行节点。

- 节点 ID：`@@NODE_ID@@`
- 部署形态：ModelScope 创空间（Docker SDK，端口 7860）

> 控制面地址不写在这里：本仓库可能被设为公开，而控制面地址（含端口）属于内部
> 拓扑信息。地址由 Ai Lubricant 经创空间环境变量注入，见下方「凭据」一节。

## 怎么启动

由 Ai Lubricant 自动触发上线（调 ModelScope OpenAPI 的 deploy 接口）。平台会按
仓库里的 `Dockerfile` 构建镜像并启动容器，容器内的节点会自动拨回控制面。

如需手动重来：在本创空间的「设置」页点击 **上线**。

## 怎么改

本仓库的文件由 Ai Lubricant 生成，**手工修改会在下次重新部署时被覆盖**。
请在 Ai Lubricant 管理端「节点 → 创空间」页操作。

## 凭据

**本仓库不含任何密钥。** 节点凭据（`AGENT_COMPOSE_SERVER` / `_NODE_ID` /
`_NODE_SECRET`）由 Ai Lubricant 经 ModelScope OpenAPI 写入本创空间的
**环境变量**，容器启动时由 `start.sh` 读取。所以本仓库可以公开，轮换凭据也不必
改仓库——在「设置 → 环境变量」里改即可。

## 排查

打开本创空间的访问地址会看到节点状态页：显示当前连接状态，以及连不上时的原因。
最常见的失败是**控制面地址不可达**——`AGENT_COMPOSE_SERVER` 若是内网地址
（`10.` / `192.168.` / `127.`），公网上的容器永远拨不到它。
"""


# ── 凭据封存 ────────────────────────────────────────────────────────────────
# AES-256-GCM，blob = base64(nonce || ciphertext||tag)，12 字节 nonce——与
# node_server.crypto.SecretStore 逐字节一致（那套又对齐 Go daemon 的
# pkg/auth/secretstore），所以两处封存的凭据可以互相解开，运维只有一把钥匙。
#
# 为什么在这里重写而不是 import node_server.crypto
# -----------------------------------------------
# user_platform 与 node_server 是「不共享 Python 代码」的两个包：前者跑数据服务，
# 后者是独立进程、经 HTTP 访问（见 user_platform/__init__.py 的模块 docstring 与
# node_server/shared_store.py 的自包含约束）。让数据服务 import 控制面的模块会
# 破坏这条边界。同目录的 apple_signing/gsa.py 也是直接用 cryptography 库而非复用
# node_server，本模块照此办理。代价是 ~30 行重复；收益是边界不被侵蚀，且换实现时
# 不会牵动另一个进程。

_KEY_BYTES = 32
_NONCE_BYTES = 12


def _parse_master_key(raw: str) -> bytes:
    """解出 32 字节主密钥（hex 或 base64，两种都接受）。

    解析口径与 ``node_server.crypto.parse_master_key`` 一致——同一个环境变量
    ``NODE_CREDENTIAL_ENCRYPTION_KEY`` 会被两边读，口径分叉的表现是「另一侧解不开」。
    """
    import base64 as _b64
    import binascii

    raw = (raw or "").strip()
    if not raw:
        raise ValueError("master key is empty")
    if len(raw) == _KEY_BYTES * 2:
        try:
            return binascii.unhexlify(raw)
        except (binascii.Error, ValueError):
            pass
    for decoder in (_b64.standard_b64decode, _b64.urlsafe_b64decode):
        for candidate in (raw, raw + "=" * ((-len(raw)) % 4)):
            try:
                decoded = decoder(candidate)
            except (binascii.Error, ValueError):
                continue
            if len(decoded) == _KEY_BYTES:
                return decoded
    raise ValueError("master key must decode to 32 bytes (hex or base64)")


def _cipher():
    """构造 AESGCM；密钥缺失/非法返回 None（降级为明文 + API 掩码）。"""
    from .config import settings

    raw = (settings.node_credential_encryption_key or "").strip()
    if not raw:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(_parse_master_key(raw))
    except Exception:  # noqa: BLE001 — 封存是增强，不是前置
        logger.warning("[studio] 凭据封存不可用，将明文存储并在 API 层掩码", exc_info=True)
        return None


def seal_token(token: str) -> str:
    """封存凭据。无密钥时加 ``plain:`` 前缀标记明文，便于读取时区分。"""
    token = (token or "").strip()
    if not token:
        return ""
    cipher = _cipher()
    if cipher is None:
        return "plain:" + token
    import base64 as _b64
    import secrets as _secrets

    nonce = _secrets.token_bytes(_NONCE_BYTES)
    sealed = cipher.encrypt(nonce, token.encode("utf-8"), None)
    return _b64.standard_b64encode(nonce + sealed).decode("ascii")


def open_token(blob: str) -> str:
    """解出凭据。解不开一律返回空串（调用方按「未配置」处理）。"""
    blob = (blob or "").strip()
    if not blob:
        return ""
    if blob.startswith("plain:"):
        return blob[len("plain:"):]
    cipher = _cipher()
    if cipher is None:
        logger.warning("[studio] 无密钥，无法解出已封存凭据")
        return ""
    import base64 as _b64

    try:
        raw = _b64.standard_b64decode(blob)
        return cipher.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], None).decode("utf-8")
    except Exception:  # noqa: BLE001
        logger.warning("[studio] 凭据解密失败（密钥轮换或数据损坏）", exc_info=True)
        return ""


def mask_token(token: str) -> str:
    """API 响应里的令牌展示形态：只留尾部 4 位。"""
    token = (token or "").strip()
    if not token:
        return ""
    if len(token) <= 4:
        return "*" * len(token)
    return "*" * (len(token) - 4) + token[-4:]


# ── 地址规范化 ──────────────────────────────────────────────────────────────
# 用户拿到地址的地方和我们需要的地方不是同一个：
#   * 浏览器地址栏复制的是**页面地址**（…/studios/<owner>/<name>），不是 clone 地址；
#   * 官方文档给的 clone 命令里带凭据（https://oauth2:<token>@…）；
#   * 有人会粘 SSH 形式（git@…:studios/….git）。
# 直接在 UI 上报「格式不对」是偷懒——三种里只有第一种是用户真正会做的事。这里
# 统一收敛成 ``https://<host>/studios/<owner>/<name>.git``，让用户贴什么都能用。

_STUDIO_HOST = "www.modelscope.cn"


def normalize_repo_url(raw: str) -> str:
    """把用户给的创空间地址收敛成 clone 用的 HTTPS 仓库地址。

    接受：页面地址、clone 地址、带凭据的 clone 地址、SSH 地址、无 scheme 的裸地址。
    **凭据一律剥掉**——粘贴带 token 的地址是最容易发生的事（官方文档里的 clone
    命令就长那样），而我们会把它存库并在前端回显，留着就是明文泄露。

    不校验 owner/name 是否真实存在（那要打网络，且失败也不该在这里拦）；只保证
    形状正确、能交给 git。
    """
    import re
    from urllib.parse import urlsplit, urlunsplit

    text = (raw or "").strip()
    if not text:
        raise ValueError("创空间仓库地址不能为空")

    # 裸 owner/name（或 owner/name.git）：没有 scheme、没有点，先于其它解析处理。
    # 不特判的话 urlsplit 会把第一段当 host、第二段当 path，最终报「找不到用户名/
    # 创空间名」这种让人摸不着头脑的错。
    if "://" not in text and "@" not in text and text.count("/") == 1:
        owner, name = text.split("/", 1)
        owner, name = owner.strip(), name.removesuffix(".git").strip("/")
        if owner and name:
            return urlunsplit(("https", _STUDIO_HOST, f"/studios/{owner}/{name}.git", "", ""))

    # SSH 形式 git@host:studios/owner/name.git → 转 HTTPS。
    ssh = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", text)
    if ssh:
        host, path = ssh.group(1), ssh.group(2)
        text = f"https://{host}/{path}"

    if "://" not in text:
        text = "https://" + text

    parts = urlsplit(text)
    if not parts.netloc:
        raise ValueError(f"地址无法解析：{raw}")
    # netloc 里带 @ 就是嵌了凭据（user:pass@host）——只保留 host 部分。
    host = parts.netloc.rsplit("@", 1)[-1]
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    # 页面地址可能带查询串（?tab=files 之类），一并丢掉。
    segments = [seg for seg in path.split("/") if seg]

    # 容忍带与不带 studios 前缀两种写法：/studios/owner/name 与 /owner/name。
    if segments and segments[0] == "studios":
        segments = segments[1:]
    if len(segments) < 2:
        raise ValueError(
            "地址里找不到「用户名/创空间名」。请从创空间「空间内容」页复制完整地址，"
            f"形如 https://{_STUDIO_HOST}/studios/<用户名>/<创空间名>"
        )

    owner, name = segments[0], segments[1]
    return urlunsplit(("https", host, f"/studios/{owner}/{name}.git", "", ""))


def normalize_public_url(raw: str) -> str:
    """把创空间公开访问地址收敛成 https:// 开头的 URL（保活探针的目标）。

    空值合法（= 不做保活）。只补 scheme、剥末尾斜杠，不做别的猜测——这个地址是
    用户从创空间页面复制的，形状本来就不固定。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    return text.rstrip("/")


def parse_studio_ref(repo_url: str) -> tuple[str, str]:
    """从创空间仓库地址里取出 ``(owner, repo_name)``。

    OpenAPI 需要这两段（``/studios/{owner}/{repo_name}/...``），而用户填的是仓库
    地址（或裸 ``owner/name``）。接受 ``normalize_repo_url`` 的输出、页面地址、
    以及裸 ``owner/name``。
    """
    from urllib.parse import urlsplit

    text = (repo_url or "").strip()
    if not text:
        raise ValueError("创空间仓库地址为空")

    if "://" in text:
        path = urlsplit(text).path
    elif text.count("/") == 1:
        parts = text.split("/", 1)
        return parts[0], parts[1].removesuffix(".git")
    else:
        path = text

    segments = [seg for seg in path.split("/") if seg]
    if segments and segments[0] == "studios":
        segments = segments[1:]
    if len(segments) < 2:
        raise ValueError(f"从 {repo_url} 里解析不出「用户名/创空间名」")
    owner, name = segments[0], segments[1]
    return owner, name.removesuffix(".git")


# ── 工件渲染 ────────────────────────────────────────────────────────────────

def _sub(template: str, values: dict[str, str]) -> str:
    """占位符替换。用 ``@@TOKEN@@`` 而不是 f-string：Dockerfile/shell 里
    ``${VAR}`` 遍地都是，f-string 的括号转义会淹没内容。"""
    out = template
    for key, value in values.items():
        out = out.replace(f"@@{key}@@", str(value))
    return out


def render_health_js() -> str:
    return _HEALTH_JS


def render_start_sh() -> str:
    """渲染容器入口。

    **不接凭据参数**：凭据走创空间环境变量（OpenAPI 的 variables 接口写入），
    仓库文件里不留任何密钥。所以本函数是常量渲染——签名里曾经有
    server/node_id/secret 三个参数，那是「烘进仓库」时代的残留。
    """
    return _START_SH


def render_dockerfile(
    *,
    exec_url: str,
    runtime_url: str,
    runtime_sha: str,
    apt_mirror: str = "",
    npm_registry: str = "",
) -> str:
    """渲染创空间 Dockerfile。

    ``exec_url`` / ``runtime_url`` 是当前登记版本的发行产物地址（release
    catalog 渲染后的绝对 URL）。**直接烘进 ARG 默认值**——创空间按仓库构建，
    不能传 ``--build-arg``，默认值必须自足（写成 ARG 而非硬编码 curl 是为了手工
    重建时还能覆盖）。

    ``apt_mirror`` / ``npm_registry`` 是构建期的国内镜像源。默认由调用方填国内
    地址（创空间构建机恒在阿里云国内），空值则走官方源。
    """
    return _sub(_DOCKERFILE, {
        "EXEC_URL": exec_url,
        "RUNTIME_URL": runtime_url,
        "RUNTIME_SHA": runtime_sha,
        "APT_MIRROR": apt_mirror,
        "NPM_REGISTRY": npm_registry,
    })


def render_readme(*, node_id: str, server: str, base: str) -> str:
    return _sub(_README, {"NODE_ID": node_id, "SERVER": server, "BASE": base or server})


# 构建期镜像源默认值。**与 MIRROR_MODE 无关**：那个开关描述的是「我们的服务端
# 在哪」，而创空间构建机的位置由平台决定、恒在阿里云国内。默认走国内源能显著
# 缩短构建（apt 与 npm 都免绕国际出口），这是「部署慢」最直接的一刀。
#
# 用 env 而非硬编码，是为了让运维在平台换区、或国内源故障时能就地改回官方源。
_DEFAULT_APT_MIRROR = "mirrors.aliyun.com"
_DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com"


def default_build_mirrors() -> dict[str, str]:
    """创空间构建期的 apt / npm 镜像源（可用环境变量覆盖为空以走官方源）。"""
    return {
        "apt_mirror": os.getenv("STUDIO_APT_MIRROR", _DEFAULT_APT_MIRROR).strip(),
        "npm_registry": os.getenv("STUDIO_NPM_REGISTRY", _DEFAULT_NPM_REGISTRY).strip(),
    }


def build_artifacts(
    *,
    node_id: str,
    server: str,
    exec_url: str,
    runtime_url: str,
    runtime_sha: str,
    base: str = "",
    apt_mirror: str = "",
    npm_registry: str = "",
) -> dict[str, str]:
    """一次渲染出要写进创空间仓库的全部文件（相对路径 → 内容）。

    **不含任何凭据**：节点 secret 走创空间环境变量（见 :mod:`studio_api` 的
    ``set_variables``），仓库因此可以公开。``node_id`` / ``server`` 只用于 README
    里的说明文字，不是密钥。
    """
    return {
        "Dockerfile": render_dockerfile(
            exec_url=exec_url, runtime_url=runtime_url, runtime_sha=runtime_sha,
            apt_mirror=apt_mirror, npm_registry=npm_registry,
        ),
        "start.sh": render_start_sh(),
        "health.js": render_health_js(),
        "README.md": render_readme(node_id=node_id, server=server, base=base),
    }


# ── 推送到创空间仓库 ────────────────────────────────────────────────────────

class StudioPushError(RuntimeError):
    """推送失败。message 已脱敏（绝不含令牌）。"""


def _git_env(repo_url: str, token: str) -> tuple[dict[str, str], str]:
    """构造推送用的 git 环境 + 临时全局配置文件路径。

    令牌**不进 argv、不进 env 值**：argv 会被同机 ``ps`` 看到，env 值能从
    ``/proc/<pid>/environ`` 读到。这里写一个 0600 的临时 git 配置文件，用
    ``GIT_CONFIG_GLOBAL`` 指过去——命令行上只出现一个文件路径。

    返回 ``(env, config_path)``；调用方负责删除 config_path。
    """
    credential = base64.b64encode(f"oauth2:{token}".encode("utf-8")).decode("ascii")
    fd, path = tempfile.mkstemp(prefix="ai-lubricant-studio-", suffix=".gitconfig")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                "[http]\n"
                f"\textraHeader = Authorization: Basic {credential}\n"
                "[user]\n"
                "\tname = Ai Lubricant\n"
                "\temail = node@ai-lubricant.local\n"
                "[safe]\n"
                "\tdirectory = *\n"
                # 创空间仓库自带 .gitattributes（官方要求先 git lfs install），
                # 可能把某些后缀标成 LFS。服务器上未必装了 git-lfs，一旦某个
                # 路径命中 LFS 规则，`git add` 会去调不存在的 clean filter 而失败
                # ——整条部署链路跪在一个和节点无关的二进制文件上。我们推的四个
                # 文件全是纯文本，LFS 对它们毫无意义，所以直接把 LFS 过滤器短路
                # 成 cat（读原样、写原样），既不改动既有文件的字节，也不引入依赖。
                '[filter "lfs"]\n'
                "\tclean = cat\n"
                "\tsmudge = cat\n"
                "\trequired = false\n"
            )
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": path,
        # 绝不交互：无终端环境下 git 会挂死在凭据提示上。
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
        # 双保险：即便上面 filter 短路被某个配置覆盖，LFS 也不去拉真实内容。
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    return env, path


def _redact(text: str, token: str) -> str:
    """把可能出现在 git 输出里的令牌抹掉（防御性：理论上不该出现）。"""
    out = (text or "").strip()
    if token:
        out = out.replace(token, "***")
        out = out.replace(base64.b64encode(f"oauth2:{token}".encode()).decode(), "***")
    return out[:600]


async def _run_git(args: list[str], *, cwd: str, env: dict[str, str], token: str) -> str:
    """跑一条 git 命令；失败抛 StudioPushError（输出已脱敏）。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise StudioPushError("git 操作超时（180s）——检查服务器到 modelscope.cn 的网络")
    if proc.returncode != 0:
        detail = _redact((stderr or b"").decode("utf-8", "replace"), token)
        raise StudioPushError(f"git {' '.join(args[:2])} 失败：{detail}")
    return (stdout or b"").decode("utf-8", "replace")


async def push_files(
    *,
    repo_url: str,
    token: str,
    files: dict[str, str],
    commit_message: str = "chore(node): 更新 Ai Lubricant 节点部署文件",
) -> None:
    """把 ``files`` 写到 ``repo_url`` 的 master 分支并推送。

    不用 clone：创空间仓库可能是空的（刚创建），clone 空仓库拿不到分支会让后续
    步骤多一层特判。这里 ``git init`` + ``fetch`` 拉现有内容（失败即视为空仓库），
    再覆盖写文件。``push origin HEAD:master`` 不依赖本地分支名，空/非空都成立。

    每次推送是一次完整覆盖：本模块生成的文件是唯一真相，手工改动会被覆盖
    （README 里已声明）。
    """
    repo_url = (repo_url or "").strip()
    token = (token or "").strip()
    if not repo_url:
        raise StudioPushError("创空间仓库地址为空")
    if not token:
        raise StudioPushError("ModelScope 访问令牌为空")
    if shutil.which("git") is None:
        raise StudioPushError("服务器未安装 git（根 Dockerfile 已装；裸跑形态请 apt/yum 安装）")

    env, config_path = _git_env(repo_url, token)
    workdir = tempfile.mkdtemp(prefix="ai-lubricant-studio-")
    try:
        await _run_git(["init", "-q"], cwd=workdir, env=env, token=token)
        await _run_git(["remote", "add", "origin", repo_url], cwd=workdir, env=env, token=token)

        # 拉现有 master。空仓库（或无 master 分支）时 fetch 失败——不是错误，
        # 首次部署本来就没有内容可拉。
        try:
            await _run_git(
                ["fetch", "-q", "--depth=1", "origin", "master"],
                cwd=workdir, env=env, token=token,
            )
            await _run_git(["reset", "-q", "--hard", "FETCH_HEAD"], cwd=workdir, env=env, token=token)
        except StudioPushError:
            logger.info("[studio] 仓库暂无 master 分支（首次部署），按空仓库推送")

        for rel_path, content in files.items():
            target = os.path.join(workdir, rel_path)
            os.makedirs(os.path.dirname(target) or workdir, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            # 入口脚本要有可执行位。光靠 os.chmod 不够：Windows 文件系统没有 exec
            # 位，chmod 只切只读属性，git 在那边（core.fileMode 探测为 false）会把
            # 文件记成 100644。所以下面 add 之后再用 update-index --chmod 显式写
            # index——那条命令直接改 index，不查文件系统，两个平台一致。
            if rel_path.endswith(".sh"):
                os.chmod(target, 0o755)

        await _run_git(["add", "-A"], cwd=workdir, env=env, token=token)
        for rel_path in files:
            if rel_path.endswith(".sh"):
                await _run_git(
                    ["update-index", "--chmod=+x", "--", rel_path],
                    cwd=workdir, env=env, token=token,
                )
        # 内容没变时 commit 会以「nothing to commit」失败——那是成功语义（已是
        # 目标状态），不该报错。用 diff --cached --quiet 先判断。
        status = await asyncio.create_subprocess_exec(
            "git", "diff", "--cached", "--quiet",
            cwd=workdir, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        await status.wait()
        if status.returncode == 0:
            logger.info("[studio] 仓库内容已是目标状态，无需推送")
            return
        await _run_git(["commit", "-q", "-m", commit_message], cwd=workdir, env=env, token=token)
        await _run_git(["push", "-q", "origin", "HEAD:master"], cwd=workdir, env=env, token=token)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            os.unlink(config_path)
        except OSError:
            pass


# ── 保活 ────────────────────────────────────────────────────────────────────
# 免费档创空间闲置会休眠（官方文档：「若实例一段时间未使用后，将进入休眠，
# 重新访问后即会激活启动」）。节点持续拨出**不算**「使用」，所以闲置后掉线。
#
# 走 OpenAPI 而不是打公开地址：``GET /studios/{owner}/{repo}`` 直接返回
# ``runtime.status``（含 ``Sleeping``），是权威状态而非猜测；唤醒用
# ``POST /deploy``（spec 说「停止的容器进行均可调用」），比打页面可靠。

# 连续多少轮探到 Running 才认为节点真的起来了（避免刚 deploy 完就判「已唤醒」
# 而用户那边节点还没注册）。这里只是观测，不影响唤醒决策。
async def probe_studio_status(
    *, repo_url: str, token: str, timeout: float = 20.0
) -> dict:
    """查创空间运行时状态。返回 ``{ok, status, label, error}``，任何异常都不抛。

    ``ok=False`` 只表示这次查询失败（网络/令牌），不代表创空间有问题。
    """
    import asyncio

    from . import studio_api as sa

    if not (repo_url or "").strip() or not (token or "").strip():
        return {"ok": False, "status": "", "label": "", "error": "缺少仓库地址或令牌"}
    try:
        owner, repo_name = parse_studio_ref(repo_url)
        runtime = await asyncio.wait_for(
            sa.get_runtime(owner, repo_name, token=token), timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 — 探针失败不该冒泡
        logger.debug("[studio] status probe failed for {}: {}", repo_url, exc)
        return {"ok": False, "status": "", "label": "", "error": str(exc)[:200]}
    status = str(runtime.get("status") or "")
    return {
        "ok": True,
        "status": status,
        "label": sa.status_label(status),
        "error": str(runtime.get("error_message") or "")[:300],
    }


async def record_status(row, probe: dict) -> None:
    """把一次探针结果写进账本（保活时间 + 是否可达）。

    **三处探状态的地方共用它**：「唤醒」按钮、面板「查看状态」、后台保活循环。
    不共用就会出现口径分裂——保活循环更新了状态而面板没更，或者反过来，用户看到
    两个地方对同一个创空间给出不同说法。

    只记「探针跑了 / 拿没拿到结论」：平台的运行时状态（Running/Sleeping…）不落库，
    部署进度条那边按 ``runtime.status`` 实时映射（见 ``studio_service`` 的
    ``_STATUS_TO_STEPS``），不需要这份缓存。
    """
    import datetime as _dt

    row.last_keepalive_at = _dt.datetime.utcnow()
    row.last_keepalive_ok = bool(probe.get("ok"))
    try:
        await row.save(update_fields=["last_keepalive_at", "last_keepalive_ok", "updated_at"])
    except Exception:  # noqa: BLE001 — 观测信息写不进去不该让调用方失败
        logger.warning("[studio] persist status failed for {}", getattr(row, "node_id", "?"), exc_info=True)


async def keepalive_loop() -> None:
    """后台保活循环。

    只对**当前离线**的创空间节点做检查：节点在线说明容器本来就在跑，再查一次纯属
    浪费配额。节点掉线才值得管——那正是休眠（或有故障）的表现。

    唤醒口径：查到 ``Sleeping``/``Stopped`` 才 ``POST /deploy``；正在
    ``Building``/``Deploying`` 时按兵不动（重复 deploy 会叠加构建）。
    """
    from .config import settings

    if not settings.enabled:
        # 兼容层关闭时 mc_studio_deployments 表根本不存在，查它会每轮抛异常刷日志。
        logger.info("[studio] keepalive loop skipped (user-platform compat layer disabled)")
        return

    interval = max(60, int(os.getenv("STUDIO_KEEPALIVE_INTERVAL", "300")))
    logger.info("[studio] keepalive loop started (interval={}s)", interval)
    while True:
        try:
            await _keepalive_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("[studio] keepalive tick failed", exc_info=True)
        await asyncio.sleep(interval)


# ── 节点进程真实状态探针（见 _keepalive_tick 的 Running 分支）──────────────
# 平台的 runtime.status 是**容器**状态，容器活着不代表 node-execution 没卡死。
# 我们自己渲染进容器的 health.js 暴露了一个 /healthz，返回它从节点日志里解析出
# 的真实连接态 —— 这才是「进程活没活」的依据。
#
# 端点选择：创空间的网页域名（``<owner>-<name>.ms.show``）被平台登录网关挡着，
# 匿名访问回 403「please login」，带 Bearer 也是 403 并点名要走 API 专用域名
# （实测）：
#   https://studio-<owner>-<name>.api-inference.modelscope.net/healthz
#   Authorization: Bearer <ModelScope SDK token>
#   → {"ok":true,"state":"connected","node_id":"..."}
# 所以 host 要从详情接口的 host 推成 API 专用域名，不能直接拿网页域名打。
async def probe_node_state(row, token: str) -> str:
    """问节点自己「进程现在是什么连接状态」。

    返回 ``connected`` / ``retrying`` / ``connecting`` / ``starting`` / ``unknown``，
    探不到（网络失败、空间没有 host、返回体不是我们的 JSON）时返回空串。

    **只读不处置**：本函数不触发任何重建——发现问题就把它记下来交给运维判断。
    """
    import json as _json

    import aiohttp

    host = str(getattr(row, "public_url", "") or "").strip()
    if not host:
        # 账本里的 public_url 可能是空的（老部署在「自动补 host」之前完成的）。
        # 这条路径只在「容器 Running 但控制面判离线」时才走，一次 API 调用换一个
        # 真实结论，值得。
        from . import studio_api as _sa

        try:
            owner, repo_name = parse_studio_ref(row.repo_url)
            host = str((await _sa.get_studio(owner, repo_name, token=token)).get("host") or "").strip()
        except Exception:  # noqa: BLE001 — 拿不到就算了，不阻断保活
            host = ""
        if not host:
            return ""
    # public_url 存的是网页域名（ms.show）；换成平台点名的 API 专用域名。
    try:
        rest = host.split("//", 1)[1]
        sub = rest.split(".", 1)[0]
        api_base = f"https://{sub}.api-inference.modelscope.net"
    except (IndexError, ValueError):
        return ""
    url = api_base.rstrip("/") + "/healthz"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status != 200:
                    return ""
                raw = await resp.read()
        payload = _json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception:  # noqa: BLE001 — 探针失败不该让保活整轮挂掉
        return ""
    state = payload.get("state") if isinstance(payload, dict) else None
    return str(state or "").strip()


async def keepalive_one(row, *, online: bool) -> dict:
    """单个创空间节点的保活（**旧 tick 与载体 spec 共用这一份实现**）。

    抽出来是为了「创空间包成载体 spec」（Phase 3）时不出现两套口径：载体 spec 的
    ``keepalive`` 钩子直接调它，旧循环也调它——行为天然一致，不会一边唤醒一边不唤醒。

    ``online=True``：节点在线 ⇒ 容器在跑（节点进程就跑在那个容器里），不必再打一次
    API 确认——这正是原设计跳过在线节点的原因（省配额）。只更新探针时间，不写假状态。

    ``online=False``：探状态 → 落库 → 明确「不接活」（Sleeping/Stopped）才唤醒；
    构建/部署中按兵不动（重复 deploy 会叠加构建）；Running 但节点离线只告警不重建
    （重建盖症状 + 烧构建配额，处置交人工）。

    返回 ``{ok, note}``（ok 只表达「这次保活是否有结论」，不是「节点在线」）。
    """
    from . import studio_api as sa

    if online:
        await record_status(row, {"ok": True})
        return {"ok": True, "note": "在线"}

    token = open_token(row.access_token_enc)
    if not token:
        # 没有令牌就查不了状态，也唤醒不了。跳过（不是错误：令牌缺失由部署阶段就
        # 提示过，这里再刷日志没意义）。
        return {"ok": False, "note": "缺少 ModelScope 令牌，请重新部署一次"}
    try:
        owner, repo_name = parse_studio_ref(row.repo_url)
    except ValueError as exc:
        return {"ok": False, "note": f"仓库地址无效：{exc}"}

    probe = await probe_studio_status(repo_url=row.repo_url, token=token)
    # 落库：离线节点的真实状态（Sleeping / BuildFailed / …）。
    await record_status(row, probe)

    if not probe["ok"]:
        logger.info("[studio] {} status unknown: {}", row.node_id, probe["error"])
        return {"ok": False, "note": f"查不到创空间状态：{probe['error']}"}

    status = probe["status"]
    if status in ("Sleeping", "Stopped"):
        try:
            await sa.deploy(owner, repo_name, token=token)
            logger.info("[studio] {} was {} — deploy triggered to wake it", row.node_id, status)
            return {"ok": True, "note": f"创空间{probe['label']}，已触发上线唤醒"}
        except Exception as exc:  # noqa: BLE001 — 唤醒失败下轮再试
            logger.warning("[studio] {} wake deploy failed", row.node_id, exc_info=True)
            return {"ok": False, "note": f"创空间{probe['label']}，唤醒失败：{exc}"}

    if status == "Running":
        # **容器 Running ≠ 节点进程活着**。health.js 是独立进程，一直在伺服 7860，
        # 所以 node-execution 卡死了容器照样报 Running——拿容器状态当存活判断
        # 会把「进程已死」当成「一切正常」。这里改问节点自己。
        #
        # **不自动重建**：重建只是把症状盖掉、绕过人工判断，还会烧用户的构建
        # 配额（构建本身坏掉时更会反复白建）。这里只如实记账 + 告警，处置权
        # 交给运维——面板上「重新部署」按钮是显式的，人看过了才该按。
        node_state = await probe_node_state(row, token)
        if node_state == "connected":
            # 容器 Running + 节点自报 connected，控制面却把它判成离线 ——
            # 两侧对不上，问题在控制面的存活上报，不在节点容器。
            logger.warning(
                "[studio] {} 容器 Running 且节点自报 connected，但控制面显示离线"
                "（hb={}）——疑似存活上报/心跳口径问题，非节点问题",
                row.node_id, row.last_keepalive_at,
            )
            return {"ok": False, "note": "容器 Running 且节点自报 connected，但控制面显示离线"
                                        "（疑似存活上报/心跳口径问题）"}
        logger.warning(
            "[studio] {} 容器 Running 但节点自报 state={}（控制面离线）"
            "——进程没连上或已卡死。请到创空间页面看运行日志，或在面板手动重新部署",
            row.node_id, node_state or "探不到",
        )
        return {"ok": False, "note": f"容器 Running 但节点自报 {node_state or '探不到'}"
                                     "（进程没连上或已卡死），请到创空间看日志或手动重新部署"}

    logger.info("[studio] {} status={}（{}）", row.node_id, status, probe["label"])
    return {"ok": True, "note": f"创空间状态：{probe['label']}"}


async def _keepalive_tick() -> None:
    from .models_carrier import NodeCarrierBinding
    from .models_studio import StudioDeployment
    from .nodes_service import get_local_node_client

    rows = await StudioDeployment.filter().exclude(repo_url="")
    if not rows:
        return
    try:
        live = await get_local_node_client().list_nodes()
    except Exception:  # noqa: BLE001 — 控制面不可用时无法判断在线，整轮跳过
        logger.debug("[studio] keepalive: node list unavailable, skipping tick", exc_info=True)
        return

    # Phase 3 互斥：**有载体绑定的行归载体共享循环管**（modelscope_studio spec 的
    # keepalive 与本函数共用 keepalive_one，口径一致）。这里继续兜底**没有绑定**的行
    # ——绑定补挂漏了某个节点时它不会失保活。两边都跑同一节点才会双重 deploy（叠加
    # 构建烧配额），互斥就是防这个。
    try:
        bound = set(await NodeCarrierBinding.filter().values_list("node_id", flat=True))
    except Exception:  # noqa: BLE001 - 载体层不可用（表未建/单测）时退回全量旧循环
        bound = set()

    # 节点已经被我们这边删掉了，台账里连一行都没有 → 账本剩下的只是**孤儿**：
    # 那个创空间跟我们的关系早就断了，没有节点会往里拨号，探活/唤醒都毫无意义。
    # 只对仍在本平台管辖下的节点做保活，这是本循环的边界。
    #
    # 不是「偷偷删用户的数据」：创空间仓库是用户的资产，这里只是**不再探**。
    # 用户去面板点「删除创空间」才会清账本（顺带清环境变量里的节点凭据）。
    known = {str(n.get("node_id") or "") for n in (live or [])}
    online = {
        str(n.get("node_id") or "")
        for n in (live or [])
        if n.get("online") or n.get("connected")
    }

    for row in rows:
        if row.node_id in bound:
            continue
        if row.node_id not in known:
            continue
        try:
            await keepalive_one(row, online=row.node_id in online)
        except Exception:  # noqa: BLE001 - 单节点失败不影响整轮
            logger.warning("[studio] keepalive {} failed", row.node_id, exc_info=True)
