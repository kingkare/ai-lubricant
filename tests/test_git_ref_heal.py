"""git ref 自愈测试 —— 修 "couldn't find remote ref main"。

覆盖三点（都不联网，git 命令用 monkeypatch 桩）：
1. ``_normalize_version_repos``：空 ref **不再兜底 "main"**（留空交给探测默认分支）。
2. ``_ref_missing_on_remote``：远端有该 ref→False；没有→True；sha 形态→判不准 False。
3. ``init_snapshot``：脏 ref（main）在远端不存在时 → 探测默认分支（master）替换，
   返回 ``ref_corrected=True`` + ``requested_ref="main"``。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.projects import git_source as gs  # noqa: E402
from testing_platform.projects import service as psvc  # noqa: E402


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


@pytest.mark.asyncio
async def test_normalize_repos_empty_ref_stays_empty(tortoise_db):
    """空 ref 不再被兜底成 "main"（脏数据根因）。"""
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="backend", repo_url="https://x/y.git")
    norm = await psvc._normalize_version_repos(
        [{"git_id": str(git.id), "ref": "", "is_primary": True}], project_id=proj.id
    )
    assert norm[0]["ref"] == ""  # 留空 → init_snapshot 探测远端默认分支
    assert norm[0]["ref"] != "main"


@pytest.mark.asyncio
async def test_ref_missing_on_remote(monkeypatch):
    """ls-remote 桩：有输出→False；无输出非 sha→True；无输出 sha→False。"""
    calls = {"out": "abc123\trefs/heads/main\n"}

    async def fake_run_git(*args, cwd=None, timeout=1800.0):
        return calls["out"]

    monkeypatch.setattr(gs, "_run_git", fake_run_git)

    # 远端有 main → False
    assert await gs._ref_missing_on_remote("u", "main") is False
    # 远端无 master（空输出）→ True
    calls["out"] = ""
    assert await gs._ref_missing_on_remote("u", "master") is True
    # sha 形态且无输出 → 判不准 False（放行给 fetch）
    assert await gs._ref_missing_on_remote("u", "deadbeefcafe1234") is False


@pytest.mark.asyncio
async def test_init_snapshot_self_heals_dirty_ref(tortoise_db, monkeypatch):
    """脏 ref（main）在远端不存在 → 探测默认分支（master）替换 + ref_corrected=True。"""
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="backend", repo_url="https://x/y.git")

    # 桩：ls-remote --heads --tags 对 main 返回空（远端没有 main）
    async def fake_ref_missing(url, ref):
        return ref == "main"

    async def fake_detect(url):
        return "master"

    # 桩：_materialize 什么都不做（不碰真实 git）；rev-parse 返回假 sha
    async def fake_materialize(dest, url, ref, *, fresh_clone):
        return None

    async def fake_run_git(*args, cwd=None, timeout=1800.0):
        if args[:1] == ("submodule",):
            return ""
        return "0" * 40

    monkeypatch.setattr(gs, "_ref_missing_on_remote", fake_ref_missing)
    monkeypatch.setattr(gs, "_detect_default_branch", fake_detect)
    monkeypatch.setattr(gs, "_materialize", fake_materialize)
    monkeypatch.setattr(gs, "_run_git", fake_run_git)

    snap = await gs.init_snapshot(git.id, ref_override="main", version_id=str(uuid.uuid4()))
    assert snap["ref"] == "master"
    assert snap["ref_corrected"] is True
    assert snap["requested_ref"] == "main"


@pytest.mark.asyncio
async def test_init_snapshot_keeps_valid_ref(tortoise_db, monkeypatch):
    """ref 在远端存在 → 原样用，ref_corrected=False。"""
    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="backend", repo_url="https://x/y.git")

    async def fake_ref_missing(url, ref):
        return False  # 远端有 develop

    async def fake_materialize(dest, url, ref, *, fresh_clone):
        return None

    async def fake_run_git(*args, cwd=None, timeout=1800.0):
        if args[:1] == ("submodule",):
            return ""
        return "1" * 40

    monkeypatch.setattr(gs, "_ref_missing_on_remote", fake_ref_missing)
    monkeypatch.setattr(gs, "_materialize", fake_materialize)
    monkeypatch.setattr(gs, "_run_git", fake_run_git)

    snap = await gs.init_snapshot(git.id, ref_override="develop", version_id=str(uuid.uuid4()))
    assert snap["ref"] == "develop"
    assert snap["ref_corrected"] is False
