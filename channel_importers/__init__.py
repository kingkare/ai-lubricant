"""外部供应商框架导入。

把其它 LLM 网关/中转框架（New API、one-api…）的渠道列表整体导入成 ai-lubricant
的本地渠道。每个框架一个适配器，见 ``adapters/``。

对外只暴露注册表读写与公共数据结构；适配器在 ``adapters/__init__.py`` 里显式
import 完成自注册——**不要用 importlib 字符串路径**，否则 PyInstaller 打包时
需要手工维护 hiddenimports。
"""
from __future__ import annotations

from .base import (
    ChannelImporter,
    ExternalChannel,
    ExternalChannelAccount,
    FetchRequest,
    ImportScan,
    PasteRequest,
)
from .errors import ChannelImportError, FetchError, ParseError, UnknownImporterError
from .registry import describe_importers, get_importer, list_importers, register_importer

# 导入即完成各适配器自注册。放在最后，避免适配器 import 本包时撞上未完成的初始化。
from . import adapters  # noqa: E402,F401

__all__ = [
    "ChannelImporter",
    "ChannelImportError",
    "ExternalChannel",
    "ExternalChannelAccount",
    "FetchError",
    "FetchRequest",
    "ImportScan",
    "ParseError",
    "PasteRequest",
    "UnknownImporterError",
    "adapters",
    "describe_importers",
    "get_importer",
    "list_importers",
    "register_importer",
]
