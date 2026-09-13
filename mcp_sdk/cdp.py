"""cdp-bridge MCP 的方法式客户端。

把 ``cdp-bridge`` 服务暴露的每个工具折成一个同名方法（``get_tabs()`` /
``execute_js("...")`` / ``navigate(url)`` …），参数名与 MCP 工具 schema 一致。
底层沿用 MCP 现有调用链：``MCPManager._sse_call`` → ``_gateway_rpc`` → 进程内
``_handle_rpc``。SDK 不引入任何新的鉴权/归属概念：``token`` 就是 MCP 那套
``service_tokens["cdp-bridge"]``，由网关 ``_check_service_auth`` 判定。

工具表见 ``mcp_runtime/builtin_plugins/cdp_bridge_plugin.py::_TOOLS``。
未封装的新工具可直接走 ``call(tool, args)`` 逃生口。
"""
from __future__ import annotations

import uuid
from typing import Any

from agent.mcp_client import MCPManager


class CdpClient:
    """cdp-bridge MCP 的方法式客户端。一个实例持一个 tab 租约 holder。"""

    SERVICE = "cdp-bridge"

    def __init__(
        self,
        token: str = "",
        *,
        holder: str = "",
        session_id: str = "",
    ) -> None:
        # 复用既有 MCPManager：token 即 MCP 服务 token；holder 默认与 agent 的
        # ``agent:{uuid}`` 区分开，让 SDK 调用不与 agent 的 tab 租约互相踩。
        self._mgr = MCPManager(service_tokens={self.SERVICE: token or ""})
        self._mgr.lease_id = holder or f"cdp_sdk:{uuid.uuid4().hex[:16]}"
        if session_id:
            self._mgr.cdp_session_id = str(session_id)

    # ── 底层 ──────────────────────────────────────────────────────────────

    async def call(self, tool: str, args: dict | None = None) -> list[dict]:
        """调用 cdp-bridge 的任意工具，返回 MCP content 列表。

        ``tool`` 用工具全名（如 ``"browser_get_tabs"``）。这是逃生口：新工具
        没封装方法时也能用。
        """
        return await self._mgr._sse_call(self.SERVICE, tool, args or {})

    async def list_tools(self) -> list[dict]:
        """经 MCP tools/list 拿工具描述（自省用）。"""
        return await self._mgr._sse_list_tools(self.SERVICE)

    # ── 工具方法：一工具一方法，参数名对齐 MCP schema ──────────────────────

    async def get_tabs(self) -> list[dict]:
        """列出所有已连接的浏览器标签页（id / url / title）。"""
        return await self.call("browser_get_tabs")

    async def scan(
        self,
        *,
        tabs_only: bool = False,
        switch_tab_id: str = "",
        text_only: bool = False,
    ) -> list[dict]:
        """返回当前标签页的优化 HTML/文本 + 标签页列表。"""
        return await self.call("browser_scan", {
            "tabs_only": tabs_only,
            "switch_tab_id": switch_tab_id,
            "text_only": text_only,
        })

    async def execute_js(
        self,
        script: str,
        *,
        switch_tab_id: str = "",
        no_monitor: bool = False,
    ) -> list[dict]:
        """在浏览器里执行 JavaScript，返回结果与 DOM 变化。"""
        return await self.call("browser_execute_js", {
            "script": script,
            "switch_tab_id": switch_tab_id,
            "no_monitor": no_monitor,
        })

    async def switch_tab(self, tab_id: str) -> list[dict]:
        """切换 MCP 的活动标签页（不改可见的 Chrome 标签）。"""
        return await self.call("browser_switch_tab", {"tab_id": tab_id})

    async def focus_tab(self, tab_id: str) -> list[dict]:
        """把某个 Chrome 标签页带到前台（激活标签并聚焦其窗口）。"""
        return await self.call("browser_focus_tab", {"tab_id": tab_id})

    async def open_tab(
        self,
        *,
        url: str = "",
        new_window: bool = False,
        active: bool = True,
    ) -> list[dict]:
        """打开一个新标签页或新窗口。新标签页注册需一点时间，不会立刻出现在 get_tabs。"""
        return await self.call("browser_open_tab", {
            "url": url,
            "new_window": new_window,
            "active": active,
        })

    async def batch(
        self,
        commands: list[dict],
        *,
        tab_id: str = "",
        timeout: float = 20,
    ) -> list[dict]:
        """一次请求里跑多条扩展/CDP 命令。"""
        return await self.call("browser_batch", {
            "commands": commands,
            "tab_id": tab_id,
            "timeout": timeout,
        })

    async def wait(
        self,
        condition_js: str,
        *,
        timeout: float = 10,
        interval: float = 0.5,
        switch_tab_id: str = "",
    ) -> list[dict]:
        """等待某个 JS 条件返回真值。"""
        return await self.call("browser_wait", {
            "condition_js": condition_js,
            "timeout": timeout,
            "interval": interval,
            "switch_tab_id": switch_tab_id,
        })

    async def navigate(self, url: str) -> list[dict]:
        """把活动标签页导航到指定 URL。"""
        return await self.call("browser_navigate", {"url": url})

    async def screenshot(self, *, tab_id: str = "", path: str = "") -> list[dict]:
        """截取活动标签页的截图。

        Agent 运行时会把 PNG 字节落到 workspace 文件，返回的 ``path`` 才是引用方式；
        ``path`` 可指定目标工作区路径。
        """
        return await self.call("browser_screenshot", {"tab_id": tab_id, "path": path})

    async def network_start(self, *, tab_id: str = "", url_pattern: str = "") -> list[dict]:
        """开始抓取某标签页的网络请求（附加调试器并保持）。"""
        return await self.call("browser_network_start", {
            "tab_id": tab_id,
            "url_pattern": url_pattern,
        })

    async def network_get(self, *, tab_id: str = "") -> list[dict]:
        """读取目前已抓取的网络请求（不停止抓取）。"""
        return await self.call("browser_network_get", {"tab_id": tab_id})

    async def network_stop(self, *, tab_id: str = "") -> list[dict]:
        """停止抓取网络请求，分离调试器，返回全部已抓请求。"""
        return await self.call("browser_network_stop", {"tab_id": tab_id})

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def close(self) -> None:
        """释放本实例 holder 持有的 CDP tab 租约（= MCPManager.close 对 cdp 的处理）。"""
        await self._mgr.close()
