"""Proxy-side resolution: an environment token resolves to its project's repo.

This is the half of the environment credential that runs in the *control*
service (``node_server.git_proxy``), where it reads raw SQL rather than the
gateway's ORM. The pool is faked so the resolution rules can be exercised
without PostgreSQL: which table the id lands in decides the path, and the
environment's ``enabled`` flag is the live revocation gate.

The credential itself is never invented here — it is read from
``mc_git_identities`` at request time, exactly as the task path does.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from node_server import git_proxy


class _FakeConn:
    """Answers the handful of ``fetchrow`` queries ``_resolve`` issues.

    Keyed by a substring of the SQL so a test declares only the rows it cares
    about; anything unmatched returns None, which is what a missing row does.
    """

    def __init__(self, rows: list[tuple[str, dict | None]]):
        self.rows = rows

    async def fetchrow(self, sql: str, *args):
        for needle, row in self.rows:
            if needle in sql:
                return row
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_pool(monkeypatch):
    def _install(rows):
        # ``_resolve*`` do ``from .shared_store import pool`` inside the function
        # body, so the patch has to land on shared_store — patching the git_proxy
        # module attribute would have no effect on the already-bound local name.
        from node_server import shared_store

        monkeypatch.setattr(shared_store, "pool", lambda: _FakePool(_FakeConn(rows)))

    return _install


ENV_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
IDENTITY_ID = uuid.uuid4()


def _env_rows(*, enabled=True, project_id=PROJECT_ID, submodule_paths=None):
    return [
        ("FROM mc_tasks WHERE id", None),  # id is not a task → environment path
        (
            "FROM mc_task_environments WHERE id",
            {
                "enabled": enabled,
                "project_id": project_id,
                "submodule_paths": submodule_paths,
            },
        ),
        (
            "FROM mc_projects WHERE id",
            {"repo_url": "https://github.com/o/r.git", "git_identity_id": IDENTITY_ID},
        ),
        (
            "FROM mc_git_identities WHERE id",
            {"username": "bot", "access_token": "ghp_real"},
        ),
    ]


@pytest.mark.asyncio
async def test_environment_resolves_to_its_projects_repo(fake_pool):
    fake_pool(_env_rows())
    target = await git_proxy._resolve(str(ENV_ID), "main")
    assert target.repo_url == "https://github.com/o/r.git"
    # The real credential is read at request time and injected upstream; the
    # node only ever held the signed proxy token.
    assert target.access_token == "ghp_real"
    assert target.username == "bot"


@pytest.mark.asyncio
async def test_disabled_environment_is_revoked(fake_pool):
    fake_pool(_env_rows(enabled=False))
    with pytest.raises(git_proxy._GitProxyError) as exc:
        await git_proxy._resolve(str(ENV_ID), "main")
    assert exc.value.status == 403
    # The message names the switch to flip, rather than sending the operator
    # looking at the token.
    assert "disabled" in exc.value.message


@pytest.mark.asyncio
async def test_missing_environment_is_revoked(fake_pool):
    fake_pool([("FROM mc_tasks WHERE id", None), ("FROM mc_task_environments WHERE id", None)])
    with pytest.raises(git_proxy._GitProxyError) as exc:
        await git_proxy._resolve(str(ENV_ID), "main")
    assert exc.value.status == 403


@pytest.mark.asyncio
async def test_environment_without_project_has_no_repo(fake_pool):
    fake_pool(_env_rows(project_id=None))
    with pytest.raises(git_proxy._GitProxyError) as exc:
        await git_proxy._resolve(str(ENV_ID), "main")
    assert exc.value.status == 404
    assert "no bound project" in exc.value.message


@pytest.mark.asyncio
async def test_environment_submodule_allowlist_is_carried(fake_pool):
    # The environment stores the paths in their own column, so the value IS the
    # list (unlike the task path, which digs it out of config_snapshot).
    fake_pool(_env_rows(submodule_paths=["o/sib.git"]))
    target = await git_proxy._resolve(str(ENV_ID), "main")
    assert target.submodule_paths == ["o/sib.git"]


@pytest.mark.asyncio
async def test_environment_submodule_allowlist_tolerates_json_string(fake_pool):
    """A driver may hand the JSON column back as text."""
    fake_pool(_env_rows(submodule_paths='["o/sib.git"]'))
    target = await git_proxy._resolve(str(ENV_ID), "main")
    assert target.submodule_paths == ["o/sib.git"]


@pytest.mark.asyncio
async def test_task_id_still_takes_the_task_path(fake_pool):
    """The environment path must not swallow the historic task credential."""
    task_id = uuid.uuid4()
    fake_pool([
        ("FROM mc_tasks WHERE id", {"status": "processing", "deleted_at": None, "config_snapshot": None}),
        (
            "FROM mc_project_tasks",
            {"repo_url": "https://github.com/o/task.git", "git_identity_id": IDENTITY_ID, "project_id": None},
        ),
        ("FROM mc_git_identities WHERE id", {"username": "bot", "access_token": "ghp_task"}),
    ])
    target = await git_proxy._resolve(str(task_id), "main")
    assert target.repo_url == "https://github.com/o/task.git"
    assert target.access_token == "ghp_task"


@pytest.mark.asyncio
async def test_inactive_task_is_still_revoked(fake_pool):
    fake_pool([
        ("FROM mc_tasks WHERE id", {"status": "finished", "deleted_at": None, "config_snapshot": None}),
    ])
    with pytest.raises(git_proxy._GitProxyError) as exc:
        await git_proxy._resolve(str(uuid.uuid4()), "main")
    assert exc.value.status == 403
