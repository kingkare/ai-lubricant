"""Tests for the workflow engine (agent/workflow.py) — pure logic, no DB."""
import asyncio
import os
import sys
from typing import Any

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.workflow import (
    NodeExecutors,
    NodeResult,
    WorkflowContext,
    WorkflowDefError,
    WorkflowRunner,
    eval_predicate,
    find_refs,
    interpolate,
    parse_ref,
    resolve_value,
    topo_layers,
    validate_def,
    validate_output,
)


# ── references ────────────────────────────────────────────────────────────────

def test_parse_ref_basic_and_paths():
    assert parse_ref("$login.status").root == "login"
    assert parse_ref("$login.status").path == ["status"]
    assert parse_ref("$args.api_base").root == "args"
    assert parse_ref("$input.accounts").root == "input"
    assert parse_ref("$item.account").path == ["account"]
    with pytest.raises(WorkflowDefError):
        parse_ref("not a ref")


def test_find_refs_in_templates():
    refs = find_refs("check {$item.account} then {$fetch.total}")
    assert [r.text for r in refs] == ["$item.account", "$fetch.total"]


def test_interpolate_templates():
    ctx = WorkflowContext(
        args={"api": "http://x"},
        inputs={"site": "example.com"},
        nodes={"login": {"status": "ok", "output": {"name": "acct1"}}},
        item={"account": "a@x.com"},
    )
    assert interpolate("hi {$item.account}", ctx) == "hi a@x.com"
    assert interpolate("{$login.name}", ctx) == "acct1"
    assert interpolate("obj {$args.api}", ctx) == "obj http://x"
    assert interpolate("site {$input.site}", ctx) == "site example.com"


def test_interpolate_renders_dicts_as_json():
    ctx = WorkflowContext(nodes={"n": {"status": "ok", "output": {"a": 1}}})
    assert interpolate("got {$n}", ctx) == 'got {"a": 1}'


def test_resolve_value_descends_structures():
    ctx = WorkflowContext(args={"token": "t1"})
    out = resolve_value({"h": {"Authorization": "Bearer {$args.token}"}, "list": ["$args.token"]}, ctx)
    assert out == {"h": {"Authorization": "Bearer t1"}, "list": ["t1"]}


# ── predicates ────────────────────────────────────────────────────────────────

def _pred_ctx():
    return WorkflowContext(nodes={"login": {"status": "ok", "output": {"count": 3, "name": "a"}}})


def test_predicate_comparisons():
    ctx = _pred_ctx()
    assert eval_predicate("$login.count >= 3", ctx)
    assert eval_predicate("$login.name == 'a'", ctx)
    assert not eval_predicate("$login.count > 3", ctx)


def test_predicate_boolean_ops_and_not():
    ctx = _pred_ctx()
    assert eval_predicate("$login.count >= 3 and $login.name == 'a'", ctx)
    assert eval_predicate("$login.count > 3 or $login.name == 'a'", ctx)
    assert eval_predicate("not $login.count > 3", ctx)


def test_predicate_in_and_exists():
    ctx = _pred_ctx()
    assert eval_predicate("$login.name in ['a','b']", ctx)
    assert eval_predicate("exists($login.name)", ctx)
    assert not eval_predicate("exists($login.missing)", ctx)


def test_predicate_type_error_raises():
    ctx = _pred_ctx()
    with pytest.raises(WorkflowDefError):
        eval_predicate("$login.count >= 'abc'", ctx)


def test_predicate_unknown_ref_raises():
    with pytest.raises(WorkflowDefError):
        eval_predicate("$ghost.x == 1", _pred_ctx())


# ── output schema validation ──────────────────────────────────────────────────

def test_validate_output_object_ok_and_errors():
    schema = {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string", "enum": ["ok", "failed"]},
                        "count": {"type": "integer"}},
    }
    assert validate_output({"status": "ok", "count": 2}, schema) == []
    errs = validate_output({"count": "x"}, schema)
    assert any("status" in e for e in errs)
    assert any("expected integer" in e for e in errs)


def test_validate_output_array_items():
    schema = {"type": "array", "items": {"type": "object", "required": ["id"]}}
    assert validate_output([{"id": 1}, {"id": 2}], schema) == []
    errs = validate_output([{"id": 1}, {}], schema)
    assert len(errs) == 1


# ── static validation ─────────────────────────────────────────────────────────

def _def(nodes, edges=None, inputs=None):
    # 节点 name（中文）现为必填；测试 fixture 未关心它，统一补成 id，
    # 保持各用例只聚焦自己断言的那件事。
    d: dict[str, Any] = {"nodes": [{**n, "name": n.get("name") or n.get("id") or "?"} for n in nodes]}
    if edges is not None:
        d["edges"] = edges
    if inputs is not None:
        d["inputs"] = inputs
    return d


def test_validate_def_ok_graph_with_edges():
    validate_def(_def(
        [
            {"id": "a", "kind": "run_mcp", "service": "s", "method": "m",
             "output_schema": {"type": "object", "properties": {"items": {"type": "array"}}}},
            {"id": "b", "kind": "code_run", "code": "print(1)",
             "output_schema": {"type": "array"}},
            {"id": "c", "kind": "run_session", "sop": "do {$b}", "agent_id": "any"},
        ],
        edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        inputs=[{"key": "site", "note": "站点"}],
    ))


def test_validate_def_rejects_unknown_kind_and_empty():
    with pytest.raises(WorkflowDefError):
        validate_def({"nodes": []})
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "wat"}]))
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "", "kind": "code_run", "code": "x"}]))


def test_validate_def_rejects_duplicate_ids():
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "code_run", "code": "x"}, {"id": "a", "kind": "code_run", "code": "y"}]))


def test_validate_def_rejects_cycle():
    with pytest.raises(WorkflowDefError):
        validate_def(_def(
            [{"id": "a", "kind": "code_run", "code": "x"}, {"id": "b", "kind": "code_run", "code": "y"}],
            edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        ))


def test_validate_def_rejects_unknown_edge_endpoint():
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "code_run", "code": "x"}], edges=[{"source": "a", "target": "ghost"}]))


def test_validate_def_rejects_self_edge():
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "code_run", "code": "x"}], edges=[{"source": "a", "target": "a"}]))


def test_validate_def_rejects_legacy_node_routing():
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "code_run", "code": "x", "depends_on": ["b"]}]))
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "code_run", "code": "x", "when": "$args.ok"}]))


def test_validate_def_rejects_non_ancestor_ref():
    # 边是 a→b，则 b 的后代引用不可见：a 引用 $b（它的下游）→ 保存时拒。
    with pytest.raises(WorkflowDefError):
        validate_def(_def(
            [
                {"id": "b", "kind": "code_run", "code": "x", "output_schema": {"type": "object"}},
                {"id": "a", "kind": "code_run", "code": "print($b)"},
            ],
            edges=[{"source": "a", "target": "b"}],
        ))
    # 完全没有边相连：c 不是 b 的祖先 → 拒。
    with pytest.raises(WorkflowDefError):
        validate_def(_def([
            {"id": "c", "kind": "code_run", "code": "x", "output_schema": {"type": "object"}},
            {"id": "b", "kind": "code_run", "code": "print($c)"},
        ]))


def test_validate_def_rejects_field_missing_from_schema():
    with pytest.raises(WorkflowDefError):
        validate_def(_def(
            [
                {"id": "c", "kind": "code_run", "code": "x", "output_schema": {"type": "object", "properties": {"known": {"type": "string"}}}},
                {"id": "b", "kind": "run_session", "sop": "use {$c.unknown_field}"},
            ],
            edges=[{"source": "c", "target": "b"}],
        ))


def test_validate_def_rejects_item_without_foreach_scope():
    with pytest.raises(WorkflowDefError):
        validate_def(_def(
            [
                {"id": "a", "kind": "code_run", "code": "x", "output_schema": {"type": "array"}},
                {"id": "b", "kind": "code_run", "code": "print($item)"},
            ],
            edges=[{"source": "a", "target": "b"}],
        ))


def test_validate_def_inputs_table():
    # account_list 需要 url；custom 不需要。
    validate_def(_def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}],
                      inputs=[{"key": "accounts", "note": "巡检账号"}]))
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}],
                          inputs=[{"key": "bad key", "note": "x"}]))
    # 旧的 input 级 source 字段已废弃（供值方式移到了启动方式）→ 拒。
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}],
                          inputs=[{"key": "accounts", "source": "account_list", "url": "x"}]))
    # $input 引用未声明的 key → 拒。
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "run_session", "sop": "use {$input.ghost}"}],
                          inputs=[{"key": "real", "note": "x"}]))


def test_validate_def_inputs_types():
    """入参类型声明：type 必须 ∈ INPUT_TYPES；缺省 = string；回写规范形状。"""
    from agent.workflow import validate_inputs, INPUT_TYPES

    defn = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    defn["inputs"] = [
        {"key": "name", "type": "string", "note": "文本"},
        {"key": "count", "type": "number"},
        {"key": "flag", "type": "boolean"},
        {"key": "blob", "type": "json"},
        {"key": "list", "type": "array"},
        {"key": "implicit"},  # 缺省 → string
    ]
    validate_def(defn)
    table = validate_inputs(defn)
    assert table["name"]["type"] == "string"
    assert table["count"]["type"] == "number"
    assert table["flag"]["type"] == "boolean"
    assert table["blob"]["type"] == "json"
    assert table["list"]["type"] == "array"
    assert table["implicit"]["type"] == "string"  # 缺省回退
    # 规范形状：每条只有 key/type/note 三键（不带用户塞进来的杂字段）
    assert set(table["name"]) == {"key", "type", "note"}

    # 未知 type → 拒（文案点明期望集）
    bad = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    bad["inputs"] = [{"key": "k", "type": "wat"}]
    with pytest.raises(WorkflowDefError, match="unknown type"):
        validate_def(bad)
    # 大小写不敏感（JSON 不规范时常见大写）
    ok_ci = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    ok_ci["inputs"] = [{"key": "k", "type": "NUMBER"}]
    validate_def(ok_ci)
    # 八种类型：五种数据形状 + 四种资源型（model/account/cdp_client/proxy），与前端 WorkflowInputType 一致
    assert INPUT_TYPES == ("string", "number", "boolean", "json", "array",
                           "model", "account", "cdp_client", "proxy")
    # 资源型入参：account 带 site_url 时保留；model/cdp_client/proxy 没有 options 也合法
    res = _def([{"id": "a", "name": "s", "kind": "cache", "action": "get", "key": "wf:k"}])
    res["inputs"] = [
        {"key": "m", "type": "model"},
        {"key": "acc", "type": "account", "site_url": "https://x.com/login"},
        {"key": "cli", "type": "cdp_client"},
        {"key": "px", "type": "proxy"},
    ]
    table = validate_inputs(res)
    assert table["m"]["type"] == "model"
    assert table["acc"]["site_url"] == "https://x.com/login"
    assert table["cli"]["type"] == "cdp_client"
    assert table["px"]["type"] == "proxy"


def test_coerce_inputs_types():
    """类型矫正：字符串数字/布尔按声明还原，array 必须真的是数组，未声明键透传。"""
    from agent.workflow import coerce_inputs

    defn = {"nodes": [{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}],
            "edges": [],
            "inputs": [
                {"key": "n", "type": "number"},
                {"key": "b", "type": "boolean"},
                {"key": "s", "type": "string"},
                {"key": "arr", "type": "array"},
                {"key": "obj", "type": "json"},
            ]}
    out = coerce_inputs(defn, {
        "n": "7",          # cron JSON / Agent 传字符串数字 → 还原成数
        "b": "true",
        "s": 42,           # 数字给了 string 槽 → 转字符串
        "arr": '["a","b"]',
        "obj": {"k": 1},
        "extra": "未声明键原样透传",
    })
    assert out == {"n": 7, "b": True, "s": "42",
                   "arr": ["a", "b"], "obj": {"k": 1}, "extra": "未声明键原样透传"}
    # 显式 null 原样保留（丢掉会让 $input.n 变成 missing field）
    assert coerce_inputs(defn, {"n": None, "b": True}) == {"n": None, "b": True}
    # 数字槽收了非数字 → 报错
    with pytest.raises(Exception, match="expected a number"):
        coerce_inputs(defn, {"n": "abc"})
    # boolean 槽收了乱串 → 报错
    with pytest.raises(Exception, match="expected a boolean"):
        coerce_inputs(defn, {"b": "maybe"})
    # array 槽收了对象 JSON → 报错（必须真的是数组）
    with pytest.raises(Exception, match="expected a JSON array"):
        coerce_inputs(defn, {"arr": '{"a":1}'})
    # bool 不是 number（Python bool 是 int 子类，须显式拦）
    with pytest.raises(Exception, match="expected a number"):
        coerce_inputs(defn, {"n": True})


def test_validate_inputs_attributes():
    """入参属性：options（可选项）/default（默认值）/required（必填）。"""
    from agent.workflow import validate_inputs, WorkflowDefError

    defn = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    defn["inputs"] = [
        {"key": "env", "type": "string", "options": ["dev", "staging", "prod"], "default": "dev", "required": True},
        {"key": "retries", "type": "number", "options": [0, 1, 2], "default": 1},
        {"key": "flag", "type": "boolean", "default": True},
        {"key": "plain", "type": "string"},
    ]
    validate_def(defn)
    table = validate_inputs(defn)
    assert table["env"]["options"] == ["dev", "staging", "prod"]
    assert table["env"]["default"] == "dev"
    assert table["env"]["required"] is True
    assert table["retries"]["default"] == 1
    assert table["flag"]["default"] is True
    # 没写属性的入参不带这些键（规范形状）
    assert "options" not in table["plain"] and "default" not in table["plain"] and "required" not in table["plain"]

    # options 必须是**非空**数组
    bad = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    bad["inputs"] = [{"key": "k", "options": []}]
    with pytest.raises(WorkflowDefError, match="non-empty"):
        validate_def(bad)

    # options 不能用于 json/array
    bad2 = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    bad2["inputs"] = [{"key": "k", "type": "json", "options": ["a"]}]
    with pytest.raises(WorkflowDefError, match="cannot be used with type"):
        validate_def(bad2)

    # default 必须是合法类型
    bad3 = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    bad3["inputs"] = [{"key": "n", "type": "number", "default": "abc"}]
    with pytest.raises(WorkflowDefError, match="expected a number"):
        validate_def(bad3)

    # default 有 options 时必须命中其一
    bad4 = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    bad4["inputs"] = [{"key": "env", "options": ["dev", "prod"], "default": "staging"}]
    with pytest.raises(WorkflowDefError, match="is not one of options"):
        validate_def(bad4)


def test_coerce_inputs_options():
    """coerce 时值必须命中 options（选择项）。"""
    from agent.workflow import coerce_inputs, WorkflowDefError

    defn = {"nodes": [{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}], "edges": [],
            "inputs": [{"key": "env", "options": ["dev", "prod"]}]}
    assert coerce_inputs(defn, {"env": "dev"}) == {"env": "dev"}
    with pytest.raises(WorkflowDefError, match="not one of options"):
        coerce_inputs(defn, {"env": "staging"})


def test_apply_input_defaults():
    """default 补缺 + required 校验：跑之前就定论，别拖到图里报 missing field。"""
    from agent.workflow import apply_input_defaults, WorkflowDefError

    defn = {"nodes": [{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}], "edges": [],
            "inputs": [
                {"key": "env", "default": "dev"},
                {"key": "token", "required": True},
                {"key": "opt", "default": "x"},
            ]}
    # 缺 env → 补默认；token 显式给了 → 保留；opt 缺 → 补默认
    out = apply_input_defaults(defn, {"token": "t"})
    assert out == {"token": "t", "env": "dev", "opt": "x"}
    # 不覆盖显式给的值（含 falsy）
    assert apply_input_defaults(defn, {"env": "", "token": "t"})["env"] == ""
    # required 没值 → 报错（点出缺哪些）
    with pytest.raises(WorkflowDefError, match="missing required input"):
        apply_input_defaults(defn, {})
    # required 有默认值时不再算缺
    defn2 = {"nodes": [{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}], "edges": [],
             "inputs": [{"key": "t", "required": True, "default": "d"}]}
    assert apply_input_defaults(defn2, {}) == {"t": "d"}
    # 不 mutate 入参
    src = {"token": "t"}
    apply_input_defaults(defn, src)
    assert src == {"token": "t"}


def test_validate_def_triggers_table():
    """启动方式：只有 cron 是声明式的（手动/Agent 隐式常显）；params 供值。"""
    from agent.workflow import validate_triggers
    defn = _def(
        [{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}],
    )
    defn["triggers"] = [
        {"id": "nightly", "kind": "cron", "cron": "0 3 * * *",
         "params": {"source": "account_list", "url": "https://x.com/login", "multiple": True}},
        {"id": "hourly", "kind": "cron", "cron": "hourly",
         "params": {"source": "custom", "value": {"scope": "hot"}}, "enabled": False},
    ]
    validate_def(defn)
    specs = validate_triggers(defn)
    assert [t["id"] for t in specs] == ["nightly", "hourly"]
    assert specs[0]["params"]["source"] == "account_list"
    assert specs[0]["params"]["multiple"] is True
    assert specs[1]["params"]["value"] == {"scope": "hot"}
    assert specs[1]["enabled"] is False

    # kind=cron 缺 cron → 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**defn, "triggers": [{"id": "x", "kind": "cron", "params": {}}]})
    # manual/agent 不再是合法 trigger kind（隐式）→ 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**defn, "triggers": [{"id": "x", "kind": "manual"}]})
    # 未知 kind → 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**defn, "triggers": [{"id": "x", "kind": "webhook"}]})
    # 重复 id → 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**defn, "triggers": [{"id": "x", "kind": "cron", "cron": "0 9 * * *", "params": {}},
                                           {"id": "x", "kind": "cron", "cron": "0 9 * * *", "params": {}}]})
    # account_list 缺 url → 拒；custom value 非对象 → 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**defn, "triggers": [{"id": "x", "kind": "cron", "cron": "0 9 * * *",
                                            "params": {"source": "account_list"}}]})
    with pytest.raises(WorkflowDefError):
        validate_def({**defn, "triggers": [{"id": "x", "kind": "cron", "cron": "0 9 * * *",
                                            "params": {"source": "custom", "value": [1]}}]})


def test_validate_def_end_and_failure():
    """结束终端配置（合并后）：通知（模版/渠道/时机）+ 失败只配重试 Agent。"""
    base = [{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}]
    d = _def(base)
    d["end"] = {"notify_template": "wf {$run.status}", "notify_channels": ["u1", "u2"],
                "notify_when": "always", "notify_params": {"env": "prod"}}
    d["failure"] = {"agent_id": 3}
    validate_def(d)
    end, failure = __import__("agent.workflow", fromlist=["validate_end_failure"]).validate_end_failure(d)
    assert end["notify_when"] == "always"
    assert failure == {"agent_id": 3}

    # notify_when 三值合法
    for when in ("success", "failed", "always"):
        validate_def({**d, "end": {**d["end"], "notify_when": when}})
    # 未知 notify_when → 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**d, "end": {**d["end"], "notify_when": "wat"}})
    # 旧 failure.action 字段已废弃 → 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**d, "failure": {"action": "none"}})
    with pytest.raises(WorkflowDefError):
        validate_def({**d, "failure": {"action": "notify_retry_agent", "agent_id": 3}})
    # agent_id 非数字 → 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**d, "failure": {"agent_id": "x"}})
    # agent_id 缺省 = 不重试，合法
    validate_def({**d, "failure": {}})
    # 渠道必须是字符串数组 → 拒
    with pytest.raises(WorkflowDefError):
        validate_def({**d, "end": {**d["end"], "notify_channels": [1, 2]}})


def test_validate_def_kind_specific_required_fields():
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "run_session", "sop": ""}]))
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "run_mcp", "service": "s", "method": ""}]))
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "web", "operation": ""}]))
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "code_run", "code": ""}]))
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "file_write", "path": "x", "content": ""}]))


def test_validate_def_cache_key_prefix_enforced():
    with pytest.raises(WorkflowDefError):
        validate_def(_def([{"id": "a", "kind": "cache", "action": "set", "key": "other:k", "value": "1"}]))
    validate_def(_def([{"id": "a", "kind": "cache", "action": "set", "key": "wf:k", "value": "1"}]))


def test_validate_def_fill_credential_needs_credential_kind():
    # fill_credential 的凭据类型用 `credential_kind`——`kind` 是引擎的节点类型
    # 字段，不能携带凭据类型。缺它 / 值非法 → 保存即拒（原来要跑到 MCP 调用才炸）。
    with pytest.raises(WorkflowDefError, match="credential_kind"):
        validate_def(_def([{"id": "a", "kind": "web", "operation": "fill_credential", "url": "$args.u"}]))
    with pytest.raises(WorkflowDefError, match="credential_kind"):
        validate_def(_def([{"id": "a", "kind": "web", "operation": "fill_credential",
                            "credential_kind": "totp", "url": "$args.u"}]))
    # 合法两种凭据类型放行；普通 web 操作不受影响。
    validate_def(_def([{"id": "a", "kind": "web", "operation": "fill_credential",
                        "credential_kind": "password", "url": "$args.u"}]))
    validate_def(_def([{"id": "a", "kind": "web", "operation": "fill_credential",
                        "credential_kind": "OTP", "url": "$args.u"}]))
    validate_def(_def([{"id": "a", "kind": "web", "operation": "read_page", "url": "$args.u"}]))


def test_validate_def_edge_when_refs():
    # 边 b→c 的条件只能引用 b 或 b 的祖先；引用 b 的后代 c 自己 → 拒。
    with pytest.raises(WorkflowDefError):
        validate_def(_def(
            [
                {"id": "b", "kind": "code_run", "code": "x", "output_schema": {"type": "object"}},
                {"id": "c", "kind": "code_run", "code": "z", "output_schema": {"type": "object"}},
            ],
            edges=[{"source": "b", "target": "c", "when": "$c.ok == true"}],
        ))
    # 合法：条件引用 source（a）自己的输出。
    validate_def(_def(
        [
            {"id": "a", "kind": "code_run", "code": "x", "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
            {"id": "b", "kind": "code_run", "code": "y"},
        ],
        edges=[{"source": "a", "target": "b", "when": "$a.ok == true"}],
    ))
    # 合法：条件引用 source 的祖先（a→b→c，c 那条边用 $a）。
    validate_def(_def(
        [
            {"id": "a", "kind": "code_run", "code": "x", "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
            {"id": "b", "kind": "code_run", "code": "y"},
            {"id": "c", "kind": "code_run", "code": "z"},
        ],
        edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "c", "when": "$a.ok == true"}],
    ))


# ── execution ────────────────────────────────────────────────────────────────

class FakeExecutors(NodeExecutors):
    """Scripted executors: maps node id → output (or exception)."""

    def __init__(self, outputs: dict[str, Any]):
        self.outputs = outputs
        self.calls: list[tuple[str, Any]] = []

    async def run_code_run(self, node, ctx, params):
        self.calls.append(("code_run", node["id"]))
        out = self.outputs[node["id"]]
        if isinstance(out, Exception):
            raise out
        return out

    async def run_file_read(self, node, ctx, params):
        return self.outputs[node["id"]]

    async def run_file_write(self, node, ctx, params):
        return self.outputs[node["id"]]

    async def run_web(self, node, ctx, params):
        return self.outputs[node["id"]]

    async def run_mcp(self, node, ctx, params):
        self.calls.append(("run_mcp", node["id"]))
        out = self.outputs[node["id"]]
        if isinstance(out, Exception):
            raise out
        return out

    async def run_session(self, node, ctx, params):
        self.calls.append(("run_session", node["id"]))
        out = self.outputs[node["id"]]
        if isinstance(out, Exception):
            raise out
        return out

    async def run_cache(self, node, ctx, params):
        return self.outputs[node["id"]]

    async def run_http(self, node, ctx, params):
        self.calls.append(("run_http", node["id"], params.get("proxy")))
        return self.outputs[node["id"]]

    async def run_select(self, node, ctx, params):
        # 与真执行器同判定：按序取第一个 when 命中的 set，无命中用 default。
        from agent.workflow import eval_predicate
        for i, case in enumerate(node.get("cases") or []):
            when = str((case or {}).get("when") or "").strip()
            try:
                hit = eval_predicate(when, ctx) if when else True
            except Exception:
                hit = False
            if hit and i < len(params.get("cases") or []):
                return params["cases"][i].get("set")
        return params.get("default") or node.get("default") or {}


@pytest.mark.asyncio
async def test_runner_happy_path_with_variable_flow():
    defn = _def(
        [
            {"id": "fetch", "kind": "run_mcp", "service": "s", "method": "list",
             "output_schema": {"type": "object", "properties": {"accounts": {"type": "array"}}}},
            {"id": "go", "kind": "run_session", "sop": "process {$fetch.accounts}"},
        ],
        edges=[{"source": "fetch", "target": "go"}],
    )
    ex = FakeExecutors({"fetch": {"accounts": ["a", "b"]}, "go": "done"})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(args={}))

    assert result["status"] == "ok"
    assert result["nodes"]["fetch"]["status"] == "ok"
    assert result["nodes"]["go"]["status"] == "ok"
    assert ("run_session", "go") in ex.calls


@pytest.mark.asyncio
async def test_runner_edge_not_ok_skips_target_but_runs_sibling_branch():
    defn = _def(
        [
            {"id": "a", "kind": "code_run", "code": "x"},
            {"id": "a_child", "kind": "run_session", "sop": "p"},
            {"id": "b", "kind": "code_run", "code": "y"},
            {"id": "b_child", "kind": "run_session", "sop": "q"},
        ],
        edges=[
            {"source": "a", "target": "a_child"},
            {"source": "b", "target": "b_child"},
        ],
    )
    ex = FakeExecutors({"a": RuntimeError("boom"), "b": [1, 2], "a_child": "unused", "b_child": "ok"})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(args={}))

    assert result["status"] == "partial"
    assert result["nodes"]["a"]["status"] == "failed"
    assert result["nodes"]["a_child"]["status"] == "skipped"
    assert result["nodes"]["b"]["status"] == "ok"
    assert result["nodes"]["b_child"]["status"] == "ok"


@pytest.mark.asyncio
async def test_runner_any_active_edge_runs_node():
    """标准工作流图语义：任一入边激活（源 ok + 条件成立）该节点就执行。"""
    defn = _def(
        [
            {"id": "check", "kind": "code_run", "code": "x",
             "output_schema": {"type": "object", "properties": {"status": {"type": "string"}}}},
            {"id": "yes_branch", "kind": "run_session", "sop": "y"},
            {"id": "no_branch", "kind": "cache", "action": "set", "key": "wf:no", "value": "1"},
            {"id": "join", "kind": "code_run", "code": "z"},
        ],
        edges=[
            {"source": "check", "target": "yes_branch", "when": "$check.status == 'ok'"},
            {"source": "check", "target": "no_branch", "when": "$check.status != 'ok'"},
            {"source": "yes_branch", "target": "join"},
            {"source": "no_branch", "target": "join"},
        ],
    )
    ex = FakeExecutors({"check": {"status": "ok"}, "yes_branch": "y!", "no_branch": "unused", "join": "joined"})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(args={}))

    assert result["nodes"]["yes_branch"]["status"] == "ok"
    assert result["nodes"]["no_branch"]["status"] == "skipped"
    assert result["nodes"]["join"]["status"] == "ok"  # 任一入边（yes_branch）激活


@pytest.mark.asyncio
async def test_runner_unevaluable_when_is_skipped_not_fatal():
    defn = _def(
        [
            {"id": "a", "kind": "code_run", "code": "x", "output_schema": {"type": "object", "properties": {"s": {"type": "string"}}}},
            {"id": "b", "kind": "code_run", "code": "y"},
        ],
        edges=[{"source": "a", "target": "b", "when": "$a.s == 'ok'"}],
    )
    # a fails → its output missing → b's when is unevaluable → skipped, not crash.
    ex = FakeExecutors({"a": RuntimeError("nope"), "b": 1})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(args={}))
    assert result["nodes"]["b"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_runner_foreach_node_loops_whole_downstream():
    """遍历节点：它 + 全部下游，按 items 各跑一遍，$item 对体内每个节点可见。
    fetch → foreach(items=$fetch) → do(用 $item) → after(引用 $do)。
    每个元素：do 跑一次、after 跑一次；体内下游是多个步骤，靠连线关联。
    """
    defn = _def(
        [
            {"id": "fetch", "kind": "code_run", "code": "x", "output_schema": {"type": "array"}},
            {"id": "each", "name": "遍历", "kind": "foreach", "items": "$fetch"},
            {"id": "do", "kind": "code_run", "code": "x",
             "output_schema": {"type": "object", "properties": {"v": {"type": "integer"}}}},
            {"id": "after", "kind": "code_run", "code": "x"},
        ],
        edges=[{"source": "fetch", "target": "each"}, {"source": "each", "target": "do"},
               {"source": "do", "target": "after"}],
    )
    ex = FakeExecutors({"fetch": [1, 2, 3], "do": {"v": 9}, "after": "done"})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(args={}))

    assert result["status"] == "ok"
    # 体内每个下游节点各按 3 个元素跑一遍 → 输出收集成 3 项列表。
    assert len(result["nodes"]["do"]["output"]) == 3
    assert len(result["nodes"]["after"]["output"]) == 3
    # foreach 头节点本身只汇总，不执行动作。
    assert "3 items" in result["nodes"]["each"]["decision"]


@pytest.mark.asyncio
async def test_runner_foreach_node_item_visible_in_body():
    """遍历体内多个步骤都能拿到 $item（账号列表 → 逐步用当前账号）。"""
    defn = _def(
        [
            {"id": "each", "name": "遍历", "kind": "foreach", "items": "$input.accounts"},
            {"id": "login", "kind": "run_session", "sop": "login as {$item.username}"},
            {"id": "report", "kind": "http", "url": "https://x/{$item.username}"},
        ],
        inputs=[{"key": "accounts", "type": "account", "site_url": "https://x.com"}],
        edges=[{"source": "each", "target": "login"}, {"source": "login", "target": "report"}],
    )
    ex = FakeExecutors({"login": "ok", "report": {"status": 200}})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(
        inputs={"accounts": [{"username": "a"}, {"username": "b"}]}, args={}))

    assert result["status"] == "ok"
    # login / report 各跑 2 遍（每个账号一遍），report 的 url 用到了当遍 $item。
    assert len(result["nodes"]["login"]["output"]) == 2
    assert len(result["nodes"]["report"]["output"]) == 2


@pytest.mark.asyncio
async def test_runner_foreach_over_scalar_runs_once():
    """遍历对象是标量（手动运行只挑了一个账号 → username 字符串）→ 当单元素跑一遍。"""
    defn = _def(
        [
            {"id": "each", "name": "遍历", "kind": "foreach", "items": "$input.accounts"},
            {"id": "do", "kind": "run_session", "sop": "login as {$item}"},
        ],
        inputs=[{"key": "accounts", "type": "account", "site_url": "https://x.com"}],
        edges=[{"source": "each", "target": "do"}],
    )
    ex = FakeExecutors({"do": "ok"})
    result = await WorkflowRunner(defn, executors=ex).run(
        WorkflowContext(inputs={"accounts": "12345"}, args={}))

    assert result["status"] == "ok"
    assert ex.calls == [("run_session", "do")]      # 只跑了一遍
    assert result["nodes"]["do"]["output"] == ["ok"]
    assert "1 items" in result["nodes"]["each"]["decision"]


@pytest.mark.asyncio
async def test_runner_foreach_body_error_is_surfaced_on_collapsed_node():
    """循环体内节点的报错原文必须透传到折叠结果，并标注是哪个元素。

    回归：折叠各元素产出时曾只留 status/output，把 error 丢了 —— 遍历里失败只能
    看到 failed 却查不到原因。
    """
    defn = _def(
        [
            {"id": "each", "name": "遍历", "kind": "foreach", "items": "$input.accounts"},
            {"id": "do", "kind": "run_session", "sop": "login as {$item}"},
        ],
        inputs=[{"key": "accounts", "type": "account", "site_url": "https://x.com"}],
        edges=[{"source": "each", "target": "do"}],
    )
    ex = FakeExecutors({"do": RuntimeError("该账号没有已保存密码")})
    result = await WorkflowRunner(defn, executors=ex).run(
        WorkflowContext(inputs={"accounts": ["a", "b"]}, args={}))

    node = result["nodes"]["do"]
    assert node["status"] == "failed"
    assert "该账号没有已保存密码" in node["error"]
    assert node["error"].startswith("[a] ")      # 首个失败元素可归因
    assert "另有 1 条" in node["error"]          # 其余同类折叠成计数


@pytest.mark.asyncio
async def test_validate_def_rejects_item_outside_foreach():
    """$item 只能用在遍历节点的下游；头节点自己的 items 表达式里用 $item 无意义 → 拒。"""
    bad = _def([{"id": "x", "kind": "code_run", "code": "{$item.y}"}])
    with pytest.raises(WorkflowDefError, match="not inside a foreach"):
        validate_def(bad)


@pytest.mark.asyncio
async def test_select_node_hit_default_when_no_case_matches():
    """select 无命中 → default（validate_def 要求有 default）。单运行、按声明入参分支。"""
    defn = _def(
        [
            {"id": "sel", "kind": "select",
             "cases": [{"when": "$input.account == 'zzz'", "set": {"k": "X"}}],
             "default": {"k": "fallback"}},
        ],
        inputs=[{"key": "account", "type": "account", "site_url": "https://x.com"}],
    )
    ex = FakeExecutors({})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(inputs={"account": "other"}, args={}))
    assert result["nodes"]["sel"]["output"] == {"k": "fallback"}


@pytest.mark.asyncio
async def test_select_node_picks_by_input_and_interpolates():
    """cron 拆出的每条运行 $input.account 各是一个账号；select 按它选代理/浏览器，
    set 的值支持 $ref 插值。"""
    defn = _def(
        [
            {"id": "pick", "kind": "select",
             "cases": [{"when": "$input.account == 'vip'", "set": {"proxy_id": "p_vip", "note": "{$input.account}"}}],
             "default": {"proxy_id": "p_default", "note": "{$input.account}"}},
        ],
        inputs=[{"key": "account", "type": "account", "site_url": "https://x.com"}],
    )
    ex = FakeExecutors({})
    runner = WorkflowRunner(defn, executors=ex)
    hit = await runner.run(WorkflowContext(inputs={"account": "vip"}, args={}))
    assert hit["nodes"]["pick"]["output"] == {"proxy_id": "p_vip", "note": "vip"}
    miss = await runner.run(WorkflowContext(inputs={"account": "guest"}, args={}))
    assert miss["nodes"]["pick"]["output"] == {"proxy_id": "p_default", "note": "guest"}


@pytest.mark.asyncio
async def test_runner_output_schema_violation_fails_node():
    defn = _def(
        [{"id": "a", "kind": "code_run", "code": "x",
          "output_schema": {"type": "object", "required": ["status"]}}],
    )
    ex = FakeExecutors({"a": {"wrong": 1}})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(args={}))
    assert result["nodes"]["a"]["status"] == "failed"
    assert "output_schema" in result["nodes"]["a"]["error"]


@pytest.mark.asyncio
async def test_runner_root_node_without_edges_always_runs():
    defn = _def([{"id": "a", "kind": "cache", "action": "get", "key": "wf:k"}])
    ex = FakeExecutors({"a": {"found": False}})
    runner = WorkflowRunner(defn, executors=ex)
    result = await runner.run(WorkflowContext(args={}))
    assert result["nodes"]["a"]["status"] == "ok"


@pytest.mark.asyncio
async def test_runner_persists_per_node_via_callback():
    seen: list[NodeResult] = []

    async def cb(res: NodeResult) -> None:
        seen.append(res)

    defn = _def([{"id": "a", "kind": "code_run", "code": "x"}])
    runner = WorkflowRunner(defn, executors=FakeExecutors({"a": 1}), on_node_done=cb)
    await runner.run(WorkflowContext(args={}))
    assert [r.node_id for r in seen] == ["a"]
    assert seen[0].status == "ok"


def test_topo_layers_orders_dependencies():
    defn = _def(
        [
            {"id": "c", "kind": "code_run", "code": "x"},
            {"id": "b", "kind": "code_run", "code": "x"},
            {"id": "a", "kind": "code_run", "code": "x"},
        ],
        edges=[{"source": "b", "target": "c"}, {"source": "a", "target": "b"}],
    )
    layers = topo_layers(defn)
    assert layers[0] == ["a"]
    assert layers[1] == ["b"]
    assert layers[2] == ["c"]


# ── http 步骤（发送请求）──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runner_http_step_passes_proxy_ref():
    """kind=http 分派到 run_http；proxy 字段照 params 透传（含 $input 插值成具体值）。"""
    defn = _def(
        [{"id": "call", "name": "调用", "kind": "http", "url": "{$input.site}",
          "proxy": "$input.px"}],
        inputs=[{"key": "site", "type": "string"}, {"key": "px", "type": "proxy"}],
    )
    validate_def(defn)
    ex = FakeExecutors({"call": {"status": 200, "body": "ok"}})
    runner = WorkflowRunner(defn, executors=ex)
    ctx = WorkflowContext(inputs={"site": "https://x.com", "px": "proxy_abc"}, args={})
    result = await runner.run(ctx)
    assert result["status"] == "ok"
    # params 已按声明插值：url 用 site 的值、proxy 用 px 的值（proxy-typed 入参的字符串形态）
    assert ("run_http", "call", "proxy_abc") in ex.calls
