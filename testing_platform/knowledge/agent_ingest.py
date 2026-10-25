"""Agent-track ingestion landing — write agent-parsed knowledge into L2.

The B track contract (``test-project-definition.md`` §八 / plans/09 §6):

```
source.fetch (取内容)  →  agent 解析  →  knowledge.put (本模块)
```

The platform **never parses** B-track formats itself; but whatever an agent
extracts must still pass the **same gates** a deterministic extraction would:

* **锚点闸 (anchor gate)** — an item that claims to be about a piece of code
  must resolve its anchor against ``mc_knowledge_anchor``. Unresolved anchors
  do **not** block the write; they demote the item to ``candidate`` and are
  reported back, so nothing silently "floats" as if it were grounded.
* **可测闸 (testability gate)** — a knowledge item is only worth keeping if a
  test could act on it: ``rule`` needs a condition+expected, ``requirement``
  needs acceptance, ``api_endpoint`` needs a shape. Items failing it are stored
  as ``candidate`` with the reason recorded.

**AI output is ``candidate`` by default** — promotion to ``confirmed`` is a
human call (design 八「AI 不裁判」). This module only ever writes candidate rows
for agent provenance; deterministic extractors go through
:meth:`GraphifyAdapter.upsert`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from uuid import UUID as _UUID

from .models import KnowledgeAnchor, KnowledgeEdge, KnowledgeItem

#: agent 产出的默认置信度上限（AI 不得自封 1.0）
AGENT_CONFIDENCE_CAP = 0.95

#: 各 kind 的「可测」判据：content 里必须齐哪些字段
_TESTABILITY: dict[str, tuple[str, ...]] = {
    "rule": ("condition", "expected"),
    "requirement": ("acceptance",),
    "api_endpoint": ("method", "path"),
    "db_table": ("name", "columns"),
    "config_key": ("key",),
    "bug_pattern": ("symptom",),
}

#: 单条目最多建多少条边（防 agent 一次灌爆图谱）
_MAX_LINKS_PER_ITEM = 50


class KnowledgePutError(ValueError):
    """agent 产出落库失败（结构非法，连 candidate 都当不了）。"""


def _as_uuid(value: Any, *, field: str) -> _UUID:
    if isinstance(value, _UUID):
        return value
    try:
        return _UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise KnowledgePutError(f"{field} 不是合法 UUID：{value!r}") from exc


def check_testability(kind: str, content: Mapping[str, Any]) -> tuple[bool, Optional[str]]:
    """可测闸：这条知识**能不能被测试作用**。缺判据 → 不通过 + 原因。"""
    required = _TESTABILITY.get(str(kind))
    if not required:
        # 无判据的类型（doc_ref/glossary/concept…）不强制可测
        return True, None
    missing = [f for f in required if not _present(content.get(f))]
    if missing:
        return False, f"kind={kind} 缺可测判据字段：{missing}"
    return True, None


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple)):
        return bool(value)
    return True


def _clamp_confidence(value: Any) -> float:
    """AI 置信度钳到 (0, 0.95]——**框架裁判**，不接受 agent 自封的 1.0。"""
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.7
    if conf <= 0:
        return 0.5
    return min(conf, AGENT_CONFIDENCE_CAP)


async def _resolve_anchor(
    project_id: _UUID, anchor_ref: Mapping[str, Any]
) -> Optional[KnowledgeAnchor]:
    """锚点闸：按 (kind?, path, symbol) 在 mc_knowledge_anchor 里定位。"""
    path = str(anchor_ref.get("path") or "").strip()
    if not path:
        return None
    symbol = str(anchor_ref.get("symbol") or "").strip()
    qs = KnowledgeAnchor.filter(project_id=project_id, path=path)
    kind = anchor_ref.get("kind")
    if kind:
        qs = qs.filter(kind=str(kind))
    if symbol:
        qs = qs.filter(symbol=symbol)
    return await qs.first()


async def put_items(
    *,
    project_id: Any,
    items: Sequence[Mapping[str, Any]],
    source_id: Any = None,
    provenance: Optional[Mapping[str, Any]] = None,
    agent: Optional[str] = None,
) -> dict[str, Any]:
    """把 agent 解析出的知识条目写进 L2（过锚点闸 + 可测闸）。

    每个 item：``{kind, key, title?, content, confidence?, anchors?, links?}``

    * 幂等键 ``(project_id, kind, key)`` —— 重跑更新而非重复。
    * status 恒为 ``candidate``（AI 产出需人确认才升 confirmed）。
    * ``anchors: [{path, symbol?, kind?}]`` 解析不到 → 记进
      ``content["_gate"]["unresolved_anchors"]`` 并回传。
    * ``links: [{to_kind, to_key, relation}]`` → 建 ``mc_knowledge_edge``。
    """
    if not items:
        raise KnowledgePutError("items 不能为空")

    pid = _as_uuid(project_id, field="project_id")
    sid = _as_uuid(source_id, field="source_id") if source_id else None
    base_prov = dict(provenance or {})
    base_prov.setdefault("extractor", agent or "agent")
    base_prov.setdefault("track", "agent")

    created = updated = unchanged = 0
    gated: list[dict[str, Any]] = []
    docs: list[dict[str, Any]] = []

    for raw in items:
        if not isinstance(raw, Mapping):
            raise KnowledgePutError(f"item 必须是对象，收到 {type(raw).__name__}")
        kind = str(raw.get("kind") or "").strip()
        key = str(raw.get("key") or "").strip()
        if not kind or not key:
            raise KnowledgePutError(f"item 缺 kind/key：{raw!r}")
        content = dict(raw.get("content") or {})
        confidence = _clamp_confidence(raw.get("confidence"))

        # ── 可测闸 ──
        testable, test_reason = check_testability(kind, content)
        # ── 锚点闸 ──
        anchor_refs: list[Mapping[str, Any]] = [
            a for a in (raw.get("anchors") or []) if isinstance(a, Mapping)
        ]
        resolved: list[KnowledgeAnchor] = []
        unresolved: list[dict[str, Any]] = []
        for ref in anchor_refs:
            hit = await _resolve_anchor(pid, ref)
            if hit is None:
                unresolved.append({
                    "path": ref.get("path"), "symbol": ref.get("symbol"),
                    "kind": ref.get("kind"),
                })
            else:
                resolved.append(hit)

        gate: dict[str, Any] = {}
        if not testable:
            gate["testability"] = test_reason
        if unresolved:
            gate["unresolved_anchors"] = unresolved
        if gate:
            content["_gate"] = gate
            gated.append({"kind": kind, "key": key, **gate})

        prov = dict(base_prov)
        prov["confidence_declared"] = raw.get("confidence")
        prov["agent"] = agent or base_prov.get("extractor")

        defaults = {
            "title": raw.get("title"),
            "content": content,
            "status": "candidate",  # AI 产出一律 candidate，人确认才升
            "confidence": confidence,
            "provenance": prov,
            "source_id": sid,
            "anchor_id": resolved[0].id if resolved else None,
        }
        existing = await KnowledgeItem.filter(project_id=pid, kind=kind, key=key).first()
        if existing is None:
            row = await KnowledgeItem.create(
                project_id=pid, kind=kind, key=key, **defaults
            )
            created += 1
            action = "created"
        else:
            changed = {k: v for k, v in defaults.items() if getattr(existing, k, None) != v}
            if changed:
                # 版本只增不覆盖：内容变了才 +1
                await KnowledgeItem.filter(id=existing.id).update(version=existing.version + 1, **changed)
                for k, v in changed.items():
                    setattr(existing, k, v)
                existing.version += 1
                updated += 1
                action = "updated"
            else:
                unchanged += 1
                action = "unchanged"

        edge_count = 0  # links 延后到全部 item 落库后再建（同批内可互相引用）
        docs.append({
            "id": str((row if existing is None else existing).id),
            "kind": kind,
            "key": key,
            "status": "candidate",
            "confidence": confidence,
            "action": action,
            "anchors_resolved": [str(a.id) for a in resolved],
            "anchors_unresolved": unresolved,
            "testable": testable,
            "_links": list(raw.get("links") or []),
            "_row": (row if existing is None else existing),
            "_anchors": resolved,
        })

    # ── 第二遍：建边（此时同批 item 都已存在，item→item 引用可解析）──
    for doc in docs:
        doc["edges"] = await _put_links(pid, doc.pop("_row"), doc.pop("_links"))
        # 锚点闸通过 → 自动建 item→anchor 的 grounded 边（覆盖矩阵的边）
        doc["anchor_edges"] = await _put_anchor_edges(pid, doc["id"], doc.pop("_anchors"))

    return {
        "project_id": str(pid),
        "items": docs,
        "count": len(docs),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "gated": gated,
        "note": "agent 产出落库为 candidate；升 confirmed 需人工确认（AI 不裁判）",
    }


async def _put_anchor_edges(pid: _UUID, item_id: str, anchors: Sequence[KnowledgeAnchor]) -> int:
    """锚点闸通过 → 自动建 ``item --grounded_in--> anchor`` 边。

    这是**覆盖矩阵的边**：条目声称"关于哪段代码"，解析成功就落成图边，
    下游（release.impact / knowledge.profile / 覆盖缺口）直接读边，不再逐条查。
    """
    made = 0
    for anchor in anchors:
        await KnowledgeEdge.get_or_create(
            project_id=pid, relation="grounded_in",
            from_kind="item", from_id=_as_uuid(item_id, field="item_id"),
            to_kind="anchor", to_id=anchor.id,
        )
        made += 1
    return made

    return {
        "project_id": str(pid),
        "items": docs,
        "count": len(docs),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "gated": gated,
        "note": "agent 产出落库为 candidate；升 confirmed 需人工确认（AI 不裁判）",
    }


async def _put_links(
    pid: _UUID, from_row: KnowledgeItem, links: Sequence[Any]
) -> int:
    """建 item→item / item→anchor 边（relation 白名单外也收，字符串存）。

    目标解析规则：
    * ``to_kind="anchor"`` → 用 ``(path, symbol)`` 定位锚点（**不需要 to_key**）
    * 其余 → 用 ``(to_kind, to_key)`` 定位 item
    解析不到的目标**静默跳过**（不阻断本条 item 的落库，但少一条边）。
    """
    if not links:
        return 0
    made = 0
    for link in links[:_MAX_LINKS_PER_ITEM]:
        if not isinstance(link, Mapping):
            continue
        to_kind = str(link.get("to_kind") or "").strip()
        relation = str(link.get("relation") or "references").strip()
        if not to_kind:
            continue
        if to_kind == "anchor":
            anchor = await KnowledgeAnchor.filter(
                project_id=pid, path=str(link.get("path") or ""),
                symbol=str(link.get("symbol") or ""),
            ).first()
            if anchor is None:
                continue
            to_id, to_kind_val = anchor.id, "anchor"
        else:
            to_key = str(link.get("to_key") or "").strip()
            if not to_key:
                continue
            to_item = await KnowledgeItem.filter(project_id=pid, kind=to_kind, key=to_key).first()
            if to_item is None:
                continue
            to_id, to_kind_val = to_item.id, "item"
        await KnowledgeEdge.get_or_create(
            project_id=pid, relation=relation,
            from_kind="item", from_id=from_row.id,
            to_kind=to_kind_val, to_id=to_id,
        )
        made += 1
    return made


__all__ = [
    "AGENT_CONFIDENCE_CAP",
    "KnowledgePutError",
    "check_testability",
    "put_items",
]
