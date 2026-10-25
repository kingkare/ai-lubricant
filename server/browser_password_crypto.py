"""浏览器插件密码管理器：服务端列加密（AES-256-GCM）。

用户在浏览器登录成功后由插件弹卡询问是否保存，密码经已鉴权桥接 WS 上行到
服务端，落 ``browser_passwords.password_enc`` 列。明文只在浏览器内、WS 帧
（token 鉴权、不落日志）与服务端瞬时内存中存在；数据库只见密文。

密钥来源（优先级）::

    1. 环境变量 ``BROWSER_PASSWORD_KEY``（strip 后非空，长度 ≥ 16）。
    2. 从 ``AGENT_ATTACHMENT_SIGNING_KEY`` 域分离派生：
       ``sha256(b"browser-password:" + attachment_key)`` —— 多实例天然一致
       （该键已要求多实例配同值）。
    3. 都没有 → ``ensure_password_key()`` 在启动期生成随机值并写回 ``.env``
       （照抄 ``attachment_signing.ensure_signing_key`` 路径）。

Blob 格式（带版本、域分离 AAD 防挪用）::

    blob = "v1:" + base64(iv_12B) + ":" + base64(ct + GCM_tag_16B)
    AAD = b"browser-password:v1"

密钥轮换 = 已存密码全部无法解密（``decrypt_password`` 抛 ``InvalidTag``）。
轮换绝不静默进行：调用方（``pwd_get`` 帧处理）把 ``InvalidTag`` 翻译成
「记录无法解密（服务端密钥已变更）」并提示用户删除重录。
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets

from loguru import logger

BLOB_VERSION = "v1"
AAD = b"browser-password:v1"
_ENV_NAME = "BROWSER_PASSWORD_KEY"
_FALLBACK_ENV = "AGENT_ATTACHMENT_SIGNING_KEY"
MIN_KEY_LEN = 16


def _raw_key() -> bytes:
    """32 字节对称密钥；无任何来源时返回 ``b""``（调用方据此判错）。"""
    raw = os.getenv(_ENV_NAME)
    if raw and raw.strip():
        key = raw.strip().encode("utf-8")
        if len(key) >= MIN_KEY_LEN:
            return hashlib.sha256(key).digest()  # 任意外形 → 32B，避免长度参差
    fallback = os.getenv(_FALLBACK_ENV)
    if fallback and fallback.strip():
        return hashlib.sha256(b"browser-password:" + fallback.strip().encode("utf-8")).digest()
    return b""


def _key() -> bytes:
    key = _raw_key()
    if not key:
        raise RuntimeError("browser password key unavailable")
    return key


def ensure_password_key() -> str:
    """启动时确保密钥存在：缺失即生成随机值并持久化到 ``.env``。

    不静默降级——密码管理器是必需能力，缺密钥会让所有保存的密码无法解密。
    生成后写回 ``.env``（沿用 ``attachment_signing.ensure_signing_key`` 的同款
    持久化路径），并注入当前进程环境，使本次运行立即可用。多实例部署下应显式
    配置同一密钥；此处的自动生成是单机/首启兜底。
    """
    existing = os.getenv(_ENV_NAME)
    if existing and existing.strip():
        return existing.strip()
    generated = secrets.token_urlsafe(48)
    os.environ[_ENV_NAME] = generated
    try:
        import dotenv_loader
        dotenv_loader.update_env_vars({_ENV_NAME: generated}, path=dotenv_loader.ENV_FILE)
        logger.info("[browser-password] generated and persisted {} to .env", _ENV_NAME)
    except Exception as exc:  # noqa: BLE001 — 持久化失败不阻断启动，本进程仍可用内存值
        logger.warning(
            "[browser-password] generated {} but failed to persist to .env ({}); "
            "multi-instance deployments must set it explicitly",
            _ENV_NAME, exc,
        )
    return generated


def encrypt_password(plaintext: str) -> str:
    """明文 → ``v1:<iv_b64>:<ct_b64>``。随机 12B IV，AAD 域分离。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(_key()).encrypt(iv, plaintext.encode("utf-8"), AAD)  # ct 末尾含 16B tag
    return f"{BLOB_VERSION}:{base64.b64encode(iv).decode()}:{base64.b64encode(ct).decode()}"


def decrypt_password(blob: str) -> str:
    """``v1:<iv_b64>:<ct_b64>`` → 明文。格式/版本错抛 ``ValueError``；
    密钥不匹配或密文被篡改抛 ``cryptography.exceptions.InvalidTag``。"""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not isinstance(blob, str) or not blob:
        raise ValueError("invalid_blob")
    parts = blob.split(":")
    if len(parts) != 3:
        raise ValueError("invalid_blob")
    version, iv_b64, ct_b64 = parts
    if version != BLOB_VERSION:
        raise ValueError("unsupported_blob_version")
    try:
        iv = base64.b64decode(iv_b64)
        ct = base64.b64decode(ct_b64)
    except Exception:
        raise ValueError("invalid_blob")
    return AESGCM(_key()).decrypt(iv, ct, AAD).decode("utf-8")
