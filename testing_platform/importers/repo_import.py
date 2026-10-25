"""Repository interface import — routes (scanned) + OpenAPI specs → transports.

Bridges :mod:`route_scanner` and :mod:`openapi_importer` into one call: given a
materialized snapshot directory, scan declared HTTP routes across languages,
find + parse any OpenAPI spec files, and upsert everything into
``mc_test_transport`` (``project_id + code`` idempotent, same as the OpenAPI
importer).

Deterministic only — no LLM. The AI 补全 step (reading code to fill
``description``) is a separate, opt-in agent pass driven from the UI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from .openapi_importer import _endpoint_code, import_openapi, load_openapi_spec
from .route_scanner import Route, scan as scan_routes

#: Filenames recognised as OpenAPI specs (case-insensitive stem).
_SPEC_NAMES = {"openapi", "swagger", "api-docs", "apispec"}
_SPEC_EXTS = (".json", ".yaml", ".yml")

#: Directory depth cap when hunting for spec files (avoid walking node_modules).
_SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "target", "out",
    "__pycache__", ".venv", "venv", "site-packages",
}


def _find_spec_files(root: Path) -> list[Path]:
    """Locate OpenAPI spec files under ``root`` (best-effort, capped)."""
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        stem = path.stem.lower()
        if path.suffix.lower() in _SPEC_EXTS and (
            stem in _SPEC_NAMES or stem.endswith(".openapi") or "openapi" in stem or "swagger" in stem
        ):
            found.append(path)
        if len(found) >= 20:  # sane cap — a repo rarely has more
            break
    return found


async def _route_to_transport(project_id: Any, route: Route) -> Any:
    """Upsert one scanned route as a transport (idempotent by code)."""
    from ..assets.models import TestTransport
    from ..assets.service import create_transport

    code = _endpoint_code(route.method, route.path)
    existing: Optional[TestTransport] = None
    try:
        existing = await TestTransport.get(project_id=project_id, code=code)
    except Exception:  # noqa: BLE001 — not found → create
        existing = None

    summary = f"{route.method} {route.path}" + (f" · {route.handler}" if route.handler else "")
    if existing is not None:
        # Keep human/AI-set description; only fill when empty.
        if not existing.description:
            existing.description = summary
        if not existing.module and route.module:
            existing.module = route.module
        await existing.save()
        return existing

    return await create_transport(
        project_id=project_id,
        code=code,
        kind="http",
        description=summary,
        module=route.module or None,
        status="draft",
    )


async def import_from_snapshot(
    snapshot_dir: str | Path,
    project_id: Any,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan a snapshot → routes + OpenAPI specs → upsert transports.

    Returns ``{"routes", "openapi_files", "openapi_endpoints", "transports",
    "dry_run", "scanned": [{method, path, framework, file, line, handler}]}``.
    """
    root = Path(snapshot_dir)
    routes = scan_routes(root)
    spec_files = _find_spec_files(root)

    scanned = [
        {
            "method": r.method, "path": r.path, "framework": r.framework,
            "file": r.file, "line": r.line, "handler": r.handler, "module": r.module,
        }
        for r in routes
    ]

    if dry_run:
        openapi_count = 0
        for spec in spec_files:
            try:
                doc = load_openapi_spec(spec.read_text(encoding="utf-8", errors="replace"))
                openapi_count += sum(
                    1
                    for _p, item in (doc.get("paths") or {}).items()
                    for m in (item.keys() if isinstance(item, dict) else [])
                    if str(m).lower() in ("get", "post", "put", "patch", "delete", "options", "head", "trace")
                )
            except Exception:  # noqa: BLE001 — a bad spec shouldn't kill the dry-run
                continue
        return {
            "routes": len(routes),
            "openapi_files": len(spec_files),
            "openapi_endpoints": openapi_count,
            "transports": [],
            "dry_run": True,
            "scanned": scanned,
        }

    transports: list[Any] = []
    for route in routes:
        transports.append(await _route_to_transport(project_id, route))

    openapi_endpoints = 0
    for spec in spec_files:
        try:
            text = spec.read_text(encoding="utf-8", errors="replace")
            rows = await import_openapi(text, project_id)
            transports.extend(rows)
            openapi_endpoints += len(rows)
        except Exception:  # noqa: BLE001 — skip an unparseable spec, keep the rest
            continue

    # De-dupe by code (a route and a spec may describe the same endpoint).
    by_code: dict[str, Any] = {}
    for row in transports:
        by_code[row.code] = row

    return {
        "routes": len(routes),
        "openapi_files": len(spec_files),
        "openapi_endpoints": openapi_endpoints,
        "transports": list(by_code.values()),
        "dry_run": False,
        "scanned": scanned,
    }


__all__ = ["import_from_snapshot"]
