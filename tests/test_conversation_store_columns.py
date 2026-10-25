"""agent_conversations 插入行与列名必须等长（防手误回归）。

历史 bug：给会话表加 run_status/run_status_info 两列时，只改了 conversation_store
的 4 处 insert 行（→17），忘了同步 clickhouse 的 column_names（→15）。clickhouse_connect
在深处抛 "Insert data column count does not match column names"，表现为
「发送受理超时」——报错点离手误很远，排查代价高。

这里用 AST 静态扫 conversation_store 的 4 处 insert_conversation_row，逐一对齐
integrations.clickhouse._CONVERSATION_COLUMNS 的长度，任何漂移立刻红。
"""
from __future__ import annotations

import ast
from pathlib import Path

from integrations.clickhouse import _CONVERSATION_COLUMNS

_ROOT = Path(__file__).resolve().parent.parent


def _insert_row_lengths(path: Path, method: str) -> list[tuple[int, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method:
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.List):
                out.append((node.lineno, len(arg.elts)))
    return out


def test_conversation_insert_rows_match_column_names():
    rows = _insert_row_lengths(_ROOT / "agent" / "conversation_store.py", "insert_conversation_row")
    assert rows, "expected conversation_store to call insert_conversation_row"
    n_cols = len(_CONVERSATION_COLUMNS)
    for lineno, n in rows:
        assert n == n_cols, (
            f"conversation_store.py:{lineno} inserts {n} values but "
            f"_CONVERSATION_COLUMNS has {n_cols} — 加列时两边都要改"
        )


def test_message_insert_rows_match_column_names():
    rows = _insert_row_lengths(_ROOT / "agent" / "conversation_store.py", "insert_message_row")
    assert rows, "expected conversation_store to call insert_message_row"
    # agent_messages 的列名在 clickhouse.py 内联定义，这里取现存的插入行长度互证
    # （3 处必须一致，否则某处漏了列）。
    lengths = {n for _lineno, n in rows}
    assert len(lengths) == 1, f"message insert rows disagree on width: {sorted(lengths)}"
