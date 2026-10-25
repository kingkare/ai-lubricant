"""节点载体（Node Carrier）框架的契约测试。

覆盖三块：
1. ``carrier_loader``：载入 spec、MANIFEST 校验、钩子签名校验、错误报文。
2. ``models_carrier``：保活策略取值（前端/路由共用的三选一）。
3. ``carrier_service`` 的 ctx 契约：install prompt 是两发命令、可字节对账。

载体 spec 源码在 ``specs/carriers/``（specs/ 整棵被 .gitignore——载体 spec 与代码
渠道 spec 同一口径：本地管理、DB 真源，出厂默认灌库时落一份）。测试直接按文件
路径读它们（见 :func:`_spec_path` / :func:`_spec_module`），不经包导入。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from user_platform.carrier_loader import (
    ALL_HOOKS,
    CarrierSpecError,
    load_carrier_class,
    invalidate_cache,
    hook_names,
    validate_manifest,
)
from user_platform.models_carrier import (
    KEEPALIVE_POLICIES,
    KEEPALIVE_OFF,
    KEEPALIVE_ON_DEMAND,
    KEEPALIVE_TIMED,
)

_SPECS_DIR = Path(__file__).resolve().parent.parent / "specs" / "carriers"


def _spec_path(name: str) -> Path:
    """出厂载体 spec 文件路径（specs/carriers/<name>.py）。"""
    return _SPECS_DIR / f"{name}.py"


def _spec_module(name: str):
    """把 specs/carriers/<name>.py 作为模块加载一次，返回模块对象（取模块级函数用）。

    spec 源码里有 ``from user_platform.models_carrier import …`` 这类**绝对导入**，
    所以不能用简单 exec——用 importlib 按文件加载，让它跑在一个真模块命名空间里。
    （相对导入在 exec 命名空间里必然 ImportError，但绝对导入在这里可用。）
    """
    path = _spec_path(name)
    spec = importlib.util.spec_from_file_location(f"carriers_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOOD_SPEC = """
class DemoCarrier:
    MANIFEST = {"name": "demo", "label": "演示载体", "fields": []}

    @classmethod
    async def provision(cls, ctx):
        return None

    @classmethod
    async def bootstrap(cls, ctx):
        return {"note": "ok"}

    @classmethod
    async def keepalive(cls, ctx):
        return {"ok": True, "note": "在线"}
"""


def _load(name: str, source: str):
    try:
        return load_carrier_class(name, source)
    finally:
        pass


# ── 载体 spec 载入 ──────────────────────────────────────────────────────────────

def test_load_carrier_class_extracts_hooks_and_manifest():
    invalidate_cache("demo")
    cls = load_carrier_class("demo", GOOD_SPEC)
    assert cls.MANIFEST["name"] == "demo"
    assert set(hook_names(cls)) >= {"provision", "bootstrap"}
    # MANIFEST 不是钩子，不该进 hook_names
    assert "MANIFEST" not in hook_names(cls)


def test_load_is_cached_by_sha1():
    invalidate_cache("demo")
    a = load_carrier_class("demo", GOOD_SPEC)
    b = load_carrier_class("demo", GOOD_SPEC)
    assert a is b, "同源码必须命中缓存（不重复 exec）"
    c = load_carrier_class("demo", GOOD_SPEC.replace("演示载体", "改过的"))
    assert c is not a, "源码变了必须重 exec"


def test_empty_source_rejected():
    with pytest.raises(CarrierSpecError):
        load_carrier_class("demo", "   ")


def test_syntax_error_reported_with_line():
    with pytest.raises(CarrierSpecError) as e:
        load_carrier_class("bad_syntax", "class X:\n    def broken(:\n")
    assert "语法错误" in str(e.value)


def test_missing_class_rejected():
    with pytest.raises(CarrierSpecError) as e:
        load_carrier_class("no_class", "X = 1\n")
    assert "未定义任何 spec 类" in str(e.value)


def test_multiple_classes_rejected():
    src = "class A:\n    def m(self): pass\n\nclass B:\n    def n(self): pass\n"
    with pytest.raises(CarrierSpecError) as e:
        load_carrier_class("two_classes", src)
    assert "只能定义一个类" in str(e.value)


def test_missing_required_hooks_rejected():
    src = "class OnlyProvision:\n    MANIFEST = {'name':'x','label':'y'}\n" \
          "    async def provision(cls, ctx):\n        return None\n"
    with pytest.raises(CarrierSpecError) as e:
        load_carrier_class("no_bootstrap", src)
    assert "缺少必需钩子" in str(e.value)


def test_sync_hook_rejected():
    src = ("class SyncCarrier:\n"
           "    MANIFEST = {'name':'s','label':'s'}\n"
           "    async def provision(cls, ctx): return None\n"
           "    async def bootstrap(cls, ctx): return {}\n"
           "    def keepalive(cls, ctx): return {}   # 漏了 async\n")
    with pytest.raises(CarrierSpecError) as e:
        load_carrier_class("sync_hook", src)
    assert "必须是 async def" in str(e.value)


def test_unknown_hook_rejected():
    """拼错的钩子名不能静默不生效（最难查的一类 bug）。"""
    src = ("class Typo:\n"
           "    MANIFEST = {'name':'t','label':'t'}\n"
           "    async def provision(cls, ctx): return None\n"
           "    async def bootstrap(cls, ctx): return {}\n"
           "    async def keepalve(cls, ctx): return {}   # 少个 i\n")
    with pytest.raises(CarrierSpecError) as e:
        load_carrier_class("typo_hook", src)
    assert "未识别的钩子" in str(e.value) and "keepalve" in str(e.value)


def test_extra_positional_param_rejected():
    src = ("class Extra:\n"
           "    MANIFEST = {'name':'e','label':'e'}\n"
           "    async def provision(cls, ctx, something): return None\n"
           "    async def bootstrap(cls, ctx): return {}\n")
    with pytest.raises(CarrierSpecError) as e:
        load_carrier_class("extra_param", src)
    assert "形参" in str(e.value)


def test_manifest_validation():
    with pytest.raises(CarrierSpecError):
        validate_manifest("not a dict", "x")
    with pytest.raises(CarrierSpecError):
        validate_manifest({"name": "x"}, "x")          # 缺 label
    with pytest.raises(CarrierSpecError):
        validate_manifest({"name": "x", "label": "y", "fields": "nope"}, "x")  # fields 非 list
    assert validate_manifest({"name": "x", "label": "y"}, "x")["name"] == "x"


# ── 内置 spec：AutoClaw 沙箱载体 ────────────────────────────────────────────────

def test_autoclaw_builtin_spec_loads():
    path = _spec_path("autoclaw_sandbox")
    if not path.exists():
        pytest.skip("specs/carriers/autoclaw_sandbox.py 缺失（本地管理）")
    invalidate_cache("autoclaw_sandbox")
    cls = load_carrier_class("autoclaw_sandbox", path.read_text(encoding="utf-8"))
    m = cls.MANIFEST
    assert m["name"] == "autoclaw_sandbox"
    assert m["role_supported"] == ["execution"]
    # 等拨回预算必须够长（下 28MB + 189MB + 起 daemon）
    assert int(m["register_timeout_seconds"]) >= 300
    # 默认保活：按需 + 8 分钟（必须小于 AutoClaw 的 10 分钟 access_hold）
    d = m["default_keepalive"]
    assert d["policy"] == KEEPALIVE_ON_DEMAND
    assert 0 < int(d["interval_minutes"]) < 10
    assert set(hook_names(cls)) >= {"provision", "bootstrap", "keepalive", "status", "teardown"}
    # 表单字段都得有 key/label，否则前端渲染空行
    for f in m["fields"]:
        assert f.get("key") and f.get("label"), f


def test_autoclaw_builtin_tail_int():
    mod = _spec_module("autoclaw_sandbox")

    assert mod._tail_int("6728 /tmp/node-install.sh") == 6728
    assert mod._tail_int("执行完毕，输出为\n  6728 /tmp/x\n") == 6728
    assert mod._tail_int("") == -1
    assert mod._tail_int("没有数字这行") == -1


# ── 上游偶发的有界重试（第三方 AI 会 502，不能整发作废）─────────────────────────

def test_builtin_specs_have_no_relative_imports():
    """出厂 spec 里**禁止相对导入**——exec 命名空间不在任何包内，`from .x import y`
    必然 ImportError；而钩子里的 try/except 会把失败静默吞掉，表现为记账/落库永远
    不生效（2026-09-30 实证：_bump_attempts 的 bootstrap_attempts 从未写进 DB，
    keepalive 的「重跑 3 次后交人工」上限形同虚设）。必须写绝对导入。"""
    import re as _re

    if not _SPECS_DIR.is_dir():
        pytest.skip("specs/carriers 目录缺失")
    offenders = []
    for path in sorted(_SPECS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if _re.match(r"\s*from\s+\.(\s|\.|\w|$)", line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "出厂 spec 出现相对导入（exec 后必然失败）：" + "; ".join(offenders)


def test_autoclaw_bump_attempts_persists_through_exec_namespace(monkeypatch):
    """_bump_attempts 走 exec 出来的类也必须真的落库（相对导入静默失败的回归）。"""
    import types as _t

    mod = _spec_module("autoclaw_sandbox")
    from user_platform import models_carrier

    updates: list[dict] = []

    class _FakeQS:
        # Tortoise 的 Model.filter() 是**同步**的（返回 QuerySet），只有 .update() 才
        # 是协程——桩写成 async def 会让 .update 打在协程上，AttributeError 被
        # _bump_attempts 的 except 吞掉，测试变成假失败。
        calls: list = []

        @classmethod
        def filter(cls, **kw):
            cls.calls.append(kw)

            async def _update(**fields):
                updates.append(fields)
                return 1

            return _t.SimpleNamespace(update=_update)

    monkeypatch.setattr(models_carrier, "NodeCarrierBinding", _FakeQS)

    class _Ctx:
        node_id = "node-x"
        binding = _t.SimpleNamespace(bootstrap_attempts=0, bootstrap_last_error="")

    ctx = _Ctx()
    asyncio.run(mod._bump_attempts(ctx, 2, "boom"))
    assert _FakeQS.calls == [{"pk": "node-x"}]
    assert updates == [{"bootstrap_attempts": 2, "bootstrap_last_error": "boom"}]
    assert ctx.binding.bootstrap_attempts == 2


def test_autoclaw_retry_classifier():
    mod = _spec_module("autoclaw_sandbox")
    _is_retryable = mod._is_retryable

    # 上游 5xx / 网络 / 超时 → 可重试（E2E 实测的 502 就在这类）
    assert _is_retryable(RuntimeError(
        "CarrierError: AI 渠道 autoclaw 调用失败: HTTPException: 502: "
        "AutoClaw 会话错误: LLM request failed."))
    assert _is_retryable(RuntimeError("AI 渠道 autoclaw 超时（300s）未返回"))
    assert _is_retryable(RuntimeError("AI 渠道 autoclaw 没返回正文（空响应）"))

    # 4xx / 配置错 → 立刻失败，别白烧次数
    assert not _is_retryable(RuntimeError(
        "CarrierError: AI 渠道 autoclaw 调用失败: HTTPException: 404: Not Found"))
    # 402 积分不足（spec 会把「LLM request failed 且余额 0」升级成 402）：
    # 重试也不会自己好，必须等签到/充值
    assert not _is_retryable(RuntimeError(
        "CarrierError: AI 渠道 autoclaw 调用失败: HTTPException: 402: AutoClaw 积分不足（等待积分恢复"))
    assert not _is_retryable(RuntimeError("CarrierError: 代码渠道 autoclaw 不存在或未加载"))
    assert not _is_retryable(RuntimeError("渠道 autoclaw 没有可用账号"))
    assert not _is_retryable(RuntimeError("未指定模型（config.model）"))


def test_autoclaw_chat_retries_transient_then_succeeds(monkeypatch):
    """502 重来、4xx 不重来；成功那次的正文原样返回。"""
    mod = _spec_module("autoclaw_sandbox")

    # 退避压到 0：测的是「重不重试」，不是「等多久」
    monkeypatch.setattr(mod, "_RETRY_BASE_DELAY", 0.0)

    class _Ctx:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls = 0
            self.progress: list[tuple] = []

        async def call_channel_chat(self, provider, prompt, *, model, username, timeout=300.0):
            self.calls += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def set_progress(self, step, status, message=""):
            self.progress.append((step, status, message))

    transient = RuntimeError(
        "CarrierError: AI 渠道 autoclaw 调用失败: HTTPException: 502: LLM request failed.")

    # 两次 502 后成功：重试到第 3 次才返回
    ctx = _Ctx([transient, transient, "6728 /tmp/node-install.sh"])
    text = asyncio.run(mod._chat_with_retry(
        ctx, "autoclaw", "prompt", model="m", username="", what="落盘"))
    assert text == "6728 /tmp/node-install.sh"
    assert ctx.calls == 3
    assert any("重试" in (msg or "") for _, _, msg in ctx.progress)

    # 4xx 一次就抛，绝不重试
    ctx = _Ctx([RuntimeError(
        "CarrierError: AI 渠道 autoclaw 调用失败: HTTPException: 404: Not Found")])
    with pytest.raises(RuntimeError):
        asyncio.run(mod._chat_with_retry(
            ctx, "autoclaw", "prompt", model="m", username="", what="落盘"))
    assert ctx.calls == 1

    # 一直 502 → 到 BOOTSTRAP_RETRIES 次就停（有界）
    ctx = _Ctx([transient] * 10)
    with pytest.raises(RuntimeError):
        asyncio.run(mod._chat_with_retry(
            ctx, "autoclaw", "prompt", model="m", username="", what="落盘"))
    assert ctx.calls == mod.BOOTSTRAP_RETRIES


# ── 保活策略取值 ────────────────────────────────────────────────────────────────

def test_keepalive_policies_are_three():
    assert set(KEEPALIVE_POLICIES) == {KEEPALIVE_OFF, KEEPALIVE_ON_DEMAND, KEEPALIVE_TIMED}
    assert KEEPALIVE_POLICIES[0] in ("off", "on_demand", "timed")


def test_all_hooks_tuple_is_stable():
    """钩子表被 MANIFEST/路由/前端共用，改名要显式改测试。"""
    assert set(ALL_HOOKS) == {
        "MANIFEST", "provision", "bootstrap", "keepalive", "status", "teardown", "logs",
    }


# ── 路由注册 ────────────────────────────────────────────────────────────────────

def test_carrier_routes_registered():
    """载体的全部端点都得挂上（漏一个前端就 404）。"""
    from user_platform.routes_carriers import admin_router

    got = {r.path for r in admin_router.routes}
    assert "/api/v1/admin/node-carriers" in got
    assert "/api/v1/admin/node-carriers/specs/{name}" in got
    assert "/api/v1/admin/nodes/{node_id}/carrier" in got
    assert "/api/v1/admin/nodes/{node_id}/carrier/deploy" in got
    assert "/api/v1/admin/nodes/{node_id}/carrier/keepalive" in got
    assert "/api/v1/admin/nodes/{node_id}/carrier/policy" in got
    assert "/api/v1/admin/nodes/{node_id}/carrier/teardown" in got


def test_carrier_routes_do_not_collide_with_studio():
    """载体前缀不能和创空间撞车（两者同在 /api/v1/admin）。"""
    from user_platform.routes_carriers import admin_router as carrier_router
    from user_platform.routes_studio import admin_router as studio_router

    carrier_paths = {(tuple(sorted(r.methods)), r.path) for r in carrier_router.routes}
    studio_paths = {(tuple(sorted(r.methods)), r.path) for r in studio_router.routes}
    assert not (carrier_paths & studio_paths)


# ── 自定义管理节点：代码挂在管理节点行上 ────────────────────────────────────────

def test_load_spec_inline_source_resolves_without_db():
    """resolve_spec 的「source 非空」分支不走 DB：直接 load_carrier_class。"""
    import types as _t

    from user_platform import carrier_service as cs

    binding = _t.SimpleNamespace(
        node_id="node-mgr", carrier_name="custom", source=GOOD_SPEC, manager_node_id="")
    cls = asyncio.run(cs.load_spec_for_binding(binding))
    assert cls.MANIFEST["name"] == "demo"


def test_carrier_manager_routes_registered():
    """虚拟管理节点路由：全套 CRUD + 子节点 + options；老自定义路由已删。"""
    from user_platform.routes_carriers import admin_router

    got = {r.path for r in admin_router.routes}
    for path in ("/api/v1/admin/node-managers",
                 "/api/v1/admin/node-managers/{manager_id}",
                 "/api/v1/admin/node-managers/{manager_id}/children",
                 "/api/v1/admin/node-managers/{manager_id}/children/{child_id}",
                 "/api/v1/admin/node-carriers/specs/{name}/options"):
        assert path in got, f"缺少 {path}"
    # 老「把代码挂到真实节点」的路由已删（不建真实管理节点）
    assert "/api/v1/admin/nodes/{node_id}/carrier/custom" not in got
    assert "/api/v1/admin/nodes/{node_id}/carrier/source" not in got
    assert "/api/v1/admin/nodes/{manager_node_id}/carrier/children" not in got


def test_resolve_spec_source_priority_over_carrier_name():
    """source 非空时直接用 source，忽略 carrier_name（避免冲突）。"""
    import types as _t

    from user_platform import carrier_service as cs

    # carrier_name 是 "will_be_ignored"，但 source 指向 GOOD_SPEC → 必须走 source
    binding = _t.SimpleNamespace(
        node_id="node-x", carrier_name="will_be_ignored", source=GOOD_SPEC, manager_node_id="")
    cls = asyncio.run(cs.load_spec_for_binding(binding))
    assert cls.MANIFEST["name"] == "demo"


# ── 虚拟管理节点 + spec 声明式 UI ──────────────────────────────────────────────

def test_manual_spec_loads_without_carrier_hooks():
    """deploy_mode='manual' 的纯声明式 spec（可管理/分组容器）**不要求载体钩子**。"""
    from user_platform.carrier_loader import load_carrier_class as _load

    base = _SPECS_DIR
    for name, requires_client in (("manageable", True), ("grouping", False)):
        invalidate_cache(name)
        cls = _load(name, (base / f"{name}.py").read_text(encoding="utf-8"))
        m = cls.MANIFEST
        assert m["deploy_mode"] == "manual"
        assert m["requires_client"] is requires_client
        # 没有载体钩子也不算错（manual 模式）
        assert set(hook_names(cls)) == set()


def test_manifest_declares_ui_schema():
    """四个出厂 spec 都声明了 manager_fields / child_tabs / detail_tabs / movable。"""
    from user_platform.carrier_loader import load_carrier_class as _load

    base = _SPECS_DIR
    expect = {
        "manageable": (0, 1, True),
        "grouping": (0, 1, True),
        "modelscope_studio": (1, 1, False),   # token 归管理节点；子节点锁死不可移动
        "autoclaw_sandbox": (3, 1, True),
    }
    for name, (n_mgr_fields, n_child_tabs, movable) in expect.items():
        invalidate_cache(name)
        cls = _load(name, (base / f"{name}.py").read_text(encoding="utf-8"))
        m = cls.MANIFEST
        assert len(m.get("manager_fields") or []) == n_mgr_fields, name
        assert len(m.get("child_tabs") or []) == n_child_tabs, name
        assert m.get("movable") is movable, name
        # child_tabs 每项要有 name/label/kind
        for t in m["child_tabs"]:
            assert t.get("name") and t.get("label") and t.get("kind"), (name, t)


def test_detail_tabs_new_shape():
    """detail_tabs 新形态：is_home 概览带 + data 数据源 + fields 表达式 + panel 组件名。"""
    from user_platform.carrier_loader import load_carrier_class as _load

    for name in ("modelscope_studio", "autoclaw_sandbox", "grouping"):
        invalidate_cache(name)
        cls = _load(name, (_SPECS_DIR / f"{name}.py").read_text(encoding="utf-8"))
        tabs = cls.MANIFEST.get("detail_tabs") or []
        assert tabs, name
        home = [t for t in tabs if t.get("is_home")]
        assert len(home) == 1, f"{name}: 恰好一个 is_home 概览带"
        assert home[0].get("data", {}).get("source"), f"{name}: 概览带要声明 data.source"
        assert home[0].get("fields"), f"{name}: 概览带要有 fields"
        for f in home[0]["fields"]:
            assert f.get("label"), (name, f)
        # 非 is_home 的 tab 走 panel（交互组件），不塞 fields
        for t in tabs:
            if not t.get("is_home"):
                assert t.get("panel"), f"{name}: 非概览 tab 应声明 panel——{t}"


def test_manifest_validation_rejects_bad_ui_schema():
    """声明式 UI 键形状错 → 400 级错误（别让前端渲染时才炸）。"""
    from user_platform.carrier_loader import validate_manifest as _v

    ok = {"name": "x", "label": "y", "child_tabs": [{"name": "a", "label": "A"}]}
    assert _v(ok, "x") is ok

    with pytest.raises(CarrierSpecError):
        _v({"name": "x", "label": "y", "child_tabs": [{"label": "无 name"}]}, "x")
    with pytest.raises(CarrierSpecError):
        _v({"name": "x", "label": "y", "manager_fields": "not-a-list"}, "x")
    with pytest.raises(CarrierSpecError):
        _v({"name": "x", "label": "y", "deploy_mode": "weird"}, "x")


def test_resolve_spec_options_finds_child_tab_fields(monkeypatch):
    """options_source 在 child_tabs[].fields 里也要能找到（回归）。

    创空间的规格/镜像/SDK 写在 child_tabs 里，旧实现只查顶层 fields → 必然
    报「没有字段 hardware」，下拉永远拉不到。
    """
    from user_platform import carrier_service as cs

    _patch_load_spec(monkeypatch)
    # modelscope_studio 的 hardware 在 child_tabs[0].fields，options_source=studio.hardware
    calls = {"n": 0}

    async def fake_handler(config, params):
        calls["n"] += 1
        return {"options": [{"label": "cpu.basic", "value": "cpu.basic"}], "error": ""}

    monkeypatch.setitem(cs._OPTIONS_SOURCES, "studio.hardware", fake_handler)
    got = asyncio.run(cs.resolve_spec_options("modelscope_studio", "hardware"))
    assert calls["n"] == 1, "child_tabs 里的字段应被找到并解析"
    assert got["options"][0]["value"] == "cpu.basic"
    # manager_fields 里的字段同样可解析
    got2 = asyncio.run(cs.resolve_spec_options("autoclaw_sandbox", "provider"))
    assert got2["error"] == "" and got2["options"] == []   # provider 是 text，无 options_source


def _builtin_class(name: str):
    """从 specs/carriers/ 加载出厂 spec 类（绕开 DB，测试用）。"""
    from user_platform.carrier_loader import load_carrier_class as _load

    path = _spec_path(name)
    invalidate_cache(name)
    return _load(name, path.read_text(encoding="utf-8"))


def _patch_load_spec(monkeypatch):
    """把 carrier_service.load_spec 换成「从出厂文件加载」，避免测试碰 DB。"""
    from user_platform import carrier_service as cs

    async def fake_load_spec(name: str):
        return _builtin_class(name)

    monkeypatch.setattr(cs, "load_spec", fake_load_spec)


def test_resolve_spec_manager_points_to_builtin_spec(monkeypatch):
    """子节点回指**内置载体**的管理节点（source 为空）也能解析出 spec。

    回归：旧实现强制要求 mgr.source 非空 → 创空间/分组容器这类内置 spec 管理节点
    的子节点直接报错「没有载体代码」。
    """
    import types as _t

    from user_platform import carrier_service as cs
    from user_platform import models_carrier

    _patch_load_spec(monkeypatch)
    mgr = _t.SimpleNamespace(node_id="mgr-1", carrier_name="modelscope_studio", source="")

    async def fake_get_or_none(**kw):
        return mgr if kw.get("node_id") == "mgr-1" else None

    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "get_or_none",
                        staticmethod(fake_get_or_none))
    cls = asyncio.run(cs.resolve_spec(manager_node_id="mgr-1", node_id="node-c"))
    assert cls.MANIFEST["name"] == "modelscope_studio"


def test_create_node_manager_rejects_requires_client_spec(monkeypatch):
    """requires_client=True 的 spec（可管理）不能建成虚拟管理节点。"""
    from user_platform import carrier_service as cs

    _patch_load_spec(monkeypatch)
    with pytest.raises(cs.CarrierError, match="requires_client"):
        asyncio.run(cs.create_node_manager(spec_name="manageable"))


def test_resolve_spec_options_registry(monkeypatch):
    """options_source 注册表：未注册的 key 回 error 不炸；静态 options 直通。"""
    import types as _t

    from user_platform import carrier_service as cs

    spec_src = """
class OptCarrier:
    MANIFEST = {"name": "opt", "label": "选项载体", "fields": [
        {"key": "static", "label": "静态", "type": "select",
         "options": [{"label": "甲", "value": "a"}]},
        {"key": "dyn", "label": "动态", "type": "select", "options_source": "nope.missing"},
    ]}
    @classmethod
    async def provision(cls, ctx): return None
    @classmethod
    async def bootstrap(cls, ctx): return {}
"""
    from user_platform.carrier_loader import load_carrier_class as _load

    invalidate_cache("opt")
    cls = _load("opt", spec_src)

    async def fake_load_spec(name: str):
        return cls

    monkeypatch.setattr(cs, "load_spec", fake_load_spec)

    got = asyncio.run(cs.resolve_spec_options("opt", "static"))
    assert got["options"] == [{"label": "甲", "value": "a"}] and got["error"] == ""
    bad = asyncio.run(cs.resolve_spec_options("opt", "dyn"))
    assert bad["options"] == [] and "未注册的选项源" in bad["error"]
    missing = asyncio.run(cs.resolve_spec_options("opt", "not_a_field"))
    assert missing["options"] == [] and "没有字段" in missing["error"]


def test_create_node_manager_writes_virtual_row(monkeypatch):
    """创建虚拟管理节点：合成 id mgr-*、is_manager=True、不调控制面。"""
    from user_platform import carrier_service as cs
    from user_platform import models_carrier

    _patch_load_spec(monkeypatch)
    created: list = []

    class _B:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    async def fake_create(**kw):
        created.append(kw)
        return _B(**kw)

    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "create", staticmethod(fake_create))
    out = asyncio.run(cs.create_node_manager(
        name="我的创空间", spec_name="modelscope_studio",
        config={"access_token": "t"}, keepalive_policy=KEEPALIVE_TIMED,
        keepalive_interval_minutes=5))
    assert out["manager_id"].startswith("mgr-")
    assert created and created[0]["is_manager"] is True
    assert created[0]["carrier_name"] == "modelscope_studio"
    assert created[0]["node_id"] == out["manager_id"]
    assert created[0]["name"] == "我的创空间"


def test_delete_node_manager_refuses_with_children(monkeypatch):
    """有子节点时删管理节点 → 拒绝并报子节点数（不级联）。"""
    from user_platform import carrier_service as cs
    from user_platform import models_carrier

    class _M:
        node_id = "mgr-1"

    async def fake_get_or_none(**kw):
        return _M()

    class _QS:
        def filter(self, **kw):
            return self

        def __await__(self):
            async def _():
                return [object(), object()]   # 2 个子节点
            return _().__await__()

    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "get_or_none", staticmethod(fake_get_or_none))
    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "filter",
                        classmethod(lambda cls, **kw: _QS()))
    with pytest.raises(cs.CarrierError, match="2 个子节点"):
        asyncio.run(cs.delete_node_manager("mgr-1"))


def test_keepalive_now_skips_manager_rows(monkeypatch):
    """keepalive_now 对 is_manager=True 行直接返回（管理节点无宿主）。"""
    import types as _t

    from user_platform import carrier_service as cs
    from user_platform import models_carrier

    mgr = _t.SimpleNamespace(node_id="mgr-1", carrier_name="grouping", is_manager=True)

    async def fake_get_or_none(**kw):
        return mgr

    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "get_or_none", staticmethod(fake_get_or_none))
    out = asyncio.run(cs.keepalive_now("mgr-1"))
    assert out["ok"] is True and "管理节点" in out["note"]


def test_seed_builtin_specs_updates_stale_builtin_row(monkeypatch):
    """出厂行（builtin=True）sha1 过期 → 更新；运营改过的（builtin=False）→ 不动。

    回归（2026-09-30）：重试改动写在文件里（12474B），DB 还是老源码（9004B），
    因为旧实现「有行就跳过」——文件改了生产跑的却不是那份，对不上。
    """
    import hashlib as _hashlib
    from user_platform import carrier_service as cs
    from user_platform import models_carrier

    builtin_path = cs._BUILTIN_DIR / "autoclaw_sandbox.py"
    if not builtin_path.exists():
        pytest.skip("specs/carriers/autoclaw_sandbox.py 缺失")
    file_source = builtin_path.read_text(encoding="utf-8")
    file_sha1 = _hashlib.sha1(file_source.encode()).hexdigest()

    class _Row:
        def __init__(self, name, source, sha1, builtin, label=""):
            self.name, self.source, self.sha1 = name, source, sha1
            self.builtin, self.label, self.load_error = builtin, label, "old"
            self.saved = 0

        async def save(self):
            self.saved += 1

    # ① 过期的出厂行 → 必须更新到文件里的新源码
    stale = _Row("autoclaw_sandbox", "<old builtin source>", "0" * 40, True)

    class _Model:
        # 桩按 name 存行：builtin 目录里有多个 spec，对所有 name 返回同一行会让
        # 第二个 spec 把上一个的 source 覆盖掉，断言就对不上了。
        rows: dict = {"autoclaw_sandbox": stale}
        created: list = []

        @classmethod
        async def get_or_none(cls, **kw):
            return cls.rows.get(kw.get("name"))

        @classmethod
        async def create(cls, **kw):
            # 「有行时不该走 create」只对 autoclaw_sandbox 断言；其它出厂 spec 是
            # 新行，走 create 正常。
            if kw.get("name") == "autoclaw_sandbox":
                raise AssertionError("已存在行时不该走 create")
            cls.created.append(kw["name"])
            return _Row(kw["name"], kw["source"], kw["sha1"], True)

    monkeypatch.setattr(models_carrier, "NodeCarrierSpec", _Model)
    monkeypatch.setattr("user_platform.models_carrier.NodeCarrierSpec", _Model)
    n = asyncio.run(cs.seed_builtin_specs())
    assert stale.source == file_source, "过期出厂行未更新到文件源码"
    assert stale.sha1 == file_sha1
    assert stale.saved == 1, "未触发 save（改动没落库）"
    assert stale.load_error == "", "更新后应清掉 load_error"
    assert n >= 1

    # ② 运营改过的行（builtin=False）→ 绝不覆盖
    edited = _Row("autoclaw_sandbox", "# 运营改的版本", "1" * 40, False)

    class _ModelEdited(_Model):
        @classmethod
        async def get_or_none(cls, **kw):
            # 只有 autoclaw_sandbox 有行；modelscope_studio 缺行走 create
            return edited if kw.get("name") == "autoclaw_sandbox" else None

    monkeypatch.setattr(models_carrier, "NodeCarrierSpec", _ModelEdited)
    monkeypatch.setattr("user_platform.models_carrier.NodeCarrierSpec", _ModelEdited)
    asyncio.run(cs.seed_builtin_specs())
    assert edited.source == "# 运营改的版本", "运营改过的出厂行被冲掉了"
    assert edited.saved == 0

    # ③ sha1 未变 → 不动
    fresh = _Row("autoclaw_sandbox", file_source, file_sha1, True)

    class _ModelFresh(_Model):
        @classmethod
        async def get_or_none(cls, **kw):
            # 只有 autoclaw_sandbox 有行；modelscope_studio 缺行走 create
            return fresh if kw.get("name") == "autoclaw_sandbox" else None

    monkeypatch.setattr(models_carrier, "NodeCarrierSpec", _ModelFresh)
    monkeypatch.setattr("user_platform.models_carrier.NodeCarrierSpec", _ModelFresh)
    asyncio.run(cs.seed_builtin_specs())
    assert fresh.saved == 0, "sha1 未变不该重写"


# ── Phase 3：创空间包成载体 spec（只测逻辑，不碰生产数据）───────────────────────

def _load_studio_spec():
    from user_platform.carrier_loader import invalidate_cache, load_carrier_class

    path = _spec_path("modelscope_studio")
    if not path.exists():
        pytest.skip("modelscope_studio spec 缺失")
    invalidate_cache("modelscope_studio")
    return load_carrier_class("modelscope_studio", path.read_text(encoding="utf-8"))


def test_studio_spec_loads():
    cls = _load_studio_spec()
    m = cls.MANIFEST
    assert m["name"] == "modelscope_studio"
    assert m["role_supported"] == ["execution"]
    d = m["default_keepalive"]
    assert d["policy"] == KEEPALIVE_TIMED
    assert int(d["interval_minutes"]) == 5
    assert set(hook_names(cls)) >= {"provision", "bootstrap", "keepalive", "status", "teardown"}


def _patch_studio(monkeypatch, *, row, nodes, keepalive_one):
    """把 spec 依赖的三处打桩：账本行、控制面 list_nodes、keepalive_one。"""
    from user_platform import models_studio, studio_deploy
    from user_platform.node_client import client as ncc

    async def _get_or_none(**kw):
        return row

    async def _list_nodes(self):
        return nodes

    monkeypatch.setattr(models_studio.StudioDeployment, "get_or_none", staticmethod(_get_or_none))
    monkeypatch.setattr(studio_deploy, "keepalive_one", keepalive_one)
    monkeypatch.setattr(ncc.NodeClient, "list_nodes", _list_nodes)


def test_studio_keepalive_delegates_to_keepalive_one(monkeypatch):
    """spec 的 keepalive 必须**委派**给 studio_deploy.keepalive_one（与旧循环同一份实现）。"""
    cls = _load_studio_spec()
    seen: dict = {}

    class _Row:
        node_id = "node-s"
        repo_url = "https://www.modelscope.cn/studios/o/r.git"

    async def fake_keepalive_one(row, *, online: bool):
        seen["online"] = online
        return {"ok": True, "note": "在线" if online else "已处理"}

    _patch_studio(monkeypatch, row=_Row(),
                  nodes=[{"node_id": "node-s", "online": True, "connected": True}],
                  keepalive_one=fake_keepalive_one)

    import types as _t
    out = asyncio.run(cls.keepalive(_t.SimpleNamespace(node_id="node-s")))
    assert out["note"] == "在线"
    assert seen.get("online") is True


def test_studio_keepalive_skips_orphan_without_probing(monkeypatch):
    """孤儿（账本有创空间、台账没节点）必须**不探活**——否则每轮白烧 ModelScope 配额。"""
    cls = _load_studio_spec()
    probed: list = []

    class _Row:
        node_id = "node-gone"
        repo_url = "https://www.modelscope.cn/studios/o/r.git"

    async def fake_keepalive_one(row, *, online: bool):
        probed.append(online)
        return {"ok": True, "note": "x"}

    _patch_studio(monkeypatch, row=_Row(),
                  nodes=[{"node_id": "node-other", "online": False}],
                  keepalive_one=fake_keepalive_one)

    import types as _t
    out = asyncio.run(cls.keepalive(_t.SimpleNamespace(node_id="node-gone")))
    assert probed == [], "孤儿不该走 keepalive_one（探活/唤醒）"
    assert "不在台账" in out["note"] and out["ok"] is True


def test_studio_keepalive_no_deployment_row(monkeypatch):
    """没有创空间部署记录（账本被删）→ 明确报出来，不抛异常打断整轮保活。"""
    cls = _load_studio_spec()

    async def fake_keepalive_one(row, *, online: bool):  # pragma: no cover
        raise AssertionError("无账本行时不该走到 keepalive_one")

    _patch_studio(monkeypatch, row=None, nodes=[], keepalive_one=fake_keepalive_one)
    import types as _t
    out = asyncio.run(cls.keepalive(_t.SimpleNamespace(node_id="node-x")))
    assert out["ok"] is False and "无创空间部署记录" in out["note"]


def test_studio_keepalive_control_plane_down_is_not_a_crash(monkeypatch):
    """控制面不可用：本轮跳过并回 ok=False，不能抛（旧 tick 也是整轮跳过）。"""
    cls = _load_studio_spec()
    from user_platform import models_studio
    from user_platform.node_client import client as ncc

    class _Row:
        node_id = "node-s"
        repo_url = "https://www.modelscope.cn/studios/o/r.git"

    async def fake_get_or_none(**kw):
        return _Row()

    async def boom(self):
        raise RuntimeError("control plane down")

    monkeypatch.setattr(models_studio.StudioDeployment, "get_or_none", staticmethod(fake_get_or_none))
    monkeypatch.setattr(ncc.NodeClient, "list_nodes", boom)
    import types as _t
    out = asyncio.run(cls.keepalive(_t.SimpleNamespace(node_id="node-s")))
    assert out["ok"] is False and "控制面不可用" in out["note"]


def test_studio_binding_helpers_are_idempotent(monkeypatch):
    """ensure/remove 绑定：ensure 已有则不重复建；remove 缺行不报错。"""
    import types as _t

    from user_platform import carrier_service as cs
    from user_platform import models_carrier

    created: list = []

    class _B:
        def __init__(self, node_id, carrier_name="modelscope_studio"):
            self.node_id, self.carrier_name = node_id, carrier_name

    existing = _B("node-a")

    async def fake_get_or_none(**kw):
        return existing if kw.get("node_id") == "node-a" else None

    async def fake_create(**kw):
        created.append(kw)
        return _B(kw["node_id"])

    deleted: list = []

    class _QS:
        @classmethod
        def filter(cls, **kw):
            deleted.append(kw)

            async def _do_delete():
                return 1
            return _t.SimpleNamespace(delete=_do_delete)

    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "get_or_none", staticmethod(fake_get_or_none))
    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "create", staticmethod(fake_create))
    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "filter",
                        classmethod(lambda cls, **kw: _QS.filter(**kw)))

    asyncio.run(cs.ensure_studio_binding("node-a"))
    assert created == [], "已有绑定不该重复建"
    asyncio.run(cs.ensure_studio_binding("node-b"))
    assert len(created) == 1 and created[0]["carrier_name"] == "modelscope_studio"
    assert created[0]["keepalive_policy"] == KEEPALIVE_TIMED
    asyncio.run(cs.remove_studio_binding("node-a"))
    assert deleted == [{"node_id": "node-a"}]


def test_studio_tick_skips_bound_rows(monkeypatch):
    """旧 tick 与载体循环**互斥**：有绑定的行必须跳过（否则双重 deploy 叠加构建）。"""
    from user_platform import models_carrier, models_studio, nodes_service, studio_deploy

    class _Row:
        def __init__(self, node_id):
            self.node_id = node_id
            self.repo_url = "https://www.modelscope.cn/studios/o/r.git"

    studio_rows = [_Row("node-bound"), _Row("node-free")]
    called: list = []

    # Tortoise QuerySet 是 awaitable，await 返回模型列表。桩要模拟这个行为。
    class _QuerySet:
        def __init__(self, rows):
            self.rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def __await__(self):
            # 返回一个协程，yield 出 rows
            async def _():
                return self.rows
            return _().__await__()

        @classmethod
        def filter(cls, **kw):
            return cls(studio_rows)

        def exclude(self, **kw):
            return self  # 简化：exclude 不真过滤

    class _BoundQS:
        @classmethod
        async def values_list(cls, *a, **kw):
            return ["node-bound"]

    async def fake_keepalive_one(row, *, online: bool):
        called.append(row.node_id)
        return {"ok": True, "note": ""}

    async def fake_list_nodes(self):
        return [{"node_id": "node-bound", "online": False},
                {"node_id": "node-free", "online": False}]

    monkeypatch.setattr(models_studio.StudioDeployment, "filter", _QuerySet.filter)
    monkeypatch.setattr(models_carrier.NodeCarrierBinding, "filter",
                        classmethod(lambda cls, **kw: _BoundQS))
    monkeypatch.setattr(studio_deploy, "keepalive_one", fake_keepalive_one)
    monkeypatch.setattr(nodes_service, "get_local_node_client",
                        lambda: type("C", (), {"list_nodes": fake_list_nodes})())

    asyncio.run(studio_deploy._keepalive_tick())
    assert called == ["node-free"], f"有绑定的行不该再跑：{called}"

