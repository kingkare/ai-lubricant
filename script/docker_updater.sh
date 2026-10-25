#!/usr/bin/env bash
# docker_updater.sh —— docker compose 形态的服务端升级执行器（宿主侧，root）。
#
# 由 alb-upgrade.path（标记文件变更即触发）或 alb-upgrade.timer（30s 兜底）拉起。
#
# 升级方式：**下载发布包**（不是 git）。包由 publish_github.sh 打在 GitHub Release
# 上，自包含主仓 + node_server + user-frontend + VERSION。之所以不用 git：
#   - 部署机/容器可能没有 git（镜像里只有 pip 依赖，无 git）
#   - GitHub 的 tag 源码包不含子模块内容（gitlink 是空目录），解压后 build 必失败
#
# 三个 app 服务共用一个镜像（ai-lubricant:<TAG>），build 一次 up -d 一次全升；
# compose 的 depends_on: service_healthy 保证 node-server 先健康再动主服务，
# 切换铁律（绝不让两个 Registry 同时下发）天然满足。
#
# 仅支持 Linux（systemd 触发 + docker compose v2 + curl + rsync）。
#
# 用法：
#   bash docker_updater.sh            # 正常一轮（由 path/timer 拉起）
#   DRY_RUN=1 bash docker_updater.sh  # 空跑：只读标记、不下载不构建不重启
set -euo pipefail

# ── 配置（env 可覆盖）──────────────────────────────────────────────────────
PROJECT_DIR="${ALB_PROJECT_DIR:-/opt/ai-lubricant}"
# 标记/状态目录：与 ai-lubricant 容器的 /app/data 同一个 bind mount
# （compose: ./data/ai_data:/app/data），web 写 /app/data/upgrade_*，
# 宿主在这里读，两边看到同一份文件。
STATE_DIR="${SERVER_UPGRADE_STATE_DIR:-$PROJECT_DIR/data/ai_data}"
MARKER="$STATE_DIR/upgrade_target"
STATE_FILE="$STATE_DIR/upgrade_state.json"
LOG_FILE="${SERVER_UPGRADE_LOG:-$PROJECT_DIR/data/ai_logs/upgrade.log}"
COMPOSE_CMD="${ALB_COMPOSE_CMD:-docker compose}"
HEALTH_TIMEOUT="${SERVER_UPGRADE_HEALTH_TIMEOUT:-180}"   # 健康轮询总秒数
VERSION_PORT="${ALB_VERSION_PORT:-8001}"                 # 版本回读端口（容器 published 端口）
DRY_RUN="${DRY_RUN:-0}"

LOCK="/run/alb-docker-upgrade.lock"
mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")" 2>/dev/null || true

_log() { echo "$(date '+%F %T') $*" >> "$LOG_FILE" 2>/dev/null || echo "$*"; }

_do() {
  if [ "$DRY_RUN" = "1" ]; then _log "[dry-run] $*"; return 0; fi
  "$@"
}

_write_state() {  # phase [target] [error]
  local phase="$1" target="${2:-}" error="${3:-}"
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] state: phase=$phase target=$target error=$error"; return 0
  fi
  python3 - "$STATE_FILE" "$phase" "$target" "$error" <<'PY' || _log "⚠ 状态写入失败"
import json, datetime, os, sys, tempfile
path, phase, target, error = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
state = {"phase": phase, "target": target, "error": (error or "")[:500]}
try:
    old = json.load(open(path, encoding="utf-8"))
    if isinstance(old, dict) and old.get("started_at"):
        state["started_at"] = old["started_at"]
except Exception:
    pass
state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".upgrade_state.", suffix=".json")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(state, fh, ensure_ascii=False)
os.replace(tmp, path)
PY
}

_marker_field() {  # $1=文件 $2=键
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    v = d.get(sys.argv[2])
except Exception:
    v = None
print(v if isinstance(v, str) else "")
PY
}

_current_version() {
  # 当前运行版本 = 项目目录里 VERSION 文件的内容（包自带，publish 写入 tag）。
  # 回落到容器内 VERSION 读不到时的 dev。
  if [ -f "$PROJECT_DIR/VERSION" ]; then
    tr -d '\r\n' < "$PROJECT_DIR/VERSION"
  else
    echo "dev"
  fi
}

_health_status() {  # $1 = 容器名
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo "missing"
}

_verify_health() {
  # 三个 app 容器都有 healthcheck（compose 定义），等它们全绿。
  # tunnel-server 的 8004 是 expose 不是 published，宿主 curl 不到——但容器健康
  # 检查跑在容器内，docker inspect 能拿到，所以统一走 inspect 而不是 curl 端口。
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] 将轮询三容器 healthcheck（${HEALTH_TIMEOUT}s）"; return 0
  fi
  local containers=("ai-lubricant-node-server" "ai-lubricant" "ai-lubricant-tunnel-server")
  local i=0
  sleep 3
  while [ "$i" -lt "$HEALTH_TIMEOUT" ]; do
    local all_healthy=1
    for c in "${containers[@]}"; do
      local st; st=$(_health_status "$c")
      if [ "$st" != "healthy" ]; then
        all_healthy=0
        _log "  · $st: $c"
      fi
    done
    if [ "$all_healthy" = "1" ]; then
      _log "三服务全绿"
      return 0
    fi
    i=$((i + 5))
    sleep 5
  done
  _log "✗ 健康验证超时（${HEALTH_TIMEOUT}s）"
  return 1
}

_verify_version() {  # $1 = 期望版本
  # 健康只证明进程活着；版本回读才证明「跑的确实是新代码」——容器读镜像内
  # VERSION 文件并通过 /api/v1/server/version 暴露。对不上说明镜像没换成新的
  # （层缓存/构建异常/up 没重建容器）。
  local want="$1" i=0 got=""
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] 将回读 http://127.0.0.1:$VERSION_PORT/api/v1/server/version 期望 $want"
    return 0
  fi
  while [ "$i" -lt 30 ]; do
    got="$(curl -sf --max-time 5 "http://127.0.0.1:$VERSION_PORT/api/v1/server/version" 2>/dev/null \
           | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)"
    if [ "$got" = "$want" ]; then
      _log "版本回读一致：$got"
      return 0
    fi
    i=$((i + 2)); sleep 2
  done
  _log "✗ 版本回读不符：期望 $want，实得 ${got:-（读不到）}"
  return 1
}

_rollback() {  # $1 = target $2 = old $3 = reason
  local target="$1" old="$2" reason="$3"
  _log "回滚：$target → $old（$reason）"
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] 将回切 $old 并重建容器"; return 0
  fi
  if [ -z "$old" ] || [ "$old" = "dev" ]; then
    _write_state failed "$target" "回滚失败（无旧版版本号）：$reason"
    _log "✗✗ 无旧版可回切，需人工介入"
    return 0
  fi
  # 旧镜像还在本地（GC 只删更早的），秒回——不重新下载/构建
  if docker image inspect "ai-lubricant:$old" >/dev/null 2>&1; then
    _write_state restarting "$target"
    if (cd "$PROJECT_DIR" && TAG="$old" $COMPOSE_CMD up -d) 2>>"$LOG_FILE"; then
      if _verify_health; then
        _write_state failed "$target" "已回滚到 $old：$reason"
        return 0
      fi
    fi
  fi
  _write_state failed "$target" "回滚失败（旧镜像 $old 不可用）：$reason"
  _log "✗✗ 回滚失败，需人工介入"
}

_cleanup_claimed() { rm -f -- "$1" 2>/dev/null || true; }

_gc_images() {  # $1 = 当前 tag；保留 current + 最近 2 个 tag 的镜像
  # 镜像 tag 与版本号同名（compose 用 TAG=$target 构建）。按创建时间倒序，
  # 跳过 current，删第 3 个起。正在运行的容器用的镜像删不掉（docker 会拒绝），
  # 所以即便误判也伤不到现役。
  if [ "$DRY_RUN" = "1" ]; then return 0; fi
  local keep="$1" n=0 line tag
  while IFS= read -r line; do
    tag=$(echo "$line" | awk '{print $1}')
    [ -z "$tag" ] && continue
    [ "$tag" = "$keep" ] && continue
    [ "$tag" = "local" ] && continue          # 默认 tag 不删（手工 build 的产物）
    n=$((n + 1))
    if [ "$n" -gt 2 ]; then
      docker rmi "ai-lubricant:$tag" >/dev/null 2>&1 || true
      _log "GC 删除旧镜像：ai-lubricant:$tag"
    fi
  done < <(docker images ai-lubricant --format '{{.Tag}} {{.CreatedAt}}' 2>/dev/null | sort -k2 -r || true)
}

# ── 主流程 ────────────────────────────────────────────────────────────────

# 单飞
if [ "$DRY_RUN" != "1" ]; then
  exec 9>"$LOCK"
  flock -n 9 || { _log "上一轮升级仍在进行，跳过"; exit 0; }
fi

[ -f "$MARKER" ] || exit 0

# 认领标记（dry-run 只读不认领，避免吃掉真实请求）
claimed="$MARKER.claimed-$$"
if [ "$DRY_RUN" = "1" ]; then
  claimed="$MARKER"
else
  mv "$MARKER" "$claimed" 2>/dev/null || exit 0
fi

target=$(_marker_field "$claimed" target)
proxy_url=$(_marker_field "$claimed" proxy_url)
bundle_url=$(_marker_field "$claimed" bundle_url)
old=$(_current_version)

# 标记内容必须是合法版本号（防止畸形值拼进 URL/镜像 tag）
case "$target" in
  "" | *[!A-Za-z0-9._/-]*)
    _write_state failed "$target" "标记内容非法"
    _cleanup_claimed "$claimed"; exit 0 ;;
esac

_log "── 升级请求：$old → $target（dry-run=$DRY_RUN）"

# 预检：compose 文件在（升级靠「下载包 + 重建镜像」，不需要宿主是 git 仓库）
if [ ! -f "$PROJECT_DIR/docker-compose.yml" ]; then
  _write_state failed "$target" "$PROJECT_DIR/docker-compose.yml 不存在"
  _cleanup_claimed "$claimed"; exit 0
fi

# 自愈：target == 当前版本 → 只重建容器（断电后构建完但 up -d 没跑的场景）
if [ "$target" = "$old" ]; then
  _log "自愈：$target（重建容器，不重新下载代码）"
  _write_state restarting "$target"
  (cd "$PROJECT_DIR" && TAG="$target" _do $COMPOSE_CMD up -d --force-recreate)
  if _verify_health && _verify_version "$target"; then
    _write_state done "$target"
  else
    _write_state failed "$target" "自愈重启失败"
  fi
  _cleanup_claimed "$claimed"; exit 0
fi

# ── 下载升级包 ────────────────────────────────────────────────────────────
# 地址优先用标记里带的（登记时写入 version.json 的 download_url），没有则按
# GitHub Release 约定拼。
_write_state pulling "$target"
if [ -z "$bundle_url" ]; then
  bundle_url="https://github.com/${ALB_GH_OWNER:-wuxin-gh}/${ALB_MAIN_REPO:-ai-lubricant}/releases/download/${target}/ai-lubricant-${target}.tar.gz"
fi
_log "下载升级包：$bundle_url"

work_dir="$PROJECT_DIR/.upgrade-work"
src="$work_dir/ai-lubricant"
download_ok=1

if [ "$DRY_RUN" = "1" ]; then
  _log "[dry-run] curl -fL --proxy <proxy> -o $work_dir/bundle.tar.gz $bundle_url"
  _log "[dry-run] tar -xzf → 校验关键文件"
  _log "[dry-run] rsync 包内容 → $PROJECT_DIR（排除 data/ .env .git/ .upgrade-work/）"
  _log "[dry-run] TAG=$target docker compose build && up -d"
else
  rm -rf "$work_dir"; mkdir -p "$work_dir"
  curl_args=(-fL --retry 3 --retry-delay 5 --connect-timeout 20 -o "$work_dir/bundle.tar.gz")
  if [ -n "$proxy_url" ] && [ "$proxy_url" != "none" ]; then
    curl_args+=(--proxy "$proxy_url")
  fi
  curl "${curl_args[@]}" "$bundle_url" 2>>"$LOG_FILE" || download_ok=0

  if [ "$download_ok" = "1" ]; then
    tar -xzf "$work_dir/bundle.tar.gz" -C "$work_dir" 2>>"$LOG_FILE" || download_ok=0
  fi

  if [ "$download_ok" = "1" ]; then
    # 预检包完整性：缺关键文件就拒，不冒险覆盖现役
    for f in main.py requirements.txt Dockerfile node_server user-frontend/dist/index.html; do
      if [ ! -e "$src/$f" ]; then
        _rollback "$target" "$old" "升级包残缺：缺 $f"
        _cleanup_claimed "$claimed"; exit 0
      fi
    done
    _log "包校验通过（VERSION=$(cat "$src/VERSION" 2>/dev/null | tr -d '\r\n' || echo '?')）"
  else
    _rollback "$target" "$old" "升级包下载/解压失败"
    _cleanup_claimed "$claimed"; exit 0
  fi
fi

# ── 应用：把包内容同步到项目目录 ──────────────────────────────────────────
# 包是完整代码快照；rsync 覆盖后项目目录 = 该版本代码 + 本地数据/配置。
# 排除项是「不该被包覆盖」的东西：运行时数据、本地 .env、git 元数据、工作目录。
_write_state installing "$target"
if [ "$DRY_RUN" != "1" ]; then
  if ! rsync -a --delete \
      --exclude 'data/' --exclude '.env' --exclude '.git/' \
      --exclude '.upgrade-work/' --exclude 'node_modules/' \
      "$src/" "$PROJECT_DIR/" 2>>"$LOG_FILE"; then
    _rollback "$target" "$old" "代码同步失败"
    _cleanup_claimed "$claimed"; exit 0
  fi
  _log "代码已同步到 $PROJECT_DIR"
fi

# ── 构建 + 重建容器 ──────────────────────────────────────────────────────
# TAG 必须与 build 同源：镜像名是 ai-lubricant:${TAG}，build 与 up 用不同 TAG
# 会找不到镜像（曾写成 build 不传 TAG / up 传 TAG，升级必失败）。
(
  cd "$PROJECT_DIR"
  TAG="$target" _do $COMPOSE_CMD build
) || { _rollback "$target" "$old" "docker compose build 失败"; _cleanup_claimed "$claimed"; exit 0; }

_write_state restarting "$target"
(
  cd "$PROJECT_DIR"
  TAG="$target" _do $COMPOSE_CMD up -d
) || { _rollback "$target" "$old" "docker compose up 失败"; _cleanup_claimed "$claimed"; exit 0; }

# ── 验证：容器健康 + 版本回读 ────────────────────────────────────────────
_write_state verifying "$target"
if ! _verify_health; then
  _rollback "$target" "$old" "健康验证超时"
  _cleanup_claimed "$claimed"; exit 0
fi
if ! _verify_version "$target"; then
  _rollback "$target" "$old" "版本回读不符（容器仍在跑旧代码）"
  _cleanup_claimed "$claimed"; exit 0
fi

# 清理工作目录 + GC 旧镜像
if [ "$DRY_RUN" != "1" ]; then rm -rf "$work_dir"; fi
_gc_images "$target"

_write_state done "$target"
_cleanup_claimed "$claimed"
_log "✓ $old → $target 升级完成"
