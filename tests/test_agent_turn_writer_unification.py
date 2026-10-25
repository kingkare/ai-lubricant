"""api.py 与 cdp_chat_service.py 的 on_event 已统一到 ConversationTurnWriter。

历史病根：两份 ``on_event`` 各抄一份 ``collected_*`` 累积器，按手维护 → 必然漂移：
CDP 漏了子 agent 聚合、``checkpoint`` 漏写 ``subagents=``、错误处理一个进正文一个
进 error 字段……每个不一致都是一份「存储形态≠显示形态」。本轮把它们收敛到
``agent/turn_writer.py``，两处的 on_event 各缩成 ``await writer.handle(event)``。

这里用源码静态断言钉死收敛：两处都导入并构造了 writer、不再有 ``collected_*``
局部变量、不再各自定义 ``_merge_question_into_content``。谁哪天又往 api/cdp 里
粘一段内联累积逻辑，立刻红。
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_both_modules_import_and_construct_the_writer():
    for rel in ("agent/api.py", "agent/cdp_chat_service.py"):
        tree = ast.parse(_src(rel))
        imported = constructed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "agent.turn_writer":
                # 或累积，不能赋值：同模块可能有多条 import（api.py 里还有一个
                # _apply_subagent_event 转发 shim），最后一条不含本名就会误判 False。
                imported = imported or any(a.name == "ConversationTurnWriter" for a in node.names)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ConversationTurnWriter":
                constructed = True
        assert imported, f"{rel} no longer imports ConversationTurnWriter — 别把统一实现改回两份"
        assert constructed, f"{rel} no longer constructs ConversationTurnWriter"


def test_no_inline_collected_locals():
    """两处都不该再有 collected_* 局部变量（统一到 writer.*）。"""
    for rel in ("agent/api.py", "agent/cdp_chat_service.py"):
        tree = ast.parse(_src(rel))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            if isinstance(node, ast.Attribute) and isinstance(node.attr, str):
                names.add(node.attr)
        leaked = [n for n in names if n.startswith("collected_")]
        assert not leaked, f"{rel} reintroduced collected_* locals: {leaked}"


def test_cdp_no_longer_defines_merge_question_into_content():
    """``_merge_question_into_content`` 的唯一实现住在 turn_writer；CDP 的副本已删。"""
    tree = ast.parse(_src("agent/cdp_chat_service.py"))
    defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_merge_question_into_content" not in defs


def test_turn_writer_is_the_single_implementation():
    """turn_writer 提供 apply_frame/handle/checkpoint/final_state/fold 全套。"""
    tree = ast.parse(_src("agent/turn_writer.py"))
    defs = {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("_apply_subagent_event", "merge_question_into_content",
                 "apply_frame", "handle", "checkpoint", "final_state", "fold_frames_to_fields"):
        assert name in defs, f"turn_writer lost {name}"


def test_turn_writer_has_subagent_reasoning_branch():
    """① 的核心：subagent_reasoning 分支必须在，否则子 agent 思考又只实时不落库。"""
    src = _src("agent/turn_writer.py")
    assert "subagent_reasoning" in src
