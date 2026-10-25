"""凭据文件导入识别（providers/credential_import.py）单元测试。

覆盖三种格式（JSON / HAR / cookies.txt）的解析、格式探测、schema 字段映射与错误路径，
以及管理端 ``/accounts/import-file`` 端点的胶水层（大小护栏、格式探测、spec 钩子覆盖）。
"""
import asyncio
import io
import json

import pytest
from fastapi import HTTPException, UploadFile

import admin
from providers import credential_import as ci


# ---- 格式探测 ----

def test_detect_format_by_extension():
    assert ci.detect_format("creds.json", b"{}") == "json"
    assert ci.detect_format("dump.har", b"{}") == "har"
    assert ci.detect_format("my-cookies.txt", b"x") == "cookies"


def test_detect_format_har_by_content():
    """无扩展名时，log.entries 形态识别为 har。"""
    har = json.dumps({"log": {"entries": []}}).encode()
    assert ci.detect_format("dump.txt", har) == "har"


def test_detect_format_unknown():
    assert ci.detect_format("notes.md", b"hello world") == "unknown"
    assert ci.detect_format("a.bin", b"\xff\xfe\x00") == "unknown"


# ---- 凭据 JSON ----

def test_parse_json_flat_credentials():
    content = json.dumps({
        "access_token": "sk-abc",
        "refresh_token": "rt-1",
        "user_name": "alice",
        "account_id": "acct-9",
    }).encode()
    result = ci.parse_credentials(content, "json")
    assert result["format"] == "json"
    # 按 schema_fields 映射（有 schema 时）；无 schema 时全部进 extras
    assert result["extras"]["access_token"] == "sk-abc"
    assert result["extras"]["refresh_token"] == "rt-1"
    assert result["extras"]["username"] == "alice"
    assert result["extras"]["account_id"] == "acct-9"


def test_parse_json_nested_and_bearer_stripped():
    content = json.dumps({
        "data": {"auth": {"authorization": "Bearer eyJhbGci"}},
    }).encode()
    result = ci.parse_credentials(content, "json")
    assert result["extras"]["access_token"] == "eyJhbGci"


def test_parse_json_accounts_wrapper():
    content = json.dumps({"accounts": [{"token": "t-1", "user_email": "a@b.com"}]}).encode()
    result = ci.parse_credentials(content, "json")
    assert result["extras"]["access_token"] == "t-1"
    assert result["extras"]["email"] == "a@b.com"
    assert any("1 个账号" in n for n in result["notes"])


def test_parse_json_maps_to_schema_fields():
    content = json.dumps({"access_token": "sk-abc", "refresh_token": "rt-1"}).encode()
    schema_fields = [
        {"key": "password", "label": "API Key"},
        {"key": "refresh_token", "label": "Refresh Token"},
    ]
    result = ci.parse_credentials(content, "json", schema_fields)
    assert result["fields"] == {"password": "sk-abc", "refresh_token": "rt-1"}
    assert result["extras"] == {}


def test_parse_json_no_credentials_raises():
    content = json.dumps({"hello": "world", "n": 1}).encode()
    with pytest.raises(ValueError, match="没找到可识别的凭据字段"):
        ci.parse_credentials(content, "json")


def test_parse_json_invalid_raises():
    with pytest.raises(ValueError, match="无效的 JSON"):
        ci.parse_credentials(b"{not json", "json")


# ---- HAR ----

def _har(entries: list[dict]) -> bytes:
    return json.dumps({"log": {"version": "1.2", "entries": entries}}).encode()


def test_parse_har_extracts_auth_and_cookie():
    content = _har([{
        "request": {
            "url": "https://api.example.com/v1/chat",
            "headers": [
                {"name": "Authorization", "value": "Bearer sk-har"},
                {"name": "Cookie", "value": "sid=abc; token=xyz"},
            ],
        },
    }])
    result = ci.parse_credentials(content, "har")
    assert result["extras"]["access_token"] == "sk-har"
    assert result["extras"]["cookie"] == "sid=abc; token=xyz"


def test_parse_har_prefers_latest_entry():
    content = _har([
        {"request": {"url": "https://api.example.com/1", "headers": [
            {"name": "Authorization", "value": "Bearer old"}]}},
        {"request": {"url": "https://api.example.com/2", "headers": [
            {"name": "Authorization", "value": "Bearer new"}]}},
    ])
    result = ci.parse_credentials(content, "har")
    assert result["extras"]["access_token"] == "new"


def test_parse_har_filters_by_base_url_host():
    content = _har([
        {"request": {"url": "https://other.com/x", "headers": [
            {"name": "Authorization", "value": "Bearer wrong"}]}},
        {"request": {"url": "https://api.example.com/x", "headers": [
            {"name": "Authorization", "value": "Bearer right"}]}},
    ])
    result = ci.parse_credentials(content, "har", base_url="https://api.example.com")
    assert result["extras"]["access_token"] == "right"


def test_parse_har_no_credentials_raises():
    content = _har([{"request": {"url": "https://a.com", "headers": [
        {"name": "Accept", "value": "*/*"}]}}])
    with pytest.raises(ValueError, match="没找到 Cookie / Authorization"):
        ci.parse_credentials(content, "har")


# ---- cookies.txt ----

def test_parse_cookies_txt_netscape():
    content = (
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t1700000000\tsid\tabc123\n"
        ".example.com\tTRUE\t/\tFALSE\t1700000000\ttoken\txyz789\n"
    ).encode()
    result = ci.parse_credentials(content, "cookies")
    assert result["format"] == "cookies"
    assert result["extras"]["cookie"] == "sid=abc123; token=xyz789"
    assert any("2 条 cookie" in n for n in result["notes"])


def test_parse_cookies_txt_httponly_prefix():
    content = (
        "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t1700000000\tsession_id\ts-1\n"
        ".example.com\tTRUE\t/\tFALSE\t1700000000\tx\t1\n"
    ).encode()
    result = ci.parse_credentials(content, "cookies")
    # #HttpOnly_ 前缀是真实 cookie 行（不是注释）
    assert result["extras"]["cookie"] == "session_id=s-1; x=1"
    assert any("2 条 cookie" in n for n in result["notes"])


def test_parse_cookies_txt_maps_to_schema_cookie_field():
    content = ".example.com\tTRUE\t/\tTRUE\t1700000000\tsid\tabc\n".encode()
    schema_fields = [{"key": "cookies", "label": "Cookie"}]
    result = ci.parse_credentials(content, "cookies", schema_fields)
    assert result["fields"] == {"cookies": "sid=abc"}


def test_parse_cookies_txt_empty_raises():
    with pytest.raises(ValueError, match="没找到有效的 cookie 行"):
        ci.parse_credentials(b"# nothing here\n", "cookies")


# ---- 通用 ----

def test_unknown_format_raises():
    with pytest.raises(ValueError, match="无法识别文件格式"):
        ci.parse_credentials(b"hello", "nope")


def test_non_utf8_raises():
    with pytest.raises(ValueError, match="不是 UTF-8"):
        ci.parse_credentials(b"\xff\xfe\x00\x01", "json")


def test_accepted_keys_cover_common_variants():
    """schema 里 password/api_key/key/access_token 都能接住 access_token 槽位。"""
    content = json.dumps({"token": "t-1"}).encode()
    for key in ("password", "api_key", "key", "access_token"):
        result = ci.parse_credentials(content, "json", [{"key": key}])
        assert result["fields"] == {key: "t-1"}, key


# ---- 管理端 /accounts/import-file 端点 ----

def _upload(content: bytes, filename: str = "creds.json") -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=filename)


async def _allow_admin(*_a, **_k):
    return "admin-token-abcdef0123456789"


def _install_import_deps(monkeypatch, *, schema_fields=None, provider_class=None):
    """装好 import-file 端点的桩：admin 鉴权、配置、schema、provider 类/实例。"""
    monkeypatch.setattr(admin, "_require_admin", _allow_admin)

    async def read_config(_name):
        return {"base_url": "https://api.example.com"}

    cls = provider_class or type("P", (), {})
    monkeypatch.setattr(admin, "_read_provider_config", read_config)
    monkeypatch.setattr(admin, "_provider_account_schema",
                        lambda *_a, **_k: {"fields": schema_fields or []})
    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: cls)
    monkeypatch.setattr(admin, "_build_auth_runtime_instance", lambda *_a, **_k: cls())

    async def log_op(*_a, **_k):
        return None

    monkeypatch.setattr(admin, "_log_operation", log_op)


def test_import_file_json_returns_mapped_fields(monkeypatch):
    content = json.dumps({"access_token": "sk-abc", "refresh_token": "rt-1"}).encode()
    schema = [{"key": "password", "label": "API Key"}, {"key": "refresh_token", "label": "Refresh"}]
    _install_import_deps(monkeypatch, schema_fields=schema)

    result = asyncio.run(admin.import_provider_account_credentials("demo", _upload(content), "Bearer t"))
    assert result["ok"] is True
    assert result["format"] == "json"
    assert result["fields"] == {"password": "sk-abc", "refresh_token": "rt-1"}
    assert result["extras"] == {}


def test_import_file_unknown_format_400(monkeypatch):
    _install_import_deps(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.import_provider_account_credentials(
            "demo", _upload(b"hello world", "notes.md"), "Bearer t"))
    assert exc.value.status_code == 400
    assert "无法识别" in str(exc.value.detail)


def test_import_file_empty_400(monkeypatch):
    _install_import_deps(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.import_provider_account_credentials(
            "demo", _upload(b"", "creds.json"), "Bearer t"))
    assert exc.value.status_code == 400
    assert "为空" in str(exc.value.detail)


def test_import_file_oversized_400(monkeypatch):
    _install_import_deps(monkeypatch)
    monkeypatch.setattr(admin, "ACCOUNT_IMPORT_MAX_BYTES", 16)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.import_provider_account_credentials(
            "demo", _upload(b"x" * 100, "creds.json"), "Bearer t"))
    assert exc.value.status_code == 400
    assert "过大" in str(exc.value.detail)


def test_import_file_custom_hook_overrides_generic_parser(monkeypatch):
    """渠道声明 parse_credential_file 时优先于通用解析器。"""

    class Provider:
        async def parse_credential_file(self, filename, content_bytes, cfg=None):
            return {"fields": {"password": "from-hook"}, "extras": {"raw": "x"},
                    "notes": ["由渠道自定义解析器识别"]}

    _install_import_deps(monkeypatch, provider_class=Provider)

    result = asyncio.run(admin.import_provider_account_credentials(
        "demo", _upload(b"nonstandard", "creds.dat"), "Bearer t"))
    assert result["format"] == "custom"
    assert result["fields"] == {"password": "from-hook"}
    assert result["extras"] == {"raw": "x"}


def test_import_file_custom_hook_failure_falls_back(monkeypatch):
    """钩子抛错 → 回落通用解析器（非标格式渠道自己炸不影响标格式用户）。"""

    class Provider:
        async def parse_credential_file(self, filename, content_bytes, cfg=None):
            raise RuntimeError("解析炸了")

    schema = [{"key": "password"}]
    _install_import_deps(monkeypatch, schema_fields=schema, provider_class=Provider)

    content = json.dumps({"access_token": "sk-abc"}).encode()
    result = asyncio.run(admin.import_provider_account_credentials(
        "demo", _upload(content, "creds.json"), "Bearer t"))
    assert result["format"] == "json"
    assert result["fields"] == {"password": "sk-abc"}


# ---- JSON 批量导入：单条映射（map_account_entry）----

_CB_SCHEMA = [
    {"key": "password", "label": "Access Token", "secret": True},
    {"key": "user_id", "label": "User ID"},
    {"key": "enterprise_id", "label": "Enterprise ID", "required": True},
    {"key": "refresh_token", "label": "Refresh Token", "secret": True},
]


def test_map_account_entry_upstream_raw_keys():
    """上游原始键名（access_token / user_name）落到 schema 声明的字段上。"""
    fields, extras = ci.map_account_entry(
        {"access_token": "sk-abc", "user_id": "u-1", "user_name": "alice", "region": "cn"},
        _CB_SCHEMA)
    assert fields["password"] == "sk-abc"
    assert fields["user_id"] == "u-1"
    assert fields["username"] == "alice"
    assert extras == {"region": "cn"}


def test_map_account_entry_direct_field_keys():
    fields, extras = ci.map_account_entry(
        {"username": "bob", "password": "sk-yyy", "enterprise_id": "ent-9"}, _CB_SCHEMA)
    assert fields == {"password": "sk-yyy", "enterprise_id": "ent-9", "username": "bob"}
    assert extras == {}


def test_map_account_entry_strips_bearer_and_skips_empty():
    fields, _ = ci.map_account_entry(
        {"username": "c", "authorization": "Bearer sk-zzz", "password": "", "user_id": None},
        _CB_SCHEMA)
    assert fields["password"] == "sk-zzz"
    assert "user_id" not in fields


def test_map_account_entry_nested_values_go_to_extras():
    fields, extras = ci.map_account_entry({"username": "y", "extra_info": {"a": 1}}, _CB_SCHEMA)
    assert extras == {"extra_info": {"a": 1}}


def test_map_account_entry_no_username_no_crash():
    fields, _ = ci.map_account_entry({"access_token": "t"}, _CB_SCHEMA)
    assert "username" not in fields


def test_is_secret_field():
    assert ci.is_secret_field("password", _CB_SCHEMA)
    assert ci.is_secret_field("refresh_token", _CB_SCHEMA)
    assert ci.is_secret_field("access_token", [])       # 槽位命中即算凭据
    assert not ci.is_secret_field("user_id", _CB_SCHEMA)


# ---- 键名归一 / 值类型校正 ----

def test_map_account_entry_camel_case_keys():
    """camelCase 上游键名归一后命中 schema 的 snake_case 字段。"""
    fields, extras = ci.map_account_entry(
        {"userName": "alice", "accessToken": "sk-abc", "userId": "u1", "enterpriseId": "e1"},
        _CB_SCHEMA)
    assert fields["username"] == "alice"
    assert fields["password"] == "sk-abc"
    assert fields["user_id"] == "u1"
    assert fields["enterprise_id"] == "e1"
    assert extras == {}


def test_map_account_entry_case_and_separator_insensitive():
    fields, _ = ci.map_account_entry(
        {"User_Name": "alice", "ACCESS-TOKEN": "sk-abc", "Enterprise_Id": "e1"}, _CB_SCHEMA)
    assert fields["username"] == "alice"
    assert fields["password"] == "sk-abc"
    assert fields["enterprise_id"] == "e1"


def test_map_account_entry_matches_by_label():
    """schema 字段 label 也能当导入键名（用户按界面字段名导出）。"""
    schema = [{"key": "uin", "label": "UIN", "type": "text"}]
    fields, _ = ci.map_account_entry({"UIN": "10001", "user_name": "a"}, schema)
    assert fields["uin"] == "10001"


def test_map_account_entry_numeric_value_becomes_string():
    """数字/布尔按字段类型校正：number/checkbox 留原值，其余转字符串。

    spec 里 ``p.uin.strip()`` / ``p.user_id.startswith(...)`` 是常态，JSON 给数字会炸。
    """
    schema = [
        {"key": "uin", "label": "UIN", "type": "text"},
        {"key": "user_id", "label": "User ID", "type": "text"},
        {"key": "weight", "label": "权重", "type": "number"},
        {"key": "switch", "label": "启用", "type": "checkbox"},
    ]
    fields, _ = ci.map_account_entry(
        {"username": "a", "uin": 10001, "user_id": 12345, "weight": 3, "switch": True}, schema)
    assert fields["uin"] == "10001" and isinstance(fields["uin"], str)
    assert fields["user_id"] == "12345"
    assert fields["weight"] == 3 and isinstance(fields["weight"], int)
    assert fields["switch"] is True


def test_map_account_entry_secret_number_stays_string():
    schema = [{"key": "password", "label": "Token", "type": "password"}]
    fields, _ = ci.map_account_entry({"username": "a", "password": 987654321}, schema)
    assert fields["password"] == "987654321" and isinstance(fields["password"], str)


def test_map_account_entry_upstream_key_over_label_prefers_password_field():
    """access_token 命中 password 字段（schema 声明哪个就写哪个），不会留在 extras。"""
    fields, extras = ci.map_account_entry(
        {"user_name": "a", "access_token": "sk-1"}, _CB_SCHEMA)
    assert fields["password"] == "sk-1"
    assert "access_token" not in extras


# ---- JSON 批量导入：管理端 /accounts/import-json 端点 ----

def _install_json_import_deps(monkeypatch, *, schema_fields=None, accounts=None):
    """装好 import-json 端点的桩：admin 鉴权、配置、schema、审计。"""
    monkeypatch.setattr(admin, "_require_admin", _allow_admin)
    cfg = {"accounts": accounts or [], "base_url": "https://api.example.com"}

    async def read_config(_name):
        return cfg

    monkeypatch.setattr(admin, "_read_provider_config", read_config)
    monkeypatch.setattr(admin, "_provider_account_schema",
                        lambda *_a, **_k: {"fields": schema_fields if schema_fields is not None else _CB_SCHEMA})

    async def log_op(*_a, **_k):
        return None

    monkeypatch.setattr(admin, "_log_operation", log_op)
    return cfg


def test_import_json_preview_maps_and_masks(monkeypatch):
    _install_json_import_deps(monkeypatch)
    payload = {"text": json.dumps([
        {"username": "alice", "access_token": "sk-secretvalue", "user_id": "1", "enterprise_id": "e1"},
        {"username": "bob", "password": "sk-othersecret", "user_id": "2", "enterprise_id": "e2"},
    ])}

    result = asyncio.run(admin.import_provider_accounts_json("demo", payload, "Bearer t"))
    assert result["ok"] is True
    assert result["total"] == 2 and result["importable"] == 2 and result["failed"] == 0
    # 预览凭据脱敏（不落库的预览也不给全值）
    assert result["rows"][0]["fields"]["password"] == "sk-s...alue"
    assert result["rows"][1]["fields"]["password"] == "sk-o...cret"
    # accounts 才是完整可落库行：上游键已归一到 schema 字段
    assert result["accounts"][0] == {
        "password": "sk-secretvalue", "user_id": "1", "enterprise_id": "e1",
        "username": "alice", "switch": True,
    }


def test_import_json_wrapper_object(monkeypatch):
    _install_json_import_deps(monkeypatch)
    payload = {"text": json.dumps({"accounts": [{"username": "a", "password": "p", "enterprise_id": "e"}]})}
    result = asyncio.run(admin.import_provider_accounts_json("demo", payload, "Bearer t"))
    assert result["importable"] == 1


def test_import_json_collects_unimportable_rows(monkeypatch):
    """缺必填 / 与已有重复 / 批内重复 / 缺用户名 → 拦下并说明原因，好行仍可导入。"""
    _install_json_import_deps(monkeypatch, accounts=[{"username": "dup"}])
    payload = {"text": json.dumps([
        {"username": "ok", "password": "p", "enterprise_id": "e"},
        {"username": "miss-req", "password": "p"},                    # 缺必填 enterprise_id
        {"username": "dup", "password": "p", "enterprise_id": "e"},   # 与已有账号重复
        {"username": "ok", "password": "p", "enterprise_id": "e"},    # 批内重复
        {"password": "p", "enterprise_id": "e"},                       # 缺用户名
        "not-a-dict",                                                  # 条目不是对象
    ])}

    result = asyncio.run(admin.import_provider_accounts_json("demo", payload, "Bearer t"))
    assert result["total"] == 6 and result["importable"] == 1 and result["failed"] == 5
    assert [r["username"] for r in result["rows"]] == ["ok", "miss-req", "dup", "ok", "", ""]
    assert result["rows"][1]["errors"] == ["缺少必填字段 Enterprise ID"]
    assert result["rows"][2]["errors"] == ["用户名 'dup' 已存在"]
    assert result["rows"][3]["errors"] == ["用户名 'ok' 重复"]
    assert result["rows"][4]["errors"] == ["缺少用户名"]
    assert result["rows"][5]["errors"] == ["条目必须是对象"]
    # 错误行不进 accounts（确认导入只提交好行）
    assert [a["username"] for a in result["accounts"]] == ["ok"]


def test_import_json_invalid_json_400(monkeypatch):
    _install_json_import_deps(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.import_provider_accounts_json("demo", {"text": "{not json"}, "Bearer t"))
    assert exc.value.status_code == 400
    assert "无效的 JSON" in str(exc.value.detail)


def test_import_json_empty_text_400(monkeypatch):
    _install_json_import_deps(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.import_provider_accounts_json("demo", {"text": "  "}, "Bearer t"))
    assert exc.value.status_code == 400
    assert "粘贴" in str(exc.value.detail)


def test_import_json_no_account_array_400(monkeypatch):
    _install_json_import_deps(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.import_provider_accounts_json(
            "demo", {"text": json.dumps({"foo": "bar"})}, "Bearer t"))
    assert exc.value.status_code == 400
    assert "账号数组" in str(exc.value.detail)


def test_import_json_empty_array_400(monkeypatch):
    _install_json_import_deps(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.import_provider_accounts_json("demo", {"text": "[]"}, "Bearer t"))
    assert exc.value.status_code == 400
    assert "没有账号条目" in str(exc.value.detail)


def test_import_json_oversized_400(monkeypatch):
    _install_json_import_deps(monkeypatch)
    monkeypatch.setattr(admin, "ACCOUNT_JSON_IMPORT_MAX_BYTES", 16)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.import_provider_accounts_json(
            "demo", {"text": json.dumps([{"username": "x" * 100}])}, "Bearer t"))
    assert exc.value.status_code == 400
    assert "过大" in str(exc.value.detail)


def test_import_json_does_not_persist(monkeypatch):
    """预览端点零写入：落库由确认后的 batch-add 负责。"""
    _install_json_import_deps(monkeypatch)

    def boom(*_a, **_k):
        raise AssertionError("import-json 不该写库")

    monkeypatch.setattr(admin, "_persist_account", boom)
    result = asyncio.run(admin.import_provider_accounts_json(
        "demo", {"text": json.dumps([{"username": "a", "password": "p", "enterprise_id": "e"}])}, "Bearer t"))
    assert result["importable"] == 1
