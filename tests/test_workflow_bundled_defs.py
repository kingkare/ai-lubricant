"""Bundled workflow defs stay valid under the live engine rules.

The shipped ``agent/workflows/*.json`` files are loaded as user workflows
(recover/re-run paths, import fixtures). Every engine-side tightening must keep
them passing ``validate_def`` — otherwise a restart recovery or an import
trips on a definition we ourselves shipped. No DB: pure JSON + validation.
"""
import json
import os
import sys
from pathlib import Path

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.workflow import WorkflowDefError, validate_def

BUNDLED_DIR = Path(_proj) / "agent" / "workflows"


def _bundled_files() -> list[Path]:
    return sorted(BUNDLED_DIR.glob("*.json"))


def test_bundled_workflow_files_validate():
    files = _bundled_files()
    assert files, f"no bundled workflow defs under {BUNDLED_DIR}"
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        # Files are either {definition: {...}} wrappers or the bare definition.
        defn = doc.get("definition") if isinstance(doc.get("definition"), dict) else doc
        try:
            validate_def(defn)
        except WorkflowDefError as exc:  # pragma: no cover - failure detail
            raise AssertionError(f"{path.name} no longer validates: {exc}") from None


def test_no_bundled_web_node_uses_fill_credential_without_credential_kind():
    """A bundled web(fill_credential) step must carry credential_kind.

    Guards the shipped defs against the `kind` collision: `kind` names the node
    type, so the credential type has to live in its own field (validate_def
    enforces it — this test pins that the shipped files already comply).
    """
    for path in _bundled_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        defn = doc.get("definition") if isinstance(doc.get("definition"), dict) else doc
        for node in defn.get("nodes") or []:
            if node.get("kind") != "web":
                continue
            if str(node.get("operation") or "").lower() != "fill_credential":
                continue
            assert str(node.get("credential_kind") or "").lower() in ("otp", "password"), (
                f"{path.name}: node {node.get('id')!r} is web(fill_credential) "
                f"without a valid credential_kind"
            )
