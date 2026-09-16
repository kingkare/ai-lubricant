"""外部供应商框架导入：公共数据结构与适配器协议。

设计要点：
- 适配器只负责「把外部框架的渠道翻译成 ``ExternalChannel``」，**不碰数据库、
  不碰 admin 路由**。翻译是纯函数式的（``parse``）或只读网络（``fetch``），
  这样绝大部分逻辑可以脱离 DB/网络做单测。
- ``ExternalChannel`` 是**目标框架无关**的中间表示：字段口径对齐 ai-lubricant 的
  渠道基础配置（chat_protocols / models / accounts…），新增适配器只需产出它。
- ``source_meta`` 里**绝不能放密钥**。预览响应会把它整体回给前端，序列化层
  （``payload.py``）还会再按 secret 启发式剥一层做纵深防御。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ExternalChannelAccount:
    """外部渠道的一个账号（通常就是一个 API Key）。

    ``api_key`` 为空表示上游没有提供密钥——调用方据此**不写账号行**，
    而不是写一行空 key 的占位账号（占位行在运行池里会表现为永久认证失败）。
    """

    username: str
    api_key: str = ""
    remark: str = ""


@dataclass
class ExternalChannel:
    """一个外部渠道的框架无关中间表示。"""

    source: str
    external_id: str
    remark: str
    base_url: str = ""
    enabled: bool = True
    # chat_protocols 行的字段口径与 _normalize_chat_protocols 一致；
    # 导入侧只写一条行（网关已做客户端协议→上游协议转换），row 级 models 保持空。
    chat_protocols: list[dict[str, Any]] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    # provider_models 行：{"upstream_model_id": ..., "model_id": ...}。
    # 必须是 dict——裸字符串会被 db._provider_model_payload 按序列拆错。
    model_rows: list[dict[str, str]] = field(default_factory=list)
    model_id_rewrite_rules: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    accounts: list[ExternalChannelAccount] = field(default_factory=list)
    account_weight: int = 1
    models_path: str = "/v1/models"
    website_url: str = ""
    timeout: int = 120
    auto_update_models: bool = False
    # 无法作为静态凭据渠道导入时置 False，原因写在 errors 里。
    importable: bool = True
    source_meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def candidate_id(self) -> str:
        """预览/提交两侧的稳定标识：``{source}:{external_id}``。"""
        return f"{self.source}:{self.external_id}"

    @property
    def has_keys(self) -> bool:
        return any(acc.api_key for acc in self.accounts)


@dataclass
class ImportScan:
    """一次拉取/解析的结果。

    除了候选渠道，还带 ``dropped``（被规则剔除的条目及原因）与 ``warnings``
    （整批级别的提示）——用户必须能看见「上游有 42 个渠道、其中 3 个没填地址被
    过滤了」，而不是面对一个数目对不上的列表。
    """

    channels: list[ExternalChannel] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scanned: int = 0


@dataclass
class FetchRequest:
    """经外部框架的管理 API 拉取渠道列表。"""

    base_url: str
    token: str = ""
    user_id: str = ""
    proxy_config_id: str = ""
    page_size: int = 100
    max_pages: int = 50
    include_disabled: bool = True
    timeout: int = 20


@dataclass
class PasteRequest:
    """粘贴文本导入。``format`` 为 ``auto`` 时按信封形状自动识别。"""

    text: str
    format: str = "auto"


@runtime_checkable
class ChannelImporter(Protocol):
    """外部供应商框架适配器。"""

    id: str
    display_name: str
    supports_api_fetch: bool
    supports_paste: bool
    docs_url: str

    async def fetch(self, req: FetchRequest) -> ImportScan: ...

    def parse(self, req: PasteRequest) -> ImportScan: ...
