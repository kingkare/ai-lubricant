"""Subprocess wrapper around the Graphify CLI (plan ``09-knowledge-system.md`` §4.7/§4.9).

Graphify must run in an **isolated** virtualenv — never the project's shared
``.venv``, because installing packages there pollutes the shared environment.
Point ``GRAPHIFY_VENV`` at that isolated venv; this module locates its Python
and invokes ``python -m graphify`` so the CLI works regardless of platform
layout (``Scripts/python.exe`` on Windows, ``bin/python`` on POSIX).

``--code-only`` is the verified path (§4.9): local AST extraction, no LLM key,
no network. Document/PDF/image extraction — which demands an LLM key — is
deliberately out of scope for this adapter.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Sequence

# Hard requirement: never fall back to the project .venv / system python.
# 读**惰性**化：模块导入时固化 env 会让「先 import 后 load_project_env()」的
# 启动顺序悄悄拿到 None（表现为 GRAPHIFY_VENV is not set，即使 .env 里配了）。
GRAPHIFY_VENV = os.environ.get("GRAPHIFY_VENV")


def _graphify_venv() -> str | None:
    """当前生效的隔离 venv 路径（环境优先，退回导入期快照）。"""
    return os.environ.get("GRAPHIFY_VENV") or GRAPHIFY_VENV

OUT_SUBDIR = "graphify-out"
GRAPH_JSON = "graph.json"
_MODULE = "graphify"

_INSTALL_HINT = (
    "GRAPHIFY_VENV is not set. Graphify must run in an isolated venv "
    "(never the shared project .venv). Create one and export it:\n"
    "  python -m venv .venv-graphify\n"
    "  .venv-graphify/Scripts/python -m pip install graphifyy==0.9.71\n"
    "  set GRAPHIFY_VENV=...\\.venv-graphify   (bash: export GRAPHIFY_VENV=...)"
)


class GraphifyNotInstalled(RuntimeError):
    """``GRAPHIFY_VENV`` unset, or it does not contain a Python interpreter."""


class GraphifyRunError(RuntimeError):
    """The graphify subprocess failed or produced no ``graph.json``."""


def _venv_python(venv: str | None = None) -> Path:
    """Resolve the isolated venv's Python interpreter."""
    root = venv or _graphify_venv()
    if not root:
        raise GraphifyNotInstalled(_INSTALL_HINT)
    base = Path(root)
    if not base.is_dir():
        raise GraphifyNotInstalled(f"GRAPHIFY_VENV={base} is not a directory")
    for rel in ("Scripts/python.exe", "Scripts/python", "bin/python", "bin/python3"):
        cand = base / rel
        if cand.is_file():
            return cand
    raise GraphifyNotInstalled(
        f"No python interpreter found under {base!s} — is GRAPHIFY_VENV a virtualenv?"
    )


class GraphifyRunner:
    """Runs ``graphify extract`` and returns the raw ``graph.json`` payload."""

    def __init__(self, venv: str | None = None, *, timeout: float = 900.0) -> None:
        self.venv = venv
        self.timeout = timeout

    @property
    def python(self) -> Path:
        return _venv_python(self.venv)

    async def _exec(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        """Run a command in the isolated venv; return (returncode, combined output)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd) if cwd else None,
            )
        except FileNotFoundError as exc:  # interpreter vanished
            raise GraphifyNotInstalled(f"cannot spawn {args[0]}: {exc}") from exc
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout or self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise GraphifyRunError(
                f"graphify timed out after {timeout or self.timeout}s: {' '.join(args)}"
            ) from None
        return proc.returncode, raw.decode("utf-8", errors="replace")

    async def version(self, *, timeout: float = 60.0) -> str | None:
        """Detect the installed graphify version (``graphify --version``).

        Returns e.g. ``"0.9.71"``, or ``None`` when the CLI is unusable.
        """
        py = self.python
        code, out = await self._exec([str(py), "-m", _MODULE, "--version"], timeout=timeout)
        if code != 0:
            return None
        for token in out.replace("\r", "\n").split():
            if token[:1].isdigit():
                return token.strip("v,")
        return out.strip() or None

    async def extract(
        self,
        project_path: str | Path,
        *,
        code_only: bool = True,
        out_dir: str | Path | None = None,
        extra_args: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Run ``graphify extract <path> [--code-only]`` and parse ``graph.json``.

        Graphify writes ``<path>/graphify-out/graph.json`` by default; ``out_dir``
        redirects that via ``--out`` when the target is read-only.
        """
        path = Path(project_path).resolve()
        if not path.is_dir():
            raise GraphifyRunError(f"project path does not exist or is not a directory: {path}")

        args = [str(self.python), "-m", _MODULE, "extract", str(path)]
        if code_only:
            args.append("--code-only")
        if out_dir:
            args += ["--out", str(Path(out_dir).resolve())]
        args += list(extra_args)

        code, out = await self._exec(args, cwd=path)
        target = Path(out_dir).resolve() if out_dir else path
        graph_file = target / OUT_SUBDIR / GRAPH_JSON
        if not graph_file.is_file():
            raise GraphifyRunError(
                f"graphify extract failed (exit {code}) or wrote no {graph_file}:\n{out}"
            )
        try:
            payload = json.loads(graph_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GraphifyRunError(f"{graph_file} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise GraphifyRunError(f"{graph_file} is not a JSON object")
        return payload

    async def update(self, project_path: str | Path, *, extra_args: Sequence[str] = ()) -> dict[str, Any]:
        """Re-extract only changed code files (no LLM, uses graphify's SHA256 cache)."""
        path = Path(project_path).resolve()
        args = [str(self.python), "-m", _MODULE, "update", str(path), *extra_args]
        code, out = await self._exec(args, cwd=path)
        graph_file = path / OUT_SUBDIR / GRAPH_JSON
        if not graph_file.is_file():
            raise GraphifyRunError(f"graphify update failed (exit {code}):\n{out}")
        return json.loads(graph_file.read_text(encoding="utf-8"))
