"""TOTP（RFC 6238）验证码生成 + ``otpauth://`` URI 解析。

纯标准库实现（hmac / hashlib / struct / base64），不引入任何第三方依赖——
``opencv`` 在本项目是孤儿安装、``cryptography`` 的 twofactor.TOTP 签名不稳，
而 RFC 6238 本身就二十行。所有上层（存储层、WS 帧、REST、扩展下发的码）共用
这一份口径，避免「服务端算的码」与「扩展算的码」漂移。

``otpauth://totp/<label>?secret=..&issuer=..&algorithm=..&digits=..&period=..``
是 Google Authenticator 系（含 Microsoft/ AWS / 各大站 2FA 向导）二维码的标准
负载。解析容忍：label 里 ``Issuer:account`` 或纯 account 两种形态、大小写、
缺省参数（algorithm=SHA1、digits=6、period=30）。

⚠ 本模块**不解码二维码图片**：识别 QR 归扩展（jsQR，浏览器侧有现成图像管线），
服务端只接收已解析出的 ``otpauth://`` 文本或裸密钥，杜绝把图像上传到服务端。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

# 与 Google Authenticator 一致的默认参数；偏离它们的站点靠 URI 参数覆盖。
DEFAULT_DIGITS = 6
DEFAULT_PERIOD = 30
DEFAULT_ALGORITHM = "SHA1"

# 算法名 → hashlib 构造器。RFC 4226/6238 只认这三家。
_ALGOS = {
    "SHA1": hashlib.sha1,
    "SHA256": hashlib.sha256,
    "SHA512": hashlib.sha512,
}

# base32 密钥清洗：丢空格/连字符、大小写归一，只留 RFC 4648 字母表字符。
_B32_STRIP = re.compile(r"[^A-Z2-7]")
# 单条记录能接受的密钥上限（base32 后字节），防异常长串撑爆内存。
MAX_SECRET_BYTES = 512


def normalize_secret(secret: str) -> str:
    """任意形态 base32（带空格/小写/尾部漏 padding）→ 大写、去填充的 base32 串。

    抛 ``ValueError("secret_required")``（空）/ ``ValueError("secret_invalid_base32")``
    （清洗后无法解码）。返回的是**已规范**的密钥，存储层直接加密它、生成器直接解码它。
    """
    raw = _B32_STRIP.sub("", str(secret or "").upper())
    if not raw:
        raise ValueError("secret_required")
    # b32decode 要求长度是 8 的倍数：补 '=' 到下一个 8。
    padded = raw + "=" * ((-len(raw)) % 8)
    try:
        key = base64.b32decode(padded, casefold=False)
    except Exception as exc:  # binascii.Error 等
        raise ValueError("secret_invalid_base32") from exc
    if not key or len(key) > MAX_SECRET_BYTES:
        raise ValueError("secret_invalid_base32")
    # 去掉补上的 '='，只回有效字符，作为规范化存储形态。
    return raw


def _secret_bytes(secret: str) -> bytes:
    """规范化 base32 → 原始密钥字节。假定调用方已 normalize_secret。"""
    padded = secret + "=" * ((-len(secret)) % 8)
    return base64.b32decode(padded)


def totp_code(
    secret: str,
    *,
    at: float | None = None,
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """当前（或 ``at`` 指定时刻）的 TOTP 码，左补零到 ``digits`` 位。

    ``secret`` 必须是 ``normalize_secret`` 规范过的 base32。时间步 = ``floor(at/period)``，
    用大端 8 字节计数喂 HMAC（RFC 6238 §5.2 / 4232 R4）。动态截断（RFC 4232 R4）。
    """
    moment = at if at is not None else time.time()
    algo = _ALGOS.get(str(algorithm or "").upper())
    if algo is None:
        raise ValueError("algorithm_unsupported")
    counter = int(moment) // int(period)
    key = _secret_bytes(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), algo).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    code = truncated % (10 ** int(digits))
    return str(code).zfill(int(digits))


def seconds_remaining(*, at: float | None = None, period: int = DEFAULT_PERIOD) -> int:
    """本步还有多少秒翻转（扩展画倒计时环用）。范围 1..period。"""
    moment = at if at is not None else time.time()
    step = int(period)
    return step - (int(moment) % step)


def expires_at(*, at: float | None = None, period: int = DEFAULT_PERIOD) -> int:
    """本步的**绝对失效时刻**（epoch 秒，= 下一个 period 边界）。

    出网一律用绝对时刻而不是「剩余秒数」：剩余秒要客户端自己从收到那刻起倒计时，
    网络延迟 + 客户端与服务器时钟不一致会越读越偏；绝对时刻让客户端用
    ``expiresAt - now`` 直接算，且订阅回帧与后续推送携带**同一个** expiresAt。
    """
    moment = at if at is not None else time.time()
    step = int(period)
    return (int(moment) // step + 1) * step


def parse_otpauth(uri: str) -> dict:
    """``otpauth://`` URI → 结构化字段（供存储层直接落库、UI 直接回填）。

    返回 ``{kind, label, issuer, account, secret, digits, period, algorithm}``，
    ``secret`` 已 normalize。解析失败抛 ``ValueError("<code>")``：
      - ``uri_required`` 空
      - ``uri_not_otpauth`` scheme 不是 otpauth
      - ``uri_not_totp`` kind 不是 totp（hotp/motp 等不支持自动出码）
      - ``secret_required`` / ``secret_invalid_base32``（转 normalize 的错误）

    label 形如 ``GitHub:alice@example.com`` 时按首个 ``:`` 拆 issuer/account；
    查询串里的 ``issuer`` 优先于 label 前缀（RFC 一致口径）。
    """
    text = str(uri or "").strip()
    if not text:
        raise ValueError("uri_required")
    parsed = urlparse(text)
    if parsed.scheme.lower() != "otpauth":
        raise ValueError("uri_not_otpauth")
    # otpauth://totp/LABEL?.. → netloc 空、path = "/LABEL"
    kind = (parsed.netloc or parsed.path.strip("/").split("/", 1)[0] or "").lower()
    if kind != "totp":
        raise ValueError("uri_not_totp")
    # label = path 去掉首个 "/" 段（kind）之后的部分
    label = unquote(parsed.path)
    if label.startswith("/"):
        label = label[1:]
    # 有些实现把 kind 放 path（totp/Label），此处 label 可能仍含 "totp/"
    if label.lower().startswith("totp/"):
        label = label[len("totp/"):]
    qs = parse_qs(parsed.query or "", keep_blank_values=True)
    # 键名归一小写：个别工具吐的是 SECRET= / Issuer= 这类混合大小写。
    qs = {str(k).lower(): v for k, v in qs.items()}

    def _one(name: str, default: str = "") -> str:
        vals = qs.get(name) or []
        return (vals[0] if vals else default) or default

    issuer = _one("issuer")
    label_issuer, label_account = _split_label(label)
    # query 里的 issuer 是真相源：label 前缀按它剥，而不是「见首个冒号就拆」——
    # issuer 本身含冒号时（如 "A:B"，编码后 label = "A%3AB:acct"，解码成 "A:B:acct"）
    # 首冒号拆分会把 account 错拆成 "B:acct"。
    if issuer and label.startswith(issuer + ":"):
        account = label[len(issuer) + 1:].strip()
    elif issuer and label.startswith(quote(issuer, safe="") + ":"):
        account = unquote(label[len(quote(issuer, safe="")) + 1:]).strip()
    else:
        account = label_account
    issuer = issuer or label_issuer

    digits = _int_param(_one("digits"), DEFAULT_DIGITS, field="digits")
    period = _int_param(_one("period"), DEFAULT_PERIOD, field="period")
    algorithm = (_one("algorithm", DEFAULT_ALGORITHM) or DEFAULT_ALGORITHM).upper()

    return {
        "kind": "totp",
        "label": label,
        "issuer": issuer,
        "account": account,
        "secret": normalize_secret(_one("secret")),
        "digits": digits,
        "period": period,
        "algorithm": algorithm,
    }


def _split_label(label: str) -> tuple[str, str]:
    """``"GitHub:alice"`` → ``("GitHub", "alice")``；无 ``:`` 时 issuer 空、account=整串。"""
    label = str(label or "").strip()
    if ":" in label:
        head, tail = label.split(":", 1)
        return head.strip(), tail.strip()
    return "", label


def _int_param(value: str, default: int, *, field: str) -> int:
    """URI 整数参数解析 + 边界校验（非法回落到默认，越界抛错让上层给可读提示）。"""
    value = str(value or "").strip()
    if not value:
        return default
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    if field == "digits":
        if not (6 <= num <= 8):
            raise ValueError("digits_unsupported")
    elif field == "period":
        if not (15 <= num <= 300):
            raise ValueError("period_unsupported")
    return num


def build_otpauth_uri(
    *,
    issuer: str,
    account: str,
    secret: str,
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """把一条验证器拼成 ``otpauth://`` URI（导出备份用）。

    与 :func:`parse_otpauth` 互逆：``parse_otpauth(build_otpauth_uri(...))``
    round-trip 得到同一批字段（``build`` 产出的 ``secret`` 是规范 base32，回读时
    ``normalize_secret`` 幂等）。label 里 ``issuer:account`` 都做 URL 编码（account
    常含 ``@``，issuer 可能含 ``:``）。``digits``/``period``/``algorithm`` 只在偏离
    默认时才写进 query——与各大验证器导出的格式一致，且默认值回读时由
    ``parse_otpauth`` 补齐，不会丢信息。

    label 里 ``issuer:account`` 两个段各自 quote：issuer 可能含 ``:``（少数站点名
    带冒号），account 含 ``@``——整串一起 quote 会把分隔符 ``:`` 也编码掉，回读时
    就拆不出 issuer/account 了。
    """
    label = f"{quote(str(issuer), safe='')}:{quote(str(account), safe='')}" if issuer else quote(str(account), safe="")
    params = [("secret", normalize_secret(secret))]
    if issuer:
        params.append(("issuer", str(issuer)))
    if int(digits) != DEFAULT_DIGITS:
        params.append(("digits", str(int(digits))))
    if int(period) != DEFAULT_PERIOD:
        params.append(("period", str(int(period))))
    algo = str(algorithm or DEFAULT_ALGORITHM).upper()
    if algo != DEFAULT_ALGORITHM:
        params.append(("algorithm", algo))
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
    return f"otpauth://totp/{label}?{query}"
