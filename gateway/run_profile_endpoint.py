"""HTTP endpoint: POST /api/v1/run-profile.

Lets external orchestrators (MC pipeline nodes, LangGraph nodes, n8n,
human curl) hand a unit of work to a Hermes profile and get back a
structured result. This is the Hermes side of the bridge for the
"swappable workflow engines" architecture documented in
``.claude/plans/2026-05-23T06-30__workflow-swappable-implementation.md``.

Wire:
  external system ─POST /api/v1/run-profile─▶ this endpoint ─▶
    creates kanban task on board ``bridge-runs`` (auto-init) with
    assignee=<profile> ─▶ Hermes dispatcher spawns the profile worker
    ─▶ worker calls kanban_complete(result=...) ─▶ this endpoint
    returns the result.

Sync mode (default): blocks up to ``timeout_sec`` seconds polling for
terminal status.

Async mode: returns immediately with ``{run_id}``. Client polls
``GET /api/v1/run-profile/<run_id>`` until terminal.

Idempotent board creation. No state stored outside the kanban DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants

BRIDGE_BOARD_SLUG = "bridge-runs"
BRIDGE_BOARD_NAME = "Workflow Bridge Runs"
BRIDGE_BOARD_DESC = (
    "External-orchestrator → Hermes-profile bridge. Each task here was "
    "created by POST /api/v1/run-profile from an MC pipeline node, "
    "LangGraph adapter, or other workflow backend. Workers run the "
    "named profile to completion; results are returned to the caller "
    "via the HTTP response body."
)

# Status buckets — terminal vs in-flight.
TERMINAL_STATUSES = {"done", "blocked", "failed", "timed_out", "crashed"}

DEFAULT_TIMEOUT_SEC = 600
MAX_TIMEOUT_SEC = 3600
POLL_INTERVAL_SEC = 1.0


# ---------------------------------------------------------------------------
# Bridge board bootstrap

def ensure_bridge_board() -> None:
    """Idempotently create the ``bridge-runs`` board.

    Safe to call on every request — ``create_board`` is mkdir-p semantics.
    """
    from hermes_cli import kanban_db
    kanban_db.create_board(
        BRIDGE_BOARD_SLUG,
        name=BRIDGE_BOARD_NAME,
        description=BRIDGE_BOARD_DESC,
        meta_extra={"kind": "bridge", "lifetime": "permanent"},
    )


# ---------------------------------------------------------------------------
# Validation

def _validate_payload(payload: dict[str, Any]) -> tuple[Optional[str], dict[str, Any]]:
    """Return ``(error_message_or_None, sanitized_payload)``."""
    if not isinstance(payload, dict):
        return "payload must be a JSON object", {}

    profile = payload.get("profile")
    if not profile or not isinstance(profile, str):
        return "missing required: profile (string)", {}

    title = payload.get("title")
    if not title or not isinstance(title, str):
        return "missing required: title (string)", {}

    body = payload.get("body")
    if not body or not isinstance(body, str):
        return "missing required: body (string)", {}

    timeout_sec = payload.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    if not isinstance(timeout_sec, (int, float)) or timeout_sec <= 0:
        return "timeout_sec must be a positive number", {}
    timeout_sec = min(int(timeout_sec), MAX_TIMEOUT_SEC)

    inputs = payload.get("inputs")
    if inputs is not None and not isinstance(inputs, dict):
        return "inputs must be a JSON object (or omitted)", {}

    async_mode = bool(payload.get("async", False))

    return None, {
        "profile": profile,
        "title": title.strip(),
        "body": body,
        "inputs": inputs or {},
        "timeout_sec": timeout_sec,
        "async": async_mode,
    }


# ---------------------------------------------------------------------------
# Task creation

def _compose_body(body: str, inputs: dict[str, Any]) -> str:
    """Append optional ``inputs`` as a fenced JSON block at the end of body."""
    if not inputs:
        return body
    return (
        f"{body.rstrip()}\n\n"
        f"## inputs (workflow node arguments)\n\n"
        f"```json\n{json.dumps(inputs, ensure_ascii=False, indent=2)}\n```\n"
    )


def create_bridge_task(
    *,
    profile: str,
    title: str,
    body: str,
    inputs: dict[str, Any],
) -> str:
    """Create a kanban task on the bridge board. Returns ``task_id``.

    Raises ``ValueError`` if profile doesn't exist on disk (avoids the
    silent ``skipped_nonspawnable`` stall — see dispatch_once in
    kanban_db.py).
    """
    from hermes_cli import kanban_db
    from hermes_cli.profiles import profile_exists

    if not profile_exists(profile):
        raise ValueError(
            f"profile '{profile}' is not on disk under /opt/data/profiles/. "
            f"Cannot dispatch — dispatcher would silently skip."
        )

    ensure_bridge_board()
    conn = kanban_db.connect(board=BRIDGE_BOARD_SLUG)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=title,
            body=_compose_body(body, inputs),
            assignee=profile,
            created_by="run-profile-endpoint",
            workspace_kind="scratch",
        )
    finally:
        conn.close()
    return task_id


def get_task_snapshot(task_id: str) -> Optional[dict[str, Any]]:
    """Return a dict snapshot of the task, or None if missing.

    Always reads from the bridge board (we only create tasks here, so a
    task with this id should only exist on this board). For
    cross-board lookups, use kanban_db.find_task_globally.
    """
    from hermes_cli import kanban_db
    conn = kanban_db.connect(board=BRIDGE_BOARD_SLUG)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            return None
        # Surface enough fields for the HTTP response.
        return {
            "id": task.id,
            "status": task.status,
            "assignee": task.assignee,
            "result": task.result,
            "consecutive_failures": task.consecutive_failures,
            "last_failure_error": task.last_failure_error,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Polling

async def wait_for_terminal(
    task_id: str,
    timeout_sec: int,
    poll_interval_sec: float = POLL_INTERVAL_SEC,
) -> dict[str, Any]:
    """Poll the task until terminal status or timeout.

    Returns the final snapshot. On timeout, returns the latest snapshot
    with status still non-terminal so the caller can decide to retry.
    """
    deadline = time.monotonic() + timeout_sec
    last_snap: dict[str, Any] = {}
    while True:
        snap = get_task_snapshot(task_id)
        if snap is None:
            # Vanished — return synthetic
            return {
                "id": task_id,
                "status": "vanished",
                "error": "task no longer present on bridge board",
            }
        last_snap = snap
        if snap["status"] in TERMINAL_STATUSES:
            return snap
        if time.monotonic() >= deadline:
            return last_snap
        await asyncio.sleep(poll_interval_sec)


# ---------------------------------------------------------------------------
# aiohttp handler

async def handle_run_profile(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/v1/run-profile — create + (optionally) await."""
    from aiohttp import web

    try:
        payload = await request.json()
    except Exception as e:
        return web.json_response(
            {"error": f"invalid JSON body: {e}"}, status=400
        )

    err, sanitized = _validate_payload(payload)
    if err is not None:
        return web.json_response({"error": err}, status=400)

    try:
        task_id = create_bridge_task(
            profile=sanitized["profile"],
            title=sanitized["title"],
            body=sanitized["body"],
            inputs=sanitized["inputs"],
        )
    except ValueError as e:
        # Profile-not-on-disk falls here.
        return web.json_response({"error": str(e)}, status=503)
    except Exception as e:
        logger.exception("[run-profile] create_bridge_task failed")
        return web.json_response(
            {"error": f"create failed: {e}"}, status=500
        )

    if sanitized["async"]:
        return web.json_response(
            {"run_id": task_id, "status": "ready",
             "poll_url": f"/api/v1/run-profile/{task_id}"},
            status=202,
        )

    final = await wait_for_terminal(task_id, sanitized["timeout_sec"])
    if final["status"] in TERMINAL_STATUSES:
        return web.json_response(
            {"run_id": task_id,
             "status": final["status"],
             "result": final.get("result"),
             "error": final.get("last_failure_error"),
             },
            status=200 if final["status"] == "done" else 200,  # 200 for both — caller inspects status
        )
    # Timed out before terminal — return 408 so caller knows it can poll later.
    return web.json_response(
        {"run_id": task_id,
         "status": final["status"],
         "message": (
             f"task still {final['status']} after "
             f"{sanitized['timeout_sec']}s; poll "
             f"GET /api/v1/run-profile/{task_id} for completion"
         ),
         },
        status=408,
    )


async def handle_get_run_profile(request) -> "web.Response":  # type: ignore[name-defined]
    """GET /api/v1/run-profile/<run_id> — poll status."""
    from aiohttp import web

    run_id = request.match_info.get("run_id")
    if not run_id:
        return web.json_response({"error": "missing run_id"}, status=400)

    snap = get_task_snapshot(run_id)
    if snap is None:
        return web.json_response(
            {"error": f"run_id {run_id} not found on bridge board"},
            status=404,
        )
    return web.json_response(
        {"run_id": run_id,
         "status": snap["status"],
         "result": snap.get("result"),
         "error": snap.get("last_failure_error"),
         "assignee": snap.get("assignee"),
         },
        status=200,
    )
