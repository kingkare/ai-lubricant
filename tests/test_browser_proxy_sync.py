"""浏览器代理 Profile 同步 / 跨浏览器导入契约（用内存 fake 池，不依赖真 PG）。

覆盖：
- upsert/list 的扩展原生形状往返（fixed/pac/auto 子树）；
- sync_profiles_from_extension 全量替换 + 空列表 no-op 保护 + 删除墓碑；
- import_profiles 同域复制 + id 冲突改名 + 源=目标拒绝；
- list_profiles_for_client_ids 分组。
"""
import asyncio
import re

import pytest

import mcp_builtin.browser_proxy as bp
from server import db


class _FakeConn:
    """asyncpg 连接的内存替身，只实现本模块用到的 SQL 子集。"""

    def __init__(self, store):
        self.store = store  # dict[(client_id, id)] -> row dict

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        if "CREATE TABLE" in s or "CREATE INDEX" in s or "ALTER TABLE" in s:
            return None
        if s.startswith("INSERT INTO browser_proxy_profiles"):
            pid, cid, name, ptype, config, scope = args
            import json
            self.store[(cid, pid)] = {
                "id": pid, "client_id": cid, "name": name, "type": ptype,
                "config": json.loads(config) if isinstance(config, str) else config,
                "scope": scope,
            }
            return "INSERT 0 1"
        if s.startswith("DELETE FROM browser_proxy_profiles WHERE client_id = $1 AND id = $2"):
            cid, pid = args
            existed = self.store.pop((cid, pid), None)
            return "DELETE 1" if existed else "DELETE 0"
        if "NOT (id = ANY($2::text[]))" in s:
            cid, keep = args
            keep = set(keep)
            for k in [k for k in self.store if k[0] == cid and k[1] not in keep]:
                del self.store[k]
            return None
        if "id = ANY($2::text[])" in s:
            cid, ids = args
            n = 0
            for pid in ids:
                if self.store.pop((cid, pid), None) is not None:
                    n += 1
            return f"DELETE {n}"
        if s.startswith("DELETE FROM browser_proxy_profiles WHERE client_id = $1") and "$2" not in s:
            (cid,) = args
            for k in [k for k in self.store if k[0] == cid]:
                del self.store[k]
            return None
        return None

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "id = ANY($1::text[])" in s:
            (cids,) = args
            cids = set(cids)
            rows = [r for (c, _), r in self.store.items() if c in cids]
            rows.sort(key=lambda r: r["id"])
            return rows
        if "WHERE client_id = $1 ORDER BY" in s:
            (cid,) = args
            return [r for (c, _), r in self.store.items() if c == cid]
        return []

    async def fetchrow(self, sql, *args):
        cid, pid = args
        return self.store.get((cid, pid))

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT COUNT(*)"):
            (cid,) = args
            return len([1 for (c, _) in self.store if c == cid])
        if s.startswith("SELECT 1 FROM"):
            cid, pid = args
            return 1 if (cid, pid) in self.store else None
        return None


class _FakePool:
    def __init__(self):
        self.store = {}

    def acquire(self):
        conn = _FakeConn(self.store)

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()


@pytest.fixture()
def fake_pool(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(db.PostgresClient, "pool", pool, raising=False)
    yield pool


def _run(coro):
    return asyncio.run(coro)


FIXED = {"id": "pf1", "name": "Clash", "type": "fixed",
         "fixed": {"scheme": "http", "host": "127.0.0.1", "port": 7890, "bypass": []}}
AUTO = {"id": "pf2", "name": "auto", "type": "auto",
        "auto": {"rules": [{"condition": {"type": "hostWildcard", "pattern": "*.x.com"}, "profileId": "pf1"}],
                 "defaultProfileId": ""}}
DIRECT = {"id": "pf3", "name": "direct", "type": "direct"}


def test_upsert_roundtrip_preserves_native_shape(fake_pool):
    cid = "c1"
    saved = _run(bp.upsert_profile(cid, FIXED))
    assert saved["type"] == "fixed" and saved["fixed"]["port"] == 7890
    got = _run(bp.get_profile(cid, "pf1"))
    # 关键：读回来是扩展原生形状（fixed 子树），不是 config 包裹。
    assert got["fixed"]["host"] == "127.0.0.1"
    assert "config" not in got


def test_upsert_auto_and_direct_shapes(fake_pool):
    cid = "c2"
    _run(bp.upsert_profile(cid, AUTO))
    got = _run(bp.get_profile(cid, "pf2"))
    assert got["auto"]["rules"][0]["condition"]["pattern"] == "*.x.com"
    _run(bp.upsert_profile(cid, DIRECT))
    got2 = _run(bp.get_profile(cid, "pf3"))
    assert got2["type"] == "direct" and "auto" not in got2 and "fixed" not in got2


def test_sync_full_replace(fake_pool):
    cid = "c3"
    _run(bp.upsert_profile(cid, {"id": "old", "name": "old", "type": "direct"}))
    result = _run(bp.sync_profiles_from_extension(cid, [FIXED, AUTO]))
    assert result["noop"] is False and result["replaced"] == 2
    ids = {p["id"] for p in _run(bp.list_profiles(cid))}
    assert ids == {"pf1", "pf2"}


def test_sync_empty_is_noop(fake_pool):
    """空推（本地被清空）不能擦掉服务端真相源。"""
    cid = "c4"
    _run(bp.upsert_profile(cid, FIXED))
    result = _run(bp.sync_profiles_from_extension(cid, [], []))
    assert result["noop"] is True
    assert {p["id"] for p in _run(bp.list_profiles(cid))} == {"pf1"}


def test_sync_with_deleted_tombstone(fake_pool):
    cid = "c5"
    _run(bp.upsert_profile(cid, FIXED))
    _run(bp.upsert_profile(cid, AUTO))
    result = _run(bp.sync_profiles_from_extension(cid, [FIXED], ["pf2"]))
    assert result["deleted"] == 1
    assert {p["id"] for p in _run(bp.list_profiles(cid))} == {"pf1"}


def test_import_profiles_cross_client(fake_pool):
    src, dst = "src", "dst"
    _run(bp.replace_profiles(src, [FIXED, AUTO]))
    _run(bp.replace_profiles(dst, []))
    result = _run(bp.import_profiles(dst, ["pf1"], source_client_id=src))
    assert result["imported"] == 1
    got = _run(bp.get_profile(dst, "pf1"))
    assert got["fixed"]["port"] == 7890
    # 源不受影响
    assert {p["id"] for p in _run(bp.list_profiles(src))} == {"pf1", "pf2"}


def test_import_profiles_id_collision_renames(fake_pool):
    src, dst = "src2", "dst2"
    _run(bp.replace_profiles(src, [FIXED]))
    _run(bp.replace_profiles(dst, [{"id": "pf1", "name": "existing", "type": "direct"}]))
    result = _run(bp.import_profiles(dst, ["pf1"], source_client_id=src))
    assert result["imported"] == 1
    ids = {p["id"] for p in _run(bp.list_profiles(dst))}
    assert "pf1" in ids and len(ids) == 2


def test_import_same_client_rejected(fake_pool):
    with pytest.raises(ValueError):
        _run(bp.import_profiles("same", ["x"], source_client_id="same"))


def test_list_profiles_for_client_ids_groups(fake_pool):
    a, b = "ga", "gb"
    _run(bp.replace_profiles(a, [FIXED]))
    _run(bp.replace_profiles(b, [AUTO]))
    grouped = _run(bp.list_profiles_for_client_ids([a, b]))
    assert {p["id"] for p in grouped[a]} == {"pf1"}
    assert {p["id"] for p in grouped[b]} == {"pf2"}


def test_owner_client_ids_and_scope(monkeypatch):
    """跨浏览器导入的授权边界：list_owner_client_ids 只回该 owner 的 client。"""
    import builtin_tool_store
    calls = {}

    async def fake_list(resource_type, owner_user_id=None, enabled=True):
        calls["owner"] = owner_user_id
        return [{"id": 7}, {"id": 8}]

    monkeypatch.setattr(builtin_tool_store, "list_resources", fake_list)
    ids = _run(bp.list_owner_client_ids("u1"))
    assert ids == ["7", "8"] and calls["owner"] == "u1"
    assert _run(bp.list_owner_client_ids("")) == []