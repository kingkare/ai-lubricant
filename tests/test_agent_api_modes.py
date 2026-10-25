from pathlib import Path

import pytest

import agent.api as agent_api


def test_send_message_request_accepts_execution_modes():
    interact = agent_api.SendMessageRequest(content="hi")
    assert interact.mode == "interact"
    assert interact.goal_config is None

    goal = agent_api.SendMessageRequest(
        content="work",
        mode="goal",
        goal_config={"objective": "improve docs", "budget_seconds": 600, "max_turns": 8},
    )
    assert goal.mode == "goal"
    assert goal.goal_config.objective == "improve docs"
    assert goal.goal_config.budget_seconds == 600

    # supervisor = 监督模式：无需 goal_config（普通发消息即可，用户口径）。
    supervisor = agent_api.SendMessageRequest(content="decompose and delegate", mode="supervisor")
    assert supervisor.mode == "supervisor"
    assert supervisor.goal_config is None

    with pytest.raises(Exception):
        agent_api.SendMessageRequest(content="x", mode="invalid")


def test_supervisor_hint_shared_and_inlines_redline():
    """红线常量两处注入点共用（api + cdp import 同一份），含红线关键句与 SOP 路径。"""
    from agent.cdp_chat_service import _SUPERVISOR_HINT as cdp_hint

    assert agent_api._SUPERVISOR_HINT is cdp_hint  # 同一份常量，防两处文案漂移
    assert "spawn_subagent" in agent_api._SUPERVISOR_HINT
    assert "supervisor_sop.md" in agent_api._SUPERVISOR_HINT
    assert "read-only" in agent_api._SUPERVISOR_HINT


def test_attachment_default_uses_real_agent_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Legacy/empty DB workspace resolves to data/agents/<id>, not shared agent/workspace."""
    from agent import file_memory as fm

    monkeypatch.setattr(fm, "_MEMORY_ROOT", tmp_path / "agents")
    relative, target = agent_api._attachment_target(str(fm.agent_root(7)), "conv-1", "note.txt")

    assert relative.startswith(".chat-attachments/conv-1/")
    assert target.is_relative_to((tmp_path / "agents" / "7").resolve())
