"""真实 git 端到端测试 —— 复现并验证 "couldn't find remote ref main" 自愈。

**真跑 git**（不是桩）：建一个本地仓库（默认分支 master，没有 main），让
``init_snapshot(ref_override="main")`` 去拉——旧代码会 128，新代码应自愈到 master。

跳过条件：机器上没有 git 可执行文件。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.projects import git_source as gs  # noqa: E402
from testing_platform.projects import service as psvc  # noqa: E402


def _git(*args: str, cwd: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120
    )
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {out}"
    return out


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.projects.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def local_repo(tmp_path: Path) -> str:
    """建一个本地 git 仓库：默认分支 = master（**没有 main**），带一个文件 + 一次提交。"""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git("init", "-b", "master", cwd=str(repo))
    _git("config", "user.email", "t@t.t", cwd=str(repo))
    _git("config", "user.name", "t", cwd=str(repo))
    (repo / "hello.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    _git("add", ".", cwd=str(repo))
    _git("commit", "-m", "init", cwd=str(repo))
    return str(repo)


@pytest.fixture
def short_snap_root(tmp_path: Path) -> str:
    """短快照根（Windows MAX_PATH：pytest tmp_path 太深会让 git clone 报 Filename too long）。"""
    # 用仓库所在盘的根下一个短目录，避免深层嵌套路径
    base = Path(tmp_path.anchor) / "tgs" / uuid.uuid4().hex[:8]
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


@pytest.mark.asyncio
async def test_real_git_dirty_main_self_heals(tortoise_db, local_repo, short_snap_root, monkeypatch):
    """真仓库（只有 master）+ 脏 ref "main" → init_snapshot 自愈到 master，不再 128。"""
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", short_snap_root)
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="origin", repo_url=local_repo)

    snap = await gs.init_snapshot(git.id, ref_override="main", version_id=str(uuid.uuid4()))

    assert snap["ref"] == "master", f"应自愈到 master，实际 {snap['ref']}"
    assert snap["ref_corrected"] is True
    assert snap["requested_ref"] == "main"
    # 真的把代码 checkout 出来了
    assert (Path(snap["path"]) / "hello.py").is_file()
    assert len(snap["commit"]) == 40


@pytest.mark.asyncio
async def test_real_git_valid_ref_no_correction(tortoise_db, local_repo, short_snap_root, monkeypatch):
    """真仓库 + 正确的 ref（master）→ 原样用，ref_corrected=False。"""
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", short_snap_root)
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="origin", repo_url=local_repo)

    snap = await gs.init_snapshot(git.id, ref_override="master", version_id=str(uuid.uuid4()))
    assert snap["ref"] == "master"
    assert snap["ref_corrected"] is False
    assert (Path(snap["path"]) / "hello.py").is_file()


@pytest.mark.asyncio
async def test_real_git_empty_ref_detects_default(tortoise_db, local_repo, short_snap_root, monkeypatch):
    """真仓库 + 空 ref → 探测默认分支 master（不再硬编码 main）。"""
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", short_snap_root)
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="origin", repo_url=local_repo)

    snap = await gs.init_snapshot(git.id, ref_override="", version_id=str(uuid.uuid4()))
    assert snap["ref"] == "master"
    assert (Path(snap["path"]) / "hello.py").is_file()


@pytest.mark.asyncio
async def test_real_git_idempotent_second_run(tortoise_db, local_repo, short_snap_root, monkeypatch):
    """第二次拉（快照已存在）→ fetch+reset 路径也能自愈脏 ref。"""
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", short_snap_root)
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="origin", repo_url=local_repo)
    vid = str(uuid.uuid4())

    first = await gs.init_snapshot(git.id, ref_override="master", version_id=vid)
    assert first["fresh_clone"] is True
    # 第二次带脏 ref → 走 existing 分支（fetch+reset），仍应自愈
    second = await gs.init_snapshot(git.id, ref_override="main", version_id=vid)
    assert second["fresh_clone"] is False
    assert second["ref"] == "master"
    assert second["ref_corrected"] is True


@pytest.mark.asyncio
async def test_list_remote_branches_real(tortoise_db, local_repo, short_snap_root, monkeypatch):
    """真仓库列远端分支 → 返回 [master]，默认分支 master。"""
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", short_snap_root)
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="origin", repo_url=local_repo)

    res = await gs.list_remote_branches(git.id)
    assert res["branches"] == ["master"]
    assert res["default_branch"] == "master"


@pytest.fixture
def local_repo_has_main(tmp_path: Path) -> str:
    """真仓库：默认分支 master，但**同时存在一个 main 分支**。"""
    repo = tmp_path / "both"
    repo.mkdir()
    _git("init", "-b", "master", cwd=str(repo))
    _git("config", "user.email", "t@t.t", cwd=str(repo))
    _git("config", "user.name", "t", cwd=str(repo))
    (repo / "hello.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", ".", cwd=str(repo))
    _git("commit", "-m", "init", cwd=str(repo))
    _git("branch", "main", cwd=str(repo))  # main 真实存在
    return str(repo)


@pytest.mark.asyncio
async def test_no_false_correction_when_main_exists(
    tortoise_db, local_repo_has_main, short_snap_root, monkeypatch
):
    """远端**真的有 main** → 绝不能自作主张改成 master（会拉错分支，比报错更糟）。"""
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", short_snap_root)
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="origin", repo_url=local_repo_has_main)

    snap = await gs.init_snapshot(git.id, ref_override="main", version_id=str(uuid.uuid4()))
    assert snap["ref"] == "main", "main 存在时必须用 main，不能纠正"
    assert snap["ref_corrected"] is False


@pytest.fixture
def local_repo_unusual_default(tmp_path: Path) -> str:
    """真仓库：默认分支是**不常见名字** trunk（既不是 main 也不是 master）。"""
    repo = tmp_path / "trunkrepo"
    repo.mkdir()
    _git("init", "-b", "trunk", cwd=str(repo))
    _git("config", "user.email", "t@t.t", cwd=str(repo))
    _git("config", "user.name", "t", cwd=str(repo))
    (repo / "hello.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", ".", cwd=str(repo))
    _git("commit", "-m", "init", cwd=str(repo))
    return str(repo)


@pytest.mark.asyncio
async def test_self_heal_to_unusual_default(
    tortoise_db, local_repo_unusual_default, short_snap_root, monkeypatch
):
    """默认分支既不是 main 也不是 master（trunk）→ 探测出的必须是 trunk。"""
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", short_snap_root)
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="origin", repo_url=local_repo_unusual_default)

    snap = await gs.init_snapshot(git.id, ref_override="main", version_id=str(uuid.uuid4()))
    assert snap["ref"] == "trunk", f"应探测出 trunk，实际 {snap['ref']}"
    assert snap["ref_corrected"] is True


@pytest.mark.asyncio
async def test_truly_missing_ref_still_raises(
    tortoise_db, local_repo, short_snap_root, monkeypatch
):
    """远端既没有这个 ref、探测出的默认分支也拉不到时 → 必须**显式报错**，绝不静默成功。

    （用不存在的 tag 名 + 桩掉探测让它返回一个远端没有的分支，验证错误会冒出来。）
    """
    monkeypatch.setenv("TESTING_SNAPSHOT_ROOT", short_snap_root)
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="origin", repo_url=local_repo)

    async def fake_detect(url: str) -> str:
        return "nonexistent-branch-xyz"

    monkeypatch.setattr(gs, "_detect_default_branch", fake_detect)

    with pytest.raises(gs.GitSourceError):
        await gs.init_snapshot(git.id, ref_override="main", version_id=str(uuid.uuid4()))
