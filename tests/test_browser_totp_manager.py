"""CDP 身份验证器（``totp_credentials`` + ``totp_*`` 帧）的行为回归。

FakePool/假对象模式 + 扩展源码契约断言，不依赖真实 Postgres —— 遵循
``tests-need-postgres-baseline``。覆盖四层：TOTP 算法、otpauth 解析、存储层 SQL、
WS 帧处理，以及 background/popup 的传输契约。

安全不变量（本套件的核心）：
- 任何回帧 / meta **都不含 ``secret_enc`` 或明文密钥**；
- owner 恒来自已鉴权 adapter，帧内身份字段被忽略；
- 密钥的解密只发生在 ``get_credential`` 内存里，且不进返回值。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, "server")

import totp_code  # noqa: E402
import totp_store  # noqa: E402
from totp_code import normalize_secret, parse_otpauth, seconds_remaining, totp_code as compute_code  # noqa: E402

TEST_KEY = "unit-test-browser-password-key-0123456789"
EXT_DIR = Path(__file__).resolve().parent.parent / "mcp_builtin" / "cdp_bridge" / "tmwd_cdp_bridge"


@pytest.fixture(autouse=True)
def _ensure_key(monkeypatch):
    monkeypatch.setenv("BROWSER_PASSWORD_KEY", TEST_KEY)


def _ext(name: str) -> str:
    return (EXT_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fake pool / conn（仿 tests/test_browser_password_manager.py）
# ---------------------------------------------------------------------------
class _FakeConn:
    def __init__(self, state: dict):
        self._state = state

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
        return self._state.get("val_result")

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


@pytest.fixture()
def fake_pool(monkeypatch):
    def install(state: dict):
        import db

        conn = _FakeConn(state)
        monkeypatch.setattr(db.PostgresClient, "pool", _FakePool(conn))
        return conn

    return install


class FakeWebSocket:
    def __init__(self):
        self.outbox = []

    async def send_text(self, text):
        self.outbox.append(text)


class _ExtWsAdapter:
    def __init__(self, client_id):
        self._client_id = client_id


def _frames(ws):
    return [json.loads(t) for t in ws.outbox]


@pytest.fixture()
def totp_route(monkeypatch):
    import mcp_plugin_store
    from mcp_runtime import sse_gateway as gw

    async def owner_of(client_id):
        return "u_owner" if client_id == "123" else None

    monkeypatch.setattr(mcp_plugin_store, "get_cdp_client_owner_user_id", owner_of)
    return gw


# ---------------------------------------------------------------------------
# TOTP 算法
# ---------------------------------------------------------------------------
def test_rfc6238_sha1_vectors():
    """RFC 6238 Appendix B：SHA1 / 8 位 / 30s 的官方向量。"""
    seed = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    for t, expected in {59: "94287082", 1111111109: "07081804"}.items():
        assert compute_code(seed, at=t, digits=8, period=30, algorithm="SHA1") == expected


def test_totp_matches_cryptography_reference():
    """与 cryptography 库逐位一致（SHA1/256/512 × 多 digits/period/时刻）。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.twofactor.totp import TOTP as Ref

    algos = {"SHA1": hashes.SHA1(), "SHA256": hashes.SHA256(), "SHA512": hashes.SHA512()}
    cases = [
        (b"\x01" * 16, 6, 30, "SHA1", 59),
        (b"\x01" * 20, 6, 30, "SHA1", 1111111109),
        (b"\x01" * 20, 6, 30, "SHA1", 1234567890),
        (b"\x01" * 32, 8, 60, "SHA256", 1234567890),
        (b"\x01" * 64, 6, 30, "SHA512", 1234567890),
        (b"\x01" * 20, 6, 15, "SHA1", 1699999999),
    ]
    for key, digits, period, algo, t in cases:
        seed_b32 = base64.b32encode(key).decode().rstrip("=")
        ref = Ref(key, length=digits, algorithm=algos[algo], time_step=period, enforce_key_length=False)
        assert compute_code(seed_b32, at=t, digits=digits, period=period, algorithm=algo) == \
            ref.generate(float(t)).decode()


def test_totp_pads_to_digits():
    """<10^digits 的码左补零（保证 6 位，不用不同长度）。"""
    seed = base64.b32encode(b"\x00" * 16).decode().rstrip("=")
    for _ in range(200):
        code = compute_code(seed)
        assert len(code) == 6 and code.isdigit()


def test_seconds_remaining_range():
    for period in (15, 30, 60):
        for t in (0, 1, period - 1, period, period + 1, 1699999999):
            r = seconds_remaining(at=t, period=period)
            assert 1 <= r <= period


# ---------------------------------------------------------------------------
# otpauth 解析
# ---------------------------------------------------------------------------
def test_parse_full_otpauth():
    uri = "otpauth://totp/GitHub:alice@example.com?secret=JBSWY3DPEHPK3PXP&issuer=GitHub"
    r = parse_otpauth(uri)
    assert r["issuer"] == "GitHub" and r["account"] == "alice@example.com"
    assert r["secret"] == "JBSWY3DPEHPK3PXP"
    assert r["digits"] == 6 and r["period"] == 30 and r["algorithm"] == "SHA1"


def test_parse_query_overrides_and_case_insensitive():
    """secret=/issuer= 与混合大小写 query 都要认（个别工具吐 SECRET=）。"""
    uri = "OTPAUTH://TOTP/Acme:u@a.com?SECRET=JBSWY3DPEHPK3PXP&ISSUER=Acme&digits=8&period=60&algorithm=sha256"
    r = parse_otpauth(uri)
    assert r["issuer"] == "Acme" and r["account"] == "u@a.com"
    assert r["secret"] == "JBSWY3DPEHPK3PXP"
    assert r["digits"] == 8 and r["period"] == 60 and r["algorithm"] == "SHA256"


def test_parse_account_only_label():
    r = parse_otpauth("otpauth://totp/alice@example.com?secret=JBSWY3DPEHPK3PXP")
    assert r["issuer"] == "" and r["account"] == "alice@example.com"


def test_parse_rejects_bad_input():
    with pytest.raises(ValueError, match="uri_not_otpauth"):
        parse_otpauth("https://example.com")
    with pytest.raises(ValueError, match="uri_not_totp"):
        parse_otpauth("otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP&counter=5")
    with pytest.raises(ValueError, match="secret_required"):
        parse_otpauth("otpauth://totp/x")
    with pytest.raises(ValueError, match="secret_invalid_base32"):
        parse_otpauth("otpauth://totp/x?secret=!!!bad!!!")
    with pytest.raises(ValueError):
        parse_otpauth("")


def test_normalize_secret_strips_and_cases():
    assert normalize_secret("jbsw y3dp ehpk3pxp") == "JBSWY3DPEHPK3PXP"
    assert normalize_secret("JBSWY3DPEHPK3PXP====") == "JBSWY3DPEHPK3PXP"
    with pytest.raises(ValueError, match="secret_required"):
        normalize_secret("   ")
    with pytest.raises(ValueError, match="secret_invalid_base32"):
        normalize_secret("a")


# ---------------------------------------------------------------------------
# 导出 URI 构建（备份）
# ---------------------------------------------------------------------------
def test_build_otpauth_uri_roundtrip():
    """build→parse 必须回到同一批字段（导出文件可被重新导入）。"""
    from totp_code import build_otpauth_uri

    cases = [
        dict(issuer="Google", account="wuxin1903@gmail.com", secret="PAFE3CBIQV4VIOWA4KZEWT2DXAU6VY5V"),
        dict(issuer="GitHub", account="alice@example.com", secret="JBSWY3DPEHPK3PXP",
             digits=8, period=60, algorithm="SHA256"),
        dict(issuer="", account="bare-account", secret="JBSWY3DPEHPK3PXP"),
    ]
    for c in cases:
        uri = build_otpauth_uri(**c)
        back = parse_otpauth(uri)
        assert back["account"] == c["account"]
        assert back["secret"] == normalize_secret(c["secret"])
        assert back["digits"] == c.get("digits", 6)
        assert back["period"] == c.get("period", 30)
        assert back["algorithm"] == c.get("algorithm", "SHA1").upper()
        if c["issuer"]:
            assert back["issuer"] == c["issuer"]


def test_build_otpauth_uri_omits_defaults():
    """默认参数不写进 query（与 Aegis 等导出风格一致，且回读靠 parse 补默认）。"""
    from totp_code import build_otpauth_uri

    uri = build_otpauth_uri(issuer="GitHub", account="a@b.c", secret="JBSWY3DPEHPK3PXP")
    assert "digits" not in uri and "period" not in uri and "algorithm" not in uri
    assert uri.startswith("otpauth://totp/")
    # 非默认值必须写出来，否则回读丢信息
    uri2 = build_otpauth_uri(issuer="GitHub", account="a@b.c", secret="JBSWY3DPEHPK3PXP", digits=8)
    assert "digits=8" in uri2


def test_parse_uses_query_issuer_for_colon_in_issuer():
    """issuer 含冒号时 account 不能被首冒号错拆（query issuer 是真相源）。"""
    from totp_code import build_otpauth_uri

    uri = build_otpauth_uri(issuer="A:B", account="x@y.com", secret="JBSWY3DPEHPK3PXP")
    r = parse_otpauth(uri)
    assert r["issuer"] == "A:B" and r["account"] == "x@y.com"


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
def _row(**over):
    row = {
        "id": 7, "owner_user_id": "u1", "label": "GitHub:alice", "issuer": "GitHub",
        "account": "alice",
        "domains": ["github.com"], "note": "工作账号", "url": "https://github.com",
        "digits": 6, "period": 30, "algorithm": "SHA1", "source": "user",
        "created_at": None, "updated_at": None,
    }
    row.update(over)
    return row


def test_row_to_meta_never_exposes_secret():
    meta = totp_store._row_to_meta(_row())
    assert "secret_enc" not in meta and "secret" not in meta
    assert meta["domains"] == ["github.com"] and meta["note"] == "工作账号"


def test_list_credentials_filters_by_domain(fake_pool):
    """「本站」过滤在 Python 侧走 host_matches_domains（issuer 站点名兜底也能命中）。"""
    rows = [
        _row(id=1, domains=["google"]),          # 导入兜底的站点名
        _row(id=2, domains=["github.com"]),      # 真实域名
        _row(id=3, domains=["dgn cloud"]),       # 带空格的站点名
        _row(id=4, domains=["other.com"]),
    ]
    state = {"fetch_result": rows}
    fake_pool(state)
    out = asyncio.run(totp_store.list_credentials("u1", domain="accounts.google.com"))
    assert [m["id"] for m in out] == [1]  # google 站点名命中 accounts.google.com
    state["fetch_result"] = rows
    out2 = asyncio.run(totp_store.list_credentials("u1", domain="github.com"))
    assert [m["id"] for m in out2] == [2]
    state["fetch_result"] = rows
    out3 = asyncio.run(totp_store.list_credentials("u1", domain="dgn.cloud"))
    assert [m["id"] for m in out3] == [3]
    # 不过滤时全量
    state["fetch_result"] = rows
    out4 = asyncio.run(totp_store.list_credentials("u1"))
    assert len(out4) == 4
    sql, args = state["fetch"][0]
    assert args[0] == "u1"
    assert "secret_enc" not in sql  # 列表查询绝不 SELECT 密钥列


def test_host_matches_domains_smart_matching():
    """issuer 站点名兜底（google / dgn cloud）也要能匹配真实主机名；不误命中。"""
    from totp_store import host_matches_domains as m

    assert m("accounts.google.com", ["google"]) is True      # 单 token 站点名命中主机段
    assert m("evilgoogle.com", ["google"]) is False          # 子串但非独立段，不命中
    assert m("dgn.cloud", ["dgn cloud"]) is True             # 带空格站点名按 token 兜底
    assert m("comic.xin", ["kimchi"]) is False               # 不相关不命中
    assert m("www.github.com", ["github.com"]) is True       # 父域
    assert m("my.gitlab.com", ["gitlab.com"]) is True        # 后缀域
    assert m("notexample.com", ["example.com"]) is False     # 伪后缀（缺前面的点）
    assert m("https://api.github.com/x", ["github"]) is True # URL 输入自动清洗
    assert m("", ["github.com"]) is False                    # 空主机名
    assert m("github.com", []) is False                      # 无关联网站


def test_get_credential_returns_code_not_secret(fake_pool, monkeypatch):
    """详情回 meta + 当前码 + 绝对失效时刻，不回密钥。"""
    state = {"row_result": _row(secret_enc="v1:aGVsbG8=:dGVzdA==")}
    fake_pool(state)
    monkeypatch.setattr(totp_store, "decrypt_password", lambda blob: "JBSWY3DPEHPK3PXP")
    out = asyncio.run(totp_store.get_credential("u1", 7))
    assert out is not None
    assert out["code"] and len(out["code"]) == 6
    assert out["expiresAt"] > time.time()  # 绝对失效时刻在未来
    assert "secondsRemaining" not in out   # 不再下发剩余秒数
    assert "secret_enc" not in out and "secret" not in out


def test_get_credential_missing_returns_none(fake_pool):
    fake_pool({"row_result": None})
    assert asyncio.run(totp_store.get_credential("u1", 999)) is None


def test_find_by_domain_requires_domain(fake_pool):
    fake_pool({})
    # 空域名直接回空列表，不猜、不兜底
    assert asyncio.run(totp_store.find_by_domain("u1", "")) == []


def test_find_by_domain_matches_issuer_token(fake_pool, monkeypatch):
    """agent 取码路径：issuer 站点名兜底的条目也能被真实主机名取到。"""
    rows = [
        _row(id=1, domains=["google"], secret_enc="v1:aGVsbG8=:dGVzdA=="),
        _row(id=2, domains=["other.com"], secret_enc="v1:aGVsbG8=:dGVzdA=="),
    ]
    state = {"fetch_result": rows}
    fake_pool(state)
    monkeypatch.setattr(totp_store, "decrypt_password", lambda blob: "JBSWY3DPEHPK3PXP")
    out = asyncio.run(totp_store.find_by_domain("u1", "accounts.google.com"))
    assert [m["id"] for m in out] == [1]  # other.com 的不被带出来
    assert out[0]["code"] and len(out[0]["code"]) == 6


def test_expires_at_is_next_period_boundary():
    """expires_at 回的是**下一个 period 边界**（绝对 epoch 秒），不是剩余秒数。"""
    import totp_code as tc

    # at=1700000000.5，period=30 → 边界 = floor(1700000000.5/30+1)*30
    exp = tc.expires_at(at=1700000000.5, period=30)
    assert exp == (1700000000 // 30 + 1) * 30
    assert exp > 1700000000.5 and exp - 1700000000.5 <= 30
    # 同一 at、不同 period 各自算自己的边界
    assert tc.expires_at(at=1700000000.5, period=60) == (1700000000 // 60 + 1) * 60


def test_list_with_codes_single_query_meta_and_code(fake_pool, monkeypatch):
    """列表页：一次取表 + 同一时刻算全表码（替代 list+get 的 N+1），回 meta+code+expiresAt。"""
    rows = [
        _row(id=1, domains=["github.com"], secret_enc="v1:aGVsbG8=:dGVzdA=="),
        _row(id=2, domains=["google"], secret_enc="v1:aGVsbG8=:dGVzdA=="),
    ]
    state = {"fetch_result": rows}
    fake_pool(state)
    monkeypatch.setattr(totp_store, "decrypt_password", lambda blob: "JBSWY3DPEHPK3PXP")
    out = asyncio.run(totp_store.list_with_codes("u1"))
    assert len(out) == 2
    # 每条都是完整 meta + code + 绝对失效时刻，且**不含** secret_enc / 剩余秒数
    for e in out:
        assert e["code"] and e["expiresAt"] > time.time()
        assert "secondsRemaining" not in e
        assert "secret_enc" not in e and "secret" not in e
        assert "issuer" in e and "domains" in e  # meta 字段齐全
    assert out[0]["expiresAt"] == out[1]["expiresAt"]  # 同一 now → 同一失效时刻
    # 只发一条 SQL（无 N+1）
    assert len(state.get("fetch", [])) == 1
    # domain 过滤走 host_matches_domains（issuer 站点名兜底也能命中）
    state["fetch_result"] = rows
    scoped = asyncio.run(totp_store.list_with_codes("u1", "accounts.google.com"))
    assert [e["id"] for e in scoped] == [2]


def test_list_with_codes_never_selects_secret_into_output(fake_pool, monkeypatch):
    """回帧不得出现 secret_enc（明文密钥无任何出口）。"""
    fake_pool({"fetch_result": [_row(id=1, secret_enc="v1:enc")]})
    monkeypatch.setattr(totp_store, "decrypt_password", lambda blob: "JBSWY3DPEHPK3PXP")
    out = asyncio.run(totp_store.list_with_codes("u1"))
    assert "secret_enc" not in json.dumps(out)
    assert "JBSWY3DPEHPK3PXP" not in json.dumps(out)


def test_current_code_uses_single_instant(monkeypatch):
    """_current_code 只取一次时刻：传 at 时绝不碰 time.time()，不传时恰好取一次。

    跨期瞬间若分别取 time()，会出现「码是上一期、失效时刻已是新一期」——就是读秒不准。
    """
    import time as _t

    monkeypatch.setattr(totp_store, "decrypt_password", lambda blob: "JBSWY3DPEHPK3PXP")
    # 传 at：不该再调 time.time()
    calls = []

    def spy():
        calls.append(1)
        return 1_700_000_000.5

    monkeypatch.setattr(_t, "time", spy)
    row = _row(secret_enc="v1:aGVsbG8=:dGVzdA==", period=30, digits=6)
    code, expires = totp_store._current_code(row, at=1_700_000_000.5)
    # 绝对失效时刻 = 下一个 30s 边界（1700000000 // 30 + 1) * 30
    assert calls == [] and expires == (1_700_000_000 // 30 + 1) * 30 and len(code) == 6
    # 不传 at：只取一次
    calls.clear()
    totp_store._current_code(row)
    assert len(calls) == 1


def test_upsert_requires_account_and_domains(fake_pool):
    fake_pool({"val_result": 0})
    for kwargs, code in (
        (dict(secret="JBSWY3DPEHPK3PXP", issuer="GitHub", account="", domains=["x.com"]), "account_required"),
        (dict(secret="JBSWY3DPEHPK3PXP", issuer="GitHub", account="a@b.c", domains=[]), "domains_required"),
        (dict(secret="", issuer="GitHub", account="a@b.c", domains=["x.com"]), "secret_required"),
        (dict(secret="JBSWY3DPEHPK3PXP", issuer="G", account="a@b.c", domains=["x.com"], digits=4), "digits_unsupported"),
        (dict(secret="JBSWY3DPEHPK3PXP", issuer="G", account="a@b.c", domains=["x.com"], period=5), "period_unsupported"),
        (dict(secret="JBSWY3DPEHPK3PXP", issuer="G", account="a@b.c", domains=["x.com"], algorithm="MD5"), "algorithm_unsupported"),
    ):
        with pytest.raises(ValueError) as ei:
            asyncio.run(totp_store.upsert_credential("u1", **kwargs))
        assert str(ei.value) == code, f"{kwargs}: expected {code}, got {ei.value}"


def test_upsert_encrypts_and_returns_meta(fake_pool, monkeypatch):
    state = {"row_result": _row()}
    fake_pool(state)
    seen = {}
    monkeypatch.setattr(totp_store, "encrypt_password", lambda p: seen.__setitem__("enc", p) or "ENC")
    out = asyncio.run(totp_store.upsert_credential(
        "u1", secret="JBSWY3DPEHPK3PXP", issuer="GitHub", account="alice",
        domains=["GitHub.com", "github.com", ""], note="工作", source="user",
    ))
    assert seen["enc"] == "JBSWY3DPEHPK3PXP"  # 加密前规范化，未加密
    # 关联网站去重 + 小写
    insert_args = state["fetchrow"][0][1]
    assert insert_args[7] == "ENC"
    assert out["domains"] == ["github.com"]  # 去重（同域名二次出现被吃掉）
    assert "JBSWY3DPEHPK3PXP" not in json.dumps(out)


def test_update_credential_ignores_secret(fake_pool):
    """编辑不接受 secret —— 换密钥走 upsert，避免改备注顺手动密钥。"""
    state = {"row_result": _row()}
    fake_pool(state)
    out = asyncio.run(totp_store.update_credential("u1", 7, note="改了", secret="NOPE")) \
        if False else asyncio.run(totp_store.update_credential("u1", 7, note="改了"))
    assert out is not None
    # update_credential 的签名不含 secret（传了会被当 TypeError/忽略）
    import inspect
    assert "secret" not in inspect.signature(totp_store.update_credential).parameters


def test_delete_credential_requires_owner(fake_pool):
    state = {"row_result": {"id": 7, "label": "GitHub:alice"}}
    fake_pool(state)
    assert asyncio.run(totp_store.delete_credential("u1", 7)) is True
    # 缺 owner 时 SQL 必带 owner 条件
    sql, args = state["fetchrow"][0]
    assert "owner_user_id" in sql and args[1] == "u1"


# ---------------------------------------------------------------------------
# 导入 / 导出（store）
# ---------------------------------------------------------------------------
def test_credential_exists_hits_unique_key(fake_pool):
    """判重按 (owner, issuer, account) 唯一键，不按 id。"""
    state = {"val_result": 1}
    fake_pool(state)
    assert asyncio.run(totp_store.credential_exists("u1", "GitHub", "alice")) is True
    sql, args = state["fetchval"][0]
    assert "issuer" in sql and "account" in sql
    assert args == ("u1", "GitHub", "alice")


def test_export_credentials_returns_plaintext_secret(fake_pool, monkeypatch):
    """导出是明文密钥的唯一出口：回 meta + 规范 base32 secret。"""
    state = {"fetch_result": [_row(secret_enc="v1:aGVsbG8=:dGVzdA==", issuer="GitHub", account="alice")]}
    fake_pool(state)
    monkeypatch.setattr(totp_store, "decrypt_password", lambda blob: "JBSWY3DPEHPK3PXP")
    out = asyncio.run(totp_store.export_credentials("u1"))
    assert out and out[0]["secret"] == "JBSWY3DPEHPK3PXP"
    assert out[0]["issuer"] == "GitHub"
    sql, _ = state["fetch"][0]
    assert "ORDER BY issuer" in sql  # 排序稳定（diff 友好）
    assert "secret_enc" in sql  # 导出才 SELECT 密钥列


def test_export_credentials_no_rows_returns_empty(fake_pool):
    fake_pool({"fetch_result": []})
    assert asyncio.run(totp_store.export_credentials("u1")) == []


# ---------------------------------------------------------------------------
# WS 帧
# ---------------------------------------------------------------------------
def test_totp_frames_never_reach_driver(totp_route, monkeypatch):
    from mcp_runtime import sse_gateway as gw

    seen = []

    class _Driver:
        async def ingest_message(self, *a, **k):
            seen.append(a)

    class _Adapter:
        _client_id = "123"

    class _Ws:
        pass

    raw = json.dumps({"type": "totp_list", "reqId": "r0"})
    try:
        asyncio.run(gw._route_ext_frame(raw, _Ws(), _Adapter(), _Driver()))
    except Exception:
        pass
    assert seen == []


def test_totp_list_returns_code_not_secret(totp_route, fake_pool, monkeypatch):
    import totp_store as ts

    state = {"fetch_result": [_row()]}
    fake_pool(state)

    async def fake_list_codes(owner, domain=None):
        return [{**_row(), "code": "123456", "expiresAt": 1700000030}]

    monkeypatch.setattr(ts, "list_with_codes", fake_list_codes)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_list", "reqId": "r1"}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_list_result" and payload["ok"] is True
    assert payload["entries"][0]["code"] == "123456"
    dumped = json.dumps(payload)
    assert "secret_enc" not in dumped
    # 明文密钥也不得出现在回帧
    assert "JBSWY3DPEHPK3PXP" not in dumped


def test_totp_save_never_echoes_secret(totp_route, fake_pool, monkeypatch):
    import totp_store as ts

    async def fake_upsert(owner, **kw):
        return {"id": 7, "issuer": kw["issuer"]}

    async def fake_get(owner, eid):
        return _row(id=eid, code="654321", expiresAt=1700000030)

    monkeypatch.setattr(ts, "upsert_credential", fake_upsert)
    monkeypatch.setattr(ts, "get_credential", fake_get)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_save", "reqId": "r2", "secret": "JBSWY3DPEHPK3PXP",
                    "issuer": "GitHub", "account": "alice", "domains": ["github.com"]}),
        ws, _ExtWsAdapter("123")))
    saved = _frames(ws)[0]
    assert saved["type"] == "totp_save_result"
    assert "JBSWY3DPEHPK3PXP" not in json.dumps(saved)


def test_totp_update_edits_display_fields_not_secret(totp_route, fake_pool, monkeypatch):
    """编辑帧 totp_update：按 id 改展示字段（无 secret），回 meta+code 不含密钥。"""
    import totp_store as ts

    seen = {}

    async def fake_update(owner, eid, **kw):
        seen["eid"] = eid
        seen["kw"] = kw
        return _row(id=eid, issuer=kw.get("issuer") or "GitHub")

    async def fake_get(owner, eid):
        return {**_row(id=eid, issuer="GitHub"), "code": "123456", "expiresAt": 1700000030}

    monkeypatch.setattr(ts, "update_credential", fake_update)
    monkeypatch.setattr(ts, "get_credential", fake_get)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_update", "reqId": "ru1", "id": 7,
                    "issuer": "GitHub", "account": "alice", "domains": ["github.com"],
                    "note": "工作账号", "secret": "SHOULD_BE_IGNORED"}),
        ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_update_result" and payload["ok"] is True
    assert seen["eid"] == 7
    # 展示字段透传，且**绝不**把 secret 传给存储层（update 签名本就没有 secret）
    assert seen["kw"]["issuer"] == "GitHub" and seen["kw"]["note"] == "工作账号"
    assert "secret" not in seen["kw"]
    assert "secret" not in json.dumps(payload)


def test_totp_update_missing_entry_errors(totp_route, monkeypatch):
    """id 不存在/越权 → update_credential 回 None → 错误帧。"""
    import totp_store as ts

    async def fake_update(owner, eid, **kw):
        return None

    monkeypatch.setattr(ts, "update_credential", fake_update)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_update", "reqId": "ru2", "id": 999, "issuer": "X"}),
        ws, _ExtWsAdapter("123")))
    assert _frames(ws)[0]["type"] == "totp_error"


def test_totp_frame_ignores_declared_owner(totp_route, fake_pool):
    """帧内 owner/identity 字段一律忽略：owner 只来自已鉴权 adapter。"""
    state = {"fetch_result": []}
    fake_pool(state)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_list", "reqId": "r3", "owner": "attacker", "owner_user_id": "attacker"}),
        ws, _ExtWsAdapter("123")))
    assert _frames(ws)[0]["ok"] is True


def test_totp_error_when_no_client_id(totp_route):
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_list", "reqId": "r4"}), ws, _ExtWsAdapter("")))
    assert _frames(ws)[0]["type"] == "totp_error"


def test_totp_error_when_client_unbound(totp_route):
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_list", "reqId": "r5"}), ws, _ExtWsAdapter("404")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_error" and "未绑定用户" in payload["message"]


def test_totp_value_error_mapped_to_chinese(totp_route, fake_pool, monkeypatch):
    import totp_store as ts

    async def bad(*a, **k):
        raise ValueError("secret_invalid_base32")

    monkeypatch.setattr(ts, "upsert_credential", bad)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_save", "reqId": "r6", "secret": "!!", "account": "a", "domains": ["x.com"]}),
        ws, _ExtWsAdapter("123")))
    assert _frames(ws)[0]["message"].startswith("密钥格式无效")


def test_totp_import_frame_separates_skip_from_error(totp_route, monkeypatch):
    """缺账号的行算「跳过」不算「失败」：数据本身没带账号，不是用户粘错了。"""
    import totp_store as ts

    async def fake_exists(owner, issuer, account):
        return False

    async def fake_upsert(owner, **kw):
        if not kw.get("account"):
            raise ValueError("account_required")
        return {"id": 1}

    async def fake_audit(owner, **kw):
        return None

    monkeypatch.setattr(ts, "credential_exists", fake_exists)
    monkeypatch.setattr(ts, "upsert_credential", fake_upsert)
    monkeypatch.setattr(ts, "audit_bulk", fake_audit)
    ws = FakeWebSocket()
    # 第一条没账号（label 空）→ 跳过；第二条有账号 → 成功
    text = (
        "otpauth://totp/?secret=JBSWY3DPEHPK3PXP&issuer=NoAcct\n"
        "otpauth://totp/GitHub:alice?secret=JBSWY3DPEHPK3PXP&issuer=GitHub\n"
    )
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_import", "reqId": "ri-skip", "content": text}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_import_result"
    assert payload["imported"] == 1
    assert payload["skipped_count"] == 1            # 缺账号计入跳过
    skipped = [e for e in payload["errors"] if e.get("skipped")]
    assert skipped and skipped[0]["line"] == 1
    assert "跳过" in skipped[0]["reason"]


def test_totp_unknown_op_returns_error(totp_route):
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_boom", "reqId": "r7"}), ws, _ExtWsAdapter("123")))
    assert _frames(ws)[0]["type"] == "totp_error"


def test_totp_import_frame_reports_line_errors(totp_route, monkeypatch):
    """WS 导入帧：好行入库、坏行报行号；回帧不回明文密钥。"""
    import totp_store as ts

    upserted = []

    async def fake_exists(owner, issuer, account):
        return False

    async def fake_upsert(owner, **kw):
        upserted.append(kw)
        return {"id": len(upserted)}

    async def fake_audit(owner, **kw):
        return None

    monkeypatch.setattr(ts, "credential_exists", fake_exists)
    monkeypatch.setattr(ts, "upsert_credential", fake_upsert)
    monkeypatch.setattr(ts, "audit_bulk", fake_audit)
    ws = FakeWebSocket()
    text = "otpauth://totp/Google:a@b.c?secret=JBSWY3DPEHPK3PXP&issuer=Google\nnot-a-uri\n"
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_import", "reqId": "ri1", "content": text}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_import_result" and payload["ok"] is True
    assert payload["imported"] == 1
    assert payload["errors"] and payload["errors"][0]["line"] == 2
    assert upserted[0]["secret"] == "JBSWY3DPEHPK3PXP"   # 服务端解析后的规范化字段
    assert "JBSWY3DPEHPK3PXP" not in json.dumps(payload)  # 回帧不含密钥


def test_totp_export_frame_requires_password(totp_route, monkeypatch):
    """WS 导出帧：密码校验不过就拒绝；通过才回明文备份（内存/下载，不落日志）。"""
    import totp_store as ts

    async def fake_verify(owner, password):
        return password == "good"

    async def boom_export(*a, **k):
        raise AssertionError("密码不对时不该走到导出")

    monkeypatch.setattr(ts, "verify_owner_password", fake_verify)
    monkeypatch.setattr(ts, "export_credentials", boom_export)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_export", "reqId": "re1", "password": "wrong"}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_error" and "账号密码" in payload["message"]


def test_totp_export_frame_returns_backup(totp_route, monkeypatch):
    import totp_store as ts

    async def fake_verify(owner, password):
        return True

    async def fake_export(owner):
        return [{"issuer": "Google", "account": "a@b.c", "secret": "JBSWY3DPEHPK3PXP",
                 "digits": 6, "period": 30, "algorithm": "SHA1"}]

    async def fake_audit(owner, **kw):
        return None

    monkeypatch.setattr(ts, "verify_owner_password", fake_verify)
    monkeypatch.setattr(ts, "export_credentials", fake_export)
    monkeypatch.setattr(ts, "audit_bulk", fake_audit)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_export", "reqId": "re2", "password": "good"}), ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_export_result" and payload["count"] == 1
    assert payload["content"].startswith("otpauth://totp/")  # 备份含明文密钥（这是唯一出口）


def test_totp_export_owner_password_verifier(fake_pool, monkeypatch):
    """verify_owner_password：查 mc_users 哈希；无表/无用户/密码空都 False。"""
    # 密码为空直接 False，不查库
    assert asyncio.run(totp_store.verify_owner_password("u1", "")) is False
    # mc_users 表不存在（to_regclass 回 None）→ False
    state = {"val_result": None, "row_result": None}
    fake_pool(state)
    assert asyncio.run(totp_store.verify_owner_password("u1", "x")) is False


def test_totp_invalid_tag_mapped_to_key_changed(totp_route, monkeypatch):
    from cryptography.exceptions import InvalidTag
    import totp_store as ts

    async def boom(*a, **k):
        raise InvalidTag()

    monkeypatch.setattr(ts, "get_credential", boom)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_get", "reqId": "r8", "id": 7}), ws, _ExtWsAdapter("123")))
    assert "密钥已变更" in _frames(ws)[0]["message"]


def test_totp_refresh_codes_batch_only_touches_listed_ids(totp_route, monkeypatch):
    """批量取码帧：只回指定 id 的 code/expiresAt，不回 meta、不回密钥。"""
    import totp_store as ts

    seen_ids = []

    async def fake_get(owner, eid):
        seen_ids.append(eid)
        return {**_row(id=eid), "code": f"{eid}111", "expiresAt": 1700000028}

    monkeypatch.setattr(ts, "get_credential", fake_get)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_refresh_codes", "reqId": "rc1", "ids": [7, 8]}),
        ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_refresh_codes_result" and payload["ok"] is True
    assert seen_ids == [7, 8]                       # 只解这两条，不全表
    assert len(payload["entries"]) == 2
    e = payload["entries"][0]
    assert e["code"] == "7111" and e["expiresAt"] == 1700000028 and e["id"] == 7
    # 回帧只含三要素，不回 domains/note/secret_enc 等 meta
    assert set(e.keys()) == {"id", "code", "expiresAt"}
    assert "secret" not in json.dumps(payload)


def test_totp_refresh_codes_drops_foreign_and_bad_ids(totp_route, monkeypatch):
    """id 不属本人时 get_credential 回 None → 静默跳过；非法 id 不进查询。"""
    import totp_store as ts

    seen = []

    async def fake_get(owner, eid):
        seen.append(eid)
        return None if eid == 9 else {**_row(id=eid), "code": "123456", "expiresAt": 1700000010}

    monkeypatch.setattr(ts, "get_credential", fake_get)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_refresh_codes", "reqId": "rc2", "ids": [3, 9, 0, "abc", -5]}),
        ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert seen == [3, 9]            # 0/"abc"/-5 被清洗掉
    assert [e["id"] for e in payload["entries"]] == [3]  # 9 不存在 → 静默跳过


def test_totp_refresh_codes_accepts_non_list(totp_route, monkeypatch):
    """ids 不是数组也不能炸（页面伪造/字段错位兜底）。"""
    import totp_store as ts

    called = []

    async def fake_get(owner, eid):
        called.append(eid)
        return None

    monkeypatch.setattr(ts, "get_credential", fake_get)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_refresh_codes", "reqId": "rc3", "ids": "not-a-list"}),
        ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_refresh_codes_result" and payload["entries"] == []
    assert called == []


def test_tick_is_display_only_no_fetch():
    """订阅/推模型下 tick **纯显示**：按 expiresAt 画环，绝不发请求、不重建表。
    三条根因全锁：①整表 reload（replaceChildren 拆 DOM）②本地自己掐表回头拉
    （=轮询，正是「要等下一次更新才看到」）③setInterval 每秒 -1 累加漂移。
    """
    src = _ext("popup.js")
    start = src.index("function tickTotpCountdown")
    tick_src = src[start:src.index("\n}", start + 1)]
    # ① 不整表重取
    assert "reloadTotpList" not in tick_src
    # ② 不发任何刷新请求（新码由服务端 totp_push 推来）
    assert "sendTotpRequest" not in tick_src and "refreshTotpCodes" not in tick_src
    # ③ 按绝对到期时刻算，不 -1 累加（消除 setInterval 抖动漂移）
    assert "expiresAt" in tick_src and "Math.ceil" in tick_src
    # 到点新码走推送落地
    assert "applyTotpPush" in src and "totp_push" in src


def test_subscribe_model_contract():
    """订阅推送接线：popup 进页订阅、关窗退订；背景注册 port + 广播无 reqId 推帧。"""
    popup = _ext("popup.js")
    assert "totp_subscribe" in popup and "totp_unsubscribe" in popup
    bg = _ext("background.js")
    assert "'totp_subscribe'" in bg and "'totp_unsubscribe'" in bg
    # 无 reqId 的推送帧走广播（不能按 reqId 定向丢帧）
    assert "broadcastTotpPush" in bg and "_totpPorts" in bg
    assert "totp_push" in bg and "data.reqId" in bg  # forwardTotpFrame 先判 push
    # 网关侧订阅 + 断连清理
    gw = (Path(__file__).resolve().parent.parent / "mcp_runtime" / "sse_gateway.py").read_text(encoding="utf-8")
    assert "await totp_push.subscribe" in gw and "totp_push.drop_adapter" in gw
    assert "totp_push.invalidate" in gw  # 增删改后失效内存


def test_push_memory_model_contract():
    """服务端内存推模型：内存存列表(meta+code+expiresAt)、订阅即回内存、先刷内存再推。"""
    push = (Path(__file__).resolve().parent.parent / "mcp_runtime" / "totp_push.py").read_text(encoding="utf-8")
    assert "_MEM" in push                       # 服务端内存：owner -> 列表
    assert "list_with_codes" in push            # 刷新走单查询取码（含 expiresAt）
    assert "codes_snapshot" not in push         # 旧快照口径已废
    # 订阅立即回一份内存快照（不必等下一次翻转）
    assert "async def subscribe" in push and "_send(adapter, {\"type\": \"totp_push\"" in push
    # 推送循环：先刷新内存、再推（订阅回帧与推送同源 → 同一 expiresAt）
    assert "async def _pump" in push and "_refresh(owner)" in push
    # 睡眠按绝对失效时刻算，不按剩余秒
    assert "expiresAt" in push and "_min_expires" in push


def test_push_subscribe_returns_memory_snapshot_immediately(monkeypatch):
    """订阅即推：subscribe 立刻把内存里的列表推给该连接（不必等下一次翻转）。"""
    import importlib

    import totp_store as ts

    push = importlib.import_module("mcp_runtime.totp_push")
    # 干净起点
    push._SUBS.clear()
    push._MEM.clear()
    for t in list(push._PUMPS.values()):
        t.cancel()
    push._PUMPS.clear()

    async def fake_list_with_codes(owner, domain=None):
        return [{"id": 1, "issuer": "GitHub", "domains": ["github.com"],
                 "code": "123456", "expiresAt": 1700000030}]

    monkeypatch.setattr(ts, "list_with_codes", fake_list_with_codes)

    sent = []

    class _Adapter:
        def post_message(self, text):
            sent.append(json.loads(text))

    async def run():
        adapter = _Adapter()
        snap = await push.subscribe(adapter, "u1", "github.com")
        assert len(snap) == 1 and snap[0]["expiresAt"] == 1700000030
        # 立即推了一份（无 reqId 的 totp_push）
        assert sent and sent[0]["type"] == "totp_push"
        assert sent[0]["entries"][0]["expiresAt"] == 1700000030  # 与返回值同一份
        # 收尾，别留后台任务
        push.unsubscribe(adapter)

    asyncio.run(run())
    assert push._SUBS == {} and push._MEM == {}


def test_push_domain_filter_reuses_store_matching(monkeypatch):
    """订阅带 domain 时按 host_matches_domains 过滤内存列表（本站页签）。"""
    import importlib

    push = importlib.import_module("mcp_runtime.totp_push")
    push._MEM["u1"] = {"entries": [
        {"id": 1, "domains": ["github.com"], "code": "111111", "expiresAt": 1700000030},
        {"id": 2, "domains": ["google"], "code": "222222", "expiresAt": 1700000030},
    ]}
    scoped = push._snapshot("u1", "accounts.google.com")
    assert [e["id"] for e in scoped] == [2]
    assert [e["id"] for e in push._snapshot("u1", "")] == [1, 2]
    push._MEM.clear()


def test_push_invalidate_refreshes_memory_then_pushes(monkeypatch):
    """增删改后 invalidate：**先刷新内存、再推**给订阅者（不是干等下一次翻转）。"""
    import importlib

    import totp_store as ts

    push = importlib.import_module("mcp_runtime.totp_push")
    push._SUBS.clear(); push._MEM.clear(); push._PUMPS.clear()

    calls = {"n": 0}

    async def fake_list_with_codes(owner, domain=None):
        calls["n"] += 1
        return [{"id": 1, "issuer": "X", "domains": ["x.com"],
                 "code": f"00000{calls['n']}", "expiresAt": 1700000030}]

    monkeypatch.setattr(ts, "list_with_codes", fake_list_with_codes)

    sent = []

    class _Adapter:
        def post_message(self, text):
            sent.append(json.loads(text))

    async def run():
        adapter = _Adapter()
        push._SUBS[adapter] = {"owner": "u1", "domain": ""}
        await push.invalidate("u1")           # 增删改触发
        assert calls["n"] == 1                 # 刷新了内存
        assert sent[-1]["type"] == "totp_push" # 并推了出去
        assert sent[-1]["entries"][0]["code"] == "000001"

    asyncio.run(run())
    push._SUBS.clear(); push._MEM.clear()


def test_push_refreshes_all_entries_not_just_first(monkeypatch):
    """翻转刷新必须覆盖**全部**条目（旧 bug：按 domain 订阅时泵只覆盖子集，
    表现为「只刷新第一个」）。全量订阅后一次刷新推全部条目、各带新码。"""
    import importlib

    import totp_store as ts

    push = importlib.import_module("mcp_runtime.totp_push")
    push._SUBS.clear(); push._MEM.clear(); push._PUMPS.clear()

    round_no = {"n": 0}

    async def fake_list_with_codes(owner, domain=None):
        round_no["n"] += 1
        n = round_no["n"]
        return [
            {"id": 1, "issuer": "A", "domains": ["a.com"], "code": f"1{n}", "expiresAt": 1700000030},
            {"id": 2, "issuer": "B", "domains": ["b.com"], "code": f"2{n}", "expiresAt": 1700000030},
            {"id": 3, "issuer": "C", "domains": ["c.com"], "code": f"3{n}", "expiresAt": 1700000030},
        ]

    monkeypatch.setattr(ts, "list_with_codes", fake_list_with_codes)

    sent = []

    class _Adapter:
        def post_message(self, text):
            sent.append(json.loads(text))

    async def run():
        adapter = _Adapter()
        push._SUBS[adapter] = {"owner": "u1", "domain": ""}   # 全量订阅
        await push.invalidate("u1")                            # 触发一次刷新+推
        entries = sent[-1]["entries"]
        assert len(entries) == 3                               # 全部三条都在
        assert {e["id"] for e in entries} == {1, 2, 3}
        # 三条都是**新一期**的码（不是只有第一条刷新）
        assert [e["code"] for e in entries] == ["11", "21", "31"]

    asyncio.run(run())
    push._SUBS.clear(); push._MEM.clear()


def test_push_unsubscribe_stops_pump_and_clears_memory():
    """最后一个订阅者退订 → 停任务、清内存（下次订阅冷启动重刷）。"""
    import importlib

    push = importlib.import_module("mcp_runtime.totp_push")
    push._SUBS.clear(); push._MEM.clear(); push._PUMPS.clear()

    class _Adapter:
        def post_message(self, text):
            pass

    async def run():
        a = _Adapter()
        push._SUBS[a] = {"owner": "u1", "domain": ""}
        push._MEM["u1"] = {"entries": [{"id": 1, "code": "1", "expiresAt": 1}]}
        push._PUMPS["u1"] = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        push.unsubscribe(a)
        assert push._SUBS == {}
        assert "u1" not in push._MEM          # 内存清掉
        assert "u1" not in push._PUMPS        # 任务停了

    asyncio.run(run())


def test_web_console_refreshes_per_entry_not_whole_list():
    """网页端同样不得整表 refetch：只刷到期那几条（POST /code），结构变化才 reload。"""
    base = Path(__file__).resolve().parent.parent
    page = (base / "user-frontend" / "src" / "pages" / "console" / "user" / "resources.tsx").read_text(encoding="utf-8")
    assert "refreshExpired" in page and "refreshTotpCode" in page
    # 每秒 tick 的区间里不得出现 listTotpCredentials（整表重取 = 全表重绘）
    start = page.index("setInterval(() => {")
    tick_src = page[start:start + 700]
    assert "listTotpCredentials" not in tick_src


def test_background_js_totp_refresh_codes_contract():
    """背景页要把 totp_refresh_codes 注册进路由 + 装配 ids。"""
    src = _ext("background.js")
    assert "'totp_refresh_codes'" in src
    assert re.search(r"msg\.cmd === 'totp_refresh_codes'[\s\S]*?frame\.ids", src)


def test_totp_by_domain_returns_codes(totp_route, monkeypatch):
    import totp_store as ts

    async def fake_find(owner, domain):
        return [{**_row(id=7), "code": "111222", "expiresAt": 1700000012}]

    monkeypatch.setattr(ts, "find_by_domain", fake_find)
    ws = FakeWebSocket()
    asyncio.run(totp_route._handle_totp_frame(
        json.dumps({"type": "totp_by_domain", "reqId": "r9", "domain": "github.com"}),
        ws, _ExtWsAdapter("123")))
    payload = _frames(ws)[0]
    assert payload["type"] == "totp_codes_result" and payload["entries"][0]["code"] == "111222"


# ---------------------------------------------------------------------------
# 扩展源码契约（防回归：改坏接线让测试红）
# ---------------------------------------------------------------------------
def test_background_js_totp_transport_contract():
    src = _ext("background.js")
    assert "totp_frame" in src and "sendTotpFrame" in src
    assert "queueTotpFrame" in src and "flushTotpFrameQueue" in src
    # 未就绪必须排队 + 主动踢连接（与 pwd 同款 SW 唤醒竞态保护）
    assert re.search(r"function sendTotpFrame[\s\S]*?void connectWS\(\)", src), \
        "sendTotpFrame 必须保留 void connectWS()（SW 唤醒竞态）"
    assert "queued: true" in src
    # 断连收尾必须覆盖 totp 在途与队列
    assert "failPendingTotpRequests" in src and "failTotpFrameQueue" in src
    # auth_ok 后续发
    assert "flushTotpFrameQueue()" in src
    # 帧路由：totp_ 前缀分流 + 到达 forwardTotpFrame
    assert "startsWith('totp_')" in src and "forwardTotpFrame" in src
    # popup 的长期 port（name='totp'）
    assert "'totp'" in src and "_totpPort" in src


def test_totp_has_no_local_cache_uses_server_memory():
    """TOTP 本机**不做任何本地镜像**（与密码库的 pwd_cache.js 相反）。

    密钥不出服务端 → 本机没有算码材料，列表由服务端**内存**持有、订阅即推来。
    故扩展侧不得有 totp 专用缓存文件/命令（那是错的层——修首屏延迟应在服务端内存）。
    """
    assert not (EXT_DIR / "totp_cache.js").exists()
    src = _ext("background.js")
    assert "totp_cache" not in src and "totp_cached_list" not in src
    assert "totpCache" not in src


def test_popup_js_totp_contract():
    src = _ext("popup.js")
    assert "initAuthenticator" in src
    assert "sendTotpRequest" in src and "ensureTotpPort" in src
    assert "view-totp" in src or "'totp'" in src
    # 三条录入路径：①框选页面二维码 ②选图/粘贴识别 ③手输 otpauth/密钥
    assert "scanQrOnPage" in src and "readTotpQrFromImageFile" in src
    assert "armTotpPasteDecode" in src
    assert "totpDomainsInput" in src  # 关联网站字段
    assert "totpNoteInput" in src  # 备注字段
    assert "parseOtpauth" in src
    # 订阅/推模型：码由服务端 totp_push 推来（applyTotpPush 原地 patch），本地 tick
    # 只按 expiresAt 画环；绝无整表 reload，也绝无「自己掐表回头拉」。
    assert "tickTotpCountdown" in src and "reloadTotpList" in src
    assert "applyTotpPush" in src and "patchTotpRowCode" in src and "patchTotpRowRing" in src
    assert "setTotpEntryExpiry" in src and "expiresAt" in src
    assert "totp_subscribe" in src and "totp_unsubscribe" in src
    # 只订阅一次（全量）：切页签不重订，本站是**客户端过滤**视图。
    assert "visibleTotpEntries" in src and "totpHostMatches" in src
    assert "subscribeTotpFull" in src
    # 切页签只重画、不重新订阅（否则与服务端推送竞争 → 数据「没了」）
    switch_src = src[src.index("function switchTotpTab"):]
    switch_src = switch_src[:switch_src.index("\n}", 1)]
    assert "totp_subscribe" not in switch_src and "renderTotpList" in switch_src
    # 订阅恒带空 payload（全量）——绝不按 domain 订阅
    sub_src = src[src.index("async function subscribeTotpFull"):]
    sub_src = sub_src[:sub_src.index("\n}", 1)]
    assert "'totp_subscribe', {}" in sub_src and "domain" not in sub_src
    # 首屏即时画：订阅即由服务端**立即**推内存快照（totp_push）→ 收到即画。
    assert "totp_subscribe" in src and "正在获取验证码" in src
    # 编辑：行上有「编辑」按钮 → totp_update（不改密钥，secret 输入框禁用）
    assert "openTotpEdit" in src and "totp_update" in src and "saveTotpEdit" in src
    assert "totpEditingId" in src and "secretInput.disabled = true" in src
    # tick 里不得**调用**整表重取（旧 bug：remaining=0 时每秒 reload → 一直闪）。
    # 注释里提到 reloadTotpList 是允许的，只查调用形式。
    tick_src = src[src.index("function tickTotpCountdown"):]
    tick_src = tick_src[:tick_src.index("\n}", 1)]
    assert "reloadTotpList()" not in tick_src and "void reloadTotpList" not in tick_src, \
        "tick 里不得调用 reloadTotpList（整表重建=闪烁）"
    # 导入/导出：选 .txt 一步批量入库、二次鉴权导出下载
    assert "runTotpImport" in src and "doTotpImportFile" in src
    assert "doTotpExport" in src and "totp_export" in src and "totp_import" in src
    assert "URL.createObjectURL" in src  # 导出落盘下载


def test_scan_qr_opens_snip_page_not_fullpage_decode():
    """框选识别：popup 截图存 session 后开独立 snip 页，不在 popup 里整页硬扫。"""
    src = _ext("popup.js")
    assert "captureVisibleTab" in src
    assert "totpSnipShot" in src and "snip.html" in src
    assert "chrome.windows.create" in src


def test_snip_page_contract():
    """snip 页：读截图 → 拖框 → 原分辨率裁块 jsQR → totp_save 保存 → 自关。"""
    page = _ext("snip.html")
    assert 'id="shot"' in page and 'id="box"' in page and 'id="result"' in page
    assert 'id="rDomains"' in page and 'id="rNote"' in page  # 关联网站/备注就地编辑
    assert "jsqr.min.js" in page and "snip.js" in page
    js = _ext("snip.js")
    assert "totpSnipShot" in js and "window.jsQR" in js
    assert "mousedown" in js and "mouseup" in js        # 拖框交互
    assert "drawImage" in js and "getImageData" in js   # 原分辨率裁块
    assert "sendSnipRequest" in js and "'totp_save'" in js  # 经 port 保存（非 sendMessage 回执）
    assert "chrome.storage.session" in js               # 截图只走 session，不落 local
    # 用完即清，截图不滞留
    assert "storage.session.remove" in js


def test_popup_html_totp_view_present():
    html = _ext("popup.html")
    assert 'id="view-totp"' in html
    for field in ("totpSecretInput", "totpIssuerInput", "totpAccountInput",
                  "totpDomainsInput", "totpNoteInput", "totpScanQr", "totpList"):
        assert f'id="{field}"' in html, f"缺少 {field}"
    assert "jsqr.min.js" in html  # 二维码解码依赖已引入


def test_jsqr_vendor_present_and_umd_global():
    """jsQR 必须 vendor 进来（popup.html 依赖它），且导出 window.jsQR。"""
    vendor = EXT_DIR / "vendor" / "jsqr.min.js"
    assert vendor.exists(), "缺少 vendor/jsqr.min.js"
    assert 'root["jsQR"]' in vendor.read_text(encoding="utf-8")


def test_server_route_registered():
    """REST 管理端路由已注册到 user_platform（网页端能管理验证器）。"""
    init = (Path(__file__).resolve().parent.parent / "user_platform" / "__init__.py").read_text(encoding="utf-8")
    assert "routes_totp" in init and "totp_credentials_router" in init


def test_db_has_totp_table_and_gin_index():
    db = (Path(__file__).resolve().parent.parent / "server" / "db.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS totp_credentials" in db
    assert "GIN (domains)" in db  # 关联网站数组索引
    assert "create_totp_credential_tables" in db
    # init 里挂上了
    assert "await cls.create_totp_credential_tables()" in db


def test_gateway_routes_totp_prefix():
    gw_src = (Path(__file__).resolve().parent.parent / "mcp_runtime" / "sse_gateway.py").read_text(encoding="utf-8")
    assert 'frame_type.startswith("totp_")' in gw_src
    assert "_handle_totp_frame" in gw_src


def test_totp_list_uses_single_query_not_n_plus_1():
    """列表帧必须一次取表算码（list_with_codes），不得 list + 逐条 get 的 N+1。"""
    base = Path(__file__).resolve().parent.parent
    gw_src = (base / "mcp_runtime" / "sse_gateway.py").read_text(encoding="utf-8")
    start = gw_src.index('if kind == "totp_list":')
    branch = gw_src[start:gw_src.index('elif kind == "totp_get":', start)]
    assert "list_with_codes" in branch
    assert "totp_svc.get_credential(" not in branch  # 不再逐条取
    # REST 列表同样收敛到单查询
    rest = (base / "user_platform" / "routes_totp.py").read_text(encoding="utf-8")
    lst = rest[rest.index("async def list_totp_credentials"):]
    lst = lst[:lst.index("async def _owner_label")]
    assert "list_with_codes" in lst and "totp_store.get_credential(" not in lst


# ---------------------------------------------------------------------------
# REST 导入 / 导出
# ---------------------------------------------------------------------------
class _FakeUser:
    def __init__(self, uid="u1", role="user", password="hashed"):
        self.id = uid
        self.role = role
        self.password = password


@pytest.fixture()
def totp_routes(monkeypatch):
    """导入 user_platform.routes_totp：绕开 package init，只装需要的依赖。"""
    import importlib
    mod = importlib.import_module("user_platform.routes_totp")
    return mod


def test_export_requires_password_and_has_no_admin_bypass(totp_routes, monkeypatch):
    """导出明文密钥：必须过 verify_password，管理员无旁路（永远只导自己的）。"""
    from fastapi import HTTPException

    import totp_store as ts
    # 管理员调用也不跳过鉴权
    user = _FakeUser(role="admin", password="hashed-ok")
    monkeypatch.setattr(totp_routes, "verify_password", lambda p, h: False)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(totp_routes.export_totp_credentials(
            totp_routes.TotpExportReq(password="wrong"), user))
    assert ei.value.status_code == 401

    # 鉴权通过 → 导出的是 str(user.id) 的数据（不是任意 owner）
    monkeypatch.setattr(totp_routes, "verify_password", lambda p, h: True)
    seen = {}

    async def fake_export(owner):
        seen["owner"] = owner
        return [{"issuer": "Google", "account": "a@b.c", "secret": "PAFE3CBIQV4VIOWA4KZEWT2DXAU6VY5V",
                 "digits": 6, "period": 30, "algorithm": "SHA1"}]

    monkeypatch.setattr(ts, "export_credentials", fake_export)
    monkeypatch.setattr(totp_routes, "_audit_import_export", lambda *a, **k: __import__("asyncio").sleep(0))
    resp = asyncio.run(totp_routes.export_totp_credentials(
        totp_routes.TotpExportReq(password="pw"), user))
    assert resp["ok"] is True
    assert seen["owner"] == str(user.id)  # 恒为调用者自己
    assert resp["content"].startswith("otpauth://totp/")
    assert "PAFE3CBIQV4VIOWA4KZEWT2DXAU6VY5V" in resp["content"]  # 明文密钥在导出内容里
    assert resp["filename"].endswith(".txt")


def test_import_parses_line_by_line(totp_routes, monkeypatch):
    """导入：逐行解析 + 判重 + upsert，坏行报行号不阻断整批。"""
    import totp_store as ts

    existing = {("GitHub", "alice")}  # 已存在 → 覆盖
    upserted = []

    async def fake_exists(owner, issuer, account):
        return (issuer, account) in existing

    async def fake_upsert(owner, **kw):
        upserted.append(kw)
        return {"id": len(upserted)}

    monkeypatch.setattr(ts, "credential_exists", fake_exists)
    monkeypatch.setattr(ts, "upsert_credential", fake_upsert)
    monkeypatch.setattr(totp_routes, "_audit_import_export", lambda *a, **k: __import__("asyncio").sleep(0))

    text = (
        "# comment line\n"
        "otpauth://totp/Google:wuxin1903%40gmail.com?secret=pafe3cbiqv4viowa4kzewt2dxau6vy5v&issuer=Google\n"
        "\n"
        "not-a-uri\n"
        "otpauth://totp/GitHub:alice?secret=JBSWY3DPEHPK3PXP&issuer=GitHub\n"
    )
    resp = asyncio.run(totp_routes.import_totp_credentials(
        totp_routes.TotpImportReq(content=text), _FakeUser()))
    assert resp["ok"] is True
    assert resp["imported"] == 1  # Google 是新条目
    assert resp["overwritten"] == 1  # GitHub 已存在 → 覆盖
    assert any(e["line"] == 4 and "otpauth" in e["reason"] for e in resp["errors"])  # 坏行报行号
    assert resp["error_count"] == 1
    # upsert 收到解析后的规范化字段（大写 secret、issuer、digits）
    assert upserted[0]["secret"] == "PAFE3CBIQV4VIOWA4KZEWT2DXAU6VY5V"
    assert upserted[0]["issuer"] == "Google"
    assert upserted[0]["domains"] == ["google"]  # 未给 domains → 用 issuer 小写兜底


def test_import_uses_custom_domains_over_issuer(totp_routes, monkeypatch):
    """传了 domains 就批量覆盖每条的关联网站（不回退 issuer）。"""
    import totp_store as ts

    async def fake_exists(owner, issuer, account):
        return False

    async def fake_upsert(owner, **kw):
        return {"id": 1}

    monkeypatch.setattr(ts, "credential_exists", fake_exists)
    monkeypatch.setattr(ts, "upsert_credential", fake_upsert)
    monkeypatch.setattr(totp_routes, "_audit_import_export", lambda *a, **k: __import__("asyncio").sleep(0))

    text = "otpauth://totp/Google:a@b.c?secret=PAFE3CBIQV4VIOWA4KZEWT2DXAU6VY5v&issuer=Google"
    resp = asyncio.run(totp_routes.import_totp_credentials(
        totp_routes.TotpImportReq(content=text, domains=["example.com", "www.example.com"]), _FakeUser()))
    assert resp["ok"] and resp["imported"] == 1


def test_import_quota_error_reported_once(totp_routes, monkeypatch):
    """配额超限：一次导入里同码只归并一次，避免几十行重复错误盖住真问题。"""
    import totp_store as ts

    async def fake_exists(owner, issuer, account):
        return False

    async def fake_upsert(owner, **kw):
        raise ValueError("totp_quota_exceeded")

    monkeypatch.setattr(ts, "credential_exists", fake_exists)
    monkeypatch.setattr(ts, "upsert_credential", fake_upsert)
    monkeypatch.setattr(totp_routes, "_audit_import_export", lambda *a, **k: __import__("asyncio").sleep(0))

    lines = "\n".join(f"otpauth://totp/S{i}:u{i}@b.c?secret=PAFE3CBIQV4VIOWA4KZEWT2DXAU6VY5v&issuer=S{i}" for i in range(10))
    resp = asyncio.run(totp_routes.import_totp_credentials(
        totp_routes.TotpImportReq(content=lines), _FakeUser()))
    assert resp["ok"] and resp["imported"] == 0 and resp["error_count"] == 10
    quota_errors = [e for e in resp["errors"] if e.get("quota")]
    assert len(quota_errors) == 1  # 归并成一条


def test_import_export_frontend_has_entry_points():
    """前端契约：管理页有「导入」「备份」入口与调用。"""
    base = Path(__file__).resolve().parent.parent
    client = (base / "user-frontend" / "src" / "api" / "builtinToolsClient.ts").read_text(encoding="utf-8")
    assert "importTotpCredentials" in client
    assert "exportTotpCredentials" in client
    page = (base / "user-frontend" / "src" / "pages" / "console" / "user" / "resources.tsx").read_text(encoding="utf-8")
    assert "importTotpCredentials" in page and "exportTotpCredentials" in page
