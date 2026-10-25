"""CodeBuddy 合并版代码渠道（specs/codebuddy_code_channel.py）· DEPLOY_REGION 开关契约。

一份文件承载国内/国际两个区域，顶部 `DEPLOY_REGION = "cn" | "intl"` 决定全部区域差异；
代码逻辑零分叉（除 model_sources / fetch_models 外），差异全部经 _PROFILE 表驱动。
本测试锁住「拧开关 = 切到各自现网形态」的契约，防止后续维护把某一侧改坏。
specs/ 在 .gitignore 内、由使用者自持，文件不存在时整模块跳过。
"""
from __future__ import annotations

import asyncio
import importlib.util
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC = Path(__file__).resolve().parent.parent / "specs" / "codebuddy_code_channel.py"
if not _SPEC.exists():
    pytest.skip(f"{_SPEC} 不存在（使用者自持）", allow_module_level=True)

_BASE_SOURCE = _SPEC.read_text(encoding="utf-8")

# 每个区域的现网形态期望（与 _PROFILE_TABLE 一一对应）。
_VARIANTS = {
    "cn": {
        "chat_base": "https://www.codebuddy.cn",
        "billing_base": "https://www.codebuddy.cn",
        "x_domain": "www.codebuddy.cn",
        "inject_reasoning_effort": False,
        "inject_prompt_cache_key": False,
        "inject_device_headers": False,
        "use_checkin_status_precheck": False,
        "supports_claims": False,
        "show_account_info": True,
        "downgrade_effort": True,
        "normalize_body_shape": False,
        "action_ids": ["checkin", "sync_balance"],
        "badge_keys": {"last_checkin", "credit_remain", "account_type", "uin", "user_id"},
    },
    "intl": {
        "chat_base": "https://www.workbuddy.ai",
        "billing_base": "https://www.workbuddy.ai",
        "x_domain": "www.workbuddy.ai",
        "inject_reasoning_effort": True,
        "inject_prompt_cache_key": True,
        "inject_device_headers": True,
        "use_checkin_status_precheck": True,
        "supports_claims": True,
        "show_account_info": False,
        "downgrade_effort": False,
        "normalize_body_shape": True,
        "action_ids": ["checkin", "sync_balance", "sync_plan",
                       "claim_trial", "claim_gift", "claim_compensation"],
        "badge_keys": {"last_checkin", "credit_remain", "plan_tier", "last_trial", "user_id"},
    },
}


import re

_SWITCH_RE = re.compile(r'^DEPLOY_REGION = "cn"$', re.M)


def _source(region: str) -> str:
    """把顶部的开关行换成目标 region（行锚定，避免误命中注释里的示例字面量）。"""
    assert _SWITCH_RE.search(_BASE_SOURCE), '合并版必须保留独立一行 DEPLOY_REGION = "cn"'
    return _SWITCH_RE.sub(f'DEPLOY_REGION = "{region}"', _BASE_SOURCE, count=1)


def _module(region: str) -> types.ModuleType:
    """独立 exec 出模块对象（能拿模块级常量与 helper，且 setattr 可 patch 开关）。"""
    mod = types.ModuleType(f"codebuddy_merged_{region}")
    mod.__file__ = str(_SPEC)
    exec(compile(_source(region), f"<codebuddy-{region}>", "exec"), mod.__dict__)
    return mod


def _cls(region: str):
    chan = f"codebuddy-merged-{region}"
    cls = load_code_provider_class(chan, _source(region))
    yield_cls = cls
    invalidate_cache(chan)
    return yield_cls


def _stub(**extra):
    values = dict(base_url="https://misconfigured.example/v2", api_key="tok",
                  username="tester", user_id="uid-1", enterprise_id="", domain="",
                  proxy=None, PROVIDER_NAME="codebuddy-test")
    values.update(extra)
    return SimpleNamespace(**values)


def _payload(mod, body: dict, model: str = "step-5-preview", **kw):
    kwargs = dict(kw)
    kwargs["_base_payload"] = body
    out = mod.CodeBuddyIntlChannel.payload(_stub(), "openai", model, [], True, kwargs)
    return out if out else body


def _headers(mod, base: dict | None = None):
    return mod.CodeBuddyIntlChannel.headers(_stub(), dict(base or {}), {})


# ==================== 开关 → 区域档案常量 ====================

@pytest.mark.parametrize("region", list(_VARIANTS))
def test_switch_selects_profile_constants(region):
    mod = _module(region)
    v = _VARIANTS[region]
    assert mod.DEPLOY_REGION == region
    assert mod.CHAT_BASE == v["chat_base"]
    assert mod.BILLING_BASE == v["billing_base"]
    assert mod.X_DOMAIN == v["x_domain"]
    assert mod.INJECT_REASONING_EFFORT is v["inject_reasoning_effort"]
    assert mod.INJECT_PROMPT_CACHE_KEY is v["inject_prompt_cache_key"]
    assert mod.INJECT_DEVICE_HEADERS is v["inject_device_headers"]
    assert mod.USE_CHECKIN_STATUS_PRECHECK is v["use_checkin_status_precheck"]
    assert mod.SUPPORTS_CLAIMS is v["supports_claims"]
    assert mod.SHOW_ACCOUNT_INFO is v["show_account_info"]
    assert mod.DOWNGRADE_EFFORT is v["downgrade_effort"]
    assert mod.NORMALIZE_BODY_SHAPE is v["normalize_body_shape"]


def test_unknown_region_rejected():
    """DEPLOY_REGION 拼错必须立刻抛（_PROFILE_TABLE[region] KeyError），不能静默走默认。"""
    src = _SWITCH_RE.sub('DEPLOY_REGION = "xh"', _BASE_SOURCE, count=1)
    with pytest.raises(KeyError):
        exec(compile(src, "<xh>", "exec"), {})


# ==================== 寻址：域名写死、渠道配置不参与（两区域同款钩子）====================

@pytest.mark.parametrize("region", list(_VARIANTS))
def test_addressing_hardcoded_per_region(region):
    mod = _module(region)
    v = _VARIANTS[region]
    p = _stub(base_url="https://WRONG.example/path")  # 配错也不影响
    cls = mod.CodeBuddyIntlChannel
    assert cls.base_url(p) == v["chat_base"]
    assert cls.build_url(p) == f"{v['chat_base']}/v2/chat/completions"
    assert mod._base(p, "billing") == v["billing_base"]


# ==================== 出站 headers：设备头族仅 intl ====================

@pytest.mark.parametrize("region", list(_VARIANTS))
def test_headers_device_family_gated(region):
    h = _headers(_module(region))
    v = _VARIANTS[region]
    # 两区域共同的基础 CLI 指纹
    assert h["User-Agent"] == "WorkBuddy/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1"
    assert h["X-CodeBuddy-Request"] == "1"
    assert h["X-Product"] == "SaaS"
    assert h["X-User-Id"] == "uid-1"
    if v["inject_device_headers"]:
        assert "X-Machine-ID" in h and "X-Session-ID" in h
        assert h.get("X-Agent-Purpose") == "conversation"
    else:
        assert "X-Machine-ID" not in h and "X-Session-ID" not in h
        assert "X-Agent-Purpose" not in h


# ==================== 出站 payload：缓存键 / 形状归一 仅 intl ====================

@pytest.mark.parametrize("region", list(_VARIANTS))
def test_payload_cache_key_gated(region):
    body = {"model": "deepseek-v4.1-flash",
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]}
    out = _payload(_module(region), body, "deepseek-v4.1-flash", client_session_id="sess-1")
    has = "prompt_cache_key" in out
    assert has is _VARIANTS[region]["inject_prompt_cache_key"]


@pytest.mark.parametrize("region", list(_VARIANTS))
def test_payload_max_completion_tokens_gated(region):
    body = {"model": "deepseek-v4.1-flash",
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
            "max_completion_tokens": 100}
    out = _payload(_module(region), body, "deepseek-v4.1-flash")
    if _VARIANTS[region]["normalize_body_shape"]:
        assert "max_completion_tokens" not in out
        assert out.get("max_tokens") == 100
    else:
        # CN 不翻译别名：原样保留（与现网 codebuddy_code_channel_cn.py 一致）
        assert out.get("max_completion_tokens") == 100


# ==================== 档位降级 + 输出上限裁剪：仅 CN 生效 ====================

@pytest.mark.parametrize("region,expected", [("cn", "high"), ("intl", "xhigh")])
def test_downgrade_effort_step5_xhigh(region, expected):
    mod = _module(region)
    # 预热进程内能力缓存用静态表（冷启动）：step-5-preview=[low,medium,high]，默认档 high。
    out = _payload(mod, {"model": "step-5-preview",
                         "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
                         "reasoning_effort": "xhigh"},
                   "step-5-preview", reasoning_effort="xhigh")
    assert out.get("reasoning_effort") == expected


@pytest.mark.parametrize("region", list(_VARIANTS))
def test_no_max_tokens_clamp(region):
    """max_tokens 不由 spec 裁剪：上游实测不拦输出越界（重放 200000 → 200），且平台选路侧
    已按模型元数据 max_output_tokens 过滤装不下的候选。两区域都应原样透传。"""
    out = _payload(_module(region), {"model": "step-5-preview",
                                     "messages": [{"role": "system", "content": "s"},
                                                  {"role": "user", "content": "hi"}],
                                     "max_tokens": 65536},
                   "step-5-preview")
    assert out.get("max_tokens") == 65536
    assert "max_output_tokens" not in out


def test_downgrade_supported_effort_passthrough():
    """已在支持集内的档位（high/medium/low）原样透传，幂等不滚雪球。"""
    mod = _module("cn")
    for eff in ("high", "medium", "low"):
        b = {"model": "step-5-preview",
             "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
             "reasoning_effort": eff}
        assert _payload(mod, dict(b), "step-5-preview", reasoning_effort=eff).get("reasoning_effort") == eff


def test_downgrade_replay_idempotent():
    """重入（payload 二次过钩子）不二次改写——重试路径安全。"""
    mod = _module("cn")
    once = _payload(mod, {"model": "step-5-preview",
                          "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
                          "reasoning_effort": "xhigh", "max_tokens": 65536},
                    "step-5-preview", reasoning_effort="xhigh")
    twice = _payload(mod, dict(once), "step-5-preview", reasoning_effort=once.get("reasoning_effort"))
    assert twice.get("reasoning_effort") == once.get("reasoning_effort") == "high"
    # max_tokens 不被裁剪（平台选路侧按元数据过滤，spec 不重复处理）
    assert twice.get("max_tokens") == once.get("max_tokens") == 65536


def test_downgrade_unknown_model_passthrough():
    """模型不在能力表 / 档位未知词 → 透传（panel 语义，绝不臆造档位）。"""
    mod = _module("cn")
    b = {"model": "totally-unknown-model",
         "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
         "reasoning_effort": "xhigh"}
    out = _payload(mod, dict(b), "totally-unknown-model", reasoning_effort="xhigh")
    assert out.get("reasoning_effort") == "xhigh"


# ==================== fetch_models 能力缓存喂降级链（闭环）====================

class _FakeResp:
    def __init__(self, env, status=200):
        import json as _json
        self._text = _json.dumps(env)
        self.status = status

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeReq:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """按 path 分发预置 env 的假 session（仅 fetch_models 用）。"""

    def __init__(self, routes):
        self._routes = routes

    def get(self, url, **kw):
        import json as _json
        for needle, env in self._routes.items():
            if needle in url:
                return _FakeReq(_FakeResp(env))
        return _FakeReq(_FakeResp({"code": -1, "msg": "404"}, status=404))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


_CN_CONSOLE_ENV = {
    "code": 0, "msg": "ok",
    "data": {
        "models": [
            {"id": "step-5-preview", "name": "step-5-preview", "maxOutputTokens": 64000,
             "reasoning": {"supportedEfforts": ["low", "medium", "high"], "defaultEffort": "high"}},
        ],
        "agents": [{"name": "cli", "models": ["step-5-preview"]}],
    },
}


def test_fetch_models_caps_drive_downgrade_for_region():
    """fetch_models 拉到的 supportedEfforts/maxOutputTokens 灌进缓存后，
    降级链优先用权威值（而非静态兜底表）。以 CN console 单源为例。"""
    mod = _module("cn")
    # 清掉可能来自其它用例的缓存，强制走 console 拉取路径。
    mod._MODEL_CAPS.clear()
    p = _stub()
    p._make_session = lambda *a, **k: _FakeSession({"/console/enterprises/personal/models": _CN_CONSOLE_ENV})
    models = asyncio.run(mod.CodeBuddyIntlChannel.fetch_models(p))
    assert [m["id"] for m in models] == ["step-5-preview"]
    assert models[0]["supported_efforts"] == ["low", "medium", "high"]
    assert mod._MODEL_CAPS["step-5-preview"]["efforts"] == ["low", "medium", "high"]
    # max_output 不再进 caps（裁剪已删：平台选路侧已按 max_output_tokens 过滤）
    assert "max_output" not in mod._MODEL_CAPS["step-5-preview"]


def test_fetch_models_cn_uses_console_not_v3():
    """CN 区域 fetch_models 走 console 单源，不请求 v3/config（现网 codebuddy 实测）。"""
    mod = _module("cn")
    called: list[str] = []

    class _Probe(_FakeSession):
        def get(self, url, **kw):
            called.append(url)
            return super().get(url, **kw)

    p = _stub()
    p._make_session = lambda *a, **k: _Probe({"/console/enterprises/personal/models": _CN_CONSOLE_ENV})
    asyncio.run(mod.CodeBuddyIntlChannel.fetch_models(p))
    assert any("/console/enterprises/personal/models" in u for u in called)
    assert not any("/v3/config" in u for u in called)


# ==================== 11128 脱敏（两区域同款，锁住不被合并改坏）====================

@pytest.mark.parametrize("region", list(_VARIANTS))
def test_sanitize_identity_and_dev_role(region):
    mod = _module(region)
    body = {"model": "hy3", "messages": [
        {"role": "developer", "content": "You are Claude Code, Anthropic's official CLI for Claude."},
        {"role": "user", "content": "hi"},
    ]}
    out = _payload(mod, body, "hy3")
    msgs = out["messages"]
    assert msgs[0]["role"] == "system"
    assert "Claude Code" not in msgs[0]["content"]


# ==================== 现网等价对照已下线 ====================
# 旧拆分版（specs/codebuddy_code_channel_cn.py / _intl_v2.py）已于 2026-10-20 合并删除
# （备份见 deleted_backups/codebuddy-specs-consolidated-20261020/）。当初用来证明「合并版
# == 各自现网逐字」的对照测试随源文件失效；等价性已由上方各区域的 profile 断言承接。

