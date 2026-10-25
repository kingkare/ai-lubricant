"""``agent_message_events`` 的时间线表：DDL 自证 + 插入行宽自检。

这张表是 agent 对话「时间线顺序」的唯一权威（每帧一行、``seq`` 有序），形态对齐
``task_messages``。与后者不同：**只有数据服务进程写它**（``agent/conversation_store``），
DDL 单一事实源，所以这里不需要跨进程逐字比对，需要的是另外两件事：

1. 插入列名与 DDL 声明的列**逐个对齐**（clickhouse-connect 按位插值，错位是静默的：
   ``tool_name`` 会落进 ``event_kind``）。历史 bug 见 test_conversation_store_columns
   的注释——加列只改了一边，报错点离手误很远。
2. ``version == seq``：ReplacingMergeTree 按 version 收敛，排序键是
   ``(conversation_id, message_id, seq)``。version 不等于 seq 时，重投同一帧不会
   收敛（两个不同 version 都留下），时间线出现重复段。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from integrations.clickhouse import AGENT_MESSAGE_EVENTS_COLUMNS, AGENT_MESSAGE_EVENTS_DDL

_ROOT = Path(__file__).resolve().parent.parent


def _ddl_column_names(ddl: str) -> list[str]:
    """取 CREATE TABLE 括号里的列名（顺序即声明顺序）。

    不能按最后一个 ``)`` 截：``ORDER BY (conversation_id, message_id, seq)`` 也带
    括号，会把排序键当成两列。这里取第一个 ``(`` 到它的**配对**右括号。
    """
    start = ddl.index("(")
    depth = 0
    end = start
    for i in range(start, len(ddl)):
        if ddl[i] == "(":
            depth += 1
        elif ddl[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = ddl[start + 1: end]
    names: list[str] = []
    depth = 0
    current = ""
    for ch in body:
        if ch == "(":
            depth += 1
            current += ch
        elif ch == ")":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            names.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        names.append(current)
    # 每列形如 "name Type ..." —— 首 token 是列名。
    return [re.sub(r"\s+", " ", n).strip().split(" ")[0] for n in names if re.sub(r"\s+", " ", n).strip()]


def test_ddl_declares_every_insert_column_in_order():
    assert _ddl_column_names(AGENT_MESSAGE_EVENTS_DDL) == AGENT_MESSAGE_EVENTS_COLUMNS


def test_engine_and_sort_key_are_what_the_read_path_assumes():
    flat = re.sub(r"\s+", " ", AGENT_MESSAGE_EVENTS_DDL)
    assert "ENGINE = ReplacingMergeTree(version)" in flat
    assert "ORDER BY (conversation_id, message_id, seq)" in flat


def test_event_insert_row_width_matches_columns():
    """conversation_store 造的那一行必须与列名等长（同 agent_messages 的教训）。"""
    tree = ast.parse((_ROOT / "agent" / "conversation_store.py").read_text(encoding="utf-8"))
    widths: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "insert_message_event_row"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            widths.append((node.lineno, len(node.args[0].elts)))
        # add_message_event 用列表字面量组装 row，再交给 insert_message_event_row。
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "row":
            if isinstance(node.value, ast.List):
                widths.append((node.lineno, len(node.value.elts)))
    assert widths, "expected conversation_store to build an agent_message_events row"
    n = len(AGENT_MESSAGE_EVENTS_COLUMNS)
    for lineno, w in widths:
        assert w == n, f"conversation_store.py:{lineno} builds {w} values, columns has {n}"
