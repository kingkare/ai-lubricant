#!/usr/bin/env bash
# check_spec_links.sh —— 文档链接校验的 bash 包装（Tessl check-spec-links.sh 的对应物）。
#
# 设计依据：docs/ai-testing-platform/plans/09-knowledge-system.md §4.6.5
#   扫白皮书 / 计划里的反引号符号引用 + `[@test]` 测试引用 + frontmatter targets:，
#   校验文件与符号存在，坏链 → exit 非 0（可直接进 CI）。
#
# 默认扫描：docs/project-system/whitepapers/ + docs/ai-testing-platform/
#
# 用法：
#   bash script/check_spec_links.sh                        # 默认范围
#   bash script/check_spec_links.sh docs/                  # 自定义范围（位置参数）
#   bash script/check_spec_links.sh --max-report 20        # 透传选项给 python 校验器
#   bash script/check_spec_links.sh --json                 # 只出 JSON
#   PYTHON=python3.11 bash script/check_spec_links.sh      # 指定解释器
# 约定：位置参数只在「看起来像路径」时才算扫描范围（含 / 、以 .md 结尾、或真实存在），
#      以免把 --max-report 20 的值 20 当成目录。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/check_spec_links.py"

# --- 选 python 解释器（Git Bash 里 python3 常是 Windows Store 存根，须实测可用）---
PY_BIN="${PYTHON:-}"
if [ -z "${PY_BIN}" ]; then
  for cand in python python3 py; do
    if command -v "${cand}" >/dev/null 2>&1; then
      if "${cand}" -c "print('ok')" >/dev/null 2>&1; then
        PY_BIN="${cand}"; break
      fi
    fi
  done
fi
if [ -z "${PY_BIN}" ]; then
  echo "错误：找不到 python 解释器（可用 PYTHON=/path/to/python 指定）" >&2
  exit 2
fi

if [ ! -f "${PY_SCRIPT}" ]; then
  echo "错误：找不到校验器 ${PY_SCRIPT}" >&2
  exit 2
fi

DEFAULT_DIRS=(
  "docs/project-system/whitepapers"
  "docs/ai-testing-platform"
)

# --- 分流：路径参数 vs 透传选项 ---
# 需要取下一个参数作值的选项（其余 - 开头的都是布尔开关）
opt_needs_value() {
  case "$1" in
    --max-report|--out|--root) return 0 ;;
    *) return 1 ;;
  esac
}

TARGETS=()
PASSTHRU=()
want_value=0
for a in "$@"; do
  if [ "${want_value}" -eq 1 ]; then
    PASSTHRU+=("$a"); want_value=0; continue
  fi
  case "$a" in
    -*)
      PASSTHRU+=("$a")
      case "$a" in
        *=*) : ;;                       # --foo=bar 自带值
        *) opt_needs_value "$a" && want_value=1 ;;
      esac
      ;;
    *)
      TARGETS+=("$a")
      ;;
  esac
done
# 结尾悬空了一个「要值的选项」→ 说明参数没给全
if [ "${want_value}" -eq 1 ]; then
  echo "错误：最后一个选项缺少参数值" >&2
  exit 2
fi

# --- 决定扫描范围 ---
if [ "${#TARGETS[@]}" -eq 0 ]; then
  SCOPE=("${DEFAULT_DIRS[@]}")
  SCOPE_LABEL="${DEFAULT_DIRS[*]}"
else
  SCOPE=("${TARGETS[@]}")
  SCOPE_LABEL="${TARGETS[*]}"
fi

# --- 是否只输出 JSON（跳过 banner，保证 stdout 干净）---
JSON_ONLY=0
for a in "$@" "${PASSTHRU[@]:-}"; do
  [ "$a" = "--json" ] && JSON_ONLY=1
done
if [ "${JSON_ONLY}" -eq 0 ]; then
  echo "== check_spec_links =="
  echo "root   : ${ROOT}"
  echo "python : ${PY_BIN}"
  echo "范围   : ${SCOPE_LABEL}"
  [ "${#PASSTHRU[@]}" -gt 0 ] && echo "选项   : ${PASSTHRU[*]}"
  echo ""
fi

"${PY_BIN}" "${PY_SCRIPT}" --root "${ROOT}" "${SCOPE[@]}" "${PASSTHRU[@]}"
rc=$?

if [ "${JSON_ONLY}" -eq 0 ]; then
  echo ""
  if [ "${rc}" -eq 0 ]; then
    echo ">>> PASS：文档引用全部可解析。"
  elif [ "${rc}" -eq 1 ]; then
    echo ">>> FAIL：发现坏链（文档引用了不存在的文件/符号，或引用了尚未实现的符号）。"
    echo "    修文档（改成真实路径/符号，或去掉多余反引号）或补实现后再提交。"
  else
    echo ">>> 校验器异常退出（exit ${rc}）。"
  fi
fi
exit "${rc}"
