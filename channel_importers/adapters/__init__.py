"""适配器自注册入口。

**显式 import，不要用 importlib 字符串路径**——PyInstaller 打包（desktop/AiLubricant.spec）
靠静态分析收集模块，字符串路径会漏掉、需要手工维护 hiddenimports。
新增适配器时在下面加一行 import。
"""
from __future__ import annotations

from . import newapi  # noqa: F401 - 导入即注册

__all__ = ["newapi"]
