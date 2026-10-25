"""CDP 身份验证器（TOTP）：管理端 API（/devices?kind=cdp 页的验证器管理入口）。

与密码管理器同构，但**安全口径更严**：

- 归属是**用户级**的（``totp_credentials.owner_user_id`` = C 端用户 id），
  同一用户的所有浏览器客户端共享一份验证器库，所以没有 resource_id 路径参数。
- 列表/详情**只回 meta + 当前验证码**，**不回明文密钥**。因此这里**没有 view 明文端点**——
  要看码就在列表里看当前 6 位（瞬时值，会翻转）。
- **唯一的明文出口是 ``/export``**（备份）：``build_otpauth_uri`` 拼出的文本含明文密钥，
  所以比照密码管理器的「查看明文」要求**二次鉴权**，且更严——管理员**没有旁路**，
  导出的永远只是自己的数据（导别人的验证器密钥 = 废掉对方 2FA）。
- 新增/编辑走声明式字段（issuer/account/domains/note）；**密钥只在新增时写入，
  之后不可改**（换密钥 = 站点重发二维码，重新新增覆盖同 (issuer,account)）。
- ``/import`` 批量导入 Aegis / andOTP / Bitwarden 导出的纯文本（每行一条 ``otpauth://``
  URI）；逐行解析、逐行 upsert，回 imported/overwritten/skipped + 行号级错误。
- 每次保存/删除/导入/导出写 ``security_events``（``tag="totp_credential"``），
  detail 不含密钥。导出记 ``severity="warning"``（明文外泄动作）。
- 本模块不得把密钥写进日志。
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .crypto import verify_password
from .deps import get_current_user
from .models import User

router = APIRouter(
    prefix="/api/v1/users/builtin-tools/totp-credentials",
    tags=["user-platform-totp"],
)


class TotpCreateReq(BaseModel):
    secret: str = Field(..., description="base32 密钥，或整条 otpauth:// URI（自动解析）")
    label: str = Field(default="", description="显示名（留空由 issuer/account 组合）")
    issuer: str = Field(default="", description="站点/服务名（如 GitHub）")
    account: str = Field(..., description="账号标识（如邮箱）；与 issuer 组成唯一键")
    domains: list[str] = Field(default_factory=list, description="关联网站域名列表")
    note: str = Field(default="", description="备注")
    url: str = Field(default="", description="登录页 URL（可选）")
    digits: int = Field(default=6, description="验证码位数（6/7/8）")
    period: int = Field(default=30, description="翻转周期秒（15~300）")
    algorithm: str = Field(default="SHA1", description="HMAC 算法（SHA1/SHA256/SHA512）")


class TotpUpdateReq(BaseModel):
    entry_id: int = Field(..., description="要编辑的记录 ID")
    label: str | None = Field(default=None, description="新显示名（None=不改）")
    issuer: str | None = Field(default=None, description="新站点名（None=不改）")
    account: str | None = Field(default=None, description="新账号（None=不改）")
    domains: list[str] | None = Field(default=None, description="新关联域名（None=不改）")
    note: str | None = Field(default=None, description="新备注（None=不改）")
    url: str | None = Field(default=None, description="新 URL（None=不改）")


class TotpDeleteReq(BaseModel):
    entry_id: int = Field(..., description="要删除的记录 ID")


class TotpCodeReq(BaseModel):
    entry_id: int = Field(..., description="要刷新验证码的记录 ID")


class TotpImportReq(BaseModel):
    content: str = Field(
        ...,
        description="Aegis / andOTP / Bitwarden 导出的纯文本：每行一条 otpauth:// URI",
    )
    domains: list[str] = Field(
        default_factory=list,
        description="批量关联到所有导入条目的域名；留空则用每条 URI 里的 issuer（小写）",
    )


class TotpExportReq(BaseModel):
    password: str = Field(..., description="当前账号密码，用于二次鉴权（导出含明文密钥）")


# 存储层 ValueError code → 中文（与 sse_gateway 的 _TOTP_VALUE_ERRORS 同口径）
_VALUE_ERROR_MESSAGES = {
    "secret_required": "密钥不能为空",
    "secret_invalid_base32": "密钥格式无效（需 base32，或粘贴完整 otpauth:// 链接）",
    "account_required": "账号不能为空",
    "domains_required": "至少关联一个网站域名",
    "digits_unsupported": "验证码位数仅支持 6/7/8",
    "period_unsupported": "翻转周期需在 15~300 秒之间",
    "algorithm_unsupported": "算法仅支持 SHA1/SHA256/SHA512",
    "totp_quota_exceeded": "保存的验证器数量已达上限（300 条）",
    "duplicate_entry": "该站点下已存在同名账号的验证器",
    "uri_required": "otpauth 链接不能为空",
    "uri_not_otpauth": "不是 otpauth:// 链接",
    "uri_not_totp": "仅支持 TOTP（时间型）验证器",
    "invalid_blob": "记录无法解密（服务端密钥已变更）",
    "unsupported_blob_version": "记录无法解密（服务端密钥已变更）",
}


def _is_admin(user: User) -> bool:
    return str(user.role or "").lower() == "admin"


def _err(exc: ValueError) -> HTTPException:
    code = str(exc)
    if code == "duplicate_entry":
        return HTTPException(status_code=409, detail=_VALUE_ERROR_MESSAGES[code])
    return HTTPException(status_code=422, detail=_VALUE_ERROR_MESSAGES.get(code, "参数无效"))


@router.get("")
async def list_totp_credentials(user: User = Depends(get_current_user)) -> dict:
    """列出验证器 meta + **当前验证码**（不含密钥）。管理员看全部用户（带归属列）。"""
    from db import PostgresClient
    import totp_store

    if not PostgresClient.pool:
        raise HTTPException(status_code=503, detail="服务暂不可用")

    if _is_admin(user):
        async with PostgresClient.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, owner_user_id FROM totp_credentials ORDER BY updated_at DESC"
            )
            owner_ids = {str(r["owner_user_id"]) for r in rows}
            label_by_owner = {}
            for oid in owner_ids:
                label_by_owner[oid] = await _owner_label(conn, oid)
        # 逐条取码（复用 list + get 的口径：meta + code）；管理员分支按 owner 逐个取。
        entries = []
        for oid in owner_ids:
            for full in await totp_store.list_with_codes(oid):
                full["ownerLabel"] = label_by_owner.get(oid, oid)
                entries.append(full)
        entries.sort(key=lambda e: e.get("updatedAt") or "", reverse=True)
        return {"ok": True, "entries": entries, "admin": True}

    # 一次取表、同一时刻算码（list_with_codes）——替代 list + 逐条 get 的 N+1。
    return {"ok": True, "entries": await totp_store.list_with_codes(str(user.id)), "admin": False}


async def _owner_label(conn, owner_user_id: str) -> str:
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


@router.post("/create")
async def create_totp_credential(
    body: TotpCreateReq,
    user: User = Depends(get_current_user),
) -> dict:
    """新增一条验证器。``secret`` 可以是裸 base32，也可以是整条 ``otpauth://`` URI。"""
    import totp_store
    from totp_code import parse_otpauth

    issuer, account, domains = body.issuer, body.account, body.domains
    label, note, url = body.label, body.note, body.url
    digits, period, algorithm = body.digits, body.period, body.algorithm
    secret = body.secret

    text = str(secret or "").strip()
    if text.lower().startswith("otpauth://"):
        try:
            parsed = parse_otpauth(text)
        except ValueError as exc:
            raise _err(exc)
        # URI 是真相源：字段以解析结果为准，请求里显式给的展示字段（note/url/额外域名）叠加。
        issuer = issuer or parsed["issuer"]
        account = account or parsed["account"]
        label = label or parsed["label"]
        digits = parsed["digits"]
        period = parsed["period"]
        algorithm = parsed["algorithm"]
        secret = parsed["secret"]
        if not domains:
            domains = [issuer.lower()] if issuer else []

    try:
        meta = await totp_store.upsert_credential(
            str(user.id),
            secret=secret,
            label=label, issuer=issuer, account=account,
            domains=domains, note=note, url=url,
            digits=digits, period=period, algorithm=algorithm,
            source="user",
        )
    except ValueError as exc:
        raise _err(exc)
    full = await totp_store.get_credential(str(user.id), meta["id"])
    return {"ok": True, "entry": full or meta}


@router.post("/update")
async def update_totp_credential(
    body: TotpUpdateReq,
    user: User = Depends(get_current_user),
) -> dict:
    """编辑展示字段（站点/账号/关联域名/备注/URL/显示名）。**不能改密钥**。"""
    import totp_store

    try:
        meta = await totp_store.update_credential(
            str(user.id), int(body.entry_id),
            label=body.label, issuer=body.issuer, account=body.account,
            domains=body.domains, note=body.note, url=body.url,
            allow_any_owner=_is_admin(user),
        )
    except ValueError as exc:
        raise _err(exc)
    if meta is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    full = await totp_store.get_credential(str(user.id), meta["id"])
    return {"ok": True, "entry": full or meta}


@router.post("/code")
async def refresh_totp_code(
    body: TotpCodeReq,
    user: User = Depends(get_current_user),
) -> dict:
    """刷新单条当前码（网页倒计时归零时拉新码，不必整表重载）。"""
    from db import PostgresClient
    import totp_store

    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT owner_user_id FROM totp_credentials WHERE id=$1", int(body.entry_id)
        )
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    owner = str(row["owner_user_id"])
    if not _is_admin(user) and owner != str(user.id):
        raise HTTPException(status_code=404, detail="记录不存在")
    full = await totp_store.get_credential(owner, int(body.entry_id))
    if not full:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True, "entry": full}


@router.post("/delete")
async def delete_totp_credential(
    body: TotpDeleteReq,
    user: User = Depends(get_current_user),
) -> dict:
    """删除一条验证器。管理员可删任意用户的，普通用户只能删自己的。"""
    from db import PostgresClient
    import totp_store

    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT owner_user_id FROM totp_credentials WHERE id=$1", int(body.entry_id)
        )
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    owner = str(row["owner_user_id"])
    if not _is_admin(user) and owner != str(user.id):
        raise HTTPException(status_code=404, detail="记录不存在")
    if not await totp_store.delete_credential(owner, int(body.entry_id)):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True, "deleted": True}


# 导入的文本硬上限：防超大文件把逐行解析拖死（每条 URI 也就一两百字节，够装几千条）。
_IMPORT_MAX_CHARS = 1_000_000
# 导入结果里最多回传多少条行级错误（其余折叠成计数，不刷屏）。
_IMPORT_MAX_ERRORS = 50


def _import_error_message(code: str) -> str:
    return _VALUE_ERROR_MESSAGES.get(str(code), "该行无法解析")


@router.post("/import")
async def import_totp_credentials(
    body: TotpImportReq,
    user: User = Depends(get_current_user),
) -> dict:
    """批量导入验证器：文本每行一条 ``otpauth://`` URI（Aegis/andOTP/Bitwarden 的导出格式）。

    逐行解析、逐行 upsert（同 (issuer, account) 覆盖——重复导入幂等）。空行与以 ``#``
    开头的注释行跳过。非 ``otpauth://`` 开头的行会被**警告但不阻断**（有人会把整段
    JSON 误粘进来，逐行跳过比整批失败友好）。回明细计数 + 前若干条行级错误。

    归属恒为当前用户，与逐条新增同口径。
    """
    import totp_store
    from totp_code import parse_otpauth

    text = str(body.content or "")
    if len(text) > _IMPORT_MAX_CHARS:
        raise HTTPException(status_code=413, detail="导入内容过大（上限 1MB）")

    lines = text.splitlines()
    imported = 0
    overwritten = 0
    skipped_blank = 0
    skipped_account = 0
    errors: list[dict] = []
    error_count = 0
    owner = str(user.id)

    for idx, line in enumerate(lines, start=1):
        raw = str(line or "").strip()
        if not raw or raw.startswith("#"):
            skipped_blank += 1
            continue
        if not raw.lower().startswith("otpauth://"):
            error_count += 1
            if len(errors) < _IMPORT_MAX_ERRORS:
                errors.append({"line": idx, "reason": "不是 otpauth:// 链接（可能误粘了整段 JSON）"})
            continue
        try:
            parsed = parse_otpauth(raw)
        except ValueError as exc:
            error_count += 1
            if len(errors) < _IMPORT_MAX_ERRORS:
                errors.append({"line": idx, "reason": _import_error_message(str(exc))})
            continue

        existed = await totp_store.credential_exists(owner, parsed["issuer"], parsed["account"])
        domains = body.domains or ([parsed["issuer"].lower()] if parsed["issuer"] else [])
        try:
            await totp_store.upsert_credential(
                owner,
                secret=parsed["secret"],
                label=parsed["label"], issuer=parsed["issuer"], account=parsed["account"],
                domains=domains, note="", url="",
                digits=parsed["digits"], period=parsed["period"], algorithm=parsed["algorithm"],
                source="user",
            )
        except ValueError as exc:
            code = str(exc)
            # 缺账号（该行 otpauth 本身没带）→ 归入「跳过」，不是格式错误。文案区分。
            reason = _import_error_message(code)
            skipped = code == "account_required"
            if skipped:
                skipped_account += 1
                reason = "该行没有账号（otpauth 缺 account），已跳过"
            # 配额超限：一次导入里同码只记一次，避免几十行重复错误盖住真正问题。
            if code != "totp_quota_exceeded" or not any(e.get("quota") for e in errors):
                if len(errors) < _IMPORT_MAX_ERRORS:
                    errors.append({"line": idx, "reason": reason, "quota": code == "totp_quota_exceeded", "skipped": skipped})
            if not skipped:
                error_count += 1
            continue
        if existed:
            overwritten += 1
        else:
            imported += 1

    await _audit_import_export(user, kind="import", imported=imported, overwritten=overwritten, errors=error_count)
    return {
        "ok": True,
        "imported": imported,
        "overwritten": overwritten,
        "skipped_blank": skipped_blank,
        "errors": errors,
        "error_count": error_count,
        # 缺账号的行是数据本身没带账号，不是出错——与 error_count 分开回，前端才好区分。
        "skipped_account": skipped_account,
    }


@router.post("/export")
async def export_totp_credentials(
    body: TotpExportReq,
    user: User = Depends(get_current_user),
) -> dict:
    """导出全部验证器为纯文本（每行一条 ``otpauth://``），可被 Aegis/本工具重新导入。

    ⚠ 这是明文密钥的**唯一出口**：要求二次鉴权（当前账号密码），且**管理员无旁路**——
    永远只导 ``str(user.id)`` 自己的数据（导别人的验证器密钥等于废掉对方 2FA）。
    每次导出写审计（``severity="warning"``，detail 只记数量不含内容）。
    """
    import totp_store
    from totp_code import build_otpauth_uri

    if not verify_password(body.password, user.password or ""):
        raise HTTPException(status_code=401, detail="账号密码不正确，无法导出")

    rows = await totp_store.export_credentials(str(user.id))
    lines = []
    for r in rows:
        lines.append(build_otpauth_uri(
            issuer=r["issuer"], account=r["account"], secret=r["secret"],
            digits=r["digits"], period=r["period"], algorithm=r["algorithm"],
        ))
    content = "\n".join(lines) + ("\n" if lines else "")
    await _audit_import_export(user, kind="export", count=len(lines))
    return {
        "ok": True,
        "count": len(lines),
        "content": content,
        "filename": f"ai-lubricant-totp-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt",
    }


async def _audit_import_export(user: User, *, kind: str, **counts) -> None:
    """写导入/导出审计。detail 只记动作类型与计数，绝无密钥/条目内容。"""
    from db import PostgresClient

    detail = {"owner_user_id": str(user.id), "action": f"totp_{kind}"}
    detail.update({k: v for k, v in counts.items() if v is not None})
    severity = "warning" if kind == "export" else "info"
    async with PostgresClient.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO security_events (event_type, severity, tag, detail) VALUES ($1,$2,$3,$4::jsonb)",
            f"totp_credentials_{kind}", severity, "totp_credential",
            json.dumps(detail, ensure_ascii=False),
        )
