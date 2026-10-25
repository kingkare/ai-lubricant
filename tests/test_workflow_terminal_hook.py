"""Terminal hook（结束/失败合并面板）行为回归：notify_when 时机门控 + Agent 重试。"""
import asyncio
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)
for _p in (os.path.join(_proj, "server"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv_loader import load_project_env  # noqa: E402

load_project_env()

import agent.workflow_service as svc  # noqa: E402
from agent.workflow import WorkflowDefError  # noqa: E402

DEFROW = {"id": 1, "name": "wf"}


@pytest.mark.asyncio
async def test_fire_terminal_gating(monkeypatch):
    """notify_when 决定通知时机；cancelled/interrupted 不报；agent_id 触发重试。"""
    calls: list[tuple] = []

    async def fake_dispatch(owner, channels, text, run_id, status, fn, fe, extra_params=None):
        calls.append(("notify", status, tuple(channels)))

    async def fake_retry(agent_id, def_row, run_id, fn, fe):
        calls.append(("retry", agent_id))

    monkeypatch.setattr(svc, "_dispatch_notify", fake_dispatch)
    monkeypatch.setattr(svc, "_retry_with_agent", fake_retry)

    defn = {
        "nodes": [{"id": "a", "name": "A", "kind": "cache", "action": "get", "key": "wf:k"}],
        "end": {"notify_template": "wf {$run.status}", "notify_channels": ["ch"],
                "notify_when": "success"},
        "failure": {"agent_id": 7},
    }

    async def run(status: str) -> list[tuple]:
        calls.clear()
        await svc._fire_terminal(DEFROW, defn, 9, {"status": status, "nodes": {}}, "owner")
        return list(calls)

    # when=success：成功发、失败不发（但重试仍触发）、cancelled 什么都不发
    r = await run("ok")
    assert ("notify", "ok", ("ch",)) in r
    r = await run("failed")
    assert not any(c[0] == "notify" for c in r) and ("retry", 7) in r
    r = await run("cancelled")
    assert r == []

    # when=failed：只有失败发
    defn["end"]["notify_when"] = "failed"
    r = await run("failed")
    assert ("notify", "failed", ("ch",)) in r and ("retry", 7) in r
    r = await run("ok")
    assert not any(c[0] == "notify" for c in r)

    # when=always：成功失败都发
    defn["end"]["notify_when"] = "always"
    r = await run("ok")
    assert ("notify", "ok", ("ch",)) in r
    r = await run("failed")
    assert ("notify", "failed", ("ch",)) in r and ("retry", 7) in r

    # 未配重试 Agent → 失败也不重试
    defn["failure"] = {}
    r = await run("failed")
    assert ("notify", "failed", ("ch",)) in r and not any(c[0] == "retry" for c in r)


@pytest.mark.asyncio
async def test_resolve_inputs_manual_and_typed(monkeypatch):
    """手跑（不选启动方式）时声明入参由 args 填进 $input；类型按声明矫正。"""
    defn = {
        "inputs": [{"key": "site", "type": "string"}, {"key": "pages", "type": "number"}],
        "triggers": [
            {"id": "t_custom", "kind": "cron", "cron": "0 3 * * *",
             "params": {"source": "custom", "value": {"site": "fallback.com", "pages": 1}}},
            {"id": "t_acct", "kind": "cron", "cron": "0 4 * * *",
             "params": {"source": "account_list", "url": "https://x.com/login", "multiple": False}},
            {"id": "t_acct_multi", "kind": "cron", "cron": "0 5 * * *",
             "params": {"source": "account_list", "url": "https://x.com/login", "multiple": True}},
        ],
        "nodes": [{"id": "a", "kind": "run_session", "sop": "go {$input.site} {$input.pages}"}],
        "edges": [],
    }

    async def fake_accounts(owner, url):
        return [{"account": "u1"}]

    monkeypatch.setattr(svc, "_resolve_accounts", fake_accounts)

    # 手动/隐式启动：args 直接按声明键填进 $input（原来这里返回空，表单值只停在 $args）
    out = await svc._resolve_inputs(defn, None, "owner", None, {"site": "a.com", "pages": "3"})
    assert out == {"site": "a.com", "pages": 3}

    # custom 启动方式：value 打底 + 声明键的显式 args 覆盖 + 类型矫正
    out = await svc._resolve_inputs(defn, {"id": "t_custom"}, "owner", None, {"pages": "9"})
    assert out == {"site": "fallback.com", "pages": 9}

    # account_list（不扇出）：$input.accounts = 解析出的账号列表
    out = await svc._resolve_inputs(defn, {"id": "t_acct"}, "owner", None, {})
    assert out["accounts"] == [{"account": "u1"}]

    # account_list 扇出单跑：只放当前账号。即便 accounts 声明成 array 也不矫正——
    # 值是引擎给的（扇出时是单个账号对象），形状本就对。
    out = await svc._resolve_inputs(defn, {"id": "t_acct_multi"}, "owner", None, {},
                                    single_account={"account": "u2"})
    assert out == {"accounts": {"account": "u2"}}

    defn["inputs"].append({"key": "accounts", "type": "array", "note": "账号列表"})
    out = await svc._resolve_inputs(defn, {"id": "t_acct_multi"}, "owner", None, {},
                                    single_account={"account": "u2"})
    assert out == {"accounts": {"account": "u2"}}


@pytest.mark.asyncio
async def test_resolve_inputs_account_typed_input_gets_username(monkeypatch):
    """声明 type=account 的入参：按启动方式的运行模式灌用户名形态的值。

    用户口径：扇出单跑 → {'account': 'u1'}（本遍账号的 username 标量）；
    整体一次 → {'account': ['u1','u2']}（username 列表）。显式 args 的其余
    声明键仍逐键覆盖。
    """
    defn = {
        "inputs": [{"key": "account", "type": "account", "site_url": "https://x.com/login", "required": True}],
        "triggers": [
            {"id": "t_acct", "kind": "cron", "cron": "0 4 * * *",
             "params": {"source": "account_list", "url": "https://x.com/login", "multiple": False}},
            {"id": "t_acct_multi", "kind": "cron", "cron": "0 5 * * *",
             "params": {"source": "account_list", "url": "https://x.com/login", "multiple": True}},
        ],
        "nodes": [{"id": "a", "name": "跑一遍", "kind": "run_session", "sop": "go {$input.account}"}],
        "edges": [],
    }

    async def fake_accounts(owner, url):
        return [
            {"id": 1, "domain": "x.com", "username": "u1", "url": ""},
            {"id": 2, "domain": "x.com", "username": "u2", "url": ""},
        ]

    monkeypatch.setattr(svc, "_resolve_accounts", fake_accounts)

    # 整体一次（multiple=false）：account = username 列表。
    out = await svc._resolve_inputs(defn, {"id": "t_acct"}, "owner", None, {})
    assert out["account"] == ["u1", "u2"]

    # 扇出单跑：account = 本遍 username 标量。
    out = await svc._resolve_inputs(defn, {"id": "t_acct_multi"}, "owner", None, {},
                                    single_account={"id": 1, "domain": "x.com", "username": "u1", "url": ""})
    assert out["account"] == "u1"

    # 「只跑一个」（accounts_override 单元素）：等同扇出单跑的形状。
    out = await svc._resolve_inputs(defn, {"id": "t_acct"}, "owner", None, {},
                                    accounts_override=[{"id": 2, "domain": "x.com", "username": "u2", "url": ""}])
    assert out["account"] == "u2"

    # 其余声明键的显式 args 仍逐键覆盖（account 由引擎灌，args 不覆盖引擎值）。
    defn2 = {
        "inputs": [{"key": "account", "type": "account", "site_url": "https://x.com", "required": True},
                   {"key": "site_url", "type": "string"}],
        "triggers": [defn["triggers"][0]],
        "nodes": defn["nodes"],
        "edges": [],
    }
    out = await svc._resolve_inputs(defn2, {"id": "t_acct"}, "owner", None, {"site_url": "https://y.com"})
    assert out["site_url"] == "https://y.com"
    assert out["account"] == ["u1", "u2"]

    # 账号列表为空：引擎没有 username 可灌，required 校验照常上抛（不能假装有值）。
    async def empty_accounts(owner, url):
        return []

    monkeypatch.setattr(svc, "_resolve_accounts", empty_accounts)
    with pytest.raises(WorkflowDefError, match="missing required input"):
        await svc._resolve_inputs(defn2, {"id": "t_acct"}, "owner", None, {})


@pytest.mark.asyncio
async def test_resolve_inputs_legacy_accounts_array_kept(monkeypatch):
    """旧形状（声明 accounts=array + trigger account_list/multiple）逐字不变：
    值是原始账号对象（整体=数组，扇出单跑=单对象）——历史 fixture 依赖这个形状。"""
    defn = {
        "inputs": [{"key": "accounts", "type": "array", "note": "账号列表"}],
        "triggers": [
            {"id": "t_acct", "kind": "cron", "cron": "0 4 * * *",
             "params": {"source": "account_list", "url": "https://x.com", "multiple": False}},
            {"id": "t_acct_multi", "kind": "cron", "cron": "0 5 * * *",
             "params": {"source": "account_list", "url": "https://x.com", "multiple": True}},
        ],
        "nodes": [{"id": "a", "name": "跑一遍", "kind": "run_session", "sop": "go {$input.accounts}"}],
        "edges": [],
    }
    one = {"id": 1, "domain": "x.com", "username": "u1", "url": ""}

    async def fake_accounts(owner, url):
        return [one, {"id": 2, "domain": "x.com", "username": "u2", "url": ""}]

    monkeypatch.setattr(svc, "_resolve_accounts", fake_accounts)

    out = await svc._resolve_inputs(defn, {"id": "t_acct"}, "owner", None, {})
    assert out["accounts"] == [one, {"id": 2, "domain": "x.com", "username": "u2", "url": ""}]

    out = await svc._resolve_inputs(defn, {"id": "t_acct_multi"}, "owner", None, {}, single_account=one)
    assert out["accounts"] == one


@pytest.mark.asyncio
async def test_run_multi_pick_accounts_filters_fanout(monkeypatch):
    """start_and_run_multi 的 pick_accounts：扇出只跑选中账号，整体一次只灌选中数组。"""
    defn = {
        "inputs": [{"key": "account", "type": "string", "required": True}],
        "triggers": [
            {"id": "t_fan", "kind": "cron", "cron": "0 5 * * *",
             "params": {"source": "account_list", "url": "https://x.com", "multiple": True}},
            {"id": "t_once", "kind": "cron", "cron": "0 6 * * *",
             "params": {"source": "account_list", "url": "https://x.com", "multiple": False}},
        ],
        "nodes": [{"id": "a", "name": "跑一遍", "kind": "run_session", "sop": "go {$input.account}"}],
        "edges": [],
    }
    all_accounts = [
        {"id": 1, "domain": "x.com", "username": "u1", "url": ""},
        {"id": 2, "domain": "x.com", "username": "u2", "url": ""},
        {"id": 3, "domain": "x.com", "username": "u3", "url": ""},
    ]

    async def fake_get_def(def_id, version=None):
        return {"id": 1, "version": 1, "user_id": "u1", "definition": defn}

    started: list[dict] = []

    async def fake_start_single(def_row, definition, args, *, trigger, caller, cron,
                                trigger_id, single_account, accounts_override=None):
        started.append({"single": single_account, "override": accounts_override})
        return len(started)

    monkeypatch.setattr(svc.workflow_store, "get_def", fake_get_def)
    monkeypatch.setattr(svc, "_resolve_accounts", lambda owner, url: _ret(all_accounts))
    monkeypatch.setattr(svc, "_start_single", fake_start_single)

    # 扇出 + 选 u1,u3 → 恰好两条单跑，single_account 分别是这两个。
    ids = await svc.start_and_run_multi(1, args={}, trigger_id="t_fan", pick_accounts=["u1", "u3"])
    assert ids == [1, 2]
    assert [s["single"]["username"] for s in started] == ["u1", "u3"]

    started.clear()
    # 整体一次 + 选 u2 → 一条运行，accounts_override = 只含 u2。
    ids = await svc.start_and_run_multi(1, args={}, trigger_id="t_once", pick_accounts=["u2"])
    assert ids == [1]
    assert started[0]["override"] == [{"id": 2, "domain": "x.com", "username": "u2", "url": ""}]

    started.clear()
    # 不选 → 扇出全部三条。
    await svc.start_and_run_multi(1, args={}, trigger_id="t_fan")
    assert [s["single"]["username"] for s in started] == ["u1", "u2", "u3"]

    # 选了列表里没有的账号 → 明确报错（不静默跑零条）。
    with pytest.raises(ValueError, match="不在该站点的账号列表"):
        await svc.start_and_run_multi(1, args={}, trigger_id="t_fan", pick_accounts=["ghost"])


async def _ret(value):
    return value


