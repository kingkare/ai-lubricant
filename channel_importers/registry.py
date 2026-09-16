"""外部供应商框架导入：适配器注册表。

适配器在模块导入时自注册（见 ``adapters/__init__.py``）。注册表本身不 import 任何
适配器，避免循环依赖。
"""
from __future__ import annotations

from .base import ChannelImporter
from .errors import UnknownImporterError

_IMPORTERS: dict[str, ChannelImporter] = {}


def register_importer(importer: ChannelImporter) -> None:
    """注册一个适配器；同 id 重复注册时后者覆盖前者（便于测试替换）。"""
    _IMPORTERS[importer.id] = importer


def get_importer(importer_id: str) -> ChannelImporter:
    key = (importer_id or "").strip()
    importer = _IMPORTERS.get(key)
    if importer is None:
        known = "、".join(sorted(_IMPORTERS)) or "（无）"
        raise UnknownImporterError(f"未知的外部供应商框架「{key}」，已注册：{known}")
    return importer


def list_importers() -> list[ChannelImporter]:
    """按 id 稳定排序，供 GET /admin/channel-importers。"""
    return [_IMPORTERS[key] for key in sorted(_IMPORTERS)]


def describe_importers() -> list[dict]:
    return [
        {
            "id": imp.id,
            "display_name": imp.display_name,
            "supports_api_fetch": bool(imp.supports_api_fetch),
            "supports_paste": bool(imp.supports_paste),
            "docs_url": imp.docs_url,
        }
        for imp in list_importers()
    ]
