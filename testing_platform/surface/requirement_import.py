"""需求文档导入（multipart）：.docx → 富文本正文，.xmind → 思维导图树。

能力面（``surface/api.py``）原本是**纯 JSON**；本模块新增两条**收文件**的路由，
挂在 ``/api/v1/testing/requirement/import/{docx,xmind}``（main.py 无需改动——
父 router 已 include，子路由用相对前缀 include，模式同 ``chat.py`` / ``source_sync.py``）。

设计要点：

* **.docx 走 python-docx**（已在 venv，requirements.txt 补声明）——按**文档顺序**
  遍历段落与表格（``element.body.iterchildren()``），标题样式 → ``<h1..h6>``，
  表格 → 表格节点；产出 **Tiptap/ProseMirror 文档 JSON**（前端直接 ``setContent``），
  另附纯文本（供 description 投影 / 兜底）。
* **.xmind 复用** ``knowledge.source_fetch._parse_xmind``（已支持 XMind Zen 的
  ``content.json`` 与 XMind 8 的 ``content.xml``）——取其 topic 树转成前端
  ``{id,text,children}`` 形态（补 id）。
* **守卫**：后缀白名单 + 大小上限（20 MiB）；解析异常统一 422，与能力面错误口径一致。
"""
from __future__ import annotations

import io
import uuid
from typing import Any, Mapping, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile

router = APIRouter(prefix="/requirement", tags=["testing-requirement-import"])

#: 单文件大小上限（20 MiB）——docx/xmind 是文档，够大且防滥用。
MAX_IMPORT_BYTES = 20 * 1024 * 1024


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message})


async def _read_upload(file: UploadFile, exts: tuple[str, ...]) -> bytes:
    name = (file.filename or "").lower()
    if not name.endswith(exts):
        raise HTTPException(
            status_code=415,
            detail={"code": "unsupported_file", "message": f"仅支持 {'/'.join(exts)}：{file.filename!r}"},
        )
    data = await file.read()
    if not data:
        raise _bad_request("empty_file", "文件为空")
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "file_too_large", "message": f"文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MiB 上限"},
        )
    return data


# ── .docx → Tiptap 文档 + 纯文本 ────────────────────────────────

def docx_to_tiptap(data: bytes) -> tuple[dict, str, Optional[str]]:
    """解析 .docx → (Tiptap 文档, 纯文本, 文档标题?)。

    按文档顺序遍历 body 的 ``w:p`` / ``w:tbl``，标题样式映射到 heading 级别，
    行内 run 的 bold/italic/underline 转 marks。表格转 Tiptap table 节点。
    """
    try:
        from docx import Document  # python-docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover —— 依赖缺失时才走到
        raise _bad_request("docx_parser_missing", f"python-docx 未安装：{exc}") from exc

    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 —— 非法 docx / 加密 / 损坏
        raise _bad_request("docx_parse_failed", f"docx 解析失败：{exc}") from exc

    nodes: list[dict] = []
    text_lines: list[str] = []
    title: Optional[str] = None

    def para_to_node(paragraph: Any) -> Optional[dict]:
        text = paragraph.text or ""
        if not text.strip():
            return None
        style = (paragraph.style.name or "").lower() if paragraph.style else ""
        content = _runs_to_content(paragraph)
        if style.startswith("heading"):
            level = 1
            parts = style.split()
            if parts and parts[-1].isdigit():
                level = max(1, min(int(parts[-1]), 6))
            return {"type": "heading", "attrs": {"level": level}, "content": content}
        if style.startswith("title"):
            return {"type": "heading", "attrs": {"level": 1}, "content": content}
        return {"type": "paragraph", "content": content}

    for block in _iter_block_items(document, qn, Paragraph, Table):
        if isinstance(block, Paragraph):
            node = para_to_node(block)
            if node is None:
                continue
            if title is None and node["type"] == "heading" and node["attrs"]["level"] == 1:
                title = block.text.strip()
            nodes.append(node)
            text_lines.append(block.text)
        else:  # Table
            rows: list[dict] = []
            row_texts: list[str] = []
            for row in block.rows:
                cells = []
                for cell in row.cells:
                    cell_paras = [p.text for p in cell.paragraphs if p.text.strip()]
                    cells.append({
                        "type": "tableCell",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": t}]}
                                    for t in cell_paras] or [{"type": "paragraph"}],
                    })
                    row_texts.append(cell.text)
                rows.append({"type": "tableRow", "content": cells})
            nodes.append({"type": "table", "content": rows})
            text_lines.append(" | ".join(t for t in row_texts if t.strip()))

    doc = {"type": "doc", "content": nodes} if nodes else {"type": "doc", "content": [{"type": "paragraph"}]}
    return doc, "\n".join(t for t in text_lines if t.strip()), title


def _runs_to_content(paragraph: Any) -> list[dict]:
    """段落的行内内容：run → text 节点 + bold/italic/underline marks。"""
    out: list[dict] = []
    for run in paragraph.runs:
        if not run.text:
            continue
        marks: list[dict] = []
        if run.bold:
            marks.append({"type": "bold"})
        if run.italic:
            marks.append({"type": "italic"})
        if run.underline:
            marks.append({"type": "underline"})
        node: dict[str, Any] = {"type": "text", "text": run.text}
        if marks:
            node["marks"] = marks
        out.append(node)
    if not out:
        text = paragraph.text or ""
        if text:
            out.append({"type": "text", "text": text})
    return out


def _iter_block_items(document: Any, qn: Any, Paragraph: Any, Table: Any):
    """按文档顺序产出 Paragraph / Table（python-docx 的段落与表格是分开的列表）。"""
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


# ── .xmind → 思维导图树 {id,text,children} ──────────────────────

def xmind_to_tree(data: bytes) -> tuple[dict, str]:
    """解析 .xmind → ({root:{id,text,children}}, 纯文本)。复用 knowledge 的解析器。"""
    from ..knowledge.source_fetch import SourceFetchError, _parse_xmind

    try:
        parsed, text = _parse_xmind(data)
    except SourceFetchError as exc:
        raise _bad_request("xmind_parse_failed", str(exc)) from exc

    sheets = (parsed or {}).get("sheets") or []
    root_topic = (sheets[0].get("root") if sheets else None) or {}
    root = _topic_to_node(root_topic)
    if not root:
        raise _bad_request("xmind_empty", "xmind 无可用的思维导图节点")
    return root, (text or "")


def _topic_to_node(topic: Mapping[str, Any]) -> Optional[dict]:
    if not isinstance(topic, Mapping):
        return None
    children_in = (topic.get("children") or {}).get("attached") or []
    children = [n for n in (_topic_to_node(c) for c in children_in) if n]
    return {
        "id": uuid.uuid4().hex,
        "text": str(topic.get("title") or ""),
        "children": children,
    }


# ── 路由 ────────────────────────────────────────────────────────

@router.post("/import/docx")
async def import_docx(file: UploadFile = File(...)) -> dict:
    """上传 .docx → ``{content, text, title, name}``（content = Tiptap 文档 JSON）。"""
    data = await _read_upload(file, (".docx",))
    content, text, title = docx_to_tiptap(data)
    return {"content": content, "text": text, "title": title, "name": file.filename}


@router.post("/import/xmind")
async def import_xmind(file: UploadFile = File(...)) -> dict:
    """上传 .xmind → ``{root, text, name}``（root = 思维导图树 {id,text,children}）。"""
    data = await _read_upload(file, (".xmind",))
    root, text = xmind_to_tree(data)
    return {"root": root, "text": text, "name": file.filename}


__all__ = ["router", "docx_to_tiptap", "xmind_to_tree", "MAX_IMPORT_BYTES"]
