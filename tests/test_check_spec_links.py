# -*- coding: utf-8 -*-
"""script/check_spec_links.py 的单元测试（人造 fixture：临时 .md + .py）。

覆盖：
  - 反引号符号引用：存在 = ok、不存在 = broken、多义 = unverified
  - 反引号文件引用：存在 = ok、不存在 = broken
  - `path/to/file.py::Symbol`：文件缺失 / 符号缺失 = broken
  - Tessl 风格 `[@test] path`：测试文件存在 = ok、不存在 = broken
  - frontmatter `targets:`：匹配 = ok、无匹配 = broken
  - 退出码：有坏链 = 非 0、无坏链 = 0
  - 只读保证：跑完不改任何文件
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "script"
sys.path.insert(0, str(SCRIPT_DIR))

import check_spec_links as csl  # noqa: E402


# --------------------------------------------------------------------------- #
# fixture：搭一个迷你仓库
# --------------------------------------------------------------------------- #

@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "code").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "sub").mkdir()

    (tmp_path / "code" / "widget.py").write_text(
        "class Widget:\n    def render(self):\n        return 1\n",
        encoding="utf-8")
    (tmp_path / "code" / "dup_a.py").write_text("class Dup:\n    pass\n", encoding="utf-8")
    (tmp_path / "code" / "dup_b.py").write_text("class Dup:\n    pass\n", encoding="utf-8")
    (tmp_path / "code" / "task_utils.py").write_text(
        "def create_task():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_widget.py").write_text(
        "def test_widget():\n    assert True\n", encoding="utf-8")
    return tmp_path


def write_doc(root: Path, body: str, rel: str = "docs/spec.md") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def check(root: Path, scan=("docs",)) -> dict:
    return csl.run(root, list(scan))


# --------------------------------------------------------------------------- #
# 符号引用
# --------------------------------------------------------------------------- #

def test_symbol_exists_ok(mini_repo):
    write_doc(mini_repo, "引用 `Widget` 类，见 `task_utils.create_task`。\n")
    r = check(mini_repo)
    syms = {x["raw"] for x in (r["broken"] + r["unverified"])}
    assert "Widget" not in syms, f"不应坏链: {syms}"
    assert r["ok"] >= 1


def test_symbol_missing_broken(mini_repo):
    write_doc(mini_repo, "调用 `definitely_missing_symbol`。\n")
    r = check(mini_repo)
    broken_raws = [x["raw"] for x in r["broken"]]
    assert "definitely_missing_symbol" in broken_raws
    assert r["broken"], "坏链应非空"


def test_symbol_ambiguous_unverified(mini_repo):
    write_doc(mini_repo, "`Dup` 是唯一裸名吗？不，dup_a / dup_b 各有一个。\n")
    r = check(mini_repo)
    unver = [x["raw"] for x in r["unverified"]]
    assert "Dup" in unver, f"多义应 unverified，实为 broken={[x['raw'] for x in r['broken']]}"
    assert "Dup" not in [x["raw"] for x in r["broken"]]


def test_plain_word_not_treated_as_symbol(mini_repo):
    """`id` / `status` / `true` 这类通用词不该被当成符号引用。"""
    write_doc(mini_repo, "字段 `id`、`status`、`true`。\n")
    r = check(mini_repo)
    assert r["total"] == 0, f"通用词不应算引用: {r}"


def test_trailing_underscore_prefix_skipped(mini_repo):
    """`mc_` 这类表前缀占位不是符号。"""
    write_doc(mini_repo, "表前缀 `mc_`。\n")
    r = check(mini_repo)
    assert r["total"] == 0


# --------------------------------------------------------------------------- #
# 文件引用
# --------------------------------------------------------------------------- #

def test_file_path_exists_ok(mini_repo):
    write_doc(mini_repo, "见 `code/widget.py` 和 `tests/test_widget.py`。\n")
    r = check(mini_repo)
    assert not r["broken"], f"文件都在: {r['broken']}"


def test_file_path_missing_broken(mini_repo):
    write_doc(mini_repo, "见 `code/nope.py`。\n")
    r = check(mini_repo)
    assert any(x["kind"] == "path" and "nope.py" in x["raw"] for x in r["broken"])


def test_relative_path_resolves_against_md_dir(mini_repo):
    """`../code/widget.py` 相对 docs/spec.md 解析。"""
    write_doc(mini_repo, "见 `../code/widget.py`。\n")
    r = check(mini_repo)
    assert not r["broken"], f"相对 md 目录解析失败: {r['broken']}"


# --------------------------------------------------------------------------- #
# path::Symbol
# --------------------------------------------------------------------------- #

def test_path_symbol_ok(mini_repo):
    write_doc(mini_repo, "见 `code/widget.py::Widget`。\n")
    r = check(mini_repo)
    assert not r["broken"], r["broken"]


def test_path_symbol_missing_file_broken(mini_repo):
    write_doc(mini_repo, "见 `code/ghost.py::Widget`。\n")
    r = check(mini_repo)
    assert any(x["kind"] == "path_symbol" for x in r["broken"])


def test_path_symbol_missing_symbol_broken(mini_repo):
    write_doc(mini_repo, "见 `code/widget.py::GhostClass`。\n")
    r = check(mini_repo)
    assert any(x["kind"] == "path_symbol" for x in r["broken"])


# --------------------------------------------------------------------------- #
# [@test]
# --------------------------------------------------------------------------- #

def test_at_test_file_exists_ok(mini_repo):
    write_doc(mini_repo, "需求由 `[@test] tests/test_widget.py` 覆盖。\n")
    r = check(mini_repo)
    assert not r["broken"], f"`[@test]` 文件存在却报坏链: {r['broken']}"
    assert r["total"] == 1 and r["ok"] == 1, f"应恰好 1 条 ok 的 [@test] 引用: {r}"


def test_at_test_file_missing_broken(mini_repo):
    write_doc(mini_repo, "需求由 `[@test] tests/test_missing.py` 覆盖。\n")
    r = check(mini_repo)
    assert any(x["kind"] == "test" and "test_missing" in x["target"] for x in r["broken"])


def test_at_test_relative_to_md_dir(mini_repo):
    write_doc(mini_repo, "需求由 `[@test] ../tests/test_widget.py` 覆盖。\n")
    r = check(mini_repo)
    assert not r["broken"], f"相对 md 目录解析失败: {r['broken']}"


def test_at_test_placeholder_skipped(mini_repo):
    """文档里讲语法用的 `[@test] <路径>` 是占位符，不该当真路径校验。"""
    write_doc(mini_repo, "语法：`[@test] <路径>`，例如 `[@test] ../tests/test_x.py`。\n")
    r = check(mini_repo)
    assert not r["broken"], f"占位符不应算坏链: {r['broken']}"


# --------------------------------------------------------------------------- #
# frontmatter targets:
# --------------------------------------------------------------------------- #

def test_targets_glob_ok(mini_repo):
    write_doc(mini_repo, "---\ntargets:\n  - code/*.py\n---\n# spec\n")
    r = check(mini_repo)
    assert not r["broken"], r["broken"]
    assert r["ok"] >= 1


def test_targets_no_match_broken(mini_repo):
    write_doc(mini_repo, "---\ntargets:\n  - missing/**/*.py\n---\n# spec\n")
    r = check(mini_repo)
    assert any(x["kind"] == "target" for x in r["broken"])


def test_targets_inline_list(mini_repo):
    write_doc(mini_repo, "---\ntargets: [code/widget.py, code/task_utils.py]\n---\n# spec\n")
    r = check(mini_repo)
    assert not r["broken"], r["broken"]


# --------------------------------------------------------------------------- #
# 输出结构 / 退出码
# --------------------------------------------------------------------------- #

def test_json_shape(mini_repo):
    write_doc(mini_repo, "`Widget` 存在；`ghost`_missing 不存在。\n")
    r = check(mini_repo)
    for key in ("total", "ok", "broken", "unverified"):
        assert key in r, f"缺少字段 {key}"
    assert isinstance(r["broken"], list)
    assert isinstance(r["unverified"], list)
    assert r["ok"] + len(r["broken"]) + len(r["unverified"]) == r["total"]


def test_exit_code_nonzero_when_broken(mini_repo, capsys):
    write_doc(mini_repo, "调用 `no_such_symbol_here`。\n")
    rc = csl.main(["--root", str(mini_repo), "docs", "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["broken"]
    assert rc == 1, "有坏链时退出码应为非 0"


def test_exit_code_zero_when_clean(mini_repo, capsys):
    write_doc(mini_repo, "见 `code/widget.py`，以及 `Widget`。\n")
    rc = csl.main(["--root", str(mini_repo), "docs", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert not parsed["broken"], parsed["broken"]
    assert rc == 0, "无坏链时退出码应为 0"


def test_checker_is_readonly(mini_repo):
    """只读：跑完所有文件内容不变。"""
    write_doc(mini_repo, "`Widget` 与 `code/widget.py`。\n")
    before = {p: p.read_bytes() for p in mini_repo.rglob("*") if p.is_file()}
    check(mini_repo)
    after = {p: p.read_bytes() for p in mini_repo.rglob("*") if p.is_file()}
    assert before == after, "校验器改动了文件！"


def test_skips_junk_dirs(mini_repo):
    """符号索引不该 descend 进 node_modules / .git 这类目录。"""
    junk = mini_repo / "node_modules"
    junk.mkdir()
    (junk / "fake.py").write_text("class Fake:\n    pass\n", encoding="utf-8")
    write_doc(mini_repo, "`Fake` 应该查不到（node_modules 被跳过）。\n")
    r = check(mini_repo)
    assert any(x["raw"] == "Fake" for x in r["broken"]), r
