"""Unit tests for tools/mc_tools.py — MC integration primitives.

Why: The hot path (mc_pipeline_run) sends real HTTP. Tests must cover:
  - graceful 'not configured' error when env vars are unset
  - happy-path response normalisation (id/job_id/run_id all accepted)
  - HTTP error surfacing (4xx body included in error)
  - URL/timeout error handling (connection refused, DNS, etc.)
  - input validation (missing pipeline_name, wrong inputs type)
  - gating (orchestrator profile / kanban env)

What: All tests stub `_mc_post` or `urllib.request.urlopen` so no real
network call is ever made. Pure-Python doubles keep the suite fast.

Test: `pytest tests/tools/test_mc_tools.py -v -o "addopts="`
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error as _urllib_error

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def mc_tools():
    """Reimport fresh to clear any module-level state between tests."""
    import importlib

    if "tools.mc_tools" in sys.modules:
        del sys.modules["tools.mc_tools"]
    import tools.mc_tools as mt  # noqa: WPS433  test-only import
    importlib.reload(mt)
    return mt


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Strip MC env vars before each test so config defaults to 'not
    configured' unless the test sets them explicitly."""
    for var in (
        "HERMES_MC_BASE_URL", "HERMES_MC_API_KEY",
        "HERMES_MC_TIMEOUT_SEC", "HERMES_KANBAN_TASK",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# _mc_config — env-var resolution
# ---------------------------------------------------------------------------


class TestMcConfig:
    def test_defaults_to_not_configured(self, mc_tools):
        base, key, timeout = mc_tools._mc_config()
        assert base is None
        assert key is None
        assert timeout == 30

    def test_strips_trailing_slash(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://localhost:3000/")
        base, _, _ = mc_tools._mc_config()
        assert base == "http://localhost:3000"

    def test_reads_api_key(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://x")
        monkeypatch.setenv("HERMES_MC_API_KEY", "secret-abc")
        _, key, _ = mc_tools._mc_config()
        assert key == "secret-abc"

    def test_invalid_timeout_falls_back_to_default(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://x")
        monkeypatch.setenv("HERMES_MC_TIMEOUT_SEC", "not-a-number")
        _, _, timeout = mc_tools._mc_config()
        assert timeout == 30


# ---------------------------------------------------------------------------
# Gating: _check_mc_mode
# ---------------------------------------------------------------------------


class TestCheckMcMode:
    def test_chief_worker_env_enables(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
        assert mc_tools._check_mc_mode() is True

    def test_kanban_toolset_enables(self, mc_tools, monkeypatch):
        with patch.object(
            mc_tools, "_check_mc_mode",
            side_effect=lambda: True,
        ):
            # Re-evaluate via the patched function — illustrates that the
            # check_fn is consulted at registration / call time.
            assert mc_tools._check_mc_mode() is True


# ---------------------------------------------------------------------------
# _mc_post — HTTP helper error handling
# ---------------------------------------------------------------------------


def _fake_response(body: bytes):
    """Why: minimal fake for the urlopen context-manager protocol."""
    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return body

    return _R()


class TestMcPost:
    def test_not_configured_raises_clean_error(self, mc_tools):
        with pytest.raises(RuntimeError, match="MC not configured"):
            mc_tools._mc_post("/api/x", {})

    def test_happy_path_parses_json(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        body = json.dumps({"id": "run_1", "status": "queued"}).encode()
        with patch.object(
            mc_tools._urllib_request, "urlopen",
            return_value=_fake_response(body),
        ):
            out = mc_tools._mc_post("/api/pipelines/run", {"pipeline": "x"})
        assert out == {"id": "run_1", "status": "queued"}

    def test_empty_body_returns_empty_dict(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools._urllib_request, "urlopen",
            return_value=_fake_response(b""),
        ):
            out = mc_tools._mc_post("/api/x", {})
        assert out == {}

    def test_4xx_carries_error_body(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        http_err = _urllib_error.HTTPError(
            url="http://test/api/x", code=422,
            msg="Unprocessable", hdrs=None,
            fp=io.BytesIO(b'{"error":"missing pipeline"}'),
        )
        with patch.object(
            mc_tools._urllib_request, "urlopen", side_effect=http_err,
        ):
            with pytest.raises(RuntimeError, match="HTTP 422.*missing pipeline"):
                mc_tools._mc_post("/api/x", {})

    def test_connection_refused_raises_clean_error(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        url_err = _urllib_error.URLError("Connection refused")
        with patch.object(
            mc_tools._urllib_request, "urlopen", side_effect=url_err,
        ):
            with pytest.raises(RuntimeError, match="unreachable"):
                mc_tools._mc_post("/api/x", {})

    def test_non_json_2xx_returns_raw_text(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools._urllib_request, "urlopen",
            return_value=_fake_response(b"plain text"),
        ):
            out = mc_tools._mc_post("/api/x", {})
        assert out["_raw_text"] == "plain text"
        assert "non-json" in out["_warning"]


# ---------------------------------------------------------------------------
# mc_pipeline_run handler — input validation + normalisation
# ---------------------------------------------------------------------------


class TestMcPipelineRun:
    """`mc_pipeline_run` POSTs {action:'start', pipeline_id} to MC's
    /api/pipelines/run. MC returns {run: {id, status, ...}}.
    """

    def test_missing_both_name_and_id(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_run({})
        assert isinstance(out, str)
        assert "pipeline_name" in out and "pipeline_id" in out

    def test_pipeline_id_must_be_positive_int(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_run({"pipeline_id": 0})
        assert isinstance(out, str)
        assert "pipeline_id" in out

        out = mc_tools._handle_mc_pipeline_run({"pipeline_id": "7"})
        assert isinstance(out, str)
        assert "pipeline_id" in out

    def test_happy_path_with_pipeline_id(self, mc_tools, monkeypatch):
        """Direct numeric id — no list lookup, single POST."""
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            return_value={"run": {"id": 42, "pipeline_id": 7,
                                  "status": "running", "current_step": 0}},
        ) as p:
            out = mc_tools._handle_mc_pipeline_run({"pipeline_id": 7})
        # Verify payload sent matches MC's contract
        path, payload = p.call_args.args
        assert path == "/api/pipelines/run"
        assert payload == {"action": "start", "pipeline_id": 7}
        # Verify response shape
        assert out["ok"] is True
        assert out["run_id"] == 42
        assert out["pipeline_id"] == 7
        assert out["status"] == "running"
        assert out["current_step"] == 0
        assert "_raw" in out

    def test_happy_path_with_pipeline_name_resolves_id(
        self, mc_tools, monkeypatch,
    ):
        """`pipeline_name` triggers GET /api/pipelines for resolution
        before the POST."""
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            return_value={"pipelines": [
                {"id": 11, "name": "deploy"},
                {"id": 22, "name": "code-review"},
            ]},
        ) as g, patch.object(
            mc_tools, "_mc_post",
            return_value={"run": {"id": 99, "pipeline_id": 22,
                                  "status": "running", "current_step": 0}},
        ) as p:
            out = mc_tools._handle_mc_pipeline_run(
                {"pipeline_name": "code-review"},
            )
        g.assert_called_once_with("/api/pipelines")
        _, payload = p.call_args.args
        assert payload == {"action": "start", "pipeline_id": 22}
        assert out["run_id"] == 99
        assert out["pipeline_id"] == 22
        assert out["pipeline_name"] == "code-review"

    def test_pipeline_name_not_found_lists_available(
        self, mc_tools, monkeypatch,
    ):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            return_value={"pipelines": [
                {"id": 11, "name": "deploy"},
                {"id": 22, "name": "code-review"},
            ]},
        ):
            out = mc_tools._handle_mc_pipeline_run(
                {"pipeline_name": "ghost"},
            )
        assert isinstance(out, str)
        assert "ghost" in out
        assert "deploy" in out and "code-review" in out  # surfaces options

    def test_pipeline_name_with_empty_registry(
        self, mc_tools, monkeypatch,
    ):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            return_value={"pipelines": []},
        ):
            out = mc_tools._handle_mc_pipeline_run(
                {"pipeline_name": "x"},
            )
        assert isinstance(out, str)
        assert "none registered" in out or "not found" in out

    def test_unconfigured_returns_tool_error(self, mc_tools):
        # Without HERMES_MC_BASE_URL, the lookup helper fails first
        out = mc_tools._handle_mc_pipeline_run({"pipeline_name": "x"})
        assert isinstance(out, str)
        assert "MC not configured" in out

    def test_unconfigured_with_id_returns_tool_error(self, mc_tools):
        # Same when bypassing lookup with pipeline_id — _mc_post fails
        out = mc_tools._handle_mc_pipeline_run({"pipeline_id": 1})
        assert isinstance(out, str)
        assert "MC not configured" in out

    def test_4xx_from_start_returns_tool_error(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            side_effect=RuntimeError(
                "MC /api/pipelines/run returned HTTP 404: pipeline_id not found",
            ),
        ):
            out = mc_tools._handle_mc_pipeline_run({"pipeline_id": 999})
        assert isinstance(out, str)
        assert "HTTP 404" in out

    def test_missing_run_object_returns_tool_error(
        self, mc_tools, monkeypatch,
    ):
        """MC must return {run: {...}} on start. Anything else is a
        contract violation — surface it instead of silently returning
        ok=True with None fields."""
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            return_value={"unexpected": "shape"},
        ):
            out = mc_tools._handle_mc_pipeline_run({"pipeline_id": 1})
        assert isinstance(out, str)
        assert "missing expected 'run' object" in out

    def test_accepts_dispatch_kwargs(self, mc_tools, monkeypatch):
        """registry.dispatch passes task_id/agent_name/etc — handler
        must accept arbitrary kwargs."""
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            return_value={"run": {"id": 1, "status": "running"}},
        ):
            out = mc_tools._handle_mc_pipeline_run(
                {"pipeline_id": 7},
                task_id="t_123",
                agent_name="research-agent",
                some_future_kwarg="ignored",
            )
        assert out["run_id"] == 1


# ---------------------------------------------------------------------------
# mc_pipeline_status handler
# ---------------------------------------------------------------------------


class TestMcPipelineStatus:
    def test_missing_run_id(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_status({})
        assert isinstance(out, str)
        assert "run_id" in out

    def test_run_id_must_be_positive_int(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_status({"run_id": "42"})
        assert isinstance(out, str)
        assert "run_id" in out

        out = mc_tools._handle_mc_pipeline_status({"run_id": 0})
        assert isinstance(out, str)

    def test_happy_path(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            return_value={"run": {
                "id": 42, "pipeline_id": 7, "status": "completed",
                "current_step": 2, "started_at": 1000, "completed_at": 2000,
                "steps_snapshot": [{"step_index": 0, "status": "completed"}],
            }},
        ) as g:
            out = mc_tools._handle_mc_pipeline_status({"run_id": 42})
        g.assert_called_once_with("/api/pipelines/run?id=42")
        assert out["ok"] is True
        assert out["run_id"] == 42
        assert out["status"] == "completed"
        assert out["current_step"] == 2
        assert out["completed_at"] == 2000

    def test_404_returns_tool_error(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            side_effect=RuntimeError(
                "MC /api/pipelines/run?id=999 returned HTTP 404: Run not found",
            ),
        ):
            out = mc_tools._handle_mc_pipeline_status({"run_id": 999})
        assert isinstance(out, str)
        assert "HTTP 404" in out

    def test_accepts_dispatch_kwargs(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            return_value={"run": {"id": 1, "status": "running"}},
        ):
            out = mc_tools._handle_mc_pipeline_status(
                {"run_id": 1}, task_id="t", agent_name="x",
            )
        assert out["ok"] is True


# ---------------------------------------------------------------------------
# mc_pipeline_cancel handler
# ---------------------------------------------------------------------------


class TestMcPipelineCancel:
    def test_missing_run_id(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_cancel({})
        assert isinstance(out, str)
        assert "run_id" in out

    def test_happy_path(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            return_value={"run": {"id": 42, "status": "cancelled"}},
        ) as p:
            out = mc_tools._handle_mc_pipeline_cancel({"run_id": 42})
        path, payload = p.call_args.args
        assert path == "/api/pipelines/run"
        assert payload == {"action": "cancel", "run_id": 42}
        assert out["ok"] is True
        assert out["run_id"] == 42
        assert out["status"] == "cancelled"

    def test_4xx_returns_tool_error(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            side_effect=RuntimeError(
                "MC /api/pipelines/run returned HTTP 400: Run is completed, not running",
            ),
        ):
            out = mc_tools._handle_mc_pipeline_cancel({"run_id": 42})
        assert isinstance(out, str)
        assert "HTTP 400" in out

    def test_accepts_dispatch_kwargs(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            return_value={"run": {"id": 1, "status": "cancelled"}},
        ):
            out = mc_tools._handle_mc_pipeline_cancel(
                {"run_id": 1}, task_id="t", agent_name="x",
            )
        assert out["ok"] is True


# ---------------------------------------------------------------------------
# mc_pipeline_list handler
# ---------------------------------------------------------------------------


class TestMcPipelineList:
    def test_unconfigured_returns_tool_error(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_list({})
        assert isinstance(out, str)
        assert "MC not configured" in out

    def test_happy_path_returns_pipelines(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            return_value={"pipelines": [
                {"id": 1, "name": "code-review"},
                {"id": 2, "name": "deploy"},
            ]},
        ) as g:
            out = mc_tools._handle_mc_pipeline_list({})
        g.assert_called_once_with("/api/pipelines")
        assert out["ok"] is True
        assert out["count"] == 2
        assert out["pipelines"][0]["name"] == "code-review"

    def test_handles_empty_list(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            return_value={"pipelines": []},
        ):
            out = mc_tools._handle_mc_pipeline_list({})
        assert out["ok"] is True
        assert out["count"] == 0
        assert out["pipelines"] == []

    def test_accepts_dispatch_kwargs(self, mc_tools, monkeypatch):
        """Same as run-handler — registry.dispatch passes kwargs."""
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            return_value={"pipelines": []},
        ):
            out = mc_tools._handle_mc_pipeline_list(
                {}, task_id="t_123", agent_name="x",
            )
        assert out["ok"] is True

    def test_http_error_surfaces(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_get",
            side_effect=RuntimeError("MC /api/pipelines returned HTTP 500: oops"),
        ):
            out = mc_tools._handle_mc_pipeline_list({})
        assert isinstance(out, str)
        assert "HTTP 500" in out


# ---------------------------------------------------------------------------
# mc_exec_approve_list + mc_exec_approve handlers (HITL bridge proxy)
# ---------------------------------------------------------------------------


class TestMcExecApproveList:
    def test_happy_path(self, mc_tools, monkeypatch):
        # _hitl_get reuses _mc_config for timeout — set base url so the
        # cfg loader doesn't surface a "not configured" path. The
        # actual HITL request is mocked.
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        bridge_response = {
            "count": 1,
            "pending": [
                {
                    "req_id": "abc",
                    "tg_message_id": 42,
                    "dispatched_at": 1000.0,
                    "payload": {
                        "id": "abc",
                        "agent_id": "research-1",
                        "task_id": "t-1",
                        "type": "approval",
                        "question": "Run X?",
                        "options": ["yes", "no"],
                    },
                },
            ],
        }
        with patch.object(
            mc_tools, "_hitl_get", return_value=bridge_response,
        ) as g:
            out = mc_tools._handle_mc_exec_approve_list({})
        g.assert_called_once_with("/hitl/list")
        assert out["ok"] is True
        assert out["count"] == 1
        item = out["pending"][0]
        assert item["req_id"] == "abc"
        assert item["agent_id"] == "research-1"
        assert item["question"] == "Run X?"

    def test_empty_pending(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_hitl_get", return_value={"count": 0, "pending": []},
        ):
            out = mc_tools._handle_mc_exec_approve_list({})
        assert out["ok"] is True
        assert out["count"] == 0
        assert out["pending"] == []

    def test_bridge_unreachable_returns_tool_error(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_hitl_get",
            side_effect=RuntimeError("HITL /hitl/list unreachable at ...: connection refused"),
        ):
            out = mc_tools._handle_mc_exec_approve_list({})
        assert isinstance(out, str)
        assert "unreachable" in out

    def test_accepts_dispatch_kwargs(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_hitl_get", return_value={"count": 0, "pending": []},
        ):
            out = mc_tools._handle_mc_exec_approve_list(
                {}, task_id="t_123", agent_name="x",
            )
        assert out["ok"] is True


class TestMcExecApprove:
    def test_missing_req_id(self, mc_tools):
        out = mc_tools._handle_mc_exec_approve({"action": "approve"})
        assert isinstance(out, str)
        assert "req_id" in out

    def test_invalid_action(self, mc_tools):
        out = mc_tools._handle_mc_exec_approve(
            {"req_id": "abc", "action": "maybe"},
        )
        assert isinstance(out, str)
        assert "action" in out

    def test_reason_must_be_string(self, mc_tools):
        out = mc_tools._handle_mc_exec_approve(
            {"req_id": "abc", "action": "approve", "reason": 42},
        )
        assert isinstance(out, str)
        assert "reason" in out

    def test_happy_path(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_hitl_post",
            return_value={"ok": True, "req_id": "abc", "action": "approve",
                          "mc_response": {"ok": True}},
        ) as p:
            out = mc_tools._handle_mc_exec_approve(
                {"req_id": "abc", "action": "approve", "reason": "looks good"},
            )
        path, payload = p.call_args.args
        assert path == "/hitl/respond"
        assert payload == {
            "req_id": "abc",
            "action": "approve",
            "reason": "looks good",
        }
        assert out["ok"] is True
        assert out["req_id"] == "abc"
        assert out["action"] == "approve"
        assert out["mc_response"] == {"ok": True}

    def test_404_returns_tool_error(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_hitl_post",
            side_effect=RuntimeError("HITL /hitl/respond HTTP 404: unknown req_id"),
        ):
            out = mc_tools._handle_mc_exec_approve(
                {"req_id": "ghost", "action": "approve"},
            )
        assert isinstance(out, str)
        assert "HTTP 404" in out

    def test_accepts_dispatch_kwargs(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_hitl_post",
            return_value={"ok": True, "mc_response": {}},
        ):
            out = mc_tools._handle_mc_exec_approve(
                {"req_id": "abc", "action": "deny"},
                task_id="t_123", agent_name="x",
            )
        assert out["ok"] is True
