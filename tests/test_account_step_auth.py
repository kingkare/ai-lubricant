"""步骤认证（step auth）全链路：start → submit(next/pending/authorized/error/exit)。

锁的契约：
1. mode='steps' 的 start 走 begin_step_auth；begin 可返回 exit=True 表示「渠道不需要步骤」；
2. submit 返回 next 推进步骤并落 state 行，pending 原地提示不推进，authorized **不落库**、
   只回传 account_data 供前端回填表单（用户确认后手动保存），error → 400；
3. 非 steps 会话提交 → 400，已 completed 的会话提交 → 幂等返回 completed，未知 state → 404；
4. _shape_step_auth_response 把 provider 内部 state 映射成 provider_state 并剥掉 poll 字段；
5. auth_context 按签名探测转发（老 spec 少声明尾参不能炸）；
6. 设备码/回调被 _device_flow_to_step 包装成「异步步骤」（step.poll），存量 spec 零改动。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import admin


async def _allow_admin(*_args, **_kwargs):
    return "admin-token-abcdef0123456789"


def _request(*, base_url="http://127.0.0.1:8001/", headers=None):
    """最小 Request 替身：只用到 headers / base_url / url_for。"""
    return SimpleNamespace(
        headers=headers or {},
        base_url=base_url,
        url_for=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no router")),
    )


def _install_common(monkeypatch, states: dict):
    """start / submit 共用的桩：admin 鉴权、配置读写、状态存取、日志。"""
    monkeypatch.setattr(admin, "_require_admin", _allow_admin)

    async def read_config(_name):
        return {}

    async def set_state(state, data):
        states[state] = dict(data)

    async def get_state(state):
        return states.get(state)

    async def del_state(state):
        states.pop(state, None)

    async def log_op(*_a, **_k):
        return None

    monkeypatch.setattr(admin, "_read_provider_config", read_config)
    monkeypatch.setattr(admin, "_set_account_auth_state", set_state)
    monkeypatch.setattr(admin, "_get_account_auth_state", get_state)
    monkeypatch.setattr(admin, "_del_account_auth_state", del_state)
    monkeypatch.setattr(admin, "_log_operation", log_op)
    monkeypatch.setattr(admin, "_merge_saved_account_for_auth", lambda acc, _cfg: dict(acc))
    monkeypatch.setattr(admin, "_read_proxies", lambda: [])
    monkeypatch.setattr(admin, "_resolve_account_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "_resolve_account_url_prefix", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "_resolve_account_proxy_config_id", lambda *_a, **_k: "")
    monkeypatch.setattr(admin, "_provider_extra", lambda *_a, **_k: {})


# ==================== _shape_step_auth_response / _apply_step_auth_state ====================

def test_shape_strips_poll_fields_and_maps_provider_state():
    result = {
        "task_type": "steps",
        "step": {"id": "phone"},
        "step_state": {"opaque": True},
        "state": "internal-state",
        "poll_status": True,
        "poll_interval": 5,
    }
    shaped = admin._shape_step_auth_response(result)
    assert shaped["mode"] == "steps"
    assert "poll_status" not in shaped
    assert "poll_interval" not in shaped
    # provider 内部 state 与后端管理态 state 是两个东西，改名不混
    assert shaped["provider_state"] == "internal-state"
    assert "state" not in shaped


def test_shape_defaults_mode_to_steps():
    assert admin._shape_step_auth_response({"step": {"id": "x"}})["mode"] == "steps"


def test_apply_step_auth_state_writes_step_fields():
    state_data = {"provider": "demo", "account": {}}
    result = {
        "task_type": "steps",
        "step": {"id": "phone", "fields": []},
        "step_state": {"opaque": True},
        "expires_in": 600,
    }
    expires_in = admin._apply_step_auth_state(state_data, result)
    assert expires_in == 600
    assert state_data["task_type"] == "steps"
    assert state_data["status"] == "pending"
    assert state_data["step"] == {"id": "phone", "fields": []}
    assert state_data["step_state"] == {"opaque": True}
    assert "expires_at" in state_data
    # 同步步骤不进扫描器：不写 next_poll_at / interval
    assert "next_poll_at" not in state_data
    assert "interval" not in state_data


def test_apply_step_auth_state_async_step_promotes_poll_params():
    """迁移后的设备码 spec：begin_step_auth 返回 step.poll + step_state.poll_params。
    框架必须把 poll_params 提到顶层 + 写 next_poll_at/interval，否则后台扫描器拿不到
    device_code、无法轮询（迁移最容易漏的就是这一步）。"""
    import time
    state_data = {"provider": "demo", "account": {}}
    before = time.time()
    result = {
        "task_type": "steps",
        "step": {
            "id": "device_authorize",
            "fields": [],
            "user_code": "WXYZ-1234",
            "poll": {"interval": 5, "expires_in": 900},
        },
        "step_state": {"poll_params": {"device_code": "dc-1"}},
        "expires_in": 900,
    }
    admin._apply_step_auth_state(state_data, result)
    # poll_params 提到顶层（扫描器读 record["poll_params"]）
    assert state_data["poll_params"] == {"device_code": "dc-1"}
    assert state_data["interval"] == 5
    assert state_data["next_poll_at"] >= before + 5 - 1
    # step_state 原样保留（含 poll_params 副本，spec 自己用）
    assert state_data["step_state"]["poll_params"] == {"device_code": "dc-1"}
    assert state_data["step"]["poll"]["interval"] == 5


def test_apply_step_auth_state_async_step_without_poll_params_no_poll():
    """step.poll 声明但 step_state 无 poll_params：不写 next_poll_at（没法轮询就别进扫描器）。"""
    state_data = {"provider": "demo", "account": {}}
    result = {
        "task_type": "steps",
        "step": {"id": "x", "fields": [], "poll": {"interval": 5}},
        "step_state": {},   # 没 poll_params
    }
    admin._apply_step_auth_state(state_data, result)
    assert "next_poll_at" not in state_data
    assert "poll_params" not in state_data


# ==================== _invoke_begin_step_auth ====================

def test_invoke_begin_rejects_missing_hook():
    """未声明 begin_step_auth → 501（admin 靠它判能力，不能返回假 pending）。"""

    class Provider:
        pass

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin._invoke_begin_step_auth(Provider, "demo", {}, {}, {}))
    assert exc.value.status_code == 501
    assert "begin_step_auth" in str(exc.value.detail)


def test_invoke_begin_rejects_wrong_task_type(monkeypatch):
    class Provider:
        async def begin_step_auth(self, auth_context=None):
            return {"task_type": "device_code", "step": {"id": "x"}}

    monkeypatch.setattr(admin, "_build_auth_runtime_instance", lambda *_a, **_k: Provider())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin._invoke_begin_step_auth(Provider, "demo", {}, {}, {}))
    assert exc.value.status_code == 500
    assert "steps" in str(exc.value.detail)


def test_invoke_begin_rejects_missing_step(monkeypatch):
    class Provider:
        async def begin_step_auth(self, auth_context=None):
            return {"task_type": "steps"}

    monkeypatch.setattr(admin, "_build_auth_runtime_instance", lambda *_a, **_k: Provider())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin._invoke_begin_step_auth(Provider, "demo", {}, {}, {}))
    assert exc.value.status_code == 500
    assert "step" in str(exc.value.detail)


# ==================== start_provider_account_auth（mode='steps'）====================

def test_start_steps_dispatches_to_begin_step_auth(monkeypatch):
    states: dict = {}
    _install_common(monkeypatch, states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def begin_step_auth(self, auth_context=None):
            return {
                "task_type": "steps",
                "step": {"id": "phone", "title": "输入手机号",
                         "fields": [{"key": "phone", "type": "tel", "label": "手机号"}]},
                "step_state": {"internal": "opaque-1"},
                "expires_in": 600,
            }

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)
    monkeypatch.setattr(admin, "_provider_account_schema",
                        lambda *_a, **_k: {"auth_start": {"enabled": True, "mode": "steps"}})

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.start_provider_account_auth(
        "demo", {"account": {"username": "u"}, "origin": "https://panel.example.com"},
        request, "Bearer t"))

    assert result["mode"] == "steps"
    assert result["step"]["id"] == "phone"
    assert result["step_state"] == {"internal": "opaque-1"}
    assert "poll_status" not in result        # 前端据此不启轮询
    assert "redirect_uri" not in result       # steps 不回传回调地址
    stored = states[result["state"]]
    assert stored["task_type"] == "steps"
    assert stored["step"]["id"] == "phone"
    assert stored["step_state"] == {"internal": "opaque-1"}


def test_start_steps_legacy_hook_without_context(monkeypatch):
    """老 spec 写 begin_step_auth(self) —— 框架按签名探测，不因多传参数而炸。"""
    states: dict = {}
    _install_common(monkeypatch, states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def begin_step_auth(self):
            return {"task_type": "steps", "step": {"id": "init"}}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)
    monkeypatch.setattr(admin, "_provider_account_schema",
                        lambda *_a, **_k: {"auth_start": {"mode": "steps"}})

    request = _request(headers={"origin": "http://127.0.0.1:8001"})
    result = asyncio.run(admin.start_provider_account_auth("demo", {"account": {}}, request, "Bearer t"))
    assert result["step"]["id"] == "init"


def test_start_steps_failure_cleans_state(monkeypatch):
    """begin 失败 → state 行删掉（与 device_code 一致，不留半截会话）。"""
    states: dict = {}
    _install_common(monkeypatch, states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def begin_step_auth(self, auth_context=None):
            raise RuntimeError("上游拒绝")

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)
    monkeypatch.setattr(admin, "_provider_account_schema",
                        lambda *_a, **_k: {"auth_start": {"mode": "steps"}})

    request = _request(headers={"origin": "http://127.0.0.1:8001"})
    with pytest.raises(RuntimeError):
        asyncio.run(admin.start_provider_account_auth("demo", {"account": {}}, request, "Bearer t"))
    assert states == {}


# ==================== submit_provider_account_auth_step ====================

def _seed_steps_session(states: dict, **overrides) -> str:
    """预置一个 pending 的 steps 会话，返回 state。"""
    state = "seed-state-xyz"
    states[state] = {
        "provider": "demo",
        "task_type": "steps",
        "status": "pending",
        "account": {"username": "u"},
        "step": {"id": "phone", "fields": [{"key": "phone", "type": "tel"}]},
        "step_state": {"internal": "opaque-1"},
        **overrides,
    }
    return state


def test_submit_next_advances_step(monkeypatch):
    states: dict = {}
    _install_common(monkeypatch, states)
    state = _seed_steps_session(states)
    seen: dict = {}

    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            seen.update(step_id=step_id, values=values, step_state=step_state, action=action)
            return {
                "status": "next",
                "step": {"id": "sms", "title": "输入验证码",
                         "fields": [{"key": "code", "type": "code"}]},
                "step_state": {"internal": "opaque-2"},
            }

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.submit_provider_account_auth_step(
        "demo", {"state": state, "step_id": "phone", "values": {"phone": "13800001111"}},
        request, "Bearer t"))

    assert result["status"] == "next"
    assert result["step"]["id"] == "sms"
    assert result["step_state"] == {"internal": "opaque-2"}
    # 服务端把持久化的 step_state 原样交给 hook
    assert seen["step_id"] == "phone"
    assert seen["values"] == {"phone": "13800001111"}
    assert seen["step_state"] == {"internal": "opaque-1"}
    # 下一步已落 state 行
    assert states[state]["step"]["id"] == "sms"
    assert states[state]["step_state"] == {"internal": "opaque-2"}


def test_submit_pending_keeps_step_and_surfaces_message(monkeypatch):
    """pending = 原地提示（如「验证码已发送」），不推进步骤。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    state = _seed_steps_session(states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            return {"status": "pending", "message": "验证码已发送，请查收"}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.submit_provider_account_auth_step(
        "demo", {"state": state, "step_id": "phone", "values": {"phone": "13800001111"}},
        request, "Bearer t"))

    assert result["status"] == "pending"
    assert result["message"] == "验证码已发送，请查收"
    assert states[state]["step"]["id"] == "phone"        # 步骤不变
    assert states[state]["step_state"] == {"internal": "opaque-1"}


def test_submit_next_to_async_step_lands_in_pending_list(monkeypatch):
    """回归（autoclaw「没有渠道认领这条回调地址」）：

    第一步是同步表单步（无 poll），第二步才是异步步（带 poll + poll_params）。
    推进到第二步时必须写 next_poll_at，否则该会话既不在扫描器轮询列表、也不在
    loopback 认领列表里 —— 用户粘验证码/凭证就报「没有渠道认领」。
    """
    states: dict = {}
    _install_common(monkeypatch, states)
    # 第一步：同步表单步，无 poll
    state = "sync-then-async"
    states[state] = {
        "provider": "demo", "task_type": "steps", "status": "pending",
        "account": {}, "step": {"id": "entry", "fields": []},
        "step_state": {},
    }

    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            return {
                "status": "next",
                "step": {"id": "device_authorize", "fields": [],
                         "poll": {"interval": 5, "expires_in": 600}},
                "step_state": {"poll_params": {"login": "sms", "phone": "138****1111"}},
            }

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.submit_provider_account_auth_step(
        "demo", {"state": state, "step_id": "entry", "values": {"region": "cn"}},
        request, "Bearer t"))

    assert result["status"] == "next"
    stored = states[state]
    # 关键：异步步的轮询字段必须落到顶层（扫描器 + loopback 认领都靠它）
    assert stored["poll_params"] == {"login": "sms", "phone": "138****1111"}
    assert "next_poll_at" in stored
    assert stored["interval"] == 5


def test_submit_next_to_sync_step_clears_polling(monkeypatch):
    """异步步退回同步步：清掉轮询字段，别让扫描器继续轮询一个不需要轮询的会话。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    state = "async-then-sync"
    states[state] = {
        "provider": "demo", "task_type": "steps", "status": "pending",
        "account": {}, "step": {"id": "device_authorize", "fields": [],
                                "poll": {"interval": 5}},
        "step_state": {"poll_params": {"device_code": "dc"}},
        "poll_params": {"device_code": "dc"}, "interval": 5, "next_poll_at": 0,
    }

    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            return {"status": "next", "step": {"id": "manual", "fields": []},
                    "step_state": {}}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request(headers={"origin": "https://panel.example.com"})
    asyncio.run(admin.submit_provider_account_auth_step(
        "demo", {"state": state, "step_id": "device_authorize", "values": {}},
        request, "Bearer t"))

    stored = states[state]
    assert "next_poll_at" not in stored
    assert "poll_params" not in stored
    assert "interval" not in stored


def test_submit_action_is_forwarded(monkeypatch):
    """actions（如「重新发送」）走 submit 的 action 参数，不改步骤。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    state = _seed_steps_session(states)
    seen: dict = {}
    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            seen["action"] = action
            return {"status": "pending", "message": "已重新发送"}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.submit_provider_account_auth_step(
        "demo", {"state": state, "step_id": "phone", "values": {}, "action": "resend"},
        request, "Bearer t"))

    assert seen["action"] == "resend"
    assert result["status"] == "pending"


def test_submit_receives_auth_context_when_signature_allows(monkeypatch):
    """声明了 auth_context 尾参的 spec 能收到框架上下文（start 与 submit 同源）。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    state = _seed_steps_session(states)
    seen: dict = {}

    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            seen["ctx"] = auth_context
            return {"status": "pending"}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request(headers={"origin": "https://panel.example.com"})
    asyncio.run(admin.submit_provider_account_auth_step(
        "demo", {"state": state, "step_id": "phone", "values": {}}, request, "Bearer t"))

    assert seen["ctx"] is not None
    assert seen["ctx"]["origin"] == "https://panel.example.com"


def test_submit_authorized_returns_account_data_for_replay(monkeypatch):
    """authorized 不落库：返回 account_data，前端回填表单后用户手动保存。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    state = _seed_steps_session(states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            return {"status": "authorized",
                    "account_data": {"user_name": "neo", "access_token": "tok"}}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.submit_provider_account_auth_step(
        "demo", {"state": state, "step_id": "sms", "values": {"code": "123456"}},
        request, "Bearer t"))

    assert result["status"] == "completed"
    assert result["account"]["user_name"] == "neo"
    assert result["account"]["access_token"] == "tok"
    # authorized 不调 finalize，只标记 completed + 返回 data
    assert states[state]["status"] == "completed"
    assert states[state]["account_data"]["user_name"] == "neo"


def test_submit_error_raises_400(monkeypatch):
    states: dict = {}
    _install_common(monkeypatch, states)
    state = _seed_steps_session(states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            return {"status": "error", "error": "验证码错误"}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request(headers={"origin": "https://panel.example.com"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.submit_provider_account_auth_step(
            "demo", {"state": state, "step_id": "sms", "values": {"code": "wrong"}},
            request, "Bearer t"))
    assert exc.value.status_code == 400
    assert "验证码错误" in str(exc.value.detail)


def test_submit_next_without_step_raises_500(monkeypatch):
    """渠道返回 next 却没给下一步 step → 500（框架契约：next 必须带 step.id）。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    state = _seed_steps_session(states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            return {"status": "next"}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)

    request = _request()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.submit_provider_account_auth_step(
            "demo", {"state": state, "step_id": "phone", "values": {}}, request, "Bearer t"))
    assert exc.value.status_code == 500
    assert "step" in str(exc.value.detail)


def test_invoke_submit_rejects_unknown_status(monkeypatch):
    class Provider:
        async def submit_step_auth(self, step_id, values, step_state, action=None, auth_context=None):
            return {"status": "banana"}

    monkeypatch.setattr(admin, "_build_auth_runtime_instance", lambda *_a, **_k: Provider())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin._invoke_submit_step_auth(
            Provider, "demo", {}, {}, "s1", {}, {}, None, {}))
    assert exc.value.status_code == 500
    assert "banana" in str(exc.value.detail)


def test_invoke_submit_rejects_missing_hook():
    class Provider:
        pass

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin._invoke_submit_step_auth(
            Provider, "demo", {}, {}, "s1", {}, {}, None, {}))
    assert exc.value.status_code == 501
    assert "submit_step_auth" in str(exc.value.detail)


# ==================== 会话校验 ====================

def test_submit_rejects_non_steps_session(monkeypatch):
    states: dict = {}
    _install_common(monkeypatch, states)
    states["s1"] = {"provider": "demo", "task_type": "device_code", "status": "pending"}

    request = _request()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.submit_provider_account_auth_step(
            "demo", {"state": "s1", "step_id": "x", "values": {}}, request, "Bearer t"))
    assert exc.value.status_code == 400
    assert "步骤认证" in str(exc.value.detail)


def test_submit_completed_session_is_idempotent(monkeypatch):
    states: dict = {}
    _install_common(monkeypatch, states)
    states["s1"] = {
        "provider": "demo", "task_type": "steps", "status": "completed",
        "completed": True, "account_data": {"user_name": "neo"}, "message": "done",
    }

    request = _request()
    result = asyncio.run(admin.submit_provider_account_auth_step(
        "demo", {"state": "s1", "step_id": "x", "values": {}}, request, "Bearer t"))
    assert result["status"] == "completed"
    assert result["account"]["user_name"] == "neo"


def test_submit_unknown_state_returns_404(monkeypatch):
    _install_common(monkeypatch, {})

    request = _request()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.submit_provider_account_auth_step(
            "demo", {"state": "nonexistent", "step_id": "x", "values": {}}, request, "Bearer t"))
    assert exc.value.status_code == 404


def test_submit_other_provider_state_returns_404(monkeypatch):
    """别人的会话不能提交（state 与 provider 必须匹配）。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    states["s1"] = {"provider": "other", "task_type": "steps", "status": "pending"}

    request = _request()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.submit_provider_account_auth_step(
            "demo", {"state": "s1", "step_id": "x", "values": {}}, request, "Bearer t"))
    assert exc.value.status_code == 404


def test_submit_missing_state_raises_400(monkeypatch):
    _install_common(monkeypatch, {})

    request = _request()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.submit_provider_account_auth_step(
            "demo", {"step_id": "x", "values": {}}, request, "Bearer t"))
    assert exc.value.status_code == 400
    assert "state" in str(exc.value.detail)


def test_submit_missing_step_id_raises_400(monkeypatch):
    _install_common(monkeypatch, {})

    request = _request()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.submit_provider_account_auth_step(
            "demo", {"state": "s1", "values": {}}, request, "Bearer t"))
    assert exc.value.status_code == 400
    assert "step_id" in str(exc.value.detail)


# ==================== status 查询（刷新后恢复当前步）====================

def test_auth_status_returns_current_step_for_steps_session(monkeypatch):
    states: dict = {}
    _install_common(monkeypatch, states)
    _seed_steps_session(states)

    request = _request()
    result = asyncio.run(admin.get_provider_account_auth_status("demo", "seed-state-xyz", "Bearer t"))
    assert result["status"] == "pending"
    assert result["step"]["id"] == "phone"
    assert result["step_state"] == {"internal": "opaque-1"}


def test_auth_status_steps_completed_returns_account(monkeypatch):
    states: dict = {}
    _install_common(monkeypatch, states)
    states["s1"] = {
        "provider": "demo", "task_type": "steps", "status": "completed",
        "completed": True, "account": {"username": "neo"}, "message": "步骤认证成功",
    }

    result = asyncio.run(admin.get_provider_account_auth_status("demo", "s1", "Bearer t"))
    assert result["status"] == "completed"
    assert result["account"]["username"] == "neo"


def test_auth_status_completed_prefers_account_data_over_start_shell(monkeypatch):
    """completed 行优先回传 account_data（凭证），不是 /auth/start 落的请求表单。

    统一模型下授权完成不落库：扫描器 / loopback / 步骤 authorized 都把凭证写进
    ``account_data``，而 ``account`` 仍是启动时前端表单的原样（username 空串那层
    空壳，恒为真值 dict）。原顺序 ``account or account_data`` 被空壳遮住，前端拿到
    空对象 → 页面显示「授权已完成」却一个字段都没回填（codearts 曾复现此坑）。
    """
    states: dict = {}
    _install_common(monkeypatch, states)
    states["s1"] = {
        "provider": "demo", "task_type": "steps", "status": "completed",
        "completed": True,
        "account": {"username": "", "switch": True, "proxy": ""},   # start 落的请求表单空壳
        "account_data": {"username": "neo", "access_key_id": "AK", "security_token": "ST"},
        "message": "授权完成，请核对账号信息后保存",
    }

    result = asyncio.run(admin.get_provider_account_auth_status("demo", "s1", "Bearer t"))
    assert result["status"] == "completed"
    assert result["account"]["access_key_id"] == "AK"
    assert result["account"]["username"] == "neo"


def test_auth_status_completed_falls_back_to_account_when_no_account_data(monkeypatch):
    """存量 callback 落库行只有 account（无 account_data）→ 仍回传 account，不回归。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    states["s1"] = {
        "provider": "demo", "status": "completed", "completed": True,
        "account": {"username": "legacy"},
    }

    result = asyncio.run(admin.get_provider_account_auth_status("demo", "s1", "Bearer t"))
    assert result["status"] == "completed"
    assert result["account"]["username"] == "legacy"


def test_cancel_steps_session_deletes_state(monkeypatch):
    """取消只是删 state 行（steps 与 device_code 共用），不需要额外清理。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    _seed_steps_session(states)

    result = asyncio.run(admin.cancel_provider_account_auth("demo", "seed-state-xyz", "Bearer t"))
    assert result["ok"] is True
    assert "seed-state-xyz" not in states


# ==================== exit（渠道不需要步骤） + 设备码包装成步骤 ====================

def test_begin_step_auth_exit_short_circuits(monkeypatch):
    """begin_step_auth 返回 exit=True：渠道不需要步骤，state 不留、直接进表单。"""
    states: dict = {}
    _install_common(monkeypatch, states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def begin_step_auth(self, auth_context=None):
            return {"task_type": "steps", "exit": True,
                    "account_data": {"username": "preset-user", "password": "tok"}}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)
    monkeypatch.setattr(admin, "_provider_account_schema",
                        lambda *_a, **_k: {"auth_start": {"enabled": True, "mode": "steps"}})

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.start_provider_account_auth(
        "demo", {"account": {}}, request, "Bearer t"))

    assert result["mode"] == "steps"
    assert result["exit"] is True
    assert result["account"]["username"] == "preset-user"
    # exit：不留 state 行
    assert states == {}


def test_device_flow_wrapped_into_async_step(monkeypatch):
    """设备码 begin_device_flow 被 _device_flow_to_step 包装成异步步骤：step.poll + 展示信息。"""
    flow = {
        "task_type": "device_code",
        "auth_url": "https://up.example/auth",
        "verification_uri": "https://up.example/device",
        "user_code": "ABCD-1234",
        "interval": 7,
        "expires_in": 600,
        "poll_params": {"device_code": "dc", "interval": 7},
    }
    step = admin._device_flow_to_step(flow, completion="poll")
    assert step["id"] == "device_authorize"
    assert step["user_code"] == "ABCD-1234"
    assert step["auth_url"] == "https://up.example/auth"
    assert step["verification_uri"] == "https://up.example/device"
    assert step["poll"]["interval"] == 7
    assert step["poll"]["expires_in"] == 600
    assert "验证码：ABCD-1234" in step["hint"]
    assert "https://up.example/device" in step["hint"]


def test_device_flow_to_step_loopback_adds_replay_hint():
    """loopback 完成方式：步骤 hint 带补投提示（跨机粘 URL）。"""
    flow = {"auth_url": "https://127.0.0.1:9988/callback"}
    step = admin._device_flow_to_step(flow, completion="loopback")
    assert "粘到下方补投" in step["hint"]


def test_start_device_flow_wraps_to_step(monkeypatch):
    """设备码渠道 start：对外是 steps 形态，内部 state 仍是 device_code（扫描器不动）。"""
    states: dict = {}
    _install_common(monkeypatch, states)

    class Provider:
        def __init__(self, **_kw):
            pass

        async def begin_device_flow(self, auth_context=None):
            return {"task_type": "device_code", "auth_url": "https://up.example/auth",
                    "user_code": "ABCD", "interval": 5, "expires_in": 300,
                    "poll_params": {"device_code": "dc"}}

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)
    monkeypatch.setattr(admin, "_provider_account_schema",
                        lambda *_a, **_k: {"auth_start": {"enabled": True, "mode": "device_code"}})

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.start_provider_account_auth(
        "demo", {"account": {}}, request, "Bearer t"))

    # 对外：统一成 steps 形态
    assert result["mode"] == "steps"
    assert result["step"]["user_code"] == "ABCD"
    assert result["step"]["poll"]["interval"] == 5
    # 内部：状态机仍是 device_code，扫描器照常轮询
    stored = states[result["state"]]
    assert stored["task_type"] == "device_code"
    assert stored["poll_params"] == {"device_code": "dc"}


def test_start_no_auth_hooks_returns_exit(monkeypatch):
    """渠道无任何授权钩子（纯手填）→ exit=True，直接进表单。"""
    states: dict = {}
    _install_common(monkeypatch, states)

    class Provider:
        def __init__(self, **_kw):
            pass

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)
    monkeypatch.setattr(admin, "_provider_account_schema",
                        lambda *_a, **_k: {"auth_start": {"enabled": False}})

    request = _request(headers={"origin": "https://panel.example.com"})
    result = asyncio.run(admin.start_provider_account_auth(
        "demo", {"account": {}}, request, "Bearer t"))

    assert result["mode"] == "steps"
    assert result["exit"] is True
    assert states == {}


# ==================== 授权出站必须跟账号代理走 ====================
# 出站收口到 ProxyManager 后，路由**只认 proxy_config_id**（facade 把调用点的 proxy=
# 直接吞掉）。授权用的是 throwaway 实例，历史上只挂 p.proxy → 用户在账号表单/授权面板
# 选了代理，begin_step_auth / submit_step_auth / 后台轮询照样直连——境外上游必死。

def _stub_proxy_pool(monkeypatch):
    pool = [{"id": "px-1", "name": "东京出口", "url": "http://1.2.3.4:8080",
             "mode": "network", "username": "", "password": ""}]
    monkeypatch.setattr(admin, "_read_proxies", lambda: pool)
    monkeypatch.setattr(admin, "_resolve_account_proxy",
                        lambda acc, *_a: "http://1.2.3.4:8080" if (acc.get("proxy_id") or acc.get("proxy")) == "px-1" else None)
    monkeypatch.setattr(admin, "_resolve_account_url_prefix", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "_resolve_account_proxy_config_id",
                        lambda acc, *_a: "px-1" if (acc.get("proxy_id") or acc.get("proxy")) == "px-1" else "")
    return pool


def test_auth_runtime_instance_carries_proxy_config_id(monkeypatch):
    """步骤认证的 throwaway 实例必须带 proxy_config_id，否则统一出站按直连路由。"""
    _stub_proxy_pool(monkeypatch)
    monkeypatch.setattr(admin, "_provider_extra", lambda *_a, **_k: {})
    seen: dict = {}

    class Provider:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)
    inst = admin._build_auth_runtime_instance("demo", {}, {"username": "u", "proxy": "px-1"})
    assert seen["proxy_config_id"] == "px-1"
    assert seen["proxy"] == "http://1.2.3.4:8080"
    assert inst is not None


def test_scanner_instance_carries_proxy_config_id(monkeypatch):
    """后台轮询（异步步/设备码）造的实例同样要带代理——否则 begin 走代理、poll 直连。"""
    _stub_proxy_pool(monkeypatch)
    monkeypatch.setattr(admin, "_provider_extra", lambda *_a, **_k: {})
    seen: dict = {}

    class Provider:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(admin, "_get_provider_class", lambda *_a, **_k: Provider)
    admin._auth_scanner_instances.clear()
    try:
        admin._build_scanner_instance("demo", {}, {"username": "u", "proxy_id": "px-1"}, cache_key="demo:st")
        assert seen["proxy_config_id"] == "px-1"
    finally:
        admin._auth_scanner_instances.clear()


def _seed_auth_session(states: dict, **overrides) -> str:
    state = "proxy-state"
    states[state] = {
        "provider": "demo", "task_type": "steps", "status": "pending",
        "account": {"username": "u"},
        "step": {"id": "entry", "fields": []}, "step_state": {},
        **overrides,
    }
    return state


def test_auth_proxy_endpoint_updates_session_account(monkeypatch):
    """授权途中换代理：会话账号快照必须被改写（后续每步/轮询都从它取代理）。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    _stub_proxy_pool(monkeypatch)
    state = _seed_auth_session(states)
    admin._auth_scanner_instances["demo:" + state] = object()

    result = asyncio.run(admin.update_provider_account_auth_proxy(
        "demo", {"state": state, "proxy": "px-1"}, "Bearer t"))
    assert result["ok"] is True
    assert result["proxy_config_id"] == "px-1"
    # 换成规范字段 proxy_id（与账号持久化 _normalize_account_proxy_ref 同口径，不双写）
    assert states[state]["account"]["proxy_id"] == "px-1"
    assert "proxy" not in states[state]["account"]
    # 按会话缓存的扫描器实例持旧代理 → 必须丢弃
    assert f"demo:{state}" not in admin._auth_scanner_instances


def test_auth_proxy_endpoint_can_clear_back_to_direct(monkeypatch):
    """空代理 = 显式改回直连：两个引用字段都得清掉，别留脏 proxy_id。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    _stub_proxy_pool(monkeypatch)
    state = _seed_auth_session(states, account={"username": "u", "proxy_id": "px-1"})

    result = asyncio.run(admin.update_provider_account_auth_proxy(
        "demo", {"state": state, "proxy": None}, "Bearer t"))
    assert result["proxy_config_id"] == ""
    assert "proxy_id" not in states[state]["account"]
    assert "proxy" not in states[state]["account"]


def test_auth_proxy_endpoint_rejects_proxy_outside_pool(monkeypatch):
    """代理引用必须命中代理池：静默回退直连会让用户以为选了代理。"""
    states: dict = {}
    _install_common(monkeypatch, states)
    _stub_proxy_pool(monkeypatch)
    state = _seed_auth_session(states)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.update_provider_account_auth_proxy(
            "demo", {"state": state, "proxy": "nope"}, "Bearer t"))
    assert exc.value.status_code == 400
    assert "代理池" in str(exc.value.detail)
    # 校验失败不能半途改坏会话
    assert states[state]["account"] == {"username": "u"}


def test_auth_proxy_endpoint_rejects_missing_state(monkeypatch):
    states: dict = {}
    _install_common(monkeypatch, states)
    _stub_proxy_pool(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.update_provider_account_auth_proxy(
            "demo", {"state": "ghost", "proxy": "px-1"}, "Bearer t"))
    assert exc.value.status_code == 404

