"""web(fill_credential) executor mapping — credential_kind → MCP `kind`.

The graph cannot carry a field named ``kind`` for the credential type: ``kind``
is the engine's node-type field and is stripped from the param bag by
``node_params``, so it never reaches an executor. The graph writes
``credential_kind`` instead and ``run_web`` maps it onto the MCP method's own
``kind`` argument (`browser_fill_credential(kind=...)`).

Covers:
- the mapping reaches the MCP call as `kind`, alongside account/selector;
- a missing or invalid `credential_kind` fails with an actionable message
  (validate_def rejects it at save time too, this is the run-time belt);
- the engine never forwards `kind` itself (the param bag has it stripped).
"""
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.workflow import WorkflowContext, node_params
from agent.workflow_exec import WorkflowStepExecutors


def _executor():
    ex = WorkflowStepExecutors(owner=None, workspace_root="data/workflows/test")
    ex._mcp_ready = True
    ex._browser_entry = {
        "service": "cdp-bridge",
        "method_names": ["browser_fill_credential", "browser_totp_code", "browser_execute_js"],
    }
    captured: list[tuple[str, str, dict]] = []

    async def _call_mcp(service: str, method: str, args: dict):
        captured.append((service, method, args))
        return {"status": "ok", "filled": True}

    ex._call_mcp = _call_mcp  # type: ignore[method-assign]
    ex.captured = captured  # type: ignore[attr-defined]
    return ex


@pytest.mark.asyncio
async def test_credential_kind_mapped_to_mcp_kind():
    ex = _executor()
    out = await ex.run_web(
        {"id": "fill", "kind": "web", "operation": "fill_credential"},
        WorkflowContext(),
        {
            "operation": "fill_credential",
            "credential_kind": "password",
            "url": "https://example.com/login",
            "account": "alice@example.com",
        },
    )
    assert out.get("filled") is True
    service, method, args = ex.captured[0]
    assert service == "cdp-bridge"
    assert method == "browser_fill_credential"
    assert args["kind"] == "password"
    assert args["account"] == "alice@example.com"
    assert args["url"] == "https://example.com/login"
    # The graph-level alias must not leak to the MCP call — `kind` is THE
    # credential kind now, never a second copy under the alias' name.
    assert "credential_kind" not in args


@pytest.mark.asyncio
async def test_credential_kind_case_insensitive():
    ex = _executor()
    await ex.run_web(
        {"id": "fill", "kind": "web", "operation": "fill_credential"},
        WorkflowContext(),
        {"operation": "fill_credential", "credential_kind": "OTP", "domain": "example.com"},
    )
    _, _, args = ex.captured[0]
    assert args["kind"] == "otp"


@pytest.mark.asyncio
async def test_missing_credential_kind_fails_actionably():
    ex = _executor()
    with pytest.raises(RuntimeError, match="credential_kind"):
        await ex.run_web(
            {"id": "fill", "kind": "web", "operation": "fill_credential"},
            WorkflowContext(),
            {"operation": "fill_credential", "url": "https://example.com/login"},
        )
    assert ex.captured == [], "the MCP call must not be attempted without a credential kind"


def test_engine_strips_kind_from_param_bag():
    """The root cause guard: `kind` can never travel through node params."""
    params = node_params({"id": "a", "name": "步骤", "kind": "web", "operation": "fill_credential"})
    assert "kind" not in params
    assert params.get("operation") == "fill_credential"
