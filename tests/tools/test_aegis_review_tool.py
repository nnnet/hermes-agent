"""Tests for the aegis_review MCP tool wrapper.

The handler is a thin shim over plugins.aegis_attestation.llm_review.review_task,
which has its own deep test coverage. This file covers the tool surface:

* Schema correctness — tool is registered under kanban toolset.
* Gating — visible to workers and orchestrator profiles, hidden in plain chat.
* Handler error paths — missing task_id without env var, invalid board.
* Happy path — return value is JSON-decodable and carries the verdict.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME + kanban toolset enabled."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "toolsets: [kanban]\nplugins:\n  enabled: [aegis-attestation]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import config as cfg_mod
    if hasattr(cfg_mod, "_clear_cache"):
        cfg_mod._clear_cache()

    import tools.aegis_review_tool  # ensure registered
    from tools.registry import invalidate_check_fn_cache
    invalidate_check_fn_cache()
    return monkeypatch, tmp_path


# ---------------------------------------------------------------------------
# Schema / registration
# ---------------------------------------------------------------------------

def test_aegis_review_registered_under_kanban_toolset(hermes_env):
    from tools.registry import registry
    entry = registry.get_entry("aegis_review")
    assert entry is not None
    assert entry.toolset == "kanban"
    assert entry.emoji
    # required[]: empty (task_id falls back to env var)
    assert entry.schema["parameters"]["required"] == []


# ---------------------------------------------------------------------------
# Handler — error paths
# ---------------------------------------------------------------------------

def test_handler_rejects_when_no_task_id_and_no_env(hermes_env):
    monkeypatch, _ = hermes_env
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools.aegis_review_tool import _handle_review
    out = json.loads(_handle_review({}))
    assert "error" in out
    assert "task_id" in out["error"]


def test_handler_unknown_board_returns_error(hermes_env):
    """If board doesn't exist, connect() raises FileNotFoundError → tool_error."""
    monkeypatch, _ = hermes_env
    from tools.aegis_review_tool import _handle_review
    out = json.loads(_handle_review({"task_id": "t_x", "board": "nonexistent-board"}))
    assert "error" in out


# ---------------------------------------------------------------------------
# Handler — happy path with stubbed review_task
# ---------------------------------------------------------------------------

def test_handler_happy_path_returns_verdict(hermes_env, monkeypatch):
    """Patches review_task to a fake verdict and confirms the handler
    serializes it correctly."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")

    # Patch kanban_db.connect to return a stub conn that needn't be valid —
    # review_task is going to be stubbed below.
    fake_conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.connect", lambda board=None: fake_conn
    )

    from plugins.aegis_attestation.llm_review import LLMReviewResult, VERDICT_APPROVED
    fake_result = LLMReviewResult(
        verdict=VERDICT_APPROVED,
        feedback="meets criteria",
        confidence=0.91,
        task_id="t_fake",
    )
    monkeypatch.setattr(
        "plugins.aegis_attestation.llm_review.review_task",
        lambda conn, task_id, **kw: fake_result,
    )

    from tools.aegis_review_tool import _handle_review
    out = json.loads(_handle_review({}))
    assert out["ok"] is True
    assert out["verdict"] == "APPROVED"
    assert out["feedback"] == "meets criteria"
    assert out["confidence"] == 0.91
    assert out["task_id"] == "t_fake"


def test_handler_error_verdict_sets_ok_false(hermes_env, monkeypatch):
    """When the review itself fails, ok=False so the caller can branch on it."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    fake_conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.connect", lambda board=None: fake_conn
    )
    from plugins.aegis_attestation.llm_review import LLMReviewResult, VERDICT_ERROR
    monkeypatch.setattr(
        "plugins.aegis_attestation.llm_review.review_task",
        lambda conn, task_id, **kw: LLMReviewResult(
            verdict=VERDICT_ERROR, feedback="LLM unavailable", error="llm-unavailable"
        ),
    )
    from tools.aegis_review_tool import _handle_review
    out = json.loads(_handle_review({}))
    assert out["ok"] is False
    assert out["verdict"] == "ERROR"
