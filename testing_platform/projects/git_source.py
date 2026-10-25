"""Code-source init/sync — clone & fetch snapshots from configured repos.

The test project **owns no code**: it holds only ``mc_test_project_git`` rows.
This layer materializes a **working snapshot** on disk (a full clone under a
platform snapshot root) so the knowledge pipeline (Graphify) can run against
real source. Snapshots are disposable caches — the DB is the project's only
truth; a snapshot directory can be deleted and re-cloned at any time.

Clone style mirrors the platform's own README (``git clone
--recurse-submodules``): a **full** clone (no ``--depth``) followed by
``git submodule update --init --recursive``. Shallow clones are deliberately
avoided — they break submodules (a pinned submodule commit is often not
advertised, so the shallow fetch is refused), and most of a platform repo's code
lives in submodules.

Credential model: ``credential_ref`` is a **reference**, never a plaintext. It
names an env var (``{"kind": "env", "name": "GIT_TOKEN"}``) or a platform git
identity id (``{"kind": "identity", "id": ...}``). Resolution here injects the
token into the clone URL's userinfo *in-process* and never persists it.

Deterministic only — no LLM anywhere on this path (design 确定性的归确定性).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from .service import get_git, get_project

# Snapshot root: ``$TESTING_SNAPSHOT_ROOT``, else ``<project>/.claude/testing-snapshots``.
_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / ".claude" / "testing-snapshots"


def _snapshot_root() -> Path:
    env = os.environ.get("TESTING_SNAPSHOT_ROOT")
    return Path(env).resolve() if env else _DEFAULT_ROOT


def _short_id(value: Any) -> str:
    """UUID → 前 12 位（目录名用，压短快照路径，避开 Windows MAX_PATH(260)）。

    完整 UUID 36 字符当目录名时，三个 UUID + graphify cache 后缀（~117）会把快照路径
    顶破 260 → graphify 写 cache 临时文件报 ``[Errno 2] No such file or directory``。
    前 12 位（2^48 空间）对单个项目下的版本/仓库足以去重，碰撞概率可忽略。
    非 UUID 原样返回。
    """
    s = str(value)
    return s.replace("-", "")[:12] if len(s) >= 32 and "-" in s else s


def _snapshot_path(project_id: str, git_id: str, *, version_id: Optional[str] = None) -> Path:
    """快照目录（**短 id 压缩**，避开 Windows MAX_PATH(260)）。

    用短 id（UUID 前 12 位）当目录名，避免三个完整 UUID（108 字符）+ graphify cache
    后缀（~117）顶破 260。旧的全 UUID 目录会因路径变了被当 fresh_clone 重拉一次
    （快照是可弃缓存，重拉无副作用）。

    带版本：``<root>/<proj_短>/v<ver_短>/<git_短>``；不带：``<root>/<proj_短>/<git_短>``。
    """
    root = _snapshot_root()
    pid = _short_id(project_id)
    gid = _short_id(git_id)
    if version_id:
        return root / pid / f"v{_short_id(version_id)}" / gid
    return root / pid / gid


def _rmtree_force(path: Path) -> None:
    """递归删除目录，Windows 下先清只读位。

    ``shutil.rmtree(..., ignore_errors=True)`` 在 Windows 上会**静默失败**——
    git 的 ``.git/objects`` 是只读的，``PermissionError`` 被 ignore_errors 吞掉，
    目录残留 → 后续 ``git clone`` 报 "already exists and is not an empty directory"。
    这里逐文件清掉只读位再删，删不掉的显式抛错（不静默）。
    """
    if not path.exists():
        return
    import platform

    def _on_error(func, p, exc_info):  # noqa: ANN001 — rmtree onerror 签名
        # 只读位导致删不掉 → 清位后重试；仍失败则抛出（不静默）。
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            raise

    if platform.system() == "Windows":
        shutil.rmtree(path, onerror=_on_error)
    else:
        shutil.rmtree(path)


class GitSourceError(RuntimeError):
    """克隆/拉取失败（带退出码 + 输出）。"""

    def __init__(self, message: str, *, returncode: Optional[int] = None, output: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.output = output


async def _resolve_credential(credential_ref: Optional[Mapping[str, Any]]) -> Optional[str]:
    """把 credential_ref 解析成 git 可用的 token（不落盘、不持久化）。

    ``{"kind": "env", "name": "GIT_TOKEN"}`` → ``os.environ["GIT_TOKEN"]``。
    ``{"kind": "identity", "id": "<uuid>"}`` → 查 mc_git_identities 表拿 access_token。
    其余 kind 返回 None——克隆退化为匿名（私有仓库会失败并显式报错，绝不静默）。
    凭据按需扩展，不在此硬编码。
    """
    if not credential_ref:
        return None
    kind = credential_ref.get("kind")
    if kind == "env":
        name = credential_ref.get("name")
        if name:
            return os.environ.get(str(name))
    if kind == "identity":
        identity_id = credential_ref.get("id")
        if identity_id:
            from tortoise import Tortoise
            conn = Tortoise.get_connection("user_platform")
            # 直接 SQL 查 access_token，不经过 Tortoise ORM 模型（避免循环依赖）
            rows = await conn.execute_query_dict(
                "SELECT access_token FROM mc_git_identities WHERE id = $1 AND user_id IS NOT NULL",
                [identity_id],
            )
            if rows and rows[0].get("access_token"):
                return str(rows[0]["access_token"])
    return None


def _auth_url(repo_url: str, token: Optional[str]) -> str:
    """有 token 时注入 https userinfo（``https://<token>@host/...``）。"""
    if not token:
        return repo_url
    if repo_url.startswith("https://"):
        return repo_url[:8] + f"{token}@" + repo_url[8:]
    return repo_url


async def _run_git(*args: str, cwd: Optional[Path] = None, timeout: float = 1800.0) -> str:
    """Run ``git`` synchronously in a thread (git is CPU/IO, not async).

    Default timeout is generous (30 min): a **full** clone of a large repo (with
    submodules) is far slower than a shallow one, and we no longer use ``--depth``.
    """
    def _exec() -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise GitSourceError(
                f"git {' '.join(args)} failed (exit {proc.returncode}): {out}",
                returncode=proc.returncode,
                output=out,
            )
        return out.strip()

    return await asyncio.to_thread(_exec)


async def _detect_default_branch(url: str) -> str:
    """探测远端默认分支（``git ls-remote --symref origin HEAD``）。

    返回 ``refs/heads/<name>`` 里的 ``<name>``；探测失败退回 ``main``
    （GitHub 新仓默认 main）。用于仓库未显式配 ref 时的兜底。
    """
    try:
        out = await _run_git("ls-remote", "--symref", url, "HEAD")
    except GitSourceError:
        return "main"
    for line in out.splitlines():
        # 形如 ``ref: refs/heads/main\tHEAD``
        if line.startswith("ref:") and "refs/heads/" in line:
            return line.split("refs/heads/", 1)[1].split()[0].strip()
    return "main"


async def list_remote_branches(
    git_id: Any,
    *,
    repo_url: Optional[str] = None,
    credential_ref: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """列远端分支 + 探测默认分支（``git ls-remote --heads`` / ``--symref HEAD``）。

    给版本表单用：分支不再是自由文本，而是**远端真实分支下拉**，默认选中
    远端默认分支（master/main）。这是修 "couldn't find remote ref main" 的根治
    手段——用户选的永远是远端存在的 ref。

    可传 ``git_id``（读仓库配置 + 凭据），或显式 ``repo_url``（+可选
    ``credential_ref``）。返回 ``{"repo_url", "branches": [str], "default_branch"}``。
    """
    url = repo_url
    cred = credential_ref
    if git_id:
        git = await get_git(git_id)
        url = url or git.repo_url
        cred = cred if cred is not None else git.credential_ref
    if not url:
        raise GitSourceError("list_remote_branches 需要 git_id 或 repo_url")
    token = await _resolve_credential(cred)
    auth = _auth_url(url, token)

    # 默认分支（--symref HEAD）：探测失败不阻断——退回首个分支或 main。
    default_branch = ""
    try:
        symref = await _run_git("ls-remote", "--symref", auth, "HEAD")
        for line in symref.splitlines():
            if line.startswith("ref:") and "refs/heads/" in line:
                default_branch = line.split("refs/heads/", 1)[1].split()[0].strip()
                break
    except GitSourceError:
        default_branch = ""

    try:
        heads = await _run_git("ls-remote", "--heads", auth)
    except GitSourceError as exc:
        raise GitSourceError(
            f"列远端分支失败：{exc}", returncode=exc.returncode, output=exc.output
        ) from None

    branches: list[str] = []
    for line in heads.splitlines():
        # 形如 ``<sha>\trefs/heads/<name>``
        if "\trefs/heads/" in line:
            name = line.split("\trefs/heads/", 1)[1].strip()
            if name and name not in branches:
                branches.append(name)
    branches.sort()
    if not default_branch:
        default_branch = "main" if "main" in branches else (branches[0] if branches else "main")
    return {"repo_url": url, "branches": branches, "default_branch": default_branch}


async def _ref_missing_on_remote(url: str, ref: str) -> bool:
    """远端**确实没有**这个 ref 才返回 True（判不准一律 False，交给 fetch 报错）。

    这是「自愈」的判据：版本行里存着历史脏 ref（如远端只有 master 却存了 main），
    直接 ``git fetch origin main`` 会 128。先用 ``ls-remote`` 精确匹配一次——
    匹配不到且不是 sha 形态，就认定 ref 不存在，退回探测远端默认分支。
    """
    import re

    try:
        out = await _run_git("ls-remote", "--heads", "--tags", url, ref)
    except GitSourceError:
        return False  # 网络/鉴权问题：别误判成 ref 不存在
    if out.strip():
        return False
    # sha 形态无法用 ls-remote 判定（远端默认不广告任意 commit）→ 判不准，放行
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        return False
    return True


async def _materialize(dest: Path, url: str, ref: str, *, fresh_clone: bool) -> None:
    """把 ``dest`` 拉成 ``ref`` 的快照（全新则 clone→fetch→checkout→reset→换目录）。"""
    if fresh_clone:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Clone to a **unique** temp dir then move into place, so a failed/interrupted
        # clone never leaves a half-dir at ``dest`` and two concurrent builds never
        # collide on a shared temp name.
        #
        # **Full clone, no ``--depth``** — mirrors the platform's own README
        # (``git clone --recurse-submodules``). Shallow clones break submodules:
        # a submodule pins a specific commit, and servers routinely refuse to serve a
        # shallow fetch of a commit that isn't advertised — so ``submodule update``
        # fails and the submodule dirs come back EMPTY.
        #
        # We do NOT use ``clone --branch <ref>``: it rejects commits and bare/local
        # repos alike — the fetch+checkout path below handles branch/tag/commit
        # uniformly. Submodules are inited **after** the move (caller): a submodule's
        # ``.git`` records an absolute gitdir path; initing them in tmp would leave
        # pointers aimed at the discarded tmp path.
        tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
        _rmtree_force(tmp)
        try:
            await _run_git("clone", url, str(tmp))
            await _run_git("fetch", "origin", ref, cwd=tmp)
            await _run_git("checkout", "-f", ref, cwd=tmp)
            await _run_git("reset", "--hard", "FETCH_HEAD", cwd=tmp)
            # A stale dest (from an earlier aborted run) would block the move —
            # clear it explicitly rather than relying on replace() semantics.
            if dest.exists():
                _rmtree_force(dest)
            tmp.replace(dest)
        except Exception:
            _rmtree_force(tmp)
            raise
    else:
        # existing snapshot → fetch the ref and hard-reset (snapshot is disposable).
        # No ``--depth``: keep the full history so submodule commits stay reachable.
        await _run_git("fetch", "origin", ref, cwd=dest)
        await _run_git("checkout", "-f", ref, cwd=dest)
        await _run_git("reset", "--hard", "FETCH_HEAD", cwd=dest)


async def init_snapshot(
    git_id: Any,
    *,
    ref_override: Optional[str] = None,
    version_id: Optional[str] = None,
) -> dict[str, Any]:
    """克隆或拉取一个 git 配置对应的代码快照（幂等：目录已存在则 fetch+reset）。

    ``ref_override``：本版本为该仓选定的 ref（branch/tag/commit），优先于
    仓库配置里的 ``ref_value``。``version_id``：把快照落到版本目录，避免同一
    仓库不同版本的 checkout 互相踩。

    **ref 自愈**（修 "couldn't find remote ref main"）：版本行里可能存着历史脏 ref
    （远端只有 master 却存了 main）。这里先 ``ls-remote`` 校验 ref 是否真在远端；
    不在就退回**探测远端默认分支**，并在返回值里带 ``ref_corrected=True`` +
    ``requested_ref``，让调用方（构建代码图谱）把纠正后的 ref **写回版本行**——
    一次修好，之后不再重复。

    Returns ``{"project_id", "git_id", "version_id", "path", "ref", "commit",
    "requested_ref", "ref_corrected", "fresh_clone", "submodule_warning"}``.
    """
    git = await get_git(git_id)
    project = await get_project(git.project_id)

    dest = _snapshot_path(str(project.id), str(git.id), version_id=version_id)
    token = await _resolve_credential(git.credential_ref)
    url = _auth_url(git.repo_url, token)
    # ref 优先级：版本选定 > 仓库配置 > 探测远端默认分支
    requested = (ref_override or git.ref_value or "").strip()
    ref_corrected = False
    if requested and await _ref_missing_on_remote(url, requested):
        ref = await _detect_default_branch(url)
        ref_corrected = ref != requested
    elif requested:
        ref = requested
    else:
        ref = await _detect_default_branch(url)

    fresh_clone = not (dest / ".git").is_dir()
    submodule_warning: Optional[str] = None
    try:
        await _materialize(dest, url, ref, fresh_clone=fresh_clone)
    except GitSourceError as exc:
        # 兜底第二层：ls-remote 判不准（TOCTOU / 服务端行为差异）时 fetch 仍可能报
        # "couldn't find remote ref"。这时再探一次默认分支重试一次。
        msg = str(exc).lower()
        if "couldn't find remote ref" in msg and ref:
            detected = await _detect_default_branch(url)
            if detected and detected != ref:
                ref = detected
                ref_corrected = detected != requested
                fresh_clone = not (dest / ".git").is_dir()
                await _materialize(dest, url, ref, fresh_clone=fresh_clone)
            else:
                raise
        else:
            raise

    # Submodules: the platform's README clones with ``--recurse-submodules`` because
    # submodule dirs (node_server/nodes/mobile/…) hold most of the code. We pull them
    # **best-effort** — a broken or unreachable submodule URL must not fail the whole
    # snapshot (the main repo still yields a usable graph). No-op when the repo has no
    # ``.gitmodules``. Runs at ``dest`` (final path) so submodule gitdir pointers are
    # absolute-correct. Full clone above means the pinned submodule commits are all
    # reachable, so this actually populates the dirs.
    try:
        await _run_git("submodule", "update", "--init", "--recursive", cwd=dest)
    except GitSourceError as exc:
        submodule_warning = str(exc)

    commit = await _run_git("rev-parse", "HEAD", cwd=dest)
    return {
        "project_id": str(project.id),
        "git_id": str(git.id),
        "version_id": str(version_id) if version_id else None,
        "path": str(dest),
        "ref": ref,
        "commit": commit,
        "requested_ref": requested or None,
        "ref_corrected": ref_corrected,
        "fresh_clone": fresh_clone,
        "submodule_warning": submodule_warning,
    }


async def sync_all(project_id: Any) -> dict[str, Any]:
    """同步一个测试项目下所有 active git 配置的快照。"""
    from .service import list_gits

    gits = await list_gits(project_id, status="active")
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for git in gits:
        try:
            results.append(await init_snapshot(git.id))
        except (GitSourceError, LookupError) as exc:
            errors.append({"git_id": str(git.id), "name": git.name, "error": str(exc)})
    return {"synced": len(results), "failed": len(errors), "results": results, "errors": errors}


def snapshot_path(project_id: Any, git_id: Any, *, version_id: Optional[str] = None) -> Path:
    """静态查快照路径（不落库、不克隆），供 Graphify 定位。"""
    return _snapshot_path(str(project_id), str(git_id), version_id=version_id)


__all__ = [
    "GitSourceError",
    "init_snapshot",
    "snapshot_path",
    "sync_all",
]
