#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_spec_links.py —— 白皮书/计划文档的「链接校验器」（Tessl check-spec-links.sh 的符号级版）。

设计依据：docs/ai-testing-platform/plans/09-knowledge-system.md §4.6.5
  - Tessl 的 `targets:`（frontmatter，spec→code）+ `[@test] <路径>`（正文行内，spec→test）
  - Graphify 的「Markdown 反引号符号引用 → 文档→代码边」思路（本脚本只做**校验**，不建图）

它补的两个缺口（同文档 §4.6.3）：
  1. 白皮书规则零代码符号引用 → 现在可以把 `` `ClassName.method` `` 写进文档并被校验
  2. 325 个 pytest 用例零关联 → 现在可以用 `[@test] tests/test_x.py` 把用例挂到文档上

**只读**：不修改任何文档，只扫描 + 报告。
**纯 stdlib**：re / os / sys / json / glob / argparse / pathlib。

输出：结构化 JSON（{total, ok, broken[], unverified[]}）+ 人类可读报告。
退出码：broken > 0 → 非 0（可直接进 CI）。

用法：
    python script/check_spec_links.py                    # 扫 docs/
    python script/check_spec_links.py docs/project-system docs/ai-testing-platform
    python script/check_spec_links.py --json             # 只输出 JSON
    python script/check_spec_links.py --root . --max-report 30
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量 / 配置
# --------------------------------------------------------------------------- #

#: 路径引用可能带的文件后缀（用于区分「文件引用」与「点号符号引用」）
FILE_EXTS = {
    "py", "sh", "bash", "zsh", "sql", "toml", "yaml", "yml", "ini", "cfg",
    "json", "json5", "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue",
    "go", "rs", "java", "kt", "c", "h", "cpp", "hpp", "cc", "cs", "rb", "php",
    "md", "mdx", "txt", "html", "htm", "css", "scss", "less",
    "proto", "env", "lock", "xml", "csv", "conf",
}

#: 像域名/URL 的段（避免把 `mirrors.aliyun.com` 当成 `ClassName.method`）
TLDS = {"com", "org", "net", "io", "cn", "dev", "app", "co", "me", "info",
        "xyz", "tech", "top", "ai", "sh", "us", "uk", "edu", "gov", "cloud"}

#: 构建符号索引时跳过的目录名（构建产物 / 三方依赖 / 本地工具 / 快照备份）
#: 以 "." 开头的目录（.claude / .codegraph / .venv / .worktrees / .tmp* …）一律跳过，
#: 它们是工具与缓存目录，里面的 .py 常是仓库的重复副本（会污染符号计数）。
SKIP_DIRS = {
    "node_modules", "bower_components", "site-packages", "egg-info",
    "__pycache__", "dist", "build", "out", "target", "vendor",
    "venv", "env", "archive", "deleted_backups", "backups",
    "logs", "data", "coverage", "htmlcov",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
}


def _should_skip_dir(name: str, extra: set[str] | None = None) -> bool:
    if name.startswith(".") or name in SKIP_DIRS:
        return True
    return bool(extra) and name in extra



#: 反引号代码片段：`...`
RE_BACKTICK = re.compile(r"`([^`\n]+)`")
#: `[@test] <路径>`（正文行内；允许被反引号包裹，路径止于空白/反引号/括号）
RE_AT_TEST = re.compile(r"\[@test\]\s*`?([^\s`\)\]，。；]+)")
#: 文件内符号定义：class X / def Y / async def Y（含缩进的方法）
RE_SYMBOL_DEF = re.compile(r"^\s*(?:class|def)\s+([A-Za-z_]\w*)", re.M)
RE_SYMBOL_DEF_ASYNC = re.compile(r"^\s*async\s+def\s+([A-Za-z_]\w*)", re.M)
#: 符号在指定文件里的出现（用于 path::Symbol / module.Symbol 的精确校验）
def _sym_in_text(text: str, name: str) -> bool:
    return bool(re.search(r"(?:^|\s)(?:class|def|async\s+def)\s+" + re.escape(name) + r"\b", text))

#: 裸标识符
RE_IDENT = re.compile(r"^[A-Za-z_]\w*$")
#: 点号符号：a.b.c
RE_DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
#: 路径限定符号：path.py::Symbol::method
RE_PATH_SYM = re.compile(r"^([\w./\-]+\.\w+)::([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)$")
#: 看起来像路径（含 / 或以已知后缀结尾）
RE_PATHISH = re.compile(r"^[\w./\-]+$")


# --------------------------------------------------------------------------- #
# 符号索引
# --------------------------------------------------------------------------- #

class SymbolIndex:
    """仓库 `.py` 文件的 `class X` / `def Y` 名字索引（grep 风格，不要求精确）。"""

    def __init__(self, root: Path):
        self.root = root
        self._classes: dict[str, set[str]] = {}
        self._funcs: dict[str, set[str]] = {}
        self._modules: set[str] = set()          # .py 文件名（去后缀），用于「疑似模块名」判定
        self._file_cache: dict[str, str | None] = {}
        self._built = False

    # -- 构建 ------------------------------------------------------------- #

    def build(self) -> None:
        if self._built:
            return
        self._built = True
        for dirpath, dirnames, filenames in os.walk(self.root):
            # 原地裁剪，避免 descend
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                self._modules.add(fn[:-3])
                text = self._read(rel)
                if text is None:
                    continue
                for m in RE_SYMBOL_DEF.finditer(text):
                    name = m.group(1)
                    bucket = self._classes if m.group(0).lstrip().startswith("class") else self._funcs
                    bucket.setdefault(name, set()).add(rel)
                for m in RE_SYMBOL_DEF_ASYNC.finditer(text):
                    self._funcs.setdefault(m.group(1), set()).add(rel)

    def _read(self, rel: str) -> str | None:
        if rel in self._file_cache:
            return self._file_cache[rel]
        try:
            text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = None
        self._file_cache[rel] = text
        return text

    # -- 查询 ------------------------------------------------------------- #

    def lookup(self, name: str) -> set[str]:
        """返回定义了 `class name` 或 `def name` 的文件集合（相对 root）。"""
        self.build()
        return self._classes.get(name, set()) | self._funcs.get(name, set())

    def is_module(self, name: str) -> bool:
        """是否存在同名 `.py` 模块（文档常直接写模块名而非符号）。"""
        self.build()
        return name in self._modules

    def defined_in(self, rel: str, name: str) -> bool:
        text = self._read(rel)
        return text is not None and _sym_in_text(text, name)


# --------------------------------------------------------------------------- #
# 引用抽取
# --------------------------------------------------------------------------- #

class Ref:
    __slots__ = ("file", "line", "raw", "kind", "target", "symbols", "reason", "status")

    def __init__(self, file: str, line: int, raw: str, kind: str, target: str,
                 symbols: tuple[str, ...] = ()):
        self.file = file
        self.line = line
        self.raw = raw
        self.kind = kind          # path | path_symbol | symbol | test | target
        self.target = target      # 解析用的目标串
        self.symbols = symbols    # 需要校验的符号名
        self.reason = ""          # 校验后填充
        self.status = "pending"   # ok | broken | unverified

    def to_dict(self) -> dict:
        d = {
            "file": self.file,
            "line": self.line,
            "raw": self.raw,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
        }
        if self.symbols:
            d["symbols"] = list(self.symbols)
        if self.reason:
            d["reason"] = self.reason
        return d


def _strip_wrappers(span: str) -> str:
    """去掉包裹/尾随的括号与调用参数：`Foo(bar=1)` → `Foo`。"""
    s = span.strip()
    # 去掉尾随的 (...) / [...] 内容（调用/参数）
    s = re.sub(r"\([^()]*\)\s*$", "", s).strip()
    s = re.sub(r"\[[^\[\]]*\]\s*$", "", s).strip()
    return s


def _looks_like_domain(s: str) -> bool:
    parts = s.split(".")
    if len(parts) < 2:
        return False
    if parts[-1] in TLDS and all(p.islower() and "_" not in p for p in parts):
        return True
    return False


def _is_placeholder(s: str) -> bool:
    """判断是否为说明性占位符（不是真路径）：`<路径>` 或文档里的示例 `../tests/test_x.py`。"""
    if "<" in s or ">" in s:
        return True
    if re.search(r"[一-鿿]", s):  # 含 CJK：是说明文字
        return True
    # 09 号计划文档里 `[@test]` 的示例路径（test_x.py / test_y.py …）
    stem = s.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem in {"test_x", "test_y", "test_z", "test_foo", "test_bar"}:
        return True
    return False



def _classify_backtick(span: str) -> Ref | None:
    """把一个反引号片段分类为引用候选；不是引用的返回 None。"""
    raw = span
    s = _strip_wrappers(span)
    if not s:
        return None

    # 1) path::Symbol::method
    m = RE_PATH_SYM.match(s)
    if m:
        path, syms = m.group(1), tuple(m.group(2).split("::"))
        return Ref("", 0, raw, "path_symbol", path, syms)

    # 2) 纯文件/目录路径
    if _is_path(s):
        return Ref("", 0, raw, "path", s)

    # 3) 点号符号 a.b.c（排除域名）
    if RE_DOTTED.match(s) and not _looks_like_domain(s):
        return Ref("", 0, raw, "symbol", s, (s.split(".")[-1],))

    # 4) 裸标识符：只在「像符号」时收（含下划线，或 CamelCase）
    #    避免把 `id` / `status` / `code` / `true` 这类通用词当成符号
    if RE_IDENT.match(s):
        if s.endswith("_") and not s.startswith("_"):
            return None  # `mc_` / `raw_` 这类前缀占位不是符号
        has_underscore = "_" in s
        is_camel = any(c.isupper() for c in s)
        if has_underscore or is_camel:
            return Ref("", 0, raw, "symbol", s, (s,))
    return None


def _is_path(s: str) -> bool:
    """判断是否像文件/目录路径。"""
    if "://" in s or s.startswith("http"):
        return False
    if not RE_PATHISH.match(s):
        return False
    core = s.rstrip("/")
    if not core:
        return False
    if "/" in core:
        # 有斜杠：只要末段带已知后缀，或整体以 / 结尾（目录），就算路径
        last = core.split("/")[-1]
        if "." in last and last.rsplit(".", 1)[-1].lower() in FILE_EXTS:
            return True
        if s.endswith("/"):
            return True
        # 形如 providers/custom 也可能指模块，交给符号解析；这里不算路径
        return False
    # 无斜杠：必须带已知后缀才算文件（避免把 `foo.bar` 误判成文件）
    if "." in core and core.rsplit(".", 1)[-1].lower() in FILE_EXTS:
        return True
    return False


def extract_refs(md_path: Path, rel: str) -> list[Ref]:
    """从单个 md 文件抽取全部引用候选。"""
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    refs: list[Ref] = []

    # --- frontmatter targets: （Tessl spec→code）---
    refs.extend(_extract_targets(text, rel))

    for i, line in enumerate(lines, start=1):
        # --- [@test] <路径>（spec→test）---
        for m in RE_AT_TEST.finditer(line):
            p = m.group(1).strip().rstrip("。，,;；")
            if not p or _is_placeholder(p):
                continue  # 跳过 `<路径>` / 说明性占位
            r = Ref(rel, i, line.strip()[:200], "test", p)
            refs.append(r)
        # --- 反引号符号/路径引用 ---
        for m in RE_BACKTICK.finditer(line):
            span = m.group(1)
            if span.strip().startswith("[@test]"):
                continue  # 已由 [@test] 分支处理
            r = _classify_backtick(span)
            if r is not None:
                r.file = rel
                r.line = i
                refs.append(r)
    return refs


def _extract_targets(text: str, rel: str) -> list[Ref]:
    """解析文件头 `--- ... ---` frontmatter 里的 `targets:` 列表（Tessl 语法）。"""
    out: list[Ref] = []
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    fm = text[3:end]
    in_targets = False
    for ln in fm.splitlines():
        stripped = ln.strip()
        if re.match(r"^targets\s*:", stripped):
            in_targets = True
            inline = stripped.split(":", 1)[1].strip()
            if inline.startswith("[") and inline.endswith("]"):
                for item in inline[1:-1].split(","):
                    item = item.strip().strip("'\"")
                    if item:
                        out.append(Ref(rel, 1, f"targets: {item}", "target", item))
            continue
        if in_targets:
            if stripped.startswith("- "):
                item = stripped[2:].strip().strip("'\"")
                if item:
                    out.append(Ref(rel, 1, f"targets: {item}", "target", item))
            elif stripped and not stripped.startswith("#"):
                in_targets = False
    return out


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

class Checker:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.index = SymbolIndex(self.root)
        self._name_index: dict[str, list[str]] | None = None

    # -- 路径解析 --------------------------------------------------------- #

    def resolve_path(self, raw: str, md_rel: str) -> Path | None:
        """把文档里的路径解析成绝对路径（先相对 md 目录，再相对仓库根，
        最后尝试全仓库按文件名匹配——裸文件名如 `code-map.md` 可能在不同子目录）。"""
        s = raw.strip().strip("`").rstrip("。，,;；")
        s = s.split("#", 1)[0]          # 去 #L10 锚点
        s = re.sub(r":\d+(-\d+)?$", "", s)  # 去 :12-34 行区间（codetospec 风格）
        s = s.rstrip("/")
        if not s:
            return None
        md_dir = self._md_dir(md_rel)
        candidates = []
        if s.startswith("/"):
            candidates.append(self.root / s.lstrip("/"))
        else:
            candidates.append((md_dir / s))
            candidates.append(self.root / s)
        for c in candidates:
            try:
                if c.exists():
                    return c
            except OSError:
                continue
        # 全仓库按文件名匹配（跳过 junk 目录）
        return self._find_by_basename(s)

    def _find_by_basename(self, s: str) -> Path | None:
        """在仓库内找 `name.ext`（不含目录部分）；唯一命中返回，多个/没有返回 None。"""
        name = Path(s.replace("\\", "/")).name
        if not name:
            return None
        hits = self._basename_index().get(name, [])
        if len(hits) == 1:
            return self.root / hits[0]
        return None  # 0 个或同名多个（歧义）→ 不算 ok

    def _basename_index(self) -> dict[str, list[str]]:
        """全仓库文件名 → 相对路径列表（一次 walk，缓存复用）。"""
        if self._name_index is not None:
            return self._name_index
        idx: dict[str, list[str]] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
            rel_dir = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            prefix = "" if rel_dir == "." else rel_dir + "/"
            for fn in filenames:
                idx.setdefault(fn, []).append(prefix + fn)
        self._name_index = idx
        return idx

    def _rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    # -- 单条校验 --------------------------------------------------------- #

    def check(self, r: Ref) -> None:
        if r.kind in ("path", "target"):
            self._check_path(r)
        elif r.kind == "path_symbol":
            self._check_path_symbol(r)
        elif r.kind == "symbol":
            self._check_symbol(r)
        elif r.kind == "test":
            self._check_test(r)
        else:
            r.status = "unverified"
            r.reason = "unknown kind"

    def _md_dir(self, md_rel: str) -> Path:
        p = Path(md_rel)
        return (p if p.is_absolute() else (self.root / p)).parent

    def _check_path(self, r: Ref) -> None:
        target = r.target
        if r.kind == "target" and any(ch in target for ch in "*?["):
            # glob：展开（相对 md 目录 + 仓库根）
            md_dir = self._md_dir(r.file)
            pats = [str(md_dir / target), str(self.root / target)]
            hits = []
            for pat in pats:
                hits.extend(_glob.glob(pat, recursive=True))
            if hits:
                r.status = "ok"
                r.reason = f"glob 命中 {len(hits)} 个"
            else:
                r.status = "broken"
                r.reason = "glob 无匹配"
            return
        p = self.resolve_path(target, r.file)
        if p is not None and p.exists():
            r.status = "ok"
        else:
            r.status = "broken"
            r.reason = f"路径不存在: {target}"

    def _check_path_symbol(self, r: Ref) -> None:
        p = self.resolve_path(r.target, r.file)
        if p is None or not p.exists():
            r.status = "broken"
            r.reason = f"文件不存在: {r.target}"
            return
        rel = self._rel(p)
        missing = [s for s in r.symbols if not self.index.defined_in(rel, s)]
        if missing:
            r.status = "broken"
            r.reason = f"{rel} 中未定义: {', '.join(missing)}"
        else:
            r.status = "ok"

    def _check_symbol(self, r: Ref) -> None:
        name = r.symbols[-1] if r.symbols else r.target
        # 点号符号 a.b.c：先尝试把 a/b 当模块定位（精确）
        if r.kind == "symbol" and "." in r.target:
            mod = r.target.rsplit(".", 1)[0].replace(".", "/")
            for cand in (f"{mod}.py", f"{mod}/__init__.py"):
                if (self.root / cand).exists():
                    if self.index.defined_in(cand, name):
                        r.status = "ok"
                        r.reason = f"命中 {cand}"
                        return
                    r.status = "broken"
                    r.reason = f"{cand} 中未定义 {name}"
                    return
        hits = self.index.lookup(name)
        if len(hits) == 1:
            r.status = "ok"
            r.reason = f"命中 {next(iter(hits))}"
        elif len(hits) == 0:
            # class/def 找不到 → 看是不是模块名（如 `file_write_approval` 指 agent/file_write_approval.py）
            if self.index.is_module(name):
                r.status = "unverified"
                r.reason = f"同名模块存在 {name}.py，但无 class/def（疑似模块引用）"
            else:
                r.status = "broken"
                r.reason = f"仓库内无 class/def {name}"
        else:
            r.status = "unverified"
            r.reason = f"歧义：{len(hits)} 个文件定义 {name}"

    def _check_test(self, r: Ref) -> None:
        p = self.resolve_path(r.target, r.file)
        if p is not None and p.exists():
            r.status = "ok"
        else:
            r.status = "broken"
            r.reason = f"测试文件不存在: {r.target}"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def collect_md_files(root: Path, scan_dirs: list[str]) -> list[tuple[Path, str]]:
    """返回 [(绝对路径, 相对 root 的 posix 路径)]；扫描目录可位于 root 之外，
    此时用「相对该扫描目录」的路径兜底。"""
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def key_for(p: Path) -> str:
        try:
            return p.resolve().relative_to(root).as_posix()
        except ValueError:
            return p.resolve().as_posix()

    for d in scan_dirs:
        base = Path(d)
        if not base.is_absolute():
            base = root / d
        if base.is_file() and base.suffix == ".md":
            rel = key_for(base)
            if rel not in seen:
                seen.add(rel)
                out.append((base, rel))
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if not _should_skip_dir(x)]
            for fn in sorted(filenames):
                if not fn.endswith(".md"):
                    continue
                full = Path(dirpath) / fn
                rel = key_for(full)
                if rel not in seen:
                    seen.add(rel)
                    out.append((full, rel))
    out.sort(key=lambda t: t[1])
    return out


def run(root: Path, scan_dirs: list[str]) -> dict:
    checker = Checker(root)
    files = collect_md_files(root, scan_dirs)
    all_refs: list[Ref] = []
    for path, rel in files:
        all_refs.extend(extract_refs(path, rel))
    for r in all_refs:
        checker.check(r)

    broken = [r for r in all_refs if r.status == "broken"]
    unverified = [r for r in all_refs if r.status == "unverified"]
    ok = [r for r in all_refs if r.status == "ok"]

    # 按 kind 统计
    by_kind: dict[str, int] = {}
    for r in all_refs:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1

    return {
        "root": root.as_posix(),
        "scan_dirs": list(scan_dirs),
        "scanned_files": [rel for _, rel in files],
        "total": len(all_refs),
        "ok": len(ok),
        "broken": [r.to_dict() for r in broken],
        "unverified": [r.to_dict() for r in unverified],
        "by_kind": by_kind,
    }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def _redact(s: str) -> str:
    """防御性脱敏：万一引用里带疑似凭据（sk-xxx / 长 hex / token=…），打码。"""
    s = re.sub(r"\b(sk|pk|ghp|gho|xox[baprs])-[A-Za-z0-9_\-]{8,}", r"\1-***", s)
    s = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "***", s)
    s = re.sub(r"(?i)(token|secret|password|api[_-]?key)\s*[=:]\s*\S+", r"\1=***", s)
    return s


def human_report(result: dict, max_report: int = 100) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    add("  文档链接校验报告 (check_spec_links)")
    add("=" * 78)
    add(f"仓库根    : {result['root']}")
    add(f"扫描文件  : {len(result['scanned_files'])} 个 .md")
    add("")
    add(f"引用总数  : {result['total']}")
    add(f"  ✅ ok        : {result['ok']}")
    add(f"  ❌ broken    : {len(result['broken'])}")
    add(f"  ⚠️  unverified: {len(result['unverified'])}   (歧义/无法判定，不计入坏链)")
    add("")
    bk = result.get("by_kind", {})
    if bk:
        add("按类型：" + "  ".join(f"{k}={v}" for k, v in sorted(bk.items())))
        add("")

    def dump(title: str, items: list[dict], emoji: str) -> None:
        if not items:
            return
        add(f"--- {emoji} {title} ({len(items)}) ---")
        for it in items[:max_report]:
            loc = f"{it['file']}:{it['line']}"
            sym = ""
            if it.get("symbols"):
                sym = " [" + ",".join(it["symbols"]) + "]"
            add(f"  {loc}  {it['kind']}{sym}  `{_redact(it['raw'])}`")
            if it.get("reason"):
                add(f"      → {_redact(it['reason'])}")
        if len(items) > max_report:
            add(f"  ... 另有 {len(items) - max_report} 条（用 --max-report 调大 / --json 看全量）")
        add("")

    dump("坏链 broken", result["broken"], "❌")
    dump("待定 unverified", result["unverified"], "⚠️")

    verdict = "FAIL" if result["broken"] else "PASS"
    add("=" * 78)
    add(f"  结论: {verdict}   (broken={len(result['broken'])})")
    add("=" * 78)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，报告含中文/符号 → 强制 UTF-8，避免 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    here = Path(__file__).resolve()
    default_root = here.parent.parent  # script/ 的上一级

    ap = argparse.ArgumentParser(
        description="校验文档里的代码/测试引用（Tessl check-spec-links.sh 的符号级版）")
    ap.add_argument("dirs", nargs="*", default=None,
                    help="要扫描的目录/文件（默认: docs/）")
    ap.add_argument("--root", default=str(default_root),
                    help="仓库根目录（默认: 脚本上一级）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--max-report", type=int, default=100, help="人类报告每类最多显示条数")
    ap.add_argument("--out", default=None, help="同时把 JSON 写到该文件")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    scan = args.dirs or ["docs"]

    result = run(root, scan)

    if args.out:
        try:
            Path(args.out).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[warn] 无法写 --out: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(human_report(result, max_report=args.max_report))

    return 1 if result["broken"] else 0


if __name__ == "__main__":
    sys.exit(main())
