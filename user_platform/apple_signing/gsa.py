"""Apple GrandSlam（GSA）认证（移植自 iPASide gsa.py，服务端化为纯函数）。

对 ``gsa.apple.com/grandslam`` 实现 Apple 修改版 SRP-6a 登录，anisette 头来自本包
anisette 模块。社区 GrandSlam 实现（JJTech0130 / nythepegasus）的忠实移植，
三处生产化改动：(1) anisette 进程内提供而非远端服务器，(2) TLS 校验保持开启，
(3) 2FA 两步流（触发推送 → 提交验证码）跨两次 HTTP 请求，pending 状态由路由层
持有（进程内 TTL dict），引擎不再自管磁盘 session。

**密码绝不落库/落日志**：只进函数参数，用完即弃。持久化的是 session token
（adsid / GsIdmsToken / auth_token），由路由层写 PG secret_data。
``complete_2fa`` 需重发密码——它内部再跑一遍 SRP 认证拿 fresh session 才能换
developer token（iPASide 原设计，勿改成缓存密码）。

同步 requests 网络——路由层 asyncio.to_thread 包装。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import plistlib
from typing import Any

import requests
import srp._pysrp as srp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import anisette, tls
from .errors import GsaError

# Apple 变体 SRP：SHA-256、2048-bit group、x 计算不含用户名。
srp.rfc5054_enable()
srp.no_username_in_x()

_GS_LOOKUP = "https://gsa.apple.com/grandslam/GsService2/lookup"
_XCODE_APP_INFO = "com.apple.gs.xcode.auth"
_TIMEOUT = 30

# isideload GrandSlam base_headers：URL bag / gsService / mid / 2FA 端点的公共
# 底座头。anisette 三头不进 HTTP 头——它们只随 cpd 走 body（isideload 同款）。
_BASE_HEADERS = {
    "Content-Type": "text/x-xml-plist",
    "Accept": "text/x-xml-plist",
    "X-Mme-Client-Info": "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>",
    "User-Agent": "akd/1.0 CFNetwork/808.1.4",
    "X-Xcode-Version": "27.0 (27A5218g)",
    "X-Apple-App-Info": _XCODE_APP_INFO,
}

# URL bag（isideload GrandSlam::new 启动取一次）：所有端点动态取，不硬编码——
# 2FA 的 trustedDeviceSecondaryAuth/validateCode、gsService、mid 端点全在里面。
_url_bag: dict[str, Any] | None = None


def _base_request_headers() -> dict[str, str]:
    return dict(_BASE_HEADERS)


def _fetch_url_bag() -> dict[str, Any]:
    global _url_bag
    if _url_bag is not None:
        return _url_bag
    status, _hdrs, content = _http("GET", _GS_LOOKUP, _base_request_headers(), b"")
    if status >= 400:
        raise GsaError(f"URL bag 请求失败 (HTTP {status})：{content[:200]!r}")
    urls = plistlib.loads(content).get("urls")
    if not isinstance(urls, dict):
        raise GsaError("URL bag 响应缺 urls")
    _url_bag = urls
    return _url_bag


def _bag_url(key: str) -> str:
    urls = _fetch_url_bag()
    url = urls.get(key)
    if not url:
        raise GsaError(f"URL bag 缺 {key}（现有: {sorted(urls)}）")
    return str(url)

# gsa.apple.com 对数据中心/非 Apple 认可网络 IP 直接回 503（无友好错误码）。


def _http(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    """统一的 HTTP 出口：直连（trust_env=False）。

    trust_env=False：不吃 HTTP(S)_PROXY 环境变量——环境变量代理只会把 GSA
    请求绕进 Clash 抖动里（实测 22s/请求）。
    """
    with requests.Session() as session:
        session.trust_env = False
        resp = session.request(
            method,
            url,
            headers=headers,
            data=body,
            timeout=_TIMEOUT,
            verify=tls.ca_bundle(),
        )
    return resp.status_code, dict(resp.headers), resp.content

_PLIST_PROLOG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
)


# --------------------------------------------------------------------------- #
# 请求管线
# --------------------------------------------------------------------------- #
def _cpd(headers: dict[str, str]) -> dict[str, Any]:
    """Client-provided data：对齐 isideload——只放固定标志位 + 这三个 anisette 字段。

    多放 X-Apple-I-MD-LU / X-Apple-I-MD-RINFO 会让 Apple 报 MID is invalid (-80009)。
    loc 固定 en_US（isideload 也是硬编码，不读 X-Apple-Locale）。
    """
    return {
        "bootstrap": True,
        "icscrec": True,
        "pbe": False,
        "prkgen": True,
        "svct": "iCloud",
        "loc": "en_US",
        "X-Mme-Device-Id": headers.get("X-Mme-Device-Id", ""),
        "X-Apple-I-MD": headers.get("X-Apple-I-MD", ""),
        "X-Apple-I-MD-M": headers.get("X-Apple-I-MD-M", ""),
    }


def _gs_request(params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    # cpd 必须作为 Request 下的嵌套字典（"cpd": {...}），不可把它的字段平铺进
    # Request——isideload plist!{ "Request": {"cpd": cpd, ...}}。
    # 平铺会让 Apple 找不到 cpd，报 -80009 MID is invalid。
    request_body = dict(params)
    request_body["cpd"] = _cpd(headers)
    body = {"Header": {"Version": "1.0.1"}, "Request": request_body}
    status, _hdrs, content = _http("POST", _bag_url("gsService"), _base_request_headers(), plistlib.dumps(body))
    if status >= 400:
        from . import anisette as _a
        src = "远程" if _a._remote_server else "本地库"
        raise GsaError(f"Apple GSA 请求失败 (HTTP {status}，anisette 来源: {src})：{content[:200]!r}")
    return plistlib.loads(content)["Response"]


def _check(response: dict[str, Any]) -> None:
    status = response.get("Status", response)
    ec = status.get("ec", 0)
    if ec != 0:
        raise GsaError(f"Apple error {ec}: {status.get('em', 'unknown error')}")


# --------------------------------------------------------------------------- #
# SRP 密码学
# --------------------------------------------------------------------------- #
def _derive_password(password: str, salt: bytes, iterations: int, s2k_fo: bool) -> bytes:
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    if s2k_fo:
        digest = digest.hex().encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", digest, salt, iterations, 32)


def _session_hmac(session_key: bytes, label: str) -> bytes:
    return hmac.new(session_key, label.encode(), hashlib.sha256).digest()


def _loads_plist(raw: bytes) -> dict[str, Any]:
    """解析 GSA plist，容忍缺失的 XML prolog。"""
    try:
        return plistlib.loads(raw)
    except Exception:  # noqa: BLE001 — GSA 部分响应缺 prolog
        return plistlib.loads(_PLIST_PROLOG + raw)


def _decrypt_spd(session_key: bytes, data: bytes) -> dict[str, Any]:
    key = _session_hmac(session_key, "extra data key:")
    iv = _session_hmac(session_key, "extra data iv:")[:16]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    raw = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    raw = unpadder.update(raw) + unpadder.finalize()
    return _loads_plist(raw)


def _authenticate_once(
    email: str, password: str, headers: dict[str, str]
) -> tuple[dict[str, Any], str | None]:
    """跑一遍完整 SRP 握手。返回 (session-data, 二次认证类型)。"""
    usr = srp.User(email, b"", hash_alg=srp.SHA256, ng_type=srp.NG_2048)
    _, a_pub = usr.start_authentication()

    init = _gs_request(
        {"A2k": a_pub, "ps": ["s2k", "s2k_fo"], "u": email, "o": "init"}, headers
    )
    _check(init)

    protocol = init.get("sp", "s2k")
    if protocol not in ("s2k", "s2k_fo"):
        raise GsaError(f"unsupported SRP protocol from server: {protocol}")

    # 拿到盐后喂入加盐迭代密码。
    usr.p = _derive_password(password, init["s"], init["i"], protocol == "s2k_fo")
    m1 = usr.process_challenge(init["s"], init["B"])
    if m1 is None:
        raise GsaError("failed to process SRP challenge (bad server response)")

    complete = _gs_request(
        {"c": init["c"], "M1": m1, "u": email, "o": "complete"}, headers
    )
    _check(complete)

    usr.verify_session(complete["M2"])
    if not usr.authenticated():
        raise GsaError("server session verification failed (possible MITM)")

    spd = _decrypt_spd(usr.get_session_key(), complete["spd"])
    secondary = complete.get("Status", {}).get("au")
    # isideload：repair = 没开 2FA → LoggedIn；未知 au → NeedsExtraStep，
    # 取 com.apple.gs.idms.pet token 兜底成功则同样视为已登录。
    if secondary == "repair":
        secondary = None
    elif secondary and secondary not in ("trustedDeviceSecondaryAuth", "secondaryAuth"):
        pet_ok = False
        try:
            pet_ok = bool(
                spd.get("t", {}).get("com.apple.gs.idms.pet", {}).get("token")
            )
        except Exception:  # noqa: BLE001 — spd 字段形态异常按无 pet 处理
            pet_ok = False
        if pet_ok:
            secondary = None
    return spd, secondary


# --------------------------------------------------------------------------- #
# 两步验证（受信设备推送）——isideload build_2fa_headers + URL bag 端点
# --------------------------------------------------------------------------- #
def _identity_token(adsid: str, idms_token: str) -> str:
    return base64.b64encode(f"{adsid}:{idms_token}".encode()).decode()


def _twofa_headers(adsid: str, idms_token: str, headers: dict[str, str]) -> dict[str, str]:
    """isideload：grandslam get() 的 base_headers + anisette 三头 + 身份头。

    anisette 头只带 X-Mme-Device-Id / X-Apple-I-MD / X-Apple-I-MD-M
    （AnisetteData::get_header_map），**不带 X-Apple-I-MD-LU**——isideload 里
    LU 是注释掉的，多带会改变 Apple 对请求的设备指纹判定。
    """
    out = _base_request_headers()
    for key in ("X-Mme-Device-Id", "X-Apple-I-MD", "X-Apple-I-MD-M"):
        out[key] = headers.get(key, "")
    out["X-Apple-Identity-Token"] = _identity_token(adsid, idms_token)
    out["X-Apple-I-MD-RINFO"] = headers.get("X-Apple-I-MD-RINFO", "")
    return out


def _trigger_trusted(adsid: str, idms_token: str, headers: dict[str, str]) -> None:
    """让 Apple 往受信设备推送 2FA 验证码（URL 从 bag 取，不硬编码）。

    响应体即使成功也是 HTML 中间页，**status 才是信号**。
    """
    status, _hdrs, _content = _http(
        "GET", _bag_url("trustedDeviceSecondaryAuth"),
        _twofa_headers(adsid, idms_token, headers), b""
    )
    if status != 200:
        raise GsaError(
            f"Apple 未发送验证码 (HTTP {status})。请检查网络后重试。"
        )


def _submit_trusted(adsid: str, idms_token: str, code: str, headers: dict[str, str]) -> None:
    req_headers = _twofa_headers(adsid, idms_token, headers)
    req_headers["security-code"] = code
    status, _hdrs, content = _http("GET", _bag_url("validateCode"), req_headers, b"")
    if status >= 400:
        raise GsaError(f"Apple 2FA 验证失败 (HTTP {status})：{content[:200]!r}")
    _check(plistlib.loads(content))


# --------------------------------------------------------------------------- #
# SMS 2FA（移植自 isideload apple_account.rs：trusted-phone JSON 流程）
# --------------------------------------------------------------------------- #
# isideload 的 SMS 路径走 gsa.apple.com/auth 的 JSON IDMS 端点（与 trusted-device
# 的 plist 端点不同）。三步：列号码 → PUT /auth/verify/phone 发短信 →
# POST /auth/verify/phone/securitycode 验码。Content-Type 是 application/json，
# 不是 text/x-xml-plist——_sms_headers 单独构造。
_GS_AUTH = "https://gsa.apple.com/auth"
_GS_SMS_SEND = "https://gsa.apple.com/auth/verify/phone"
_GS_SMS_VERIFY = "https://gsa.apple.com/auth/verify/phone/securitycode"


def _sms_headers(adsid: str, idms_token: str, headers: dict[str, str]) -> dict[str, str]:
    """isideload get_sms/post_sms/put_sms：JSON 端点的 2FA 头。"""
    out = _base_request_headers()
    # base 的 plist Accept/Content-Type 改成 JSON（与 isideload base_headers(sms=True) 一致）
    out["Content-Type"] = "application/json"
    out["Accept"] = "application/json"
    for key in ("X-Mme-Device-Id", "X-Apple-I-MD", "X-Apple-I-MD-M"):
        out[key] = headers.get(key, "")
    out["X-Apple-Identity-Token"] = _identity_token(adsid, idms_token)
    out["X-Apple-I-MD-RINFO"] = headers.get("X-Apple-I-MD-RINFO", "")
    return out


class _SmsServiceError(GsaError):
    """Apple SMS 端点返回的 serviceErrors（可携带可重试/终止信号）。"""

    def __init__(self, code: str, title: str, message: str) -> None:
        super().__init__(f"{code}: {title} - {message}")
        self.code = code


def _parse_sms_service_errors(text: str) -> list[dict[str, str]]:
    """isideload parse_sms_service_error：从 serviceErrors 数组抽 {code,title,message}。"""
    try:
        import json as _json
        payload = _json.loads(text)
    except Exception:  # noqa: BLE001 — 非 JSON 错误体不是 SMS 服务错误
        return []
    errors = payload.get("serviceErrors") if isinstance(payload, dict) else None
    if not isinstance(errors, list) or not errors:
        return []
    out: list[dict[str, str]] = []
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        out.append({
            "code": str(entry.get("code") or "unknown"),
            "title": str(entry.get("title") or ""),
            "message": str(entry.get("message") or ""),
        })
    return out


def list_trusted_phone_numbers(adsid: str, idms_token: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """GET gsa.apple.com/auth → trustedPhoneNumbers（isideload get_trusted_numbers）。

    返回 [{"id": int, "number_with_dial_code": str, ...}]；空列表意味着账号无受信
    电话号码，SMS 2FA 不可用。
    """
    status, _hdrs, content = _http(
        "GET", _GS_AUTH, _sms_headers(adsid, idms_token, headers), b""
    )
    if status != 200:
        raise GsaError(f"Apple 未返回受信电话号码 (HTTP {status})：{content[:200]!r}")
    try:
        import json as _json
        payload = _json.loads(content)
    except Exception as exc:  # noqa: BLE001
        raise GsaError(f"受信电话号码响应无法解析：{exc}") from exc
    numbers = payload.get("trustedPhoneNumbers") if isinstance(payload, dict) else None
    if not isinstance(numbers, list):
        return []
    return [{"id": n.get("id"), "number_with_dial_code": n.get("numberWithDialCode")} for n in numbers if isinstance(n, dict)]


# SMS 错误码分类（isideload apple_account.rs）：
#   -21669      验证码错误 → 可重试（保留 pending）
#   -22979/-22981 短信重发受限，但上一条码仍有效 → 仍进输码态
#   -28248      未知 2FA / 人工处理 → 终止
SMS_RETRYABLE_CODES = {"-21669"}
SMS_CODE_STILL_VALID_CODES = {"-22979", "-22981"}
SMS_TERMINAL_CODES = {"-28248"}


def _raise_sms_error(status: int, content: bytes) -> None:
    """把 SMS 端点的错误体映射成可分类的 GsaError（保留码供路由层判定重试）。"""
    text = content.decode("utf-8", "replace") if isinstance(content, (bytes, bytearray)) else str(content)
    for entry in _parse_sms_service_errors(text):
        code = entry["code"]
        if code in SMS_TERMINAL_CODES:
            raise GsaError(f"Apple 要求额外的二次认证步骤 ({code})：{entry['message']}")
        # 可重试/旧码仍有效的错误统一标 GsaError；路由层按 code 决定保留 pending。
        raise _SmsServiceError(code, entry["title"], entry["message"])
    raise GsaError(f"Apple SMS 请求失败 (HTTP {status})：{text[:200]}" )


def send_sms_code(adsid: str, idms_token: str, phone_id: int, headers: dict[str, str]) -> None:
    """PUT gsa.apple.com/auth/verify/phone {phoneNumber:{id},mode:"sms"}（isideload send_sms_2fa）。

    -22979/-22981：Apple 拒发新短信但旧码仍有效，**不算失败**——路由层让用户继续输码。
    """
    import json as _json
    body = _json.dumps({"phoneNumber": {"id": phone_id}, "mode": "sms"}).encode()
    status, _hdrs, content = _http(
        "PUT", _GS_SMS_SEND, _sms_headers(adsid, idms_token, headers), body
    )
    if status in (200, 412):
        return  # 412 = 已有活跃 challenge（旧码仍有效），isideload 也判成功
    if status >= 400:
        text = content.decode("utf-8", "replace") if isinstance(content, (bytes, bytearray)) else str(content)
        for entry in _parse_sms_service_errors(text):
            if entry["code"] in SMS_CODE_STILL_VALID_CODES:
                return  # 旧码仍有效，继续进输码态
            raise _SmsServiceError(entry["code"], entry["title"], entry["message"])
    # 非预期状态也按失败
    raise GsaError(f"Apple SMS 发送失败 (HTTP {status})：{content[:200]!r}")


def verify_sms_code(adsid: str, idms_token: str, phone_id: int, code: str, headers: dict[str, str]) -> None:
    """POST gsa.apple.com/auth/verify/phone/securitycode（isideload verify_sms_2fa）。

    -21669 验证码错误 → 抛 _SmsServiceError(code="-21669")，路由层保留 pending 让用户重输。
    其他 serviceErrors 终止当前 pending。
    """
    import json as _json
    body = _json.dumps({
        "securityCode": {"code": code},
        "phoneNumber": {"id": phone_id},
        "mode": "sms",
    }).encode()
    status, _hdrs, content = _http(
        "POST", _GS_SMS_VERIFY, _sms_headers(adsid, idms_token, headers), body
    )
    if status == 200:
        return
    text = content.decode("utf-8", "replace") if isinstance(content, (bytes, bytearray)) else str(content)
    for entry in _parse_sms_service_errors(text):
        raise _SmsServiceError(entry["code"], entry["title"], entry["message"])
    raise GsaError(f"Apple SMS 验证失败 (HTTP {status})：{text[:200]}")


# --------------------------------------------------------------------------- #
# App-token 交换（developer services 用的按服务 Xcode token）
# --------------------------------------------------------------------------- #
_XCODE_AUTH_APP = "com.apple.gs.xcode.auth"


def _coerce_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return base64.b64decode(value)
    raise GsaError("expected bytes or base64 string in session data")


def _app_tokens_checksum(sk: bytes, adsid: str, apps: list[str]) -> bytes:
    mac = hmac.new(sk, b"apptokens" + adsid.encode(), hashlib.sha256)
    for app in apps:
        mac.update(app.encode())
    return mac.digest()


def _decrypt_gcm(sk: bytes, encrypted: bytes) -> bytes:
    # Apple 线格式："XYZ"（3 字节，同时是 AAD）| IV(16) | 密文 | tag(16)。
    if len(encrypted) < 35 or encrypted[:3] != b"XYZ":
        raise GsaError("malformed encrypted app token")
    iv = encrypted[3:19]
    ciphertext_and_tag = encrypted[19:]
    return AESGCM(sk).decrypt(iv, ciphertext_and_tag, b"XYZ")


def _fetch_app_token(
    spd: dict[str, Any], headers: dict[str, str], app: str = _XCODE_AUTH_APP
) -> dict[str, Any]:
    """把 GSA session 换成按域 Xcode token（X-Apple-GS-Token）。"""
    sk = _coerce_bytes(spd["sk"])
    adsid = spd["adsid"]
    params = {
        "u": adsid,
        "app": [app],
        "c": spd["c"],
        "t": spd["GsIdmsToken"],
        "checksum": _app_tokens_checksum(sk, adsid, [app]),
        "o": "apptokens",
    }
    response = _gs_request(params, headers)
    _check(response)
    token_plist = _loads_plist(_decrypt_gcm(sk, response["et"]))
    token_info = token_plist["t"][app]
    return {"token": token_info["token"], "expiry": token_info.get("expiry")}


def _finalize_session(
    email: str, spd: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    """铸出 developer-services token，返回路由层落库的 session dict。"""
    app_token = _fetch_app_token(spd, headers)
    return {
        "status": "authenticated",
        "session": {
            "email": email,
            "adsid": spd.get("adsid"),
            "GsIdmsToken": spd.get("GsIdmsToken"),
            "auth_token": app_token.get("token"),
            "auth_token_expiry": app_token.get("expiry"),
        },
    }


# --------------------------------------------------------------------------- #
# 公共 API（纯函数：状态由调用方持有）
# --------------------------------------------------------------------------- #
def begin_login(email: str, password: str) -> dict[str, Any]:
    """启动登录。返回 authenticated（带 session）或 2fa_required（带 pending 数据）。

    - ``{"status": "authenticated", "session": {email, adsid, GsIdmsToken,
       auth_token, auth_token_expiry}}``
    - ``{"status": "2fa_required", "method": "trusteddevice"|"sms",
       "pending": {"email", "adsid", "idms", "method"},
       "phone_numbers": [{"id","number_with_dial_code"}]}``（仅 SMS 路径带
       phone_numbers；路由层存 phone_numbers 供前端选号。trusted-device 路径推送
       一次即让受信设备弹码，无号码列表。）
    """
    headers = anisette.get_headers()
    spd, secondary = _authenticate_once(email, password, headers)
    if not secondary:
        return _finalize_session(email, spd, headers)

    adsid, idms = spd["adsid"], spd["GsIdmsToken"]
    method = "trusteddevice" if secondary == "trustedDeviceSecondaryAuth" else "sms"
    result: dict[str, Any] = {
        "status": "2fa_required",
        "method": method,
        "pending": {"email": email, "adsid": adsid, "idms": idms, "method": method},
    }
    if method == "trusteddevice":
        _trigger_trusted(adsid, idms, anisette.get_headers())
    else:
        # SMS：列出受信电话号码供前端选号。失败不阻塞登录——路由层在前端选号
        # 时再调 list_trusted_phone_numbers 端点（pending 已含 adsid/idms）。
        try:
            result["phone_numbers"] = list_trusted_phone_numbers(
                adsid, idms, anisette.get_headers()
            )
        except GsaError:
            # 号码列表拿不到（网络/权限），让前端在选号时重试；pending 已带身份。
            result["phone_numbers"] = []
    return result


def complete_2fa(
    email: str, password: str, code: str, pending: dict[str, Any]
) -> dict[str, Any]:
    """提交 2FA 码后**重新认证**拿 session token（密码必须重发，见模块 docstring）。

    pending 是 begin_login 返回的 ``pending`` dict（路由层存取）。码错 → GsaError。
    SMS 路径（method=="sms"，phone_id 非空）走 gsa.apple.com/auth 的 JSON IDMS
    流程；trusted-device 路径走 GsService2/validateCode 的 plist 流程。
    """
    method = pending.get("method")
    headers = anisette.get_headers()
    if method == "trusteddevice":
        _submit_trusted(pending["adsid"], pending["idms"], code, headers)
    elif method == "sms":
        phone_id = pending.get("phone_id")
        if not phone_id:
            raise GsaError("SMS 2FA 缺少 phone_id（请先调用 list_trusted_phone_numbers 选号）")
        verify_sms_code(pending["adsid"], pending["idms"], int(phone_id), code, headers)
    else:
        raise GsaError(f"未知 2FA 方法: {method!r}")

    spd, secondary = _authenticate_once(email, password, headers)
    if secondary:
        raise GsaError(f"2FA 后仍要求二次认证: {secondary}")
    return _finalize_session(email, spd, headers)
