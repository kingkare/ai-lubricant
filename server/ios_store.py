"""iOS 签名配置存储（Stage 3）。

签名材料（App Store Connect p8 / p12+mobileprovision）服务端托管，job 启动时
经 TLS NodeConnect 一次性下发到节点；secret_data 永不回显（GET 仅返 id/name/kind）。

WDA 产物不再单独入库——prepare 时从市场 device-control iOS 发行版解析 GitHub
Release 直链，宿主节点经自身出口代理下载后重签安装（见 routes_ios.prepare_wda）。
``ios_wda_artifacts`` 表保留建表语句以防已部署库报错，但已无 API/store 引用。
"""
from __future__ import annotations

import base64
import json
import plistlib
import re
from datetime import datetime, timezone
from typing import Any

from db import PostgresClient

# mobileprovision 是一个 CMS/PKCS#7 签名的 plist：plist XML 原文嵌在二进制里，
# 正则截出来用 plistlib（标准库）解析即可，无需新增依赖。
_MOBILEPROVISION_PLIST_RE = re.compile(rb"<\?xml[\s\S]*?</plist>")


def _encode_secret(secret_data: dict[str, Any] | None) -> str | None:
    """把 secret_data 编码成 jsonb 参数。

    ``secret_data`` 是 JSONB 列，而本进程的 asyncpg 池**没有**注册 jsonb
    codec：asyncpg 只接受 str，直接传 dict 会在 bind 阶段抛
    ``TypeError: expected str, got dict``（Apple ID 登录 500 的根因）。写入统一
    走这里，与 builtin_tool_store 的 ``json.dumps(..., ensure_ascii=False)`` 同口径。
    """
    if secret_data is None:
        return None
    return json.dumps(secret_data, ensure_ascii=False)


def _decode_secret(value: Any) -> dict[str, Any]:
    """把读回的 jsonb 值还原成 dict。

    同一个 codec 缺失意味着 asyncpg 把 jsonb 读成 **字符串**；而所有消费方
    （routes_ios 的富化/物化、自动续签）都按 dict 取键（``secret.get("team_id")``），
    不还原就会 AttributeError。None / 已是 dict 时按原样/空 dict 处理。
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _row_with_secret(row: Any) -> dict | None:
    """dict(row) + 把 secret_data 列还原成 dict（存在该列时才动）。"""
    if row is None:
        return None
    out = dict(row)
    if "secret_data" in out:
        out["secret_data"] = _decode_secret(out["secret_data"])
    return out


def parse_mobileprovision_metadata(base64_blob: str) -> dict[str, str] | None:
    """解析 .mobileprovision 内嵌 plist 的展示元数据（不碰签名、不解密）。

    返回 {"expires_at": ISO, "team_id": str, "profile_name": str}；内容不是有效
    描述文件时返回 None。routes_ios 用它：创建时拒收脏数据 + 列表时算 status；
    p12 材料的实际约束就是描述文件的 ExpirationDate（恒 ≤ 证书 notAfter）。
    """
    try:
        blob = base64.b64decode(base64_blob or "")
    except Exception:  # noqa: BLE001 — 任意输入都可能进来，非法即非法
        return None
    match = _MOBILEPROVISION_PLIST_RE.search(blob)
    if not match:
        return None
    try:
        plist = plistlib.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return None
    expires_at = plist.get("ExpirationDate")
    if not isinstance(expires_at, datetime):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    team = plist.get("TeamIdentifier") or [""]
    team_id = ""
    if isinstance(team, (list, tuple)) and team:
        team_id = str(team[0])
    return {
        "expires_at": expires_at.isoformat(),
        "team_id": team_id,
        "profile_name": str(plist.get("Name") or ""),
    }


async def create_signing_profile(
    owner_user_id: str, name: str, kind: str, secret_data: dict[str, Any]
) -> dict:
    """Create an iOS signing profile (asc / p12 / apple_id).

    kind='asc': secret_data must contain {p8_key, key_id, issuer_id, team_id}
    kind='p12': secret_data must contain {p12_base64, p12_password, mobileprovision_base64}
    kind='apple_id': secret_data 只存 session token / cert / profile 缓存
    （密码绝不落库）——由 routes_ios 的 Apple ID 登录端点构造，不经通用创建路由。
    """
    if kind not in ("asc", "p12", "apple_id"):
        raise ValueError(f"Invalid signing profile kind: {kind}")
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ios_signing_profiles (owner_user_id, name, kind, secret_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id, owner_user_id, name, kind, created_at
            """,
            owner_user_id,
            name,
            kind,
            _encode_secret(secret_data),
        )
        return dict(row)


async def get_signing_profile(profile_id: int, owner_user_id: str | None = None) -> dict | None:
    """Retrieve one signing profile metadata (without secret_data).

    If owner_user_id is given, enforce ownership check.
    """
    async with PostgresClient.pool.acquire() as conn:
        if owner_user_id is not None:
            row = await conn.fetchrow(
                """
                SELECT id, owner_user_id, name, kind, created_at
                FROM ios_signing_profiles
                WHERE id = $1 AND owner_user_id = $2
                """,
                profile_id,
                owner_user_id,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT id, owner_user_id, name, kind, created_at
                FROM ios_signing_profiles
                WHERE id = $1
                """,
                profile_id,
            )
        return dict(row) if row else None


async def get_signing_profile_secret(profile_id: int, owner_user_id: str) -> dict | None:
    """Retrieve the full signing profile including secret_data (job dispatch only)."""
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, owner_user_id, name, kind, secret_data, created_at
            FROM ios_signing_profiles
            WHERE id = $1 AND owner_user_id = $2
            """,
            profile_id,
            owner_user_id,
        )
        return _row_with_secret(row)


async def list_signing_profiles(owner_user_id: str, *, include_secret: bool = False) -> list[dict]:
    """List all signing profiles owned by the user.

    include_secret=True 时多查 secret_data——仅供路由富化（算 status/expiry）用，
    路由必须剥掉 secret 再返回给客户端。
    """
    cols = (
        "id, owner_user_id, name, kind, created_at, secret_data"
        if include_secret
        else "id, owner_user_id, name, kind, created_at"
    )
    async with PostgresClient.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {cols}
            FROM ios_signing_profiles
            WHERE owner_user_id = $1
            ORDER BY created_at DESC
            """,
            owner_user_id,
        )
        return [_row_with_secret(r) for r in rows]


async def update_signing_profile(
    profile_id: int, owner_user_id: str, name: str, secret_data: dict[str, Any] | None
) -> dict | None:
    """更新签名配置：改名 / 整包替换材料（owner 校验）。

    secret_data=None = 只改名、保留现有材料。RETURNING 带 secret_data 供路由富化
    响应（路由会剥掉）。p12 材料过期后**原地换**（不删重建），避免 id 变化打断
    设备绑定的 signing_profile_id 链（自动续签据此取配置）。
    """
    async with PostgresClient.pool.acquire() as conn:
        if secret_data is None:
            row = await conn.fetchrow(
                """
                UPDATE ios_signing_profiles
                SET name = $3
                WHERE id = $1 AND owner_user_id = $2
                RETURNING id, owner_user_id, name, kind, secret_data, created_at
                """,
                profile_id,
                owner_user_id,
                name,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE ios_signing_profiles
                SET name = $3, secret_data = $4
                WHERE id = $1 AND owner_user_id = $2
                RETURNING id, owner_user_id, name, kind, secret_data, created_at
                """,
                profile_id,
                owner_user_id,
                name,
                _encode_secret(secret_data),
            )
        return _row_with_secret(row)


async def update_signing_profile_secret(profile_id: int, secret_data: dict[str, Any]) -> bool:
    """仅替换 secret_data（不动 name/created_at）。

    apple_id 物化链专用：材料刷新（cert/profile 缓存）与 session_expired 标记
    回写。调用方（routes_ios 物化层）已按 owner 取过该行，这里不再重复校验
    owner——保持函数最小，避免把 owner 穿进派发材料 dict。
    """
    async with PostgresClient.pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE ios_signing_profiles SET secret_data = $2 WHERE id = $1",
            profile_id,
            _encode_secret(secret_data),
        )
        return result.split()[-1] != "0"


async def delete_signing_profile(profile_id: int, owner_user_id: str) -> bool:
    """Delete a signing profile (owner-check enforced)."""
    async with PostgresClient.pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM ios_signing_profiles WHERE id = $1 AND owner_user_id = $2",
            profile_id,
            owner_user_id,
        )
        return result.split()[-1] != "0"
