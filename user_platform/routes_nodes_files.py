"""Node host file browser backed by the structured host exec channel.

Two surfaces over one implementation:

* **Admin** ``/api/v1/admin/nodes/{node_id}/files*`` — platform administrators,
  any node in any state (an offline node can still be inspected for triage).
* **User** ``/api/v1/teams/my-nodes/{node_id}/files*`` — the signed-in user may
  only touch a node one of their groups was granted, and only while that node is
  an approved, online, real client. This mirrors the user terminal WebSocket
  gate in :mod:`.routes_nodes_terminal` — the file browser lives inside that
  terminal, so it must not be reachable where the terminal itself is refused.

The path parsing, command construction and response decoding are shared; only
the authorization differs. Writes on the user surface are audited.
"""
from __future__ import annotations

import base64
import binascii
import json
import ntpath
import posixpath
import re
import shlex
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from .deps import client_meta, get_current_user
from .models import User
from .node_client import get_local_node_client

router = APIRouter(prefix="/api/v1/admin/nodes", tags=["user-platform-nodes-admin"])
team_router = APIRouter(prefix="/api/v1/teams/my-nodes", tags=["user-platform-nodes-team"])

# File operations that change the node host filesystem — audited on the user
# surface (reads are not: listing a directory is not a state change).
_MUTATING_OPS = {"mkdir", "write", "rename", "delete"}

_WINDOWS_PATH_RE = re.compile(r"^/([A-Za-z]):(?:/(.*))?$")
_MAX_READ_BYTES = 1024 * 1024
_MAX_WINDOWS_WRITE_BYTES = 1024
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Git-repo scan (environment adoption): bounded by depth, result count and a
# prune list — a scan is a user click, not a filesystem walk. The prune list is
# dependency/build output only; a real checkout never lives inside one.
_GIT_REPO_SCAN_MAX_DEPTH = 5
_GIT_REPO_SCAN_DEFAULT_DEPTH = 3
_GIT_REPO_SCAN_MAX_REPOS = 50
_SCAN_PRUNE_DIRS = (
    "node_modules", ".venv", "venv", ".tox", "__pycache__", "site-packages",
    ".cache", "target", "dist", "build",
)

# Editors we know how to enumerate projects for. Each one keeps its own record
# of the directories it has been run in, and only that record is authoritative:
# scanning a generic ``~/projects`` would list directories no editor ever
# opened, and would show a Claude project while the user picked Codex.
_SCAN_PROVIDERS = ("claude", "codex", "opencode", "cursor", "gemini", "dsh")


class HostFileRequest(BaseModel):
    path: str = "/"
    operation: str = "list"
    content: str = Field(default="", max_length=2_000_000)
    destination: str = ""


def _path(value: str) -> str:
    raw = (value or "/").replace("\\", "/")
    if "\x00" in raw or any(part == ".." for part in raw.split("/")):
        raise HTTPException(status_code=400, detail="非法路径")
    clean = posixpath.normpath("/" + raw.lstrip("/"))
    if clean == "." or clean.startswith("/../") or clean == "/..":
        raise HTTPException(status_code=400, detail="非法路径")
    return clean


def _windows_path(value: str) -> tuple[str, str | None]:
    raw = value or "/"
    if "\x00" in raw or "\\" in raw or any(part == ".." for part in raw.split("/")):
        raise HTTPException(status_code=400, detail="非法 Windows 路径")
    if raw == "/":
        return "/", None
    match = _WINDOWS_PATH_RE.fullmatch(raw)
    if match is None:
        raise HTTPException(status_code=400, detail="Windows 路径必须为 /C:/... 格式")
    drive = match.group(1).upper()
    raw_tail_parts = (match.group(2) or "").split("/")
    tail_parts = [part for part in raw_tail_parts if part not in ("", ".")]
    if any(
        ":" in part or part.rstrip(". ") in {"", ".", ".."} or part != part.rstrip(". ")
        for part in tail_parts
    ):
        raise HTTPException(status_code=400, detail="非法 Windows 路径")
    virtual = f"/{drive}:" + (f"/{'/'.join(tail_parts)}" if tail_parts else "")
    native = f"{drive}:\\" + "\\".join(tail_parts)
    return virtual, native


def _native_to_windows_path(value: str) -> str:
    drive, tail = ntpath.splitdrive(value)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("\\", "/")):
        raise ValueError("invalid Windows absolute path")
    parts = [part for part in tail.replace("\\", "/").split("/") if part]
    return f"/{drive[0].upper()}:" + (f"/{'/'.join(parts)}" if parts else "")


def _ps_data(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _powershell_command(body: str) -> str:
    prelude = (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        "$utf8=[Text.UTF8Encoding]::new($false);"
        "$OutputEncoding=$utf8;[Console]::OutputEncoding=$utf8;"
    )
    encoded = base64.b64encode((prelude + body).encode("utf-16le")).decode("ascii")
    return f"powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"


def _ps_path_assignment(native_path: str, name: str = "p") -> str:
    return (
        f"${name}=[Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{_ps_data(native_path)}'));"
    )


def _windows_command(op: str, native_path: str | None, content: str, destination: str | None) -> str:
    if op == "list" and native_path is None:
        return _powershell_command(
            "$entries=@(Get-PSDrive -PSProvider FileSystem | ForEach-Object {"
            "[pscustomobject]@{name=($_.Name+':');path=($_.Name+':\\');is_dir=$true;size=0}"
            "});[pscustomobject]@{entries=$entries}|ConvertTo-Json -Depth 4 -Compress"
        )
    if native_path is None:
        raise HTTPException(status_code=400, detail="不能修改文件系统根目录")

    assign = _ps_path_assignment(native_path)
    if op == "list":
        body = assign + (
            "if (-not (Test-Path -LiteralPath $p -PathType Container)){throw 'directory not found'};"
            "$entries=@(Get-ChildItem -LiteralPath $p -Force | ForEach-Object {"
            "[pscustomobject]@{name=$_.Name;path=$_.FullName;is_dir=$_.PSIsContainer;"
            "size=$(if ($_.PSIsContainer){0}else{$_.Length})}"
            "});[pscustomobject]@{entries=$entries}|ConvertTo-Json -Depth 4 -Compress"
        )
    elif op == "read":
        body = assign + (
            "if (-not (Test-Path -LiteralPath $p -PathType Leaf)){throw 'file not found'};"
            "$s=[IO.File]::OpenRead($p);try{$b=New-Object byte[] 1048576;"
            "$n=$s.Read($b,0,$b.Length);$data=[Convert]::ToBase64String($b,0,$n);"
            "$truncated=$s.Length -gt 1048576}finally{$s.Dispose()};"
            "[pscustomobject]@{data=$data;truncated=$truncated}|ConvertTo-Json -Compress"
        )
    elif op == "mkdir":
        body = assign + "[IO.Directory]::CreateDirectory($p)|Out-Null"
    elif op == "write":
        body = assign + f"$data=[Convert]::FromBase64String('{_ps_data(content)}');[IO.File]::WriteAllBytes($p,$data)"
    elif op == "rename":
        if destination is None:
            raise HTTPException(status_code=400, detail="缺少目标路径")
        body = assign + _ps_path_assignment(destination, "d") + "Move-Item -LiteralPath $p -Destination $d"
    else:
        body = assign + "if (-not (Test-Path -LiteralPath $p)){throw 'path not found'};Remove-Item -LiteralPath $p -Recurse -Force"
    return _powershell_command(body)


def _host_exec_error(result: dict[str, Any]) -> str | None:
    error = str(result.get("error") or "").strip()
    if error:
        return error
    exit_code = result.get("exitCode", result.get("exit_code", 0))
    try:
        failed_exit = int(exit_code) != 0
    except (TypeError, ValueError):
        return "节点返回了无效退出码"
    if failed_exit or ("success" in result and result.get("success") is False):
        return str(result.get("stderr") or "文件操作失败")
    return None


def _json_output(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("stdoutTruncated") or result.get("stdout_truncated"):
        raise HTTPException(status_code=502, detail="节点文件操作响应被截断")
    try:
        payload = json.loads(result.get("stdout") or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="节点返回了无效文件数据") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="节点返回了无效文件数据")
    return payload


def _windows_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _json_output(result)
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise HTTPException(status_code=502, detail="节点返回了无效目录数据")
    entries = []
    for item in raw_entries:
        if not isinstance(item, dict):
            raise HTTPException(status_code=502, detail="节点返回了无效目录项")
        try:
            entry_path = _native_to_windows_path(str(item.get("path") or ""))
            size = int(item.get("size") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="节点返回了无效目录项") from exc
        entries.append({
            "name": str(item.get("name") or entry_path.rsplit("/", 1)[-1]),
            "path": entry_path,
            "is_dir": bool(item.get("is_dir")),
            "size": size,
        })
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    return entries


def _require_admin(user: User = Depends(get_current_user)) -> User:
    if user is None or user.role != "admin" or user.is_deleted or user.is_blocked or user.status != "active":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def _require_node_access(node_id: str, user: User = Depends(get_current_user)) -> User:
    """User-surface gate: a granted node, in a state that supports host access.

    Same two checks the user terminal WebSocket makes (see
    ``routes_nodes_terminal.user_node_terminal``): the caller must hold a
    GroupNode grant for this node, and the node must be an approved, online,
    real client. A grouping container (``is_passive``) has no host to browse.

    Kept intentionally strict — unlike the admin surface, a user may not reach
    into an offline or unapproved node.
    """
    from .nodes_service import nodes_service

    if await nodes_service.user_can_use_node(str(user.id), node_id) is None:
        raise HTTPException(status_code=403, detail="无权访问该节点")
    live = (await nodes_service._live_node_map()).get(node_id)
    if live is None:
        raise HTTPException(status_code=404, detail="节点不存在")
    online = live.get("online") if "online" in live else live.get("connected")
    if live.get("is_passive") or live.get("status") != "approved" or not online:
        raise HTTPException(status_code=409, detail="节点未审批、离线或不支持文件浏览")
    return user


async def _audit_user_file_op(
    request: Request,
    user: User,
    node_id: str,
    operation: str,
    payload: dict[str, Any],
) -> None:
    """Record a user-surface host filesystem write (best-effort).

    Mirrors the terminal audit: host-grade actions are always recorded, and a
    failure here never fails an operation that already happened.
    """
    try:
        from .deps import resolve_team_id
        from .team_users_service import team_users_service

        ip, ua = client_meta(request)
        await team_users_service.record_audit(
            await resolve_team_id(str(user.id)),
            str(user.id),
            f"node.file_{operation}",
            request={"node_id": node_id, **payload},
            response={"ok": True},
            source_ip=ip,
            user_agent=ua,
        )
    except Exception:  # noqa: BLE001 - audit must never break the file action
        return


async def _node_is_windows(node_id: str) -> tuple[Any, bool]:
    """Resolve the live node client + whether the target node runs Windows."""
    client = get_local_node_client()
    try:
        nodes = await client.list_nodes()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    node = next((item for item in nodes if item.get("node_id") == node_id), None)
    if node is None:
        raise HTTPException(status_code=404, detail="节点不存在")
    return client, str((node.get("capabilities") or {}).get("os") or "").lower() == "windows"


async def _do_host_file(node_id: str, req: HostFileRequest) -> dict[str, Any]:
    """Run one host file operation. Authorization is the caller's job."""
    op = (req.operation or "list").strip().lower()
    if op not in {"home", "list", "read", "mkdir", "write", "rename", "delete"}:
        raise HTTPException(status_code=400, detail="不支持的文件操作")

    client, is_windows = await _node_is_windows(node_id)

    if is_windows:
        if op == "home":
            path, native_path = "/", None
            command = _powershell_command("[Console]::Out.Write((Get-Location).Path)")
            cwd = ""
        else:
            path, native_path = _windows_path(req.path)
            destination = None
            if op == "rename":
                _, destination = _windows_path(req.destination)
                if destination is None:
                    raise HTTPException(status_code=400, detail="不能重命名到文件系统根目录")
            if op in {"write", "mkdir", "rename", "delete"} and native_path is None:
                raise HTTPException(status_code=400, detail="不能修改文件系统根目录")
            if op in {"rename", "delete"} and path.count("/") == 1:
                raise HTTPException(status_code=400, detail="不能修改驱动器根目录")
            if op == "write" and len(req.content.encode("utf-8")) > _MAX_WINDOWS_WRITE_BYTES:
                raise HTTPException(status_code=400, detail="Windows 节点单次写入最多支持 1 KiB")
            command = _windows_command(op, native_path, req.content, destination)
            cwd = ""
    else:
        if op == "home":
            path = "/"
            command = "printf '%s' \"$HOME\""
        else:
            path = _path(req.path)
            qpath = shlex.quote(path)
            if op == "list":
                # 纯 POSIX 实现：GNU find 的 -printf 是 Linux 专属，macOS/BSD find
                # 直接报 "unknown primary or operator"。cd 进目录用内置 [ ] 判类型、
                # wc -c 取字节数（FIFO 等特殊文件不打开内容，避免 wc 挂死等写入端），
                # 输出仍是「类型\t字节数\t绝对路径」，解析协议不变。
                command = (
                    f"if [ -d {qpath} ]; then cd {qpath} || exit 2; "
                    f'for name in .* *; do case "$name" in .|..) continue;; esac; '
                    f'[ -e "$name" ] || [ -L "$name" ] || continue; '
                    f'if [ -d "$name" ]; then printf \'d\\t0\\t%s\\n\' "${{PWD%/}}/$name"; '
                    f'elif [ -f "$name" ]; then printf \'f\\t%s\\t%s\\n\' "$(wc -c < "$name")" "${{PWD%/}}/$name"; '
                    f'else printf \'f\\t0\\t%s\\n\' "${{PWD%/}}/$name"; fi; '
                    f"done; else exit 2; fi"
                )
            elif op == "read":
                command = f"if [ -f {qpath} ]; then head -c {_MAX_READ_BYTES} {qpath}; else exit 2; fi"
            elif op == "mkdir":
                command = f"mkdir -p {qpath}"
            elif op == "write":
                # 不走 base64：macOS base64 解码是 -D（-d 是 GNU 专有）。内容经
                # shlex.quote 进单引号不做任何展开；NUL 穿不过 argv，明确拒绝。
                if "\x00" in req.content:
                    raise HTTPException(status_code=400, detail="仅支持写入 UTF-8 文本内容")
                command = f"printf %s {shlex.quote(req.content)} > {qpath}"
            elif op == "rename":
                destination_path = _path(req.destination)
                command = f"mv {qpath} {shlex.quote(destination_path)}"
            else:
                command = f"rm -rf -- {qpath}"
        cwd = "/"

    try:
        result = await client.host_exec(node_id, command, cwd=cwd, max_output_bytes=2_000_000)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if error := _host_exec_error(result):
        raise HTTPException(status_code=400, detail=error)

    if op == "home":
        try:
            if is_windows:
                raw_home = str(result.get("stdout") or "").strip()
                home_path = _native_to_windows_path(raw_home)
            else:
                raw_home = str(result.get("stdout") or "").strip()
                if not raw_home.startswith("/"):
                    raise ValueError("home path is not absolute")
                home_path = _path(raw_home)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="节点返回了无效用户目录") from exc
        return {"path": home_path}

    if op == "read":
        if is_windows:
            payload = _json_output(result)
            try:
                raw = base64.b64decode(str(payload.get("data") or ""), validate=True)
                content = raw.decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise HTTPException(status_code=400, detail="仅支持预览 UTF-8 文本文件") from exc
            return {"path": path, "content": content, "truncated": bool(payload.get("truncated"))}
        return {"path": path, "content": result.get("stdout", ""), "truncated": result.get("stdoutTruncated", False)}
    if op == "list":
        if is_windows:
            entries = _windows_entries(result)
        else:
            entries = []
            for line in (result.get("stdout") or "").splitlines():
                kind, size, entry_path = (line.split("\t", 2) + ["", "", ""])[:3]
                if entry_path:
                    entries.append({"name": entry_path.rsplit("/", 1)[-1], "path": entry_path, "is_dir": kind == "d", "size": int(size or 0)})
            entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return {"path": path, "entries": entries}
    return {"ok": True, "path": path, "operation": op}


@router.post("/{node_id}/files")
async def node_host_file(
    node_id: str, req: HostFileRequest, user: User = Depends(_require_admin)
) -> dict[str, Any]:
    """管理端：在节点主机上执行一次文件操作（list/read/mkdir/write/rename/delete/home）。

    权限由依赖把关，任意节点状态均可用（离线节点仅供排查，实际操作会 503）。
    """
    del user  # 管理端不需要调用者身份，鉴权已由依赖完成
    return await _do_host_file(node_id, req)


def _scan_prune_expression() -> str:
    """The ``-name a -o -name b`` chain shared by both scan commands."""
    return " -o ".join(f"-name {shlex.quote(name)}" for name in _SCAN_PRUNE_DIRS)


# ── 编辑器自己的项目记录 ─────────────────────────────────────────────────────
#
# 「节点上已有哪些项目」这件事，只有编辑器自己知道：每个编辑器都留了一份它
# 在哪些目录里跑过的记录。所以扫描读的是那份记录，不是去猜 ~/projects 之类的
# 通用目录（那会列出没人打开过的目录），也不是反解 slug（那有歧义）。
#
# 各编辑器的记录位置（本仓库实测）：
#
#   claude   ~/.claude.json 的 ``projects`` 键 —— 真实路径，正斜杠形式
#   codex    ~/.codex/sessions/**/rollout-*.jsonl 首行 payload.cwd
#   opencode ~/.local/share/opencode/opencode.db 的 project.worktree（sqlite）
#   cursor   ~/.cursor/projects/<slug>/          —— 只有转义目录名，本机无实例
#   gemini   无项目历史（只有 settings.json）
#   dsh      $DSH_HOME（默认 ~/.dsh），暂无已知格式
#
# 拿不到真实路径的（cursor/gemini/dsh）返回空列表，由前端引导去「浏览目录」，
# 而不是拿一个转义目录名冒充路径——那会存进环境、派发时才发现跑不了。

# 每个编辑器的「列会话」命令：输出逐行 ``<会话id>\t<最后修改时间(epoch秒)>``。
#
# 三条约束，都是"不要全量"来的：
#
# 1. **按工作目录筛**。会话记录挂在编辑器的 HOME 下，但一个 HOME 里可能有该编辑器
#    跑过的**所有**项目。不隔离环境的 HOME 就是节点操作者的真实 HOME——不筛就会
#    把他别的项目的历史当成"这个环境的会话"。用前缀匹配（而不是相等）：worktree
#    模式下会话跑在 ``envs/<id>/worktrees/<task>``，也在环境之下。
# 2. **只要近 N 天**。更早的会话不扫——历史越久越没用，而扫描成本随文件数线性涨。
# 3. **节点只读**。会话历史是编辑器自己的事（它写就写、不写就不写），节点不写任何
#    会话状态，只去读编辑器留下的记录——跟「列项目」同一套来源。
#
# 时间戳两种形态都试：GNU 用 ``stat -c %Y``、BSD/macOS 用 ``stat -f %m``（本仓库的
# BSD 兼容纪律）。

def _posix_cwd_probe(file_var: str) -> str:
    """把 ``$<file_var>`` 头部里的会话 cwd 取出来，放进 ``$cwd``（**原样**，不加工）。

    刻意不在 shell 里做路径归一或比较：反斜杠要穿过「Python 字符串 → shell → 参数
    展开/sed」三层转义，实测每一层都可能被吃掉（先是 ``unterminated 's' command``，
    后是替换静默失效）。所以这里只做纯粹的取字符串，路径语义全部交给 Python——
    那边能写测试，也就不用跟转义层数搏斗。
    """
    return (
        f"""raw=$(head -c 4000 "${file_var}" 2>/dev/null """
        f"""| grep -oE '"cwd":"[^"]*"' | head -1); """
        f"""raw=${{raw#*'"cwd":"'}}; cwd=${{raw%%'"'*}}; """
    )


def _posix_title_probe(file_var: str) -> str:
    """把 ``$<file_var>`` 里的会话标题取出来放进 ``$title``（原样，清洗在 Python 做）。

    标题**不是**编辑器的现成字段——三家都得从记录里派生（本机实盘核对过）：

    * claude：jsonl 里没有 title/summary（``"summary"`` 的命中全是工具 schema 的
      属性名）。最近一次用户提问在 ``last-prompt`` 记录里，而它写在**文件尾部**
      （单文件可达数十条，取最后一条才是"当前在聊什么"），所以读 ``tail -c``。
      没有 lastPrompt（会话还没走到那一步）再回退首条用户消息（``head -c``，跳过
      ``<system-reminder>`` 那条——它是环境提示不是人说的话）。
    * codex：rollout 首行 ``session_meta`` 只有 base_instructions，没标题；标题在
      第一条 ``user_message`` 的 ``message`` 里，用 ``head -c`` 取（``grep -m1``
      命中即停，成本上限就是"到首条用户消息的距离"，实测约 45KB）。

    与 cwd 探针同理：不在 shell 里做转义/裁剪，只负责把原始值捞进变量；换行、制表、
    超长、占位标题都在 :func:`environment_service._clean_session_title` 统一收。
    读不到就是空串（``head/tail`` 对不存在的文件静默失败），下游落兜底名
    （``会话 <id前8>``），绝不渲染裸 uuid。
    """
    return (
        f"""title=$(tail -c 60000 "${file_var}" 2>/dev/null """
        f"""| grep -oE '"lastPrompt":"[^"]{{1,300}}' | tail -1); """
        f"""title=${{title#*'"lastPrompt":"'}}; """
        f"""if [ -z "$title" ]; then title=$(head -c 20000 "${file_var}" 2>/dev/null """
        f"""| grep -oE '"content":"[^"]{{1,300}}' | grep -v '"content":"<system-reminder>' | head -1); """
        f"""title=${{title#*'"content":"'}}; fi; """
    )


def _posix_codex_title_probe(file_var: str) -> str:
    """:func:`_posix_title_probe` 的 codex 版：首条 user_message 的 message 字段。"""
    return (
        f"""title=$(head -c 200000 "${file_var}" 2>/dev/null """
        f"""| grep -m1 -oE '"type":"user_message","message":"[^"]{{1,300}}' ); """
        f"""title=${{title##*'"message":"'}}; """
    )


def _posix_claude_mode_probe(file_var: str) -> str:
    """claude 的会话权限模式：``permissionMode``（每次切档写一行、部分 user 帧也带）。

    取值（default/plan/acceptEdits/bypassPermissions/auto）正好是平台 mode 的取值集，
    原样带出不做映射。先读文件尾部（最近一次档位=当前档），读不到回退首部
    （只在开头设过一次的会话）——两次读都有界（60KB/4KB），不扫全文件。
    """
    return (
        f"""mode=$(tail -c 60000 "${file_var}" 2>/dev/null | grep -oE '"permissionMode":"[^"]{{1,40}}' | tail -1); """
        f"""mode=${{mode#*'"permissionMode":"'}}; """
        f"""if [ -z "$mode" ]; then mode=$(head -c 4000 "${file_var}" 2>/dev/null """
        f"""| grep -oE '"permissionMode":"[^"]{{1,40}}' | head -1); """
        f"""mode=${{mode#*'"permissionMode":"'}}; fi; """
    )


def _posix_claude_effort_probe(file_var: str) -> str:
    """claude 的思考等级：assistant 帧顶层的 ``effort``。

    ``max`` 由 Python 侧归一成平台词汇 ``xhigh``（见 :func:`_normalize_effort`）。
    同 mode 探针：先尾后首，读不到就是空串（前端显示「默认」）。
    """
    return (
        f"""effort=$(tail -c 60000 "${file_var}" 2>/dev/null | grep -oE '"effort":"[^"]{{1,20}}' | tail -1); """
        f"""effort=${{effort#*'"effort":"'}}; """
        f"""if [ -z "$effort" ]; then effort=$(head -c 4000 "${file_var}" 2>/dev/null """
        f"""| grep -oE '"effort":"[^"]{{1,20}}' | head -1); """
        f"""effort=${{effort#*'"effort":"'}}; fi; """
    )


def _posix_codex_mode_probe(file_var: str) -> str:
    """codex 的会话权限模式：``turn_context`` 的 ``sandbox_policy`` 的 type。

    rollout 里每轮都写一遍 turn_context，档位全程一致（用户中途改才会变），取尾部那条
    即可。``sandbox_policy`` 是嵌套对象（``"sandbox_policy":{"type":"workspace-write"...``），
    不是老版本的扁平 ``sandbox_mode``。值 read-only/workspace-write/danger-full-access
    正好是平台 codex mode 的取值集，原样带出。
    """
    return (
        f"""tc=$(tail -c 40000 "${file_var}" 2>/dev/null | grep -m1 -oE '"type":"turn_context".{{0,4000}}'); """
        f"""mode=$(printf '%s' "$tc" | grep -oE '"type":"(read-only|workspace-write|danger-full-access)"' | head -1); """
        f"""mode=${{mode#*'"type":"'}}; mode=${{mode%"'"'}}; """
    )


def _posix_codex_effort_probe(file_var: str) -> str:
    """codex 的思考等级：``turn_context`` 的 ``effort``（codex 词表已是
    low/medium/high/xhigh，与平台一致，原样带出）。"""
    return (
        f"""tc=$(tail -c 40000 "${file_var}" 2>/dev/null | grep -m1 -oE '"type":"turn_context".{{0,4000}}'); """
        f"""effort=$(printf '%s' "$tc" | grep -oE '"effort":"[^"]{{1,20}}' | head -1); """
        f"""effort=${{effort#*'"effort":"'}}; """
    )


def _normalize_effort(raw: str) -> str:
    """思考等级归一到平台词汇（low/medium/high/xhigh）。

    claude 写 ``max``，平台词汇是 ``xhigh``；其余原样。不认识的值给空——
    前端对空显示「默认」，不猜。
    """
    value = str(raw or "").strip().lower()
    if value == "max":
        return "xhigh"
    return value if value in ("low", "medium", "high", "xhigh") else ""


def _posix_session_command(provider: str, *, home: str, workdir: str, since: int) -> str | None:
    """POSIX 下该编辑器的列会话命令；不认识的编辑器返回 None。

    输出 ``<会话id>\\t<时间戳>\\t<cwd原始值>\\t<标题原始值>``（cwd/标题仅文件型编辑器
    有，opencode 两列都来自 sqlite）。**不在这里按工作目录筛**——过滤由 Python 做，
    见 :func:`_session_matches_workdir`。
    """
    qhome = shlex.quote(home)
    if provider == "claude":
        # claude：一个 <会话id>.jsonl 就是一个会话，文件名即 id；cwd 在文件里。
        return (
            f"for d in {qhome}/.claude/projects/*/; do "
            f'[ -d "$d" ] || continue; '
            f'probe=$(ls "$d"*.jsonl 2>/dev/null | head -1); '
            f'[ -n "$probe" ] || continue; '
            + _posix_cwd_probe("probe")
            + f'for f in "$d"*.jsonl; do '
            f'[ -f "$f" ] || continue; '
            f's=$(basename "$f" .jsonl); '
            f't=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0); '
            f'[ "$t" -ge {int(since)} ] || continue; '
            + _posix_title_probe("f")
            + _posix_claude_mode_probe("f")
            + _posix_claude_effort_probe("f")
            + f"""printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$s" "$t" "$cwd" "$title" "$mode" "$effort"; done; done 2>/dev/null"""
        )
    if provider == "codex":
        # codex：rollout-<时间>-<uuid>.jsonl，取文件名末尾的 uuid；cwd 在首行 payload 里。
        return (
            f"""find {qhome}/.codex/sessions -name 'rollout-*.jsonl' 2>/dev/null | while read -r f; do """
            + _posix_cwd_probe("f")
            + f"""t=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0); """
            f'[ "$t" -ge {int(since)} ] || continue; '
            + _posix_codex_title_probe("f")
            + _posix_codex_mode_probe("f")
            + _posix_codex_effort_probe("f")
            + f"""s=$(basename "$f" .jsonl); s=${{s##*-}}; """
            f"""printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$s" "$t" "$cwd" "$title" "$mode" "$effort"; done"""
        )
    if provider == "opencode":
        # opencode：会话在 sqlite 的 session 表，它自带 directory 列（真路径，无反斜杠
        # 转义问题），也**自带 title 列**（实测有真标题，如 "Install and configure
        # oh-my-openagent"）——只有这一家的标题是现成的。
        return (
            "node -e 'const{DatabaseSync}=require(\"node:sqlite\");"
            f'const home={json.dumps(home)};const since={int(since)};'
            'try{const d=new DatabaseSync(home+"/.local/share/opencode/opencode.db");'
            'for(const r of d.prepare("SELECT id, time_updated, directory, title FROM session WHERE time_updated >= ?").all(since*1000))'
            # 后两列 mode/effort 留空：opencode 的 session 没有权限档枚举（permission
            # 是逐工具的规则数组）也没有思考等级，读不出就如实空着（前端显示默认）。
            'console.log(r.id+"\\t"+Math.floor((r.time_updated||0)/1000)+"\\t"+(r.directory||"")+"\\t"+(r.title||"")+"\\t\\t")'
            "}catch(e){}' 2>/dev/null"
        )
    return None


def _session_matches_workdir(raw_cwd: str, workdir: str) -> bool:
    """这条会话记录属不属于该工作目录。

    两侧都要归一：编辑器记录里是 JSON 转义的原生路径（``C:\\\\Users\\\\x``，读出来
    是 ``C:\\Users\\x``），环境的 ``external_path`` 可能是 ``D:\\code\\x`` 或虚拟形式
    ``/D:/code/x``。归一后按**前缀**匹配（不是相等）：worktree 模式下会话跑在
    ``<workdir>/worktrees/<task>``，也在环境之下。

    ``workdir`` 为空（环境没绑目录）时不筛——那是不隔离环境的兜底，宁可多给也不
    漏掉本该属于这个环境的会话。
    """
    target = _normalize_workdir_for_match(workdir)
    if not target:
        return True
    candidate = _normalize_workdir_for_match(raw_cwd)
    if not candidate:
        return False
    # 大小写：**只有 Windows 路径**（带盘符）不区分——同一个目录可能被记成
    # ``D:/Code`` 或 ``d:/code``。POSIX 路径区分大小写，无脑小写会把
    # ``/home/Op`` 和 ``/home/op`` 当成同一个目录。
    if re.match(r"^[A-Za-z]:/", target):
        target, candidate = target.lower(), candidate.lower()
    return candidate == target or candidate.startswith(target + "/")


def _windows_session_command(provider: str, *, home: str, workdir: str, since: int) -> str | None:
    """Windows(PowerShell) 下该编辑器的列会话命令；不认识的编辑器返回 None。

    输出 ``<会话id>\\t<时间戳>\\t<cwd原始值>\\t<标题原始值>``，工作目录过滤由 Python
    做（见 :func:`_session_matches_workdir`）——PowerShell 里的字符串比较同样会踩
    大小写敏感的坑，不如统一到一处。

    标题同样在 shell 里只做"捞原始值"，但比 POSIX 侧多一道**大小守卫**：这里不是
    tail/head 而是 ``ReadAllText`` 整文件读，17MB 以上的 jsonl 会让扫描卡住。
    """
    # 50MB 上限：超了就不取标题（读出来对用户也没有信息量，而代价是扫描卡住）。
    size_guard = "$_.Length -le 52428800"
    if provider == "claude":
        # 两个坑，都是踩了才补的：
        #
        # 1. 正则字符类里别写 ``\r\n``——Python 侧 ``"...\"\\r\\n..."``（或这里
        #    写成 `[^\"\r\n]`）进 PS 后是**字面 CR/LF**，单引号字符串当场跨行，
        #    PS 解析器直接 MissingEndCurlyBrace、整条命令失败 → 扫描 0 行。
        #    jsonl 一行一条记录，``[^"]`` 已经够了；标题值里的 ``\n`` 是转义后的
        #    两个字符，不会真的换行。
        # 2. ``if(GUARD){...}`` 必须在 Write-Output **之前**闭合：否则写输出会落
        #    进 GUARD 里，>50MB 的 jsonl 连会话行都不出。
        body = (
            "$since=" + str(int(since)) + ";"
            "$root=Join-Path " + _ps_literal(home) + " '.claude\\projects';"
            "if(Test-Path -LiteralPath $root){"
            "Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue|ForEach-Object{"
            "$probe=Get-ChildItem -LiteralPath $_.FullName -Filter '*.jsonl' -ErrorAction SilentlyContinue|Select-Object -First 1;"
            "if(-not $probe){return};"
            "$m=[regex]::Match([IO.File]::ReadAllText($probe.FullName),'\"cwd\":\"([^\"]*)\"');"
            "$cwd='';if($m.Success){$cwd=$m.Groups[1].Value};"
            "Get-ChildItem -LiteralPath $_.FullName -Filter '*.jsonl' -ErrorAction SilentlyContinue|ForEach-Object{"
            "$t=[int][double]::Parse((Get-Date $_.LastWriteTimeUtc -UFormat %s));"
            "if($t -ge $since){"
            "$title='';$mode='';$effort='';if(" + size_guard + "){"
            "$txt=[IO.File]::ReadAllText($_.FullName);"
            "$ms=[regex]::Matches($txt,'\"lastPrompt\":\"[^\"]{1,300}');"
            "if($ms.Count -gt 0){$title=($ms[$ms.Count-1].Value -replace '^\"lastPrompt\":\"','')}"
            "else{$mc=[regex]::Match($txt,'\"content\":\"(?!<system-reminder>)[^\"]{1,300}');"
            "if($mc.Success){$title=($mc.Value -replace '^\"content\":\"','')}}"
            # 权限档：末条 permissionMode = 当前档；思考等级：末条 effort。
            # 必须在 else 之外（lastPrompt 命中与否都要取），且仍在 GUARD 内。
            "$pms=[regex]::Matches($txt,'\"permissionMode\":\"[^\"]{1,40}');"
            "if($pms.Count -gt 0){$mode=($pms[$pms.Count-1].Value -replace '^\"permissionMode\":\"','')}"
            "$efs=[regex]::Matches($txt,'\"effort\":\"[^\"]{1,20}');"
            "if($efs.Count -gt 0){$effort=($efs[$efs.Count-1].Value -replace '^\"effort\":\"','')}"
            "}"   # 关掉 GUARD：标题取不到/文件超限都照样出这一行
            "Write-Output ($_.BaseName+[char]9+$t+[char]9+$cwd+[char]9+$title+[char]9+$mode+[char]9+$effort)}}}}"
        )
        return _powershell_command(body)
    if provider == "codex":
        body = (
            "$since=" + str(int(since)) + ";"
            "$root=Join-Path " + _ps_literal(home) + " '.codex\\sessions';"
            "if(Test-Path -LiteralPath $root){"
            "Get-ChildItem -LiteralPath $root -Recurse -Filter 'rollout-*.jsonl' -ErrorAction SilentlyContinue|ForEach-Object{"
            "$m=[regex]::Match([IO.File]::ReadAllText($_.FullName),'\"cwd\":\"([^\"]*)\"');"
            "$cwd='';if($m.Success){$cwd=$m.Groups[1].Value};"
            "$t=[int][double]::Parse((Get-Date $_.LastWriteTimeUtc -UFormat %s));"
            "if($t -lt $since){return};"
            "$id=[regex]::Match($_.BaseName,'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$');"
            "$title='';$mode='';$effort='';if(" + size_guard + "){"
            "$txt=[IO.File]::ReadAllText($_.FullName);"
            "$mm=[regex]::Match($txt,'\"type\":\"user_message\",\"message\":\"[^\"]{1,300}');"
            "if($mm.Success){$title=($mm.Value -replace '^.*\"message\":\"','')}"
            # 权限档 = turn_context 里 sandbox_policy 的 type（这三个值只在该语境出现）；
            # 思考等级 = effort（codex 词表已与平台一致）。
            "$sm=[regex]::Matches($txt,'\"type\":\"(read-only|workspace-write|danger-full-access)\"');"
            "if($sm.Count -gt 0){$mode=($sm[$sm.Count-1].Groups[1].Value)}"
            "$efs=[regex]::Matches($txt,'\"effort\":\"[^\"]{1,20}');"
            "if($efs.Count -gt 0){$effort=($efs[$efs.Count-1].Value -replace '^\"effort\":\"','')}"
            "}"   # 关掉 GUARD
            "if($id.Success){Write-Output ($id.Groups[1].Value+[char]9+$t+[char]9+$cwd+[char]9+$title+[char]9+$mode+[char]9+$effort)}}"
            "}"
        )
        return _powershell_command(body)
    if provider == "opencode":
        body = (
            "node -e \"const{DatabaseSync}=require('node:sqlite');"
            "const home=process.env.USERPROFILE;"
            "const since=Number(process.argv[1]);"
            "try{const d=new DatabaseSync(home+'\\\\.local\\\\share\\\\opencode\\\\opencode.db');"
            "for(const r of d.prepare('SELECT id,time_updated,directory,title FROM session WHERE time_updated>=?').all(since*1000))"
            "console.log(r.id+'\\t'+Math.floor((r.time_updated||0)/1000)+'\\t'+(r.directory||'')+'\\t'+(r.title||''))}catch(e){}\" "
            + str(int(since)) + " 2>$null"
        )
        return _powershell_command(body)
    return None


def _ps_literal(value: str) -> str:
    """PowerShell 单引号字符串字面量（内部的单引号翻倍转义）。"""
    return "'" + str(value).replace("'", "''") + "'"


def _session_transcript_command(
    provider: str, *, is_windows: bool, home: str, session_id: str, tail_lines: int = 80
) -> str | None:
    """读某个会话记录文件的**尾部 N 行**（NDJSON，最近的在文件末尾）。

    会话详情页的「部分历史」来源：只拿尾部，不全量——jsonl 可能十几 MB，详情页
    要的是"最近聊了什么"，不是整个文件。先定位文件再取尾部，一步完成（两次
    host_exec 会翻倍往返）。要更早的历史就把这个窗口调大（见
    :func:`environment_service.get_session_transcript`），不做 OFFSET——记录按行
    取，而一条 assistant 帧能产出多条 item，行与条对不上，偏移必然漂。

    * claude：``{home}/.claude/projects/*/{session_id}.jsonl``——slug 目录名是
      cwd 转义出来的，反解有歧义，所以按文件名找，不按目录拼。
    * codex：``{home}/.codex/sessions/**/rollout-<ts>-{session_id}.jsonl``。
    * opencode：消息在 ``{home}/.local/share/opencode/opencode.db`` 的 message+
      part 两张表（data 是 JSON，role 在 message、正文在 part 的 type=text 块），
      走 node:sqlite 出 ``role<TAB>text`` 行——解析器对 TSV 形态兼容。

    返回 None = 该编辑器暂不支持读历史（详情页降级为只显信息，不报错）。
    """
    sid = str(session_id or "").strip()
    if not sid or not str(home or "").strip():
        return None
    name = str(provider or "").strip().lower()
    tail = int(tail_lines)
    if is_windows:
        if name == "claude":
            body = (
                "$f=Get-ChildItem -LiteralPath " + _ps_literal(home + "\\.claude\\projects")
                + " -Recurse -Filter " + _ps_literal(sid + ".jsonl")
                + " -ErrorAction SilentlyContinue|Select-Object -First 1;"
                "if($f){Get-Content -LiteralPath $f.FullName -Encoding UTF8 -Tail " + str(tail) + "}"
            )
            return _powershell_command(body)
        if name == "codex":
            body = (
                "$f=Get-ChildItem -LiteralPath " + _ps_literal(home + "\\.codex\\sessions")
                + " -Recurse -Filter " + _ps_literal("*" + sid + ".jsonl")
                + " -ErrorAction SilentlyContinue|Select-Object -First 1;"
                "if($f){Get-Content -LiteralPath $f.FullName -Encoding UTF8 -Tail " + str(tail) + "}"
            )
            return _powershell_command(body)
        if name == "opencode":
            # SQL 与 DB 路径都走 base64/argv（PS 双引号串里嵌 SQL 的引号会让脚本
            # 提前终止，实测 char:309）；每行输出 role<TAB>ptype<TAB>base64(part json)
            # <TAB>ts —— part 的 JSON 里有换行，base64 才不会破坏行协议。
            import base64 as _b64
            sql = (
                "SELECT json_extract(m.data,'$.role') role,"
                "json_extract(p.data,'$.type') ptype,"
                "p.data AS pdata, p.time_created AS ts "
                "FROM part p JOIN message m ON m.id=p.message_id "
                "WHERE p.session_id=? "
                "ORDER BY p.time_created DESC LIMIT ?"
            )
            sql_b64 = _b64.b64encode(sql.encode("utf-8")).decode("ascii")
            db_path = home + "\\.local\\share\\opencode\\opencode.db"
            body = (
                "node -e \"const{DatabaseSync}=require('node:sqlite');"
                "const d=new DatabaseSync(process.argv[1]);"
                "try{const s=Buffer.from(process.argv[2],'base64').toString('utf8');"
                "const rows=d.prepare(s).all(process.argv[3]," + str(tail) + ");"
                "rows.reverse();"
                "for(const r of rows){if(!r.pdata)continue;"
                "const b=Buffer.from(String(r.pdata),'utf8').toString('base64');"
                "console.log((r.role||'')+'\t'+(r.ptype||'')+'\t'+b+'\t'+Math.floor((r.ts||0)/1000))}}catch(e){}\" "
                + _ps_literal(db_path) + " " + _ps_literal(sql_b64) + " " + _ps_literal(sid)
            )
            return _powershell_command(body)
        return None
    # POSIX（BSD 便携）：find 支持 -name，head/tail 都是标准工具。
    if name == "claude":
        base = home.rstrip("/") + "/.claude/projects"
        pattern = sid + ".jsonl"
    elif name == "codex":
        base = home.rstrip("/") + "/.codex/sessions"
        pattern = "*" + sid + ".jsonl"
    elif name == "opencode":
        import base64 as _b64
        sql = (
            "SELECT json_extract(m.data,'$.role') role,"
            "json_extract(p.data,'$.type') ptype,"
            "p.data AS pdata, p.time_created AS ts "
            "FROM part p JOIN message m ON m.id=p.message_id "
            "WHERE p.session_id=? "
            "ORDER BY p.time_created DESC LIMIT ?"
        )
        sql_b64 = _b64.b64encode(sql.encode("utf-8")).decode("ascii")
        db_path = home.rstrip("/") + "/.local/share/opencode/opencode.db"
        script = (
            'const{DatabaseSync}=require("node:sqlite");'
            "const d=new DatabaseSync(process.argv[1]);"
            "try{const s=Buffer.from(process.argv[2],'base64').toString('utf8');"
            "const rows=d.prepare(s).all(process.argv[3]," + str(tail) + ");"
            "rows.reverse();"
            "for(const r of rows){if(!r.pdata)continue;"
            "const b=Buffer.from(String(r.pdata),'utf8').toString('base64');"
            'console.log((r.role||"")+"\t"+(r.ptype||"")+"\t"+b+"\t"+Math.floor((r.ts||0)/1000))}}catch(e){}'
        )
        return (
            "node -e " + shlex.quote(script) + " "
            + shlex.quote(db_path) + " " + shlex.quote(sql_b64) + " " + shlex.quote(sid)
            + " 2>/dev/null"
        )
    qhome = shlex.quote(base)
    qpat = shlex.quote(pattern)
    return (
        f"f=$(find {qhome} -name {qpat} -print 2>/dev/null | head -1); "
        f'[ -n "$f" ] && tail -n {tail} "$f" 2>/dev/null'
    )


def _editor_session_command(
    provider: str, *, is_windows: bool, home: str, workdir: str, since: int
) -> str | None:
    """该编辑器列出「这个工作目录下、近 ``since`` 以来」的会话；不认识则 None。

    ``home`` 是**编辑器实际用的 HOME**：隔离环境传环境目录（``envs/<env_id>``），
    不隔离环境传节点操作者的真实用户目录。``workdir`` 是环境的工作目录，用来把
    该 HOME 下**别的**项目的会话排除掉。
    """
    name = str(provider or "").strip().lower()
    if is_windows:
        return _windows_session_command(name, home=home, workdir=workdir, since=since)
    return _posix_session_command(name, home=home, workdir=workdir, since=since)


# 每个编辑器的 POSIX 提取命令：输出逐行一个绝对路径。
# ``HOOME`` 占位由调用方替换成已 quote 的 HOME，避免每个片段自己拼引号。
_EDITOR_PROJECT_COMMANDS_POSIX: dict[str, str] = {
    # claude：全局配置里的 projects 键就是真实路径，纯文本即可取出；不读
    # projects/<slug>/（那是转义目录名，反解有歧义），也不读取其中的 jsonl。
    "claude": (
        r"""grep -oE '"(/[A-Za-z][^"]*|[A-Za-z]:[\\/][^"]*)"[[:space:]]*:[[:space:]]*\{' """
        r"""__HOME__/.claude.json 2>/dev/null | sed 's/"//g; s/[[:space:]]*:[[:space:]]*{$//'"""
    ),
    # codex：session 记录首行的 payload.cwd。临时目录是噪音，过滤掉。
    "codex": (
        r"""grep -rhoE '"cwd":"[^"]*"' __HOME__/.codex/sessions 2>/dev/null """
        r"""| sed 's/^"cwd":"//; s/"$//; s#\\\\#/#g' """
        r"""| grep -vE '/(Temp|tmp)/|/var/folders/' """
    ),
    # opencode：项目表在 sqlite 里。节点上通常没有 sqlite3 命令，故用 node 的
    # 内置 node:sqlite（v22+）读——节点自己就跑 node，一定有。
    "opencode": (
        r"""node -e 'const{DatabaseSync}=require("node:sqlite");"""
        r"""const p=process.env.HOME+"/.local/share/opencode/opencode.db";"""
        r"""try{const d=new DatabaseSync(p);"""
        r"""for(const r of d.prepare("SELECT worktree FROM project WHERE worktree IS NOT NULL").all())"""
        r"""if(r.worktree&&r.worktree!=="/")console.log(r.worktree)}catch(e){}' 2>/dev/null"""
    ),
}

# 每个编辑器的 Windows 提取命令（PowerShell）。
_EDITOR_PROJECT_COMMANDS_WINDOWS: dict[str, str] = {
    # claude：与 POSIX 同一套正则，直接从原文提取 projects 的键。**不能用
    # ConvertFrom-Json**：Windows 路径只差大小写（D:/x 与 d:/x），而 PowerShell
    # 的 JSON 解析是大小写不敏感的，会把它们当重复键抛
    # DuplicateKeysInJsonString，整个命令以退出码 1 失败——实测踩过。
    # Sort-Object -Unique 默认也是大小写不敏感，正好把这类重复合并掉。
    "claude": (
        "$f=Join-Path $env:USERPROFILE '.claude.json';"
        "if(Test-Path -LiteralPath $f){"
        "[regex]::Matches([IO.File]::ReadAllText($f),"
        "'\"(/[A-Za-z][^\"]*|[A-Za-z]:[\\\\/\"][^\"]*)\"\\s*:\\s*\\{')"
        "|ForEach-Object{$_.Groups[1].Value.Replace('\\\\','\\').Replace('/','/')}"
        "|Sort-Object -Unique}"
    ),
    # codex：扫 session 记录的 cwd 字段。
    "codex": (
        "$root=Join-Path $env:USERPROFILE '.codex\\sessions';"
        "if(Test-Path -LiteralPath $root){"
        "Get-ChildItem -LiteralPath $root -Recurse -Filter 'rollout-*.jsonl' -ErrorAction SilentlyContinue"
        "|ForEach-Object{"
        "$m=[regex]::Match((Get-Content -LiteralPath $_.FullName -Encoding UTF8 -TotalCount 1 -ErrorAction SilentlyContinue),"
        "'\"cwd\":\"([^\"]*)\"');"
        "if($m.Success){$m.Groups[1].Value.Replace('\\\\','\\')}}"
        "|Where-Object{$_ -notmatch '\\\\(Temp|tmp)\\\\'}"
        "|Sort-Object -Unique}"
    ),
    # opencode：同样读 sqlite，用 node:sqlite。
    "opencode": (
        "node -e \"const{DatabaseSync}=require('node:sqlite');"
        "const p=process.env.USERPROFILE+'\\\\.local\\\\share\\\\opencode\\\\opencode.db';"
        "try{const d=new DatabaseSync(p);"
        "for(const r of d.prepare('SELECT worktree FROM project WHERE worktree IS NOT NULL').all())"
        "if(r.worktree&&r.worktree!=='/')console.log(r.worktree)}catch(e){}\" 2>$null"
    ),
}


def _editor_project_command(provider: str, *, is_windows: bool, home: str) -> str | None:
    """该编辑器列出「它开过哪些项目目录」的命令；不认识编辑器时返回 None。

    ``home`` 是节点上的用户目录（POSIX 形式或原生 Windows 形式），由调用方先
    探测——各编辑器的记录都挂在它下面。
    """
    table = _EDITOR_PROJECT_COMMANDS_WINDOWS if is_windows else _EDITOR_PROJECT_COMMANDS_POSIX
    template = table.get(str(provider or "").strip().lower())
    if template is None:
        return None
    if is_windows:
        return _powershell_command(template)
    return template.replace("__HOME__", shlex.quote(home))


def _posix_git_repo_scan_command(roots: list[str], depth: int) -> str:
    """List git working trees under each of ``roots``, bounded and pruned.

    ``-prune`` on the dependency directories keeps the walk off
    ``node_modules``-shaped trees (the difference between milliseconds and
    minutes). ``.git`` itself is pruned too so its object store is never
    enumerated, and it is printed whether it is a directory (a normal clone)
    or a file (a worktree/submodule checkout) — either way its parent is a
    working tree worth listing. find 接受多个起点，不存在的起点报错但已被
    ``2>/dev/null`` 吞掉，所以缺目录的标准位置不会变成一次失败。

    GNU/BSD 都支持的写法：不用 ``-printf``（macOS find 没有）；``-maxdepth``
    两者皆有；``\\( ... \\)`` 分组加显式 ``-print``（出现显式动作后 find 不再
    隐式打印）。超时由调用方给的 ``timeout_ms`` 兜底。
    """
    qroots = " ".join(shlex.quote(r) for r in roots)
    prune = _scan_prune_expression()
    return (
        f"find {qroots} -maxdepth {depth} "
        f"\\( -type d \\( {prune} \\) \\) -prune -o -name .git -prune -print 2>/dev/null"
    )


def _windows_git_repo_scan_command(roots: list[str], depth: int) -> str:
    """PowerShell equivalent of :func:`_posix_git_repo_scan_command`.

    Enumerates ``.git`` entries (dir or file — a worktree's ``.git`` is a file)
    with a depth-bounded manual walk, pruning the same dependency directories.
    ``-Force`` is required or hidden entries like ``.git`` are skipped. 多个起
    点逐个解码进 ``$roots`` 后依次 Walk；不存在的起点 Get-ChildItem 静默报错，
    不影响其他起点。
    """
    prune = ",".join(f"'{name}'" for name in _SCAN_PRUNE_DIRS)
    decode_roots = "".join(
        "$roots+=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
        f"{_ps_data(root)}'));"
        for root in roots
    )
    body = (
        f"$roots=@();{decode_roots}"
        + f"$max={depth};$prune=@({prune});$out=New-Object System.Collections.ArrayList;"
        + "function Walk([string]$d,[int]$level){"
        + "if($level -gt $max){return};"
        + "$items=@(Get-ChildItem -LiteralPath $d -Force -ErrorAction SilentlyContinue);"
        + "foreach($i in $items){"
        + "if($i.Name -eq '.git'){[void]$out.Add($d);continue};"
        + "if($i.PSIsContainer -and ($prune -notcontains $i.Name)){Walk $i.FullName ($level+1)}"
        + "}};"
        + "foreach($r in $roots){Walk $r 0};"
        + "[pscustomobject]@{repos=@($out)}|ConvertTo-Json -Depth 4 -Compress"
    )
    return _powershell_command(body)


def _parse_posix_scan(stdout: str) -> list[str]:
    """Turn ``find``'s ``.../.git`` lines into the parent working-tree paths."""
    repos: list[str] = []
    for line in (stdout or "").splitlines():
        entry = line.strip()
        if not entry.endswith("/.git"):
            continue
        parent = entry[: -len("/.git")]
        if parent:
            repos.append(parent)
    return repos


def _parse_windows_scan(result: dict[str, Any]) -> list[str]:
    payload = _json_output(result)
    raw = payload.get("repos")
    if not isinstance(raw, list):
        return []
    repos: list[str] = []
    for item in raw:
        try:
            repos.append(_native_to_windows_path(str(item or "")))
        except (TypeError, ValueError):
            continue
    return repos


class GitRepoScanReq(BaseModel):
    """Scan parameters.

    ``provider`` is the editor whose own project record is read — that record is
    what makes the list meaningful (and what keeps a Codex pick from listing
    Claude's projects). ``root`` empty means "list what that editor has opened";
    a non-empty ``root`` scans exactly that directory instead, which is what the
    directory browser sends once the user navigates somewhere specific.
    """

    provider: str = ""
    root: str = ""
    depth: int = _GIT_REPO_SCAN_DEFAULT_DEPTH


def _normalize_scanned_path(raw: str, *, is_windows: bool) -> str | None:
    """Normalize one path a node reported into the virtual form we hand back.

    Both editors record native paths (``D:/code/x`` or ``C:\\code\\x``); the API
    speaks the virtual form (``/D:/code/x``). Anything that is not an absolute
    path is dropped rather than guessed at.
    """
    value = (raw or "").strip().strip('"')
    if not value:
        return None
    try:
        if is_windows:
            return _native_to_windows_path(value.replace("/", "\\"))
        return _path(value)
    except (HTTPException, TypeError, ValueError):
        return None


async def _do_git_repo_scan(node_id: str, req: GitRepoScanReq) -> dict[str, Any]:
    """List the project directories an editor has opened on a node.

    Authorization is the caller's job. With a ``provider`` this reads that
    editor's own record of the directories it ran in — the only authoritative
    source, and the thing that makes the result editor-specific. Without one (or
    for an editor with no usable record) the result is empty and the caller
    should fall back to browsing the filesystem.

    A non-empty ``root`` short-circuits all of that: scan exactly that directory
    for git working trees, which is what the directory browser asks for.
    """
    client, is_windows = await _node_is_windows(node_id)
    depth = max(1, min(int(req.depth or _GIT_REPO_SCAN_DEFAULT_DEPTH), _GIT_REPO_SCAN_MAX_DEPTH))

    root = (req.root or "").strip()
    if root:
        if is_windows:
            native = _windows_path(root)[1] or "C:\\"
            virtual = _windows_path(root)[0]
            command = _windows_git_repo_scan_command([native], depth)
        else:
            virtual = _path(root)
            command = _posix_git_repo_scan_command([virtual], depth)
        try:
            result = await client.host_exec(
                node_id, command, cwd="", timeout_ms=60_000, max_output_bytes=1_000_000
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if error := _host_exec_error(result):
            raise HTTPException(status_code=400, detail=error)
        repos = (
            _parse_windows_scan(result)
            if is_windows
            else _parse_posix_scan(str(result.get("stdout") or ""))
        )
        return {
            "root": virtual,
            "depth": depth,
            "repos": _dedupe_nested(repos),
            "truncated": False,
            "source": "directory",
        }

    provider = str(req.provider or "").strip().lower()
    if provider not in _SCAN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支持的编辑器：{provider or '(空)'}")

    # 各编辑器的记录都挂在用户目录下，先探测它。探测失败不报错——多数编辑器
    # 的记录本来就可能不存在（没跑过），空列表才是正确结果。
    try:
        home_res = await client.host_exec(
            node_id,
            _powershell_command("[Console]::Out.Write($env:USERPROFILE)")
            if is_windows
            else "printf '%s' \"$HOME\"",
            cwd="",
            max_output_bytes=8192,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raw_home = str(home_res.get("stdout") or "").strip()
    if not raw_home:
        raise HTTPException(status_code=502, detail="节点返回了空用户目录")
    if is_windows:
        try:
            virtual_home = _native_to_windows_path(ntpath.normpath(raw_home))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="节点返回了无效用户目录") from exc
    else:
        virtual_home = _path(raw_home)

    command = _editor_project_command(provider, is_windows=is_windows, home=virtual_home)
    if command is None:
        # 该编辑器没有已知的项目记录格式（cursor/gemini/dsh 目前如此）。空列表 +
        # 让前端引导去浏览目录，好过拿转义目录名冒充路径。
        return {
            "root": virtual_home,
            "depth": depth,
            "repos": [],
            "truncated": False,
            "source": "editor",
        }

    try:
        result = await client.host_exec(
            node_id, command, cwd="", timeout_ms=60_000, max_output_bytes=1_000_000
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # 退出码在这里没有意义，只看 stdout：
    #   * grep 找不到东西时退出 1——而"该编辑器没在这台节点上开过项目"正是靠这个
    #     表达的，那是正常的空，不是失败；
    #   * node 的 sqlite 是实验特性，会把警告写到 stderr 并带非 0 码，但 stdout
    #     的数据是好的。
    # 所以记录读不到 = 空列表。若把它当错误，前端只会看到一片空，分不清是
    # "没有记录"还是"读失败了"。
    stdout = str(result.get("stdout") or "")
    candidates = [line for line in stdout.splitlines() if line.strip()]
    repos: list[str] = []
    for raw in candidates:
        normalized = _normalize_scanned_path(raw, is_windows=is_windows)
        if normalized:
            repos.append(normalized)
    unique = _dedupe_nested(repos)
    truncated = len(unique) > _GIT_REPO_SCAN_MAX_REPOS
    return {
        "root": virtual_home,
        "depth": depth,
        "repos": unique[:_GIT_REPO_SCAN_MAX_REPOS],
        "truncated": truncated,
        "source": "editor",
    }


def _dedupe_nested(paths: list[str]) -> list[str]:
    """Unique paths, dropping any that sits under another listed path.

    A repo nested inside another repo's tree would otherwise be listed twice;
    the shallowest is the one worth adopting.
    """
    unique = sorted(set(paths))
    top: list[str] = []
    for path in unique:
        if any(path != other and path.startswith(other.rstrip("/") + "/") for other in unique):
            continue
        top.append(path)
    return top


def _parse_editor_sessions(stdout: str, workdir: str = "") -> list[dict[str, Any]]:
    """把 ``<会话id>\\t<epoch秒>[\\t<cwd>[\\t<标题>[\\t<权限档>[\\t<思考等级>]]]]`` 逐行解析成
    会话记录，并按工作目录筛。

    容忍脏行（编辑器可能往 stdout 写警告）：一行至少要两段、第二段是整数才算。
    会话 id 去重时保留较新的时间——同一个会话被列两次只可能是记录重复。

    按工作目录筛在**这里**做而不是在命令里：路径归一的转义层数在 shell 里太难对齐
    （实测踩过两次），放到 Python 有测试兜着。标题列原样带出（清洗在对账入库时做，
    见 :func:`environment_service._clean_session_title`），同 id 保留较新一条的标题。

    第 5/6 列是**从会话记录里读出来的**权限档与思考等级（claude/codex 能读、opencode
    读不到留空）——同步为任务时用它们预填任务参数，不让用户重选。思考等级经
    :func:`_normalize_effort` 归一（claude 的 ``max`` → 平台词汇 ``xhigh``）。
    """
    by_id: dict[str, tuple[int, str, str, str]] = {}
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        session_id = parts[0].strip()
        if not session_id:
            continue
        try:
            updated = int(parts[1].strip())
        except (TypeError, ValueError):
            continue
        raw_cwd = parts[2].strip() if len(parts) > 2 else ""
        if not _session_matches_workdir(raw_cwd, workdir):
            continue
        # 标题在第 4 列；只到 3 列的旧输出按无标题处理。权限档/思考等级在第 5/6 列。
        title = parts[3].strip() if len(parts) > 3 else ""
        mode = parts[4].strip() if len(parts) > 4 else ""
        effort = _normalize_effort(parts[5].strip()) if len(parts) > 5 else ""
        known = by_id.get(session_id)
        if known is None or updated > known[0]:
            by_id[session_id] = (updated, title, mode, effort)
    return [
        {"session_id": sid, "updated_at": updated, "title": title,
         "mode": mode, "effort": effort}
        for sid, (updated, title, mode, effort) in sorted(
            by_id.items(), key=lambda kv: kv[1][0], reverse=True
        )
    ]


def _item_fingerprint(item: dict) -> str:
    """一条会话记录的**内容指纹**——导入去重的键。

    **刻意不含** ``item["id"]`` / ``logical_event_id``：codex 的消息条目用的是解析器
    自增 counter（``t0``/``t1``…），窗口从 400 行调到 800 行时同一条消息会拿到
    **不同的 id**——拿它去重，历史就会整段重复入库。所以指纹只取"内容本身"里跨
    窗口不变的字段：类型 + 阶段 + 事件种类 + 秒级时间 + 正文/标题/工具输入输出。

    工具的开帧与闭帧 ``phase``/``input``/``output`` 不同 → 指纹不同 → 各存一行
    （正是前端 ``reduceItems`` 要吃的两条帧）；同一窗口重复读 → 指纹一致 → 跳过。

    已知边界：正文截到 500 字、时间只到秒，所以**同一秒内完全相同的两段正文**会
    判成同一条。会话记录里这就是重复，解析器本来也已把它们并掉，接受。
    """
    import hashlib

    def _part(value, limit: int) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value[:limit]
        return repr(value)[:limit]

    fields = (
        str(item.get("type") or ""),
        str(item.get("phase") or ""),
        str(item.get("event_kind") or ""),
        str(item.get("created_at") if item.get("created_at") is not None else ""),
        _part(item.get("text"), 500),
        _part(item.get("title"), 200),
        _part(item.get("tool_name"), 200),
        _part(item.get("input"), 300),
        _part(item.get("output"), 300),
    )
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()[:40]


def _stable_item_id(item: dict, fingerprint: str) -> str:
    """返回给前端的稳定 id。

    ``reduceItems`` 是按 ``item.id`` **就地合并**的，所以这个 id 必须满足两件事：
    同一条消息跨窗口永不变（否则重复气泡），工具的开帧与闭帧必须**相同**（否则
    一张卡永远转圈）。

    * ``tool_call``：用 ``logical_event_id``——它就是 ``call_id``/``tool_use_id``/
      ``callID``，三家都是原生稳定 id，且开闭帧同源。
    * 其余（消息/推理）：用 ``env:{指纹}``。codex 消息没有原生 id，counter 又会
      漂，指纹是唯一既稳定又唯一的来源。
    """
    if str(item.get("type") or "") == "tool_call":
        logical = str(item.get("logical_event_id") or "").strip()
        if logical:
            return logical[:200]
    return f"env:{fingerprint}"


def _transcript_epoch(value) -> int | None:
    """会话记录里的时间戳 → epoch 秒。ISO 串 / 数字（秒或毫秒）都收。"""
    from datetime import datetime

    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 10_000_000_000 else int(value)
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return _transcript_epoch(int(raw))
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _transcript_text(value) -> str:
    """工具输出/结果可能是字符串或块数组，统一成文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return ""


def _wrap_text(text: str) -> bool:
    """系统包装（不是人说的话）——别进对话流。"""
    return not text or text.startswith(
        ("<system-reminder>", "<total_tokens>", "<environment_context>", "<permissions ",
         "<turn_aborted>", "# AGENTS.md instructions")
    )


def _tool_step_summary(tool_name: str, tool_input) -> str | None:
    """卡片副标题——运行时 ``toolStepSummary``（runners/claude.ts）的 Python 同款。

    词表必须与那边一致：预览卡片的标题行和任务页长得一样靠的就是它（模型的
    description、操作对象的路径/命令/正则）。改这里先改那边，别单方面加词。
    """
    if not isinstance(tool_input, dict):
        return None

    def _s(value) -> str:
        return value if isinstance(value, str) else ""

    description = _s(tool_input.get("description"))
    if description:
        return description
    path = _s(tool_input.get("file_path")) or _s(tool_input.get("notebook_path"))
    pattern = _s(tool_input.get("pattern"))
    if tool_name in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit"):
        return path or None
    if tool_name == "Bash":
        return _s(tool_input.get("command")) or None
    if tool_name in ("Grep", "Glob"):
        return (f"{pattern} · {path}" if pattern and path else pattern) or None
    if tool_name == "WebFetch":
        return _s(tool_input.get("url")) or None
    if tool_name == "WebSearch":
        return _s(tool_input.get("query")) or None
    if tool_name == "Task":
        return _s(tool_input.get("description")) or None
    return None


def _msg_item(
    item_id, item_type: str, text: str, ts: int | None, event_kind: str,
    model: str = "",
) -> dict:
    """对话/思考条目——任务持久化层的规范形状（task_service 写 user_input、
    运行时发 agent_message/reasoning 的同一套字段）。"""
    item = {
        "id": str(item_id), "type": item_type, "text": text,
        "logical_event_id": str(item_id), "event_kind": event_kind,
        "phase": "complete", "status": "done", "created_at": ts,
    }
    if item_type == "user_input":
        # user_input 在持久化层是极简三件套（{id, type, text}），renderer 只吃
        # 这三个；保持和写入侧一致、少即是多。
        item.pop("logical_event_id")
        item.pop("event_kind")
        item.pop("phase")
        item.pop("status")
    elif model:
        # 助手消息带模型名（运行时的 claudeMessageToItems 同款）——详情页顶部的
        # 只读输入区显示"这个会话用的什么模型"，靠的就是它。
        item["model"] = model
    return item


def _tool_item(
    tool_id: str,
    *,
    title: str,
    phase: str,
    status: str,
    tool_input=None,
    output=None,
    summary: str | None = None,
    ts: int | None = None,
) -> dict:
    """工具调用条目（开帧或闭帧）。

    与运行时 ``claudeMessageToItems`` 同构：开帧带 title/input/status=running，
    闭帧稀疏但**必须**带 title/tool_name/logical_event_id（错过开帧的下游消费者
    才能路由）；错误闭帧 phase=error、status=failed。
    """
    item = {
        "id": str(tool_id), "type": "tool_call",
        "title": title, "tool_name": title,
        "logical_event_id": str(tool_id), "event_kind": "tool",
        "phase": phase, "status": status, "created_at": ts,
    }
    if tool_input is not None:
        item["input"] = tool_input
    # output 保持**原样**（字符串或内容块数组）——运行时就存 rawBlock.content，
    # 前端 outputText()/buildToolMeta 两边都吃得下；空值不发，免得闭帧把开帧的
    # 已有字段盖成空（mergeItems 只跳过 None 与空串，不跳过空数组）。
    if output is not None and output != "" and output != [] and output != {}:
        item["output"] = output
    if summary:
        item["summary"] = summary
    return item


def _parse_transcript_lines(
    raw: str, limit: int, provider: str = "claude"
) -> tuple[list[dict], bool]:
    """把会话记录解析成**任务管线吃的规范条目**（NormalizedItem）。

    「规范」不是"我看着差不多"，而是逐字段对齐那两个权威生产者：节点运行时
    ``runners/claude.ts`` 的 ``claudeMessageToItems``（实时帧）与
    ``task_service``/``task_message_store`` 的持久化条目（回放帧）。词表：

    * ``type``：``user_input`` / ``agent_message`` / ``reasoning`` / ``tool_call``
    * ``status``：**``running`` / ``done`` / ``failed``**（运行时的 SDK 词汇；
      前端 ``mapStatus`` 才翻译成 ``in_progress``/``completed``——别直接写后者）
    * ``logical_event_id`` / ``event_kind``(``message``/``reasoning``/``tool``) /
      ``phase``(``start``/``complete``/``error``) / ``tool_name``：路由与合并用的
      canonical 字段，稀疏闭帧也必须带
    * 工具 id 用**裸 id**（``tool_use_id`` / ``call_id`` / ``callID``），不自己加
      前缀；开帧与闭帧同 id，由 ``reduceItems`` 按 id 并成一张卡
    * ``summary``：``_tool_step_summary``（运行时同名函数的 Python 同款）

    为什么不是 ``{role,text}`` 那种简化形状：详情页要和任务详情**长得一样**（工具卡
    能展开、有输入输出），而任务那侧已经有整套渲染（``reduceItems`` +
    ``itemToRootMessage`` + ``TimelineRow``/``MessageBody``）。这里直接产出它吃的
    形状，前端不用再写一层映射。

    规格差异（本机三家真实会话都核对过）：

    * **claude**：一行一事件，顶层 ``type=user|assistant``，content 是块数组
      （text / thinking / tool_use / tool_result）；消息 id 用 ``uuid:index``
      （运行时同款）。``isSidechain`` 是子代理线程，不进主对话。
    * **codex**：两代格式并存——老版对话在 ``event_msg`` 的 ``payload.type``
      （user_message / agent_message），新版只有 ``response_item`` 的 message
      （role=user/assistant；developer 是注入指令）。同一条两边都落一份且成对
      相邻，按相邻文本去重。工具是 function_call / function_call_output（外加
      apply_patch 走的 custom_tool_call*，都靠 ``call_id`` 配对）。
    * **opencode**：由 node:sqlite 出每行 ``role<TAB>type<TAB>base64(part json)``
      ——base64 是因为 part 的 JSON 里有换行，会破坏行协议。DB 里一个 tool part
      就是终态，故发**单条**完整条目（开帧字段 + output），不拆两帧。

    ``tail`` 按行截，首行可能是半条 JSON——解析失败跳过。返回 (items, truncated)。
    """
    import base64 as _b64
    import json as _json

    name = str(provider or "").strip().lower()
    out: list[dict] = []
    seen_more = False
    lines = [line for line in (raw or "").splitlines() if line.strip()]
    if len(lines) >= limit:
        seen_more = True
    counter = [0]
    turn_model = [""]  # codex：最近一条 turn_context 的模型名，盖给之后的助手消息
    last_src, last_text = "", ""  # codex 双格式成对落盘的相邻去重锚点
    # 工具名表：闭帧（tool_result / function_call_output）只有 id，名字从开帧
    # 恢复——运行时同理（toolNamesById），闭帧缺名字就成了无标题空卡。
    tool_names: dict[str, str] = {}

    def _codex_dup(src: str, text: str) -> bool:
        """codex 新老格式各记一次同一条消息且成对相邻：文本相同、来源不同 = 另一半
        （重复）；来源相同则可能是用户又发了一遍同样的话，保留并更新锚点。"""
        nonlocal last_src, last_text
        if text == last_text and src != last_src:
            return True
        last_src, last_text = src, text
        return False

    def _push(item: dict) -> None:
        item.setdefault("id", "t%d" % counter[0])
        item["id"] = str(item["id"])
        out.append(item)
        counter[0] += 1

    for line in lines:
        # ── opencode：role<TAB>type<TAB>base64(json) ──
        if name == "opencode":
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            role, ptype = parts[0].strip(), parts[1].strip()
            if role not in ("user", "assistant"):
                continue  # 只有这两类角色进对话流（工具/系统行走 part 类型，不靠 role）
            try:
                part = _json.loads(_b64.b64decode(parts[2].strip()).decode("utf-8"))
            except Exception:  # noqa: BLE001 — 坏行跳过
                continue
            ts = _transcript_epoch(part.get("time") or part.get("time_created"))
            if ts is None and len(parts) > 3 and parts[3].strip().isdigit():
                ts = int(parts[3].strip())
            item_id = str(part.get("id") or "").strip()
            if ptype == "text":
                text = str(part.get("text") or "").strip()
                if _wrap_text(text):
                    continue
                _push(_msg_item(
                    item_id or counter[0],
                    "user_input" if role == "user" else "agent_message",
                    text, ts, "message",
                ))
            elif ptype == "reasoning":
                text = str(part.get("text") or "").strip()
                if text:
                    _push(_msg_item(item_id or counter[0], "reasoning", text, ts, "reasoning"))
            elif ptype == "tool":
                # DB 里一个 part 就是终态 → 单条完整条目（不拆开帧/闭帧）。
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                db_status = str(state.get("status") or "")
                status = {"completed": "done", "error": "failed"}.get(db_status, "running")
                tool_input = state.get("input")
                title = str(part.get("tool") or "")
                _push(_tool_item(
                    str(part.get("callID") or part.get("id") or counter[0]),
                    title=title, phase="complete" if status == "done" else "start",
                    status=status, tool_input=tool_input,
                    output=state.get("output"),
                    # opencode 自带的 state.title 是它的一步说明；缺了再按 claude
                    # 词表推（小写工具名会落空，回落到卡片默认标题）。
                    summary=str(state.get("title") or "")
                    or _tool_step_summary(title, tool_input),
                    ts=ts,
                ))
            continue

        try:
            obj = _json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        ts = _transcript_epoch(obj.get("timestamp"))

        # ── codex ──
        # 两代记录格式并存：老版把对话写进 event_msg（user_message/agent_message），
        # 新版只有 response_item message（role=user/assistant）。同一条消息两边都会
        # 落一次、文本相同且**成对相邻**（user: RI→EV，assistant: EV→RI），所以与上
        # 一条正文相同就跳过——既不重复，也不误伤隔开的两次相同提问。
        # turn_context 行带当轮模型名（payload.model）：取来盖在助手消息上，详情页
        # 顶部的只读输入区显示"这个会话用的什么模型"靠的就是它。
        if name == "codex":
            top = str(obj.get("type") or "")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if top == "turn_context":
                turn_model[0] = str(payload.get("model") or "").strip()
                continue
            inner = str(payload.get("type") or "")
            if top == "event_msg":
                if inner in ("user_message", "agent_message"):
                    text = str(payload.get("message") or "").strip()
                    if not _wrap_text(text) and not _codex_dup("ev", text):
                        _push(_msg_item(
                            counter[0],
                            "user_input" if inner == "user_message" else "agent_message",
                            text, ts, "message",
                            model=turn_model[0] if inner == "agent_message" else "",
                        ))
                elif inner == "agent_reasoning":
                    text = str(payload.get("text") or "").strip()
                    if text and not _codex_dup("ev", text):
                        _push(_msg_item(counter[0], "reasoning", text, ts, "reasoning"))
            elif top == "response_item":
                if inner == "message" and str(payload.get("role") or "") in ("user", "assistant"):
                    # developer/system role 是注入的指令（permissions/skills），不进对话流
                    text = str(_transcript_text(payload.get("content")) or "").strip()
                    if not _wrap_text(text) and not _codex_dup("ri", text):
                        _push(_msg_item(
                            counter[0],
                            "user_input" if payload.get("role") == "user" else "agent_message",
                            text, ts, "message",
                            model=turn_model[0] if payload.get("role") == "assistant" else "",
                        ))
                elif inner == "reasoning":
                    text = str(
                        _transcript_text(payload.get("content"))
                        or _transcript_text(payload.get("summary"))
                        or ""
                    ).strip()
                    if text and not _codex_dup("ri", text):
                        _push(_msg_item(counter[0], "reasoning", text, ts, "reasoning"))
                elif inner in ("function_call", "custom_tool_call"):
                    call_id = str(payload.get("call_id") or "")
                    if inner == "function_call":
                        args = payload.get("arguments")
                        parsed = None
                        if isinstance(args, str):
                            try:
                                parsed = _json.loads(args)
                            except ValueError:
                                parsed = {"arguments": args}
                    else:  # custom_tool_call（apply_patch 等）：input 是原样文本
                        parsed = {"input": payload.get("input")}
                    tool_name = str(payload.get("name") or "")
                    if call_id:
                        tool_names[call_id] = tool_name
                    # custom_tool_call 自带终态（status=completed）→ 直接发 done/
                    # complete；function_call 无终态标记 → running/start。
                    done = payload.get("status") == "completed"
                    _push(_tool_item(
                        call_id or counter[0],
                        title=tool_name,
                        phase="complete" if done else "start",
                        status="done" if done else "running",
                        tool_input=parsed,
                        # function_call 的 arguments 解析失败时是 {"arguments": ...}
                        # 这种包装 dict，词表推不出 summary 就落 None。
                        summary=_tool_step_summary(tool_name, parsed)
                        if isinstance(parsed, dict) and "input" not in parsed else None,
                        ts=ts,
                    ))
                elif inner in ("function_call_output", "custom_tool_call_output"):
                    call_id = str(payload.get("call_id") or "")
                    _push(_tool_item(
                        call_id or counter[0],
                        # 闭帧必须带名字（错过开帧的消费者才能路由）——运行时从
                        # toolNamesById 恢复，这里同款。
                        title=tool_names.get(call_id, ""), phase="complete",
                        status="done",
                        output=_transcript_text(payload.get("output")) or None,
                        ts=ts,
                    ))
            continue

        # ── claude（默认） ──
        if obj.get("isSidechain"):
            continue
        top = str(obj.get("type") or "")
        if top not in ("user", "assistant"):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        # 运行时用 uuid:index 作消息块 id（同一 assistant 帧里 text/thinking 多块
        # 要互不撞）。jsonl 顶层就有 uuid，取来复用——和 claude.ts 的 id 同源。
        msg_uuid = str(obj.get("uuid") or "")
        # 模型名在 message.model（运行时把它盖在每条助手块上，详情页顶部只读输入区
        # 显示"这个会话用的什么模型"）。用户帧没有。
        block_model = str(message.get("model") or "").strip() if top == "assistant" else ""
        content = message.get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text", "text": content}] if isinstance(content, str) else []
        )
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            block_id = f"{msg_uuid}:{index}" if msg_uuid else counter[0]
            if btype == "text":
                text = str(block.get("text") or "").strip()
                if _wrap_text(text):
                    continue
                _push(_msg_item(
                    block_id,
                    "user_input" if top == "user" else "agent_message",
                    text, ts, "message", model=block_model,
                ))
            elif btype == "thinking":
                text = str(block.get("thinking") or "").strip()
                if text:
                    _push(_msg_item(block_id, "reasoning", text, ts, "reasoning"))
            elif btype == "tool_use":
                tool_id = str(block.get("id") or block_id)
                tool_name = str(block.get("name") or "")
                if tool_id:
                    tool_names[tool_id] = tool_name
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    tool_input = {}
                _push(_tool_item(
                    tool_id, title=tool_name, phase="start", status="running",
                    tool_input=tool_input,
                    summary=_tool_step_summary(tool_name, tool_input),
                    ts=ts,
                ))
            elif btype == "tool_result":
                tool_id = str(block.get("tool_use_id") or "")
                if not tool_id:
                    continue  # 没法配对的开帧，丢了它（合并对不上也没法渲染）
                is_error = block.get("is_error") is True
                _push(_tool_item(
                    tool_id,
                    # 闭帧名字从开帧恢复——运行时用 toolNamesById 同款
                    title=tool_names.get(tool_id, ""),
                    phase="error" if is_error else "complete",
                    status="failed" if is_error else "done",
                    # 原样 content（字符串或块数组），运行时不拍平
                    output=block.get("content"),
                    ts=ts,
                ))

    if len(out) > limit:
        out = out[-limit:]
        seen_more = True
    return out, seen_more


def _normalize_workdir_for_match(value: str) -> str:
    """把工作目录归一成「正斜杠、无盘符前导斜杠」的形式，供 shell 前缀匹配。

    两侧形式天然不一致，必须在比较前对齐：

    * 编辑器记录里是 JSON 转义的原生路径（``C:\\\\Users\\\\x``，读出来是
      ``C:\\Users\\x``），先把双反斜杠压成单的，再转正斜杠；
    * 环境的 ``external_path`` 是用户输入的原样，可能是 ``D:\\code\\x`` 或虚拟形式
      ``/D:/code/x``。

    统一成 ``D:/code/x``：反斜杠转正斜杠，Windows 虚拟形式的前导斜杠去掉。POSIX
    路径本来就是这个形态，不受影响。
    """
    raw = str(value or "").strip().replace("\\\\", "\\").replace("\\", "/")
    if re.match(r"^/[A-Za-z]:", raw):
        raw = raw[1:]
    return raw.rstrip("/") or raw


class EnvSessionScanReq(BaseModel):
    """扫某个环境的会话。

    ``provider`` 决定读哪个编辑器的记录，``env_id`` 决定读哪个 HOME（隔离环境是
    环境自己的目录，不隔离是操作者真实 HOME），``workdir`` 把该 HOME 下**别的**
    项目的会话排除掉，``days`` 只扫最近 N 天——更久的会话不扫，服务器也不存。
    全部由调用方给出，节点不猜。
    """

    provider: str = ""
    env_id: str = ""
    workdir: str = ""
    days: int = 7


async def _do_env_session_scan(node_id: str, req: EnvSessionScanReq) -> dict[str, Any]:
    """列出某个环境、工作目录下、近 N 天的会话。**节点只读，不写任何东西。**

    会话历史是编辑器自己的事——它写就写、不写就不写。节点做的是去**读**编辑器
    留下的记录（跟「列项目」同一套来源），所以这里既不需要新 proto 帧，也不需要
    节点持久化任何会话状态。

    隔离与否决定记录在哪个 HOME 下：隔离环境用 ``envs/<env_id>``（那本身就是编辑器
    的 HOME），不隔离环境用节点操作者的真实用户目录。
    """
    client, is_windows = await _node_is_windows(node_id)
    provider = str(req.provider or "").strip().lower()
    if provider not in _SCAN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支持的编辑器：{provider or '(空)'}")
    env_id = str(req.env_id or "").strip()
    workdir = _normalize_workdir_for_match(req.workdir)
    days = max(1, min(int(req.days or 7), 90))
    since = int(time.time()) - days * 86400

    # 环境 HOME：隔离环境就是节点上那个环境目录；不隔离则退回操作者 HOME。
    # 环境目录由节点自己算：``work_root`` 是节点上报的 capability（见
    # nodes/common/agent/client.go 的 capabilityLabels），``envs/<env_id>`` 是
    # 节点的内部布局——服务端不猜、不拼，只读节点报的事实。拿不到就走操作者 HOME。
    home = ""
    if env_id:
        try:
            from .nodes_service import nodes_service

            home = await nodes_service.environment_home_path(node_id, env_id)
        except Exception:  # noqa: BLE001 — 拿不到就走操作者 HOME
            home = ""
    if not home:
        try:
            res = await client.host_exec(
                node_id,
                _powershell_command("[Console]::Out.Write($env:USERPROFILE)")
                if is_windows
                else "printf '%s' \"$HOME\"",
                cwd="",
                max_output_bytes=8192,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        home = str(res.get("stdout") or "").strip()
        if not home:
            raise HTTPException(status_code=502, detail="节点返回了空用户目录")

    command = _editor_session_command(
        provider, is_windows=is_windows, home=home, workdir=workdir, since=since
    )
    if command is None:
        # 该编辑器没有已知的会话记录格式——空列表，不猜。
        return {"sessions": [], "home": home}

    try:
        result = await client.host_exec(
            node_id, command, cwd="", timeout_ms=60_000, max_output_bytes=1_000_000
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # 退出码在这里没有意义（grep 找不到东西就退 1），只看 stdout——跟列项目同款。
    # 工作目录过滤在解析时做（见 _parse_editor_sessions），不在命令里。
    sessions = _parse_editor_sessions(str(result.get("stdout") or ""), workdir)
    return {"sessions": sessions, "home": home}


@router.post("/{node_id}/git-repos")
async def node_git_repo_scan(
    node_id: str, req: GitRepoScanReq, user: User = Depends(_require_admin)
) -> dict[str, Any]:
    """List git working trees under a node directory (admin surface)."""
    del user
    return await _do_git_repo_scan(node_id, req)


@team_router.post("/{node_id}/files")
async def user_node_host_file(
    node_id: str,
    req: HostFileRequest,
    request: Request,
    user: User = Depends(_require_node_access),
) -> dict[str, Any]:
    """Host file operation on a node the signed-in user is authorized to use."""
    result = await _do_host_file(node_id, req)
    op = (req.operation or "list").strip().lower()
    if op in _MUTATING_OPS:
        await _audit_user_file_op(
            request, user, node_id, op, {"path": req.path, "destination": req.destination or ""}
        )
    return result


@team_router.post("/{node_id}/git-repos")
async def user_node_git_repo_scan(
    node_id: str,
    req: GitRepoScanReq,
    user: User = Depends(_require_node_access),
) -> dict[str, Any]:
    """List git working trees under a node directory (user surface).

    Read-only: nothing is written, so it is not audited. The caller must hold a
    grant for this node and the node must be approved, online and a real client
    — the same gate the file browser and terminal use, since a scan reads the
    node's disk just as the file browser does.
    """
    return await _do_git_repo_scan(node_id, req)


def _upload_destination(is_windows: bool, directory: str, filename: str) -> tuple[str, str]:
    """Resolve (virtual_path, node_native_path) for uploading ``filename`` into
    ``directory``. The filename is validated as a single path segment — no
    separators, no traversal, no drive letter — so it cannot escape the chosen
    directory or smuggle in an absolute path."""
    name = (filename or "").strip()
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="非法文件名")
    if "/" in name or "\\" in name or "\x00" in name or ":" in name:
        raise HTTPException(status_code=400, detail="文件名不能包含路径分隔符")
    if is_windows:
        virtual, native = _windows_path(directory)
        if native is None:
            raise HTTPException(status_code=400, detail="不能上传到文件系统根目录")
        # Reuse the strict segment checks by validating the full joined path.
        joined_virtual = f"{virtual.rstrip('/')}/{name}"
        _, joined_native = _windows_path(joined_virtual)
        if joined_native is None:  # pragma: no cover - defensive
            raise HTTPException(status_code=400, detail="非法 Windows 路径")
        return joined_virtual, joined_native
    directory_path = _path(directory)
    joined = posixpath.normpath(f"{directory_path.rstrip('/')}/{name}")
    if joined in {"/", "."} or ".." in joined.split("/"):
        raise HTTPException(status_code=400, detail="非法路径")
    return joined, joined


async def _do_host_file_upload(
    node_id: str, path: str, file: UploadFile, overwrite: bool
) -> dict[str, Any]:
    """Upload one file to a node host directory. Authorization is the caller's job."""
    data = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件不能超过 10 MiB")

    client, is_windows = await _node_is_windows(node_id)

    virtual_path, native_path = _upload_destination(is_windows, path, file.filename or "")
    try:
        result = await client.upload_file(node_id, native_path, data, overwrite=bool(overwrite))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "上传失败")
    return {
        "ok": True,
        "path": virtual_path,
        "bytes_written": int(result.get("bytes_written") or 0),
    }


@router.post("/{node_id}/files/upload")
async def node_host_file_upload(
    node_id: str,
    path: str = Form(...),
    file: UploadFile = File(...),
    overwrite: bool = Form(False),
    user: User = Depends(_require_admin),
) -> dict[str, Any]:
    """Upload a binary file to a node host directory via chunked HostFileUpload.

    ``path`` is the target directory (virtual form: ``/`` for POSIX, ``/C:/..``
    for Windows). The uploaded part's filename becomes the destination name; the
    node writes to a temp file and atomically renames on the final chunk.
    """
    del user  # 管理端不需要调用者身份，鉴权已由依赖完成
    return await _do_host_file_upload(node_id, path, file, overwrite)


@team_router.post("/{node_id}/files/upload")
async def user_node_host_file_upload(
    node_id: str,
    request: Request,
    path: str = Form(...),
    file: UploadFile = File(...),
    overwrite: bool = Form(False),
    user: User = Depends(_require_node_access),
) -> dict[str, Any]:
    """Upload to a node the signed-in user is authorized to use (audited)."""
    result = await _do_host_file_upload(node_id, path, file, overwrite)
    await _audit_user_file_op(
        request, user, node_id, "upload", {"path": result.get("path") or path}
    )
    return result
