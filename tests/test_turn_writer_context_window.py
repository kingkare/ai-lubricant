"""done 帧必须带上「本轮模型的有效上下文上限」，且**以 PG 为事实源**。

历史病根：前端 token 详情拿「会话选中的模型名」去浏览器侧拉的模型目录里 join
``max_context_tokens``。目录请求失败退化成无上限兜底项、自定义模型组名与真实模型名
对不上，都会 join 失败 → 显示「上限未知」，哪怕用户已在 PG 里配了 1M。改成服务端随
done 下发 ``usage.context_window``（agent/turn_writer.resolve_context_window），
取值口径与 ``load_model_catalog_source`` 一致：

* kind='real' 行 → 自身 metadata.max_context_tokens
* kind='custom' 组 → 成员窗口最小值（组能路由到的最小窗口才是安全上限）
* 都没有 → 旧 model_metadata 表 → 内存目录兜底

这里钉死这个优先级；谁把它改回「只读缓存目录」，立刻红。
"""
from __future__ import annotations

import pytest

from agent.turn_writer import ConversationTurnWriter, resolve_context_window


class _FakePool:
    """假 PG 池：只认成员批量查询。"""

    def __init__(self, member_rows):
        self._member_rows = member_rows
        self.queries = 0

    async def fetch(self, sql, *args):
        self.queries += 1
        return self._member_rows


def _patch_pg(monkeypatch, *, group=None, legacy=None, member_rows=()):
    import db as db_module

    pool = _FakePool(list(member_rows))
    monkeypatch.setattr(db_module.PostgresClient, "pool", pool, raising=False)

    async def _get_model_group(name):
        return group

    async def _get_legacy(name):
        return legacy

    monkeypatch.setattr(db_module.PostgresClient, "get_model_group", _get_model_group, raising=False)
    monkeypatch.setattr(
        db_module.PostgresClient, "get_model_max_context_tokens", _get_legacy, raising=False,
    )
    return pool


def _patch_catalog(monkeypatch, value):
    import rate_limiter

    class _Pool:
        @classmethod
        def get_model_info(cls, model_id):
            return {"max_context_tokens": value} if value else {}

    monkeypatch.setattr(rate_limiter, "ModelClientPool", _Pool, raising=False)


@pytest.mark.asyncio
async def test_real_row_uses_own_metadata(monkeypatch):
    """真实模型行：直接取自身配置的 1M，而不是任何缓存/目录值。"""
    _patch_pg(monkeypatch, group={"kind": "real", "models": [], "metadata": {"max_context_tokens": 1_000_000}})
    _patch_catalog(monkeypatch, 128_000)
    assert await resolve_context_window("claude-opus-4-8") == 1_000_000


@pytest.mark.asyncio
async def test_custom_group_takes_member_minimum(monkeypatch):
    """自定义组（组名自己没元数据）：取成员最小窗口，一条 SQL 批量拉齐。"""
    pool = _patch_pg(monkeypatch, group={
        "kind": "custom",
        "models": ["claude-opus-4-8", "grok-4.5"],
        "metadata": {},
    }, member_rows=[
        {"name": "claude-opus-4-8", "ctx": "1000000"},
        {"name": "grok-4.5", "ctx": "265000"},
    ])
    assert await resolve_context_window("opus") == 265_000
    assert pool.queries == 1, "成员窗口应一次批量查询，不能逐成员往返"


@pytest.mark.asyncio
async def test_falls_back_to_legacy_table_then_catalog(monkeypatch):
    """组名查不到 → 旧 model_metadata 表；再没有 → 内存目录兜底（顺序即优先级）。"""
    _patch_pg(monkeypatch, group=None, legacy=524_288)
    _patch_catalog(monkeypatch, 128_000)
    assert await resolve_context_window("some-model") == 524_288

    _patch_pg(monkeypatch, group=None, legacy=None)
    _patch_catalog(monkeypatch, 128_000)
    assert await resolve_context_window("some-model") == 128_000


@pytest.mark.asyncio
async def test_pg_failure_never_raises(monkeypatch):
    """PG 炸了不能把回合收口带崩——退回目录，目录也没有就 0。"""
    import db as db_module

    async def _boom(name):
        raise RuntimeError("pg down")

    monkeypatch.setattr(db_module.PostgresClient, "pool", object(), raising=False)
    monkeypatch.setattr(db_module.PostgresClient, "get_model_group", _boom, raising=False)
    _patch_catalog(monkeypatch, 700_000)
    assert await resolve_context_window("opus") == 700_000


@pytest.mark.asyncio
async def test_done_frame_carries_context_window(monkeypatch):
    """done 帧的 usage 与广播帧顶层都要带上上限：同一个 dict 随终态落库，刷新后仍在。"""
    _patch_pg(monkeypatch, group={"kind": "real", "models": [], "metadata": {"max_context_tokens": 1_000_000}})
    writer = ConversationTurnWriter(conversation_id="c", assistant_msg_id=7)
    out = await writer.handle({"type": "done", "model": "claude-opus-4-8", "usage": {"prompt_tokens": 10}})

    assert out["context_window"] == 1_000_000
    assert out["usage"]["context_window"] == 1_000_000
    # apply_frame 收进累积器的是同一个 usage 对象 → caller 的 update_message(usage=writer.usage)
    # 会把它一并落进消息行，历史回放/刷新后 token 详情仍拿得到上限。
    assert writer.usage is out["usage"]


@pytest.mark.asyncio
async def test_done_without_model_is_left_alone(monkeypatch):
    """没有模型名（旧链路）不发查询、不写字段，保持原样。"""
    calls: list[str] = []

    async def _spy(name):
        calls.append(name)
        return 1

    import db as db_module

    monkeypatch.setattr(db_module.PostgresClient, "pool", object(), raising=False)
    monkeypatch.setattr(db_module.PostgresClient, "get_model_group", _spy, raising=False)
    writer = ConversationTurnWriter(conversation_id="c", assistant_msg_id=7)
    out = await writer.handle({"type": "done", "usage": {"prompt_tokens": 1}})

    assert not calls
    assert "context_window" not in out["usage"]
