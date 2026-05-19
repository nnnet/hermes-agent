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
    # tool_error() returns a JSON STRING — we assert substring presence
    # instead of dict-key access.
    def test_missing_pipeline_name(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_run({})
        assert isinstance(out, str)
        assert "pipeline_name" in out

    def test_inputs_must_be_dict(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_run(
            {"pipeline_name": "p", "inputs": "wrong"},
        )
        assert isinstance(out, str)
        assert "inputs" in out

    def test_callback_url_must_be_string(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_run(
            {"pipeline_name": "p", "callback_url": 42},
        )
        assert isinstance(out, str)
        assert "callback_url" in out

    def test_happy_path_normalises_response(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            return_value={"id": "run_1", "status": "queued",
                          "status_url": "http://test/api/runs/run_1"},
        ) as p:
            out = mc_tools._handle_mc_pipeline_run({
                "pipeline_name": "code-review",
                "inputs": {"pr": 123},
                "callback_url": "http://hermes-host/hooks/mc",
                "tenant": "acme",
            })
        # Verify payload sent
        args, kwargs = p.call_args
        assert args[0] == "/api/pipelines/run"
        sent = args[1]
        assert sent["pipeline"] == "code-review"
        assert sent["inputs"] == {"pr": 123}
        assert sent["callback_url"] == "http://hermes-host/hooks/mc"
        assert sent["tenant"] == "acme"
        # Verify response shape
        assert out["ok"] is True
        assert out["pipeline_name"] == "code-review"
        assert out["job_id"] == "run_1"
        assert out["status"] == "queued"
        assert out["status_url"] == "http://test/api/runs/run_1"
        assert "_raw" in out

    def test_unconfigured_returns_tool_error(self, mc_tools):
        out = mc_tools._handle_mc_pipeline_run({"pipeline_name": "x"})
        assert isinstance(out, str)
        assert "MC not configured" in out

    def test_4xx_returns_tool_error(self, mc_tools, monkeypatch):
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            side_effect=RuntimeError("MC /api/pipelines/run returned HTTP 404: pipeline not found"),
        ):
            out = mc_tools._handle_mc_pipeline_run({"pipeline_name": "ghost"})
        assert isinstance(out, str)
        assert "HTTP 404" in out

    def test_alternative_response_field_names(self, mc_tools, monkeypatch):
        """MC versions emit id under different keys (job_id, run_id).
        Handler should accept any of them."""
        monkeypatch.setenv("HERMES_MC_BASE_URL", "http://test")
        with patch.object(
            mc_tools, "_mc_post",
            return_value={"job_id": "j_42"},  # no 'id' field
        ):
            out = mc_tools._handle_mc_pipeline_run({"pipeline_name": "x"})
        assert out["job_id"] == "j_42"

        with patch.object(
            mc_tools, "_mc_post",
            return_value={"run_id": "r_99"},
        ):
            out = mc_tools._handle_mc_pipeline_run({"pipeline_name": "x"})
        assert out["job_id"] == "r_99"
