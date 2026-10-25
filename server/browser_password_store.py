"""浏览器插件密码管理器存储层（``browser_passwords`` 表）。

风格对齐 ``resource_store`` / ``marketplace_leaderboard_store``：模块级函数 +
函数内延迟 import PostgresClient。

归属模型：调用方传 ``owner_user_id``（由已鉴权 CDP 客户端 token → client_id →
``mcp_plugin_store.get_cdp_client_owner_user_id`` 解析而来）。**本层不做鉴权**，
owner 由上层决定；所有查询都带 ``owner_user_id`` 条件，缺失与越权都返回
``None``/``False``（不泄漏「记录存在但不属于你」这一信息）。

明文边界：``password_enc`` 恒为 AES-256-GCM 密文。``list_passwords`` 的 SQL
**不选** ``password_enc`` 列（列表永不带密文）；只有 ``get_password`` 解密单条。
"""
from __future__ import annotations

import json
from typing import Any

from browser_password_crypto import decrypt_password, encrypt_password

MAX_DOMAIN_LEN = 255
MAX_USERNAME_LEN = 255
MAX_PASSWORD_LEN = 1024
MAX_URL_LEN = 2048
MAX_ENTRIES_PER_USER = 500
LIST_LIMIT = 1000

# 审计事件类型（只记 id 与域名，绝不记 username/password）。
EVENT_SAVED = "browser_password_saved"
EVENT_DELETED = "browser_password_deleted"
AUDIT_TAG = "browser_password"


def _pool():
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("postgres pool not initialized")
    return PostgresClient.pool


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _row_to_meta(row: Any) -> dict:
    """行 → 列表/回执用 meta。**恒不含** password / password_enc。"""
    return {
        "id": row["id"],
        "domain": row["domain"],
        "username": row["username"],
        "url": row.get("url") or "",
        "source": row.get("source") or "browser",
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


async def _audit(conn, *, event_type: str, owner_user_id: str, entry_id: int, domain: str) -> None:
    """写 security_events。detail 只含归属与定位信息，无 username、绝无密码。"""
    await conn.execute(
        "INSERT INTO security_events (event_type, severity, tag, detail) VALUES ($1,$2,$3,$4::jsonb)",
        event_type,
        "info",
        AUDIT_TAG,
        json.dumps(
            {"owner_user_id": str(owner_user_id), "entry_id": int(entry_id), "domain": domain},
            ensure_ascii=False,
        ),
    )


async def count_passwords(owner_user_id: str) -> int:
    pool = _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM browser_passwords WHERE owner_user_id=$1",
            str(owner_user_id),
        ) or 0)


async def list_passwords(owner_user_id: str, domain: str | None = None) -> list[dict]:
    """列 meta（不含密文/明文）。``domain`` 归一为小写；空则不过滤。"""
    where = ["owner_user_id=$1"]
    args: list[Any] = [str(owner_user_id)]
    normalized = str(domain).strip().lower() if domain else ""
    if normalized:
        args.append(normalized)
        where.append(f"domain=${len(args)}")
    pool = _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, domain, username, url, source, created_at, updated_at "
            "FROM browser_passwords "
            f"WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT {LIST_LIMIT}",
            *args,
        )
    return [_row_to_meta(dict(r)) for r in rows]


async def get_password(owner_user_id: str, entry_id: int) -> dict | None:
    """取单条并解密。缺失或越权返回 ``None``（无 oracle）。"""
    pool = _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, domain, username, password_enc, url, source, created_at, updated_at "
            "FROM browser_passwords WHERE id=$1 AND owner_user_id=$2",
            int(entry_id), str(owner_user_id),
        )
    if not row:
        return None
    row = dict(row)
    return {**_row_to_meta(row), "password": decrypt_password(row["password_enc"])}


async def upsert_password(
    owner_user_id: str, *, domain: str, username: str, password: str,
    url: str = "", source: str = "browser",
) -> dict:
    """按 (owner, domain, username) upsert，覆盖密文。返回 meta。

    校验失败抛 ``ValueError("<code>")``（code 见下），由帧层翻中文消息。
    ``source`` 标注这次写入来自哪：``browser``（插件自动保存）/ ``user``（网页端
    手动编辑）。取「最后写入者」语义——网页上展示的「最后更新」就是真实的最后
    一次改动来源。
    """
    domain = str(domain or "").strip().lower()
    username = str(username or "").strip()
    password = str(password or "")
    url = str(url or "").strip()
    if not domain:
        raise ValueError("domain_required")
    if not username:
        raise ValueError("username_required")
    if not password:
        raise ValueError("password_required")
    if len(domain) > MAX_DOMAIN_LEN:
        raise ValueError("domain_too_long")
    if len(username) > MAX_USERNAME_LEN:
        raise ValueError("username_too_long")
    if len(password) > MAX_PASSWORD_LEN:
        raise ValueError("password_too_long")
    url = url[:MAX_URL_LEN]

    encrypted = encrypt_password(password)
    pool = _pool()
    async with pool.acquire() as conn:
        # 配额：仅拦「新行」。已存在的 (owner,domain,username) 更新不算新增。
        if await count_passwords(owner_user_id) >= MAX_ENTRIES_PER_USER:
            exists = await conn.fetchval(
                "SELECT 1 FROM browser_passwords WHERE owner_user_id=$1 AND domain=$2 AND username=$3",
                str(owner_user_id), domain, username,
            )
            if not exists:
                raise ValueError("password_quota_exceeded")
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO browser_passwords (owner_user_id, domain, username, password_enc, url, source)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (owner_user_id, domain, username)
                DO UPDATE SET password_enc=EXCLUDED.password_enc, url=EXCLUDED.url,
                              source=EXCLUDED.source, updated_at=now()
                RETURNING id, domain, username, url, source, created_at, updated_at
                """,
                str(owner_user_id), domain, username, encrypted, url, source,
            )
            meta = _row_to_meta(dict(row))
            await _audit(
                conn, event_type=EVENT_SAVED, owner_user_id=owner_user_id,
                entry_id=meta["id"], domain=meta["domain"],
            )
    return meta


async def update_password(
    owner_user_id: str, entry_id: int, *,
    domain: str | None = None, username: str | None = None,
    password: str | None = None, url: str | None = None,
    allow_any_owner: bool = False,
) -> dict | None:
    """编辑单条记录（网页端「设置」按钮）。只更新传入的字段。

    与 ``upsert_password`` 的区别：这里按 ``id`` 定位，且**不新增行**——id 不存在
    或不属于该 owner 时返回 ``None``（无 oracle）。改 ``(domain, username)`` 可能
    撞上另一行的唯一键，此时抛 ``ValueError("duplicate_entry")``。
    ``password`` 为空串 / ``None`` 表示不改密码（避免误清空）。

    ``allow_any_owner=True``（管理员）时不加 owner 过滤，可改任意记录；审计仍按
    记录真实 owner 记。
    """
    pool = _pool()
    sets: list[str] = []
    args: list[Any] = []

    def _add(col: str, value: Any) -> None:
        args.append(value)
        sets.append(f"{col}=${len(args)}")

    if domain is not None:
        d = str(domain).strip().lower()
        if not d:
            raise ValueError("domain_required")
        if len(d) > MAX_DOMAIN_LEN:
            raise ValueError("domain_too_long")
        _add("domain", d)
    if username is not None:
        u = str(username).strip()
        if not u:
            raise ValueError("username_required")
        if len(u) > MAX_USERNAME_LEN:
            raise ValueError("username_too_long")
        _add("username", u)
    if url is not None:
        _add("url", str(url).strip()[:MAX_URL_LEN])
    if password:
        if len(password) > MAX_PASSWORD_LEN:
            raise ValueError("password_too_long")
        _add("password_enc", encrypt_password(password))

    owner_clause = "" if allow_any_owner else " AND owner_user_id=$OWNER"
    if not sets:
        # 没有任何字段要改：直接回当前 meta（前端刷新用）。
        sql = (
            "SELECT id, domain, username, url, source, created_at, updated_at "
            "FROM browser_passwords WHERE id=$1"
            + ("" if allow_any_owner else " AND owner_user_id=$2")
        )
        params = [int(entry_id)] if allow_any_owner else [int(entry_id), str(owner_user_id)]
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)
        return _row_to_meta(dict(row)) if row else None

    # 网页端编辑 → source 标 user（「最后更新者」语义）
    _add("source", "user")
    sets.append("updated_at=now()")
    if allow_any_owner:
        args.append(int(entry_id))
        sql = (
            f"UPDATE browser_passwords SET {', '.join(sets)} "
            f"WHERE id=${len(args)} "
            "RETURNING id, owner_user_id, domain, username, url, source, created_at, updated_at"
        )
    else:
        args.extend([int(entry_id), str(owner_user_id)])
        sql = (
            f"UPDATE browser_passwords SET {', '.join(sets)} "
            f"WHERE id=${len(args) - 1} AND owner_user_id=${len(args)} "
            "RETURNING id, owner_user_id, domain, username, url, source, created_at, updated_at"
        )
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                row = await conn.fetchrow(sql, *args)
                if not row:
                    return None
                meta = _row_to_meta(dict(row))
                # 审计按记录真实 owner 记（管理员改他人条目时不能记成管理员自己的）
                await _audit(
                    conn, event_type=EVENT_SAVED,
                    owner_user_id=str(row["owner_user_id"]),
                    entry_id=meta["id"], domain=meta["domain"],
                )
        except Exception as exc:  # 唯一键冲突 → 明确告知，不吞成 500
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ValueError("duplicate_entry") from exc
            raise
    return meta


async def delete_password(owner_user_id: str, entry_id: int) -> bool:
    """按 id + owner 删除。删到返回 True，否则 False（缺失或越权）。"""
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM browser_passwords WHERE id=$1 AND owner_user_id=$2 "
                "RETURNING id, domain",
                int(entry_id), str(owner_user_id),
            )
            if not row:
                return False
            await _audit(
                conn, event_type=EVENT_DELETED, owner_user_id=owner_user_id,
                entry_id=int(row["id"]), domain=row["domain"],
            )
    return True
