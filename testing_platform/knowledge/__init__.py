"""Knowledge ingestion layer (``09-knowledge-system.md`` §4.7 "tool as extractor").

Architecture: a third-party extractor (Graphify) is a *stateless producer*;
we own mapping + idempotent storage + unified queries. Each extractor gets one
adapter (``run() -> parse() -> upsert()``); downstream consumers only ever
read our tables (``mc_knowledge_*``), never call the tools.

Public surface::

    from testing_platform.knowledge import GraphifyAdapter, GraphifyRunner
    result = await GraphifyAdapter().run(project_path)
    stats   = await adapter.upsert(project_id, result.nodes, result.edges)
"""
from __future__ import annotations

from .adapter import (
    ExtractionResult,
    GraphifyAdapter,
    MappedEdge,
    MappedNode,
    NodeIdentity,
    ParsedGraph,
    UpsertStats,
)
from ._version_indexes import INDEX_DDL, ensure_version_indexes
from .case_scan import CaseScanError, extract_cases, scan_cases
from .agent_ingest import (
    AGENT_CONFIDENCE_CAP,
    KnowledgePutError,
    check_testability,
    put_items,
)
from .graphify_runner import GraphifyNotInstalled, GraphifyRunner
from .models import (
    DDL,
    KNOWLEDGE_SOURCE_KINDS,
    KNOWLEDGE_SOURCE_STATUSES,
    KNOWLEDGE_SOURCE_TYPES,
    KNOWLEDGE_SYNC_POLICIES,
    KnowledgeAnchor,
    KnowledgeEdge,
    KnowledgeItem,
    KnowledgeRaw,
    KnowledgeSource,
    TestProfile,
    create_tables_via_pg,
)
from .service import create_source, get_source, list_anchors, list_items, list_sources
from .source_fetch import (
    BUILTIN_ADAPTERS,
    SourceFetchError,
    fetch_file,
    ingest_raw,
    resolve_adapter_type,
)

__all__ = [
    "AGENT_CONFIDENCE_CAP",
    "BUILTIN_ADAPTERS",
    "CaseScanError",
    "DDL",
    "ExtractionResult",
    "GraphifyAdapter",
    "GraphifyNotInstalled",
    "GraphifyRunner",
    "KNOWLEDGE_SOURCE_KINDS",
    "KNOWLEDGE_SOURCE_STATUSES",
    "KNOWLEDGE_SOURCE_TYPES",
    "KNOWLEDGE_SYNC_POLICIES",
    "KnowledgeAnchor",
    "KnowledgeEdge",
    "KnowledgeItem",
    "KnowledgePutError",
    "KnowledgeRaw",
    "KnowledgeSource",
    "MappedEdge",
    "MappedNode",
    "NodeIdentity",
    "ParsedGraph",
    "SourceFetchError",
    "TestProfile",
    "UpsertStats",
    "check_testability",
    "create_source",
    "create_tables_via_pg",
    "extract_cases",
    "fetch_file",
    "get_source",
    "ingest_raw",
    "list_anchors",
    "list_items",
    "list_sources",
    "put_items",
    "resolve_adapter_type",
    "scan_cases",
]
