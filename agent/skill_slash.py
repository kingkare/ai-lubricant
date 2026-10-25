r"""技能（Skill）斜杠命令支持 —— 运行时聚合、标记解析与会话激活。

设计口径（不建表、不碰权限、纯提示词层）：
1. 目录聚合：SOP 目录（内置 + 绑定 + 私有蒸馏）+ 生效 MCP 服务。
   内置原子工具（code_run/web/file_* 等 9 个）显式不进列表。
2. 标记解析：行内任意位置匹配 `/(?P<tok>[\w\-·一-鿿]+)`，
   token 必须精确命中已知技能键，代码围栏内与 URL/路径不误伤。
3. 激活时机：**只看当前这条消息**。技能是「用了就生效、不用就结束」的一次性
   引用——上一条消息里的 /browser 不该影响这一条。命中后把技能的基础信息
   （名字、说明、正文位置、关键参数）追加到**这条用户消息**里（见
   :func:`build_skill_message_suffix`），不进 system prompt、不逐轮注入。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

SkillKind = Literal["sop", "mcp", "pkg"]

_MAX_SKILL_PROMOTIONS = 5
# 每条激活技能注入当前消息的字符上限（基础信息，不灌正文）。
_MAX_SKILL_NOTE_CHARS = 600

# 候选标记正则：要求斜杠前非单词/路径字符；匹配中英文、连字符、点。
_CANDIDATE_RE = re.compile(
    r"(?<![\w/:.\\-])/(?P<tok>[\w\-\.·一-鿿]+)",
    re.UNICODE,
)
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_TRAILING_PUNCT = ".,;:!?~…，。；：！？\t\r\n"


@dataclass(frozen=True)
class SkillItem:
    kind: SkillKind
    name: str              # trigger token without leading slash, e.g. "browser", "site-service"
    display_name: str = "" # human-readable title for UI / prompt (empty → fall back to name)
    description: str = ""  # one-liner summary
    sop_id: str = ""       # catalog uuid or "private:<filename>"
    service: str = ""      # MCP service name (kind == "mcp")
    location: str = ""     # where the body lives for the model: "memory/sop/x" (kind == "sop")
    enabled: bool = True


@dataclass
class SkillCatalog:
    items: list[SkillItem] = field(default_factory=list)
    by_name: dict[str, SkillItem] = field(default_factory=dict)


def _norm(key: str) -> str:
    """Normalize a trigger token for collision-free lookup: lowercase, _ to -."""
    return str(key or "").strip().lower().replace("_", "-")


def _slug_trigger(ref_or_name: str) -> str:
    """Derive a concise trigger slug from a filename or title.

    Examples:
        "browser_sop.md" -> "browser"
        "builtin/site_service_sop.md" -> "site-service"
        "private/notes.md" -> "notes"
        "cdp-bridge" -> "cdp-bridge"
    """
    raw = Path(str(ref_or_name or "").strip()).name
    if raw.lower().endswith(".md"):
        raw = raw[:-3]
    stem = _norm(raw)
    if stem.endswith("-sop") and len(stem) > 4:
        stem = stem[:-4]
    # Keep only safe identifier chars
    cleaned = re.sub(r"[^\w\-\.·一-鿿]+", "-", stem).strip("-.")
    return cleaned


async def build_catalog(
    agent_id: int | None = None,
    *,
    mcp_user_id: int | None = None,
) -> SkillCatalog:
    """Aggregate available skills for one Agent at runtime.

    Conflict resolution: sop > mcp > pkg; first-seen wins within the same kind.
    Built-in atomic tools (code_run, web, etc.) are explicitly excluded.
    """
    items: list[SkillItem] = []

    # 1. SOP catalog (builtin + bound + private distilled)
    try:
        from agent.sop_service import list_for_agent, list_catalog

        data = (
            await list_for_agent(agent_id)
            if agent_id
            else {"available": await list_catalog(include_disabled=False)}
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[skill] list SOPs failed agent=%s: %s", agent_id, exc)
        data = {"available": []}

    for row in data.get("available", []):
        if not row.get("enabled", True) or not row.get("exists", True):
            continue
        ref = row.get("filename") or row.get("name") or ""
        trigger = _slug_trigger(ref)
        if not trigger or trigger in ("readme", "index"):
            continue
        items.append(SkillItem(
            kind="sop",
            name=trigger,
            display_name=str(row.get("name") or trigger),
            description=str(row.get("description") or ""),
            sop_id=str(row.get("id") or ""),
            # 正文位置：L1 指针形状 memory/sop/<name>，让模型直接 file_read。
            location=f"memory/sop/{Path(ref).name}",
        ))

    # 2. Effective MCP services (only granted + ready)
    if mcp_user_id:
        try:
            from agent.mcp_client import resolve_effective_services

            services = await resolve_effective_services(int(mcp_user_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[skill] resolve MCP services failed user=%s: %s", mcp_user_id, exc)
            services = []
        for svc in services:
            name = str(svc.get("name") or "").strip()
            if not name:
                continue
            trigger = _slug_trigger(name)
            if not trigger:
                continue
            items.append(SkillItem(
                kind="mcp",
                name=trigger,
                display_name=name,
                description=str(svc.get("description") or ""),
                service=name,
            ))

    # 3. Deduplicate (first-seen wins: SOP before MCP)
    by_name: dict[str, SkillItem] = {}
    out: list[SkillItem] = []
    for it in items:
        key = _norm(it.name)
        if key in by_name:
            continue
        by_name[key] = it
        out.append(it)

    return SkillCatalog(items=out, by_name=by_name)


def parse_skill_tokens(text: str, catalog: SkillCatalog) -> list[str]:
    """Extract matched skill trigger keys from text (inline anywhere).

    Rules:
    - Strips code fences (```...```) before scanning.
    - Excludes URLs (https://x/web), relative paths (./foo, ../x), absolute paths (/etc/hosts).
    - Excludes pure digits (/123) and tokens with trailing slashes (/dir/).
    - Accepts case-insensitive matching; accepts "-sop" suffix fallback.
    - Returns ordered, deduplicated normalized trigger keys.
    """
    if not text or not catalog.by_name:
        return []

    scrubbed = _CODE_FENCE_RE.sub(" ", text)
    matched: list[str] = []
    seen: set[str] = set()

    for m in _CANDIDATE_RE.finditer(scrubbed):
        tok = m.group("tok").rstrip(_TRAILING_PUNCT)
        if not tok or tok.isdigit():
            continue
        # Avoid path segments like "/etc/hosts"
        after_pos = m.end()
        if after_pos < len(scrubbed) and scrubbed[after_pos] == "/":
            continue

        # -/ _ 互通 + "-sop" 后缀兜底；命中后返回**目录里的存储键**
        # （by_name 的 key），保证 collect 能直接回查。
        key = _norm(tok)
        hit = None
        stored_key = ""
        for cand in (key, key.replace("-", "_")):
            if cand in catalog.by_name:
                hit = catalog.by_name[cand]
                stored_key = cand
                break
        if not hit and key.endswith("-sop"):
            for cand in (key[:-4], key[:-4].replace("-", "_")):
                if cand in catalog.by_name:
                    hit = catalog.by_name[cand]
                    stored_key = cand
                    break

        if hit and hit.enabled and stored_key not in seen:
            seen.add(stored_key)
            matched.append(stored_key)

    return matched


async def collect_activated_skills(
    *,
    agent_id: int | None,
    mcp_user_id: int | None = None,
    current_text: str | None = None,
    max_skills: int = _MAX_SKILL_PROMOTIONS,
    catalog: SkillCatalog | None = None,
) -> list[SkillItem]:
    """Derive the skills this turn activates, from **the current message only**.

    技能是「这一条消息里用了才生效」的一次性引用：只看 ``current_text``，不扫历史。
    上一条消息里的 /browser 不应影响这一条；用户随时可以换用别的技能。返回至多
    ``max_skills`` 个 SkillItem，按首次出现顺序去重。
    """
    if not current_text:
        return []
    catalog = catalog or await build_catalog(agent_id, mcp_user_id=mcp_user_id)
    if not catalog.by_name:
        return []

    seen: set[str] = set()
    activated: list[SkillItem] = []
    for key in parse_skill_tokens(current_text, catalog):
        if key in seen:
            continue
        seen.add(key)
        item = catalog.by_name.get(key)
        if item:
            activated.append(item)
            if len(activated) >= max_skills:
                break
    return activated


def _skill_note(item: SkillItem) -> str:
    """One activated skill's basic info: name, one-liner, where the body is.

    只给基础信息（名字、说明、正文位置）——正文本身由模型按需 file_read，
    不在这里灌全文（技能正文可能很长，且多数轮次用不到全部）。
    """
    title = item.display_name or item.name
    parts = [f"/{item.name}"]
    if title and title != item.name:
        parts.append(f"（{title}）")
    line = "".join(parts)
    if item.description:
        line += f"：{_short(item.description)}"
    if item.kind == "mcp" and item.service:
        line += f" [MCP 服务 {item.service}；用 capability_call 调用其方法]"
    elif item.sop_id or item.location:
        loc = item.location or item.sop_id
        line += f" [SOP: {loc}，用 file_read 读取]"
    return f"- {line}"


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def build_skill_message_suffix(
    items: list[SkillItem],
) -> str:
    """Render activated skills as a suffix appended to **the current user message**.

    返回 "" 表示无激活。注入点是那条消息本身（option B）：技能随消息一次性
    出现，下一条消息没提就自然结束——不写进 system prompt，也不逐轮重复。
    """
    if not items:
        return ""
    lines = [_skill_note(it) for it in items]
    block = "[本轮技能]\n" + "\n".join(lines)
    return block[:_MAX_SKILL_NOTE_CHARS]


def append_to_last_user(messages: list[dict], suffix: str) -> bool:
    """Append ``suffix`` to the last user-role message in ``messages``, in place.

    The activation belongs to the request the user just sent, and ``messages``
    is the per-turn LLM view rebuilt from storage — mutating it keeps the note
    out of the database (the stored row holds the raw user text). Returns False
    when there is no user message to attach to.
    """
    if not suffix or not messages:
        return False
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                # Multimodal content parts: append a text part.
                content.append({"type": "text", "text": suffix})
            else:
                msg["content"] = f"{content or ''}\n\n{suffix}".strip()
            return True
    return False


__all__ = [
    "SkillItem",
    "SkillCatalog",
    "build_catalog",
    "parse_skill_tokens",
    "collect_activated_skills",
    "build_skill_message_suffix",
    "append_to_last_user",
]
