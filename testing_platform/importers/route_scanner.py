"""Route scanner — pull HTTP endpoints out of a code snapshot (multi-language).

The test project **owns no code**; this layer walks a materialized snapshot
(``projects/git_source.init_snapshot``) and extracts declared HTTP routes so the
interface library (``mc_test_transport``) can be seeded from a repository — the
same destination as :mod:`testing_platform.importers.openapi_importer`, but for
projects whose routes live in code rather than an OpenAPI spec.

Scope (user decision): **go / java / python / js / ts**. Parsing is deliberately
heuristic — a regex/light-scan over source text, in the same "no third-party
parser" spirit as ``openapi_importer``. It is a **best-effort seed**: dynamic
registration (e.g. routes added in a loop) is not seen, and every scanned route
is expected to be human-confirmed before use.

What we recognise:

* **Python** — FastAPI/Starlette ``@app.get("/x")`` / ``@router.post(...)``;
  Flask ``@app.route("/x", methods=["GET", "POST"])``; Django ``path("x/", view)``
  / ``re_path(r"^x/$", view)``.
* **JS/TS** — Express ``router.get("/x", ...)`` / ``app.post``; NestJS decorators
  ``@Get("/x")`` / ``@Post`` / ``@Controller("/base")`` (class prefix applied).
* **Go** — gin/echo/chi/net-http: ``r.GET("/x", ...)``, ``group.POST``,
  ``e.GET``, ``r.Get``, ``http.HandleFunc("/x", ...)``.
* **Java** — Spring ``@GetMapping("/x")`` / ``@PostMapping`` /
  ``@RequestMapping(value="/x", method=RequestMethod.GET)`` with class-level
  ``@RequestMapping("/base")`` prefix.

Output is a flat list of :class:`Route` — one per ``(method, path)``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

#: File extensions we scan, by language.
_LANG_EXT: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "js": (".js", ".jsx", ".mjs", ".cjs"),
    "ts": (".ts", ".tsx"),
    "go": (".go",),
    "java": (".java",),
}

#: Directories never worth scanning (huge, generated, vendored).
_SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "target", "out",
    "__pycache__", ".venv", "venv", ".idea", ".gradle", "site-packages",
}

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")


@dataclass(frozen=True)
class Route:
    """One declared HTTP route found in source."""

    method: str  # uppercase: GET/POST/...
    path: str  # normalized, leading slash, ``{id}``/``:id`` → ``{id}``
    framework: str  # fastapi|flask|django|express|nestjs|gin|echo|chi|nethttp|spring
    file: str  # repo-relative, forward slashes
    line: int  # 1-based
    handler: str = ""  # handler / controller function name (best-effort)
    module: str = ""  # class/controller name or URL first segment (grouping)


# ── path normalization ──────────────────────────────────────────────────────

def _norm_path(raw: str) -> str:
    """Normalize a route path: strip quotes, leading slash, convert params.

    ``:id`` (Express/gin) and ``<id>`` (Flask) and ``{id}`` (OpenAPI) all
    collapse to ``{id}``; ``*`` / ``**`` wildcards collapse to ``{wildcard}``.
    """
    p = str(raw or "").strip().strip('"').strip("'").strip()
    # Drop Django regex anchors if a regex slipped through.
    p = p.replace("^", "").replace("$", "")
    p = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", p)  # Flask <int:id> → {id}
    p = re.sub(r":(\w+)", r"{\1}", p)  # Express/gin :id → {id}
    p = re.sub(r"\*+(\w*)", lambda m: "{%s}" % (m.group(1) or "wildcard"), p)
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"/{2,}", "/", p)  # collapse duplicate slashes
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def _module_of(path: str) -> str:
    """Grouping key: first meaningful URL segment (``/api/orders`` → ``api``)."""
    seg = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    if not seg:
        return "root"
    # ``/api/v1/orders`` → skip the version-ish noise, prefer a real word.
    for s in seg:
        if s.lower() not in ("api", "v1", "v2", "v3", "rest"):
            return s
    return seg[0]


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


# ── Python ──────────────────────────────────────────────────────────────────

_PY_DECORATOR = re.compile(
    r"@(?P<obj>\w+)\.(?P<m>get|post|put|patch|delete|options|head|trace)"
    r"\(\s*(?:path\s*=\s*)?[\"'](?P<path>[^\"']*)[\"']",
    re.IGNORECASE,
)
_PY_ROUTE = re.compile(
    r"@(?P<obj>\w+)\.route\(\s*[\"'](?P<path>[^\"']*)[\"']"
    r"(?P<rest>[^)]*)",
    re.IGNORECASE | re.DOTALL,
)
_PY_DJANGO = re.compile(
    r"\b(?:path|re_path|url)\(\s*r?[\"'](?P<path>[^\"']*)[\"']\s*,\s*(?P<view>[\w.]+)",
    re.IGNORECASE,
)
_DEF_AFTER = re.compile(r"\n\s*(?:async\s+)?def\s+(\w+)")


def _scan_python(text: str, rel: str) -> list[Route]:
    out: list[Route] = []
    # FastAPI/Starlette decorators (one per method).
    for m in _PY_DECORATOR.finditer(text):
        method = m.group("m").upper()
        nxt = _DEF_AFTER.search(text, m.end())
        out.append(Route(
            method=method, path=_norm_path(m.group("path")), framework="fastapi",
            file=rel, line=_line_of(text, m.start()),
            handler=nxt.group(1) if nxt else "", module=_module_of(_norm_path(m.group("path"))),
        ))
    # Flask @app.route(path, methods=[...]).
    for m in _PY_ROUTE.finditer(text):
        path = _norm_path(m.group("path"))
        rest = m.group("rest") or ""
        methods = re.search(r"methods\s*=\s*\[([^\]]*)\]", rest, re.IGNORECASE)
        names = (
            [x.strip().strip("\"'").upper() for x in methods.group(1).split(",") if x.strip()]
            if methods else ["GET"]
        )
        nxt = _DEF_AFTER.search(text, m.end())
        for method in names:
            out.append(Route(
                method=method, path=path, framework="flask", file=rel,
                line=_line_of(text, m.start()),
                handler=nxt.group(1) if nxt else "", module=_module_of(path),
            ))
    # Django path()/re_path()/url().
    for m in _PY_DJANGO.finditer(text):
        path = _norm_path(m.group("path"))
        out.append(Route(
            method="GET", path=path, framework="django", file=rel,
            line=_line_of(text, m.start()), handler=m.group("view").split(".")[-1],
            module=_module_of(path),
        ))
    return out


# ── JS / TS ─────────────────────────────────────────────────────────────────

_JS_METHOD = re.compile(
    r"\b(?:app|router|server|api|r)\s*\.\s*(get|post|put|patch|delete|options|head)\s*"
    r"\(\s*[\"'`](?P<path>[^\"'`]*)[\"'`]",
    re.IGNORECASE,
)
_NEST_CLASS = re.compile(r"@Controller\(\s*[\"'](?P<base>[^\"']*)[\"']", re.IGNORECASE)
_NEST_METHOD = re.compile(
    r"@(Get|Post|Put|Patch|Delete|Options|Head)\(\s*[\"']?(?P<path>[^\"')]*)[\"']?\s*\)",
    re.IGNORECASE,
)


def _scan_js(text: str, rel: str) -> list[Route]:
    out: list[Route] = []
    for m in _JS_METHOD.finditer(text):
        path = _norm_path(m.group("path"))
        out.append(Route(
            method=m.group(1).upper(), path=path, framework="express", file=rel,
            line=_line_of(text, m.start()), module=_module_of(path),
        ))
    # NestJS: class-level @Controller prefix + method decorators.
    cls = _NEST_CLASS.search(text)
    base = _norm_path(cls.group("base")) if cls else ""
    for m in _NEST_METHOD.finditer(text):
        sub = m.group("path").strip()
        path = _norm_path((base.rstrip("/") + "/" + sub.lstrip("/")) if sub else base or "/")
        out.append(Route(
            method=m.group(1).upper(), path=path, framework="nestjs", file=rel,
            line=_line_of(text, m.start()), module=_module_of(path),
        ))
    return out


# ── Go ──────────────────────────────────────────────────────────────────────

_GO_METHOD = re.compile(
    r"\b(?P<obj>\w+)\.(?P<m>GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD|Get|Post|Put|Patch|Delete|Options|Head)"
    r"\(\s*[\"`](?P<path>[^\"`]*)[\"`]",
)
_GO_HANDLEFUNC = re.compile(
    r"\bHandleFunc\(\s*[\"`](?P<path>[^\"`]*)[\"`]",
)
_GO_FUNC = re.compile(r"\n\s*func\s+(\w+)\s*\(")


def _scan_go(text: str, rel: str) -> list[Route]:
    out: list[Route] = []
    for m in _GO_METHOD.finditer(text):
        method = m.group("m").upper()
        nxt = _GO_FUNC.search(text, m.end())
        path = _norm_path(m.group("path"))
        out.append(Route(
            method=method, path=path, framework="gin", file=rel,
            line=_line_of(text, m.start()),
            handler=nxt.group(1) if nxt else "", module=_module_of(path),
        ))
    for m in _GO_HANDLEFUNC.finditer(text):
        path = _norm_path(m.group("path"))
        out.append(Route(
            method="GET", path=path, framework="nethttp", file=rel,
            line=_line_of(text, m.start()), module=_module_of(path),
        ))
    return out


# ── Java (Spring) ───────────────────────────────────────────────────────────

_JAVA_CLASS_MAP = re.compile(
    r"@RequestMapping\(\s*(?:value\s*=\s*)?[\"'](?P<base>[^\"']*)[\"']", re.IGNORECASE
)
_JAVA_METHOD_MAP = re.compile(
    r"@(?P<verb>Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*)?"
    r"(?:\{?\s*)?[\"'](?P<path>[^\"']*)[\"']",
    re.IGNORECASE,
)
# @PostMapping (no path) — verb decorator with an empty/omitted path.
_JAVA_METHOD_MAP_BARE = re.compile(
    r"@(?P<verb>Get|Post|Put|Patch|Delete)Mapping\s*(?:\(\s*\))?\s*\n",
    re.IGNORECASE,
)
_JAVA_REQ_MAP = re.compile(
    r"@RequestMapping\(\s*(?:value\s*=\s*)?[\"'](?P<path>[^\"']*)[\"'][^)]*?"
    r"method\s*=\s*RequestMethod\.(?P<m>GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD|TRACE)",
    re.IGNORECASE | re.DOTALL,
)
_JAVA_METHOD = re.compile(
    r"\n\s*(?:public|private|protected)?\s*[\w<>\[\],\s?]+\s+(?P<name>\w+)\s*\("
)


def _scan_java(text: str, rel: str) -> list[Route]:
    out: list[Route] = []
    class_match = _JAVA_CLASS_MAP.search(text)
    base = _norm_path(class_match.group("base")) if class_match else ""

    def _join(sub: str) -> str:
        sub = sub.strip()
        return _norm_path((base.rstrip("/") + "/" + sub.lstrip("/")) if sub else (base or "/"))

    for m in _JAVA_METHOD_MAP.finditer(text):
        path = _join(m.group("path"))
        nxt = _JAVA_METHOD.search(text, m.end())
        out.append(Route(
            method=m.group("verb").upper(), path=path, framework="spring", file=rel,
            line=_line_of(text, m.start()),
            handler=nxt.group("name") if nxt else "", module=_module_of(path),
        ))
    # Bare verb decorators (@PostMapping with no path) → class base path.
    for m in _JAVA_METHOD_MAP_BARE.finditer(text):
        # skip if this decorator already carried a path (matched above)
        line_end = text.find("\n", m.start())
        if "(" in text[m.start():line_end] and '"' in text[m.start():line_end]:
            continue
        path = _join("")
        nxt = _JAVA_METHOD.search(text, m.end())
        out.append(Route(
            method=m.group("verb").upper(), path=path, framework="spring", file=rel,
            line=_line_of(text, m.start()),
            handler=nxt.group("name") if nxt else "", module=_module_of(path),
        ))
    for m in _JAVA_REQ_MAP.finditer(text):
        path = _join(m.group("path"))
        out.append(Route(
            method=m.group("m").upper(), path=path, framework="spring", file=rel,
            line=_line_of(text, m.start()), module=_module_of(path),
        ))
    return out


# ── dispatch ────────────────────────────────────────────────────────────────

def _lang_of(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    for lang, exts in _LANG_EXT.items():
        if ext in exts:
            return lang
    return None


def _scan_file(path: Path, rel: str, lang: str) -> list[Route]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if lang == "python":
        return _scan_python(text, rel)
    if lang in ("js", "ts"):
        return _scan_js(text, rel)
    if lang == "go":
        return _scan_go(text, rel)
    if lang == "java":
        return _scan_java(text, rel)
    return []


def scan(root: str | Path, *, max_files: int = 20000) -> list[Route]:
    """Walk ``root`` and return every declared HTTP route found.

    Deterministic, best-effort. Skips vendored/build dirs. De-duplicates by
    ``(method, path, file)`` and returns sorted by (path, method).
    """
    base = Path(root)
    if not base.is_dir():
        return []
    seen: set[tuple[str, str, str]] = set()
    out: list[Route] = []
    count = 0
    for path in sorted(base.rglob("*")):
        if count >= max_files:
            break
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        lang = _lang_of(path)
        if lang is None:
            continue
        count += 1
        rel = path.relative_to(base).as_posix()
        for route in _scan_file(path, rel, lang):
            key = (route.method, route.path, route.file)
            if key in seen:
                continue
            seen.add(key)
            out.append(route)
    out.sort(key=lambda r: (r.path, r.method))
    return out


__all__ = ["Route", "scan"]
