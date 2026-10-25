"""凭据文件导入识别：JSON / HAR / cookies.txt 通用解析器。

管理端「导入认证」用：用户上传浏览器导出的凭据文件，服务端识别出字段后回填账号表单
（不落库，用户确认后走正常 add 流程）。spec 可声明 parse_credential_file 钩子完全接管
解析（渠道自定义格式），未声明时走这里的通用解析。

三种格式：
- json   — 任意嵌套的凭据 JSON（token/cookie/账号信息），递归按键名识别
- har    — 浏览器导出的 HAR 抓包，从请求头提取 Cookie / Authorization
- cookies— Netscape cookies.txt（7 字段制表符分隔）

返回结构：{"format": str, "fields": {key: value}, "extras": {key: value}, "notes": [str]}
- fields: 匹配 schema_fields 声明的 key，前端可安全回填表单
- extras: 其他识别结果，前端放「额外字段」区
- notes: 给人读的说明
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit


# 原始键名 -> 标准槽位（小写匹配）
_KEY_TO_SLOT = {
    "access_token": "access_token",
    "token": "access_token",
    "bearer": "access_token",
    "authorization": "access_token",
    "api_key": "access_token",
    "key": "access_token",
    "refresh_token": "refresh_token",
    "cookie": "cookie",
    "cookies": "cookie",
    "user_name": "username",
    "username": "username",
    "user_email": "email",
    "email": "email",
    "account_id": "account_id",
    "session_id": "session_id",
    "sid": "session_id",
    "csrf_token": "csrf_token",
    "auth_token": "access_token",
}

# 标准槽位 -> 账号表单常见字段 key（按优先级排列）
_SLOT_TO_FORM_KEYS = {
    "access_token": ["password", "api_key", "key", "access_token"],
    "refresh_token": ["refresh_token"],
    "cookie": ["cookie", "cookies"],
    "username": ["username", "user_name"],
    "email": ["email", "user_email"],
    "account_id": ["account_id"],
    "session_id": ["session_id"],
    "csrf_token": ["csrf_token"],
}


def detect_format(filename: str, content_bytes: bytes) -> str:
    """检测文件格式 → "json" | "har" | "cookies" | "unknown"。"""
    name = (filename or "").lower()
    if name.endswith(".json"):
        return "json"
    if name.endswith(".har"):
        return "har"
    if "cookie" in name and (name.endswith(".txt") or name.endswith(".text")):
        return "cookies"

    try:
        text = content_bytes.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return "unknown"
    if not text:
        return "unknown"

    if text[0] in "{[":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return "unknown"
        if isinstance(data, dict) and isinstance(data.get("log"), dict) and isinstance(data["log"].get("entries"), list):
            return "har"
        return "json"

    # cookies.txt：非注释行含 >= 6 个制表符分隔列
    tab_lines = [l for l in text.splitlines() if l and not l.startswith("#") and "\t" in l]
    if tab_lines and all(len(l.split("\t")) >= 6 for l in tab_lines[:5]):
        return "cookies"
    return "unknown"


def _strip_bearer(value: str) -> str:
    v = value.strip()
    if v[:7].lower() == "bearer ":
        return v[7:].strip()
    return v


def _collect_json_slots(data: any) -> tuple[dict[str, any], list[str]]:
    """递归收集 JSON 里的凭据键 → 标准槽位。"""
    slots: dict[str, any] = {}
    notes: list[str] = []

    def walk(obj: any) -> None:
        nonlocal slots
        if isinstance(obj, dict):
            for k, v in obj.items():
                normalized = _KEY_TO_SLOT.get(str(k).strip().lower())
                if normalized is not None and isinstance(v, (str, int, float)) and str(v).strip():
                    slots.setdefault(normalized, _strip_bearer(str(v).strip()))
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        notes.append(f"文件含 {len(data['accounts'])} 个账号，已取第一个")
    return slots, notes


def _scan_har_entries(entries: list, host: str) -> tuple[str, str, int]:
    """扫描 HAR entries，返回 (cookie_header, authorization, matched_count)。"""
    cookie = ""
    authorization = ""
    matched = 0
    for entry in entries:
        request = entry.get("request") or {}
        url = str(request.get("url") or "")
        if host and url and host not in url.lower():
            continue
        matched += 1
        for header in request.get("headers") or []:
            name = str(header.get("name") or "").lower()
            value = str(header.get("value") or "")
            if name == "cookie" and value:
                cookie = value
            elif name == "authorization" and value:
                authorization = value
    return cookie, authorization, matched


def _collect_har_slots(data: dict, base_url: str = "") -> tuple[dict[str, any], list[str]]:
    """从 HAR 数据收集凭据槽位。"""
    entries = []
    log = data.get("log")
    if isinstance(log, dict) and isinstance(log.get("entries"), list):
        entries = log["entries"]
    if not entries:
        raise ValueError("HAR 文件里没有 log.entries")

    host = ""
    if base_url:
        try:
            host = (urlsplit(base_url).hostname or "").lower()
        except ValueError:
            host = ""

    cookie, authorization, matched = _scan_har_entries(entries, host)
    if not cookie and not authorization and host:
        cookie, authorization, matched = _scan_har_entries(entries, "")

    notes: list[str] = []
    if authorization:
        notes.append(f"从 HAR 提取 Authorization 头（{matched} 条请求）")
    if cookie:
        notes.append(f"从 HAR 提取 Cookie 头（{matched} 条请求）")

    slots: dict[str, any] = {}
    if authorization:
        slots["access_token"] = _strip_bearer(authorization)
    if cookie:
        slots["cookie"] = cookie
    return slots, notes


def _collect_cookies_txt_slots(text: str) -> tuple[dict[str, any], list[str]]:
    """从 cookies.txt 收集凭据槽位。"""
    cookies: list[tuple[str, str]] = []
    domains: set[str] = set()
    interesting_names = {"token", "session", "sid", "auth", "csrf", "key"}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domain = parts[0]
            name = parts[5]
            value = parts[6]
            domains.add(domain)
            cookies.append((name, value))
        elif "=" in line:
            name, _, value = line.partition("=")
            cookies.append((name.strip(), value.strip()))

    slots: dict[str, any] = {}
    notes: list[str] = []

    if cookies:
        slots["cookie"] = "; ".join(f"{n}={v}" for n, v in cookies)
        domain_note = f"（域名 {', '.join(sorted(domains))}）" if domains else ""
        notes.append(f"从 cookies.txt 识别出 {len(cookies)} 条 cookie{domain_note}")

        important = [n for n, _ in cookies if any(i in n.lower() for i in interesting_names)]
        if important:
            notes.append(f"其中 {len(important)} 条为重要凭据：{', '.join(important[:5])}")

    return slots, notes


def _slot_for_field_key(key: str) -> str | None:
    """schema 字段 key → 标准槽位（双向查表）。

    字段 key 可能是源键名（access_token/cookie…，走 _KEY_TO_SLOT），
    也可能是表单槽位名（password/api_key…，走 _SLOT_TO_FORM_KEYS 反查）。
    """
    k = (key or "").strip().lower()
    if not k:
        return None
    slot = _KEY_TO_SLOT.get(k)
    if slot:
        return slot
    for slot_name, form_keys in _SLOT_TO_FORM_KEYS.items():
        if k in form_keys:
            return slot_name
    return None


def _map_slots_to_schema(slots: dict[str, any], schema_fields: list[dict]) -> tuple[dict[str, any], dict[str, any]]:
    """将标准槽位映射到 schema 字段 key。

    同一槽位只填第一个命中的字段（凭据字段单写，避免 api_key > key > password
    的 init 优先级遮蔽出陈旧值）。未命中的槽位进 extras。
    """
    fields: dict[str, any] = {}
    extras: dict[str, any] = {}
    used_slots: set[str] = set()

    for field in schema_fields:
        key = str((field or {}).get("key") or "").strip()
        if not key:
            continue
        slot = _slot_for_field_key(key)
        if slot and slot in slots and slot not in used_slots:
            fields[key] = slots[slot]
            used_slots.add(slot)

    for slot, value in slots.items():
        if slot not in used_slots:
            extras[slot] = value

    return fields, extras


# 按密钥遮蔽展示的槽位（预览/回填时不给人看全值）
_SECRET_SLOTS = frozenset({"access_token", "refresh_token", "cookie", "session_id", "csrf_token"})

# username 槽位没被 schema 命中时，用来兜底当用户名的槽位（与 admin 授权回填同一条链）
_USERNAME_FALLBACK_SLOTS = ("email", "account_id")

# 保留原生类型的 schema 字段：number 留数字、checkbox 留布尔，其余一律转字符串。
_NATIVE_TYPE_FIELDS = frozenset({"number", "checkbox"})


def _normalize_import_key(key: str) -> str:
    """导入键名归一：camelCase 折成 snake_case，再去分隔符、小写。

    上游导出的 JSON 键名千奇百怪（``accessToken`` / ``Access_Token`` / ``user_ID``），
    渠道 schema 声明的是 snake_case。两边折到同一口径才匹配得上，否则用户明明贴对了
    却被判「缺字段」。
    """
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key or ""))
    return re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()


def _coerce_field_value(value: any, field_type: str) -> any:
    """按 schema 字段类型校正导入值。

    spec 里 ``p.uin.strip()`` / ``p.user_id.startswith(...)`` 是常态写法，JSON 给的是
    数字（``{"uin": 10001}``）就会 AttributeError；所以非 number/checkbox 字段一律转
    字符串。checkbox 保留布尔，number 保留数字。
    """
    if isinstance(value, (dict, list)):
        return value
    if field_type in _NATIVE_TYPE_FIELDS:
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value


def is_secret_field(key: str, schema_fields: list[dict] | None = None) -> bool:
    """账号字段是否属于凭据（该遮蔽展示）：命中密钥槽位，或 schema 标了 ``secret``。"""
    if _slot_for_field_key(key) in _SECRET_SLOTS:
        return True
    for field in schema_fields or []:
        if str((field or {}).get("key") or "").strip() == str(key).strip() and (field or {}).get("secret"):
            return True
    return False


def map_account_entry(entry: dict, schema_fields: list[dict] | None = None) -> tuple[dict, dict]:
    """把**一条**账号 JSON 条目映射成账号字段 → ``(fields, extras)``。

    JSON 批量导入用：用户手里的导出文件是 ``[{...}, {...}]``，键名可能是上游原始叫法
    （``access_token`` / ``accessToken`` / ``token`` / ``user_name``），也可能是本系统
    账号字段名（``password`` / ``user_id`` / ``enterprise_id``）。两种都要落到渠道
    ``account_schema()["fields"]`` 声明的 key 上——否则运行时按字段名取值会取空。

    - 键名按 ``_normalize_import_key`` 归一后匹配（camelCase / 大小写 / 分隔符差异都吃下），
      schema 字段的 key 与 label 都是匹配入口；
    - 命中槽位的键走 ``_map_slots_to_schema``（凭据字段单写，同槽位只填第一个字段）；
    - schema 声明但没有槽位的字段（user_id / domain / uin …）按同名键直取；
    - 值按字段类型校正（见 ``_coerce_field_value``）：number/checkbox 留原值，其余转字符串
      ——spec 里 ``p.uin.strip()`` / ``p.user_id.startswith(...)`` 是常态，JSON 给数字会炸；
    - ``username`` 总会被填上：优先 username 槽位，其次条目里的同名键，再次
      email / account_id 槽位兜底（与 admin 授权回填的兜底链一致）；
    - 其余键进 extras（调用方决定怎么处理；admin 的 JSON 导入预览层会合并回账号行，
      与单账号凭据导入把 extras 合并回表单同一口径）。

    标量之外的键值（嵌套 dict / list）不参与槽位映射，直接进 extras——凭据字段填了个
    对象是用户的数据问题，静默丢掉会让人以为导入了，预览里得能看见。
    """
    schema_fields = schema_fields or []
    # 归一键名 → schema 字段（key 和 label 都进表，key 优先）
    field_by_norm: dict[str, dict] = {}
    for field in schema_fields:
        key = str((field or {}).get("key") or "").strip()
        if not key:
            continue
        field_by_norm.setdefault(_normalize_import_key(key), field)
        label = str((field or {}).get("label") or "").strip()
        if label:
            field_by_norm.setdefault(_normalize_import_key(label), field)

    fields: dict[str, any] = {}
    extras: dict[str, any] = {}
    slots: dict[str, any] = {}
    used_item_keys: set = set()

    # 第一遍：schema 字段命中（key/label 归一匹配）。槽位字段记槽位、非槽位直取。
    for raw_key, value in (entry or {}).items():
        key = str(raw_key or "").strip()
        if not key or value is None:
            continue
        field = field_by_norm.get(_normalize_import_key(key))
        if field is None:
            continue
        field_key = str(field.get("key")).strip()
        slot = _slot_for_field_key(field_key)
        if slot:
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                if str(value).strip():
                    slots.setdefault(slot, _strip_bearer(str(value).strip()))
                    used_item_keys.add(raw_key)
                # 空串/0 的凭据值：跳过，别污染 extras（password:'' 会被当成额外字段）
            elif isinstance(value, (dict, list)):
                extras[field_key] = value
                used_item_keys.add(raw_key)
            # bool 凭据值：罕见且无意义，跳过
        elif field_key not in fields:
            fields[field_key] = _coerce_field_value(value, str(field.get("type") or "text"))
            used_item_keys.add(raw_key)

    # 第二遍：剩下的条目键按「上游原始键名 → 槽位 → schema 字段」兜底
    #（用户贴 {"access_token": "..."} 而 schema 字段叫 password 的情况）。
    for raw_key, value in (entry or {}).items():
        if raw_key in used_item_keys:
            continue
        key = str(raw_key or "").strip()
        if not key or value is None:
            continue
        slot = _slot_for_field_key(_normalize_import_key(key))
        if slot:
            if isinstance(value, (str, int, float)) and str(value).strip():
                slots.setdefault(slot, _strip_bearer(str(value).strip()))
            else:
                extras[key] = value
        else:
            extras[key] = value

    mapped, unmapped = _map_slots_to_schema(slots, schema_fields)
    for field_key, value in mapped.items():
        if field_key in fields:
            continue
        field = next((f for f in schema_fields if str((f or {}).get("key") or "").strip() == field_key), None)
        fields[field_key] = _coerce_field_value(value, str((field or {}).get("type") or "text"))
    for slot, value in unmapped.items():
        extras.setdefault(slot, value)

    if not str(fields.get("username") or "").strip():
        # 三个来源按序兜底：映射字段 → 条目原文 → 槽位（username 槽位第一，
        # email/account_id 随后——User_Name 这类归一键只进槽位，不进 fields/entry）。
        for candidate in ("username", "user_name", "user_email", "account_id", "user_id", "name"):
            text = str(fields.get(candidate) or entry.get(candidate) or slots.get(candidate) or "").strip()
            if text:
                fields["username"] = text
                break
        else:
            for slot in _USERNAME_FALLBACK_SLOTS:
                if slots.get(slot):
                    fields["username"] = slots[slot]
                    break
    # username 已落到账号主键字段上，别再以槽位名重复出现在 extras（预览会显示两遍）。
    extras.pop("username", None)

    return fields, extras



def parse_credentials(content_bytes: bytes, fmt: str, schema_fields: list[dict] | None = None,
                      base_url: str = "") -> dict:
    """解析凭据文件 → {"format", "fields", "extras", "notes"}。"""
    try:
        text = content_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("文件不是 UTF-8 文本，无法解析") from exc

    notes: list[str] = []
    slots: dict[str, any] = {}

    if fmt == "json":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"无效的 JSON 文件：{exc}") from exc
        slots, json_notes = _collect_json_slots(data)
        notes.extend(json_notes)
        if not slots:
            raise ValueError("JSON 里没找到可识别的凭据字段（token / api_key / cookie / 账号信息）")
    elif fmt == "har":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"无效的 HAR 文件：{exc}") from exc
        slots, har_notes = _collect_har_slots(data, base_url)
        notes.extend(har_notes)
        if not slots:
            raise ValueError("HAR 里没找到 Cookie / Authorization 请求头")
    elif fmt == "cookies":
        slots, cookie_notes = _collect_cookies_txt_slots(text)
        notes.extend(cookie_notes)
        if not slots:
            raise ValueError("cookies.txt 里没找到有效的 cookie 行")
    else:
        raise ValueError("无法识别文件格式，支持 .json / .har / cookies.txt")

    fields, extras = _map_slots_to_schema(slots, schema_fields or [])

    return {"format": fmt, "fields": fields, "extras": extras, "notes": notes}
