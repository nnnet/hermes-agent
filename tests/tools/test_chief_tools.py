"""Tests for the chief tool surface (tools/chief_tools.py).

Phase 0+1 POC coverage:
  - chief_spawn creates a board with kind=chief metadata + a ready initial task.
  - chief_status aggregates the board correctly (alive, stage, counts).
  - chief_list filters to chief boards only.
  - chief_terminate (cascade policy) recursively archives sub-chiefs.
  - Recursion depth guard rejects deeply-nested spawn.
  - Schema validation errors are returned as tool_error JSON, not exceptions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures: isolated HERMES_HOME so every test gets a fresh kanban root.
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_env(monkeypatch, tmp_path):
    """Point Hermes at a clean temp HOME, return the kanban_db module."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Profile config with kanban toolset enabled so check_fn passes.
    cfg = home / "config.yaml"
    cfg.write_text("toolsets: [kanban]\n", encoding="utf-8")
    # Make sure the env var that gates worker-only kanban tools is OFF —
    # chief tools should be visible without it (orchestrator mode).
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    # Reset config cache so our test config.yaml is read on every test
    # (load_config is mtime-cached at module level).
    from hermes_cli import config as cfg_mod
    if hasattr(cfg_mod, "_clear_cache"):
        cfg_mod._clear_cache()

    import tools.chief_tools as chief_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache
    invalidate_check_fn_cache()
    from hermes_cli import kanban_db
    return chief_tools, kanban_db


def _parse(result: str) -> dict:
    """Tool handlers return JSON strings — decode + sanity assertions."""
    assert isinstance(result, str), f"expected str, got {type(result)}"
    return json.loads(result)


# ---------------------------------------------------------------------------
# chief_spawn
# ---------------------------------------------------------------------------

def test_chief_spawn_creates_board_and_initial_task(hermes_env):
    chief_tools, kanban_db = hermes_env
    out = _parse(chief_tools._handle_chief_spawn({
        "name": "yt-indexer",
        "brief": "Index every video on @ExampleChannel and store transcripts.",
    }))
    assert out["ok"] is True
    chief_id = out["chief_id"]
    assert chief_id.startswith("chief-yt-indexer-")
    assert out["board"] == chief_id
    assert out["lifetime"] == "ephemeral"  # default
    assert out["terminate_policy"] == "cascade"  # default
    assert out["parent_chief_id"] is None

    # Board metadata persisted with kind=chief
    meta = kanban_db.read_board_metadata(chief_id)
    assert meta["kind"] == "chief"
    assert meta["lifetime"] == "ephemeral"
    assert meta["terminate_policy"] == "cascade"
    assert meta["spawned_at"] is not None

    # Initial task created with correct assignee + status
    conn = kanban_db.connect(board=chief_id)
    try:
        rows = conn.execute(
            "SELECT id, title, body, assignee, status FROM tasks"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["assignee"] == "chief-manager"
    assert r["status"] == "ready"
    assert "Index every video" in r["body"]


def test_chief_spawn_missing_required_args(hermes_env):
    chief_tools, _ = hermes_env
    # missing brief
    r = _parse(chief_tools._handle_chief_spawn({"name": "foo"}))
    assert r.get("error") and "brief" in r["error"]
    # missing name
    r = _parse(chief_tools._handle_chief_spawn({"brief": "do something"}))
    assert r.get("error") and "name" in r["error"]


def test_chief_spawn_validates_lifetime(hermes_env):
    chief_tools, _ = hermes_env
    r = _parse(chief_tools._handle_chief_spawn({
        "name": "x", "brief": "y", "lifetime": "forever"
    }))
    assert r.get("error") and "lifetime" in r["error"]


def test_chief_spawn_rejects_unknown_parent(hermes_env):
    chief_tools, _ = hermes_env
    r = _parse(chief_tools._handle_chief_spawn({
        "name": "x", "brief": "y", "parent_chief_id": "chief-fake-zzz"
    }))
    assert r.get("error") and "parent_chief_id" in r["error"]


def test_chief_spawn_accepts_profile_override(hermes_env, monkeypatch, tmp_path):
    """`profile=…` arg routes the initial task to a non-default chief."""
    chief_tools, kanban_db = hermes_env
    # Pre-seed an alternate profile dir so the existence check passes.
    home = Path(os.environ["HERMES_HOME"])
    alt = home / "profiles" / "mc-pm-chief"
    alt.mkdir(parents=True, exist_ok=True)
    (alt / "config.yaml").write_text("toolsets: [kanban]\n", encoding="utf-8")

    out = _parse(chief_tools._handle_chief_spawn({
        "name": "mc-proj",
        "brief": "drive an MC project to completion",
        "profile": "mc-pm-chief",
    }))
    assert out["ok"] is True
    chief_id = out["chief_id"]

    conn = kanban_db.connect(board=chief_id)
    try:
        row = dict(conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?",
            (out["initial_task"],),
        ).fetchone())
    finally:
        conn.close()
    # Assignee = the override, NOT the default 'chief-manager'.
    assert row["assignee"] == "mc-pm-chief"


def test_chief_spawn_rejects_unknown_profile(hermes_env):
    """Typo'd `profile=` returns a clear tool_error, doesn't silently fall
    back to chief-manager (which would mask the operator's intent)."""
    chief_tools, _ = hermes_env
    r = _parse(chief_tools._handle_chief_spawn({
        "name": "x",
        "brief": "y",
        "profile": "definitely-not-a-real-profile",
    }))
    assert r.get("error") and "profile" in r["error"].lower()


def test_chief_spawn_recursion_depth_guard(hermes_env):
    chief_tools, kanban_db = hermes_env
    # Create three nested chiefs — fourth should be rejected.
    root = _parse(chief_tools._handle_chief_spawn({
        "name": "root", "brief": "..."
    }))["chief_id"]
    lvl1 = _parse(chief_tools._handle_chief_spawn({
        "name": "lvl1", "brief": "...", "parent_chief_id": root
    }))["chief_id"]
    lvl2 = _parse(chief_tools._handle_chief_spawn({
        "name": "lvl2", "brief": "...", "parent_chief_id": lvl1
    }))["chief_id"]
    # 4th level rejected.
    r = _parse(chief_tools._handle_chief_spawn({
        "name": "lvl3", "brief": "...", "parent_chief_id": lvl2
    }))
    assert r.get("error") and "recursion depth" in r["error"]


# ---------------------------------------------------------------------------
# chief_status
# ---------------------------------------------------------------------------

def test_chief_status_reflects_initial_task_state(hermes_env):
    chief_tools, kanban_db = hermes_env
    cid = _parse(chief_tools._handle_chief_spawn({
        "name": "test", "brief": "do work"
    }))["chief_id"]

    s = _parse(chief_tools._handle_chief_status({"chief_id": cid}))
    assert s["ok"] is True
    assert s["chief_id"] == cid
    assert s["alive"] is True
    assert s["initial_status"] == "ready"
    assert s["subtasks_total"] == 1
    assert s["subtasks_open"] == 1
    assert s["subtasks_done"] == 0
    assert s["stage"] == "queued"  # ready = queued (dispatcher hasn't claimed)


def test_chief_status_after_initial_task_done(hermes_env):
    chief_tools, kanban_db = hermes_env
    cid = _parse(chief_tools._handle_chief_spawn({
        "name": "test", "brief": "do work"
    }))["chief_id"]
    # Mark the initial task done directly via SQL.
    conn = kanban_db.connect(board=cid)
    try:
        conn.execute("UPDATE tasks SET status = 'done'")
        conn.commit()
    finally:
        conn.close()

    s = _parse(chief_tools._handle_chief_status({"chief_id": cid}))
    assert s["alive"] is False
    assert s["initial_status"] == "done"
    assert s["subtasks_done"] == 1
    assert s["subtasks_open"] == 0
    assert s["stage"] == "completed"


def test_chief_status_rejects_non_chief_board(hermes_env):
    chief_tools, kanban_db = hermes_env
    kanban_db.create_board("ordinary-board")
    r = _parse(chief_tools._handle_chief_status({"chief_id": "ordinary-board"}))
    assert r.get("error") and "not a known chief" in r["error"]


# ---------------------------------------------------------------------------
# chief_list
# ---------------------------------------------------------------------------

def test_chief_list_filters_to_chiefs_only(hermes_env):
    chief_tools, kanban_db = hermes_env
    # Create one chief + one plain board.
    cid = _parse(chief_tools._handle_chief_spawn({
        "name": "a", "brief": "..."
    }))["chief_id"]
    kanban_db.create_board("plain-board")

    out = _parse(chief_tools._handle_chief_list({}))
    assert out["ok"] is True
    slugs = {c["chief_id"] for c in out["chiefs"]}
    assert cid in slugs
    assert "plain-board" not in slugs
    # The default board should never appear either.
    assert "default" not in slugs


def test_chief_list_shows_parent_link(hermes_env):
    chief_tools, _ = hermes_env
    root = _parse(chief_tools._handle_chief_spawn({
        "name": "root", "brief": "..."
    }))["chief_id"]
    child = _parse(chief_tools._handle_chief_spawn({
        "name": "child", "brief": "...", "parent_chief_id": root
    }))["chief_id"]
    out = _parse(chief_tools._handle_chief_list({}))
    by_id = {c["chief_id"]: c for c in out["chiefs"]}
    assert by_id[child]["parent_chief_id"] == root
    assert by_id[root]["parent_chief_id"] is None


# ---------------------------------------------------------------------------
# chief_terminate (cascade)
# ---------------------------------------------------------------------------

def test_chief_terminate_cascade_archives_descendants(hermes_env):
    chief_tools, kanban_db = hermes_env
    root = _parse(chief_tools._handle_chief_spawn({
        "name": "root", "brief": "..."
    }))["chief_id"]
    child = _parse(chief_tools._handle_chief_spawn({
        "name": "child", "brief": "...", "parent_chief_id": root
    }))["chief_id"]
    grandchild = _parse(chief_tools._handle_chief_spawn({
        "name": "grand", "brief": "...", "parent_chief_id": child
    }))["chief_id"]

    # Terminate root → cascade should walk into child + grandchild.
    r = _parse(chief_tools._handle_chief_terminate({"chief_id": root}))
    assert r["ok"] is True
    assert r["terminated"] is True

    # All three boards no longer appear in active list_boards.
    active_slugs = {b["slug"] for b in kanban_db.list_boards(include_archived=False)}
    assert root not in active_slugs
    assert child not in active_slugs
    assert grandchild not in active_slugs

    # cascaded summary lists every terminated descendant
    def _collect(node):
        yield node["chief_id"]
        for sub in node.get("cascaded", []):
            yield from _collect(sub)
    assert {root, child, grandchild} == set(_collect(r))


def test_chief_terminate_rejects_independent_policy_in_mvp(hermes_env):
    chief_tools, _ = hermes_env
    cid = _parse(chief_tools._handle_chief_spawn({
        "name": "x", "brief": "y", "terminate_policy": "independent"
    }))["chief_id"]
    r = _parse(chief_tools._handle_chief_terminate({"chief_id": cid}))
    assert r.get("error") and "independent" in r["error"]


def test_chief_terminate_rejects_unknown_chief(hermes_env):
    chief_tools, _ = hermes_env
    r = _parse(chief_tools._handle_chief_terminate({"chief_id": "chief-fake-zzz"}))
    assert r.get("error") and "not a known chief" in r["error"]


# ---------------------------------------------------------------------------
# Registration / discoverability
# ---------------------------------------------------------------------------

def test_chief_tools_registered_under_kanban_toolset(hermes_env):
    """All chief tools must be present in the kanban toolset registry."""
    chief_tools, _ = hermes_env
    from tools.registry import registry
    expected = (
        "chief_spawn", "chief_status", "chief_list", "chief_terminate",
        "tg_send", "tg_ask", "tg_ask_status", "chief_answer_question",
    )
    for name in expected:
        entry = registry.get_entry(name)
        assert entry is not None, f"{name} not registered"
        assert entry.toolset == "kanban"
        assert entry.emoji  # set


# ---------------------------------------------------------------------------
# tg_send / tg_ask / tg_ask_status — chief→operator messaging (Phase 2)
# ---------------------------------------------------------------------------

@pytest.fixture
def chief_worker_env(hermes_env, monkeypatch):
    """Spawn a root chief, then enter its worker context (env vars set
    by the dispatcher) — so tg_* gates pass."""
    chief_tools, kanban_db = hermes_env
    spawn = _parse(chief_tools._handle_chief_spawn({
        "name": "ops", "brief": "do ops", "operator_chat_id": 1234567,
    }))
    chief_id = spawn["chief_id"]
    task_id = spawn["initial_task"]
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", chief_id)
    monkeypatch.setenv("HERMES_OPERATOR_CHAT_ID", "1234567")
    monkeypatch.setenv("HERMES_HITL_BRIDGE_URL", "http://hitl-stub")
    from tools.registry import invalidate_check_fn_cache
    invalidate_check_fn_cache()
    return chief_tools, chief_id, task_id


def test_tg_send_rejects_bad_intent(chief_worker_env, monkeypatch):
    chief_tools, _, _ = chief_worker_env
    r = _parse(chief_tools._handle_tg_send({
        "text": "x" * 50, "intent": "casual_chat",
    }))
    assert r.get("error") and "intent" in r["error"]


def test_tg_send_rejects_short_text(chief_worker_env):
    chief_tools, _, _ = chief_worker_env
    r = _parse(chief_tools._handle_tg_send({
        "text": "ok",  # <30
        "intent": "milestone",
    }))
    assert r.get("error") and "30" in r["error"]


def test_tg_send_rejects_under_chief(hermes_env, monkeypatch):
    """Under-chiefs must NOT have access to tg_send. The handler should
    reject them with a clear hint to surface through parent."""
    chief_tools, _ = hermes_env
    # Spawn parent, then under-chief.
    parent = _parse(chief_tools._handle_chief_spawn({
        "name": "p", "brief": "parent", "operator_chat_id": 1,
    }))
    under = _parse(chief_tools._handle_chief_spawn({
        "name": "u", "brief": "under", "parent_chief_id": parent["chief_id"],
    }))
    # Enter under-chief worker context.
    monkeypatch.setenv("HERMES_KANBAN_TASK", under["initial_task"])
    monkeypatch.setenv("HERMES_KANBAN_BOARD", under["chief_id"])
    monkeypatch.setenv("HERMES_OPERATOR_CHAT_ID", "1")
    from tools.registry import invalidate_check_fn_cache
    invalidate_check_fn_cache()
    r = _parse(chief_tools._handle_tg_send({
        "text": "valid text long enough to pass minimum length",
        "intent": "milestone",
    }))
    assert r.get("error") and "under-chiefs" in r["error"]


def test_tg_send_rejects_when_no_operator_chat_id(chief_worker_env, monkeypatch):
    chief_tools, _, _ = chief_worker_env
    monkeypatch.delenv("HERMES_OPERATOR_CHAT_ID", raising=False)
    r = _parse(chief_tools._handle_tg_send({
        "text": "valid text long enough to pass minimum length check ok",
        "intent": "milestone",
    }))
    assert r.get("error") and "OPERATOR_CHAT_ID" in r["error"]


def test_tg_send_posts_to_bridge_on_happy_path(chief_worker_env, monkeypatch):
    """Mock httpx so we don't need a live bridge; verify POST shape."""
    chief_tools, chief_id, task_id = chief_worker_env
    captured = {}

    class _FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {"ok": True, "req_id": "abc", "tg_message_id": 42}

    class _FakeClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    r = _parse(chief_tools._handle_tg_send({
        "text": "Phase 1 complete: indexed 500 documents in 12 min.",
        "intent": "milestone",
    }))
    assert r.get("ok") and r["delivered"] is True
    assert r["req_id"] == "abc"
    assert captured["url"].endswith("/chief/send")
    body = captured["body"]
    assert body["chief_id"] == chief_id
    assert body["task_id"] == task_id
    assert body["chat_id"] == 1234567
    assert body["intent"] == "milestone"


def test_tg_ask_returns_req_id_for_polling(chief_worker_env, monkeypatch):
    chief_tools, *_ = chief_worker_env

    class _FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {
                "ok": True, "req_id": "q1",
                "expires_at": 12345.0, "timeout_sec": 600,
            }

    class _FakeClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json=None):
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    r = _parse(chief_tools._handle_tg_ask({
        "question": "Should I deploy to prod now, or wait for tomorrow's window?",
        "intent": "decision_required",
    }))
    assert r.get("ok") and r["req_id"] == "q1"


def test_tg_ask_status_returns_resolved_payload(chief_worker_env, monkeypatch):
    chief_tools, *_ = chief_worker_env

    class _FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {
                "ok": True, "status": "resolved",
                "decision": "answered", "answer": "go ahead",
                "decided_at": 1.0, "expires_at": 2.0,
            }

    class _FakeClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url):
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    r = _parse(chief_tools._handle_tg_ask_status({"req_id": "q1"}))
    assert r["status"] == "resolved"
    assert r["answer"] == "go ahead"


def test_tg_send_rate_limited_passthrough(chief_worker_env, monkeypatch):
    """Bridge 429 should surface as a helpful tool_error."""
    chief_tools, *_ = chief_worker_env

    class _FakeResp:
        status_code = 429
        text = ""
        def json(self):
            return {
                "error": "rate_limited",
                "hint": "you hit 2/hour — use kanban_comment instead",
            }

    class _FakeClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json=None):
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    r = _parse(chief_tools._handle_tg_send({
        "text": "Some milestone message that easily clears the threshold.",
        "intent": "milestone",
    }))
    assert r.get("error") and "rate-limited" in r["error"]
    assert "kanban_comment" in r["error"]


def test_chief_spawn_persists_operator_chat_id(hermes_env):
    chief_tools, kanban_db = hermes_env
    r = _parse(chief_tools._handle_chief_spawn({
        "name": "ops", "brief": "do ops", "operator_chat_id": 9876543,
    }))
    assert r["operator_chat_id"] == 9876543
    meta = kanban_db.read_board_metadata(r["chief_id"])
    assert meta.get("operator_chat_id") == 9876543


def test_chief_spawn_inherits_operator_chat_id_from_parent(hermes_env):
    chief_tools, kanban_db = hermes_env
    parent = _parse(chief_tools._handle_chief_spawn({
        "name": "p", "brief": "parent", "operator_chat_id": 555,
    }))
    child = _parse(chief_tools._handle_chief_spawn({
        "name": "c", "brief": "child", "parent_chief_id": parent["chief_id"],
        # NOTE: no operator_chat_id passed — should inherit
    }))
    assert child["operator_chat_id"] == 555


def test_chief_answer_question_forwards_to_bridge(hermes_env, monkeypatch):
    chief_tools, _ = hermes_env
    captured = {}

    class _FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {"ok": True, "req_id": "q1", "decision": "answered"}

    class _FakeClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    r = _parse(chief_tools._handle_chief_answer_question({
        "req_id": "q1", "decision": "answered", "answer": "go ahead",
    }))
    assert r.get("ok") and r["delivered"] is True
    assert captured["url"].endswith("/chief/answer")
    assert captured["body"]["answer"] == "go ahead"


def test_chief_answer_question_requires_answer_for_answered(hermes_env):
    chief_tools, _ = hermes_env
    r = _parse(chief_tools._handle_chief_answer_question({
        "req_id": "q1", "decision": "answered",
        # no 'answer' field
    }))
    assert r.get("error") and "answer" in r["error"]


# ---------------------------------------------------------------------------
# Followup script availability — chief_tools exposes the constants so
# Hermes-main / docs can reference them. The cron itself is created by
# the assistant on its own initiative (via `cronjob: create`), not by us.
# ---------------------------------------------------------------------------

def test_chief_supervisor_script_constants_are_exposed(hermes_env):
    chief_tools, _ = hermes_env
    assert chief_tools.CHIEF_SUPERVISOR_SCRIPT.endswith(".py")
    assert chief_tools.CHIEF_SUPERVISOR_DEFAULT_SCHEDULE.startswith("every ")
