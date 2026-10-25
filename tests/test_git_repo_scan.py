"""编辑器项目记录扫描：各编辑器自己的记录 → 真实项目路径列表。

扫描命令在节点上通过 host_exec 跑；这里锁定的是「命令 → 路径列表」这段契约，
以及几个关键取舍：

* 只有编辑器自己的记录是权威的——扫通用 ``~/projects`` 会列出没人打开过的目录，
  也分不清是哪个编辑器的。
* 路径取记录里的真实 cwd，不反解转义目录名（``d--code-ai-lubricant`` 有歧义）。
* 没有已知记录格式的编辑器返回空，而不是拿目录名冒充路径。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from user_platform.routes_nodes_files import (  # noqa: E402
    _dedupe_nested,
    _editor_project_command,
    _parse_posix_scan,
    _posix_git_repo_scan_command,
)

BASH = ["bash", "-c"]


def _run_posix(provider: str, home: str) -> list[str]:
    """把某编辑器的命令在假 HOME 下真跑一遍，返回逐行输出。"""
    command = _editor_project_command(provider, is_windows=False, home=home)
    assert command is not None
    env = {**os.environ, "HOME": home}
    result = subprocess.run(BASH + [command], capture_output=True, text=True, env=env)
    return [line for line in result.stdout.splitlines() if line.strip()]


# ── find 输出 → 工作树路径 ──────────────────────────────────────────────────

def test_parse_posix_scan_strips_git_suffix():
    out = (
        "/home/op/proj-a/.git\n"
        "/home/op/code/proj-b/.git\n"
        "\n"  # a stray blank line must not become a bogus entry
        "/home/op/proj-a/sub/.git\n"  # nested under an already-listed tree
    )
    repos = _parse_posix_scan(out)
    assert repos == ["/home/op/proj-a", "/home/op/code/proj-b", "/home/op/proj-a/sub"]


def test_parse_posix_scan_ignores_non_git_lines():
    # A permission-denied line from find (stderr is redirected, but a line that
    # does not end in /.git must never be treated as a repo).
    out = "find: '/root/secret': Permission denied\n/home/op/repo/.git\n"
    assert _parse_posix_scan(out) == ["/home/op/repo"]


def test_parse_posix_scan_empty():
    assert _parse_posix_scan("") == []
    assert _parse_posix_scan(None) == []  # type: ignore[arg-type]


def test_dedupe_nested_keeps_shallowest():
    """嵌套在另一条里的重复项要丢掉，留最浅的那个（那才值得采用）。"""
    assert _dedupe_nested(["/a/repo", "/a/repo/sub"]) == ["/a/repo"]
    assert _dedupe_nested(["/b", "/a", "/a/sub"]) == ["/a", "/b"]


# ── Windows 分支 ────────────────────────────────────────────────────────────
#
# Windows 命令之前只做了静态检查、没真跑，结果漏了一个真 bug：claude 那条用
# ConvertFrom-Json 取 projects 键，而 Windows 路径只差大小写（D:/x 与 d:/x），
# PowerShell 的 JSON 解析大小写不敏感，直接抛 DuplicateKeysInJsonString，
# 命令以退出码 1 失败 → 前端看到空列表。所以这里真跑 PowerShell。

def _run_windows(provider: str, userprofile: str) -> subprocess.CompletedProcess:
    command = _editor_project_command(provider, is_windows=True, home=userprofile)
    assert command is not None
    env = {**os.environ, "USERPROFILE": userprofile}
    return subprocess.run(
        ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


pytestmark_windows = pytest.mark.skipif(
    sys.platform != "win32", reason="需要真 PowerShell"
)


@pytestmark_windows
def test_claude_windows_survives_case_duplicate_keys(tmp_path):
    """大小写只差的两个路径不能让整条命令失败。

    ``ConvertFrom-Json`` 会把它当重复键抛异常——这正是线上"claude 项目全空"
    的根因。改用正则后必须仍然工作，且这类重复会被合并成一条。
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({
        "projects": {
            "D:/code/model-api": {"history": []},
            "d:/code/model-api": {"history": []},
            "D:/code/other": {"history": []},
        },
    }), encoding="utf-8")

    result = _run_windows("claude", str(home))
    assert result.returncode == 0, result.stderr
    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    joined = "\n".join(lines).lower()
    assert "d:/code/model-api" in joined
    assert "d:/code/other" in joined
    # 大小写重复合并成一条（PowerShell 的 Sort-Object -Unique 本就大小写不敏感）。
    assert len([l for l in lines if l.lower().startswith("d:/code/model-api")]) == 1


@pytestmark_windows
def test_claude_windows_missing_config_is_empty(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = _run_windows("claude", str(home))
    assert [l for l in result.stdout.splitlines() if l.strip()] == []


@pytestmark_windows
def test_codex_windows_reads_cwd_and_drops_temp(tmp_path):
    home = tmp_path / "home"
    day = home / ".codex" / "sessions" / "2026" / "05" / "21"
    day.mkdir(parents=True)
    (day / "rollout-1.jsonl").write_text(
        '{"payload":{"cwd":"D:\\\\code\\\\model-api"}}\n', encoding="utf-8"
    )
    (day / "rollout-2.jsonl").write_text(
        '{"payload":{"cwd":"C:\\\\Users\\\\op\\\\AppData\\\\Local\\\\Temp\\\\tmp.x"}}\n',
        encoding="utf-8",
    )

    result = _run_windows("codex", str(home))
    joined = result.stdout
    assert "model-api" in joined
    assert "tmp.x" not in joined


@pytestmark_windows
def test_opencode_windows_reads_worktree(tmp_path):
    """node:sqlite 的警告走 stderr 且带非 0 退出码，但 stdout 的数据必须拿到。"""
    home = tmp_path / "home"
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "opencode.db")
    conn.execute("CREATE TABLE project (id TEXT, worktree TEXT)")
    conn.executemany(
        "INSERT INTO project VALUES (?, ?)", [("a", "D:/code/notice"), ("g", "/")]
    )
    conn.commit()
    conn.close()

    result = _run_windows("opencode", str(home))
    # 退出码可以是 1（实验性警告），stdout 才是信号——服务端也只看 stdout。
    assert "D:/code/notice" in result.stdout


# ── 目录浏览：指定目录时扫该目录 ────────────────────────────────────────────

def test_directory_scan_finds_repos_and_skips_noise(tmp_path):
    """给了具体 root 时扫该目录下的 git 工作树，依赖目录里的 .git 要剪掉。"""
    root = tmp_path / "work"
    for rel in ("alpha", "nested/beta"):
        (root / rel / ".git").mkdir(parents=True)
    (root / "alpha/node_modules/dep/.git").mkdir(parents=True)

    command = _posix_git_repo_scan_command([str(root)], 4)
    stdout = subprocess.run(BASH + [command], capture_output=True, text=True).stdout
    found = {os.path.relpath(p, root).replace(os.sep, "/") for p in _parse_posix_scan(stdout)}

    assert "alpha" in found
    assert "nested/beta" in found
    assert not any("node_modules" in name for name in found)


# ── claude：读 ~/.claude.json 的 projects 键 ────────────────────────────────

def test_claude_reads_projects_keys(tmp_path):
    """claude 的真实路径就在 .claude.json 的 projects 键里，不需要反解 slug。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({
        "projects": {
            "D:/code/ai-lubricant": {"history": []},
            "C:/Users/op/Downloads": {"history": []},
        },
        "other": {"notaproject": "x"},
    }), encoding="utf-8")

    lines = _run_posix("claude", str(home))
    joined = "\n".join(lines)
    assert "D:/code/ai-lubricant" in joined
    assert "C:/Users/op/Downloads" in joined


def test_claude_missing_config_is_empty(tmp_path):
    """没跑过 claude 的节点没有 .claude.json——空列表，不是报错。"""
    home = tmp_path / "home"
    home.mkdir()
    assert _run_posix("claude", str(home)) == []


# ── codex：读 sessions 里的 cwd，滤掉临时目录 ───────────────────────────────

def test_codex_reads_session_cwd_and_drops_temp(tmp_path):
    """codex 的 session 记录带真实 cwd；临时目录是噪音，要滤掉。"""
    home = tmp_path / "home"
    day = home / ".codex" / "sessions" / "2026" / "05" / "21"
    day.mkdir(parents=True)
    (day / "rollout-1.jsonl").write_text(
        '{"type":"session_meta","payload":{"cwd":"D:\\\\code\\\\model-api"}}\n',
        encoding="utf-8",
    )
    (day / "rollout-2.jsonl").write_text(
        '{"type":"session_meta","payload":{"cwd":"C:\\\\Users\\\\op\\\\AppData\\\\Local\\\\Temp\\\\tmp.x"}}\n',
        encoding="utf-8",
    )

    lines = _run_posix("codex", str(home))
    joined = "\n".join(lines)
    assert "model-api" in joined
    assert "tmp.x" not in joined


# ── opencode：读 sqlite 的 project.worktree ─────────────────────────────────

@pytest.mark.skipif(
    subprocess.run(["bash", "-c", "command -v node"], capture_output=True).returncode != 0,
    reason="需要 node 运行 node:sqlite",
)
def test_opencode_reads_worktree_from_sqlite(tmp_path):
    """opencode 的项目在 sqlite 里；节点上通常没有 sqlite3 命令，故走 node:sqlite。"""
    home = tmp_path / "home"
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    db = db_dir / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE project (id TEXT, worktree TEXT)")
    conn.executemany(
        "INSERT INTO project VALUES (?, ?)",
        [("a", "D:/code/notice"), ("global", "/"), ("b", "D:/code/model-api")],
    )
    conn.commit()
    conn.close()

    lines = _run_posix("opencode", str(home))
    assert "D:/code/notice" in lines
    assert "D:/code/model-api" in lines
    # 根目录不是项目。
    assert "/" not in lines


# ── 没有已知记录格式的编辑器：返回 None（前端引导去浏览目录） ──────────────

@pytest.mark.parametrize("provider", ["cursor", "gemini", "dsh", "nonexistent", ""])
def test_editors_without_a_record_have_no_command(provider):
    """cursor/gemini/dsh 目前没有可用的项目记录格式。

    返回 None 是刻意的：宁可空列表 + 引导浏览目录，也不拿转义目录名冒充路径
    （那会存进环境，派发时才发现跑不了）。
    """
    assert _editor_project_command(provider, is_windows=False, home="/home/op") is None
    assert _editor_project_command(provider, is_windows=True, home="C:\\op") is None


def test_known_editors_have_commands_on_both_platforms():
    for provider in ("claude", "codex", "opencode"):
        assert _editor_project_command(provider, is_windows=False, home="/home/op")
        assert _editor_project_command(provider, is_windows=True, home="C:\\op")


# ── 会话扫描：按工作目录筛 + 只扫近 N 天 ────────────────────────────────────
#
# 三条约束，都是"不要全量"来的：只扫已登记项目的工作目录、只扫近 N 天、只推元信息。
# 路径归一在 Python 做（shell 里转义层数对不齐，实测踩过两次），命令只负责输出
# ``<会话id>\t<时间戳>\t<cwd原始值>``。

def test_session_matches_workdir_normalizes_forms():
    """D:\\x、/D:/x、d:/x 都指同一个目录；worktree 子目录也算在环境之下。"""
    from user_platform.routes_nodes_files import _session_matches_workdir as matches

    assert matches("D:\\\\code\\\\proj", "D:/code/proj")
    assert matches("D:\\\\code\\\\proj", "/D:/code/proj")
    assert matches("D:\\\\code\\\\proj\\\\worktrees\\\\task-1", "D:/code/proj")
    assert matches("D:\\\\Code\\\\Proj", "d:/code/proj")
    assert not matches("D:\\\\code\\\\proj-other", "D:/code/proj")
    assert matches("D:\\\\code\\\\x", "")


def test_session_matches_workdir_posix_is_case_sensitive():
    """/home/Op 和 /home/op 是两个目录——大小写不敏感只适用于带盘符的 Windows 路径。"""
    from user_platform.routes_nodes_files import _session_matches_workdir as matches

    assert matches("/home/op/repo", "/home/op/repo")
    assert not matches("/home/Op/repo", "/home/op/repo")


def test_parse_editor_sessions_filters_by_workdir():
    """解析时按工作目录筛；脏行容忍；同 id 留较新的时间。"""
    from user_platform.routes_nodes_files import _parse_editor_sessions as parse

    stdout = (
        "aaa\t1789000000\tD:\\\\code\\\\proj\n"
        "bbb\t1789000001\tD:\\\\code\\\\other\n"
        "garbage line without tabs\n"
        "ccc\tnot-a-number\tD:\\\\code\\\\proj\n"
        "aaa\t1789000002\tD:\\\\code\\\\proj\n"
    )
    rows = parse(stdout, "D:/code/proj")
    assert [(r["session_id"], r["updated_at"]) for r in rows] == [
        ("aaa", 1789000002),
    ]


def test_session_command_emits_cwd_column(tmp_path):
    """命令输出第三列是 cwd 原始值——过滤在 Python 做，不在 shell 里。"""
    from user_platform.routes_nodes_files import _posix_session_command

    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "D--code--proj"
    proj.mkdir(parents=True)
    (proj / "sess-1.jsonl").write_text(
        '{"type":"x"}\n{"cwd":"D:\\\\code\\\\proj"}\n', encoding="utf-8"
    )

    command = _posix_session_command(
        "claude", home=str(home), workdir="", since=1700000000
    )
    assert command is not None
    stdout = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True
    ).stdout
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    parts = lines[0].split("\t")
    assert parts[0] == "sess-1"
    assert parts[1].strip().isdigit()
    assert "code" in parts[2] and "proj" in parts[2]


# ── 会话详情的转录解析：三家编辑器形态完全不同 ──────────────────────────────
#
# codex 的对话不在顶层 type，而是 event_msg 里 payload.type=user_message/agent_message；
# opencode 走 node:sqlite 出 TSV，不是 NDJSON；claude 才是顶层 type=user|assistant。
# 用一套 claude 形状的 parser 去解另外两家 = 详情页永远「没有历史」。


def test_parse_transcript_codex_reads_event_msg_not_top_level():
    import json

    from user_platform.routes_nodes_files import _parse_transcript_lines as parse

    lines = [
        json.dumps({"timestamp": "T1", "type": "event_msg",
                    "payload": {"type": "task_started"}}),                       # 非对话，跳
        json.dumps({"timestamp": "T2", "type": "event_msg",
                    "payload": {"type": "user_message", "message": "帮我查个 bug"}}),
        json.dumps({"timestamp": "T3", "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "找到了，在这里"}}),
        json.dumps({"timestamp": "T4", "type": "event_msg",
                    "payload": {"type": "agent_message",
                                "message": "<permissions instructions>xxx"}}),   # 包装，跳
        json.dumps({"timestamp": "T5", "type": "response_item",
                    "payload": {"type": "function_call", "call_id": "call_1",
                                "name": "shell", "arguments": '{"cmd":"ls"}'}}),
        json.dumps({"timestamp": "T6", "type": "response_item",
                    "payload": {"type": "function_call_output", "call_id": "call_1",
                                "output": "a.txt"}}),
    ]
    items, trunc = parse("\n".join(lines), 60, "codex")
    # 对话在 event_msg 里（顶层 type 不是 role），工具是 response_item 的
    # call/function_call_output——靠 call_id 配成同一张卡。词表对齐运行时：
    # status=running/done，工具 id 用裸 call_id，event_kind/phase/tool_name 齐全。
    assert [i["type"] for i in items] == ["user_input", "agent_message", "tool_call", "tool_call"]
    assert items[0] == {
        "id": "0", "type": "user_input", "text": "帮我查个 bug", "created_at": None,
    }
    assert items[2]["id"] == items[3]["id"] == "call_1"  # 裸 id，无前缀
    assert items[2] == {
        "id": "call_1", "type": "tool_call", "title": "shell", "tool_name": "shell",
        "logical_event_id": "call_1", "event_kind": "tool", "phase": "start",
        "status": "running", "input": {"cmd": "ls"}, "created_at": None,
    }
    # 闭帧稀疏，但名字从开帧 tool_names 表恢复、canonical 字段齐全
    assert items[3] == {
        "id": "call_1", "type": "tool_call", "title": "shell", "tool_name": "shell",
        "logical_event_id": "call_1", "event_kind": "tool", "phase": "complete",
        "output": "a.txt", "status": "done", "created_at": None,
    }
    assert trunc is False


def test_parse_transcript_codex_new_format_response_item_only():
    """2026-09 起的新版 codex 不再写 event_msg user/agent_message，对话只在
    response_item message 里；注入内容（AGENTS.md / environment_context /
    turn_aborted、developer role）必须滤掉。"""
    import json

    from user_platform.routes_nodes_files import _parse_transcript_lines as parse

    def msg(role: str, text: str) -> str:
        return json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text",
                         "text": text}]}})

    lines = [
        msg("developer", "<permissions instructions>xxx"),
        msg("user", "# AGENTS.md instructions\n\n<INSTRUCTIONS>注入的指令</INSTRUCTIONS>"),
        msg("user", "<environment_context>\n  <cwd>D:\\code\\x</cwd>\n</environment_context>"),
        msg("user", "1.1111"),
        msg("assistant", "2.2222"),
        json.dumps({"timestamp": "T1", "type": "response_item", "payload": {
            "type": "reasoning", "summary": [],
            "content": [{"type": "reasoning_text", "text": "先想一下"}]}}),
        json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "call_id": "call_p1", "name": "apply_patch",
            "status": "completed", "input": "*** Begin Patch\n..."}}),
        json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "call_p1", "output": "done"}}),
    ]
    items, _ = parse("\n".join(lines), 60, "codex")
    assert [i["type"] for i in items] == [
        "user_input", "agent_message", "reasoning", "tool_call", "tool_call",
    ]
    assert items[0]["text"] == "1.1111"
    assert items[3]["id"] == items[4]["id"] == "call_p1"
    # custom_tool_call 自带终态 → 开帧即 complete/done
    assert items[3] == {
        "id": "call_p1", "type": "tool_call", "title": "apply_patch",
        "tool_name": "apply_patch", "logical_event_id": "call_p1",
        "event_kind": "tool", "phase": "complete", "status": "done",
        "input": {"input": "*** Begin Patch\n..."}, "created_at": None,
    }
    assert items[4]["output"] == "done" and items[4]["status"] == "done"
    assert items[4]["title"] == "apply_patch"   # 闭帧名字从开帧恢复


def test_parse_transcript_codex_dual_format_pairs_dedup():
    """老格式同一句话在 response_item 与 event_msg 各落一次（user: RI→EV，
    assistant: EV→RI），成对相邻；两条相同但隔开的提问则都要保留。"""
    import json

    from user_platform.routes_nodes_files import _parse_transcript_lines as parse

    def ri(role: str, text: str) -> str:
        return json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text",
                         "text": text}]}})

    lines = [
        ri("user", "介绍下自己"),
        json.dumps({"type": "event_msg", "payload": {"type": "user_message",
                                                     "message": "介绍下自己"}}),
        json.dumps({"type": "event_msg", "payload": {"type": "agent_message",
                                                     "message": "好的"}}),
        ri("assistant", "好的"),
        json.dumps({"type": "event_msg", "payload": {"type": "user_message",
                                                     "message": "<turn_aborted>\n中断了</turn_aborted>"}}),
        ri("user", "继续"),
        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "继续"}}),
        ri("user", "继续"),      # 用户又发了一次「继续」——上一条不是它的重复对，保留
        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "继续"}}),
    ]
    items, _ = parse("\n".join(lines), 60, "codex")
    assert [(i["type"], i["text"]) for i in items] == [
        ("user_input", "介绍下自己"), ("agent_message", "好的"),
        ("user_input", "继续"), ("user_input", "继续"),
    ]


def test_parse_transcript_opencode_base64_rows():
    """opencode 走 node:sqlite 出 `role<TAB>type<TAB>base64(part json)<TAB>ts`：
    part 的 JSON 里有换行，不 base64 会把行协议拆碎。"""
    import base64
    import json

    from user_platform.routes_nodes_files import _parse_transcript_lines as parse

    def row(role: str, ptype: str, part: dict, ts: int | None = None) -> str:
        b64 = base64.b64encode(json.dumps(part, ensure_ascii=False).encode("utf-8")).decode()
        cells = [role, ptype, b64] + ([str(ts)] if ts else [])
        return "\t".join(cells)

    lines = [
        row("user", "text", {"text": "第一个问题"}, 1789000000),
        row("assistant", "text", {"text": "第一段回答"}),        # 无 ts 列也要能解
        row("bogus", "text", {"text": "不该出现"}),              # 非 user/assistant 丢掉
        row("assistant", "text", {"text": "  "}),                # 空正文丢掉
        row("assistant", "text", {"text": "<system-reminder>提示</system-reminder>"}),  # 包装
    ]
    items, _ = parse("\n".join(lines), 60, "opencode")
    assert [(i["type"], i.get("text")) for i in items] == [
        ("user_input", "第一个问题"), ("agent_message", "第一段回答"),
    ]
    assert items[0]["created_at"] == 1789000000
    assert items[1]["created_at"] is None


def test_parse_transcript_opencode_tool_row_builds_card():
    import base64
    import json

    from user_platform.routes_nodes_files import _parse_transcript_lines as parse

    part = {"tool": "bash", "callID": "c1",
            "state": {"status": "completed", "input": {"cmd": "ls"}, "output": "a.txt"}}
    line = "assistant\ttool\t%s\t1789000060" % base64.b64encode(
        json.dumps(part).encode("utf-8")).decode()
    items, _ = parse(line, 60, "opencode")
    # opencode 一个 tool part 就是终态 → 单条完整条目，status=running/done/failed。
    assert items == [{
        "id": "c1", "type": "tool_call", "title": "bash", "tool_name": "bash",
        "logical_event_id": "c1", "event_kind": "tool", "phase": "complete",
        "status": "done", "input": {"cmd": "ls"}, "output": "a.txt",
        "created_at": 1789000060,
    }]


def test_parse_transcript_claude_blocks_and_wrappers():
    import json

    from user_platform.routes_nodes_files import _parse_transcript_lines as parse

    lines = [
        json.dumps({"type": "user", "timestamp": "2026-09-30T10:00:00Z",
                    "message": {"content": "真实提问"}}),
        json.dumps({"type": "user", "message": {"content": "<system-reminder>环境提示</system-reminder>"}}),
        json.dumps({"type": "assistant", "isSidechain": True,
                    "message": {"content": [{"type": "text", "text": "子代理话"}]}}),
        json.dumps({"type": "assistant", "timestamp": "2026-09-30T10:00:05Z",
                    "message": {"content": [
                        {"type": "thinking", "thinking": "思考块"},
                        {"type": "text", "text": "正文"},
                        {"type": "tool_use", "id": "tu_1", "name": "Read",
                         "input": {"file_path": "a.py"}},
                    ]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "文件内容"}]}}),
        json.dumps({"type": "last-prompt", "lastPrompt": "不是对话"}),
    ]
    items, _ = parse("\n".join(lines), 60, "claude")
    # 产出任务规范 NormalizedItem：消息块 id 用 uuid:index（运行时同源），
    # status 是 SDK 词汇 running/done/failed，工具结果与调用共用裸 tool_use_id。
    assert [i["type"] for i in items] == [
        "user_input", "reasoning", "agent_message", "tool_call", "tool_call",
    ]
    assert items[0] == {"id": "0", "type": "user_input", "text": "真实提问",
                        "created_at": 1790762400}   # ISO → epoch 秒
    assert items[2]["text"] == "正文"
    assert items[3]["id"] == items[4]["id"] == "tu_1"   # 裸 id
    # 开帧：canonical 字段 + summary（Read 的词表是 file_path）
    assert items[3] == {
        "id": "tu_1", "type": "tool_call", "title": "Read", "tool_name": "Read",
        "logical_event_id": "tu_1", "event_kind": "tool", "phase": "start",
        "status": "running", "input": {"file_path": "a.py"},
        "summary": "a.py", "created_at": 1790762405,
    }
    # 闭帧：稀疏但带名字与 canonical 字段；错误结果走 phase=error/status=failed
    assert items[4] == {
        "id": "tu_1", "type": "tool_call", "title": "Read", "tool_name": "Read",
        "logical_event_id": "tu_1", "event_kind": "tool", "phase": "complete",
        "output": "文件内容", "status": "done", "created_at": None,
    }


def test_session_transcript_command_opencode_sql_is_base64():
    """SQL 必须 base64：PS 双引号串里嵌 'role' 的引号会撞 SELECT 的引号，
    脚本提前终止（实测 char:309 报 Missing 错）。"""
    import base64

    from user_platform.routes_nodes_files import _session_transcript_command

    cmd = _session_transcript_command(
        "opencode", is_windows=True, home=r"C:\Users\op", session_id="ses_x", tail_lines=40
    )
    script = base64.b64decode(cmd.split(" ")[-1]).decode("utf-16le")
    assert "Buffer.from(process.argv[2],'base64')" in script
    assert "json_extract" not in script          # 明文 SQL 绝不能出现在命令行里
    assert ".local\share\opencode\opencode.db" in script


def test_item_fingerprint_ignores_volatile_parser_ids():
    """指纹是导入去重的键——**不能**含解析器给的 id。

    codex 的消息条目 id 是自增 counter（``t0``/``t1``…），窗口从 400 行调到 800 行
    时同一条消息会拿到不同的 counter。指纹若含 id，历史就会整段重复入库。
    """
    from user_platform.routes_nodes_files import _item_fingerprint

    base = {"type": "agent_message", "text": "回答", "created_at": 1790762400,
            "event_kind": "message", "phase": "complete"}
    # 同一内容、不同解析器 id（counter 漂了）→ 指纹必须一致。
    assert _item_fingerprint({**base, "id": "t0"}) == _item_fingerprint({**base, "id": "t7"})
    # 内容变了 → 指纹变。
    assert _item_fingerprint({**base, "id": "t0"}) != _item_fingerprint({**base, "text": "别的话"})


def test_item_fingerprint_separates_tool_frames():
    """工具开帧与闭帧是**两条**独立条目（各存一行，前端合并成一张卡）。"""
    from user_platform.routes_nodes_files import _item_fingerprint

    open_frame = {"type": "tool_call", "logical_event_id": "toolu_1", "phase": "start",
                  "status": "running", "tool_name": "Bash", "input": {"command": "ls"}}
    close_frame = {"type": "tool_call", "logical_event_id": "toolu_1", "phase": "complete",
                   "status": "done", "tool_name": "Bash", "output": "a.txt"}
    assert _item_fingerprint(open_frame) != _item_fingerprint(close_frame)


def test_stable_item_id_is_native_for_tools_and_fingerprint_for_messages():
    """返回给前端的 id 必须稳定：工具用裸 call_id（开闭帧同 id → 合并成一张卡），
    消息用内容指纹（codex 没有原生 id，counter 又会漂）。"""
    from user_platform.routes_nodes_files import _item_fingerprint, _stable_item_id

    tool = {"type": "tool_call", "logical_event_id": "toolu_9", "phase": "complete"}
    fp = _item_fingerprint(tool)
    assert _stable_item_id(tool, fp) == "toolu_9"

    # 工具的开帧与闭帧即使指纹不同，返回 id 也必须相同。
    tool_open = {"type": "tool_call", "logical_event_id": "toolu_9", "phase": "start"}
    assert _stable_item_id(tool_open, _item_fingerprint(tool_open)) == "toolu_9"

    msg = {"type": "agent_message", "text": "你好", "created_at": 1}
    mid = _stable_item_id(msg, _item_fingerprint(msg))
    assert mid.startswith("env:")
    # 同一内容两次调用 → 同一 id（跨窗口不变，前端不会重复渲染）。
    assert mid == _stable_item_id(msg, _item_fingerprint(msg))
