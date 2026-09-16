"""外部供应商框架导入：异常类型。

所有异常都带 ``detail``——它是**面向用户**的中文说明，路由层直接透出到前端提示，
不需要再翻译一层。``FetchError`` 额外区分「上游拒绝」与「网络/协议故障」，
让路由层能给出可执行的排查建议而不是一句「失败了」。
"""
from __future__ import annotations


class ChannelImportError(Exception):
    """导入链路的基类异常。``detail`` 直接作为 HTTP 响应里的 detail 透出。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class UnknownImporterError(ChannelImportError):
    """请求了一个未注册的框架 id。"""


class FetchError(ChannelImportError):
    """从外部框架拉取渠道列表失败（网络、鉴权、信封格式）。"""


class ParseError(ChannelImportError):
    """粘贴的文本无法解析成任何已知形状。"""
