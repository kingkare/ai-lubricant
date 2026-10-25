"""方法脚本 AST 解析器测试（§5.1 重构：一个脚本=一个类，方法全 @staticmethod）。

覆盖（纯函数，无 IO）：

1. 正路：单类多静态方法——类名/docstring/方法清单/入参说明（注解+默认值+Args 段）。
2. 严格单类：0 类 / 2 类 / 顶层函数 → :class:`MethodSourceError`。
3. 全静态：实例方法 / classmethod → 报错；dunder → 报错。
4. 语法错误保留行号。
5. signature 形态（params 逐参 + returns），供 ``mc_test_method.signature`` 落库。

运行：``python -m pytest tests/test_method_source.py -q``
"""
from __future__ import annotations

import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.method_source import (  # noqa: E402
    MethodSourceError,
    parse_class_source,
)

GOOD = '''
class Crypto:
    """加解密工具类。"""

    @staticmethod
    def md5(text: str) -> str:
        """计算 MD5 摘要。

        Args:
            text: 被摘要的文本
        Returns:
            十六进制摘要
        """
        return text

    @staticmethod
    async def sign(payload: dict, key: str = "secret") -> dict:
        return {"payload": payload, "key": key}
'''


def test_parse_single_class_static_methods():
    parsed = parse_class_source(GOOD)
    assert parsed.name == "Crypto"
    assert parsed.docstring == "加解密工具类。"
    assert parsed.method_names() == ["md5", "sign"]
    md5, sign = parsed.methods
    assert md5.is_async is False
    assert sign.is_async is True


def test_signature_carries_param_docs():
    parsed = parse_class_source(GOOD)
    md5 = parsed.methods[0]
    assert md5.signature == {
        "params": [{"name": "text", "type": "str", "default": "", "description": "被摘要的文本"}],
        "returns": {"type": "str", "description": "十六进制摘要"},
    }
    sign = parsed.methods[1]
    by_name = {p["name"]: p for p in sign.signature["params"]}
    assert by_name["key"]["default"] == "'secret'"  # 默认值取源码文本
    assert by_name["payload"]["type"] == "dict"


def test_zero_class_rejected():
    with pytest.raises(MethodSourceError, match="未找到类定义"):
        parse_class_source("x = 1\n")


def test_two_classes_rejected():
    with pytest.raises(MethodSourceError, match="只能定义一个类"):
        parse_class_source("class A:\n    pass\n\nclass B:\n    pass\n")


def test_top_level_function_rejected():
    with pytest.raises(MethodSourceError, match="顶层不允许定义函数"):
        parse_class_source("class A:\n    pass\n\ndef helper():\n    pass\n")


def test_instance_method_rejected():
    with pytest.raises(MethodSourceError, match="必须是 @staticmethod"):
        parse_class_source("class A:\n    def normal(self): ...\n")


def test_classmethod_rejected():
    with pytest.raises(MethodSourceError, match="必须是 @staticmethod"):
        parse_class_source("class A:\n    @classmethod\n    def make(cls): ...\n")


def test_dunder_rejected():
    with pytest.raises(MethodSourceError, match="魔法方法"):
        parse_class_source("class A:\n    def __init__(self): ...\n")


def test_duplicate_method_rejected():
    src = "class A:\n    @staticmethod\n    def run(): ...\n    @staticmethod\n    def run(): ...\n"
    with pytest.raises(MethodSourceError, match="重名"):
        parse_class_source(src)


def test_syntax_error_carries_line():
    with pytest.raises(MethodSourceError) as exc_info:
        parse_class_source("class A:\n  def broken(:\n")
    assert exc_info.value.line is not None


def test_empty_class_ok_no_methods():
    parsed = parse_class_source("class OnlyName:\n    pass\n")
    assert parsed.name == "OnlyName"
    assert parsed.methods == []


def test_varargs_kwargs_collected():
    src = '''
class Bag:
    """收集。"""

    @staticmethod
    def pack(*args, tag: str = "t", **kwargs) -> dict:
        return {}
'''
    parsed = parse_class_source(src)
    names = [p["name"] for p in parsed.methods[0].signature["params"]]
    assert names == ["*args", "tag", "**kwargs"]
