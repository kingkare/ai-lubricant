"""``ExternalChannel`` → 建渠道 payload 的纯转换。

产出直接喂给 ``server/admin.py`` 的建渠道链路（``_custom_provider_config_from_payload``
能吃下的形状）。本模块**不做 I/O**，全部是纯函数，便于单测。

两条硬约束（踩过坑，见 docs/providers/external-channel-import.md）：
1. 账号凭据写 ``password`` 而不是 ``api_key``。``CustomProvider`` 取值优先级是
   ``api_key > key > password``，而 ``_demask_custom_account_credentials`` 正是为
   保住 ``password`` 这条活凭据存在的；写 ``api_key`` 会复刻「改了密钥重启不生效」
   那个陈旧遮蔽 bug。
2. ``model_rows`` 必须是 dict。``db._provider_model_payload`` 对裸字符串按序列处理，
   ``"gpt-4o"`` 会被拆成 ``{"upstream_model_id": "g", "model_id": "p"}``。
"""
from __future__ import annotations

import re
from typing import Any

from .base import ExternalChannel

# 与 admin._scrub_relay_secret 同源的启发式：序列化 source_meta 时再剥一层，
# 保证即使某个适配器手滑把密钥塞进 meta，也不会随预览响应漏出去。
_SECRET_KEY_RE = re.compile(r"password|token|api[_-]?key|secret|cookie|authorization", re.I)
_EXACT_SECRET_KEYS = frozenset({"key", "cookies", "_cookies", "access_token", "user_id", "new-api-user"})

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
# 与 admin._validate_provider_name 同口径：内部 name 只能字母数字下划线中划线，2-50 位。
_NAME_OK_RE = re.compile(r"^[a-zA-Z0-9_-]{2,50}$")


def scrub_meta(value: Any) -> Any:
    """递归剥掉疑似密钥的键，供 source_meta 回显。"""
    if isinstance(value, dict):
        return {
            str(k): scrub_meta(v)
            for k, v in value.items()
            if str(k).lower() not in _EXACT_SECRET_KEYS and not _SECRET_KEY_RE.search(str(k))
        }
    if isinstance(value, list):
        return [scrub_meta(item) for item in value]
    return value


def local_provider_name(channel: ExternalChannel, existing_names: set[str] | None = None) -> str:
    """由展示名派生一个合法的本地内部 name。

    ``_auto_generate_name`` 对纯中文名会把所有字符替成 ``-``、strip 后只剩空串，
    兜底成 ``"ch"``——导入 30 个中文渠道就会得到 ch、ch-2…ch-30。所以退化时
    改用 ``{source}-{external_id}``，可读且稳定。
    """
    text = (channel.remark or "").strip().lower()
    base = _SLUG_RE.sub("-", text).strip("-._")
    base = base[:40]
    if len(base) < 2:
        source = _SLUG_RE.sub("-", (channel.source or "ext").lower()).strip("-") or "ext"
        ext = _SLUG_RE.sub("-", str(channel.external_id or "").lower()).strip("-")
        base = f"{source}-{ext}" if ext else f"{source}-channel"
        base = base[:50]
    if len(base) < 2:
        base = "ext-channel"
    if not _NAME_OK_RE.match(base):
        base = "ext-channel"
    if existing_names:
        name = base
        counter = 2
        while name in existing_names:
            name = f"{base[:46]}-{counter}"
            counter += 1
        return name
    return base


def build_create_payload(channel: ExternalChannel, *, name: str | None = None) -> dict[str, Any]:
    """把 ``ExternalChannel`` 转成建渠道 payload。

    ``accounts`` 只在真的有密钥时才产出条目——空 key 的占位账号会进运行池并
    表现为永久认证失败，不如让渠道先零账号存在、由用户后续补填。
    """
    accounts = [
        {
            "username": acc.username,
            "password": acc.api_key,
            "price_remark": acc.remark,
            "switch": True,
        }
        for acc in channel.accounts
        if acc.api_key
    ]
    return {
        "name": name or local_provider_name(channel),
        "remark": channel.remark,
        "enabled": bool(channel.enabled),
        "base_url": channel.base_url,
        "billing_mode": "token",
        "timeout": max(1, min(3600, int(channel.timeout or 120))),
        "models_path": channel.models_path,
        "website_url": channel.website_url,
        "icon": "",
        "tags": list(channel.tags),
        "chat_protocols": [dict(row) for row in channel.chat_protocols],
        "model_id_rewrite_rules": list(channel.model_id_rewrite_rules),
        "account_weight": max(1, int(channel.account_weight or 1)),
        "auto_update_models": bool(channel.auto_update_models),
        "accounts": accounts,
        "models": [dict(row) for row in channel.model_rows] or list(channel.models),
    }


def serialize_candidate(channel: ExternalChannel, *, mask_secret, conflict: dict | None = None) -> dict[str, Any]:
    """预览用的候选序列化：**绝不回显原始密钥**。

    密钥面只暴露 ``account_usernames`` / ``key_preview``（掩码后）/ ``has_keys``。
    """
    return {
        "candidate_id": channel.candidate_id,
        "source_id": str(channel.external_id),
        "remark": channel.remark,
        "base_url": channel.base_url,
        "enabled": bool(channel.enabled),
        "importable": bool(channel.importable),
        "protocol": (channel.chat_protocols[0].get("protocol") if channel.chat_protocols else ""),
        "chat_protocols": [dict(row) for row in channel.chat_protocols],
        "models": list(channel.models),
        "model_rows": [dict(row) for row in channel.model_rows],
        "model_id_rewrite_rules": list(channel.model_id_rewrite_rules),
        "tags": list(channel.tags),
        "account_usernames": [acc.username for acc in channel.accounts],
        "account_count": sum(1 for acc in channel.accounts if acc.api_key),
        "has_keys": channel.has_keys,
        "key_preview": [mask_secret(acc.api_key) for acc in channel.accounts if acc.api_key],
        "account_weight": int(channel.account_weight or 1),
        "source_meta": scrub_meta(channel.source_meta),
        "conflict": conflict or None,
        "warnings": list(channel.warnings),
        "errors": list(channel.errors),
    }
