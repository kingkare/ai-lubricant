"""Case-scanning pipeline — pull structured cases out of an L1 raw snapshot.

The unified extraction principle (``test-project-definition.md`` §五):
each source adapter only does "fetch + parse into intermediate format";
**extraction (rules / anchoring) goes through one pipeline**. This module is
that pipeline for ``kind=case`` sources — it reads an L1 raw snapshot and
extracts ``mc_test_case`` rows.

Column mapping is **configurable per source** (via ``source.config.column_map``)
with smart Chinese/English defaults, so a real Excel/CSV/XMind of cases drops
straight in. Cases are upserted by ``(project_id, source_id, external_key)`` —
re-scanning the same file updates, never duplicates.

Deterministic only — no LLM anywhere on this path. Agent-track PRD ingestion
(analyst workflow) is phase 3 and goes through ``knowledge.ingest`` instead.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from uuid import UUID as _UUID

from .models import KnowledgeRaw
from .source_fetch import SourceFetchError


class CaseScanError(ValueError):
    """扫描用例失败（raw 不存在 / 不可识别为用例源 / 字段缺失）。"""


# ── 列名归一化（中英双名 → 规范字段）──────────────────────────
_COLUMN_ALIASES: dict[str, str] = {
    "title": "title", "name": "title", "标题": "title", "用例名称": "title", "用例标题": "title", "名称": "title",
    "intent": "intent", "目的": "intent", "意图": "intent", "测试目的": "intent", "用途": "intent",
    "preconditions": "preconditions", "precondition": "preconditions",
    "前置条件": "preconditions", "前置": "preconditions", "前提": "preconditions",
    "steps": "steps", "step": "steps", "步骤": "steps", "操作步骤": "steps",
    "expected": "expected", "预期": "expected", "预期结果": "expected", "期望": "expected",
    "expected_result": "expected", "expect": "expected", "result": "expected",
    "priority": "priority", "优先级": "priority", "等级": "priority",
    "tags": "tags", "tag": "tags", "标签": "tags",
    "module": "module", "模块": "module",
    "feature": "feature", "功能点": "feature",
    "key": "external_key", "external_key": "external_key", "编号": "external_key", "用例编号": "external_key",
}


def _normalize_key(raw_key: Any) -> str:
    return str(raw_key or "").strip().lower().replace(" ", "_")


def _map_columns(headers: list[str], column_map: Optional[Mapping[str, str]]) -> dict[int, str]:
    """Build {col_index: canonical_field} for a tabular source.

    Explicit ``column_map`` wins; else auto-match against the alias table.
    """
    out: dict[int, str] = {}
    explicit: dict[str, str] = {}
    if column_map:
        explicit = {_normalize_key(k): v for k, v in column_map.items()}

    for idx, header in enumerate(headers):
        norm = _normalize_key(header)
        if norm in explicit:
            out[idx] = explicit[norm]
            continue
        if header in _COLUMN_ALIASES:
            out[idx] = _COLUMN_ALIASES[header]
            continue
        if norm in _COLUMN_ALIASES:
            out[idx] = _COLUMN_ALIASES[norm]
            continue
    return out


def _row_to_case(row: list[str], col_map: dict[int, str], seq: int) -> Optional[dict[str, Any]]:
    """One CSV/Excel row → case dict; returns None if no title."""
    rec: dict[str, Any] = {}
    for idx, value in enumerate(row):
        field = col_map.get(idx)
        if field is None:
            continue
        rec[field] = value
    title = (rec.get("title") or "").strip()
    if not title:
        return None
    rec.setdefault("external_key", f"row-{seq:04d}")
    rec.setdefault("priority", None)
    rec.setdefault("tags", [])
    rec.setdefault("module", None)
    rec.setdefault("feature", None)
    rec["priority"] = _normalize_priority(rec.get("priority"))
    if isinstance(rec.get("tags"), str):
        rec["tags"] = [t.strip() for t in rec["tags"].replace("，", ",").split(",") if t.strip()]
    return rec


def _normalize_priority(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().upper()
    if s in ("P0", "P1", "P2", "P3"):
        return s
    if s in ("0", "BLOCKER"):
        return "P0"
    if s in ("1", "CRITICAL"):
        return "P1"
    if s in ("2", "MAJOR"):
        return "P2"
    if s in ("3", "MINOR", "LOW"):
        return "P3"
    return None


# ── 从 L1 raw 抽取用例（按 adapter_type 分发）──────────────────
def extract_cases(raw: KnowledgeRaw, column_map: Optional[Mapping[str, str]]) -> list[dict[str, Any]]:
    """读一个 ``mc_knowledge_raw`` 行，抽出用例记录（含 external_key）。

    支持：excel / csv / json / xmind。其余格式（word/text/markdown）→ 空集
    （那些是 PRD 源，不是用例源；用例扫描只针对 kind=case 的源）。
    """
    at = raw.adapter_type.lower()
    content = raw.content or {}
    if at == "excel":
        cases: list[dict[str, Any]] = []
        seq = 0
        sheets = content.get("sheets", {}) if isinstance(content, dict) else {}
        for sheet_name, sheet in sheets.items():
            rows = sheet.get("rows", []) if isinstance(sheet, dict) else []
            if not rows:
                continue
            headers = [str(h or "").strip() for h in rows[0]]
            col_map = _map_columns(headers, column_map)
            for row in rows[1:]:
                rec = _row_to_case([str(c or "") for c in row], col_map, seq)
                seq += 1
                if rec is not None:
                    rec.setdefault("sheet", sheet_name)
                    cases.append(rec)
        return cases
    if at == "csv":
        rows = content.get("rows", []) if isinstance(content, dict) else []
        if not rows:
            return []
        headers = [str(h or "").strip() for h in rows[0]]
        col_map = _map_columns(headers, column_map)
        return [rec for i, row in enumerate(rows[1:])
                if (rec := _row_to_case([str(c or "") for c in row], col_map, i)) is not None]
    if at in ("json", "jsonl"):
        rows = content.get("rows") if at == "jsonl" else content.get("cases") or content.get("value")
        if not isinstance(rows, list):
            return []
        cases = []
        for i, item in enumerate(rows):
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            if not title:
                continue
            cases.append({
                "title": title,
                "intent": str(item.get("intent") or item.get("purpose") or "").strip() or None,
                "preconditions": item.get("preconditions") or item.get("precondition"),
                "steps": item.get("steps"),
                "expected": item.get("expected"),
                "priority": _normalize_priority(item.get("priority")),
                "tags": _coerce_tags(item.get("tags")),
                "module": item.get("module"),
                "feature": item.get("feature"),
                "external_key": str(item.get("key") or item.get("external_key") or f"json-{i:04d}"),
            })
        return cases
    if at == "xmind":
        return _xmind_to_cases(content)
    return []  # word/text/markdown 不是用例源


def _coerce_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    return [t.strip() for t in str(value).replace("，", ",").split(",") if t.strip()]


def _xmind_to_cases(content: dict[str, Any]) -> list[dict[str, Any]]:
    """XMind 主题树 → 用例：叶子主题（含预期）= 一条用例，路径=模块层级。

    约定：叶子主题的 ``labels`` 作为优先级标签，``notes`` 作为预期结果。
    """
    cases: list[dict[str, Any]] = []

    def walk(topic: Mapping[str, Any], path: list[str]) -> None:
        title = str(topic.get("title", "")).strip()
        if not title:
            return
        children = (topic.get("children", {}) or {}).get("attached", [])
        if not children:  # 叶子
            notes = topic.get("notes") or ""
            labels = topic.get("labels") or []
            cases.append({
                "title": title,
                "intent": None,
                "preconditions": None,
                "steps": [{"action": p, "expected": None} for p in path],
                "expected": notes if isinstance(notes, str) and notes else None,
                "priority": _normalize_priority(labels[0] if isinstance(labels, list) and labels else None),
                "tags": _coerce_tags(labels),
                "module": path[0] if path else None,
                "feature": path[-1] if path else None,
                "external_key": "/".join(path + [title]),
            })
            return
        for child in children:
            if isinstance(child, Mapping):
                walk(child, path + [title])

    sheets = content.get("sheets", []) if isinstance(content, dict) else []
    for sheet in sheets:
        if isinstance(sheet, Mapping):
            root = sheet.get("root", {})
            if isinstance(root, Mapping):
                walk(root, [])
    return cases


# ── 扫描：raw → upsert mc_test_case ───────────────────────────
async def scan_cases(
    *,
    raw_id: Any,
    project_id: Any,
    column_map: Optional[Mapping[str, str]] = None,
    created_by: Any,
) -> dict[str, Any]:
    """从一份 raw 抽取用例并 upsert 到 ``mc_test_case``。

    幂等键 = ``(project_id, source_id, external_key)``：同 raw 重扫更新而非重复。
    用例 ``confirm_source="doc"``、``confirm_ref`` 指向 raw（确认门：源即依据）。
    """
    from ..assets.models import TestCase
    from ..assets.service import _as_uuid as _asset_uuid  # 复用现有 UUID 归一
    from ..assets.service import _next_code  # TC-xxxx 项目内递增

    try:
        raw = await KnowledgeRaw.get(id=_asset_uuid(raw_id, field="raw_id"))
    except Exception as exc:  # DoesNotExist
        raise CaseScanError(f"raw 不存在：{raw_id!r}") from exc

    if str(raw.project_id) != str(project_id):
        raise CaseScanError(f"raw {raw_id} 不属于项目 {project_id}")

    cases = extract_cases(raw, column_map)
    created = updated = unchanged = 0
    result_cases: list[dict[str, Any]] = []
    scan_scope = _scan_scope(raw)

    for case_rec in cases:
        external_key = case_rec["external_key"]
        existing = await _find_case_by_external_key(scan_scope, raw.project_id, external_key)
        payload = {
            "title": case_rec["title"][:512],
            "intent": case_rec.get("intent"),
            "preconditions": _as_jsonb(case_rec.get("preconditions")),
            "expected": _as_jsonb(case_rec.get("expected")),
            "confirm_source": "doc",
            "confirm_ref": {
                "raw_id": str(raw.id),
                "source_id": str(raw.source_id) if raw.source_id else None,
                "locator": raw.locator,
                "scan_scope": scan_scope,
                "external_key": external_key,
                "adapter_type": raw.adapter_type,
            },
            "priority": case_rec.get("priority"),
            "module": case_rec.get("module"),
            "feature": case_rec.get("feature"),
            "test_type": case_rec.get("test_type"),
        }
        if existing is None:
            case = await TestCase.create(
                project_id=raw.project_id,
                code=await _next_code(TestCase, raw.project_id, "TC-"),
                created_by=_asset_uuid(created_by, field="created_by"),
                **payload,
            )
            created += 1
            result_cases.append({"id": str(case.id), "external_key": external_key,
                                 "title": case.title, "action": "created"})
        else:
            changed = False
            for k, v in payload.items():
                if getattr(existing, k, None) != v:
                    setattr(existing, k, v)
                    changed = True
            if changed:
                await existing.save(update_fields=list(payload.keys()))
                updated += 1
                result_cases.append({"id": str(existing.id), "external_key": external_key,
                                     "title": existing.title, "action": "updated"})
            else:
                unchanged += 1
                result_cases.append({"id": str(existing.id), "external_key": external_key,
                                     "title": existing.title, "action": "unchanged"})

    return {
        "raw_id": str(raw.id),
        "extracted": len(cases),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "cases": result_cases,
    }


async def _find_case_by_external_key(scope_key: str, project_id: _UUID, external_key: str):
    """按 (project_id, scan_scope, external_key) 找已导入的用例。

    ``confirm_ref`` 是 JSONB，Tortoise 不支持 JSONB 路径过滤，所以先按
    project_id 拉候选，再在 Python 侧匹配 confirm_ref。用例量级一般 <1万，
    一次扫描足够快；超大规模再上 GIN 索引。
    """
    from ..assets.models import TestCase

    candidates = await TestCase.filter(project_id=project_id, confirm_source="doc")
    for case in candidates:
        ref = case.confirm_ref or {}
        if ref.get("external_key") == external_key and ref.get("scan_scope") == scope_key:
            return case
    return None


def _scan_scope(raw: KnowledgeRaw) -> str:
    """扫描作用域：决定"什么算同一条用例"。

    * 有 ``source_id`` → 按源（源配置是身份的真相，重扫/内容变更都算同一条）
    * 无源但有 ``locator`` → 按文件位置（同一份文件改了内容仍是同一条）
    * 两者皆无 → 按 raw 行（退化：每份快照各自成一条，绝不误合并）
    """
    if raw.source_id:
        return f"source:{raw.source_id}"
    if raw.locator:
        return f"locator:{raw.locator}"
    return f"raw:{raw.id}"


def _as_jsonb(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    return {"value": value}


__all__ = [
    "CaseScanError",
    "extract_cases",
    "scan_cases",
]
