"""New API 适配器的纯映射/解析测试。

这些测试钉死三条「朴素实现会出错」的规则（见 channel_importers/adapters/newapi.py
模块 docstring）：priority 极性、model_mapping 方向、model_rows 必须是 dict。
无 DB、无网络。
"""
from __future__ import annotations

import json

import pytest

from channel_importers import FetchRequest, PasteRequest, get_importer, list_importers
from channel_importers.adapters import newapi as newapi_mod
from channel_importers.adapters.newapi import NewApiImporter, _channel_from_raw
from channel_importers.adapters.newapi_types import CHANNEL_TYPES, resolve_type
from channel_importers.errors import FetchError, ParseError, UnknownImporterError
from channel_importers.payload import build_create_payload, local_provider_name, scrub_meta


def _raw(**overrides) -> dict:
    base = {
        "id": 1,
        "type": 1,
        "name": "OpenAI",
        "status": 1,
        "base_url": "https://api.openai.com",
        "models": "gpt-4o",
    }
    base.update(overrides)
    return base


def _scan_one(**overrides):
    """走 parse 全链路（含 base_url 过滤），返回单个候选。"""
    importer = NewApiImporter()
    scan = importer.parse(PasteRequest(text=json.dumps([_raw(**overrides)])))
    return scan


# ── 注册表 ────────────────────────────────────────────────────────────────────

def test_new_api_is_registered():
    assert "new-api" in [imp.id for imp in list_importers()]
    assert get_importer("new-api").display_name


def test_unknown_importer_raises():
    with pytest.raises(UnknownImporterError):
        get_importer("nope")


def test_list_importers_is_stably_sorted():
    assert [imp.id for imp in list_importers()] == sorted(imp.id for imp in list_importers())


# ── type → 协议映射 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("channel_type,protocol,path", [
    (1, "openai", "/v1/chat/completions"),
    (4, "openai", "/v1/chat/completions"),
    (8, "openai", "/v1/chat/completions"),
    (14, "anthropic", "/v1/messages"),
    (20, "openai", "/v1/chat/completions"),
    (24, "gemini", ""),  # gemini 路径运行时动态拼，按约定留空
    (43, "openai", "/v1/chat/completions"),
    (48, "openai", "/v1/chat/completions"),
    (57, "responses", "/v1/responses"),
    (60, "openai", "/v1/chat/completions"),
    (62, "openai", "/v1/chat/completions"),
])
def test_channel_type_maps_to_protocol(channel_type, protocol, path):
    channel = _channel_from_raw(_raw(type=channel_type))
    row = channel.chat_protocols[0]
    assert row["protocol"] == protocol
    assert row["path"] == path


def test_gemini_uses_gemini_models_path():
    channel = _channel_from_raw(_raw(type=24))
    assert channel.models_path == "/v1beta/models"


def test_every_mapped_type_has_a_protocol_except_unimportable():
    for channel_type, mapping in CHANNEL_TYPES.items():
        if mapping.importable:
            assert mapping.protocol, f"type {channel_type} 可导入却没有协议"


def test_unknown_channel_type_falls_back_to_openai_with_warning():
    channel = _channel_from_raw(_raw(type=9999))
    assert channel.chat_protocols[0]["protocol"] == "openai"
    assert any("未知的 New API 渠道类型" in w for w in channel.warnings)


def test_non_numeric_type_falls_back_without_crashing():
    assert resolve_type("not-a-number").protocol == "openai"
    assert resolve_type(None).protocol == "openai"


@pytest.mark.parametrize("channel_type", [33, 41])
def test_signed_cloud_types_are_not_importable(channel_type):
    channel = _channel_from_raw(_raw(type=channel_type))
    assert channel.importable is False
    assert channel.errors and "签名鉴权" in channel.errors[0]


def test_risky_path_type_warns_about_protocol_path():
    channel = _channel_from_raw(_raw(type=16))  # 智谱
    assert any("核对协议路径" in w for w in channel.warnings)


def test_media_type_is_flagged():
    channel = _channel_from_raw(_raw(type=2))  # Midjourney
    assert any("媒体生成" in w for w in channel.warnings)


# ── models / model_mapping ────────────────────────────────────────────────────

def test_models_comma_string_splits_and_dedupes():
    channel = _channel_from_raw(_raw(models=" gpt-4o , gpt-4o-mini ,,gpt-4o,"))
    assert channel.models == ["gpt-4o", "gpt-4o-mini"]


def test_models_accepts_list_from_forks():
    channel = _channel_from_raw(_raw(models=["a", "b"]))
    assert channel.models == ["a", "b"]


def test_model_mapping_becomes_provider_model_rows():
    """方向钉死：New API {客户端要的 A: 上游拿的 B} → upstream=B, model=A。

    反过来写会搞坏模型名——这是本套件里最高价值的断言。
    """
    channel = _channel_from_raw(
        _raw(models="gpt-4o", model_mapping=json.dumps({"gpt-4o": "gpt-4o-2024-11-20"}))
    )
    assert {"upstream_model_id": "gpt-4o-2024-11-20", "model_id": "gpt-4o"} in channel.model_rows


def test_model_mapping_adds_models_not_listed_in_models_field():
    channel = _channel_from_raw(
        _raw(models="gpt-4o", model_mapping=json.dumps({"claude-3": "claude-3-5-sonnet"}))
    )
    assert {"upstream_model_id": "claude-3-5-sonnet", "model_id": "claude-3"} in channel.model_rows


def test_model_mapping_wildcard_emits_search_only_rule_and_warning():
    channel = _channel_from_raw(
        _raw(models="", model_mapping=json.dumps({"gpt-4*": "gpt-4o"}))
    )
    assert channel.model_id_rewrite_rules
    assert channel.model_id_rewrite_rules[0]["search_only"] is True
    assert any("通配符" in w for w in channel.warnings)


def test_broken_model_mapping_json_is_ignored():
    channel = _channel_from_raw(_raw(models="a", model_mapping="{not json"))
    assert channel.model_rows == [{"upstream_model_id": "a", "model_id": "a"}]


def test_model_rows_are_dicts_never_strings():
    """钉死 db._provider_model_payload 的裸字符串拆分 bug。

    裸字符串 "gpt-4o" 会被它按序列处理成 {"upstream_model_id": "g", "model_id": "p"}。
    """
    channel = _channel_from_raw(_raw(models="gpt-4o,gpt-4o-mini"))
    assert channel.model_rows
    for row in channel.model_rows:
        assert isinstance(row, dict)
        assert set(row) == {"upstream_model_id", "model_id"}
        assert len(row["upstream_model_id"]) > 1


# ── 密钥 / 账号 ───────────────────────────────────────────────────────────────

def test_multikey_channel_splits_into_accounts():
    channel = _channel_from_raw(_raw(key="sk-aaa\nsk-bbb\r\nsk-aaa\n"))
    assert [a.username for a in channel.accounts] == ["key-1", "key-2"]
    assert [a.api_key for a in channel.accounts] == ["sk-aaa", "sk-bbb"]
    assert channel.has_keys is True


def test_missing_key_leaves_accounts_empty():
    """列表接口 Omit("key")，所以 fetch 路径这里恒为空。"""
    channel = _channel_from_raw(_raw())
    assert channel.accounts == []
    assert channel.has_keys is False


def test_imported_account_uses_password_not_api_key():
    """CustomProvider 读 api_key > key > password；写 api_key 会复刻陈旧遮蔽 bug。"""
    channel = _channel_from_raw(_raw(key="sk-aaa"))
    payload = build_create_payload(channel, name="openai")
    assert payload["accounts"] == [
        {"username": "key-1", "password": "sk-aaa", "price_remark": "OpenAI", "switch": True}
    ]


def test_no_keys_produces_no_account_rows():
    """空 key 占位账号会进运行池并表现为永久认证失败，所以宁可不写。"""
    payload = build_create_payload(_channel_from_raw(_raw()), name="openai")
    assert payload["accounts"] == []


# ── priority / weight（极性坑） ───────────────────────────────────────────────

def test_priority_is_not_mapped_but_weight_is_clamped():
    """两侧 priority 方向相反（New API 越大越优先，本平台越小越优先），故不映射。

    未来若有人「顺手补上」priority 映射，这条测试会失败——请先读模块 docstring 坑 1。
    """
    channel = _channel_from_raw(_raw(priority=9, weight=3))
    payload = build_create_payload(channel, name="openai")
    assert "account_priority" not in payload
    assert payload["account_weight"] == 3
    assert channel.source_meta["priority_present"] is True


def test_batch_warns_when_priority_present():
    scan = _scan_one(priority=5)
    assert any("优先级" in w for w in scan.warnings)


def test_zero_weight_falls_back_to_one():
    assert _channel_from_raw(_raw(weight=0)).account_weight == 1
    assert _channel_from_raw(_raw(weight="bad")).account_weight == 1


# ── status / enabled ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [(1, True), (0, False)])
def test_status_maps_to_enabled(status, expected):
    assert _channel_from_raw(_raw(status=status)).enabled is expected


@pytest.mark.parametrize("status", [2, 3])
def test_auto_disabled_status_warns(status):
    channel = _channel_from_raw(_raw(status=status))
    assert channel.enabled is False
    assert any("自动禁用" in w for w in channel.warnings)


def test_include_disabled_filter_is_not_applied_in_parse():
    """parse 不按 enabled 过滤（那是 fetch 的 include_disabled 开关）。"""
    scan = _scan_one(status=0)
    assert len(scan.channels) == 1 and scan.channels[0].enabled is False


# ── base_url ─────────────────────────────────────────────────────────────────

def test_base_url_trailing_slash_stripped():
    assert _channel_from_raw(_raw(base_url="https://api.openai.com/")).base_url == "https://api.openai.com"


def test_base_url_trailing_v1_stripped_with_warning():
    channel = _channel_from_raw(_raw(base_url="https://api.openai.com/v1"))
    assert channel.base_url == "https://api.openai.com"
    assert any("/v1" in w for w in channel.warnings)


def test_channel_without_base_url_is_filtered_out():
    """用户决策：空地址渠道直接过滤，不进候选列表。"""
    importer = NewApiImporter()
    scan = importer.parse(PasteRequest(text=json.dumps([
        _raw(id=1, base_url=""),
        _raw(id=2, base_url="https://api.openai.com"),
    ])))
    assert [c.external_id for c in scan.channels] == ["2"]
    assert len(scan.dropped) == 1
    assert "未填渠道地址" in scan.dropped[0]["reason"]
    assert any("已过滤 1 个" in w for w in scan.warnings)


def test_dropped_entries_keep_identity_for_reporting():
    scan = _scan_one(id=42, base_url="")
    assert scan.dropped[0]["source_id"] == "42"
    assert scan.dropped[0]["remark"] == "OpenAI"


# ── 粘贴解析的信封形状 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    json.dumps({"success": True, "data": {"items": [_raw()]}}),
    json.dumps({"data": [_raw()]}),
    json.dumps({"items": [_raw()]}),
    json.dumps([_raw()]),
])
def test_paste_parse_accepts_all_envelope_shapes(text):
    scan = NewApiImporter().parse(PasteRequest(text=text))
    assert len(scan.channels) == 1


def test_paste_parse_accepts_jsonl():
    text = "\n".join(json.dumps(_raw(id=i)) for i in range(3))
    scan = NewApiImporter().parse(PasteRequest(text=text))
    assert len(scan.channels) == 3
    assert scan.channels[0].source_meta["parsed_shape"] == "jsonl"


def test_paste_parse_records_envelope_shape():
    scan = NewApiImporter().parse(
        PasteRequest(text=json.dumps({"success": True, "data": {"items": [_raw()]}}))
    )
    assert scan.channels[0].source_meta["parsed_shape"] == "api-envelope"


def test_empty_paste_raises():
    with pytest.raises(ParseError):
        NewApiImporter().parse(PasteRequest(text="   "))


def test_unrecognized_paste_raises():
    with pytest.raises(ParseError):
        NewApiImporter().parse(PasteRequest(text="hello world"))


def test_json_without_channel_array_raises():
    with pytest.raises(ParseError):
        NewApiImporter().parse(PasteRequest(text=json.dumps({"success": True, "data": {}})))


def test_paste_without_keys_warns():
    scan = NewApiImporter().parse(PasteRequest(text=json.dumps([_raw()])))
    assert any("没有渠道密钥" in w for w in scan.warnings)


# ── 本地 name 生成 ────────────────────────────────────────────────────────────

def test_local_name_slug_is_derived_and_valid():
    channel = _channel_from_raw(_raw(name="OpenAI 主力"))
    name = local_provider_name(channel)
    assert name == "openai"
    assert local_provider_name(channel, {"openai"}) == "openai-2"


def test_cjk_only_name_falls_back_to_source_and_id():
    """_auto_generate_name 对纯中文名会塌成 "ch"——30 个中文渠道就变成 ch..ch-30。"""
    channel = _channel_from_raw(_raw(id=77, name="主力渠道"))
    assert local_provider_name(channel) == "new-api-77"


def test_local_name_dedupes_within_batch():
    channel = _channel_from_raw(_raw(name="OpenAI"))
    assert local_provider_name(channel, {"openai", "openai-2"}) == "openai-3"


# ── 序列化安全 ────────────────────────────────────────────────────────────────

def test_scrub_meta_strips_secret_keys():
    scrubbed = scrub_meta({
        "key": "sk-1", "api_key": "sk-2", "password": "p", "token": "t",
        "authorization": "a", "cookies": "c", "access_token": "at", "user_id": "u",
        "new-api-user": "n", "group": "default", "type": 1,
    })
    assert scrubbed == {"group": "default", "type": 1}


def test_scrub_meta_recurses():
    assert scrub_meta({"outer": {"api_key": "x", "keep": 1}}) == {"outer": {"keep": 1}}


def test_source_meta_never_contains_the_key():
    channel = _channel_from_raw(_raw(key="sk-secret-123"))
    assert "sk-secret-123" not in json.dumps(channel.source_meta, ensure_ascii=False)


# ── fetch 路径（打桩传输） ────────────────────────────────────────────────────

class _StubResponse:
    def __init__(self, status: int, payload, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = json.dumps(payload).encode("utf-8")

    async def read(self) -> bytes:
        return self._body


class _StubProxyManager:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


@pytest.fixture()
def stub_proxy(monkeypatch):
    def install(responses):
        manager = _StubProxyManager(responses)
        monkeypatch.setattr(newapi_mod, "_proxy_manager", lambda: manager)
        return manager
    return install


def _envelope(items, total=None):
    return {"success": True, "message": "", "data": {"items": items, "total": total if total is not None else len(items)}}


@pytest.mark.asyncio
async def test_fetch_returns_channels(stub_proxy):
    stub_proxy([_StubResponse(200, _envelope([_raw()]))])
    scan = await NewApiImporter().fetch(FetchRequest(base_url="https://na.example.com", token="t"))
    assert len(scan.channels) == 1
    assert scan.channels[0].base_url == "https://api.openai.com"


@pytest.mark.asyncio
async def test_fetch_paginates_until_total(stub_proxy):
    page1 = [_raw(id=i) for i in range(2)]
    page2 = [_raw(id=i) for i in range(2, 4)]
    manager = stub_proxy([
        _StubResponse(200, _envelope(page1, total=4)),
        _StubResponse(200, _envelope(page2, total=4)),
    ])
    scan = await NewApiImporter().fetch(
        FetchRequest(base_url="https://na.example.com", token="t", page_size=2)
    )
    assert len(scan.channels) == 4
    assert len(manager.calls) == 2


@pytest.mark.asyncio
async def test_fetch_does_not_follow_redirects(stub_proxy):
    manager = stub_proxy([_StubResponse(200, _envelope([_raw()]))])
    await NewApiImporter().fetch(FetchRequest(base_url="https://na.example.com", token="t"))
    assert manager.calls[0]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_fetch_sends_bearer_token_and_user_header(stub_proxy):
    manager = stub_proxy([_StubResponse(200, _envelope([_raw()]))])
    await NewApiImporter().fetch(
        FetchRequest(base_url="https://na.example.com", token="tok", user_id="7")
    )
    headers = manager.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["New-Api-User"] == "7"


@pytest.mark.asyncio
async def test_fetch_warns_that_keys_are_unavailable(stub_proxy):
    """列表接口 Omit("key")，所以 fetch 路径导入的渠道账号必然为空，必须明确告知。"""
    stub_proxy([_StubResponse(200, _envelope([_raw()]))])
    scan = await NewApiImporter().fetch(FetchRequest(base_url="https://na.example.com", token="t"))
    assert all(not c.has_keys for c in scan.channels)
    assert any("不回传密钥" in w for w in scan.warnings)


@pytest.mark.asyncio
async def test_fetch_rejects_redirect(stub_proxy):
    stub_proxy([_StubResponse(302, {})])
    with pytest.raises(FetchError, match="重定向"):
        await NewApiImporter().fetch(FetchRequest(base_url="https://na.example.com", token="t"))


@pytest.mark.asyncio
async def test_fetch_surfaces_auth_failure(stub_proxy):
    stub_proxy([_StubResponse(401, {})])
    with pytest.raises(FetchError, match="令牌无效"):
        await NewApiImporter().fetch(FetchRequest(base_url="https://na.example.com", token="t"))


@pytest.mark.asyncio
async def test_fetch_surfaces_upstream_error_message(stub_proxy):
    stub_proxy([_StubResponse(200, {"success": False, "message": "无权限"})])
    with pytest.raises(FetchError, match="无权限"):
        await NewApiImporter().fetch(FetchRequest(base_url="https://na.example.com", token="t"))


@pytest.mark.asyncio
async def test_fetch_rejects_non_http_scheme():
    with pytest.raises(FetchError, match="http"):
        await NewApiImporter().fetch(FetchRequest(base_url="ftp://na.example.com", token="t"))


@pytest.mark.asyncio
async def test_fetch_requires_token():
    with pytest.raises(FetchError, match="令牌"):
        await NewApiImporter().fetch(FetchRequest(base_url="https://na.example.com", token=""))


@pytest.mark.asyncio
async def test_fetch_can_exclude_disabled(stub_proxy):
    stub_proxy([_StubResponse(200, _envelope([_raw(id=1, status=1), _raw(id=2, status=0)]))])
    scan = await NewApiImporter().fetch(
        FetchRequest(base_url="https://na.example.com", token="t", include_disabled=False)
    )
    assert [c.external_id for c in scan.channels] == ["1"]
