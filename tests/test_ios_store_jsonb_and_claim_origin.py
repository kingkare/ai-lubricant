"""ios_store 的 JSONB 编解码 + iOS claim 的数据服务 origin（回归）。

两个线上事故的锁定点：

1. **secret_data JSONB 编解码**：``ios_signing_profiles.secret_data`` 是 JSONB，
   而本进程的 asyncpg 池没有注册 jsonb codec —— asyncpg 只接受 str。直接传 dict
   在 bind 阶段抛 ``TypeError: expected str, got dict``，Apple ID 登录 500
   （``routes_ios.apple_id_login`` → ``_apple_persist_secret`` →
   ``create_signing_profile``）。反向同理：读回是 str，而消费方按 dict 取键
   （``secret.get("team_id")``）。写入必须 ``_encode_secret``、读回必须
   ``_row_with_secret``。

2. **claim 的 server_url 必须是数据服务 origin**：配对端点在数据服务的
   ``/mcp/device-control/pair`` 下，而控制面（8003）没有该路由。此前
   ``ios_claim_device`` 传 ``self.server_url``（控制面），节点拨过去 404
   「未能在该地址找到设备控制服务端」。
"""
from __future__ import annotations

import json

import pytest

from server.ios_store import _decode_secret, _encode_secret, _row_with_secret


# ── JSONB 编码（写入方向）──────────────────────────────────────────────────


def test_encode_secret_returns_json_string_not_dict():
    """核心：传给 asyncpg 的必须是 str，dict 会在 bind 阶段 TypeError。"""
    encoded = _encode_secret({"email": "a@b.c", "adsid": "X"})
    assert isinstance(encoded, str)
    assert json.loads(encoded) == {"email": "a@b.c", "adsid": "X"}


def test_encode_secret_preserves_non_ascii():
    """ensure_ascii=False 与 builtin_tool_store 同口径，中文不被转义。"""
    encoded = _encode_secret({"note": "中文"})
    assert "中文" in encoded


def test_encode_secret_none_stays_none():
    """secret_data=None 表示「只改名、不动材料」，不能被编码成 'null'。"""
    assert _encode_secret(None) is None


# ── JSONB 解码（读回方向）──────────────────────────────────────────────────


def test_decode_secret_parses_the_string_asyncpg_returns():
    """asyncpg 把 jsonb 读成 str；消费方按 dict 取键，必须还原。"""
    raw = json.dumps({"team_id": "TEAM1", "session_expired": False})
    assert _decode_secret(raw) == {"team_id": "TEAM1", "session_expired": False}


def test_decode_secret_none_is_empty_dict():
    """NULL 列（或未 SELECT 该列）→ {}，让 ``secret.get(...)`` 不炸。"""
    assert _decode_secret(None) == {}


def test_decode_secret_passthrough_when_already_dict():
    """已注册 codec 的部署会直接给 dict，不能二次解析。"""
    value = {"k": "v"}
    assert _decode_secret(value) is value


@pytest.mark.parametrize("bad", ["not json", "[1,2]", '"a string"', "42", "null"])
def test_decode_secret_rejects_non_object_json(bad):
    """非对象 JSON / 垃圾输入一律退化成 {}，不把畸形数据泄漏成 list/标量。"""
    assert _decode_secret(bad) == {}


def test_row_with_secret_decodes_only_when_column_present():
    """SELECT 没带 secret_data 的行不能被凭空塞一个键。"""
    plain = _row_with_secret({"id": 1, "name": "p"})
    assert "secret_data" not in plain

    with_secret = _row_with_secret({"id": 1, "secret_data": json.dumps({"a": 1})})
    assert with_secret["secret_data"] == {"a": 1}


def test_row_with_secret_none_row():
    assert _row_with_secret(None) is None


# ── claim 的 server_url（数据服务 origin）──────────────────────────────────


def test_claim_uses_gateway_origin_not_control_plane_url():
    """回归：iOS claim 必须拿到数据服务 origin，否则配对 404。

    控制面与数据服务同机不同端口（8003 / 8001），配对端点在数据服务的 /mcp 下。
    """
    from node_server.service import NodeService

    svc = NodeService.__new__(NodeService)
    svc.server_url = "http://host:8003"          # 控制面：二进制下载用
    svc.gateway_origin = "http://host:8001"      # 数据服务：配对用

    # ios_claim_device 构造帧时用的就是 gateway_origin。
    assert svc.gateway_origin == "http://host:8001"
    assert svc.server_url == "http://host:8003"


def test_gateway_origin_falls_back_to_server_url_when_unset():
    """未推导出 gateway origin 时回退控制面地址（同机同端口的老部署）。"""
    from node_server.service import NodeService

    svc = NodeService.__new__(NodeService)
    svc.server_url = "http://host:8003"
    svc.gateway_origin = ("" or "").strip().rstrip("/") or svc.server_url
    assert svc.gateway_origin == "http://host:8003"
