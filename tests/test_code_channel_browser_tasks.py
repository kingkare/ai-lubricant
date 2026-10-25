"""代码渠道「浏览器操作门 + 渠道定时任务」契约测试（无 PG / 无 Chrome）。

覆盖 plan「删除站点服务，能力移植进代码渠道」Phase 1/2 的框架层：
1. validate_spec：纯任务渠道（无钩子 + 合法 SCHEDULED_TASKS）放行；
2. validate_scheduled_tasks：id 与方法匹配、调度词汇合法、id 不遮蔽钩子；
3. build_adapter_class：SCHEDULED_TASKS 旗标复制 + 9 个浏览器门常驻挂载；
4. 浏览器门路由：p.run_js 等委托 SiteServer 静态方法；p.list_clients 按渠道
   owner_user_id 收窄（原站点 owner 语义的移植）；
5. admin._channel_tasks_meta / _run_channel_task：任务表读取、spec 方法解析
   （同步/异步都支持）、异常只 warning 不抛、无 spec 类不炸。

站点服务（site_runner）整删后，这些是渠道 spec 接管浏览器能力的唯一通路。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from providers.code_spec import (
    CLASS_FLAGS,
    CodeSpecError,
    build_adapter_class,
    validate_scheduled_tasks,
    validate_spec,
)
from providers.custom import CustomProvider


# ==================== validate_spec / validate_scheduled_tasks ====================

def test_scheduled_tasks_in_class_flags():
    assert "SCHEDULED_TASKS" in CLASS_FLAGS


def test_task_only_spec_passes():
    """一个钩子都没写、但任务声明合法 → 放行（纯保活型渠道成立）。"""
    spec = type("Spec", (), {
        "SCHEDULED_TASKS": [{"id": "keepalive", "frequency_unit": "minutes", "frequency_value": 10}],
        "keepalive": staticmethod(lambda p: None),
        "REQUIRES_BASE_URL": False,
    })
    assert validate_spec("t", spec) == {}


def test_no_hooks_no_tasks_still_rejected():
    spec = type("Spec", (), {"some_hook": staticmethod(lambda p: None)})
    with pytest.raises(CodeSpecError, match="未定义任何可识别钩子"):
        validate_spec("t", spec)


def test_hooks_plus_tasks_validated():
    """有钩子的 spec 也校验任务声明（防 id 拼错静默不跑）。"""
    spec = type("Spec", (), {
        "headers": staticmethod(lambda p, h, kw: h),
        "SCHEDULED_TASKS": [{"id": "nonexistent", "frequency_unit": "minutes", "frequency_value": 5}],
    })
    with pytest.raises(CodeSpecError, match="nonexistent"):
        validate_spec("t", spec)


def test_task_method_missing_rejected():
    spec = type("Spec", (), {
        "SCHEDULED_TASKS": [{"id": "missing_fn", "frequency_unit": "daily", "daily_time": "10:30"}],
    })
    with pytest.raises(CodeSpecError, match="没有同名方法"):
        validate_scheduled_tasks("t", spec)


def test_task_bad_unit_rejected():
    spec = type("Spec", (), {
        "SCHEDULED_TASKS": [{"id": "fn", "frequency_unit": "weekly"}],
        "fn": staticmethod(lambda p: None),
    })
    with pytest.raises(CodeSpecError, match="frequency_unit"):
        validate_scheduled_tasks("t", spec)


def test_task_daily_time_rejected():
    spec = type("Spec", (), {
        "SCHEDULED_TASKS": [{"id": "fn", "frequency_unit": "daily", "daily_time": "25:99"}],
        "fn": staticmethod(lambda p: None),
    })
    with pytest.raises(CodeSpecError, match="daily_time"):
        validate_scheduled_tasks("t", spec)


def test_task_interval_value_rejected():
    spec = type("Spec", (), {
        "SCHEDULED_TASKS": [{"id": "fn", "frequency_unit": "minutes", "frequency_value": 0}],
        "fn": staticmethod(lambda p: None),
    })
    with pytest.raises(CodeSpecError, match="frequency_value"):
        validate_scheduled_tasks("t", spec)


def test_task_id_shadowing_hook_rejected():
    spec = type("Spec", (), {
        "SCHEDULED_TASKS": [{"id": "headers", "frequency_unit": "minutes", "frequency_value": 5}],
        "headers": staticmethod(lambda p, h, kw: h),
    })
    with pytest.raises(CodeSpecError, match="冲突"):
        validate_scheduled_tasks("t", spec)


def test_task_id_duplicate_rejected():
    spec = type("Spec", (), {
        "SCHEDULED_TASKS": [
            {"id": "fn", "frequency_unit": "minutes", "frequency_value": 5},
            {"id": "fn", "frequency_unit": "hours", "frequency_value": 1},
        ],
        "fn": staticmethod(lambda p: None),
    })
    with pytest.raises(CodeSpecError, match="重复"):
        validate_scheduled_tasks("t", spec)


def test_tasks_normalized():
    spec = type("Spec", (), {
        "SCHEDULED_TASKS": [
            {"id": "a", "frequency_unit": "daily", "daily_time": "08:30", "enabled": False},
            {"id": "b", "frequency_unit": "hours", "frequency_value": 2},
        ],
        "a": staticmethod(lambda p: None),
        "b": staticmethod(lambda p: None),
    })
    out = validate_scheduled_tasks("t", spec)
    assert out == [
        {"id": "a", "frequency_unit": "daily", "daily_time": "08:30", "enabled": False},
        {"id": "b", "frequency_unit": "hours", "frequency_value": 2},
    ]


def test_baseprovider_subclass_still_rejected():
    class Bad(CustomProvider):
        pass
    with pytest.raises(CodeSpecError):
        validate_spec("t", Bad)


# ==================== build_adapter_class：旗标 + 门 ====================

_DOOR_NAMES = (
    "run_js", "open_tab", "find_tab", "set_default_tab", "navigate",
    "capture_start", "wait_capture", "capture_stop", "list_clients",
)


def test_adapter_mounts_doors_and_flag():
    spec = type("Spec", (), {
        "headers": staticmethod(lambda p, h, kw: h),
        "SCHEDULED_TASKS": [{"id": "keepalive", "frequency_unit": "minutes", "frequency_value": 10}],
        "keepalive": staticmethod(lambda p: None),
    })
    adapter = build_adapter_class("t", spec)
    assert adapter.SCHEDULED_TASKS == spec.SCHEDULED_TASKS
    for door in _DOOR_NAMES:
        assert callable(getattr(adapter, door)), door


def test_adapter_without_tasks_has_no_flag():
    spec = type("Spec", (), {"headers": staticmethod(lambda p, h, kw: h)})
    adapter = build_adapter_class("t", spec)
    assert not hasattr(adapter, "SCHEDULED_TASKS")


# ==================== 浏览器门路由 ====================

class _FakeChannel:
    def __init__(self, cfg):
        self._raw = dict(cfg)

    def get(self, key, default=None):
        return self._raw.get(key, default)


@pytest.fixture
def fake_site_server(monkeypatch):
    """monkeypatch SiteServer 静态方法，记录调用参数。"""
    import mcp_builtin.site_server as ss

    calls: list[tuple[str, tuple, dict]] = []

    def _recorder(name, ret=None):
        async def fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return ret
        return staticmethod(fn)

    for m in ("run_js", "open_tab", "find_tab", "set_default_tab", "navigate",
              "capture_start", "wait_capture", "capture_stop", "list_clients"):
        monkeypatch.setattr(ss.SiteServer, m, _recorder(m, ret=f"{m}-ret"))
    return calls


def _make_instance(adapter_cls):
    inst = adapter_cls.__new__(adapter_cls)
    return inst


def test_doors_delegate_to_site_server(fake_site_server):
    spec = type("Spec", (), {"headers": staticmethod(lambda p, h, kw: h)})
    adapter = build_adapter_class("t", spec)
    inst = _make_instance(adapter)
    inst._channel = _FakeChannel({"owner_user_id": "user-1"})

    asyncio.run(inst.run_js("c1", "return 1", tab_id="t9"))
    asyncio.run(inst.open_tab("c1", "https://x"))
    asyncio.run(inst.find_tab("c1", "duck"))
    asyncio.run(inst.navigate("c1", "https://y", tab_id="t1"))
    asyncio.run(inst.capture_start("c1", "/api/x"))
    asyncio.run(inst.wait_capture("c1", "cap-1", timeout=5))
    asyncio.run(inst.capture_stop("c1", "cap-1"))

    names = [c[0] for c in fake_site_server]
    assert names == ["run_js", "open_tab", "find_tab", "navigate",
                     "capture_start", "wait_capture", "capture_stop"]
    assert fake_site_server[0][1] == ("c1", "return 1")
    assert fake_site_server[0][2] == {"tab_id": "t9"}


def test_list_clients_owner_scoped(fake_site_server):
    spec = type("Spec", (), {"headers": staticmethod(lambda p, h, kw: h)})
    adapter = build_adapter_class("t", spec)
    inst = _make_instance(adapter)

    # 有渠道：传渠道 owner_user_id（原站点 owner 语义）
    inst._channel = _FakeChannel({"owner_user_id": "user-abc"})
    asyncio.run(inst.list_clients())
    assert fake_site_server[-1][1] == ("user-abc",)

    # 无渠道：空串（SiteServer 的管理员视图语义，与原 site 一致）
    inst._channel = None
    asyncio.run(inst.list_clients())
    assert fake_site_server[-1][1] == ("",)

    # 渠道 cfg 没 owner：空串
    inst._channel = _FakeChannel({})
    asyncio.run(inst.list_clients())
    assert fake_site_server[-1][1] == ("",)


# ==================== admin 接线：任务表读取 + 执行 ====================

def _load_admin():
    from server import admin
    return admin


def test_channel_tasks_meta_reads_flag():
    admin = _load_admin()
    spec = type("Spec", (), {
        "headers": staticmethod(lambda p, h, kw: h),
        "SCHEDULED_TASKS": [{"id": "keepalive", "frequency_unit": "minutes", "frequency_value": 10}],
        "keepalive": staticmethod(lambda p: None),
    })
    adapter = build_adapter_class("t", spec)
    assert admin._channel_tasks_meta(adapter) == [
        {"id": "keepalive", "frequency_unit": "minutes", "frequency_value": 10}]
    assert admin._channel_tasks_meta(CustomProvider) == []


def test_run_channel_task_sync_and_async(monkeypatch):
    admin = _load_admin()
    ran: dict[str, object] = {}

    spec_sync = type("SpecS", (), {
        "headers": staticmethod(lambda p, h, kw: h),
        "SCHEDULED_TASKS": [{"id": "keepalive", "frequency_unit": "minutes", "frequency_value": 1}],
        "keepalive": staticmethod(lambda p: ran.__setitem__("sync", p)),
    })
    adapter_sync = build_adapter_class("ts", spec_sync)

    async def _tick(p):
        ran["async"] = p

    spec_async = type("SpecA", (), {
        "headers": staticmethod(lambda p, h, kw: h),
        "SCHEDULED_TASKS": [{"id": "tick", "frequency_unit": "minutes", "frequency_value": 1}],
        "tick": staticmethod(_tick),
    })
    adapter_async = build_adapter_class("ta", spec_async)

    async def fake_read(name):
        return {"builtin_type": "code", "code": "x"}

    monkeypatch.setattr(admin, "_read_provider_config", fake_read)
    monkeypatch.setattr(admin, "_build_scanner_instance",
                        lambda name, cfg, **kw: f"p-{name}")

    monkeypatch.setattr(admin, "_get_provider_class", lambda name, cfg: adapter_sync)
    asyncio.run(admin._run_channel_task("ts", {"id": "keepalive", "frequency_unit": "minutes", "frequency_value": 1}))
    assert ran.get("sync") == "p-ts"

    monkeypatch.setattr(admin, "_get_provider_class", lambda name, cfg: adapter_async)
    asyncio.run(admin._run_channel_task("ta", {"id": "tick", "frequency_unit": "minutes", "frequency_value": 1}))
    assert ran.get("async") == "p-ta"


def test_run_channel_task_exception_swallowed(monkeypatch):
    admin = _load_admin()

    def boom(p):
        raise RuntimeError("task exploded")

    spec = type("Spec", (), {
        "headers": staticmethod(lambda p, h, kw: h),
        "SCHEDULED_TASKS": [{"id": "boom", "frequency_unit": "minutes", "frequency_value": 1}],
        "boom": staticmethod(boom),
    })
    adapter = build_adapter_class("tb", spec)

    async def fake_read(name):
        return {"builtin_type": "code", "code": "x"}

    monkeypatch.setattr(admin, "_read_provider_config", fake_read)
    monkeypatch.setattr(admin, "_get_provider_class", lambda name, cfg: adapter)
    monkeypatch.setattr(admin, "_build_scanner_instance", lambda name, cfg, **kw: "p")
    # 不抛 = 通过（异常只 warning）
    asyncio.run(admin._run_channel_task("tb", {"id": "boom", "frequency_unit": "minutes", "frequency_value": 1}))


def test_run_channel_task_no_spec(monkeypatch):
    admin = _load_admin()

    async def fake_read(name):
        return {"builtin_type": "code", "code": "x"}

    monkeypatch.setattr(admin, "_read_provider_config", fake_read)
    monkeypatch.setattr(admin, "_get_provider_class", lambda name, cfg: CustomProvider)
    asyncio.run(admin._run_channel_task("tn", {"id": "x", "frequency_unit": "minutes", "frequency_value": 1}))


def test_action_due_semantics_match_site_register_task():
    """渠道任务复用 _account_action_due —— 语义必须对齐原站点 task_due：
    last_run=0 立即到期；minutes/hours 按间隔；daily 按自然日时刻。"""
    admin = _load_admin()
    now = time.time()
    assert admin._account_action_due(0, {"frequency_unit": "minutes", "frequency_value": 10}, now) is True
    # 间隔未到（500s < 10min=600s）→ 不到点
    assert admin._account_action_due(now - 500, {"frequency_unit": "minutes", "frequency_value": 10}, now) is False
    # 间隔已过（700s > 600s）→ 到点
    assert admin._account_action_due(now - 700, {"frequency_unit": "minutes", "frequency_value": 10}, now) is True
    assert admin._account_action_due(now - 7200, {"frequency_unit": "hours", "frequency_value": 1}, now) is True
