"""Apple ID 密码服务端列加密（AES-256-GCM）——供 token 失效后自动重登。

免费 Apple ID 的 GSA session token（auth_token）有效期短，过期后必须重新
密码登录才能继续签名/续签。以前密码用完即弃，token 一死就卡死在「人工重登」；
现在登录时把密码加密落 secret_data（``password_enc``），token 失效时静默
重登（受信设备不弹 2FA，见 routes_ios._auto_relogin）。

密钥来源（优先级，照抄 browser_password_crypto 的链）::

    1. 环境变量 ``APPLE_SIGNING_PASSWORD_KEY``（strip 后非空，长度 ≥ 16）。
    2. 从 ``AGENT_ATTACHMENT_SIGNING_KEY`` 域分离派生：
       ``sha256(b"apple-signing-password:" + attachment_key)`` —— 多实例天然一致。
    3. 都没有 → ``ensure_apple_password_key()`` 启动期生成随机值写回 ``.env``
       （照抄 ``browser_password_crypto.ensure_password_key`` 路径）。

Blob 格式（带版本、域分离 AAD 防挪用）::

    blob = "v1:" + base64(iv_12B) + ":" + base64(ct + GCM_tag_16B)
    AAD = b"apple-signing-password:v1"

与 browser_password_crypto **不共用密钥也不共用 AAD**：同一把 attachment key
派生出两把不同的密钥，浏览器密码泄露不连带苹果密码，反之亦然。
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets

from loguru import logger

BLOB_VERSION = "v1"
AAD = b"apple-signing-password:v1"
_ENV_NAME = "APPLE_SIGNING_PASSWORD_KEY"
_FALLBACK_ENV = "AGENT_ATTACHMENT_SIGNING_KEY"
_KEY_ENV = "BROWSER_PASSWORD_KEY"  # 第 3 兜底派生源（browser_password_crypto 持久化的键）
MIN_KEY_LEN = 16


def _raw_key() -> bytes:
    """32 字节对称密钥；无任何来源时返回 ``b""``（调用方据此判错）。"""
    raw = os.getenv(_ENV_NAME)
    if raw and raw.strip():
        key = raw.strip().encode("utf-8")
        if len(key) >= MIN_KEY_LEN:
            return hashlib.sha256(key).digest()
    fallback = os.getenv(_FALLBACK_ENV)
    if fallback and fallback.strip():
        return hashlib.sha256(
            b"apple-signing-password:" + fallback.strip().encode("utf-8")
        ).digest()
    # 第 3 兜底：复用浏览器密码管理器已生成并持久化的键（同 attachment key 派生链，
    # 但本模块在派生串里带自己的域前缀，得到的密钥与其不同）。
    shared = os.getenv(_KEY_ENV)
    if shared and shared.strip():
        return hashlib.sha256(
            b"apple-signing-password:" + shared.strip().encode("utf-8")
        ).digest()
    return b""


def _key() -> bytes:
    key = _raw_key()
    if not key:
        raise RuntimeError("apple signing password key unavailable")
    return key


def ensure_apple_password_key() -> str:
    """启动时确保密钥存在：缺失即生成随机值并持久化到 ``.env``。

    不静默降级——存了密码的配置在缺密钥的实例上无法自动重登。生成后写回
    ``.env``（照抄 ``browser_password_crypto.ensure_password_key`` 的持久化路径）
    并注入当前进程环境。多实例部署应显式配同一密钥。
    """
    existing = os.getenv(_ENV_NAME)
    if existing and existing.strip():
        return existing.strip()
    generated = secrets.token_urlsafe(48)
    os.environ[_ENV_NAME] = generated
    try:
        import dotenv_loader

        dotenv_loader.update_env_vars({_ENV_NAME: generated}, path=dotenv_loader.ENV_FILE)
        logger.info("[apple-signing] generated and persisted {} to .env", _ENV_NAME)
    except Exception as exc:  # noqa: BLE001 — 持久化失败不阻断启动，本进程仍可用内存值
        logger.warning(
            "[apple-signing] generated {} but failed to persist to .env ({}); "
            "it will regenerate on restart, breaking previously stored passwords",
            _ENV_NAME, exc,
        )
    return generated


def encrypt_apple_password(plaintext: str) -> str:
    """AES-256-GCM 加密，返回 ``v1:iv:ct`` 形状的 blob。明文只在此函数入参一瞬。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _key()
    iv = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), AAD)
    return (
        f"{BLOB_VERSION}:"
        + base64.b64encode(iv).decode("ascii")
        + ":"
        + base64.b64encode(ct).decode("ascii")
    )


def decrypt_apple_password(blob: str) -> str:
    """解密 ``v1:iv:ct`` blob。密钥已轮换/不匹配时抛 ``InvalidTag``（不可静默）。"""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    parts = (blob or "").split(":")
    if len(parts) != 3 or parts[0] != BLOB_VERSION:
        raise ValueError("apple password blob 形状不对")
    iv = base64.b64decode(parts[1])
    ct = base64.b64decode(parts[2])
    try:
        return AESGCM(_key()).decrypt(iv, ct, AAD).decode("utf-8")
    except InvalidTag:
        # 密钥轮换后旧密文解不开——调用方把它翻译成「需重新登录」，绝不静默。
        raise
