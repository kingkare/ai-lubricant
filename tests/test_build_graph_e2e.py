"""端到端构建验证 —— 走 UI 完全相同的代码路径，真 git + 真子模块 + 真 graphify + 真入库。

``project.version.build_graph`` 一次调用验四件事（全是生产真实形态）：
1. 脏 ref（版本行存 main、远端只有 master）→ snapshot 步自愈，步骤回报 ref_corrected；
2. Graphify 真抽取（--code-only，不连 LLM）→ extract 步 done + anchors>0；
3. 真 upsert 进 mc_knowledge_anchor/item/edge（sqlite）→ upsert 步 done；
4. 一个仓坏（URL 不存在）+ 一个仓好 → status=partial，坏仓 failed_step=snapshot 且
   error 非空（就是弹框要显示的日志）——**不阻断好仓**。

子模块用本地仓库模拟（生产 ai-lubricant 大部分代码在子模块里，必须覆盖这条路径）。

没装隔离 graphify venv 时整组跳过。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.knowledge import models as km  # noqa: E402,F401  (注册进 app)
from testing_platform.projects import git_source as gs  # noqa: E402
from testing_platform.projects import service as psvc  # noqa: E402
from testing_platform.surface import methods as sm  # noqa: E402

_REAL_RUN_GIT = gs._run_git

_VENV = os.environ.get("GRAPHIFY_VENV") or str(Path(_PROJ) / ".venv-graphify")
needs_graphify = pytest.mark.skipif(
    not Path(_VENV).is_dir(), reason="隔离 graphify venv 不存在（GRAPHIFY_VENV 未配置）"
)


def _git(*args: str, cwd: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {out}"
    return out


_PKG_CODE = """\
class OrderService:
    def create(self, item):
        return self.reserve(item)

    def reserve(self, item):
        return {"ok": True, "item": item}

    def cancel(self, order_id):
        return self.find(order_id)

    def find(self, order_id):
        return {"id": order_id}
"""

_SUB_PKG_CODE = """\
def helper(a, b):
    return a + b

def caller(a):
    return helper(a, 1)
"""


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.projects.models",
                "testing_platform.assets.models",
                "testing_platform.knowledge.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _make_origin(with_master_only: bool = True) -> str:
    """建一个真实 git 仓库：默认分支 master，带子模块（模拟生产布局）。"""
    root = Path(_make_origin.tmp_root) / f"origin_{uuid.uuid4().hex[:8]}"
    main = root / "mainrepo"
    main.mkdir(parents=True)

    # 子模块仓库（生产大部分代码在这类目录里）
    sub = root / "subrepo"
    sub.mkdir()
    _git("init", "-b", "master", cwd=str(sub))
    _git("config", "user.email", "t@t.t", cwd=str(sub))
    _git("config", "user.name", "t", cwd=str(sub))
    (sub / "subpkg").mkdir()
    (sub / "subpkg" / "helpers.py").write_text(_SUB_PKG_CODE, encoding="utf-8")
    _git("add", ".", cwd=str(sub))
    _git("commit", "-m", "sub init", cwd=str(sub))

    # 主仓库
    _git("init", "-b", "master" if with_master_only else "main", cwd=str(main))
    _git("config", "user.email", "t@t.t", cwd=str(main))
    _git("config", "user.name", "t", cwd=str(main))
    (main / "src").mkdir()
    (main / "src" / "orders.py").write_text(_PKG_CODE, encoding="utf-8")
    # 新版 git 默认禁 file:// 子模块克隆（transport 'file' not allowed）→ 本地测试显式放行
    _git("-c", "protocol.file.allow=always", "submodule", "add", str(sub), "vendor_sub", cwd=str(main))
    _git("add", ".", cwd=str(main))
    _git("commit", "-m", "main init with submodule", cwd=str(main))
    return str(main)


_make_origin.tmp_root = ""  # 由 fixture 注入


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path: Path, monkeypatch):
    _make_origin.tmp_root = str(tmp_path / "origins")
    Path(_make_origin.tmp_root).mkdir(parents=True, exist_ok=True)
    # 快照根用**盘根短路径**：pytest tmp_path 基础已 ~108 字符，快照 + graphify cache
    # 后缀（~112）会顶破 MAX_PATH 260（生产根 46 字符 + 短 id = 196 无忧，测试要等价模拟）。
    short = Path(tmp_path.anchor) / "tgs_e2e" / uuid.uuid4().hex[:8]
    short.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", str(short))
    monkeypatch.setenv("GRAPHIFY_VENV", _VENV)
    yield short
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture(autouse=True)
def _allow_file_submodule(monkeypatch):
    """测试专用：给 ``git submodule update`` 注入 ``-c protocol.file.allow=always``。

    新版 git 默认禁 file:// 子模块克隆（transport 'file' not allowed）；生产子模块
    都是 https 不受影响，**生产代码不改**——只在测试里放行（本测试的 origin 用本地路径）。
    """

    async def wrapped(*args, cwd=None, timeout=1800.0):
        if args[:1] == ("submodule",):
            args = ("-c", "protocol.file.allow=always", *args)
        return await _REAL_RUN_GIT(*args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(gs, "_run_git", wrapped)


@pytest.mark.asyncio
@needs_graphify
async def test_build_graph_e2e_dirty_ref_self_heals_and_persists(tortoise_db):
    """全链路：脏 ref → 自愈 → 真抽取 → 真入库；步骤回报齐全（弹框数据源）。"""
    origin = _make_origin(with_master_only=True)
    proj = await psvc.create_project(name="E2E")
    git = await psvc.create_git(project_id=proj.id, name="backend", repo_url=origin)
    # 版本行存**脏 ref**（生产那个报错的形态）
    version = await psvc.create_version(
        project_id=proj.id, version_code="v1", repos=[{"git_id": str(git.id), "ref": "main", "is_primary": True}]
    )

    res = await sm.project_version_build_graph({"version_id": str(version.id)})

    assert res["status"] == "done", f"整体应成功：{res}"
    assert res["persisted"] is True
    assert len(res["repos"]) == 1
    entry = res["repos"][0]

    # ① snapshot 步：自愈 main → master，并回报纠正详情（取该步终态=最后一条）
    snap = [s for s in entry["steps"] if s["step"] == "snapshot"][-1]
    assert snap["status"] == "done", f"snapshot 步失败：{snap}"
    assert entry["ref"] == "master", f"应自愈到 master，实际 {entry['ref']}"
    assert snap["ref_corrected"] == {"from": "main", "to": "master"}

    # ② extract 步：graphify 真抽出东西
    ext = [s for s in entry["steps"] if s["step"] == "extract"][-1]
    assert ext["status"] == "done", f"extract 步失败：{ext}"
    assert entry["anchors"] > 0, "真仓库却没抽出任何 anchor"

    # ③ upsert 步：真入库
    ups = [s for s in entry["steps"] if s["step"] == "upsert"][-1]
    assert ups["status"] == "done", f"upsert 步失败：{ups}"
    assert (ups["created"] + ups["updated"]) > 0

    from testing_platform.knowledge.models import KnowledgeAnchor

    db_anchors = await KnowledgeAnchor.filter(project_id=proj.id, version_id=version.id).count()
    assert db_anchors > 0, "步骤说成功但库里没有锚点"
    # 快照里子模块代码**真的被拉下来了**（生产大部分代码在子模块，这是关键回归）
    sub_file = Path(entry["snapshot_path"]) / "vendor_sub" / "subpkg" / "helpers.py"
    assert sub_file.is_file(), f"子模块文件没拉下来：{sub_file}"
    # commit 回写（版本可复现）
    refreshed = await psvc.get_version(version.id)
    assert refreshed.repos[0]["resolved_commit"]
    assert refreshed.status == "graph_built"


@pytest.mark.asyncio
@needs_graphify
async def test_build_graph_e2e_reports_failing_step_per_repo(tortoise_db):
    """一好一坏两仓：坏仓 failed_step=snapshot + error 日志；好仓照常成功（partial）。"""
    good = _make_origin(with_master_only=True)
    proj = await psvc.create_project(name="E2E2")
    g_ok = await psvc.create_git(project_id=proj.id, name="good", repo_url=good)
    g_bad = await psvc.create_git(
        project_id=proj.id, name="broken", repo_url=str(Path(good).parent / "nope_missing_repo")
    )
    version = await psvc.create_version(
        project_id=proj.id,
        version_code="v1",
        repos=[
            {"git_id": str(g_ok.id), "ref": "master", "is_primary": True},
            {"git_id": str(g_bad.id), "ref": "master"},
        ],
    )

    res = await sm.project_version_build_graph({"version_id": str(version.id)})

    assert res["status"] == "partial", f"一好一坏应为 partial：{res['status']}"
    by_name = {r["git_name"]: r for r in res["repos"]}

    bad = by_name["broken"]
    assert bad["failed_step"] == "snapshot", f"坏仓应挂在 snapshot 步：{bad['failed_step']}"
    assert bad["error"], "坏仓必须带原始错误（弹框日志就显示它）"
    assert bad["steps"][-1]["status"] == "failed", "坏仓最后一步状态应为 failed"

    ok = by_name["good"]
    assert ok["failed_step"] is None
    assert ok["anchors"] > 0, "坏仓不该拖垮好仓"


@pytest.mark.asyncio
@needs_graphify
async def test_build_graph_e2e_rerun_is_idempotent(tortoise_db):
    """重复构建（用户要的「可以多次重新生成」）：第二次 unchanged>0 且不重复堆行。"""
    origin = _make_origin(with_master_only=True)
    proj = await psvc.create_project(name="E2E3")
    git = await psvc.create_git(project_id=proj.id, name="backend", repo_url=origin)
    version = await psvc.create_version(
        project_id=proj.id, version_code="v1", repos=[{"git_id": str(git.id), "ref": "master", "is_primary": True}]
    )

    first = await sm.project_version_build_graph({"version_id": str(version.id)})
    assert first["status"] == "done"
    second = await sm.project_version_build_graph({"version_id": str(version.id)})
    assert second["status"] == "done", f"二次构建应成功：{second}"

    from testing_platform.knowledge.models import KnowledgeAnchor

    n_anchors = await KnowledgeAnchor.filter(project_id=proj.id, version_id=version.id).count()
    entry = second["repos"][0]
    ups = [s for s in entry["steps"] if s["step"] == "upsert"][-1]
    assert ups["unchanged"] > 0, "二次构建应识别为 unchanged（幂等）"
    assert n_anchors == entry["anchors"], f"重复构建堆了行：库里 {n_anchors} vs 本轮 {entry['anchors']}"
