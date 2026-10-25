"""方法脚本运行时通道测试（MethodCapability：编译命名空间 + 调静态方法 + 类间互调）。

用假 loader（不建表、不碰 DB）验证：

1. 同步 / 异步静态方法调用；返回值透传。
2. **类间互调**：同作用域所有类编译进同一命名空间，A 类方法体里直接 ``B.foo()``。
3. 参数绑定：缺必填 / 多余参数 → 可读错误（不回显值）。
4. method_ref 形态：类不存在 / 方法不存在 → 可读错误。
5. 超时：同步方法 sleep 超 timeout → MethodCapabilityError。
6. 缓存：version 变更即重编译。

运行：``python -m pytest tests/test_method_channel.py -q``
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.method_channel import (  # noqa: E402
    MethodCapability,
    MethodCapabilityError,
)


def run(coro):
    return asyncio.run(coro)


CRYPTO = '''
class Crypto:
    """加解密。"""

    @staticmethod
    def md5(text):
        return {"digest": text}

    @staticmethod
    async def sign(payload, key="k"):
        return {"signed": payload, "key": key}

    @staticmethod
    def via_http(url):
        # 类间互调：直接引用同命名空间里的 Http 类
        return Http.fetch(url)
'''

HTTP = '''
class Http:
    """请求。"""

    @staticmethod
    def fetch(url):
        return {"body": f"got:{url}"}
'''


def _loader(modules):
    async def _load():
        return list(modules)
    return _load


def test_sync_and_async_call():
    cap = MethodCapability(loader=_loader([("Crypto", CRYPTO, 1)]))
    assert run(cap.call("Crypto.md5", {"text": "abc"})) == {"digest": "abc"}
    assert run(cap.call("Crypto.sign", {"payload": "p", "key": "s"})) == {"signed": "p", "key": "s"}
    # 默认值生效
    assert run(cap.call("Crypto.sign", {"payload": "p"})) == {"signed": "p", "key": "k"}


def test_cross_class_call():
    """类间互调：Crypto.via_http 里直接调 Http.fetch（同命名空间）。"""
    cap = MethodCapability(loader=_loader([("Crypto", CRYPTO, 1), ("Http", HTTP, 1)]))
    assert run(cap.call("Crypto.via_http", {"url": "u1"})) == {"body": "got:u1"}


def test_missing_class_readable_error():
    cap = MethodCapability(loader=_loader([("Crypto", CRYPTO, 1)]))
    with pytest.raises(MethodCapabilityError, match="方法类不存在"):
        run(cap.call("Nope.md5", {}))


def test_missing_method_readable_error():
    cap = MethodCapability(loader=_loader([("Crypto", CRYPTO, 1)]))
    with pytest.raises(MethodCapabilityError, match="方法不存在"):
        run(cap.call("Crypto.nope", {}))


def test_bad_ref_format():
    cap = MethodCapability(loader=_loader([("Crypto", CRYPTO, 1)]))
    with pytest.raises(MethodCapabilityError, match="method_ref 形如"):
        run(cap.call("NoDot", {}))


def test_unknown_arg_rejected():
    cap = MethodCapability(loader=_loader([("Crypto", CRYPTO, 1)]))
    with pytest.raises(MethodCapabilityError, match="未知参数"):
        run(cap.call("Crypto.md5", {"text": "x", "extra": "y"}))


def test_missing_required_arg_rejected():
    cap = MethodCapability(loader=_loader([("Crypto", CRYPTO, 1)]))
    with pytest.raises(MethodCapabilityError, match="缺少必填参数"):
        run(cap.call("Crypto.md5", {}))


def test_empty_library_readable_error():
    cap = MethodCapability(loader=_loader([]))
    with pytest.raises(MethodCapabilityError, match="方法脚本库为空"):
        run(cap.call("Crypto.md5", {}))


def test_user_exception_wrapped():
    src = '''
class Boom:
    """炸。"""

    @staticmethod
    def go():
        raise ValueError("bad value")
'''
    cap = MethodCapability(loader=_loader([("Boom", src, 1)]))
    with pytest.raises(MethodCapabilityError, match="ValueError"):
        run(cap.call("Boom.go", {}))


def test_timeout_sync():
    src = '''
import time
class Slow:
    """慢。"""

    @staticmethod
    def wait():
        time.sleep(5)
        return {}
'''
    cap = MethodCapability(loader=_loader([("Slow", src, 1)]), timeout=0.2)
    with pytest.raises(MethodCapabilityError, match="超时"):
        run(cap.call("Slow.wait", {}))


def test_cache_reuses_compiled_namespace():
    """同指纹（version 不变）→ 命名空间只编译一次；version 变 → 重编译。

    用模块顶层的 exec 副作用计数验证「编译次数」，loader 本身每次都会被调
    （只为算指纹），不代表重编译。
    """
    src = '''
class Counter:
    """计数。"""

    @staticmethod
    def get():
        return {"ok": True}
'''
    # 每次编译往全局塞一个标记；重编译会再走一遍 class 定义。
    # 更直接：包一层计数 loader + 检查 _cache 命中前后指纹一致。
    cap = MethodCapability(loader=_loader([("Counter", src, 1)]))
    run(cap.call("Counter.get", {}))
    first_ns = next(iter(cap._cache.values()))
    run(cap.call("Counter.get", {}))
    # 指纹未变 → 复用同一命名空间对象（未重编译）
    assert next(iter(cap._cache.values())) is first_ns


def test_cache_recompiles_on_version_bump():
    src = "class V:\n    @staticmethod\n    def get():\n        return 1\n"
    state = {"version": 1}

    async def loader():
        return [("V", src, state["version"])]

    cap = MethodCapability(loader=loader)
    run(cap.call("V.get", {}))
    first_ns = next(iter(cap._cache.values()))
    state["version"] = 2  # 源码改了（version+1）→ 指纹变 → 重编译
    run(cap.call("V.get", {}))
    assert next(iter(cap._cache.values())) is not first_ns

