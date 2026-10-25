"""技能斜杠命令支持 —— 解析、目录聚合与激活推导的单测。

不依赖 Postgres/ClickHouse：所有 DB 访问用 monkeypatch 替换成 fake。
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)


class TestParseSkillTokens:
    """parse_skill_tokens: inline anywhere, exact-match against catalog."""

    def test_parse_inline_anywhere(self):
        from agent.skill_slash import parse_skill_tokens, SkillCatalog, SkillItem

        catalog = SkillCatalog(by_name={"browser": SkillItem(kind="sop", name="browser")})
        assert parse_skill_tokens("fix /browser please", catalog) == ["browser"]
        assert parse_skill_tokens("a /browser b /cal c", catalog) == ["browser"]

    def test_parse_false_positives(self):
        from agent.skill_slash import parse_skill_tokens, SkillCatalog, SkillItem

        catalog = SkillCatalog(by_name={"hosts": SkillItem(kind="sop", name="hosts")})
        # /etc/hosts → second segment preceded by "/" → killed
        assert parse_skill_tokens("/etc/hosts", catalog) == []
        # https://x/web → ":" and "/" lookbehind kill it
        assert parse_skill_tokens("https://example.com", catalog) == []
        # C:\temp → backslash context + ":" lookbehind
        assert parse_skill_tokens("C:\\temp", catalog) == []
        # ./foo ../x → killed
        assert parse_skill_tokens("./foo", catalog) == []
        assert parse_skill_tokens("../x", catalog) == []
        # pure digits → skipped
        assert parse_skill_tokens("/123", catalog) == []
        # trailing slash path segment → killed
        assert parse_skill_tokens("/etc/", catalog) == []

    def test_parse_case_insensitive_and_dash_underscore(self):
        from agent.skill_slash import parse_skill_tokens, SkillCatalog, SkillItem

        catalog = SkillCatalog(by_name={"browser_scan": SkillItem(kind="mcp", name="browser_scan")})
        # lowercase token matches "browser_scan"
        assert parse_skill_tokens("/browser_scan", catalog) == ["browser_scan"]
        # uppercase/dash variant also matches
        assert parse_skill_tokens("/Browser-Scan", catalog) == ["browser_scan"]
        # underscore variant also matches
        assert parse_skill_tokens("/browser_scan", catalog) == ["browser_scan"]

    def test_parse_code_fence_ignored(self):
        from agent.skill_slash import parse_skill_tokens, SkillCatalog, SkillItem

        catalog = SkillCatalog(by_name={"mail": SkillItem(kind="sop", name="mail")})
        # token inside ``` ... ``` is ignored
        text = """
        some text
        ```
        /mail
        ```
        more text /mail
        """
        assert parse_skill_tokens(text, catalog) == ["mail"]

    def test_parse_no_hit_unknown_token(self):
        from agent.skill_slash import parse_skill_tokens, SkillCatalog, SkillItem

        catalog = SkillCatalog(by_name={"browser": SkillItem(kind="sop", name="browser")})
        # unknown token leaves text verbatim (no expansion)
        assert parse_skill_tokens("/unknown-token", catalog) == []

    def test_parse_dedup_order_preserved(self):
        from agent.skill_slash import parse_skill_tokens, SkillCatalog, SkillItem

        catalog = SkillCatalog(by_name={
            "browser": SkillItem(kind="sop", name="browser"),
            "plan": SkillItem(kind="sop", name="plan"),
        })
        # order of first appearance preserved; dedup
        assert parse_skill_tokens("/plan /browser /plan", catalog) == ["plan", "browser"]


class TestBuildCatalog:
    """build_catalog: aggregation from SOP + MCP sources."""

    @pytest.mark.asyncio
    async def test_build_catalog_sop_only(self, monkeypatch):
        from agent.skill_slash import build_catalog
        import agent.sop_service as ss

        rows = [
            {"id": "1", "name": "Browser SOP", "filename": "builtin/browser_sop.md", "enabled": True, "exists": True},
            {"id": "2", "name": "Plan SOP", "filename": "builtin/plan_sop.md", "enabled": True, "exists": True},
        ]
        monkeypatch.setattr(ss, "list_for_agent", AsyncMock(return_value={"available": rows}))

        catalog = await build_catalog(agent_id=7, mcp_user_id=None)
        names = {it.name for it in catalog.items}
        assert "browser" in names
        assert "plan" in names
        assert all(it.kind == "sop" for it in catalog.items)

    @pytest.mark.asyncio
    async def test_build_catalog_mcp_only(self, monkeypatch):
        from agent.skill_slash import build_catalog
        import agent.sop_service as ss
        import agent.mcp_client as mc

        services = [
            {"name": "mail", "description": "Email service"},
            {"name": "site-runner", "description": "Site builder"},
        ]
        monkeypatch.setattr(ss, "list_for_agent", AsyncMock(return_value={"available": []}))
        monkeypatch.setattr(mc, "resolve_effective_services", AsyncMock(return_value=services))

        catalog = await build_catalog(agent_id=7, mcp_user_id=42)
        names = {it.name for it in catalog.items}
        assert "mail" in names
        assert "site-runner" in names
        assert all(it.kind == "mcp" for it in catalog.items)

    @pytest.mark.asyncio
    async def test_build_catalog_collision_priority(self, monkeypatch):
        from agent.skill_slash import build_catalog
        import agent.sop_service as ss
        import agent.mcp_client as mc

        # SOP "browser-sop" slugs to "browser"; MCP "browser" would collide.
        rows = [{"id": "1", "name": "Browser SOP", "filename": "builtin/browser_sop.md", "enabled": True, "exists": True}]
        services = [{"name": "browser", "description": "MCP browser"}]

        monkeypatch.setattr(ss, "list_for_agent", AsyncMock(return_value={"available": rows}))
        monkeypatch.setattr(mc, "resolve_effective_services", AsyncMock(return_value=services))

        catalog = await build_catalog(agent_id=7, mcp_user_id=42)
        # SOP wins over MCP when slug collides (items are appended SOP-first)
        browser_item = catalog.by_name.get("browser")
        assert browser_item is not None
        assert browser_item.kind == "sop"


class TestCollectActivatedSkills:
    """collect_activated_skills: scan **only the current message**, dedup, cap."""

    @pytest.mark.asyncio
    async def test_collect_scans_current_message_only(self):
        """The skill is live because THIS message names it, not an earlier one."""
        from agent.skill_slash import collect_activated_skills, SkillItem, SkillCatalog

        catalog = SkillCatalog(by_name={"browser": SkillItem(kind="sop", name="browser")})
        activated = await collect_activated_skills(
            agent_id=7, catalog=catalog, current_text="use /browser to fix",
        )
        assert [it.name for it in activated] == ["browser"]

    @pytest.mark.asyncio
    async def test_collect_empty_without_current_text(self):
        """No slash token in this message → nothing activates, even if history had one."""
        from agent.skill_slash import collect_activated_skills, SkillItem, SkillCatalog

        catalog = SkillCatalog(by_name={"browser": SkillItem(kind="sop", name="browser")})
        activated = await collect_activated_skills(
            agent_id=7, catalog=catalog, current_text="continue with the previous task",
        )
        assert activated == []

    @pytest.mark.asyncio
    async def test_collect_empty_when_no_matches(self):
        from agent.skill_slash import collect_activated_skills, SkillCatalog, SkillItem

        catalog = SkillCatalog(by_name={"browser": SkillItem(kind="sop", name="browser")})
        activated = await collect_activated_skills(
            agent_id=7, catalog=catalog, current_text="/unknown /path/to/file",
        )
        assert activated == []

    @pytest.mark.asyncio
    async def test_collect_max_limit(self):
        from agent.skill_slash import collect_activated_skills, SkillItem, SkillCatalog

        catalog = SkillCatalog(by_name={
            "browser": SkillItem(kind="sop", name="browser"),
            "plan": SkillItem(kind="sop", name="plan"),
            "goal": SkillItem(kind="sop", name="goal"),
            "auto": SkillItem(kind="sop", name="auto"),
            "task": SkillItem(kind="sop", name="task"),
        })
        activated = await collect_activated_skills(
            agent_id=7, catalog=catalog, current_text="/browser /plan /goal /auto /task",
            max_skills=3,
        )
        assert [it.name for it in activated] == ["browser", "plan", "goal"]


class TestBuildSkillMessageSuffix:
    """build_skill_message_suffix: basic info (name/desc/location), not the body."""

    def test_suffix_carries_basic_info(self):
        from agent.skill_slash import SkillItem, build_skill_message_suffix

        items = [SkillItem(
            kind="sop", name="browser", display_name="Browser",
            description="page read/click/JS", location="memory/sop/browser_sop.md",
        )]
        suffix = build_skill_message_suffix(items)
        assert "/browser" in suffix
        assert "page read/click/JS" in suffix
        # location so the model can file_read on demand; full content not dumped.
        assert "memory/sop/browser_sop.md" in suffix
        assert "file_read" in suffix

    def test_suffix_mcp_names_capability_call(self):
        from agent.skill_slash import SkillItem, build_skill_message_suffix

        items = [SkillItem(kind="mcp", name="mail", service="mail", description="email")]
        suffix = build_skill_message_suffix(items)
        assert "/mail" in suffix
        assert "mail" in suffix and "capability_call" in suffix

    def test_suffix_empty_without_items(self):
        from agent.skill_slash import build_skill_message_suffix

        assert build_skill_message_suffix([]) == ""


class TestAppendToLastUser:
    """append_to_last_user: the activation lands in THIS turn's user message."""

    def test_appends_to_last_user_message(self):
        from agent.skill_slash import append_to_last_user

        messages = [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "do /browser now"},
        ]
        assert append_to_last_user(messages, "[本轮技能]\n- /browser")
        # Only the LAST user message changed; earlier turns stay verbatim.
        assert messages[1]["content"] == "first"
        assert messages[3]["content"].endswith("[本轮技能]\n- /browser")
        assert messages[3]["content"].startswith("do /browser now")

    def test_no_user_message_returns_false(self):
        from agent.skill_slash import append_to_last_user

        messages = [{"role": "system", "content": "base"}]
        assert not append_to_last_user(messages, "note")
        assert messages[0]["content"] == "base"

    def test_empty_suffix_noop(self):
        from agent.skill_slash import append_to_last_user

        messages = [{"role": "user", "content": "hi"}]
        assert not append_to_last_user(messages, "")
        assert messages[0]["content"] == "hi"

    def test_multimodal_content_list_appended(self):
        from agent.skill_slash import append_to_last_user

        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        assert append_to_last_user(messages, "NOTE")
        assert messages[0]["content"][-1] == {"type": "text", "text": "NOTE"}


def _resources_agent(monkeypatch, scene=None, capability_index=None, sop_bodies=None,
                     seeded_bodies=None):
    """A GenericAgent whose _ensure_resources runs without DB or real files.

    与 tests/test_agent_main.py 的同名 harness 同构，另把内置工具能力的种子 SOP
    读取（_read_seeded_sop）做成参数。
    """
    from agent import agent_main as am
    from agent.agent_main import GenericAgent
    from agent.config import AgentConfig

    agent = GenericAgent(AgentConfig(), agent_id=7, tools=Mock())
    agent._capability_index = capability_index or []
    agent._scene = scene

    async def fake_ensure_tools(self):
        return self._tools
    monkeypatch.setattr(GenericAgent, "_ensure_tools", fake_ensure_tools)
    bodies = sop_bodies or {}
    monkeypatch.setattr(GenericAgent, "_read_sop_body", lambda self, ref: bodies.get(ref, ""))
    seeded = seeded_bodies or {}
    monkeypatch.setattr(
        GenericAgent, "_read_seeded_sop",
        AsyncMock(side_effect=lambda name: seeded.get(name, "")),
    )
    monkeypatch.setattr(am, "logger", Mock())

    import agent.file_memory as fm
    monkeypatch.setattr(fm, "ensure_agent_memory", lambda _id: None)
    monkeypatch.setattr(fm, "read_l1", lambda _id: "L1-INDEX-CONTENT")
    import agent.sop_service as ss
    monkeypatch.setattr(ss, "sync_agent_sops", AsyncMock(return_value=None))
    import agent.self_improvement_log as sil
    monkeypatch.setattr(sil, "system_prompt_segment", lambda _id: "")
    return agent


_MAIL_INDEX = [
    {
        "service": "mail",
        "method_names": ["send"],
        "methods": [{"name": "send", "description": "Send email"}],
        "browser": False,
        "sop_ref": "memory/sop/mcp/mail_sop.md",
    },
    {
        "service": "cal",
        "method_names": ["list"],
        "methods": [{"name": "list", "description": "List events"}],
        "browser": False,
        "sop_ref": "memory/sop/mcp/cal_sop.md",
    },
]
_MAIL_BODY = {"memory/sop/mcp/mail_sop.md": "# mail SOP\nfull schema here"}


class TestEnsureResourcesResolvesSceneCapabilities:
    """_ensure_resources: MCP service capabilities get promoted by the scene,
    and skill activation no longer feeds the system prompt at all."""

    @pytest.mark.asyncio
    async def test_scene_mcp_service_inlined_not_pointer(self, monkeypatch):
        agent = _resources_agent(
            monkeypatch,
            scene=SimpleNamespace(capabilities=("mail",)),
            capability_index=_MAIL_INDEX,
            sop_bodies=_MAIL_BODY,
        )

        _tools, prefix = await agent._ensure_resources()

        assert "[Scene Capability: mail]" in prefix
        assert "full schema here" in prefix
        # mail 被提升后从通用索引里摘走；cal 仍是指针
        cap_section = prefix.split("[Available Capabilities]")[-1]
        assert "mail" not in cap_section
        assert "cal" in cap_section
        assert "memory/sop/mcp/cal_sop.md" in cap_section

    @pytest.mark.asyncio
    async def test_unmounted_service_dropped(self, monkeypatch):
        agent = _resources_agent(
            monkeypatch,
            scene=SimpleNamespace(capabilities=("ghost",)),
            capability_index=[],
        )

        _tools, prefix = await agent._ensure_resources()

        # ghost 不在 capability_index → 静默跳过，不留死指针
        assert "ghost" not in prefix

    @pytest.mark.asyncio
    async def test_no_scene_caps_all_pointer(self, monkeypatch):
        """回归锚点：无场景时全部走指针，不内联任何服务。"""
        agent = _resources_agent(
            monkeypatch,
            scene=SimpleNamespace(capabilities=()),
            capability_index=_MAIL_INDEX,
            sop_bodies=_MAIL_BODY,
        )

        _tools, prefix = await agent._ensure_resources()

        assert "[Scene Capability: mail]" not in prefix
        cap_section = prefix.split("[Available Capabilities]")[-1]
        assert "mail" in cap_section and "cal" in cap_section

    @pytest.mark.asyncio
    async def test_skill_no_longer_touches_system_prompt(self, monkeypatch):
        """技能激活改走消息注入（option B），不再写 _skill_services/_skill_sops。"""
        from agent.agent_main import GenericAgent
        from agent.config import AgentConfig

        agent = GenericAgent(AgentConfig(), agent_id=7)
        assert not hasattr(agent, "_skill_services")
        assert not hasattr(agent, "_skill_sops")


class TestBuiltinCapabilitySopInlined:
    """A built-in tool capability gets its seeded SOP inlined (not a pointer),
    with the new-tab policy section removed for a page-bound scene."""

    @pytest.mark.asyncio
    async def test_web_capability_inlines_sop_without_new_tab(self, monkeypatch):
        agent = _resources_agent(
            monkeypatch,
            scene=SimpleNamespace(capabilities=("web",)),
            capability_index=[],
            seeded_bodies={"browser_sop.md": _BROWSER_SOP_SAMPLE},
        )

        _tools, prefix = await agent._ensure_resources()

        assert "[Scene Capability: web]" in prefix
        # body content present
        assert "watch_network" in prefix
        # the scene-conditional new-tab policy is dropped for a page-bound scene
        assert "New task → new tab" not in prefix
        assert "## New tab vs. in-page navigation" not in prefix
        # other sections survive
        assert "## Verified pitfalls" in prefix

    @pytest.mark.asyncio
    async def test_unknown_builtin_capability_silent_skip(self, monkeypatch):
        """A capability name that is neither in _BUILTIN_CAPABILITY_SOPS nor a
        mounted MCP service → nothing inlined, no dead pointer."""
        agent = _resources_agent(
            monkeypatch,
            scene=SimpleNamespace(capabilities=("nonexistent_tool",)),
            capability_index=[],
            seeded_bodies={},
        )

        _tools, prefix = await agent._ensure_resources()

        assert "nonexistent_tool" not in prefix


_BROWSER_SOP_SAMPLE = """# Browser SOP

## Tool

- `watch_network` / `read_network` / `unwatch_network`: subscribe to everything.

## New tab vs. in-page navigation

**New task → new tab.** Use it as the entry point for any new task.

- A newly opened tab needs a moment to load.

## Verified pitfalls

- **`await` needs an explicit `return`.**
"""


class TestBrowserSkillNeverAdvertised:
    """Browser-class services never advertised even if activated."""

    @pytest.mark.asyncio
    async def test_browser_class_skipped(self, monkeypatch):
        agent = _resources_agent(
            monkeypatch,
            scene=SimpleNamespace(capabilities=()),
            capability_index=[
                {
                    "service": "cdp-bridge",
                    "method_names": ["browser_scan"],
                    "methods": [{"name": "browser_scan", "description": "Scan tab"}],
                    "browser": True,
                    "sop_ref": "memory/sop/mcp/cdp-bridge_sop.md",
                },
            ],
            sop_bodies={"memory/sop/mcp/cdp-bridge_sop.md": "# cdp-bridge"},
        )

        _tools, prefix = await agent._ensure_resources()

        # browser-class 照旧跳过（走 web 原子工具），激活也不破例
        assert "cdp-bridge" not in prefix
