"""script 步骤方法脚本执行测试（06 §6.2：method_ref + args → 类静态方法）。

引擎不碰 DB——用假 loader 注入 ``ctx.methods``（与 ``ctx.ai`` 同范式）验证：

1. 同步方法调用 → 返回值进 ``{{result.*}}``/``{{response.*}}``，json_path 断言过。
2. 异步方法（``async def``）→ maybe_await 路径 + 默认参数。
3. **类间互调**：Crypto 方法体里直接引用 Http 类（同命名空间编译）。
4. ``post_cond`` 从 ``{{result.*}}`` 写回局部变量。
5. 未注入通道 / 方法不存在 → step error（kind 可读），不抛穿主循环。

运行：``python -m pytest tests/test_engine_script_steps.py -q``
"""
from __future__ import annotations

import asyncio
import os
import sys

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.engine import (  # noqa: E402
    DictScenarioLoader,
    ExecutionContext,
    ScenarioExecutor,
    STATUS_PASS,
    STEP_ERROR,
    STEP_PASS,
    build_default_assertion_registry,
    build_default_tool_registry,
)
from testing_platform.method_channel import MethodCapability  # noqa: E402

CRYPTO = '''
class Crypto:
    """加解密类。"""

    @staticmethod
    def md5(text: str) -> dict:
        return {"digest": text}

    @staticmethod
    async def sign(payload: str, key: str = "k") -> dict:
        return {"signed": payload, "key": key}

    @staticmethod
    def via_http(url: str) -> dict:
        # 类间互调：Http 与 Crypto 编译进同一命名空间
        return Http.fetch(url)
'''

HTTP = '''
class Http:
    """请求类。"""

    @staticmethod
    def fetch(url: str) -> dict:
        return {"text": "got:" + url}
'''


def _capability():
    async def loader():
        return [("Crypto", CRYPTO, 1), ("Http", HTTP, 1)]

    return MethodCapability(loader=loader)


def _executor(steps):
    return ScenarioExecutor(
        DictScenarioLoader({"SCN-SCRIPT": {"id": "SCN-SCRIPT", "steps": steps}}),
        tools=build_default_tool_registry(),
        assertions=build_default_assertion_registry(),
    )


def _run(steps, local=None, with_methods=True):
    ctx = ExecutionContext()
    if local:
        ctx.resolver.set_scope("local", local)
    if with_methods:
        ctx.methods = _capability()
    return asyncio.run(_executor(steps).execute("SCN-SCRIPT", ctx))


def test_script_sync_method_and_result_scope():
    result = _run(
        [
            {
                "step_key": "hash",
                "seq": 10,
                "type": "script",
                "params": {"method_ref": "Crypto.md5", "args": {"text": "{{local.raw}}"}},
                "post_checks": [
                    {"type": "json_path", "path": "digest", "operator": "eq", "expected": "abc"},
                ],
            }
        ],
        local={"raw": "abc"},
    )
    assert result.status == STATUS_PASS
    assert result.steps[0].status == STEP_PASS


def test_script_async_method_with_default_arg():
    result = _run(
        [
            {
                "step_key": "sign",
                "seq": 10,
                "type": "script",
                "method_ref": "Crypto.sign",
                "args": {"payload": "P"},
                "post_checks": [
                    {"type": "json_path", "path": "key", "operator": "eq", "expected": "k"},
                ],
                "post_cond": [{"set": "{{local.sign_key}} = {{result.key}}"}],
            }
        ]
    )
    assert result.steps[0].status == STEP_PASS
    # args 未被执行器插值（只插 params），工具内补插；post_cond 从 {{result.*}} 写回
    assert result.local_vars.get("sign_key") == "k"


def test_cross_class_call():
    """类间互调：Crypto.via_http 内部直接引用 Http 类（同作用域编译进同一命名空间）。"""
    result = _run(
        [
            {
                "step_key": "cross",
                "seq": 10,
                "type": "script",
                "params": {"method_ref": "Crypto.via_http", "args": {"url": "u9"}},
                "post_checks": [
                    {"actual": "{{result.text}}", "type": "contains", "expected": "got:u9"},
                ],
            }
        ]
    )
    assert result.steps[0].status == STEP_PASS


def test_missing_channel_is_step_error_not_crash():
    result = _run(
        [
            {
                "step_key": "no_ctx",
                "seq": 10,
                "type": "script",
                "params": {"method_ref": "Crypto.md5", "args": {"text": "x"}},
                "post_checks": [{"type": "json_path", "path": "digest"}],
            }
        ],
        with_methods=False,
    )
    step = result.steps[0]
    assert step.status == STEP_ERROR
    assert "未注入方法通道" in (step.error or {}).get("message", "")


def test_unknown_method_ref_is_step_error():
    result = _run(
        [
            {
                "step_key": "bad",
                "seq": 10,
                "type": "script",
                "params": {"method_ref": "Crypto.nope", "args": {}},
                "post_checks": [{"type": "json_path", "path": "digest"}],
            }
        ]
    )
    step = result.steps[0]
    assert step.status == STEP_ERROR
    assert "方法不存在" in (step.error or {}).get("message", "")


def test_missing_method_ref_reports_step_params():
    result = _run(
        [
            {
                "step_key": "noref",
                "seq": 10,
                "type": "script",
                "params": {},
                "post_checks": [{"type": "json_path", "path": "digest"}],
            }
        ]
    )
    step = result.steps[0]
    assert step.status == STEP_ERROR
    assert (step.error or {}).get("kind") == "step_params"
