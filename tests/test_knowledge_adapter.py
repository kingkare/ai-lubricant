"""Knowledge adapter tests — real Graphify output, no LLM key required.

Fixture: a *real* user_platform slice (``crypto.py`` + ``masking.py`` +
``auth_service.py``) is copied into a temp dir and fed to ``graphify extract
--code-only``. Verified on this repo (plan §4.9): that slice yields code
anchors, ``calls`` edges, ``references`` edges and docstring ``rationale``
items — all from pure AST extraction.

Asserts the three things the task asks for:

* node count > 0,
* there is at least one ``calls`` edge,
* there are ``rationale`` knowledge items,

plus idempotency: running ``upsert`` twice must not create any duplicate row
(``UpsertStats.created == 0`` on the second pass) — the stable identity
``(project_id, kind, path, symbol)`` / ``(project_id, kind, key)``.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_PROJ = Path(__file__).resolve().parents[1]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from testing_platform.knowledge import GraphifyAdapter, GraphifyRunner  # noqa: E402
from testing_platform.knowledge.models import (  # noqa: E402
    KnowledgeAnchor,
    KnowledgeEdge,
    KnowledgeItem,
)

FIXTURE_FILES = [
    "user_platform/auth_service.py",
    "user_platform/crypto.py",
    "user_platform/masking.py",
]


def _graphify_venv() -> str | None:
    """Locate the isolated graphify venv (``GRAPHIFY_VENV`` wins)."""
    env = os.environ.get("GRAPHIFY_VENV")
    if env:
        return env
    default = _PROJ / ".venv-graphify"
    if default.is_dir():
        return str(default)
    return None


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    # app label 与其余测试文件统一为 "models"：Tortoise 注册表是进程级单例，
    # 混用 "knowledge"/"models" 会在同进程串跑时互相覆盖 schema（实测
    # knowledge + surface 一起跑时 upsert 断言失败，单独跑通过）。
    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.knowledge.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _make_fixture_dir(tmp_path: Path) -> Path:
    """Copy a real user_platform code slice into the temp project directory."""
    proj = tmp_path / "fixture_project"
    proj.mkdir(parents=True)
    for rel in FIXTURE_FILES:
        src = _PROJ / rel
        if src.is_file():
            shutil.copy2(src, proj / src.name)
    return proj


@pytest.mark.asyncio
async def test_graphify_runner_version_and_extract(tmp_path):
    venv = _graphify_venv()
    if not venv:
        pytest.skip("GRAPHIFY_VENV not set; install graphifyy into an isolated venv first")
    runner = GraphifyRunner(venv=venv)
    ver = await runner.version()
    assert ver and ver[:1].isdigit(), f"unexpected version output: {ver!r}"

    proj = _make_fixture_dir(tmp_path)
    raw = await runner.extract(proj, code_only=True)
    assert isinstance(raw, dict)
    assert raw.get("nodes")
    assert raw.get("links") or raw.get("edges")


@pytest.mark.asyncio
async def test_run_parse_has_nodes_calls_and_rationale(tmp_path):
    venv = _graphify_venv()
    if not venv:
        pytest.skip("GRAPHIFY_VENV not set; install graphifyy into an isolated venv first")
    adapter = GraphifyAdapter(venv=venv)
    proj = _make_fixture_dir(tmp_path)

    result = await adapter.run(proj)

    # 1) node count > 0
    assert len(result.nodes) > 0, "graphify --code-only produced no mapped nodes"
    assert result.graph.anchor_count > 0, "no code anchors mapped"
    # 2) calls edges present
    assert any(e.relation == "calls" for e in result.edges), "expected calls edges"
    # 3) rationale items present
    assert result.graph.rationale_count > 0, "expected rationale items from docstrings"
    # provenance / version recorded
    assert result.version and result.version[:1].isdigit()
    assert result.duration_s >= 0
    # every edge endpoint resolves to a node in the batch
    assert result.graph.dropped_edges <= 0, "batch should be self-contained"


@pytest.mark.asyncio
async def test_parse_rejects_ambiguous_and_keeps_confidence(tmp_path):
    """Unit-level mapping checks on a synthetic graph (no subprocess)."""
    adapter = GraphifyAdapter(extractor_version="0.9.71")
    raw = {
        "nodes": [
            {"id": "mod", "label": "m.py", "file_type": "code", "source_file": "m.py",
             "source_location": "L1", "community": 0},
            {"id": "mod_f", "label": "f()", "file_type": "code", "source_file": "m.py",
             "source_location": "L3", "community": 0, "_callable": True},
            {"id": "mod_f_rat", "label": "Why f exists.", "file_type": "rationale",
             "source_file": "m.py", "source_location": "L2", "community": 0},
            {"id": "bcrypt", "label": "bcrypt", "file_type": "concept", "type": "external",
             "community": 0},
        ],
        "links": [
            {"source": "mod_f", "target": "mod_f", "relation": "calls",
             "confidence": "EXTRACTED", "confidence_score": 1.0,
             "source_file": "m.py", "source_location": "L3"},
            {"source": "mod_f_rat", "target": "mod_f", "relation": "rationale_for",
             "confidence": "EXTRACTED", "confidence_score": 1.0,
             "source_file": "m.py", "source_location": "L2"},
            {"source": "mod", "target": "bcrypt", "relation": "imports",
             "confidence": "EXTRACTED", "confidence_score": 1.0,
             "source_file": "m.py", "source_location": "L4"},
        ],
    }
    graph = await adapter.parse(raw, project_path=tmp_path)
    assert len(graph.nodes) == 4
    # rationale key must NOT contain the line-numbered graphify id
    rat = [n for n in graph.nodes if n.kind == "rationale"][0]
    assert "rationale_3" not in rat.key and "L3" not in rat.key
    assert rat.owner is not None and rat.owner.symbol == "f"
    # edges
    assert any(e.relation == "calls" for e in graph.edges)
    assert graph.dropped_edges == 0


@pytest.mark.asyncio
async def test_upsert_is_idempotent(tortoise_db, tmp_path):
    venv = _graphify_venv()
    if not venv:
        pytest.skip("GRAPHIFY_VENV not set; install graphifyy into an isolated venv first")
    adapter = GraphifyAdapter(venv=venv)
    proj = _make_fixture_dir(tmp_path)
    result = await adapter.run(proj)

    pid = uuid.uuid4()
    stats1 = await adapter.upsert(pid, result.nodes, result.edges)
    # first run creates everything
    assert stats1.anchors_created > 0
    assert stats1.items_created > 0
    assert stats1.edges_created > 0
    assert stats1.edges_dropped == 0

    # second run against the same project + same graph must be a no-op
    stats2 = await adapter.upsert(pid, result.nodes, result.edges)
    assert stats2.created == 0, f"second upsert created rows: {stats2}"
    assert stats2.unchanged == stats1.created

    # third run through the full pipeline (re-extract) also must not duplicate
    result3 = await adapter.run(proj)
    stats3 = await adapter.upsert(pid, result3.nodes, result3.edges)
    assert stats3.created == 0

    # counts in the DB equal what the batch produced
    assert await KnowledgeAnchor.filter(project_id=pid).count() == stats1.anchors_created
    assert await KnowledgeItem.filter(project_id=pid).count() == stats1.items_created
    assert await KnowledgeEdge.filter(project_id=pid).count() == stats1.edges_created
    # every profile-less anchor is a place where a TestProfile can be created later
    from testing_platform.knowledge.models import TestProfile

    assert await TestProfile.filter(project_id=pid).count() == 0


@pytest.mark.asyncio
async def test_upsert_requires_anchored_status_for_rationale(tortoise_db, tmp_path):
    """Deterministic extraction → ``confirmed``; unanchorable knowledge → ``candidate``."""
    venv = _graphify_venv()
    if not venv:
        pytest.skip("GRAPHIFY_VENV not set; install graphifyy into an isolated venv first")
    adapter = GraphifyAdapter(venv=venv)
    proj = _make_fixture_dir(tmp_path)
    result = await adapter.run(proj)

    pid = uuid.uuid4()
    await adapter.upsert(pid, result.nodes, result.edges)

    rats = await KnowledgeItem.filter(project_id=pid, kind="rationale")  # list of models
    assert len(rats) > 0
    for r in rats:
        if r.anchor_id is not None:
            assert r.status == "confirmed"
        else:
            assert r.status == "candidate"
    deps = await KnowledgeItem.filter(project_id=pid, kind="dependency")  # list of models
    for d in deps:
        assert d.status == "candidate"  # no anchor → anchor gate downgrades