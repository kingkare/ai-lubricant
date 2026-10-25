"""浏览器插件密码管理器（``browser_passwords`` + ``pwd_*`` 帧）的行为回归。

FakePool/假对象模式，不依赖真实 Postgres —— 遵循 ``tests-need-postgres-baseline``。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import pytest

sys.path.insert(0, "server")

import browser_password_crypto as crypto  # noqa: E402
import browser_password_store as pwd_store  # noqa: E402
from browser_password_crypto import BLOB_VERSION, decrypt_password, encrypt_password  # noqa: E402

TEST_KEY = "unit-test-browser-password-key-0123456789"


@pytest.fixture(autouse=True)
def _ensure_key(monkeypatch):
    """所有用例都在有密钥的环境下跑（真实部署由 main.py 启动期置备）。"""
    monkeypatch.setenv("BROWSER_PASSWORD_KEY", TEST_KEY)


# ---------------------------------------------------------------------------
# Fake pool / conn（仿 tests/test_marketplace_store.py）
# ---------------------------------------------------------------------------
class _FakeConn:
    """按语句关键字路由的假连接：capture SQL 文本 + 参数断言行为。"""

    def __init__(self, state: dict):
        self._state = state
        self.fetchval_override = None

    async def execute(self, sql, *args):
        self._state.setdefault("execute", []).append((sql, args))

    async def fetchrow(self, sql, *args):
        self._state.setdefault("fetchrow", []).append((sql, args))
        return self._state.get("row_result")

    async def fetch(self, sql, *args):
        self._state.setdefault("fetch", []).append((sql, args))
        return self._state.get("fetch_result", [])

    async def fetchval(self, sql, *args):
        self._state.setdefault("fetchval", []).append((sql, args))
        if self.fetchval_override is not None:
            return await self.fetchval_override(sql, *args)
        return self._state.get("val_result")

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)

    async def release(self, conn):
        pass


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def fake_pool(monkeypatch):
    """install(state) → 返回 fake conn（可继续改 fetchval 行为）。"""

    def install(state: dict):
        import db

        conn = _FakeConn(state)
        monkeypatch.setattr(db.PostgresClient, "pool", _FakePool(conn))
        return conn

    return install


# ---------------------------------------------------------------------------
# 加密
# ---------------------------------------------------------------------------
def test_encrypt_decrypt_roundtrip():
    plaintext = "S3cr3t-P@ssw0rd"
    blob = encrypt_password(plaintext)
    assert blob.startswith(BLOB_VERSION + ":")
    assert decrypt_password(blob) == plaintext
    # 明文不出现在密文里
    assert plaintext not in blob


def test_encrypt_nondeterministic_iv():
    blobs = {encrypt_password("same-password") for _ in range(5)}
    assert len(blobs) == 5  # IV 随机 → 每次密文不同


def test_tamper_ciphertext_raises():
    blob = encrypt_password("original")
    version, iv_b64, ct_b64 = blob.split(":")
    flipped = "A" if ct_b64[0] != "A" else "B"
    tampered = f"{version}:{iv_b64}:{flipped}{ct_b64[1:]}"
    with pytest.raises(Exception):
        decrypt_password(tampered)


def test_wrong_key_raises(monkeypatch):
    blob = encrypt_password("temp-key-test")
    monkeypatch.setenv("BROWSER_PASSWORD_KEY", "another-key-entirely-9876543210")
    with pytest.raises(Exception):
        decrypt_password(blob)


def test_unknown_version_raises():
    with pytest.raises(ValueError):
        decrypt_password("v99:aGVsbG8=:dGVzdA==")


def test_malformed_blob_raises():
    with pytest.raises(ValueError):
        decrypt_password("not-a-blob")
    with pytest.raises(ValueError):
        decrypt_password("")


def test_key_derivation_stable_and_domain_separated(monkeypatch):
    """无 BROWSER_PASSWORD_KEY 时从 AGENT_ATTACHMENT_SIGNING_KEY 域分离派生，可复现。"""
    monkeypatch.delenv("BROWSER_PASSWORD_KEY", raising=False)
    monkeypatch.setenv("AGENT_ATTACHMENT_SIGNING_KEY", "attachment-key-abc")
    first = crypto._raw_key()
    second = crypto._raw_key()
    assert first == second and len(first) == 32
    # 与直接用 attachment key 不同（域分离生效）
    import hashlib

    assert first != hashlib.sha256(b"attachment-key-abc").digest()


def test_missing_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("BROWSER_PASSWORD_KEY", raising=False)
    monkeypatch.delenv("AGENT_ATTACHMENT_SIGNING_KEY", raising=False)
    assert crypto._raw_key() == b""
    with pytest.raises(RuntimeError):
        crypto._key()


def test_ensure_password_key_generates_and_persists(monkeypatch):
    monkeypatch.delenv("BROWSER_PASSWORD_KEY", raising=False)
    persisted: dict = {}

    import dotenv_loader

    monkeypatch.setattr(
        dotenv_loader, "update_env_vars",
        lambda mapping, path=None: persisted.update(mapping), raising=False,
    )
    generated = crypto.ensure_password_key()
    assert generated and len(generated) >= 16
    assert os.environ["BROWSER_PASSWORD_KEY"] == generated
    assert persisted.get("BROWSER_PASSWORD_KEY") == generated
    # 已有值时幂等（不重新生成）
    assert crypto.ensure_password_key() == generated


def test_ensure_password_key_survives_persist_failure(monkeypatch):
    monkeypatch.delenv("BROWSER_PASSWORD_KEY", raising=False)

    import dotenv_loader

    def boom(*_a, **_k):
        raise OSError("read-only fs")

    monkeypatch.setattr(dotenv_loader, "update_env_vars", boom, raising=False)
    generated = crypto.ensure_password_key()
    assert os.environ["BROWSER_PASSWORD_KEY"] == generated  # 进程内仍可用


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
def _row(**over):
    row = {"id": 7, "owner_user_id": "u_xxx", "domain": "example.com", "username": "alice",
           "url": "https://example.com/login", "updated_at": None}
    row.update(over)
    return row


def test_upsert_encrypts_password_and_returns_meta(fake_pool):
    state: dict = {"row_result": _row()}
    fake_pool(state)
    meta = asyncio.run(pwd_store.upsert_password(
        "u_xxx", domain="example.com", username="alice", password="S3cr3t-xyz"))
    assert meta["id"] == 7 and meta["username"] == "alice"
    assert "password" not in meta and "password_enc" not in meta
    # 落库参数里是密文，不是明文
    calls = state["fetchrow"]
    sql, args = calls[-1]
    assert "INSERT INTO browser_passwords" in sql
    assert "ON CONFLICT (owner_user_id, domain, username)" in sql
    assert "S3cr3t-xyz" not in sql and "S3cr3t-xyz" not in str(args)
    enc = args[3]
    assert enc.startswith("v1:")
    assert decrypt_password(enc) == "S3cr3t-xyz"


def test_upsert_normalizes_domain(fake_pool):
    state: dict = {"row_result": _row(domain="example.com")}
    fake_pool(state)
    asyncio.run(pwd_store.upsert_password(
        "u_xxx", domain="  Example.COM ", username=" alice ", password="p"))
    _sql, args = state["fetchrow"][-1]
    assert args[1] == "example.com"
    assert args[2] == "alice"


def test_list_selects_no_ciphertext_column(fake_pool):
    state: dict = {"fetch_result": [_row()]}
    fake_pool(state)
    entries = asyncio.run(pwd_store.list_passwords("u_xxx"))
    assert len(entries) == 1
    assert "password" not in entries[0] and "password_enc" not in entries[0]
    sql, args = state["fetch"][0]
    assert "password_enc" not in sql
    assert args[0] == "u_xxx"


def test_list_filters_by_domain_when_given(fake_pool):
    state: dict = {"fetch_result": []}
    fake_pool(state)
    asyncio.run(pwd_store.list_passwords("u_xxx", "Example.COM"))
    sql, args = state["fetch"][0]
    assert "AND domain=$2" in sql
    assert args[1] == "example.com"


def test_get_password_decrypts_single_row(fake_pool):
    plaintext = "get-test-pwd"
    state: dict = {"row_result": _row(id=42, password_enc=encrypt_password(plaintext))}
    fake_pool(state)
    entry = asyncio.run(pwd_store.get_password("u_xxx", 42))
    assert entry["password"] == plaintext
    assert entry["domain"] == "example.com"
    sql, args = state["fetchrow"][0]
    assert "owner_user_id=$2" in sql  # 越权查询拿不到
    assert args == (42, "u_xxx")


def test_get_password_missing_returns_none(fake_pool):
    fake_pool({"row_result": None})
    assert asyncio.run(pwd_store.get_password("u_xxx", 999)) is None


def test_delete_requires_owner_and_audits(fake_pool):
    state: dict = {"row_result": {"id": 7, "domain": "example.com"}}
    fake_pool(state)
    assert asyncio.run(pwd_store.delete_password("u_xxx", 7)) is True
    sql, args = state["fetchrow"][-1]
    assert "DELETE FROM browser_passwords" in sql
    assert "owner_user_id=$2" in sql and args == (7, "u_xxx")
    audit = [a for s, a in state["execute"] if "security_events" in s]
    assert audit and audit[0][0] == pwd_store.EVENT_DELETED
    assert audit[0][2] == pwd_store.AUDIT_TAG


def test_delete_missing_returns_false(fake_pool):
    fake_pool({"row_result": None})
    assert asyncio.run(pwd_store.delete_password("u_xxx", 999)) is False


def test_validation_error_codes(fake_pool):
    fake_pool({})
    cases = [
        (dict(domain="", username="u", password="p"), "domain_required"),
        (dict(domain="x.com", username="", password="p"), "username_required"),
        (dict(domain="x.com", username="u", password=""), "password_required"),
        (dict(domain="x.com", username="u", password="x" * 2000), "password_too_long"),
        (dict(domain="x" * 300 + ".com", username="u", password="p"), "domain_too_long"),
        (dict(domain="x.com", username="u" * 300, password="p"), "username_too_long"),
    ]
    for kwargs, code in cases:
        with pytest.raises(ValueError) as ex:
            asyncio.run(pwd_store.upsert_password("u_xxx", **kwargs))
        assert str(ex.value) == code


def test_quota_blocks_only_new_entries(fake_pool):
    state: dict = {"row_result": _row()}
    conn = fake_pool(state)

    async def over_quota(sql, *args):
        if "count(*)" in sql.lower():
            return pwd_store.MAX_ENTRIES_PER_USER + 1
        return None  # 同键已存在查询 → 无行

    conn.fetchval_override = over_quota
    with pytest.raises(ValueError) as ex:
        asyncio.run(pwd_store.upsert_password(
            "u_xxx", domain="new.com", username="new", password="p"))
    assert str(ex.value) == "password_quota_exceeded"

    # 同 (owner,domain,username) 已存在 → 放行（配额只拦新行）
    async def exists(sql, *args):
        if "count(*)" in sql.lower():
            return pwd_store.MAX_ENTRIES_PER_USER + 1
        return 1

    conn.fetchval_override = exists
    meta = asyncio.run(pwd_store.upsert_password(
        "u_xxx", domain="example.com", username="alice", password="p"))
    assert meta["id"] == 7


def test_audit_detail_never_contains_secrets(fake_pool):
    state: dict = {"row_result": _row()}
    fake_pool(state)
    asyncio.run(pwd_store.upsert_password(
        "u_xxx", domain="audit.me", username="secret-user", password="secret-pass"))
    audit_args = [a for s, a in state["execute"] if "security_events" in s]
    assert audit_args, "保存必须写审计"
    event_type, severity, tag, detail_json = audit_args[0]
    assert event_type == pwd_store.EVENT_SAVED
    assert severity == "info" and tag == pwd_store.AUDIT_TAG
    detail = json.loads(detail_json)
    assert set(detail) == {"owner_user_id", "entry_id", "domain"}
    assert "secret-user" not in detail_json
    assert "secret-pass" not in detail_json


# ---------------------------------------------------------------------------
# 帧路由（sse_gateway）
# ---------------------------------------------------------------------------
class FakeWebSocket:
    def __init__(self):
        self.outbox = []

    async def send_text(self, text):
        self.outbox.append(text)


class _ExtWsAdapter:
    def __init__(self, client_id):
        self._client_id = client_id


def _frames(ws):
    return [json.loads(o) for o in ws.outbox]


@pytest.fixture()
def pwd_route(monkeypatch):
    """装好 owner 解析 + 返回 gateway 模块与发帧 helper。"""
    import mcp_plugin_store
    from mcp_runtime import sse_gateway as gw

    async def owner_of(client_id):
        return "u_owner" if client_id == "123" else None

    monkeypatch.setattr(mcp_plugin_store, "get_cdp_client_owner_user_id", owner_of)
    return gw


def test_pwd_frames_never_reach_driver(pwd_route, monkeypatch):
    """pwd_* 必须被 _route_ext_frame 截胡，不得进 driver.ingest_message。"""
    from mcp_runtime import sse_gateway as gw

    seen = []

    class _Driver:
        async def ingest_message(self, *a, **k):
            seen.append(a)

    class _Adapter:
        _client_id = "123"

    class _Ws:
        pass

    raw = json.dumps({"type": "pwd_list", "reqId": "r0"})
    # _route_ext_frame 内部按前缀分流，driver 只应是兜底参数
    try:
        asyncio.run(gw._route_ext_frame(raw, _Ws(), _Adapter(), _Driver()))
    except Exception:
        pass
    assert seen == []


def test_pwd_list_returns_meta_only(pwd_route, fake_pool):
    state: dict = {"fetch_result": [_row()]}
    fake_pool(state)
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_list", "reqId": "r1"}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "pwd_list_result" and payload["ok"] is True
    assert payload["entries"][0]["username"] == "alice"
    assert "password" not in payload["entries"][0]
    assert "password_enc" not in json.dumps(payload)


def test_pwd_get_missing_returns_error(pwd_route, fake_pool):
    fake_pool({"row_result": None})
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_get", "reqId": "r2", "id": 999}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "pwd_error" and payload["ok"] is False
    assert payload["message"] == "记录不存在"


def test_pwd_save_roundtrip_and_delete(pwd_route, fake_pool):
    state: dict = {"row_result": _row()}
    fake_pool(state)
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_save", "reqId": "r3", "domain": "example.com",
                    "username": "alice", "password": "S3cr3t-xyz", "url": ""}),
        ws, _ExtWsAdapter("123")))
    saved = _frames(ws)[0]
    assert saved["type"] == "pwd_save_result" and saved["entry"]["id"] == 7
    assert "S3cr3t-xyz" not in json.dumps(saved)

    ws2 = FakeWebSocket()
    state["row_result"] = {"id": 7, "domain": "example.com"}
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_delete", "reqId": "r4", "id": 7}), ws2, _ExtWsAdapter("123")))
    deleted = _frames(ws2)[0]
    assert deleted["type"] == "pwd_delete_result" and deleted["id"] == 7


def test_pwd_frame_ignores_declared_owner(pwd_route, fake_pool):
    """帧内声明的 owner/identity 一律忽略：owner 只来自已鉴权 adapter。"""
    state: dict = {"fetch_result": []}
    fake_pool(state)
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_list", "reqId": "r5",
                    "owner": "attacker", "owner_user_id": "attacker", "clientId": "999"}),
        ws, _ExtWsAdapter("123")))
    assert _frames(ws)[0]["ok"] is True
    sql, args = state["fetch"][0]
    assert args[0] == "u_owner"  # 不是 attacker


def test_pwd_error_when_no_client_id(pwd_route):
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_list", "reqId": "r6"}), ws, _ExtWsAdapter("")))
    payload = _frames(ws)[0]
    assert payload["type"] == "pwd_error"
    assert "身份无效" in payload["message"]


def test_pwd_error_when_client_unbound(pwd_route):
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_list", "reqId": "r7"}), ws, _ExtWsAdapter("404")))
    payload = _frames(ws)[0]
    assert payload["type"] == "pwd_error"
    assert "未绑定用户" in payload["message"]


def test_pwd_value_error_mapped_to_chinese(pwd_route, fake_pool):
    fake_pool({})
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_save", "reqId": "r8", "domain": "", "username": "",
                    "password": ""}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "pwd_error"
    assert payload["message"] == "域名无效"


def test_pwd_invalid_tag_mapped_to_key_changed(pwd_route, monkeypatch, fake_pool):
    from cryptography.exceptions import InvalidTag

    fake_pool({"row_result": _row(password_enc="v1:aGVsbG8=:dGVzdA==")})

    async def broken(*_a, **_k):
        raise InvalidTag()

    monkeypatch.setattr(pwd_store, "get_password", broken)
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_get", "reqId": "r9", "id": 7}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "pwd_error"
    assert "密钥已变更" in payload["message"]


def test_pwd_unknown_op_returns_error(pwd_route):
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame(
        json.dumps({"type": "pwd_hack", "reqId": "r10"}), ws, _ExtWsAdapter("123")))
    assert _frames(ws)[0]["message"] == "不支持的密码管理操作"


def test_pwd_bad_json_silently_ignored(pwd_route):
    ws = FakeWebSocket()
    asyncio.run(pwd_route._handle_pwd_frame("not json", ws, _ExtWsAdapter("123")))
    assert ws.outbox == []


def test_pwd_plaintext_never_logged(pwd_route, fake_pool, monkeypatch):
    """明文密码不得出现在任何日志行里（含异常路径）。"""
    import loguru

    lines: list[str] = []
    sink_id = loguru.logger.add(lambda msg: lines.append(str(msg)), level="DEBUG")
    try:
        fake_pool({"row_result": _row()})
        ws = FakeWebSocket()
        asyncio.run(pwd_route._handle_pwd_frame(
            json.dumps({"type": "pwd_save", "reqId": "r11", "domain": "example.com",
                        "username": "alice", "password": "S3cr3t-xyz", "url": ""}),
            ws, _ExtWsAdapter("123")))

        # 再走一次异常路径
        async def boom(*_a, **_k):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(pwd_store, "upsert_password", boom)
        ws2 = FakeWebSocket()
        asyncio.run(pwd_route._handle_pwd_frame(
            json.dumps({"type": "pwd_save", "reqId": "r12", "domain": "example.com",
                        "username": "alice", "password": "S3cr3t-xyz", "url": ""}),
            ws2, _ExtWsAdapter("123")))
    finally:
        loguru.logger.remove(sink_id)
    joined = "\n".join(lines)
    assert "S3cr3t-xyz" not in joined


# ---------------------------------------------------------------------------
# 扩展源码契约
# ---------------------------------------------------------------------------
def _ext(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "mcp_builtin", "cdp_bridge",
                        "tmwd_cdp_bridge", name)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_background_js_transport_contract():
    src = _ext("background.js")
    for token in ("pwd_list", "pwd_get", "pwd_save", "pwd_delete", "sendPwdFrame",
                  "forwardPwdFrame", "failPendingPwdRequests", "_pwdReqs", "pwd_frame"):
        assert token in src, token
    assert "port.name !== 'pwd'" in src
    assert "flushPwdFrameQueue" in src


def test_background_js_frame_capture_relay_contract():
    """iframe 捕获的凭据经扩展内部通道中转回顶层画卡：明文不走 postMessage('*')。"""
    src = _ext("background.js")
    assert "pwd_frame_capture" in src
    assert "pwd_show_save_card" in src
    assert "{ frameId: 0 }" in src or "frameId: 0" in src
    # 顶层投递用 tabs.sendMessage，绝不向页面 postMessage 广播明文
    assert "postMessage('*')" not in src and 'postMessage("*")' not in src


def test_popup_js_manual_entry_contract():
    """识别漏了要有自救入口：popup 手动新增走既有 pwd_save 通道。"""
    src = _ext("popup.js")
    assert "initPwdManual" in src
    assert "pwdManualToggle" in src
    html = _ext("popup.html")
    for element_id in ("pwdManualToggle", "pwdManualBox", "pwdManualDomain",
                       "pwdManualUser", "pwdManualPass", "pwdManualSave"):
        assert element_id in html, element_id


def test_content_js_capture_contract():
    src = _ext("content.js")
    for token in ("tmwd-pwd-card", "cdpPwdPending", "cdpPwdPrompted", "credentialFingerprint",
                  "inferLoginSuccess", "showSaveConfirm", "setInputValue",
                  "maybeShowSuggestions", "tmwdGeneratePassword"):
        assert token in src, token
    # 不得再教「释放 autofill 读 value」
    assert "读tmwebdriver_sop" not in src


def test_content_js_capture_recall_contract():
    """识别强化对齐 Chrome：放宽触发 / 密码-only / Shadow DOM / iframe / 定点错误。"""
    src = _ext("content.js")
    # 1a 统一捕获入口 + 离开页面兜底触发
    assert "captureLatestLogin" in src
    assert "pagehide" in src
    # 1b 只强制密码非空，用户名允许空（分步登录第二屏/改密码页不再被静默丢弃）
    assert "if (!password) return" in src
    assert "if (!username || !password) return" not in src
    # 1b 密码-only 自动关联同域账号
    assert "cdpPwdUser@" in src and "rememberUsername" in src and "recallUsername" in src
    # 2 Shadow DOM 穿透，且排除我们自己注入的 UI（防弹卡自指环）
    assert "deepQueryAllInputs" in src and "shadowRoot" in src
    assert "isInjectedUi" in src
    # 2 shadow 内事件取真实内部节点（e.target 会被 retarget 成 host）
    assert "composedPath" in src
    # 1c 定点错误扫描取代旧「整页 4000 字一票否决」
    assert "scopedErrorScan" in src
    assert "substring(0, 4000)" not in src
    # 3 iframe 捕获：密码 IIFE 不再只跑顶层（哨兵），其余面板 IIFE 仍限顶层
    assert "// PWD_IFRAME_OK" in src


def test_config_js_generator_contract():
    src = _ext("config.js")
    assert "function tmwdGeneratePassword" in src
    assert "getRandomValues" in src


def test_content_js_embedded_iframe_login_contract():
    """跨域登录 iframe（阿里 Havana mini_login，modelscope.cn 即此类）。

    iframe 自身看不到成功——成功信号是「宿主在 iframe 提交后整页 reload、iframe 随之
    销毁」，所以：① iframe 捕获的 pending 也必须落 session 快照（原先是 iframe 不落，
    reload 后新顶层文档读不到 → 弹卡永不出现）；② 顶层见到来自 iframe 的 pending 就
    当成功（先过报错闸，防别的登录框误判）。
    """
    src = _ext("content.js")
    # ① pending 带来源标记，且 iframe 也落快照（不再被 isTopFrame 挡住）
    assert "fromIframe: !isTopFrame" in src
    assert "if (isTopFrame) setPendingSession(pendingLogin);" not in src
    # ② 顶层对 iframe 来源的 pending 直接判成功（报错闸在前）
    assert "pendingLogin.fromIframe && !scopedErrorScan()" in src
    # ③ 任何一方清 pending 都抹共享快照：iframe 取消/失败时顶层才不会拿旧 pending 误弹
    assert "setPendingSession(null);" in src
    assert "if (isTopFrame) setPendingSession(null);" not in src


def test_popup_js_manager_contract():
    src = _ext("popup.js")
    assert "chrome.runtime.connect({ name: PWD_PORT_NAME })" in src
    assert "pwd_frame" in src and "sendPwdRequest" in src and "renderPwdList" in src
    assert "pwd_list" in src and "pwd_get" in src and "pwd_delete" in src


def test_popup_html_wires_manager():
    src = _ext("popup.html")
    assert 'src="config.js"' in src
    for element_id in ("pwdGenToggle", "pwdTabSite", "pwdTabAll", "pwdSearch", "pwdList"):
        assert element_id in src, element_id


# ---------------------------------------------------------------------------
# 离线本地缓存（pwd_cache.js + background 的离线读写/补同步）
# ---------------------------------------------------------------------------
def test_pwd_cache_js_encrypts_at_rest():
    """本地镜像必须 AES-GCM 静态加密，且只落 chrome.storage.local（不是明文 JSON）。"""
    src = _ext("pwd_cache.js")
    assert "crypto.subtle.generateKey" in src
    assert "AES-GCM" in src
    assert "crypto.subtle.encrypt" in src and "crypto.subtle.decrypt" in src
    assert "chrome.storage.local" in src
    assert "cdpPwdCache" in src
    # 明文不得进 console（注释里提到「不 console.log」不算，认调用形态）
    assert "console.log(" not in src


def test_background_js_offline_password_contract():
    """断网时读走缓存、写落本地并标 pending，重连后补同步。"""
    src = _ext("background.js")
    # 缓存模块要被 SW 引入
    assert "importScripts('pwd_cache.js')" in src
    for token in ("pwdCacheSaveOffline", "pwdCacheList", "pwdCacheGet", "pwdCacheUpsert",
                  "pwdCacheRemove", "pwdCacheAddTombstone", "pwdCacheDropTombstone",
                  "pwdCacheMergeList", "pwdCachePending", "pwdCacheTombstones",
                  "syncPwdCache", "_pwdSyncReqs", "_offlinePwdFrame", "failPwdInFlight",
                  "_purgeQueuedPwdSaves"):
        assert token in src, token
    # 在途表要留帧：连接在回帧前断掉时，写才能回退成本地 pending
    assert "reqId -> { target, frame }" in src
    # auth_ok 后要补同步
    assert "void syncPwdCache();" in src
    # 离线读不再直接判死（1.5s 宽限后回缓存）
    assert "PWD_OFFLINE_READ_GRACE_MS" in src
    # 换客户端 token = 换 owner：必须清缓存，防把上一个账号的密码填进表单
    assert "pwdCacheClear" in src


def test_pwd_cache_js_read_write_api_shape():
    """缓存模块对外 API 齐备（background 依赖这些名字）。"""
    src = _ext("pwd_cache.js")
    for fn in ("function pwdCacheList(", "function pwdCacheGet(", "function pwdCacheUpsert(",
               "function pwdCacheRemove(", "function pwdCacheMergeList(",
               "function pwdCacheSaveOffline(", "function pwdCachePending(",
               "function pwdCacheTombstones(", "function pwdCacheAddTombstone(",
               "function pwdCacheDropTombstone(", "function pwdCacheClear(",
               "function pwdCacheMeta(", "function pwdCacheDropPending("):
        assert fn in src, fn


def test_offline_save_and_list_flows_mark_offline():
    """离线保存/删除的回执要带 offline 标（前端文案据此区分「已同步」与「待同步」）。"""
    src = _ext("background.js")
    assert "offline: true" in src
    panel = _ext("content.js")
    assert "saveResp.offline" in panel
    assert "已保存到本地，联网后自动同步" in panel


def test_manifest_declares_clipboard_write():
    manifest = json.loads(_ext("manifest.json"))
    assert "clipboardWrite" in manifest["permissions"]
    assert "storage" in manifest["permissions"]


def test_simphtml_guard_contract():
    path = os.path.join(os.path.dirname(__file__), "..", "mcp_builtin", "cdp_bridge", "simphtml.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "function isSecretField(node)" in src
    assert "!isSecretField(sourceNode)" in src
    assert "data-secret" in src
    # 旧串（指向已删除的 SOP 章节）必须消失
    assert "读tmwebdriver_sop的autofill章节提取" not in src


# ---------------------------------------------------------------------------
# simphtml 行为
# ---------------------------------------------------------------------------
@pytest.fixture()
def simphtml(monkeypatch):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_builtin", "cdp_bridge"))
    import simphtml as mod
    return mod


def test_is_secret_field_detects_password_like_inputs(simphtml):
    """在 Node 里跑 isSecretField 太贵；这里用等价 Python 断言契约 + JS 源码形状。"""
    src = simphtml.js_optHTML
    # 判定式必须包含 password type 与关键词启发式
    assert "'password'" in src
    assert "pass|pwd|" in src
    assert "secret|token" in src
    assert "autocomplete" in src and "aria-label" in src


def test_data_secret_survives_token_optimizer(simphtml):
    from bs4 import BeautifulSoup

    html = ('<div><input type="password" data-secret="true">'
            '<input type="text" value="hello"></div>')
    result = str(simphtml.optimize_html_for_tokens(BeautifulSoup(html, "html.parser")))
    assert "data-secret" in result
    # data-secret 的值短（4 字符），不会被 __data__ 折叠
    assert 'data-secret="true"' in result
    # 普通文本字段不受影响（防过度拦截）
    assert 'value="hello"' in result


# ---------------------------------------------------------------------------
# 管理端路由（/api/v1/users/builtin-tools/browser-passwords/*）
# ---------------------------------------------------------------------------
def _admin_app():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from user_platform.routes_browser_passwords import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_admin_routes_registered_under_builtin_tools():
    from user_platform.routes_browser_passwords import router

    paths = [r.path for r in router.routes]
    base = "/api/v1/users/builtin-tools/browser-passwords"
    # 用户级路由：没有 {resource_id} 路径参数（密码不挂在客户端上）。
    assert base in paths
    assert f"{base}/view" in paths
    assert f"{base}/delete" in paths
    assert not any("{resource_id}" in p for p in paths), "密码是用户级资源，不应带 resource_id"


def test_admin_list_requires_auth():
    """未登录 → 401/403，不是 500/404（路由可达且鉴权生效）。"""
    resp = _admin_app().get("/api/v1/users/builtin-tools/browser-passwords")
    assert resp.status_code in (401, 403), resp.status_code


def test_admin_view_requires_auth():
    resp = _admin_app().post(
        "/api/v1/users/builtin-tools/browser-passwords/view",
        json={"entry_id": 1, "password": "x"},
    )
    assert resp.status_code in (401, 403), resp.status_code


def test_admin_delete_requires_auth():
    resp = _admin_app().post(
        "/api/v1/users/builtin-tools/browser-passwords/delete",
        json={"entry_id": 1},
    )
    assert resp.status_code in (401, 403), resp.status_code


def test_admin_view_requires_password_field():
    """二次鉴权字段缺失 → 422（schema 强制要求 password）。"""
    resp = _admin_app().post(
        "/api/v1/users/builtin-tools/browser-passwords/view",
        json={"entry_id": 1},
    )
    assert resp.status_code in (401, 403, 422)


def test_admin_routes_source_contracts():
    """源码契约：二次鉴权 + 审计 + 不记明文 + 用户级归属。"""
    path = os.path.join(os.path.dirname(__file__), "..", "user_platform",
                        "routes_browser_passwords.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "verify_password" in src            # 明文查看需二次鉴权
    assert "browser_password_viewed" in src    # 查看写审计
    assert "browser_password_admin" in src     # 审计 tag
    assert "severity" in src and "warning" in src
    # 归属恒为当前用户（无 resource_id 路径参数、无跨用户路径）
    assert "str(user.id)" in src
    assert "{resource_id}" not in src
    assert "resource_type='cdp_client'" not in src
    # 明文变量不得进日志
    assert "logger" not in src and "logging" not in src


def test_frontend_wires_password_dialog():
    """前端契约：密码入口在「添加浏览器」旁边（全局，不绑客户端）+ API 无 resourceId。"""
    base = os.path.join(os.path.dirname(__file__), "..", "user-frontend", "src")
    with open(os.path.join(base, "pages", "console", "user", "resources.tsx"), encoding="utf-8") as fh:
        page = fh.read()
    assert "BrowserPasswordDialog" in page
    assert "setPasswordDialogOpen" in page
    assert "KeyRound" in page
    # 入口必须紧邻「添加浏览器」那一组按钮（activeKind === "cdp" 分支里）
    assert 'activeKind === "cdp" && (' in page
    # 不应再有绑定到单个客户端的密码入口
    assert "setPasswordClientId" not in page
    assert "onOpenPasswords" not in page

    with open(os.path.join(base, "api", "builtinToolsClient.ts"), encoding="utf-8") as fh:
        client = fh.read()
    assert "listBrowserPasswords" in client
    assert "viewBrowserPassword" in client
    assert "deleteBrowserPassword" in client
    assert "/browser-passwords" in client
    # API 签名不带 resourceId（用户级）
    assert "listBrowserPasswords(resourceId" not in client
    assert "listBrowserPasswords(): " in client


def test_content_js_closes_card_before_toast():
    """保存成功后必须先关弹框再提示（本轮修的顺序 bug）。"""
    src = _ext("content.js")
    assert "showSavedToast" in src
    # doPromptSave 里 closeSaveCard() 必须出现在 showSavedToast 之前
    idx_close = src.index("closeSaveCard();\n        showSavedToast")
    assert idx_close > 0


def test_meta_exposes_source_and_created_at(fake_pool):
    """列表/详情要带来源与创建时间（网页端要展示「最后更新者」）。"""
    state: dict = {"fetch_result": [_row(source="user", created_at="2026-09-01T00:00:00+00:00")]}
    fake_pool(state)
    entries = asyncio.run(pwd_store.list_passwords("u_xxx"))
    assert entries[0]["source"] == "user"
    assert entries[0]["createdAt"] == "2026-09-01T00:00:00+00:00"
    assert "password_enc" not in json.dumps(entries[0])
    sql = state["fetch"][0][0]
    assert "source" in sql and "created_at" in sql


def test_meta_defaults_source_to_browser(fake_pool):
    """老数据没有 source 时回落到 browser，不炸。"""
    fake_pool({"fetch_result": [_row()]})
    entries = asyncio.run(pwd_store.list_passwords("u_xxx"))
    assert entries[0]["source"] == "browser"


def test_upsert_source_reaches_sql(fake_pool):
    state: dict = {"row_result": _row()}
    fake_pool(state)
    asyncio.run(pwd_store.upsert_password(
        "u_xxx", domain="e.com", username="u", password="p", source="user"))
    sql, args = state["fetchrow"][-1]
    assert "source=EXCLUDED.source" in sql
    assert args[-1] == "user"


def test_update_password_sets_user_source(fake_pool):
    state: dict = {"row_result": _row(source="user", domain="new.com")}
    fake_pool(state)
    meta = asyncio.run(pwd_store.update_password(
        "u_xxx", 7, domain="new.com", password="new-secret"))
    assert meta["source"] == "user"
    sql, args = state["fetchrow"][-1]
    assert "UPDATE browser_passwords SET" in sql
    assert "source=$" in sql                  # source 作为参数写入（值见下）
    assert "user" in args                     # 值 = user（最后更新者）
    assert "new-secret" not in sql            # 密码必须加密后写入，不得明文进 SQL
    assert any(str(a).startswith("v1:") for a in args)
    assert "owner_user_id=$" in sql           # 越权防线
    assert args[-2:] == (7, "u_xxx")


def test_update_password_missing_returns_none(fake_pool):
    fake_pool({"row_result": None})
    assert asyncio.run(pwd_store.update_password("u_xxx", 999, url="x")) is None


def test_update_password_validation(fake_pool):
    fake_pool({})
    with pytest.raises(ValueError) as ex:
        asyncio.run(pwd_store.update_password("u_xxx", 7, domain="   "))
    assert str(ex.value) == "domain_required"
    with pytest.raises(ValueError) as ex:
        asyncio.run(pwd_store.update_password("u_xxx", 7, username=""))
    assert str(ex.value) == "username_required"
    with pytest.raises(ValueError) as ex:
        asyncio.run(pwd_store.update_password("u_xxx", 7, password="x" * 2000))
    assert str(ex.value) == "password_too_long"


def test_update_password_no_fields_returns_current(fake_pool):
    """什么都不改 → 回当前 meta，不写库。"""
    state: dict = {"row_result": _row()}
    fake_pool(state)
    meta = asyncio.run(pwd_store.update_password("u_xxx", 7))
    assert meta["id"] == 7
    assert not state.get("execute")  # 没写审计/没 UPDATE


# ---------------------------------------------------------------------------
# 端口隔离：同一个 host 不同端口不是同一站点
# ---------------------------------------------------------------------------
def test_content_js_uses_location_host_not_hostname():
    """存储/查询/匹配都必须用 location.host（含端口），否则 :7890 与 :8080 串台。"""
    src = _ext("content.js")
    assert "function currentSiteHost" in src
    assert "window.location.host ||" in src          # host 优先（含端口）
    assert "domain: currentSiteHost()" in src         # 保存时写含端口
    assert "sendPwdRequest('pwd_list', { domain: currentSiteHost() })" in src
    # 匹配必须做端口比对
    assert "function splitHostPort" in src
    assert "if (aPort !== bPort) return false;" in src
    # 默认端口归一化（example.com:443 == example.com）
    assert "replace(/:(80|443)$/, '')" in src


def test_popup_uses_host_with_port():
    src = _ext("popup.js")
    assert "(u.host || u.hostname)" in src     # 本站 tab 过滤带上端口


def test_popup_host_matching_is_port_aware():
    """本站 tab 传的是 host（含端口），服务端按精确 domain 过滤 → 端口天然隔离。"""
    src = _ext("popup.js")
    assert "pwdSiteHost" in src
    assert "{ domain: pwdSiteHost }" in src


# ---------------------------------------------------------------------------
# 网页端「设置」按钮：编辑走 REST + source=user
# ---------------------------------------------------------------------------
def test_admin_create_route_registered_and_authed():
    """管理页的「新增」：CRUD 里的 C，之前漏了。"""
    from user_platform.routes_browser_passwords import router
    paths = [r.path for r in router.routes]
    assert "/api/v1/users/builtin-tools/browser-passwords/create" in paths
    resp = _admin_app().post(
        "/api/v1/users/builtin-tools/browser-passwords/create",
        json={"domain": "e.com", "username": "u", "password": "p"},
    )
    assert resp.status_code in (401, 403), resp.status_code


def test_admin_create_requires_fields():
    """缺字段 → 422（schema 强制 domain/username/password）。"""
    resp = _admin_app().post(
        "/api/v1/users/builtin-tools/browser-passwords/create",
        json={"domain": "e.com"},
    )
    assert resp.status_code in (401, 403, 422)


def test_admin_create_marks_user_source():
    """新增走 upsert 且 source='user'（网页端手填），不是 browser。"""
    path = os.path.join(os.path.dirname(__file__), "..", "user_platform",
                        "routes_browser_passwords.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("async def create_browser_password")
    end = src.index("@router.post(\"/view\")", start)
    body = src[start:end]
    assert "upsert_password" in body
    assert 'source="user"' in body
    # 错误码翻中文（复用同一张表）
    assert "_VALUE_ERROR_MESSAGES" in body


def test_frontend_has_create_button_and_form():
    """前端契约：管理弹框有「新增密码」按钮 + 新增表单（含生成密码）。"""
    base = os.path.join(os.path.dirname(__file__), "..", "user-frontend", "src")
    with open(os.path.join(base, "pages", "console", "user", "resources.tsx"), encoding="utf-8") as fh:
        page = fh.read()
    assert "新增密码" in page
    assert "setCreateOpen" in page
    assert "createBrowserPassword" in page
    assert "doCreate" in page
    assert "新增密码记录" in page
    assert "generatePassword" in page            # 新增表单里的「生成」按钮
    assert "Wand2" in page

    with open(os.path.join(base, "api", "builtinToolsClient.ts"), encoding="utf-8") as fh:
        client = fh.read()
    assert "createBrowserPassword" in client
    assert "/browser-passwords/create" in client


def test_frontend_crud_completeness():
    """管理弹框必须覆盖 CRUD 四项（曾只做查看，缺 C）。"""
    base = os.path.join(os.path.dirname(__file__), "..", "user-frontend", "src")
    with open(os.path.join(base, "api", "builtinToolsClient.ts"), encoding="utf-8") as fh:
        client = fh.read()
    for fn in ("listBrowserPasswords", "createBrowserPassword",
               "viewBrowserPassword", "updateBrowserPassword", "deleteBrowserPassword"):
        assert f"export function {fn}" in client, f"缺 API: {fn}"


def test_admin_update_route_registered_and_authed():
    from user_platform.routes_browser_passwords import router
    paths = [r.path for r in router.routes]
    assert "/api/v1/users/builtin-tools/browser-passwords/update" in paths
    resp = _admin_app().post(
        "/api/v1/users/builtin-tools/browser-passwords/update",
        json={"entry_id": 1, "username": "new"},
    )
    assert resp.status_code in (401, 403), resp.status_code


def test_admin_update_source_contracts():
    path = os.path.join(os.path.dirname(__file__), "..", "user_platform",
                        "routes_browser_passwords.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "update_password" in src
    assert "duplicate_entry" in src            # 唯一键冲突有明确提示
    assert "source" in src                     # meta 带来源


def test_frontend_dialog_width_not_shadowed_by_base_class():
    """回归：DialogContent 基础类带 sm:max-w-md，无前缀的 max-w-* 会被它在
    ≥640px 时压掉（twMerge 不视为冲突、CSS 里 sm: 媒体查询更靠后）。必须用
    sm: 前缀写宽度，否则弹框永远卡在 448px（max-w-md）——曾反复调不动的根因。"""
    base = os.path.join(os.path.dirname(__file__), "..", "user-frontend", "src")
    with open(os.path.join(base, "pages", "console", "user", "resources.tsx"), encoding="utf-8") as fh:
        page = fh.read()
    # 找密码管理弹框那一行的 DialogContent
    m = re.search(r"DialogContent[^>]*password", page, re.IGNORECASE) or re.search(
        r'<DialogContent className="flex h-\[[^\]]+\][^"]*sm:max-w-\w+', page)
    assert m, "没找到密码管理弹框的 DialogContent"
    line = m.group(0)
    assert "sm:max-w-" in line, f"宽度必须带 sm: 前缀，否则被基础类 sm:max-w-md 压掉: {line}"
    assert "sm:max-w-md" not in line, "不应沿用基础类的 sm:max-w-md"


def test_frontend_has_settings_button_and_columns():
    """前端契约：表格含创建/更新/来源列 + 「设置」按钮 + 编辑弹框。"""
    base = os.path.join(os.path.dirname(__file__), "..", "user-frontend", "src")
    with open(os.path.join(base, "pages", "console", "user", "resources.tsx"), encoding="utf-8") as fh:
        page = fh.read()
    assert "创建时间" in page and "更新时间" in page and "来源" in page
    assert "setEditTarget" in page
    assert "updateBrowserPassword" in page
    assert "formatIso" in page
    assert "浏览器保存" in page and "用户编辑" in page
    # 弹框要更宽（带 sm: 前缀，见 test_frontend_dialog_width_not_shadowed_by_base_class）
    assert "sm:max-w-6xl" in page

    with open(os.path.join(base, "api", "builtinToolsClient.ts"), encoding="utf-8") as fh:
        client = fh.read()
    assert "updateBrowserPassword" in client
    assert "/browser-passwords/update" in client
    assert "createdAt" in client and "source" in client


def test_db_ddl_has_source_and_idempotent_alter():
    path = os.path.join(os.path.dirname(__file__), "..", "server", "db.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "source TEXT NOT NULL DEFAULT 'browser'" in src
    # 已存在的表要能补列（幂等）
    assert "ADD COLUMN IF NOT EXISTS source" in src


def test_pwd_save_never_fails_on_sw_wakeup_window():
    """回归：SW 被 pwd 消息唤醒时 connectWS() 仍在 await loadBridgeConfig()，
    此刻 connecting=false / ws=null。旧逻辑据此立刻返回
    "bridge websocket is not authenticated"——用户点保存就报错。
    现在必须：主动踢连接 + 排队（8s 兜底），不得直接失败。"""
    src = _ext("background.js")
    # 取出 sendPwdFrame 函数体
    start = src.index("function sendPwdFrame(")
    end = src.index("function forwardPwdFrame(", start)
    body = src[start:end]
    # 不得再出现「没在连就立刻失败」的分支
    assert "if (!connectingNow) return { ok: false, error: 'bridge websocket is not authenticated' };" not in body
    # 必须主动踢一次连接
    assert "void connectWS()" in body
    # 必须排队（含 8s 兜底）
    assert "queuePwdFrame(" in body
    # 兜底超时是 8s
    assert "PWD_QUEUE_TIMEOUT_MS = 8000" in src
    # 未就绪路径必须返回 queued（前端据此不显示失败）
    assert "queued: true" in body


def test_pwd_domain_uses_host_with_port():
    """存储格式必须是 host（含非默认端口），即 127.0.0.1:7890 而非 127.0.0.1。"""
    src = _ext("content.js")
    assert "function currentSiteHost()" in src
    assert "window.location.host ||" in src
    # 保存/查询都必须走它
    assert "domain: currentSiteHost()" in src
    assert "domain: currentSiteHost() }" in src
    # 不得再用裸 hostname 作为站点标识
    assert "domain: window.location.hostname" not in src
    assert "domain: window.location.hostname," not in src


def test_admin_view_source_contracts():
    """管理员视图：列表返回 admin 标志 + ownerLabel；view 对 admin 免二次鉴权。"""
    path = os.path.join(os.path.dirname(__file__), "..", "user_platform",
                        "routes_browser_passwords.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "def _is_admin" in src
    assert '"admin": _is_admin(user)' in src          # 列表回 admin 标志
    assert "ownerLabel" in src                        # 归属列
    assert "def _owner_label" in src
    assert "def _resolve_owner_for_entry" in src      # 管理员可定位任意记录
    # admin 免二次鉴权
    assert "not _is_admin(user) and not verify_password" in src
    # 查看审计记 viewer（管理员追责关键）
    assert "viewer_user_id" in src
    # update 走 allow_any_owner
    assert "allow_any_owner=_is_admin(user)" in src
    # delete 先解析 owner（管理员可删他人）
    assert "_resolve_owner_for_entry(conn, body.entry_id, user)" in src


def test_admin_update_store_allows_any_owner(fake_pool):
    """allow_any_owner=True 时 UPDATE 不带 owner 过滤，审计按记录真实 owner。"""
    state: dict = {"row_result": _row(owner_user_id="other_user")}
    fake_pool(state)
    meta = asyncio.run(pwd_store.update_password(
        "admin_id", 7, url="https://x", allow_any_owner=True))
    assert meta["id"] == 7
    sql, args = state["fetchrow"][-1]
    assert "UPDATE browser_passwords SET" in sql
    assert "owner_user_id=$" not in sql          # 管理员不加 owner 过滤
    assert args[-1] == 7
    # 审计归属必须是记录真实 owner，不是管理员自己
    audit = [a for s, a in state["execute"] if "security_events" in s]
    assert audit and "other_user" in str(audit[0][3])


def test_admin_view_lists_all_users(fake_pool):
    """管理员 list 不过滤 owner（普通用户过滤）。"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "user_platform",
                            "routes_browser_passwords.py"), encoding="utf-8").read()
    start = src.index("async def list_browser_passwords")
    end = src.index("@router.post(\"/create\")", start)
    body = src[start:end]
    assert "if _is_admin(user):" in body
    # 管理员分支的 SQL 不带 owner 过滤
    admin_branch = body[body.index("if _is_admin(user):"):body.index("else:")]
    assert "WHERE owner_user_id=$1" not in admin_branch
    # 普通用户分支必须带
    user_branch = body[body.index("else:"):]
    assert "WHERE owner_user_id=$1" in user_branch


def test_frontend_admin_view_wiring():
    """前端：admin 视图显示「归属」列 + 跳过二次鉴权弹框。"""
    base = os.path.join(os.path.dirname(__file__), "..", "user-frontend", "src")
    with open(os.path.join(base, "pages", "console", "user", "resources.tsx"), encoding="utf-8") as fh:
        page = fh.read()
    assert "adminView" in page
    assert "归属" in page
    assert "ownerLabel" in page
    assert "resp.admin" in page
    assert "管理员视图" in page
    # 管理员直接查看（跳过弹框）
    assert "doReveal(entry)" in page
    assert "!!revealTarget && !adminView" in page

    with open(os.path.join(base, "api", "builtinToolsClient.ts"), encoding="utf-8") as fh:
        client = fh.read()
    assert "ownerLabel?" in client
    assert "admin?" in client


def test_save_card_releases_mount_guard_on_close():
    """回归：closeSaveCard 必须先注销 SPA 重挂守卫，否则守卫把节点挂回来，
    弹框点取消/保存都关不掉。"""
    src = _ext("content.js")
    # 守卫要返回注销函数
    assert "return function release()" in src
    assert "guards.splice(i, 1)" in src
    # 弹卡侧要持有注销句柄，并在关闭时调用
    assert "pwdCardReleaseGuard" in src
    # closeSaveCard 必须先 release 再移除节点
    close_idx = src.index("function closeSaveCard(")
    close_end = src.index("function removeSuggestions(")
    close_body = src[close_idx:close_end]
    release_pos = close_body.index("pwdCardReleaseGuard")
    remove_pos = close_body.index("removeChild(pwdCardHost)")
    assert release_pos < remove_pos, "closeSaveCard 必须先注销守卫再移除节点"
    assert "if (pwdCardReleaseGuard)" in close_body


def test_same_password_does_not_prompt_update():
    """回归：已有同账号记录时，密码没变就静默跳过，不再弹「更新登录信息？」。"""
    src = _ext("content.js")
    start = src.index("async function doPromptSave(")
    end = src.index("function mountPwdCardHost(")
    body = src[start:end]
    assert "pwd_get" in body                       # 必须取已存密码来比
    assert "entries.find((e) => e.username === username)" in body  # 找同账号记录
    # 一致就 return，不弹
    assert "if (stored && stored === password)" in body
    assert "clearPendingLogin();" in body


def test_generator_history_wiring():
    """生成随机密码后要在 popup 里可回看（用户没复制就走开的找回入口）。"""
    content = _ext("content.js")
    popup = _ext("popup.js")
    html = _ext("popup.html")

    # content 侧：生成即记录，且按站点存、有 TTL 清理
    assert "cdpPwdLastGen" in content
    assert "function recordGeneratedPassword" in content
    assert "recordGeneratedPassword(pwd)" in content      # renderGeneratePreview 里调用
    assert "PWD_LASTGEN_TTL" in content

    # popup 侧：同键读取 + 优先当前站点 + 复制按钮
    assert "cdpPwdLastGen" in popup
    assert "function loadLastGenerated" in popup
    assert "loadLastGenerated()" in popup                  # 启动时调用
    assert "storage.onChanged" in popup                    # 页面里新生成时即时刷新
    assert "pwdSiteHost" in popup                          # 优先当前站点

    # HTML：容器 + 复制按钮存在
    assert 'id="pwdLastGen"' in html
    assert 'id="pwdLastGenValue"' in html
    assert 'id="pwdLastGenCopy"' in html
