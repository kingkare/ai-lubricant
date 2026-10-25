"""浏览器插件密码管理器：管理端 API（/devices?kind=cdp 页面的密码列表入口）。

密码归属是**用户级**的（`browser_passwords.owner_user_id` = C 端用户 id），
不挂在某个 CDP 客户端上——同一个用户的所有浏览器客户端共享同一份密码库。
所以这里没有 resource_id 路径参数，owner 恒为当前登录用户。

安全边界：
- 归属恒为当前用户（``str(user.id)``），不存在跨用户读取的路径。
- 列表只回 meta（id/domain/username/url/updatedAt），**永不含密文/明文**。
- 明文查看需**二次鉴权**：请求体带当前账号密码，``verify_password`` 校验通过才解密。
- 明文查看写审计：``security_events``（``tag="browser_password_admin"``，
  ``severity="warning"``），detail 只记 owner/entry_id/domain，**不含密码**。
- 本模块不得把明文写进日志。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .crypto import verify_password
from .deps import get_current_user
from .models import User

router = APIRouter(
    prefix="/api/v1/users/builtin-tools/browser-passwords",
    tags=["user-platform-browser-passwords"],
)


class BrowserPasswordViewReq(BaseModel):
    entry_id: int = Field(..., description="要查看的记录 ID")
    password: str = Field(..., description="当前账号密码，用于二次鉴权")


class BrowserPasswordDeleteReq(BaseModel):
    entry_id: int = Field(..., description="要删除的记录 ID")


class BrowserPasswordUpdateReq(BaseModel):
    entry_id: int = Field(..., description="要编辑的记录 ID")
    domain: str | None = Field(default=None, description="新域名（留空则不改）")
    username: str | None = Field(default=None, description="新账号（留空则不改）")
    password: str | None = Field(default=None, description="新密码（留空则不改）")
    url: str | None = Field(default=None, description="新 URL（留空则不改）")


class BrowserPasswordCreateReq(BaseModel):
    domain: str = Field(..., description="站点（含端口，如 127.0.0.1:7890）")
    username: str = Field(..., description="账号")
    password: str = Field(..., description="密码")
    url: str = Field(default="", description="完整 URL（可选）")


def _meta(row) -> dict:
    return {
        "id": row["id"],
        "domain": row["domain"],
        "username": row["username"],
        "url": row["url"] or "",
        "source": row["source"] if "source" in row else "browser",
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


# 存储层 ValueError code → 中文（与 mcp_runtime/sse_gateway 的 _PWD_VALUE_ERRORS 同口径）
_VALUE_ERROR_MESSAGES = {
    "domain_required": "站点不能为空",
    "username_required": "账号不能为空",
    "password_required": "密码不能为空",
    "password_too_long": "密码过长（最多 1024 字符）",
    "username_too_long": "账号过长（最多 255 字符）",
    "domain_too_long": "站点过长（最多 255 字符）",
    "password_quota_exceeded": "保存的密码数量已达上限（500 条）",
    "duplicate_entry": "该站点下已存在同名账号记录",
}


def _is_admin(user: User) -> bool:
    return str(user.role or "").lower() == "admin"


async def _resolve_owner_for_entry(conn, entry_id: int, user: User) -> str | None:
    """按 id 取一条记录的 owner；管理员可读任意记录，普通用户只能读自己的。

    返回 owner_user_id 或 None（不存在/越权都返回 None，不泄漏「存在但不属于你」）。
    """
    if _is_admin(user):
        row = await conn.fetchrow(
            "SELECT owner_user_id FROM browser_passwords WHERE id=$1", int(entry_id)
        )
    else:
        row = await conn.fetchrow(
            "SELECT owner_user_id FROM browser_passwords WHERE id=$1 AND owner_user_id=$2",
            int(entry_id), str(user.id),
        )
    return str(row["owner_user_id"]) if row and row["owner_user_id"] else None


async def _owner_label(conn, owner_user_id: str) -> str:
    """owner_user_id → 可读标签（邮箱/名字），用于管理员列表的「归属」列。"""
    row = await conn.fetchrow(
        "SELECT name, email FROM mc_users WHERE id=$1::uuid", owner_user_id
    )
    if not row:
        return owner_user_id
    name = row["name"] or ""
    email = row["email"] or ""
    if email and name:
        return f"{name} <{email}>"
    return email or name or owner_user_id


@router.get("")
async def list_browser_passwords(user: User = Depends(get_current_user)) -> dict:
    """列出密码元数据（**不含明文/密文**）。

    管理员看**全部用户**的（带「归属」列），普通用户只看自己的。这是「管理」
    的本意——之前只按自己 id 过滤，管理员登录看到的是空列表。
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise HTTPException(status_code=503, detail="服务暂不可用")
    async with PostgresClient.pool.acquire() as conn:
        if _is_admin(user):
            rows = await conn.fetch(
                "SELECT id, owner_user_id, domain, username, url, source, "
                "created_at, updated_at FROM browser_passwords "
                "ORDER BY updated_at DESC"
            )
            entries = []
            for r in rows:
                m = _meta(r)
                m["ownerLabel"] = await _owner_label(conn, r["owner_user_id"])
                entries.append(m)
        else:
            rows = await conn.fetch(
                "SELECT id, domain, username, url, source, created_at, updated_at "
                "FROM browser_passwords WHERE owner_user_id=$1 ORDER BY updated_at DESC",
                str(user.id),
            )
            entries = [_meta(r) for r in rows]
    return {"ok": True, "entries": entries, "admin": _is_admin(user)}


@router.post("/create")
async def create_browser_password(
    body: BrowserPasswordCreateReq,
    user: User = Depends(get_current_user),
) -> dict:
    """网页端手动新增一条密码记录（管理页的「新增」）。

    走 ``upsert_password``（同一 (owner, domain, username) 已存在时覆盖，与插件
    自动保存同一口径），``source="user"`` 标明是用户手填的。
    """
    from browser_password_store import upsert_password

    try:
        meta = await upsert_password(
            str(user.id),
            domain=body.domain, username=body.username,
            password=body.password, url=body.url,
            source="user",
        )
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(status_code=422, detail=_VALUE_ERROR_MESSAGES.get(code, "参数无效"))
    return {"ok": True, "entry": meta}


@router.post("/view")
async def view_browser_password(
    body: BrowserPasswordViewReq,
    user: User = Depends(get_current_user),
) -> dict:
    """明文查看单条密码。**需二次鉴权**：请求体带当前账号密码。

    管理员例外：管理员本身已有全量管理权限，不再要求二次输入密码（仍写审计，
    且审计里记 `viewer_user_id` 便于追责）。
    """
    from db import PostgresClient
    from browser_password_crypto import decrypt_password

    if not _is_admin(user) and not verify_password(body.password, user.password or ""):
        raise HTTPException(status_code=401, detail="账号密码不正确，无法查看")

    async with PostgresClient.pool.acquire() as conn:
        owner = await _resolve_owner_for_entry(conn, body.entry_id, user)
        if not owner:
            raise HTTPException(status_code=404, detail="记录不存在")
        row = await conn.fetchrow(
            "SELECT id, domain, username, password_enc, url, source, created_at, updated_at "
            "FROM browser_passwords WHERE id=$1",
            int(body.entry_id),
        )
        try:
            plaintext = decrypt_password(row["password_enc"])
        except Exception:
            # 密钥轮换 / 密文损坏：给可操作提示，不暴露底层异常
            raise HTTPException(status_code=409, detail="记录无法解密（服务端密钥已变更）")

        await conn.execute(
            "INSERT INTO security_events (event_type, severity, tag, detail) "
            "VALUES ($1,$2,$3,$4::jsonb)",
            "browser_password_viewed",
            "warning",
            "browser_password_admin",
            json.dumps(
                {
                    "owner_user_id": owner,
                    "entry_id": int(row["id"]),
                    "domain": row["domain"],
                    "viewer_user_id": str(user.id),  # 谁看的（管理员审计关键字段）
                },
                ensure_ascii=False,
            ),
        )

    return {"ok": True, "entry": {**_meta(row), "password": plaintext}}


@router.post("/update")
async def update_browser_password(
    body: BrowserPasswordUpdateReq,
    user: User = Depends(get_current_user),
) -> dict:
    """编辑一条密码记录（网页端「设置」按钮）。

    按 id 定位，**不新增行**；只更新传入的字段（`None` = 不改）。
    改密码不需要二次鉴权（是你自己的数据，且写入方向不泄漏明文）；
    但会写审计，source 标为 `user`（「最后更新者」）。
    """
    from browser_password_store import update_password

    try:
        meta = await update_password(
            str(user.id), int(body.entry_id),
            domain=body.domain, username=body.username,
            password=body.password, url=body.url,
            allow_any_owner=_is_admin(user),
        )
    except ValueError as exc:
        code = str(exc)
        if code == "duplicate_entry":
            raise HTTPException(status_code=409, detail="该站点下已存在同名账号记录")
        raise HTTPException(status_code=422, detail=_VALUE_ERROR_MESSAGES.get(code, "参数无效"))
    if meta is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True, "entry": meta}


@router.post("/delete")
async def delete_browser_password(
    body: BrowserPasswordDeleteReq,
    user: User = Depends(get_current_user),
) -> dict:
    """删除一条密码记录。管理员可删任意用户的，普通用户只能删自己的。"""
    from db import PostgresClient
    from browser_password_store import delete_password

    async with PostgresClient.pool.acquire() as conn:
        owner = await _resolve_owner_for_entry(conn, body.entry_id, user)
    if not owner:
        raise HTTPException(status_code=404, detail="记录不存在")
    if not await delete_password(owner, int(body.entry_id)):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True, "deleted": True}

