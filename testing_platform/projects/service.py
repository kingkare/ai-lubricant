"""Test-project domain service — CRUD + release snapshot/diff.

Thin layer over ``mc_test_project*`` (design ``test-project-definition.md`` §十).
Code generation follows ``assets.service._next_code``: ``TP-0001`` is **project
scoped** (the project's own sequence, not a child-of-a-parent sequence).

Release diff is the incremental-testing foundation (§三 版本横切):
two releases diff on ``git_tags`` / ``prd_versions`` / ``case_versions`` and
report which pointers moved → which sources need re-fetch → which anchors stale.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from uuid import UUID as _UUID

from tortoise.exceptions import DoesNotExist

from .models import (
    GIT_REF_TYPES,
    GIT_STATUSES,
    PROJECT_STATUSES,
    SOURCE_ADAPTERS,
    SOURCE_KINDS,
    SOURCE_STATUSES,
    SOURCE_SYNC_POLICIES,
    VERSION_STATUSES,
    TestProject,
    TestProjectGit,
    TestProjectRelease,
    TestProjectSource,
    TestProjectVersion,
)

PROJECT_CODE_PREFIX = "TP-"

#: 项目/版本级角色成员字段（用户定版 2026-10-15：测试 / 开发 / 产品三类，
#: 对齐禅道团队角色；存平台用户 UUID 列表，JSONB 不做 FK）。
ROLE_FIELDS: tuple[str, ...] = ("testers", "developers", "product_managers")


class ProjectNotFoundError(LookupError):
    """测试项目不存在。"""


class ProjectValidationError(ValueError):
    """入参不合法（枚举/必填/冲突）。"""


def _as_uuid(value: Any, *, field: str) -> _UUID:
    if isinstance(value, _UUID):
        return value
    try:
        return _UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProjectValidationError(f"{field} 不是合法 UUID：{value!r}") from exc


def _maybe_uuid(value: Any, *, field: str) -> Optional[_UUID]:
    if value is None or value == "":
        return None
    return _as_uuid(value, field=field)


def _validate_enum(value: Any, allowed: Sequence[str], *, field: str, required: bool) -> Any:
    if value is None:
        if required:
            raise ProjectValidationError(f"{field} 必填（可选值：{'/'.join(allowed)}）")
        return None
    if value not in allowed:
        raise ProjectValidationError(f"{field}={value!r} 不在允许集合 {list(allowed)} 内")
    return value


async def _next_project_code() -> str:
    """``TP-`` + 4 位递增（**全平台序列**，不按子对象分段）。"""
    codes = await TestProject.all().values_list("code", flat=True)
    numbers = [int(c[len(PROJECT_CODE_PREFIX):]) for c in codes
               if isinstance(c, str) and c.startswith(PROJECT_CODE_PREFIX)
               and c[len(PROJECT_CODE_PREFIX):].isdigit()]
    return f"{PROJECT_CODE_PREFIX}{(max(numbers) + 1) if numbers else 1:04d}"


def _normalize_role_list(value: Any) -> list[str]:
    """角色成员归一：UUID/str → str(id) 去重保序的列表；``None`` → 空列表。

    前端可能传单值（字符串）或数组；都归一成去重的 str UUID 列表。空值存为空列表
    （不是 null）——与 ``JSONField(default=list)`` 一致，前端读取恒为数组。
    """
    if value is None:
        return []
    if isinstance(value, (str, _UUID)):
        seq = [value]
    elif isinstance(value, (list, tuple, set)):
        seq = list(value)
    else:
        raise ProjectValidationError(f"角色成员列表不合法：{value!r}（应为 id 列表）")
    out: list[str] = []
    seen: set[str] = set()
    for raw in seq:
        if raw is None or raw == "":
            continue
        sid = str(_as_uuid(raw, field="role_id"))
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


#: 已确保过的「后加列」集合（进程内缓存，避免每次调用都查库）。
_ENSURED_COLUMNS: set[str] = set()


async def ensure_bridge_column() -> None:
    """确保 ``mc_test_project.coding_project_id`` 列存在（幂等，运行时自愈）。

    背景：``Tortoise.generate_schemas(safe=True)`` 只建缺失的**表**，不 ALTER 已存在表
    补新列。测试域是独立 app label，历史上不在 ``_ensure_additive_columns`` 的补列
    范围内——已建库加 ``coding_project_id`` 后若不重启服务，建项目会报
    ``column "coding_project_id" does not exist``。

    这里在建/改项目前**幂等补列**（``ADD COLUMN IF NOT EXISTS``），让不重启也能用；
    启动路径的 :func:`user_platform.database._ensure_additive_columns` 仍是兜底。
    列已存在（或补列失败——如权限不足）都不阻断业务：真正缺列时后续查询会照常报错。
    """
    if "coding_project_id" in _ENSURED_COLUMNS:
        return
    try:
        from tortoise import Tortoise

        # 连接别名：生产（database.py 配的是 user_platform）；测试 db_url 用 default。
        conn = None
        for alias in ("user_platform", "default"):
            try:
                conn = Tortoise.get_connection(alias)
                break
            except Exception:  # noqa: BLE001 — 别名不存在（ConfigurationError/KeyError）
                continue
        if conn is None:
            return

        # 先探测列是否存在，再决定 ALTER —— 不用 `IF NOT EXISTS`（SQLite 不支持该语法，
        # 会静默失败；PG 支持但两库统一走探测更稳）。探测按库方言走 information_schema
        # 或 PRAGMA。
        exists = await _column_exists(conn, "mc_test_project", "coding_project_id")
        if not exists:
            await conn.execute_script(
                "ALTER TABLE mc_test_project ADD COLUMN coding_project_id UUID;"
            )
        # 只有**成功**才缓存（失败不缓存，下次继续试——避免一次锁/权限失败后永久不补）
        _ENSURED_COLUMNS.add("coding_project_id")
    except Exception:  # noqa: BLE001 — 表不存在/连接未就绪/权限不足都不该阻断业务
        import logging

        logging.getLogger(__name__).debug(
            "[testing-projects] ensure coding_project_id skipped", exc_info=True
        )


async def _column_exists(conn: Any, table: str, column: str) -> bool:
    """跨库探测列是否存在（PG 用 information_schema；SQLite 用 PRAGMA）。"""
    try:
        rows = await conn.execute_query_dict(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            [table, column],
        )
        if rows:
            return True
        # PG 查询成功但空 → 列确实不存在（但要排除「表在别的 schema」的极端情况：
        # information_schema 默认只看 search_path，够用）。
        return False
    except Exception:  # noqa: BLE001 — SQLite 没有 information_schema
        rows = await conn.execute_query_dict(f"PRAGMA table_info({table})")
        return any(r.get("name") == column for r in rows)


# ── 测试项目 ────────────────────────────────────────────────────
async def create_project(
    *,
    name: str,
    description: Optional[str] = None,
    created_by: Any = None,
    status: str = "active",
    code: Optional[str] = None,
    coding_project_id: Any = None,
    testers: Any = None,
    developers: Any = None,
    product_managers: Any = None,
) -> TestProject:
    """建测试项目。``code`` 缺省取 ``TP-xxxx``（可显式给）。

    ``coding_project_id``：关联的 coding 项目（桥，可选——bug 域复用 ProjectIssue
    时经它解析 coding 项目 id；design「将来 coding 作为子集接入」的现期形态）。
    ``testers``/``developers``/``product_managers``：项目级角色成员（平台用户
    id 列表；测试/开发/产品三类，用户定版 2026-10-15）。
    """
    if not name:
        raise ProjectValidationError("测试项目 name 必填")
    resolved_status = _validate_enum(status, PROJECT_STATUSES, field="status", required=False) or "active"
    if code:
        exists = await TestProject.filter(code=code).exists()
        if exists:
            raise ProjectValidationError(f"测试项目 code 已存在：{code!r}")
    await ensure_bridge_column()
    return await TestProject.create(
        code=code or await _next_project_code(),
        name=str(name),
        description=description,
        status=resolved_status,
        coding_project_id=_maybe_uuid(coding_project_id, field="coding_project_id"),
        testers=_normalize_role_list(testers),
        developers=_normalize_role_list(developers),
        product_managers=_normalize_role_list(product_managers),
        created_by=_maybe_uuid(created_by, field="created_by"),
    )


async def get_project(project_id: Any) -> TestProject:
    await ensure_bridge_column()
    try:
        return await TestProject.get(id=_as_uuid(project_id, field="project_id"))
    except DoesNotExist:
        raise ProjectNotFoundError(f"测试项目不存在：{project_id!r}") from None


async def get_project_by_code(code: str) -> TestProject:
    try:
        return await TestProject.get(code=str(code))
    except DoesNotExist:
        raise ProjectNotFoundError(f"测试项目不存在：{code!r}") from None


async def resolve_coding_project_id(test_project_id: Any) -> Optional[_UUID]:
    """测试项目 → 关联的 coding 项目 id（桥，可能为 None）。

    缺陷域（``bug = ProjectIssue``）复用平台 issue，必须挂在 coding 项目下；
    测试项目经 ``coding_project_id`` 桥接。未关联时返回 ``None``，调用方决定
    （当前是拒建 + 可读错误，见 ``defects.service.create_bug_from_failure``）。
    """
    project = await get_project(test_project_id)
    return project.coding_project_id


async def list_projects(*, status: Optional[str] = None) -> list[TestProject]:
    await ensure_bridge_column()
    qs = TestProject.all()
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("code")


async def update_project(
    project_id: Any,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    version: Optional[int] = None,
    coding_project_id: Any = ...,
    testers: Any = ...,
    developers: Any = ...,
    product_managers: Any = ...,
    change_reason: Optional[str] = None,
) -> TestProject:
    """改测试项目。``status``/``version`` 变更走变更门（change_reason 必填）。

    ``coding_project_id``：显式传值则改桥（传 ``None`` 解绑）；不传则不动
    （用 ``...`` 哨兵区分「没传」与「传 None 解绑」）。角色成员三个字段同款
    哨兵语义：传列表即覆盖，传 ``None``/空列表即清空，不传则不动。
    """
    project = await get_project(project_id)
    updates: dict[str, Any] = {}
    if name is not None:
        if not name:
            raise ProjectValidationError("测试项目 name 不能为空")
        updates["name"] = str(name)
    if description is not None:
        updates["description"] = description
    if coding_project_id is not ...:
        updates["coding_project_id"] = _maybe_uuid(coding_project_id, field="coding_project_id")
    for field_name, value in (("testers", testers), ("developers", developers),
                              ("product_managers", product_managers)):
        if value is not ...:
            updates[field_name] = _normalize_role_list(value)
    if status is not None:
        updates["status"] = _validate_enum(status, PROJECT_STATUSES, field="status", required=True)
    if version is not None:
        if int(version) < 1:
            raise ProjectValidationError("version 必须 >= 1")
        updates["version"] = int(version)

    # 变更门：状态/版本这类结构性变更必须给出理由（design 变更必有因）
    structural = (status is not None and status != project.status) or (
        version is not None and int(version) != project.version
    )
    if structural and not change_reason:
        raise ProjectValidationError("status/version 变更必须提供 change_reason")

    for key, value in updates.items():
        setattr(project, key, value)
    if updates:
        await project.save()
    return project


async def archive_project(project_id: Any, *, change_reason: Optional[str] = None) -> TestProject:
    """归档测试项目（status=archived，不物理删除）。"""
    return await update_project(project_id, status="archived", change_reason=change_reason)


# ── git 配置 ────────────────────────────────────────────────────
async def create_git(
    *,
    project_id: Any,
    name: str,
    repo_url: str,
    ref_value: Optional[str] = None,
    ref_type: str = "branch",
    credential_ref: Optional[Mapping[str, Any]] = None,
    status: str = "active",
) -> TestProjectGit:
    """配 git 仓库（1-N）。``credential_ref`` 是引用（不存明文）。

    ``ref_value`` 可选——**分支属于版本**，不属仓库配置；留空时快照层
    自动探测远端默认分支（``git_source._detect_default_branch``）。
    """
    if not name:
        raise ProjectValidationError("git name 必填")
    if not repo_url:
        raise ProjectValidationError("git repo_url 必填")
    pid = _as_uuid(project_id, field="project_id")
    exists = await TestProjectGit.filter(project_id=pid, name=name).exists()
    if exists:
        raise ProjectValidationError(f"该项目下 git name 已存在：{name!r}")
    return await TestProjectGit.create(
        project_id=pid,
        name=str(name),
        repo_url=str(repo_url),
        ref_type=_validate_enum(ref_type, GIT_REF_TYPES, field="ref_type", required=False) or "branch",
        ref_value=str(ref_value or ""),
        credential_ref=dict(credential_ref or {}),
        status=_validate_enum(status, GIT_STATUSES, field="status", required=False) or "active",
    )


async def get_git(git_id: Any) -> TestProjectGit:
    try:
        return await TestProjectGit.get(id=_as_uuid(git_id, field="git_id"))
    except DoesNotExist:
        raise ProjectNotFoundError(f"git 配置不存在：{git_id!r}") from None


async def list_gits(
    project_id: Any,
    *,
    status: Optional[str] = None,
) -> list[TestProjectGit]:
    qs = TestProjectGit.filter(project_id=_as_uuid(project_id, field="project_id"))
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("name")


async def update_git(
    git_id: Any,
    *,
    repo_url: Optional[str] = None,
    ref_value: Optional[str] = None,
    ref_type: Optional[str] = None,
    credential_ref: Optional[Mapping[str, Any]] = None,
    status: Optional[str] = None,
) -> TestProjectGit:
    git = await get_git(git_id)
    if repo_url is not None:
        if not repo_url:
            raise ProjectValidationError("repo_url 不能为空")
        git.repo_url = str(repo_url)
    if ref_value is not None:
        git.ref_value = str(ref_value)
    if ref_type is not None:
        git.ref_type = _validate_enum(ref_type, GIT_REF_TYPES, field="ref_type", required=True)
    if credential_ref is not None:
        git.credential_ref = dict(credential_ref)
    if status is not None:
        git.status = _validate_enum(status, GIT_STATUSES, field="status", required=True)
    await git.save()
    return git


async def delete_git(git_id: Any) -> None:
    git = await get_git(git_id)
    await git.delete()


# ── 源配置 ──────────────────────────────────────────────────────
async def create_source(
    *,
    project_id: Any,
    kind: str,
    name: str,
    adapter: str = "builtin",
    adapter_type: str = "custom",
    config: Optional[Mapping[str, Any]] = None,
    sync_policy: str = "manual",
    schedule: Optional[Mapping[str, Any]] = None,
    status: str = "active",
) -> TestProjectSource:
    """配源（A/B 轨）。``kind`` 决定源性质，``adapter`` 决定轨道。"""
    if not name:
        raise ProjectValidationError("source name 必填")
    pid = _as_uuid(project_id, field="project_id")
    k = _validate_enum(kind, SOURCE_KINDS, field="kind", required=True)
    exists = await TestProjectSource.filter(project_id=pid, kind=k, name=name).exists()
    if exists:
        raise ProjectValidationError(f"该项目下 {k} 源 name 已存在：{name!r}")
    return await TestProjectSource.create(
        project_id=pid,
        kind=k,
        adapter=_validate_enum(adapter, SOURCE_ADAPTERS, field="adapter", required=False) or "builtin",
        adapter_type=str(adapter_type or "custom"),
        name=str(name),
        config=dict(config or {}),
        sync_policy=_validate_enum(sync_policy, SOURCE_SYNC_POLICIES, field="sync_policy", required=False)
        or "manual",
        schedule=dict(schedule or {}),
        status=_validate_enum(status, SOURCE_STATUSES, field="status", required=False) or "active",
    )


async def get_source(source_id: Any) -> TestProjectSource:
    try:
        return await TestProjectSource.get(id=_as_uuid(source_id, field="source_id"))
    except DoesNotExist:
        raise ProjectNotFoundError(f"源配置不存在：{source_id!r}") from None


async def list_sources(
    project_id: Any,
    *,
    kind: Optional[str] = None,
    adapter: Optional[str] = None,
    status: Optional[str] = None,
) -> list[TestProjectSource]:
    qs = TestProjectSource.filter(project_id=_as_uuid(project_id, field="project_id"))
    if kind:
        qs = qs.filter(kind=kind)
    if adapter:
        qs = qs.filter(adapter=adapter)
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("kind", "name")


async def update_source(
    source_id: Any,
    *,
    name: Optional[str] = None,
    adapter: Optional[str] = None,
    adapter_type: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    sync_policy: Optional[str] = None,
    schedule: Optional[Mapping[str, Any]] = None,
    status: Optional[str] = None,
) -> TestProjectSource:
    source = await get_source(source_id)
    if name is not None:
        if not name:
            raise ProjectValidationError("source name 不能为空")
        source.name = str(name)
    if adapter is not None:
        source.adapter = _validate_enum(adapter, SOURCE_ADAPTERS, field="adapter", required=True)
    if adapter_type is not None:
        source.adapter_type = str(adapter_type)
    if config is not None:
        source.config = dict(config)
    if sync_policy is not None:
        source.sync_policy = _validate_enum(sync_policy, SOURCE_SYNC_POLICIES, field="sync_policy", required=True)
    if schedule is not None:
        source.schedule = dict(schedule)
    if status is not None:
        source.status = _validate_enum(status, SOURCE_STATUSES, field="status", required=True)
    await source.save()
    return source


async def delete_source(source_id: Any) -> None:
    source = await get_source(source_id)
    await source.delete()


async def mark_source_synced(
    source_id: Any,
    *,
    result: Mapping[str, Any],
    status: str = "active",
) -> TestProjectSource:
    """同步后回写水位（``last_synced_at`` + ``last_sync_result``）。"""
    from tortoise.timezone import now as _tz_now

    source = await get_source(source_id)
    source.last_synced_at = _tz_now()
    source.last_sync_result = dict(result)
    source.status = _validate_enum(status, SOURCE_STATUSES, field="status", required=False) or "active"
    await source.save()
    return source


# ── 版本（一等实体）─────────────────────────────────────────────
async def _normalize_version_repos(
    repos: Optional[Sequence[Mapping[str, Any]]], *, project_id: Any
) -> list[dict[str, Any]]:
    """版本仓库项归一：校验 git_id 属于本项目、ref_type 枚举、is_primary 至多一项。

    每仓各自选 ref（branch/tag/commit），多仓多选；``is_primary=true`` 的仓是
    主分支（master/main，代表该版本的生成状态），至多一项，缺省落在首个仓。
    """
    if not repos:
        return []
    if not isinstance(repos, Sequence) or isinstance(repos, (str, bytes)):
        raise ProjectValidationError("repos 必须是数组")
    pid = _as_uuid(project_id, field="project_id")
    # 一次性取本项目全部 git 行，用于校验归属（避免逐项 await 查询）
    owned = {str(g.id): g for g in await list_gits(pid)}
    out: list[dict[str, Any]] = []
    seen_git_ids: set[str] = set()
    for raw in repos:
        if not isinstance(raw, Mapping):
            continue
        git_id = raw.get("git_id")
        if not git_id:
            raise ProjectValidationError("repos[].git_id 必填")
        gid = str(git_id)
        git = owned.get(gid)
        if git is None:
            raise ProjectValidationError(f"repos[].git_id 不属于本项目：{gid}")
        if gid in seen_git_ids:
            raise ProjectValidationError(f"repos[].git_id 重复：{gid}")
        seen_git_ids.add(gid)
        ref_type = _validate_enum(
            raw.get("ref_type") or "branch", GIT_REF_TYPES, field="repos[].ref_type", required=False
        ) or "branch"
        ref = str(raw.get("ref") or "").strip()
        # ref 留空**不兜底 "main"**——硬编码 main 会在远端默认分支是 master 时
        # 让 ``git fetch origin main`` 报 128（couldn't find remote ref main）。
        # 留空先回落该仓配置的 ref_value（用户显式指定的默认分支），仍空才由
        # ``init_snapshot`` 探测远端默认分支（``_detect_default_branch``）。
        if not ref:
            ref = str(getattr(git, "ref_value", "") or "").strip()
        out.append({
            "git_id": gid,
            "ref_type": ref_type,
            "ref": ref,
            "is_primary": bool(raw.get("is_primary")),
            "resolved_commit": str(raw.get("resolved_commit") or "") or None,
        })
    primaries = [r for r in out if r["is_primary"]]
    if len(primaries) > 1:
        raise ProjectValidationError("is_primary 至多一项")
    if out and not primaries:
        out[0]["is_primary"] = True
    return out


async def create_version(
    *,
    project_id: Any,
    version_code: str,
    name: Optional[str] = None,
    repos: Optional[Sequence[Mapping[str, Any]]] = None,
    note: Optional[str] = None,
    created_by: Any = None,
    testers: Any = None,
    developers: Any = None,
    product_managers: Any = None,
) -> TestProjectVersion:
    """建版本（一等实体）。首个版本自动 ``is_initial=true``。

    ``testers``/``developers``/``product_managers``：版本级角色成员（平台用户
    id 列表；可空——缺省看项目级，版本传值即覆盖）。
    """
    if not version_code:
        raise ProjectValidationError("version_code 必填")
    pid = _as_uuid(project_id, field="project_id")
    exists = await TestProjectVersion.filter(project_id=pid, version_code=str(version_code)).exists()
    if exists:
        raise ProjectValidationError(f"该项目下 version_code 已存在：{version_code!r}")
    normalized = await _normalize_version_repos(repos, project_id=pid)
    is_initial = not await TestProjectVersion.filter(project_id=pid).exists()
    if is_initial and normalized:
        normalized[0]["is_primary"] = True
    return await TestProjectVersion.create(
        project_id=pid,
        version_code=str(version_code),
        name=name,
        repos=normalized,
        is_initial=is_initial,
        status="draft",
        testers=_normalize_role_list(testers),
        developers=_normalize_role_list(developers),
        product_managers=_normalize_role_list(product_managers),
        note=note,
        created_by=_maybe_uuid(created_by, field="created_by"),
    )


async def get_version(version_id: Any) -> TestProjectVersion:
    try:
        return await TestProjectVersion.get(id=_as_uuid(version_id, field="version_id"))
    except DoesNotExist:
        raise ProjectNotFoundError(f"version 不存在：{version_id!r}") from None


async def list_versions(project_id: Any) -> list[TestProjectVersion]:
    return await TestProjectVersion.filter(
        project_id=_as_uuid(project_id, field="project_id")
    ).order_by("created_at")


async def update_version(
    version_id: Any,
    *,
    name: Optional[str] = None,
    repos: Optional[Sequence[Mapping[str, Any]]] = None,
    note: Optional[str] = None,
    status: Optional[str] = None,
    testers: Any = ...,
    developers: Any = ...,
    product_managers: Any = ...,
    change_reason: Optional[str] = None,
) -> TestProjectVersion:
    """改版本。改了 repos（结构变更）走变更门（change_reason 必填）。

    ``status`` 供图谱构建后回写（draft → graph_built），内部调用不强制理由。
    角色成员三个字段：传列表即覆盖（含空列表清空），不传则不动。
    """
    version = await get_version(version_id)
    if name is not None:
        version.name = name
    if note is not None:
        version.note = note
    if status is not None:
        version.status = _validate_enum(status, VERSION_STATUSES, field="status", required=True)
    for field_name, value in (("testers", testers), ("developers", developers),
                              ("product_managers", product_managers)):
        if value is not ...:
            setattr(version, field_name, _normalize_role_list(value))
    if repos is not None:
        normalized = await _normalize_version_repos(repos, project_id=version.project_id)
        if normalized != version.repos and not change_reason:
            raise ProjectValidationError("repos 变更必须提供 change_reason")
        version.repos = normalized
    await version.save()
    return version


async def resolve_version_roles(project_id: Any, version_id: Any) -> dict[str, list[str]]:
    """版本级角色成员（版本传值即覆盖，否则回退项目级）——「生效角色」。

    返回 ``{"testers": [...], "developers": [...], "product_managers": [...]}``，
    值是平台用户 id 列表（去重保序）。前端展示与策略默认分配都读这个。
    """
    project = await get_project(project_id)
    version = await get_version(version_id)
    if version.project_id != project.id:
        raise ProjectValidationError("version 不属于该 project")
    out: dict[str, list[str]] = {}
    for field_name in ROLE_FIELDS:
        version_value = _normalize_role_list(getattr(version, field_name))
        out[field_name] = version_value or _normalize_role_list(getattr(project, field_name))
    return out


async def delete_version(version_id: Any) -> None:
    version = await get_version(version_id)
    await version.delete()


# ── 版本指针快照（release，deprecated）───────────────────────────
async def create_release(
    *,
    project_id: Any,
    version_code: str,
    git_tags: Optional[Sequence[Mapping[str, Any]]] = None,
    prd_versions: Optional[Sequence[Mapping[str, Any]]] = None,
    case_versions: Optional[Sequence[Mapping[str, Any]]] = None,
    note: Optional[str] = None,
    created_by: Any = None,
) -> TestProjectRelease:
    """打版本（人工填 ``version_code``，各源指针快照）。"""
    if not version_code:
        raise ProjectValidationError("release version_code 必填")
    pid = _as_uuid(project_id, field="project_id")
    exists = await TestProjectRelease.filter(project_id=pid, version_code=str(version_code)).exists()
    if exists:
        raise ProjectValidationError(f"该项目下 version_code 已存在：{version_code!r}")
    return await TestProjectRelease.create(
        project_id=pid,
        version_code=str(version_code),
        git_tags=list(git_tags or []),
        prd_versions=list(prd_versions or []),
        case_versions=list(case_versions or []),
        note=note,
        created_by=_maybe_uuid(created_by, field="created_by"),
    )


async def get_release(release_id: Any) -> TestProjectRelease:
    try:
        return await TestProjectRelease.get(id=_as_uuid(release_id, field="release_id"))
    except DoesNotExist:
        raise ProjectNotFoundError(f"release 不存在：{release_id!r}") from None


async def list_releases(project_id: Any) -> list[TestProjectRelease]:
    return await TestProjectRelease.filter(
        project_id=_as_uuid(project_id, field="project_id")
    ).order_by("created_at")


def _index_pointers(rows: Any, *, id_key: str) -> dict[str, Any]:
    """把 ``[{id_key: X, value_key: V}]`` 索引成 ``{X: V}``（值键统一 ``ref``）。"""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    out: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        rid = row.get(id_key)
        if rid is None:
            continue
        out[str(rid)] = row.get("ref")
    return out


def diff_releases(
    base: TestProjectRelease,
    head: TestProjectRelease,
) -> dict[str, Any]:
    """两个 release 的**版本指针 diff**（增量测试根基）。

    三组指针（git_tags / prd_versions / case_versions）逐项比较，报告
    ``added``（新出现）、``removed``（消失）、``changed``（指针变了）。
    ``changed`` 的源 = 需要重新摄取 → 其锚点标 stale → 增量选例的输入。
    """
    def _norm(rows: Any, id_key: str) -> dict[str, Any]:
        return _index_pointers(rows, id_key=id_key)

    out: dict[str, Any] = {"base": base.version_code, "head": head.version_code,
                           "git_tags": {}, "prd_versions": {}, "case_versions": {}}
    for field_name, id_key in (("git_tags", "git_id"), ("prd_versions", "source_id"),
                               ("case_versions", "source_id")):
        b = _norm(getattr(base, field_name), id_key)
        h = _norm(getattr(head, field_name), id_key)
        changed = {k: {"from": b[k], "to": h[k]} for k in b.keys() & h.keys() if b[k] != h[k]}
        out[field_name] = {
            "added": sorted(h.keys() - b.keys()),
            "removed": sorted(b.keys() - h.keys()),
            "changed": changed,
            "changed_ids": sorted(changed.keys()),
        }
    return out


__all__ = [
    "GIT_REF_TYPES",
    "PROJECT_CODE_PREFIX",
    "ProjectNotFoundError",
    "ProjectValidationError",
    "archive_project",
    "create_git",
    "create_project",
    "create_release",
    "create_source",
    "create_version",
    "delete_git",
    "delete_source",
    "delete_version",
    "diff_releases",
    "get_git",
    "get_project",
    "get_project_by_code",
    "get_release",
    "get_source",
    "get_version",
    "list_gits",
    "list_projects",
    "list_releases",
    "list_sources",
    "list_versions",
    "mark_source_synced",
    "update_git",
    "update_project",
    "update_source",
    "update_version",
]
