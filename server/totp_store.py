"""CDP 身份验证器存储层（``totp_credentials`` 表）。

风格对齐 ``browser_password_store``：模块级函数 + 函数内延迟 import
PostgresClient。归属模型一致（owner_user_id 由已鉴权调用方解析，本层不做鉴权，
所有查询都带 owner 条件，缺失与越权都返回 ``None``/``False``——不泄漏「记录
存在但不属于你」）。

安全边界（比密码库更严）：
- ``secret_enc`` 恒为 AES-256-GCM 密文（复用 ``browser_password_crypto`` 那套
  密钥来源：``BROWSER_PASSWORD_KEY`` / 派生键 / 启动生成，多实例同值）。
- **没有任何出口回明文密钥**。``get_credential`` 只回 meta + 当前 TOTP 码 +
  剩余秒数；不存在「解密导出密钥」这种接口——扩展 / 网页 / agent 都拿不到。
- 审计只记 id 与 label，绝不记 secret / code。

关联网站存 ``domains``（TEXT[]，一个验证器可在多个域名用，如 GitHub 网页+API），
账号维度 ``account`` 与 issuer 组唯一键 ``UNIQUE(owner_user_id, issuer, account)``
——同站点同账号重复录入 = 覆盖（与密码库 upsert 同一口径，扫码重录不产生重复行）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from browser_password_crypto import decrypt_password, encrypt_password
from totp_code import (
    DEFAULT_ALGORITHM,
    DEFAULT_DIGITS,
    DEFAULT_PERIOD,
    expires_at,
    normalize_secret,
    totp_code,
)

MAX_LABEL_LEN = 255
MAX_ISSUER_LEN = 255
MAX_ACCOUNT_LEN = 255
MAX_DOMAIN_LEN = 255
MAX_DOMAINS = 20
MAX_NOTE_LEN = 1024
MAX_URL_LEN = 2048
MAX_ENTRIES_PER_USER = 300
LIST_LIMIT = 1000

# 审计事件类型（只记 id 与 label，绝不记 secret/code）。
EVENT_SAVED = "totp_credential_saved"
EVENT_DELETED = "totp_credential_deleted"
AUDIT_TAG = "totp_credential"

_META_COLUMNS = (
    "id, label, issuer, account, domains, note, digits, period, algorithm, "
    "source, url, created_at, updated_at"
)


def _pool():
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("postgres pool not initialized")
    return PostgresClient.pool


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _norm_domains(value: Any) -> list[str]:
    """接受 list / 逗号串 / 单值 → 去重、小写、截断的域名列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    out: list[str] = []
    for item in items:
        dom = str(item or "").strip().lower()
        if dom:
            dom = dom[:MAX_DOMAIN_LEN]
            if dom not in out:
                out.append(dom)
    return out[:MAX_DOMAINS]


# 域名/主机名清洗：去端口、去路径、去 scheme（导入文件里可能整条 URI 误当域名）。
_HOST_CLEAN_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?([^/?#]*)")


def _clean_host(value: str) -> str:
    """任意形态的「站点」→ 裸主机名（小写、去端口/路径/scheme/点前缀）。"""
    text = _HOST_CLEAN_RE.match(str(value or "").strip().lower())
    host = (text.group(1) if text else "").strip()
    host = host.split(":", 1)[0].split("/", 1)[0].split("@")[-1]
    return host.lstrip(".").strip()


def host_matches_domains(host: str, domains: list[str]) -> bool:
    """主机名与关联网站的**智能匹配**（本站页签 + agent 取码共用一条口径）。

    ``domains`` 里既有真实域名（``github.com``），也有导入/扫码时以 issuer 兜底的
    **站点名**（``google`` / ``dgn cloud``——含空格，显然不是域名）。精确等值对
    ``accounts.google.com`` 永远 False，等于把数据藏起来。规则：
      - 主机名或其父域与条目**精确相等**（``www.github.com`` 匹配 ``github.com``）；
      - 条目是「合法域形状」（无空格、含点）且为主机名的**后缀**（``gitlab.com``
        匹配 ``my.gitlab.com``，但 ``bad.example.com`` 不匹配 ``example.com``）；
      - 条目是**单 token 站点名**（无空格无点）时按 token 匹配主机名的首段或任一
        段（``accounts.google.com`` 匹配 ``google``）；
      - 条目含空格（``dgn cloud``）：任一 token 命中主机名段即算（宽松兜底，
        agent 取码宁可多回一条让用户选，也不能取不到）。
    """
    clean = _clean_host(host)
    if not clean:
        return False
    host_parts = clean.split(".")
    for raw in domains or []:
        d = _clean_host(str(raw))
        if not d:
            continue
        if d == clean or clean.endswith("." + d):
            return True
        has_space = " " in str(raw).strip()
        looks_like_domain = "." in d and " " not in d
        if looks_like_domain and clean.endswith(d) and clean.rsplit(d, 1)[0].endswith("."):
            return True
        tokens = d.split() if has_space else ([d] if "." not in d else [])
        for tok in tokens:
            if tok and tok in host_parts:
                return True
    return False


def _row_to_meta(row: Any) -> dict:
    """行 → 列表/回执 meta。**恒不含** secret_enc（明文密钥无任何出口）。"""
    domains = row.get("domains")
    if isinstance(domains, str):
        domains = _norm_domains(domains)
    elif domains is None:
        domains = []
    else:
        domains = [str(d) for d in domains]
    return {
        "id": row["id"],
        "label": row.get("label") or "",
        "issuer": row.get("issuer") or "",
        "account": row.get("account") or "",
        "domains": domains,
        "note": row.get("note") or "",
        "url": row.get("url") or "",
        "digits": int(row.get("digits") or DEFAULT_DIGITS),
        "period": int(row.get("period") or DEFAULT_PERIOD),
        "algorithm": row.get("algorithm") or DEFAULT_ALGORITHM,
        "source": row.get("source") or "user",
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


async def _audit(conn, *, event_type: str, owner_user_id: str, entry_id: int, label: str) -> None:
    """写 security_events。detail 只含归属与定位信息，无 secret、绝无 code。"""
    await conn.execute(
        "INSERT INTO security_events (event_type, severity, tag, detail) VALUES ($1,$2,$3,$4::jsonb)",
        event_type,
        "info",
        AUDIT_TAG,
        json.dumps(
            {"owner_user_id": str(owner_user_id), "entry_id": int(entry_id), "label": label},
            ensure_ascii=False,
        ),
    )


async def count_credentials(owner_user_id: str) -> int:
    pool = _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM totp_credentials WHERE owner_user_id=$1",
            str(owner_user_id),
        ) or 0)


async def list_credentials(owner_user_id: str, domain: str | None = None) -> list[dict]:
    """列 meta。``domain`` 非空时只回关联了该域名的条目（弹窗「本站」页签用）。

    域名匹配不是简单等值：``domains`` 里既可能存真实域名（``github.com``），也可能
    是导入/扫码时由 issuer 兜底的**站点名**（``google`` / ``dgn cloud``）——精确
    ``= ANY()`` 会永远匹配不上 ``accounts.google.com`` 这种真实主机名。故按 owner
    取全量（配额上限 300 行）后在 Python 侧用 :func:`host_matches_domains` 过滤。
    """
    normalized = _clean_host(domain)
    pool = _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_META_COLUMNS} FROM totp_credentials "
            f"WHERE owner_user_id=$1 ORDER BY updated_at DESC LIMIT {LIST_LIMIT}",
            str(owner_user_id),
        )
    metas = [_row_to_meta(dict(r)) for r in rows]
    if not normalized:
        return metas
    return [m for m in metas if host_matches_domains(normalized, m.get("domains") or [])]


async def list_with_codes(owner_user_id: str, domain: str | None = None) -> list[dict]:
    """列 meta + **当前码 + 绝对失效时刻**（一次取表、同一时刻算码）。

    取代旧 ``list_credentials`` + 逐条 ``get_credential`` 的 N+1。这里回完整 meta
    （站点/账号/备注/关联网站）+ ``code`` + ``expiresAt``（**epoch 秒**，本步的
    绝对翻转时刻，非剩余秒数——见 :func:`totp_code.expires_at`）。

    ``domain`` 非空时按 :func:`host_matches_domains` 过滤（「本站」页签）。
    回帧永不含密钥。
    """
    normalized = _clean_host(domain)
    pool = _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_META_COLUMNS}, secret_enc FROM totp_credentials "
            "WHERE owner_user_id=$1 "
            f"ORDER BY updated_at DESC LIMIT {LIST_LIMIT}",
            str(owner_user_id),
        )
    moment = time.time()  # 一次取好：全表多码共享同一 now，跨期不错位
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        meta = _row_to_meta(row)
        if normalized and not host_matches_domains(normalized, meta.get("domains") or []):
            continue
        period = int(row.get("period") or DEFAULT_PERIOD)
        secret = decrypt_password(row["secret_enc"])
        code = totp_code(
            secret, at=moment,
            digits=int(row.get("digits") or DEFAULT_DIGITS),
            period=period,
            algorithm=row.get("algorithm") or DEFAULT_ALGORITHM,
        )
        out.append({**meta, "code": code, "expiresAt": expires_at(at=moment, period=period)})
    return out


async def get_credential(owner_user_id: str, entry_id: int) -> dict | None:
    """取单条 meta + **当前验证码 + 绝对失效时刻**（不回密钥）。缺失/越权返回 None。"""
    pool = _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_META_COLUMNS}, secret_enc FROM totp_credentials "
            "WHERE id=$1 AND owner_user_id=$2",
            int(entry_id), str(owner_user_id),
        )
    if not row:
        return None
    row = dict(row)
    meta = _row_to_meta(row)
    code, expires = _current_code(row)
    return {**meta, "code": code, "expiresAt": expires}


def _current_code(row: dict, at: float | None = None) -> tuple[str, int]:
    """解密（仅内存瞬时）→ 算当前码 + 绝对失效时刻（epoch 秒）。密钥不外传。

    ⚠ ``at`` 必须一次取好传给两边：码与失效时刻若各取一次 ``time.time()``，跨期瞬间
    会出现「码是上一期的、时刻已是新一期」的错位——正是读秒看着不准的直接原因。
    """
    moment = at if at is not None else time.time()
    period = int(row.get("period") or DEFAULT_PERIOD)
    secret = decrypt_password(row["secret_enc"])
    code = totp_code(
        secret,
        at=moment,
        digits=int(row.get("digits") or DEFAULT_DIGITS),
        period=period,
        algorithm=row.get("algorithm") or DEFAULT_ALGORITHM,
    )
    return code, expires_at(at=moment, period=period)


async def find_by_domain(owner_user_id: str, domain: str) -> list[dict]:
    """按站点取全部验证码（agent 自动填 2FA 的入口）。回 meta + code。

    域名归一小写；空域名直接回空列表（不猜、不兜底取第一条）。匹配口径与
    ``list_credentials`` 一致（见 :func:`host_matches_domains`）——按 owner 取全量
    再 Python 侧过滤，否则站点名兜底的条目永远匹配不上真实主机名。
    """
    normalized = _clean_host(domain)
    if not normalized:
        return []
    pool = _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_META_COLUMNS}, secret_enc FROM totp_credentials "
            "WHERE owner_user_id=$1 "
            f"ORDER BY updated_at DESC LIMIT {LIST_LIMIT}",
            str(owner_user_id),
        )
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        meta = _row_to_meta(row)
        if not host_matches_domains(normalized, meta.get("domains") or []):
            continue
        code, expires = _current_code(row)
        out.append({**meta, "code": code, "expiresAt": expires})
    return out


async def upsert_credential(
    owner_user_id: str, *,
    secret: str,
    label: str = "",
    issuer: str = "",
    account: str = "",
    domains: Any = None,
    note: str = "",
    url: str = "",
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    algorithm: str = DEFAULT_ALGORITHM,
    source: str = "user",
) -> dict:
    """按 (owner, issuer, account) upsert，覆盖密钥与展示字段。返回 meta（不含密钥）。

    校验失败抛 ``ValueError("<code>")``，由帧层/REST 层翻中文：
      ``secret_required`` / ``secret_invalid_base32`` / ``account_required`` /
      ``domains_required`` / ``totp_quota_exceeded`` / 各类 *_too_long /
      ``digits_unsupported`` / ``period_unsupported`` / ``algorithm_unsupported``
    """
    normalized_secret = normalize_secret(secret)
    issuer = str(issuer or "").strip()[:MAX_ISSUER_LEN]
    account = str(account or "").strip()[:MAX_ACCOUNT_LEN]
    label = str(label or "").strip()[:MAX_LABEL_LEN] or (
        f"{issuer}:{account}" if issuer and account else (issuer or account)
    )
    domain_list = _norm_domains(domains)
    note = str(note or "").strip()[:MAX_NOTE_LEN]
    url = str(url or "").strip()[:MAX_URL_LEN]

    digits = int(digits or DEFAULT_DIGITS)
    if not (6 <= digits <= 8):
        raise ValueError("digits_unsupported")
    period = int(period or DEFAULT_PERIOD)
    if not (15 <= period <= 300):
        raise ValueError("period_unsupported")
    algorithm = str(algorithm or DEFAULT_ALGORITHM).upper()
    if algorithm not in ("SHA1", "SHA256", "SHA512"):
        raise ValueError("algorithm_unsupported")

    if not account:
        # 唯一键要有落点：站点没给 account 时用域名占位（同一 issuer 多账号仍靠
        # account 区分；只扫码没账号的场景退化成「按站点一条」）。
        raise ValueError("account_required")
    if not domain_list:
        raise ValueError("domains_required")

    encrypted = encrypt_password(normalized_secret)
    pool = _pool()
    async with pool.acquire() as conn:
        if await count_credentials(owner_user_id) >= MAX_ENTRIES_PER_USER:
            exists = await conn.fetchval(
                "SELECT 1 FROM totp_credentials WHERE owner_user_id=$1 AND issuer=$2 AND account=$3",
                str(owner_user_id), issuer, account,
            )
            if not exists:
                raise ValueError("totp_quota_exceeded")
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO totp_credentials (
                    owner_user_id, label, issuer, account, domains, note, url,
                    secret_enc, digits, period, algorithm, source
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (owner_user_id, issuer, account)
                DO UPDATE SET label=EXCLUDED.label, domains=EXCLUDED.domains,
                              note=EXCLUDED.note, url=EXCLUDED.url,
                              secret_enc=EXCLUDED.secret_enc, digits=EXCLUDED.digits,
                              period=EXCLUDED.period, algorithm=EXCLUDED.algorithm,
                              source=EXCLUDED.source, updated_at=now()
                RETURNING """ + _META_COLUMNS,
                str(owner_user_id), label, issuer, account, domain_list, note, url,
                encrypted, digits, period, algorithm, source,
            )
            meta = _row_to_meta(dict(row))
            await _audit(
                conn, event_type=EVENT_SAVED, owner_user_id=owner_user_id,
                entry_id=meta["id"], label=meta["label"],
            )
    return meta


async def update_credential(
    owner_user_id: str, entry_id: int, *,
    label: str | None = None,
    issuer: str | None = None,
    account: str | None = None,
    domains: Any = None,
    note: str | None = None,
    url: str | None = None,
    allow_any_owner: bool = False,
) -> dict | None:
    """编辑展示字段（按 id 定位，**不新增行**，也不能改密钥）。

    密钥不可改是刻意的：换 2FA 密钥 = 站点重新发二维码，走 ``upsert_credential``
    而不是这里，避免「改个备注顺手动了密钥」这种误伤。id 不存在/不属于该 owner
    返回 ``None``；改 (issuer, account) 撞唯一键抛 ``ValueError("duplicate_entry")``。
    ``allow_any_owner=True``（管理员）时不加 owner 过滤。
    """
    pool = _pool()
    sets: list[str] = []
    args: list[Any] = []

    def _add(col: str, value: Any) -> None:
        args.append(value)
        sets.append(f"{col}=${len(args)}")

    if label is not None:
        _add("label", str(label).strip()[:MAX_LABEL_LEN])
    if issuer is not None:
        _add("issuer", str(issuer).strip()[:MAX_ISSUER_LEN])
    if account is not None:
        account = str(account).strip()[:MAX_ACCOUNT_LEN]
        if not account:
            raise ValueError("account_required")
        _add("account", account)
    if domains is not None:
        domain_list = _norm_domains(domains)
        if not domain_list:
            raise ValueError("domains_required")
        _add("domains", domain_list)
    if note is not None:
        _add("note", str(note).strip()[:MAX_NOTE_LEN])
    if url is not None:
        _add("url", str(url).strip()[:MAX_URL_LEN])
    if not sets:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_META_COLUMNS} FROM totp_credentials WHERE id=$1"
                + ("" if allow_any_owner else " AND owner_user_id=$2"),
                *([int(entry_id)] if allow_any_owner else [int(entry_id), str(owner_user_id)]),
            )
        return _row_to_meta(dict(row)) if row else None

    _add("source", "user")
    sets.append("updated_at=now()")
    if allow_any_owner:
        args.append(int(entry_id))
        where = f"WHERE id=${len(args)}"
    else:
        args.extend([int(entry_id), str(owner_user_id)])
        where = f"WHERE id=${len(args) - 1} AND owner_user_id=${len(args)}"

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"UPDATE totp_credentials SET {', '.join(sets)} {where} "
                    "RETURNING id, owner_user_id, " + _META_COLUMNS,
                    *args,
                )
                if not row:
                    return None
                meta = _row_to_meta(dict(row))
                await _audit(
                    conn, event_type=EVENT_SAVED,
                    owner_user_id=str(row["owner_user_id"]),
                    entry_id=meta["id"], label=meta["label"],
                )
        except Exception as exc:  # 唯一键冲突 → 明确告知，不吞成 500
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ValueError("duplicate_entry") from exc
            raise
    return meta


async def delete_credential(owner_user_id: str, entry_id: int) -> bool:
    """按 id + owner 删除。删到返回 True，否则 False（缺失或越权）。"""
    pool = _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM totp_credentials WHERE id=$1 AND owner_user_id=$2 "
                "RETURNING id, label",
                int(entry_id), str(owner_user_id),
            )
            if not row:
                return False
            await _audit(
                conn, event_type=EVENT_DELETED, owner_user_id=owner_user_id,
                entry_id=int(row["id"]), label=row["label"],
            )
    return True


async def credential_exists(owner_user_id: str, issuer: str, account: str) -> bool:
    """(issuer, account) 唯一键是否已存在——导入判重用（决定 imported vs overwritten）。"""
    pool = _pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT 1 FROM totp_credentials WHERE owner_user_id=$1 AND issuer=$2 AND account=$3",
            str(owner_user_id), str(issuer or ""), str(account or ""),
        ))


async def audit_bulk(owner_user_id: str, *, action: str, detail_extra: dict | None = None) -> None:
    """批量导入/导出的审计（扩展 WS 帧路径用；网页端有同款 ``_audit_import_export``）。

    detail 只记动作与计数，绝无密钥/条目内容。``action``：``import`` / ``export``；
    导出恒为 ``severity="warning"``（明文外泄动作）。
    """
    pool = _pool()
    detail = {"owner_user_id": str(owner_user_id), "action": f"totp_{action}"}
    detail.update(detail_extra or {})
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO security_events (event_type, severity, tag, detail) VALUES ($1,$2,$3,$4::jsonb)",
            f"totp_credentials_{action}",
            "warning" if action == "export" else "info",
            AUDIT_TAG,
            json.dumps(detail, ensure_ascii=False),
        )


async def export_credentials(owner_user_id: str) -> list[dict]:
    """导出备份：返回含**明文 secret** 的完整条目（按 issuer/account 排序，稳定 diff）。

    ⚠ 这是全系统**唯一**回明文密钥的出口，仅供已二次鉴权的导出路径调用
    （网页 ``/export`` 与扩展 ``totp_export`` 帧，两者都先校验账号密码）。绝不接
    WS 的 list/get/agent 工具；调用方不得把返回内容写日志。返回字段：meta +
    ``secret``（规范 base32）。
    """
    pool = _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_META_COLUMNS}, secret_enc FROM totp_credentials "
            "WHERE owner_user_id=$1 ORDER BY issuer, account, id",
            str(owner_user_id),
        )
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        out.append({**_row_to_meta(row), "secret": decrypt_password(row["secret_enc"])})
    return out


async def verify_owner_password(owner_user_id: str, password: str) -> bool:
    """校验 owner 的账号密码——导出/备份的二次鉴权（网页与扩展帧共用一条口径）。

    兼容层关闭（``mc_users`` 表不存在）、用户不存在、用户无密码（纯第三方登录）、
    或密码不匹配，一律 ``False``。bcrypt 校验复用 ``user_platform.crypto``，与网页端
    ``verify_password`` 同源，避免两处哈希实现漂移。
    """
    if not password:
        return False
    from user_platform.crypto import verify_password

    pool = _pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.mc_users')")
        if not exists:
            return False
        row = await conn.fetchrow(
            "SELECT password FROM mc_users WHERE id=$1::uuid", str(owner_user_id)
        )
    if not row:
        return False
    return bool(verify_password(password, row["password"] or ""))
