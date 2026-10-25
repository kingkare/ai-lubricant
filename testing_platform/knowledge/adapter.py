"""Graphify adapter — "run the tool → map to our schema → idempotent upsert".

This is the reference implementation of the architecture decision in
``docs/ai-testing-platform/plans/09-knowledge-system.md`` §4.7 ("tool as
extractor; we own storage") and the Graphify findings in §4.9:

* ``graphify extract <path> --code-only`` (subprocess, **not** an in-process
  import — the CLI's ``--code-only`` path is the verified one and needs no LLM
  key) produces ``graphify-out/graph.json``.
* ``parse`` maps Graphify's node/link vocabulary onto our anchor/item/edge
  schema (§15.1).
* ``upsert`` re-keys every node by **our** stable identity
  (§4.1 anchor identity ``(project_id, kind, path, symbol)``; items use
  ``(project_id, kind, key)``) so re-running never duplicates rows. Graphify's
  own ids embed line numbers (``file_rationale_116``) and therefore **drift**;
  they are stored only as a source fingerprint, never as identity.

Graphify relation vocabulary is kept verbatim (``calls`` / ``contains`` /
``imports`` / ``imports_from`` / ``references`` / ``rationale_for`` /
``method`` …): the design's edge enum (§6/§15.1) is explicitly a starting
point, and downstream impact queries treat ``relation`` as an open string.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from .graphify_runner import GraphifyRunner
from .models import KnowledgeAnchor, KnowledgeEdge, KnowledgeItem

NodeKind = Literal["anchor", "item"]

# Graphify ``file_type`` values we understand (§4.9).
_FT_CODE = "code"
_FT_RATIONALE = "rationale"
_FT_CONCEPT = "concept"

# Our item kinds (§15.1 enum + two Graphify-specific extensions).
_ITEM_RATIONALE = "rationale"  # docstring / NOTE: comment → why-this-code
_ITEM_DEPENDENCY = "dependency"  # external library import
_ITEM_COMPONENT = "component"  # unresolved/external symbol reference


# --------------------------------------------------------------------------
# Mapped value objects
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeIdentity:
    """Our stable identity for a graph node (§4.1) — independent of line numbers."""

    node_type: NodeKind
    kind: str
    path: str = ""
    symbol: str = ""
    key: str = ""

    @property
    def uid(self) -> str:
        """Batch-local string key; never persisted, never a Graphify id."""
        if self.node_type == "anchor":
            return f"anchor|{self.kind}|{self.path}|{self.symbol}"
        return f"item|{self.kind}|{self.key}"


@dataclass
class MappedNode:
    """One node mapped onto ``mc_knowledge_anchor`` / ``mc_knowledge_item``."""

    node_type: NodeKind
    kind: str
    path: str = ""
    symbol: str = ""
    key: str = ""
    label: str = ""
    title: str = ""
    content: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    provenance: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = None
    source_location: str | None = None
    graphify_id: str | None = None
    community: int | None = None
    owner: NodeIdentity | None = None  # rationale → owning code anchor

    @property
    def identity(self) -> NodeIdentity:
        return NodeIdentity(self.node_type, self.kind, self.path, self.symbol, self.key)


@dataclass
class MappedEdge:
    """One edge mapped onto ``mc_knowledge_edge``."""

    from_identity: NodeIdentity
    to_identity: NodeIdentity
    relation: str
    confidence: float = 1.0
    provenance: dict[str, Any] = field(default_factory=dict)
    source_file: str | None = None
    source_location: str | None = None


@dataclass
class ParsedGraph:
    """``parse()`` output: the two mapped collections plus counters."""

    nodes: list[MappedNode]
    edges: list[MappedEdge]
    dropped_edges: int = 0  # endpoints that never resolved to a batch node

    @property
    def anchor_count(self) -> int:
        return sum(1 for n in self.nodes if n.node_type == "anchor")

    @property
    def rationale_count(self) -> int:
        return sum(1 for n in self.nodes if n.kind == _ITEM_RATIONALE)


@dataclass
class ExtractionResult:
    """``run()`` output: raw tool payload + mapped graph + provenance."""

    project_path: str
    raw: dict[str, Any]
    graph: ParsedGraph
    version: str | None
    duration_s: float

    @property
    def nodes(self) -> list[MappedNode]:
        return self.graph.nodes

    @property
    def edges(self) -> list[MappedEdge]:
        return self.graph.edges


@dataclass
class UpsertStats:
    """Row-level outcome of a batch upsert (created/updated/unchanged)."""

    anchors_created: int = 0
    anchors_updated: int = 0
    anchors_unchanged: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_unchanged: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    edges_unchanged: int = 0
    edges_dropped: int = 0

    @property
    def created(self) -> int:
        return self.anchors_created + self.items_created + self.edges_created

    @property
    def updated(self) -> int:
        return self.anchors_updated + self.items_updated + self.edges_updated

    @property
    def unchanged(self) -> int:
        return self.anchors_unchanged + self.items_unchanged + self.edges_unchanged

    @property
    def is_idempotent_rerun(self) -> bool:
        """True when nothing new appeared and nothing needed touching."""
        return self.created == 0 and self.updated == 0


# --------------------------------------------------------------------------
# Label / location helpers
# --------------------------------------------------------------------------
def _symbol_of(label: str) -> str:
    """``.issue_session()`` → ``issue_session``; ``AuthService`` → ``AuthService``."""
    s = (label or "").strip()
    if s.startswith("."):
        s = s[1:]
    if s.endswith("()"):
        s = s[:-2]
    return s.strip()


def _line_of(location: str | None) -> int | None:
    """``"L9"`` → 9 (Graphify source_location syntax)."""
    if not location:
        return None
    s = str(location).strip().lstrip("L")
    return int(s) if s.isdigit() else None


def _confidence_of(edge: dict[str, Any]) -> float:
    score = edge.get("confidence_score")
    if isinstance(score, (int, float)):
        return float(score)
    label = (edge.get("confidence") or "EXTRACTED").upper()
    if label == "EXTRACTED":
        return 1.0
    if label == "INFERRED":
        return 0.7
    return 1.0  # AMBIGUOUS edges are dropped upstream by graphify itself


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------
class GraphifyAdapter:
    """``run`` → ``parse`` → ``upsert`` for Graphify-produced graphs.

    No LLM key is required anywhere: ``--code-only`` is pure AST extraction
    (§4.9 measured 4,835 nodes / 12,224 edges / 92% EXTRACTED on this repo).
    """

    def __init__(
        self,
        runner: GraphifyRunner | None = None,
        *,
        venv: str | None = None,
        extractor_version: str | None = None,
    ) -> None:
        self.runner = runner or GraphifyRunner(venv=venv)
        self.extractor_version = extractor_version

    # ---------------- run ----------------
    async def run(self, project_path: str | Path) -> ExtractionResult:
        """Shell out to ``graphify extract --code-only`` and map the output."""
        path = Path(project_path).resolve()
        t0 = time.monotonic()
        # Version probe (§12 #8: record extractor version so mismatches can be
        # detected later). Best effort — a failing probe must not block a run.
        try:
            self.extractor_version = await self.runner.version()
        except Exception:  # noqa: BLE001 — version is provenance, not a gate
            self.extractor_version = None
        raw = await self.runner.extract(path, code_only=True)
        graph = await self.parse(raw, project_path=path)
        return ExtractionResult(
            project_path=str(path),
            raw=raw,
            graph=graph,
            version=self.extractor_version,
            duration_s=round(time.monotonic() - t0, 3),
        )

    # ---------------- parse ----------------
    async def parse(
        self,
        raw: dict[str, Any],
        *,
        project_path: str | Path | None = None,
    ) -> ParsedGraph:
        """Map Graphify nodes/links onto our anchor/item/edge schema (§15.1)."""
        raw_nodes: list[dict[str, Any]] = list(raw.get("nodes") or [])
        raw_links: list[dict[str, Any]] = list(raw.get("links") or raw.get("edges") or [])

        # Index raw nodes and pre-compute two reverse maps from the link set:
        #   * ``method``  : class → method, for qualified symbols
        #                        (``.issue_session()`` → ``AuthService.issue_session``)
        #   * ``rationale_for``: rationale → owning code node, for item.key/owner
        by_id: dict[str, dict[str, Any]] = {
            n.get("id"): n for n in raw_nodes if n.get("id") is not None
        }
        method_parent: dict[str, str] = {}
        rationale_owner: dict[str, str] = {}
        for e in raw_links:
            src, tgt = e.get("source"), e.get("target")
            if e.get("relation") == "method" and src and tgt:
                method_parent[tgt] = src
            elif e.get("relation") == "rationale_for" and src and tgt:
                rationale_owner[src] = tgt

        # ---- pass 1: nodes ----
        nodes: list[MappedNode] = []
        id_to_uid: dict[str, str] = {}
        seen: dict[str, MappedNode] = {}
        rationale_seen: dict[str, int] = defaultdict(int)
        for rn in raw_nodes:
            node = self._map_node(
                rn,
                by_id=by_id,
                method_parent=method_parent,
                rationale_owner=rationale_owner,
            )
            if node is None:
                continue
            if node.node_type == "item" and node.kind == _ITEM_RATIONALE:
                # A file/anchor may carry several docstring/comment blocks — keep
                # them all by disambiguating the stable key with an ordinal
                # (``::2``), never with Graphify's line-numbered id.
                rationale_seen[node.key] += 1
                ordinal = rationale_seen[node.key]
                if ordinal > 1:
                    node.key = f"{node.key}::{ordinal}"
            uid = node.identity.uid
            if uid in seen:
                # Graphify can emit two nodes that collapse onto one identity
                # (same class.method in a file). Keep the first — our identity
                # is unique, so a second row would violate the constraint.
                if rn.get("id"):
                    id_to_uid[rn["id"]] = uid
                continue
            seen[uid] = node
            nodes.append(node)
            if rn.get("id"):
                id_to_uid[rn["id"]] = uid

        # ---- pass 2: content_hash over the referenced line ranges (§4.6.2) ----
        if project_path is not None:
            self._compute_content_hashes(nodes, Path(project_path))

        # ---- pass 3: edges ----
        edges: list[MappedEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        dropped = 0
        for rl in raw_links:
            mapped = self._map_edge(rl, id_to_uid, seen)
            if mapped is None:
                dropped += 1
                continue
            key = (mapped.relation, mapped.from_identity.uid, mapped.to_identity.uid)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(mapped)

        return ParsedGraph(nodes=nodes, edges=edges, dropped_edges=dropped)

    # ---------------- upsert ----------------
    async def upsert(
        self,
        project_id: str | uuid.UUID,
        nodes: Sequence[MappedNode],
        edges: Sequence[MappedEdge],
        *,
        version_id: str | uuid.UUID | None = None,
        repo_name: str | None = None,
    ) -> UpsertStats:
        """Idempotent batch write keyed on *our* identity, never Graphify's id.

        Order matters: anchors → items (so rationale items can bind their owner
        anchor) → edges (endpoints resolve through the uuid maps built above).
        Uses Tortoise (the platform's ``mc_*`` convention); the connection must
        already be initialized with :mod:`testing_platform.knowledge.models`.

        ``version_id``：图谱绑版本——知识表身份唯一性含 version_id，查询按版本
        过滤（旧版本图谱保留可查）。``repo_name``：多仓时给 anchor path 加
        ``{repo_name}/`` 前缀，防跨仓路径碰撞，同时记入 provenance。
        """
        from tortoise.transactions import in_transaction

        pid = uuid.UUID(str(project_id))
        vid: uuid.UUID | None = uuid.UUID(str(version_id)) if version_id else None
        stats = UpsertStats()
        uid_to_pk: dict[str, uuid.UUID] = {}

        if repo_name:
            # 多仓合并：anchor path（含边端点 identity）统一加 ``{repo_name}/`` 前缀，
            # 防跨仓同名路径碰撞。item 的 key 不动（其身份不带 path）。
            nodes, edges = _prefix_repo(nodes, edges, repo_name)

        async with in_transaction():
            # ---- anchors ----
            anchor_recs: list[MappedNode] = self._dedupe(
                n for n in nodes if n.node_type == "anchor"
            )
            anchor_ids = await self._upsert_anchor_batch(pid, anchor_recs, stats, version_id=vid)
            for rec in anchor_recs:
                if rec.identity.uid in anchor_ids:
                    uid_to_pk[rec.identity.uid] = anchor_ids[rec.identity.uid]

            # ---- items ----
            item_recs: list[MappedNode] = self._dedupe(
                n for n in nodes if n.node_type == "item"
            )
            item_ids = await self._upsert_item_batch(pid, item_recs, uid_to_pk, stats, version_id=vid)
            for rec in item_recs:
                if rec.identity.uid in item_ids:
                    uid_to_pk[rec.identity.uid] = item_ids[rec.identity.uid]

            # ---- edges ----
            resolved: list[tuple[MappedEdge, uuid.UUID, uuid.UUID]] = []
            for e in edges:
                src = uid_to_pk.get(e.from_identity.uid)
                dst = uid_to_pk.get(e.to_identity.uid)
                if src is None or dst is None:
                    stats.edges_dropped += 1  # endpoint never made it into the batch
                    continue
                resolved.append((e, src, dst))
            await self._upsert_edge_batch(pid, resolved, stats, version_id=vid)

        return stats

    # ---- parse helpers ----
    def _map_node(
        self,
        rn: dict[str, Any],
        *,
        by_id: dict[str, dict[str, Any]],
        method_parent: dict[str, str],
        rationale_owner: dict[str, str],
    ) -> MappedNode | None:
        gid = rn.get("id")
        file_type = rn.get("file_type")
        label = str(rn.get("label") or "")
        source_file = str(rn.get("source_file") or "")
        loc = rn.get("source_location") or None
        prov = self._provenance(rn)

        if file_type == _FT_RATIONALE:
            return self._map_rationale(
                rn, gid, label, source_file, loc, prov,
                rationale_owner, by_id, method_parent,
            )

        if file_type == _FT_CONCEPT or rn.get("type") == "external":
            return self._map_concept(rn, label, prov)

        if file_type == _FT_CODE:
            # A symbol without a source file is an unresolved/external
            # reference (its defining file was out of scan scope). Storing it
            # as an *anchor* would fake a path we don't actually know, so it
            # becomes an item of kind ``component``.
            if not source_file:
                return MappedNode(
                    node_type="item",
                    kind=_ITEM_COMPONENT,
                    path="",
                    symbol="",
                    key=f"component|{label}",
                    label=label,
                    title=label,
                    content={"name": label, "resolution": "unresolved"},
                    confidence=0.5,
                    provenance=prov,
                    graphify_id=gid,
                )
            kind, symbol = self._code_anchor_kind(rn, label, method_parent, by_id)
            if kind is None:
                return None
            return MappedNode(
                node_type="anchor",
                kind=kind,
                path=source_file,
                symbol=symbol,
                label=label,
                source_location=loc,
                provenance=prov,
                graphify_id=gid,
                community=rn.get("community"),
            )
        return None

    def _code_anchor_kind(
        self,
        rn: dict[str, Any],
        label: str,
        method_parent: dict[str, str],
        by_id: dict[str, dict[str, Any]],
    ) -> tuple[str, str]:
        """Classify a code node and build its (possibly qualified) symbol."""
        gid = rn.get("id")
        if rn.get("_callable_class"):
            return "class", _symbol_of(label)
        if rn.get("_callable"):
            name = _symbol_of(label)
            # Methods are qualified with their class so two same-named methods
            # in one file don't collapse into one identity.
            parent_gid = method_parent.get(gid)
            if parent_gid and parent_gid in by_id:
                parent = by_id[parent_gid].get("label") or ""
                cls = _symbol_of(str(parent))
                if cls:
                    return "function", f"{cls}.{name}"
            return "function", name
        # File/module node (label is e.g. ``crypto.py``): path alone identifies
        # it, so the symbol slot stays empty.
        return "file", ""

    def _map_rationale(
        self,
        rn: dict[str, Any],
        gid: str | None,
        label: str,
        source_file: str,
        loc: str | None,
        prov: dict[str, Any],
        rationale_owner: dict[str, str],
        by_id: dict[str, dict[str, Any]],
        method_parent: dict[str, str],
    ) -> MappedNode:
        """Docstring / ``NOTE:`` comment → ``knowledge_item(kind=rationale)``.

        The key is ``(path, owner_symbol[, ordinal])`` — deliberately **not**
        ``..._rationale_116``, because the Graphify id embeds the line number
        and would churn on every unrelated edit above it.
        """
        owner_gid = rationale_owner.get(str(gid)) if gid else None
        owner_node = by_id.get(owner_gid) if owner_gid else None
        owner_symbol = ""
        owner: NodeIdentity | None = None
        if owner_node is not None:
            kind, symbol = self._code_anchor_kind(
                owner_node, str(owner_node.get("label") or ""), method_parent, by_id
            )
            if kind:
                owner_symbol = symbol
                owner = NodeIdentity("anchor", kind, str(owner_node.get("source_file") or ""), symbol)
        # ``path::owner_symbol`` is unique in practice; ``_ord`` disambiguates
        # a file/anchor carrying several separate docstring/comment blocks.
        base = f"{source_file}::{owner_symbol}" if source_file else owner_symbol or "<module>"
        return MappedNode(
            node_type="item",
            kind=_ITEM_RATIONALE,
            path=source_file,
            symbol="",
            key=base,  # ordinal suffix applied later by _dedupe
            label=label,
            title=_title_of(label),
            content={
                "statement": label,
                "source_file": source_file,
                "source_location": loc,
                "owner_symbol": owner_symbol,
            },
            confidence=1.0,
            provenance=prov,
            source_location=loc,
            graphify_id=gid,
            owner=owner,
        )

    def _map_concept(self, rn: dict[str, Any], label: str, prov: dict[str, Any]) -> MappedNode:
        """``type=external`` / ``file_type=concept`` → ``knowledge_item(kind=dependency)``."""
        return MappedNode(
            node_type="item",
            kind=_ITEM_DEPENDENCY,
            path="",
            symbol="",
            key=f"dependency|{label}",
            label=label,
            title=label,
            content={"name": label, "external": True},
            confidence=0.7,
            provenance=prov,
            graphify_id=rn.get("id"),
        )

    def _map_edge(
        self,
        rl: dict[str, Any],
        id_to_uid: dict[str, str],
        seen: dict[str, MappedNode],
    ) -> MappedEdge | None:
        src_gid, tgt_gid = rl.get("source"), rl.get("target")
        relation = rl.get("relation")
        if not src_gid or not tgt_gid or not relation:
            return None
        src_uid = id_to_uid.get(src_gid)
        tgt_uid = id_to_uid.get(tgt_gid)
        if src_uid is None or tgt_uid is None:
            return None
        src_node, tgt_node = seen[src_uid], seen[tgt_uid]
        return MappedEdge(
            from_identity=src_node.identity,
            to_identity=tgt_node.identity,
            relation=str(relation),
            confidence=_confidence_of(rl),
            provenance={
                "extractor": "graphify",
                "extractor_version": self.extractor_version,
                "origin": rl.get("_origin"),
                "confidence_label": rl.get("confidence"),
                "context": rl.get("context"),
                "source_node_ids": [src_gid, tgt_gid],  # fingerprints only
            },
            source_file=rl.get("source_file") or None,
            source_location=rl.get("source_location") or None,
        )

    def _provenance(self, rn: dict[str, Any]) -> dict[str, Any]:
        return {
            "extractor": "graphify",
            "extractor_version": self.extractor_version,
            "mode": "code-only",
            "origin": rn.get("_origin"),
            "graphify_node_id": rn.get("id"),  # fingerprint, never identity
            "file_type": rn.get("file_type"),
        }

    # ---- content hash ----
    @staticmethod
    def _compute_content_hashes(nodes: Iterable[MappedNode], project_path: Path) -> None:
        """Hash each anchor's referenced line range (§4.6.2 drift signal).

        Approximates "the source slice owned by this symbol": from its start
        line up to the next symbol in the same file (EOF for the last one).
        File anchors hash the whole file. Missing files simply get ``None``.
        """
        anchors = [n for n in nodes if n.node_type == "anchor"]
        by_file: dict[str, list[MappedNode]] = defaultdict(list)
        for a in anchors:
            by_file[a.path].append(a)

        for path, group in by_file.items():
            full = project_path / path
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            if not lines:
                continue
            if not any(a.kind == "function" or a.kind == "class" for a in group):
                for a in group:  # file-only group → whole-file digest
                    a.content_hash = _sha256(text)
                continue
            starts = [( _line_of(a.source_location), a) for a in group]
            starts.sort(key=lambda p: (p[0] if p[0] is not None else 10**9))
            for i, (line, a) in enumerate(starts):
                if line is None:
                    a.content_hash = _sha256(text)
                    continue
                if a.kind == "file":
                    a.content_hash = _sha256(text)
                    continue
                end = line  # exclusive-ish upper bound
                if i + 1 < len(starts) and starts[i + 1][0] is not None:
                    end = starts[i + 1][0] - 1
                else:
                    end = len(lines)
                chunk = "\n".join(lines[max(0, line - 1) : max(line, end)])
                a.content_hash = _sha256(chunk)

    # ---- upsert helpers ----
    @staticmethod
    def _dedupe(nodes: Iterable[MappedNode]) -> list[MappedNode]:
        """Collapse duplicate identities inside one batch (keep the first)."""
        out: list[MappedNode] = []
        seen: set[str] = set()
        for n in nodes:
            uid = n.identity.uid
            if uid in seen:
                continue
            seen.add(uid)
            out.append(n)
        return out

    async def _upsert_anchor_batch(
        self,
        pid: uuid.UUID,
        records: Sequence[MappedNode],
        stats: UpsertStats,
        *,
        version_id: uuid.UUID | None = None,
    ) -> dict[str, uuid.UUID]:
        if not records:
            return {}
        existing = {
            (r.kind, r.path, r.symbol): r
            for r in await KnowledgeAnchor.filter(project_id=pid, version_id=version_id)
        }
        ids: dict[str, uuid.UUID] = {}
        fresh: list[KnowledgeAnchor] = []
        for n in records:
            ident = (n.kind, n.path, n.symbol)
            defaults = {
                "label": n.label,
                "source_location": n.source_location,
                "content_hash": n.content_hash,
                "community": n.community,
                "provenance": n.provenance,
                "source_node_id": n.graphify_id,
            }
            if ident in existing:
                row = existing[ident]
                ids[n.identity.uid] = row.id
                changed = {k: v for k, v in defaults.items() if getattr(row, k, None) != v}
                if changed:
                    await KnowledgeAnchor.filter(id=row.id).update(**changed)
                    stats.anchors_updated += 1
                else:
                    stats.anchors_unchanged += 1
            else:
                row = KnowledgeAnchor(
                    id=uuid.uuid4(), project_id=pid, version_id=version_id, kind=n.kind,
                    path=n.path, symbol=n.symbol, **defaults,
                )
                fresh.append(row)
                ids[n.identity.uid] = row.id
                stats.anchors_created += 1
        if fresh:
            await KnowledgeAnchor.bulk_create(fresh, ignore_conflicts=True)
        return ids

    async def _upsert_item_batch(
        self,
        pid: uuid.UUID,
        records: Sequence[MappedNode],
        uid_to_pk: dict[str, uuid.UUID],
        stats: UpsertStats,
        *,
        version_id: uuid.UUID | None = None,
    ) -> dict[str, uuid.UUID]:
        if not records:
            return {}
        existing = {
            (r.kind, r.key): r
            for r in await KnowledgeItem.filter(project_id=pid, version_id=version_id)
        }
        ids: dict[str, uuid.UUID] = {}
        fresh: list[KnowledgeItem] = []
        for n in records:
            ident = (n.kind, n.key)
            # §5.2 anchor gate: knowledge that could not be anchored to a code
            # entity degrades to ``candidate`` instead of ``confirmed``.
            anchor_pk = uid_to_pk.get(n.owner.uid) if n.owner else None
            status = "confirmed" if anchor_pk is not None else "candidate"
            defaults = {
                "title": n.title or n.label,
                "content": n.content,
                "status": status,
                "confidence": n.confidence,
                "provenance": n.provenance,
                "anchor_id": anchor_pk,
                "source_node_id": n.graphify_id,
            }
            if ident in existing:
                row = existing[ident]
                ids[n.identity.uid] = row.id
                changed = {k: v for k, v in defaults.items() if getattr(row, k, None) != v}
                if changed:
                    await KnowledgeItem.filter(id=row.id).update(**changed)
                    stats.items_updated += 1
                else:
                    stats.items_unchanged += 1
            else:
                row = KnowledgeItem(
                    id=uuid.uuid4(), project_id=pid, version_id=version_id,
                    kind=n.kind, key=n.key, **defaults
                )
                fresh.append(row)
                ids[n.identity.uid] = row.id
                stats.items_created += 1
        if fresh:
            await KnowledgeItem.bulk_create(fresh, ignore_conflicts=True)
        return ids

    async def _upsert_edge_batch(
        self,
        pid: uuid.UUID,
        records: Sequence[tuple[MappedEdge, uuid.UUID, uuid.UUID]],
        stats: UpsertStats,
        *,
        version_id: uuid.UUID | None = None,
    ) -> None:
        if not records:
            return
        existing = {
            (r.relation, r.from_kind, r.from_id, r.to_kind, r.to_id): r
            for r in await KnowledgeEdge.filter(project_id=pid, version_id=version_id)
        }
        fresh: list[KnowledgeEdge] = []
        for e, src, dst in records:
            ident = (e.relation, "anchor" if e.from_identity.node_type == "anchor" else "item",
                     src, "anchor" if e.to_identity.node_type == "anchor" else "item", dst)
            defaults = {
                "confidence": e.confidence,
                "provenance": e.provenance,
                "source_file": e.source_file,
                "source_location": e.source_location,
            }
            if ident in existing:
                row = existing[ident]
                changed = {k: v for k, v in defaults.items() if getattr(row, k, None) != v}
                if changed:
                    await KnowledgeEdge.filter(id=row.id).update(**changed)
                    stats.edges_updated += 1
                else:
                    stats.edges_unchanged += 1
            else:
                fresh.append(
                    KnowledgeEdge(
                        id=uuid.uuid4(), project_id=pid, version_id=version_id,
                        relation=e.relation,
                        from_kind=ident[1], from_id=src, to_kind=ident[3], to_id=dst,
                        **defaults,
                    )
                )
                stats.edges_created += 1
        if fresh:
            await KnowledgeEdge.bulk_create(fresh, ignore_conflicts=True)


# --------------------------------------------------------------------------
def _prefix_repo(
    nodes: Sequence[MappedNode], edges: Sequence[MappedEdge], repo_name: str
) -> tuple[list[MappedNode], list[MappedEdge]]:
    """多仓合并：给所有 anchor 的 ``path`` 加 ``{repo_name}/`` 前缀（node + edge 端点）。

    anchor 身份含 path，跨仓同名路径（如两个仓都有 ``src/main.py``）会撞唯一键，
    前缀把身份区分开。edge 端点的 identity 必须同步加，否则 ``uid`` 对不上。
    provenance 记 ``repo_name`` 以便溯源。
    """
    def _pfx_ident(ident: NodeIdentity) -> NodeIdentity:
        if ident.node_type != "anchor" or not ident.path or ident.path.startswith(f"{repo_name}/"):
            return ident
        return NodeIdentity(ident.node_type, ident.kind, f"{repo_name}/{ident.path}", ident.symbol, ident.key)

    new_nodes: list[MappedNode] = []
    for n in nodes:
        if n.node_type == "anchor" and n.path and not n.path.startswith(f"{repo_name}/"):
            prov = dict(n.provenance or {})
            prov["repo_name"] = repo_name
            new_nodes.append(replace(n, path=f"{repo_name}/{n.path}", provenance=prov))
        else:
            new_nodes.append(n)

    new_edges: list[MappedEdge] = []
    for e in edges:
        new_edges.append(
            replace(e, from_identity=_pfx_ident(e.from_identity), to_identity=_pfx_ident(e.to_identity))
        )
    return new_nodes, new_edges


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _title_of(label: str) -> str:
    """First sentence of a rationale block, capped for the title column."""
    line = " ".join(label.split())
    for stop in (". ", ".\n", "。"):
        cut = line.find(stop)
        if 0 < cut <= 200:
            return line[: cut + 1].strip()
    return line[:200]
