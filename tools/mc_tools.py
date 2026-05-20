"""MC (Mission Control) integration tools — Hermes ↔ MC bridge primitives.

A "Mission Control" instance is an external Next.js+SQLite execution backend
that exposes 143 REST endpoints (agents, pipelines, workflows, cron, etc.)
for multi-framework AI orchestration. This module ships the smallest useful
slice — `mc_pipeline_run` — so a Hermes chief (or main:manager) can
delegate heavy work to MC without pulling in any new Python dependencies.

Architecture (see `_runtime-notes/merge-and-integration-plan.md` in the
paperclip repo):

    human ─voice──▶  Hermes main:manager      ← single human-facing channel
                          │ chief_spawn(brief)
                          ▼
                     Chief (own kanban board + worker)
                          │
                          ├─ kanban_create — sub-tasks on own board
                          ├─ chief_spawn — sub-chief
                          └─ mc_pipeline_run — heavy workflow in MC
                                │
                                ▼
                          ┌──────────────────────────────────┐
                          │ MC (external execution backend)  │
                          │   /api/pipelines/run             │
                          │   CrewAI / LangGraph / AutoGen   │
                          │   agents inside MC               │
                          └──────────────────────────────────┘

The two systems are COMPLEMENTARY:
* chief — light in-Hermes coordination, immediate visibility via kanban
* MC    — multi-framework execution, heavy long-running pipelines

Config (env vars):
    HERMES_MC_BASE_URL        e.g. http://localhost:3000  (no trailing slash)
    HERMES_MC_API_KEY         operator-role API key from MC web UI
    HERMES_MC_TIMEOUT_SEC     default 30; HTTP request timeout

When HERMES_MC_BASE_URL is unset, every tool returns a graceful "MC not
configured" error — Hermes stays usable without MC.

Test:
    pytest tests/tools/test_mc_tools.py -v -o "addopts="
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional
from urllib import error as _urllib_error
from urllib import request as _urllib_request

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating: MC tools available wherever chief tools are available — same
# orchestrator-or-chief contract. main:manager profiles AND in-process chiefs
# can both delegate; plain workers cannot (they should focus on their task).
# ---------------------------------------------------------------------------

def _check_mc_mode() -> bool:
    """Why: We piggyback on the existing `_check_chief_mode` semantics so an
    operator who's already configured the chief toolset gets MC tools too,
    no new toggle to learn.
    What: Returns True if the current profile is an orchestrator (has
    kanban in toolsets) OR if we are running INSIDE a chief worker
    (HERMES_KANBAN_TASK env var set).
    Test: tests/tools/test_mc_tools.py — covers both env paths.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP helper — stdlib-only, no requests dependency. MC speaks JSON over HTTP.
# ---------------------------------------------------------------------------

def _mc_config() -> tuple[Optional[str], Optional[str], int]:
    """Why: Single config-resolution point for every MC call. Defaults
    chosen so that an undeployed MC produces a clean 'not configured'
    error instead of a confusing connection-refused trace.
    What: Reads HERMES_MC_BASE_URL, HERMES_MC_API_KEY, HERMES_MC_TIMEOUT_SEC.
    Strips trailing slash from base_url so callers can `f"{base}/api/..."`
    without double slashes.
    Test: test_mc_config_defaults, test_mc_config_strips_trailing_slash.
    """
    base = os.environ.get("HERMES_MC_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("HERMES_MC_API_KEY", "").strip()
    try:
        timeout = int(os.environ.get("HERMES_MC_TIMEOUT_SEC", "30"))
    except (ValueError, TypeError):
        timeout = 30
    return (base or None, key or None, timeout)


def _mc_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Why: Centralised HTTP POST so every MC tool gets the same headers,
    timeout handling, JSON encoding, and error shape. Tools just compose
    a path + payload and let this helper raise/return.
    What: POSTs `payload` (JSON) to `{base_url}{path}` with the Authorization
    header set to `Bearer {api_key}`. Returns the JSON-decoded response.
    Raises on any non-2xx status with a structured exception message
    that the calling tool can wrap in tool_error().
    Test: test_mc_post_happy, test_mc_post_4xx_carries_body,
    test_mc_post_connection_refused.
    """
    base, key, timeout = _mc_config()
    if base is None:
        raise RuntimeError(
            "MC not configured — set HERMES_MC_BASE_URL in ~/.hermes/.env "
            "(and optionally HERMES_MC_API_KEY for authenticated tenants)"
        )

    url = f"{base}{path}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "hermes-mc-tools/0.1",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"

    body = json.dumps(payload).encode("utf-8")
    req = _urllib_request.Request(url, data=body, headers=headers, method="POST")

    try:
        with _urllib_request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                # Non-JSON 2xx — return as text payload so caller can
                # still surface something useful.
                return {"_raw_text": raw, "_warning": "non-json response"}
    except _urllib_error.HTTPError as e:
        # Try to parse error body — MC returns structured JSON on errors.
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover  — best-effort error extract
            pass
        raise RuntimeError(
            f"MC {path} returned HTTP {e.code}: {err_body[:300]}"
        ) from e
    except _urllib_error.URLError as e:
        raise RuntimeError(
            f"MC {path} unreachable at {url}: {e.reason}"
        ) from e


def _mc_get(path: str) -> dict[str, Any]:
    """GET counterpart to `_mc_post`. Same headers/timeout/error shape,
    no payload. Used by list/inspect tools.
    """
    base, key, timeout = _mc_config()
    if base is None:
        raise RuntimeError(
            "MC not configured — set HERMES_MC_BASE_URL in ~/.hermes/.env "
            "(and optionally HERMES_MC_API_KEY for authenticated tenants)"
        )

    url = f"{base}{path}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "hermes-mc-tools/0.1",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = _urllib_request.Request(url, headers=headers, method="GET")
    try:
        with _urllib_request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                return {"_raw_text": raw, "_warning": "non-json response"}
    except _urllib_error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            pass
        raise RuntimeError(
            f"MC {path} returned HTTP {e.code}: {err_body[:300]}"
        ) from e
    except _urllib_error.URLError as e:
        raise RuntimeError(
            f"MC {path} unreachable at {url}: {e.reason}"
        ) from e


# ---------------------------------------------------------------------------
# Tool: mc_pipeline_run
# ---------------------------------------------------------------------------

def _resolve_pipeline_id(pipeline_name: str) -> int:
    """Resolve a human-readable pipeline name to its numeric MC id by
    walking GET /api/pipelines. Raises RuntimeError on not-found or
    transport failure — caller wraps in tool_error().
    """
    result = _mc_get("/api/pipelines")
    pipelines = result.get("pipelines", result if isinstance(result, list) else [])
    if not isinstance(pipelines, list):
        raise RuntimeError(
            f"MC /api/pipelines returned unexpected shape: {type(pipelines).__name__}"
        )
    for p in pipelines:
        if isinstance(p, dict) and p.get("name") == pipeline_name:
            pid = p.get("id")
            if isinstance(pid, int):
                return pid
            raise RuntimeError(
                f"pipeline '{pipeline_name}' has non-integer id: {pid!r}"
            )
    available = ", ".join(
        repr(p.get("name")) for p in pipelines if isinstance(p, dict)
    ) or "(none registered)"
    raise RuntimeError(
        f"pipeline '{pipeline_name}' not found in MC. Available: {available}"
    )


def _handle_mc_pipeline_run(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    """Why: Delegate a heavy workflow to MC's pipelines runner — chief
    decides the work needs multi-framework orchestration (CrewAI /
    LangGraph / AutoGen agents inside MC) instead of the lighter
    in-Hermes path. Tool kicks off the pipeline and returns the new
    run's `run_id` so the chief can poll via `mc_pipeline_status` or
    cancel via `mc_pipeline_cancel`.
    What: POSTs `{action: "start", pipeline_id}` to MC's
    `/api/pipelines/run`. Accepts EITHER `pipeline_name` (resolved
    to id via GET /api/pipelines) OR `pipeline_id` directly. MC's
    pipelines do not accept runtime `inputs` — all configuration
    lives in the pipeline template's workflow_templates rows. Tool
    surfaces the new run object as `{ok, run_id, status, pipeline_id,
    pipeline_name, _raw}`.
    Test: TestMcPipelineRun.*
    """
    pipeline_id = args.get("pipeline_id")
    pipeline_name = args.get("pipeline_name") or args.get("pipeline")

    if pipeline_id is not None:
        if not isinstance(pipeline_id, int) or pipeline_id <= 0:
            return tool_error(
                "mc_pipeline_run: 'pipeline_id' must be a positive integer"
            )
    elif isinstance(pipeline_name, str) and pipeline_name:
        try:
            pipeline_id = _resolve_pipeline_id(pipeline_name)
        except RuntimeError as e:
            return tool_error(f"mc_pipeline_run: {e}")
    else:
        return tool_error(
            "mc_pipeline_run: provide either 'pipeline_name' (string) "
            "or 'pipeline_id' (integer)"
        )

    try:
        result = _mc_post(
            "/api/pipelines/run",
            {"action": "start", "pipeline_id": pipeline_id},
        )
    except RuntimeError as e:
        return tool_error(str(e))

    # MC returns `{run: {id, pipeline_id, status, current_step, ...}}`
    # on success. Normalise to flat keys the caller can consume without
    # walking a nested dict.
    run = result.get("run") if isinstance(result, dict) else None
    if not isinstance(run, dict):
        return tool_error(
            "mc_pipeline_run: MC response missing expected 'run' object; "
            f"raw: {str(result)[:200]}"
        )
    return {
        "ok": True,
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_name,
        "run_id": run.get("id"),
        "status": run.get("status"),
        "current_step": run.get("current_step"),
        "_raw": run,
    }


MC_PIPELINE_RUN_SCHEMA = {
    "name": "mc_pipeline_run",
    "description": (
        "Start a Mission Control (MC) pipeline run. Use this to delegate "
        "a heavy workflow (CrewAI / LangGraph / AutoGen multi-step "
        "orchestration) to MC. Returns the new `run_id` for polling via "
        "`mc_pipeline_status`. Requires HERMES_MC_BASE_URL (and "
        "HERMES_MC_API_KEY for authenticated tenants) in env. Use "
        "`mc_pipeline_list` first to discover available pipelines."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pipeline_name": {
                "type": "string",
                "description": (
                    "Human-readable pipeline name (e.g. 'code-review'). "
                    "Resolved to id via GET /api/pipelines. Provide this "
                    "OR pipeline_id."
                ),
            },
            "pipeline_id": {
                "type": "integer",
                "description": (
                    "Numeric MC pipeline id. Use this instead of "
                    "pipeline_name when the id is already known (e.g. "
                    "from a prior mc_pipeline_list call). Skips a lookup."
                ),
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Tool: mc_pipeline_status
# ---------------------------------------------------------------------------

def _handle_mc_pipeline_status(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    """Why: After `mc_pipeline_run` returns a run_id, the chief needs to
    poll until the pipeline finishes — without polling we lose
    end-to-end orchestration visibility. This tool reads MC's run
    state so the chief can decide to wait / advance other work /
    cancel.
    What: GETs MC `/api/pipelines/run?id={run_id}`. Returns the run
    descriptor including status, current_step, steps_snapshot, and
    completed_at. No state mutation.
    Test: TestMcPipelineStatus.*
    """
    run_id = args.get("run_id")
    if not isinstance(run_id, int) or run_id <= 0:
        return tool_error(
            "mc_pipeline_status: 'run_id' must be a positive integer"
        )

    try:
        result = _mc_get(f"/api/pipelines/run?id={run_id}")
    except RuntimeError as e:
        return tool_error(str(e))

    run = result.get("run") if isinstance(result, dict) else None
    if not isinstance(run, dict):
        return tool_error(
            "mc_pipeline_status: MC response missing expected 'run' object; "
            f"raw: {str(result)[:200]}"
        )
    return {
        "ok": True,
        "run_id": run_id,
        "status": run.get("status"),
        "current_step": run.get("current_step"),
        "pipeline_id": run.get("pipeline_id"),
        "pipeline_name": run.get("pipeline_name"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "steps_snapshot": run.get("steps_snapshot"),
        "_raw": run,
    }


MC_PIPELINE_STATUS_SCHEMA = {
    "name": "mc_pipeline_status",
    "description": (
        "Get the current status of a Mission Control pipeline run. Use "
        "this to poll a run started by `mc_pipeline_run`. Returns the "
        "run's status (running, completed, failed, cancelled), "
        "current_step index, and step-by-step snapshot."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "integer",
                "description": (
                    "The pipeline run id (returned by `mc_pipeline_run`)."
                ),
            },
        },
        "required": ["run_id"],
    },
}


# ---------------------------------------------------------------------------
# Tool: mc_pipeline_cancel
# ---------------------------------------------------------------------------

def _handle_mc_pipeline_cancel(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    """Why: A chief may need to abort a long-running pipeline (user
    interruption, downstream failure, scope change). Without cancel
    the run keeps consuming MC resources until natural completion.
    What: POSTs `{action: "cancel", run_id}` to /api/pipelines/run.
    MC marks the run as cancelled and stops scheduling new steps.
    Test: TestMcPipelineCancel.*
    """
    run_id = args.get("run_id")
    if not isinstance(run_id, int) or run_id <= 0:
        return tool_error(
            "mc_pipeline_cancel: 'run_id' must be a positive integer"
        )

    try:
        result = _mc_post(
            "/api/pipelines/run",
            {"action": "cancel", "run_id": run_id},
        )
    except RuntimeError as e:
        return tool_error(str(e))

    run = result.get("run") if isinstance(result, dict) else None
    return {
        "ok": True,
        "run_id": run_id,
        "status": (run or {}).get("status") if isinstance(run, dict) else None,
        "_raw": result,
    }


MC_PIPELINE_CANCEL_SCHEMA = {
    "name": "mc_pipeline_cancel",
    "description": (
        "Cancel a running Mission Control pipeline run. MC marks the "
        "run as cancelled and stops scheduling new steps; in-flight "
        "steps may still complete. Use sparingly — prefer letting "
        "pipelines finish naturally."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "integer",
                "description": (
                    "The pipeline run id to cancel (returned by "
                    "`mc_pipeline_run`)."
                ),
            },
        },
        "required": ["run_id"],
    },
}


# ---------------------------------------------------------------------------
# Tool: mc_pipeline_list
# ---------------------------------------------------------------------------

def _handle_mc_pipeline_list(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    """Why: A chief deciding whether to delegate work to MC first needs to
    know which pipelines are available — otherwise mc_pipeline_run with a
    nonexistent `pipeline_name` just 404s. This tool surfaces the
    registered pipelines so the model can pick the right one (or report
    "no MC pipeline registered for this kind of work" cleanly).
    What: GETs MC `/api/pipelines` and returns `{"pipelines": [...]}`.
    No required args.
    """
    try:
        result = _mc_get("/api/pipelines")
    except RuntimeError as e:
        return tool_error(str(e))

    pipelines = result.get("pipelines", result if isinstance(result, list) else [])
    return {
        "ok": True,
        "count": len(pipelines) if isinstance(pipelines, list) else 0,
        "pipelines": pipelines,
    }


MC_PIPELINE_LIST_SCHEMA = {
    "name": "mc_pipeline_list",
    "description": (
        "List Mission Control (MC) pipelines registered on the configured "
        "MC backend. Use this BEFORE mc_pipeline_run to discover available "
        "`pipeline_name` values — running an unregistered pipeline returns "
        "a 404. No arguments required."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry.register(
    name="mc_pipeline_run",
    toolset="kanban",  # same gating as chief tools — orchestrator/chief only
    schema=MC_PIPELINE_RUN_SCHEMA,
    handler=_handle_mc_pipeline_run,
    check_fn=_check_mc_mode,
    emoji="🚀",
)

registry.register(
    name="mc_pipeline_list",
    toolset="kanban",
    schema=MC_PIPELINE_LIST_SCHEMA,
    handler=_handle_mc_pipeline_list,
    check_fn=_check_mc_mode,
    emoji="📋",
)

registry.register(
    name="mc_pipeline_status",
    toolset="kanban",
    schema=MC_PIPELINE_STATUS_SCHEMA,
    handler=_handle_mc_pipeline_status,
    check_fn=_check_mc_mode,
    emoji="🔎",
)

registry.register(
    name="mc_pipeline_cancel",
    toolset="kanban",
    schema=MC_PIPELINE_CANCEL_SCHEMA,
    handler=_handle_mc_pipeline_cancel,
    check_fn=_check_mc_mode,
    emoji="✋",
)
