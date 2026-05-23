"""Parametrised contract tests: ANY registered ``WorkflowAdapter``
that implements the five-method protocol must pass these. Same suite
runs against InlineAdapter (default) and against any future backend
(LangGraph, MC-rich, ...).

The "MC" backend isn't tested here because it requires a live MC
container — covered by integration tests in
``infra/hermes/test/workflows/`` instead.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from tools.workflow_adapters import (
    WorkflowAdapter,
    WorkflowStatus,
    get_adapter,
    list_adapters,
)


# Adapters that DON'T require external services for tests.
_TESTABLE_BACKENDS = ["inline"]


def _minimal_dsl(name: str) -> dict:
    return {
        "name": name,
        "version": 1,
        "description": "contract-test",
        "nodes": [
            {"id": "only", "profile": "research-agent",
             "task": "noop — contract test"},
        ],
    }


def _two_step_dsl(name: str) -> dict:
    return {
        "name": name,
        "version": 1,
        "description": "contract-test-2step",
        "nodes": [
            {"id": "alpha", "profile": "research-agent", "task": "step alpha"},
            {"id": "beta", "profile": "research-agent",
             "task": "step beta — read {{ steps.alpha.result }}",
             "depends_on": ["alpha"]},
        ],
    }


# ---------------------------------------------------------------------------
# Contract: protocol compliance


@pytest.mark.parametrize("backend", _TESTABLE_BACKENDS)
def test_adapter_implements_protocol(backend):
    adapter = get_adapter(backend)
    assert isinstance(adapter, WorkflowAdapter)
    assert adapter.name == backend


@pytest.mark.parametrize("backend", _TESTABLE_BACKENDS)
def test_adapter_compile_returns_template_id(backend):
    adapter = get_adapter(backend)
    template_id = adapter.compile(_minimal_dsl(f"contract-compile-{backend}"))
    assert isinstance(template_id, str)
    assert template_id


@pytest.mark.parametrize("backend", _TESTABLE_BACKENDS)
def test_adapter_compile_is_idempotent(backend):
    adapter = get_adapter(backend)
    dsl = _minimal_dsl(f"contract-idempotent-{backend}")
    tid1 = adapter.compile(dsl)
    tid2 = adapter.compile(dsl)
    assert tid1 == tid2


@pytest.mark.parametrize("backend", _TESTABLE_BACKENDS)
def test_adapter_compile_rejects_invalid_dsl(backend):
    adapter = get_adapter(backend)
    with pytest.raises(Exception):
        adapter.compile({"name": "X", "version": 1, "nodes": []})  # empty nodes


@pytest.mark.parametrize("backend", _TESTABLE_BACKENDS)
def test_adapter_run_unknown_template(backend):
    adapter = get_adapter(backend)
    res = adapter.run("definitely-not-a-template-xyz", {})
    # Either returns failed-state result OR raises — both contract-acceptable
    assert res.state in {"failed", "queued"}


@pytest.mark.parametrize("backend", _TESTABLE_BACKENDS)
def test_adapter_status_unknown_run(backend):
    adapter = get_adapter(backend)
    st = adapter.status("totally-bogus-run-id")
    assert isinstance(st, WorkflowStatus)
    assert st.state in {"failed", "cancelled"}


@pytest.mark.parametrize("backend", _TESTABLE_BACKENDS)
def test_adapter_cancel_unknown_run(backend):
    adapter = get_adapter(backend)
    res = adapter.cancel("totally-bogus-run-id")
    assert isinstance(res, dict)
    assert "ok" in res
    assert res["ok"] is False


@pytest.mark.parametrize("backend", _TESTABLE_BACKENDS)
def test_adapter_list_templates_returns_list(backend):
    adapter = get_adapter(backend)
    out = adapter.list_templates()
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# Contract: run flow with /api/v1/run-profile mocked
#
# For inline, run() spawns a thread that POSTs /api/v1/run-profile per
# node. We patch the HTTP call to return canned success — verifies the
# adapter's state machine without needing the gateway running.


def _mock_post_success(profile, title, body, inputs, timeout_sec):
    return {
        "run_id": f"mocked-{title[:16]}",
        "status": "done",
        "result": f"mocked output for {profile}",
        "error": None,
    }


def _mock_post_failure(profile, title, body, inputs, timeout_sec):
    return {
        "run_id": "mocked-fail",
        "status": "blocked",
        "result": None,
        "error": "mocked failure",
    }


@pytest.mark.parametrize("backend", ["inline"])
def test_adapter_run_full_chain_succeeds(backend):
    adapter = get_adapter(backend)
    dsl = _two_step_dsl(f"contract-run-success-{backend}")
    adapter.compile(dsl)

    with patch(
        "tools.workflow_adapters.inline._post_run_profile",
        side_effect=_mock_post_success,
    ):
        res = adapter.run(dsl["name"], {})
        assert res.run_id
        assert res.state in {"queued", "running", "done"}
        # Poll
        for _ in range(40):
            st = adapter.status(res.run_id)
            if st.state in {"done", "failed", "cancelled"}:
                break
            time.sleep(0.05)
    assert st.state == "done"
    assert len(st.history) == 2
    assert st.history[0]["node_id"] == "alpha"
    assert st.history[1]["node_id"] == "beta"
    assert st.result is not None


@pytest.mark.parametrize("backend", ["inline"])
def test_adapter_run_stops_on_node_failure(backend):
    adapter = get_adapter(backend)
    dsl = _two_step_dsl(f"contract-run-failure-{backend}")
    adapter.compile(dsl)

    with patch(
        "tools.workflow_adapters.inline._post_run_profile",
        side_effect=_mock_post_failure,
    ):
        res = adapter.run(dsl["name"], {})
        for _ in range(40):
            st = adapter.status(res.run_id)
            if st.state in {"done", "failed", "cancelled"}:
                break
            time.sleep(0.05)
    assert st.state == "failed"
    # Only the first node should have run before failure halted the chain
    assert len(st.history) == 1
    assert st.history[0]["node_id"] == "alpha"
    assert st.error is not None


@pytest.mark.parametrize("backend", ["inline"])
def test_adapter_cancel_during_run(backend):
    adapter = get_adapter(backend)
    dsl = _two_step_dsl(f"contract-run-cancel-{backend}")
    adapter.compile(dsl)

    # Slow mock so we can cancel before second node starts
    def slow_success(*a, **kw):
        time.sleep(0.3)
        return _mock_post_success(*a, **kw)

    with patch(
        "tools.workflow_adapters.inline._post_run_profile",
        side_effect=slow_success,
    ):
        res = adapter.run(dsl["name"], {})
        time.sleep(0.05)  # let thread start node a
        cancel_res = adapter.cancel(res.run_id)
        assert cancel_res["ok"] is True
        for _ in range(80):
            st = adapter.status(res.run_id)
            if st.state in {"done", "failed", "cancelled"}:
                break
            time.sleep(0.05)
    # Either fully done (cancel arrived too late, single node) or cancelled.
    assert st.state in {"cancelled", "done"}


# ---------------------------------------------------------------------------
# Registry sanity


def test_inline_adapter_always_registered():
    """``inline`` must be available without any external service."""
    assert "inline" in list_adapters()


def test_get_adapter_default_falls_back_to_inline(monkeypatch):
    monkeypatch.delenv("HERMES_WORKFLOW_BACKEND", raising=False)
    from tools.workflow_adapters import base
    base.set_active_adapter(None)
    adapter = get_adapter()
    assert adapter.name == "inline"


def test_get_adapter_unknown_backend_raises():
    with pytest.raises(KeyError):
        get_adapter("does-not-exist")
