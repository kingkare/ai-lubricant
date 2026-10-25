"""Source-fetch layer — get content from any source into the L1 raw layer.

The **dual-track** design (``test-project-definition.md`` §八):

* **A track (builtin adapters)** — deterministic parsers for formats the
  platform owns: ``json`` / ``jsonl`` / ``csv`` / ``markdown`` / ``text`` /
  ``excel``(.xlsx) / ``word``(.docx) / ``xmind``. No LLM anywhere.
* **B track (agent channel)** — the platform provides no parser. It only
  exposes ``source.fetch`` as a *capability*: an agent (dsh) resolves the
  source itself (via its own MCP/Skill/API) and hands back content, which
  enters the same L1 layer. "Reading a PRD" and "reading a PRD that lives in
  Feishu" are the **same path** from here on.

Everything lands in ``mc_knowledge_raw`` — immutable, content-hash keyed.
Extraction (anchors / rules / cases) is the *next* pipeline stage and stays
out of this module.
"""
from __future__ import annotations

import hashlib
import json
import csv as _csv
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from xml.etree import ElementTree

from tortoise.exceptions import IntegrityError

from .models import KnowledgeRaw

# --------------------------------------------------------------------------
# adapter registry (A track)
# --------------------------------------------------------------------------

#: media types the builtin adapters understand → parser name
BUILTIN_ADAPTERS: dict[str, str] = {
    "json": "json",
    "jsonl": "jsonl",
    "csv": "csv",
    "markdown": "text",
    "md": "text",
    "text": "text",
    "txt": "text",
    "xlsx": "excel",
    "excel": "excel",
    "docx": "word",
    "word": "word",
    "xmind": "xmind",
}


class SourceFetchError(ValueError):
    """取源/解析失败（未知格式、文件缺失、包损坏…）。"""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_adapter_type(adapter: str, adapter_type: str) -> Optional[str]:
    """Map (track, declared type) → builtin parser name, or None for B track."""
    if adapter == "agent":
        return None
    key = (adapter_type or "").lower().strip()
    return BUILTIN_ADAPTERS.get(key)


# ── A-track parsers: bytes → (structured content, plain text) ───────────

def _parse_json(data: bytes) -> tuple[dict, Optional[str]]:
    obj = json.loads(data.decode("utf-8"))
    return (obj if isinstance(obj, dict) else {"value": obj}), None


def _parse_jsonl(data: bytes) -> tuple[dict, Optional[str]]:
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    return {"rows": rows}, None


def _parse_csv(data: bytes) -> tuple[dict, Optional[str]]:
    text = data.decode("utf-8-sig")
    reader = _csv.reader(io.StringIO(text))
    rows = [row for row in reader if row]
    return {"rows": rows}, text


def _parse_text(data: bytes) -> tuple[dict, Optional[str]]:
    text = data.decode("utf-8", errors="replace")
    return {}, text


def _parse_excel(data: bytes) -> tuple[dict, Optional[str]]:
    """xlsx → ``{"sheets": {name: {headers: [...], rows: [[...]]}}}``。

    xlsx is a zip of XML — parsed with stdlib (zipfile + ElementTree), no
    openpyxl dependency. Cell text only; formulas/styles are not preserved.
    """
    out: dict[str, Any] = {"sheets": {}}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # workbook.xml: sheet name → rId;  workbook.xml.rels: rId → target
            wb = ElementTree.fromstring(zf.read("xl/workbook.xml"))
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                  "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
            rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rid_target: dict[str, str] = {}
            for rel in rels:
                rid_target[rel.get("Id", "")] = rel.get("Target", "")

            # shared strings (inline cells reference them by index)
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                sst = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sst.findall("m:si", ns):
                    shared.append("".join(t.text or "" for t in si.iter(
                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))

            for sheet in wb.find("m:sheets", ns):
                name = sheet.get("name", "")
                rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
                target = rid_target.get(rid, "")
                if not target:
                    continue
                path = "xl/" + target.lstrip("/")
                if path not in zf.namelist():
                    continue
                ws = ElementTree.fromstring(zf.read(path))
                rows: list[list[str]] = []
                for row in ws.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                    cells: list[str] = []
                    for c in row.findall("m:c", ns):
                        ctype = c.get("t", "")
                        v = c.find("m:v", ns)
                        if ctype == "s" and v is not None:
                            idx = int(v.text or "0")
                            cells.append(shared[idx] if idx < len(shared) else "")
                        elif ctype == "inlineStr":
                            isv = c.find("m:is", ns)
                            cells.append("".join(t.text or "" for t in isv.iter(
                                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")) if isv is not None else "")
                        else:
                            cells.append(v.text if v is not None and v.text is not None else "")
                    rows.append(cells)
                out["sheets"][name] = {"rows": rows}
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise SourceFetchError(f"excel 解析失败：{exc}") from exc
    return out, None


def _parse_word(data: bytes) -> tuple[dict, Optional[str]]:
    """docx → paragraphs + tables, and a flattened markdown-ish text."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            doc = ElementTree.fromstring(zf.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise SourceFetchError(f"word 解析失败：{exc}") from exc

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    body = doc.find(f"{W}body")
    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []
    lines: list[str] = []
    if body is not None:
        for child in body:
            if child.tag == f"{W}p":
                text = "".join(t.text or "" for t in child.iter(f"{W}t"))
                paragraphs.append(text)
                if text.strip():
                    lines.append(text)
            elif child.tag == f"{W}tbl":
                table: list[list[str]] = []
                for tr in child.findall(f"{W}tr"):
                    row = ["".join(t.text or "" for t in tc.iter(f"{W}t"))
                           for tc in tr.findall(f"{W}tc")]
                    table.append(row)
                    lines.append(" | ".join(row))
                tables.append(table)
    return {"paragraphs": paragraphs, "tables": tables}, "\n".join(lines) or None


def _parse_xmind(data: bytes) -> tuple[dict, Optional[str]]:
    """xmind (zen or legacy) → nested topic tree.

    XMind Zen (.xmind ≥2020) stores content as ``content.json``; legacy
    (XMind 8) as ``content.xml``. Both are handled; output shape is the same:
    ``{"sheets": [{"title", "root": {topic tree}}]}``.
    """
    sheets: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "content.json" in names:  # XMind Zen
                data_json = json.loads(zf.read("content.json").decode("utf-8"))
                for sheet in data_json if isinstance(data_json, list) else []:
                    root = sheet.get("rootTopic") or {}
                    sheets.append({
                        "title": sheet.get("title", ""),
                        "root": _xmind_zen_topic(root),
                    })
            elif "content.xml" in names:  # XMind 8 legacy
                doc = ElementTree.fromstring(zf.read("content.xml"))
                for sheet_el in doc.iter("sheet"):
                    topic_el = sheet_el.find(".//topic")
                    sheets.append({
                        "title": sheet_el.findtext("title") or "",
                        "root": _xmind_xml_topic(topic_el) if topic_el is not None else {},
                    })
            else:
                raise SourceFetchError("xmind 包内无 content.json / content.xml")
    except (zipfile.BadZipFile, ElementTree.ParseError, json.JSONDecodeError) as exc:
        raise SourceFetchError(f"xmind 解析失败：{exc}") from exc

    def _flatten(topic: Mapping[str, Any]) -> list[str]:
        lines = [str(topic.get("title", ""))]
        for child in topic.get("children", {}).get("attached", []):
            lines.extend("  " + ln for ln in _flatten(child))
        return lines

    text = "\n".join(ln for sheet in sheets for ln in _flatten(sheet["root"]))
    return {"sheets": sheets}, text or None


def _xmind_zen_topic(topic: Mapping[str, Any]) -> dict[str, Any]:
    children = topic.get("children", {}) or {}
    return {
        "title": topic.get("title", ""),
        "children": {
            "attached": [_xmind_zen_topic(c) for c in children.get("attached", []) if isinstance(c, Mapping)],
            "detached": [_xmind_zen_topic(c) for c in children.get("detached", []) if isinstance(c, Mapping)],
        },
        **({"labels": topic["labels"]} if topic.get("labels") else {}),
        **({"notes": topic["notes"]} if topic.get("notes") else {}),
    }


def _xmind_xml_topic(el: Optional[ElementTree.Element]) -> dict[str, Any]:
    if el is None:
        return {}
    children = el.find("children")
    attached = []
    detached = []
    if children is not None:
        for mode in ("attached", "detached"):
            topics_el = children.find(mode)
            if topics_el is not None:
                bucket = attached if mode == "attached" else detached
                bucket.extend(_xmind_xml_topic(t) for t in topics_el.findall("topic"))
    return {"title": el.findtext("title") or "", "children": {"attached": attached, "detached": detached}}


_PARSERS = {
    "json": _parse_json,
    "jsonl": _parse_jsonl,
    "csv": _parse_csv,
    "text": _parse_text,
    "excel": _parse_excel,
    "word": _parse_word,
    "xmind": _parse_xmind,
}


# ── ingest: bytes → mc_knowledge_raw (idempotent on content hash) ────────

async def ingest_raw(
    *,
    project_id: Any,
    source_id: Any = None,
    kind: str = "product",
    adapter: str = "builtin",
    adapter_type: str,
    data: bytes,
    locator: Optional[str] = None,
    title: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """把一份源内容落入 L1（``mc_knowledge_raw``），返回文档 + 解析产物。

    - **content_hash 幂等**：同 (project, source, hash) 已存在 → ``created=False``，
      不重复入库（重拉同一份 PRD 不产生新行）。
    - A 轨（builtin）：按 adapter_type 分发解析器，结构化产物进 ``content``。
    - B 轨（agent）：不做解析，原样入 ``content_text``/``content``——解析是 agent 的活。
    """
    if not data:
        raise SourceFetchError("源内容为空（data 必须非空 bytes）")

    content_hash = sha256_hex(data)
    exists = await KnowledgeRaw.filter(
        project_id=project_id, source_id=source_id, content_hash=content_hash
    ).first()
    if exists is not None:
        return {"created": False, "id": str(exists.id), "content_hash": content_hash,
                "parsed": None, "raw": _raw_document(exists)}

    content: dict[str, Any] = {}
    content_text: Optional[str] = None
    parsed: Optional[dict[str, Any]] = None

    parser_name = resolve_adapter_type(adapter, adapter_type)
    if parser_name is not None:
        parser = _PARSERS[parser_name]
        content, content_text = parser(data)
        parsed = {"parser": parser_name}
    else:
        # B 轨：agent 通道，内容原样保存
        try:
            obj = json.loads(data.decode("utf-8"))
            content = obj if isinstance(obj, dict) else {"value": obj}
        except (UnicodeDecodeError, json.JSONDecodeError):
            content_text = data.decode("utf-8", errors="replace")

    row = KnowledgeRaw(
        project_id=project_id,
        source_id=source_id,
        kind=kind,
        adapter=adapter,
        adapter_type=adapter_type,
        locator=locator,
        title=title,
        content=content,
        content_text=content_text,
        media_type=adapter_type,
        byte_size=len(data),
        content_hash=content_hash,
        provenance=dict(provenance or {}),
        version=1,
        status="active",
    )
    try:
        await row.save()
    except IntegrityError:
        # 并发重拉同一份内容 → 唯一键兜底，返回已有行
        exists = await KnowledgeRaw.filter(
            project_id=project_id, source_id=source_id, content_hash=content_hash
        ).first()
        if exists is None:
            raise
        return {"created": False, "id": str(exists.id), "content_hash": content_hash,
                "parsed": None, "raw": _raw_document(exists)}

    return {"created": True, "id": str(row.id), "content_hash": content_hash,
            "parsed": parsed, "raw": _raw_document(row)}


def _raw_document(row: KnowledgeRaw) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "source_id": str(row.source_id) if row.source_id else None,
        "kind": row.kind,
        "adapter": row.adapter,
        "adapter_type": row.adapter_type,
        "locator": row.locator,
        "title": row.title,
        "media_type": row.media_type,
        "byte_size": row.byte_size,
        "content_hash": row.content_hash,
        "provenance": dict(row.provenance or {}),
        "version": row.version,
        "status": row.status,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "has_content_text": row.content_text is not None,
        "content_keys": sorted(row.content.keys()) if isinstance(row.content, dict) else [],
    }


# ── A-track file fetch: path → bytes → ingest ────────────────────────────

def _read_bytes(locator: str) -> bytes:
    path = Path(locator)
    if not path.is_file():
        raise SourceFetchError(f"源文件不存在：{locator}")
    return path.read_bytes()


async def fetch_file(
    *,
    project_id: Any,
    source_id: Any = None,
    kind: str = "product",
    adapter_type: str,
    path: str,
    title: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """A 轨文件源：本地路径 → bytes → 解析 → L1。URL/DB 源走 B 轨（agent 拉回后 ingest_raw）。"""
    data = _read_bytes(path)
    return await ingest_raw(
        project_id=project_id,
        source_id=source_id,
        kind=kind,
        adapter="builtin",
        adapter_type=adapter_type,
        data=data,
        locator=path,
        title=title or Path(path).name,
        provenance=provenance,
    )


__all__ = [
    "BUILTIN_ADAPTERS",
    "SourceFetchError",
    "fetch_file",
    "ingest_raw",
    "resolve_adapter_type",
    "sha256_hex",
]
