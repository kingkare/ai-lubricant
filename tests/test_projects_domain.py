"""测试项目域（projects）独立测试 —— models + service + git_source。

覆盖（sqlite::memory:，模式照 ``tests/test_assets_model.py``）：

1. 项目/git/源/release 四表建表（含原始 DDL 路径）。
2. service CRUD：枚举校验 / 唯一冲突 / 变更门（status/version 变更必须 change_reason）。
3. release diff：指针变化 → changed/added/removed。
4. ``git_source.init_snapshot``：真实 git（file:// 本地仓库）克隆 + 幂等 fetch；
   凭据引用只走 env，绝不落盘。

运行：``python -m pytest tests/test_projects_domain.py -q``
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.projects import models as pm  # noqa: E402
from testing_platform.projects import service as svc  # noqa: E402
from testing_platform.projects import git_source  # noqa: E402


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


def _have_git() -> bool:
    return shutil.which("git") is not None


# ── 1) 建表（模型与原始 DDL 路径都可用）────────────────────────
@pytest.mark.asyncio
async def test_tables_created(tortoise_db):
    for model in (pm.TestProject, pm.TestProjectGit, pm.TestProjectSource, pm.TestProjectRelease):
        assert await model.all().count() == 0
    assert pm.PROJECT_STATUSES == ("active", "archived")
    assert "commit" in pm.GIT_REF_TYPES
    assert pm.SOURCE_KINDS == ("prd", "case", "code", "openapi")
    assert pm.SOURCE_ADAPTERS == ("builtin", "agent")


# ── 2) service CRUD ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_project_crud_and_change_gate(tortoise_db):
    proj = await svc.create_project(name="平台回归", description="整个项目维度")
    assert proj.code.startswith("TP-")
    assert proj.status == "active"

    # 显式 code
    proj2 = await svc.create_project(name="另一个", code="TP-9999")
    assert proj2.code == "TP-9999"

    # code 冲突
    with pytest.raises(svc.ProjectValidationError):
        await svc.create_project(name="重复", code="TP-9999")

    # 结构性变更（status）无理由 → 拒绝
    with pytest.raises(svc.ProjectValidationError):
        await svc.update_project(proj.id, status="archived")

    archived = await svc.archive_project(proj.id, change_reason="项目结项")
    assert archived.status == "archived"

    listed = await svc.list_projects(status="archived")
    assert [p.code for p in listed] == [proj.code]

    with pytest.raises(svc.ProjectNotFoundError):
        await svc.get_project(uuid.uuid4())


@pytest.mark.asyncio
async def test_git_source_crud_conflicts(tortoise_db):
    proj = await svc.create_project(name="git 项目")
    git = await svc.create_git(
        project_id=proj.id,
        name="backend",
        repo_url="https://github.com/x/y.git",
        ref_type="branch",
        ref_value="main",
        credential_ref={"kind": "env", "name": "GIT_TOKEN"},
    )
    assert git.credential_ref == {"kind": "env", "name": "GIT_TOKEN"}

    # 同项目同名冲突
    with pytest.raises(svc.ProjectValidationError):
        await svc.create_git(project_id=proj.id, name="backend", repo_url="https://x", ref_value="main")

    # 枚举校验
    with pytest.raises(svc.ProjectValidationError):
        await svc.create_git(project_id=proj.id, name="x", repo_url="https://x", ref_type="tree", ref_value="main")

    updated = await svc.update_git(git.id, ref_value="release/1.2")
    assert updated.ref_value == "release/1.2"
    assert updated.ref_type == "branch"

    await svc.delete_git(git.id)
    assert len(await svc.list_gits(proj.id)) == 0


@pytest.mark.asyncio
async def test_source_crud(tortoise_db):
    proj = await svc.create_project(name="源项目")
    src = await svc.create_source(
        project_id=proj.id,
        kind="prd",
        name="飞书 PRD",
        adapter="agent",
        adapter_type="feishu",
        config={"doc_url": "https://feishu.cn/x"},
        sync_policy="scheduled",
        schedule={"kind": "cron", "expr": "0 9 * * *"},
    )
    assert src.adapter == "agent"
    assert src.sync_policy == "scheduled"

    with pytest.raises(svc.ProjectValidationError):
        await svc.create_source(project_id=proj.id, kind="prd", name="飞书 PRD")

    marked = await svc.mark_source_synced(src.id, result={"anchors": 42})
    assert marked.last_synced_at is not None
    assert marked.last_sync_result == {"anchors": 42}

    await svc.delete_source(src.id)
    assert len(await svc.list_sources(proj.id)) == 0


# ── 3) release diff ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_release_diff(tortoise_db):
    proj = await svc.create_project(name="版本项目")
    r1 = await svc.create_release(
        project_id=proj.id,
        version_code="v1.0",
        git_tags=[{"git_id": "g1", "ref": "release/1.0"}],
    )
    r2 = await svc.create_release(
        project_id=proj.id,
        version_code="v1.1",
        git_tags=[{"git_id": "g1", "ref": "release/1.1"}],
        case_versions=[{"source_id": "s1", "ref": "42"}],
    )
    diff = svc.diff_releases(r1, r2)
    assert diff["base"] == "v1.0" and diff["head"] == "v1.1"
    assert diff["git_tags"]["changed"]["g1"] == {"from": "release/1.0", "to": "release/1.1"}
    assert diff["case_versions"]["added"] == ["s1"]


# ── 3b) 版本一等实体（version）────────────────────────────────
@pytest.mark.asyncio
async def test_version_crud_and_initial(tortoise_db):
    proj = await svc.create_project(name="版本实体项目")
    g1 = await svc.create_git(project_id=proj.id, name="backend", repo_url="https://x/b.git", ref_value="main")
    g2 = await svc.create_git(project_id=proj.id, name="frontend", repo_url="https://x/f.git", ref_value="main")

    # 首个版本自动 is_initial=true，且首个仓缺省 is_primary=true
    v1 = await svc.create_version(
        project_id=proj.id,
        version_code="v1.0",
        repos=[{"git_id": str(g1.id), "ref": "main"}, {"git_id": str(g2.id), "ref": "develop"}],
    )
    assert v1.is_initial is True
    assert v1.status == "draft"
    assert v1.repos[0]["is_primary"] is True
    assert v1.repos[1]["ref"] == "develop"
    assert v1.repos[1]["is_primary"] is False

    # 第二个版本 is_initial=false
    v2 = await svc.create_version(
        project_id=proj.id, version_code="v1.1",
        repos=[{"git_id": str(g1.id), "ref": "release/1.1"}],
    )
    assert v2.is_initial is False

    # version_code 冲突
    with pytest.raises(svc.ProjectValidationError):
        await svc.create_version(project_id=proj.id, version_code="v1.0", repos=[])

    # repos[].git_id 不属于本项目 → 拒绝
    with pytest.raises(svc.ProjectValidationError):
        await svc.create_version(
            project_id=proj.id, version_code="v1.2",
            repos=[{"git_id": str(uuid.uuid4()), "ref": "main"}],
        )

    # is_primary 至多一项
    with pytest.raises(svc.ProjectValidationError):
        await svc.create_version(
            project_id=proj.id, version_code="v1.3",
            repos=[{"git_id": str(g1.id), "is_primary": True}, {"git_id": str(g2.id), "is_primary": True}],
        )

    listed = await svc.list_versions(proj.id)
    assert [v.version_code for v in listed] == ["v1.0", "v1.1"]

    # 改 repos 走变更门
    with pytest.raises(svc.ProjectValidationError):
        await svc.update_version(v2.id, repos=[{"git_id": str(g1.id), "ref": "hotfix"}])
    updated = await svc.update_version(
        v2.id, repos=[{"git_id": str(g1.id), "ref": "hotfix"}], change_reason="热修分支"
    )
    assert updated.repos[0]["ref"] == "hotfix"

    await svc.delete_version(v2.id)
    assert len(await svc.list_versions(proj.id)) == 1


@pytest.mark.asyncio
async def test_version_repo_ref_defaults_to_git_ref(tortoise_db):
    """版本仓库项未给 ref 时，回落到该仓配置的 ref_value。"""
    proj = await svc.create_project(name="默认 ref 项目")
    g = await svc.create_git(project_id=proj.id, name="api", repo_url="https://x/a.git", ref_value="develop")
    v = await svc.create_version(project_id=proj.id, version_code="v0.1", repos=[{"git_id": str(g.id)}])
    assert v.repos[0]["ref"] == "develop"
    assert v.repos[0]["ref_type"] == "branch"

    # 重复版本号冲突（version 域）
    with pytest.raises(svc.ProjectValidationError):
        await svc.create_version(project_id=proj.id, version_code="v0.1", repos=[{"git_id": str(g.id)}])


# ── 5) coding 项目桥 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_coding_project_bridge(tortoise_db):
    """create/update 带 coding_project_id；resolve 取桥；不传不动、传 None 解绑。"""
    coding = uuid.uuid4()
    proj = await svc.create_project(name="带桥", coding_project_id=coding)
    assert await svc.resolve_coding_project_id(proj.id) == coding

    # 不传 → 不动
    await svc.update_project(proj.id, name="改名")
    assert await svc.resolve_coding_project_id(proj.id) == coding

    # 传 None → 解绑
    await svc.update_project(proj.id, coding_project_id=None)
    assert await svc.resolve_coding_project_id(proj.id) is None

    # resolve 不存在的项目 → ProjectNotFoundError
    with pytest.raises(svc.ProjectNotFoundError):
        await svc.resolve_coding_project_id(uuid.uuid4())


# ── 4) git_source：真实克隆 ─────────────────────────────────
@pytest.mark.asyncio
async def test_git_source_init_snapshot(tortoise_db):
    if not _have_git():
        pytest.skip("git 不可用")

    with tempfile.TemporaryDirectory() as tmp:
        # 造一个本地 git 仓库（一个提交）
        repo = Path(tmp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
        (repo / "hello.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        repo_url = repo.as_uri()

        proj = await svc.create_project(name="快照项目")
        git = await svc.create_git(
            project_id=proj.id,
            name="fixture-repo",
            repo_url=repo_url,
            ref_type="branch",
            ref_value="main",
            credential_ref={"kind": "env", "name": "TESTING_SNAPSHOT_TOKEN"},
        )

        # 快照根指向临时目录
        snap_root = Path(tmp) / "snapshots"
        old_env = os.environ.get("TESTING_SNAPSHOT_ROOT")
        os.environ["TESTING_SNAPSHOT_ROOT"] = str(snap_root)
        os.environ["TESTING_SNAPSHOT_TOKEN"] = "not-needed-for-public-repo"
        try:
            result = await git_source.init_snapshot(git.id)
            assert result["fresh_clone"] is True
            assert result["ref"] == "main"
            snapshot = Path(result["path"])
            assert (snapshot / "hello.py").is_file()
            first_commit = result["commit"]

            # 幂等：二次调用走 fetch 路径（fresh_clone=False），commit 不变
            result2 = await git_source.init_snapshot(git.id)
            assert result2["fresh_clone"] is False
            assert result2["commit"] == first_commit
        finally:
            if old_env is None:
                os.environ.pop("TESTING_SNAPSHOT_ROOT", None)
            else:
                os.environ["TESTING_SNAPSHOT_ROOT"] = old_env
            os.environ.pop("TESTING_SNAPSHOT_TOKEN", None)