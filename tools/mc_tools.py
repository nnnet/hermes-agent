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

def _handle_mc_pipeline_run(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    """Why: Delegate a heavy workflow to MC's pipelines runner — chief
    decides the work needs multi-framework orchestration (e.g. a CrewAI
    crew or a LangGraph DAG) instead of the lighter in-Hermes path. Tool
    returns the MC job descriptor (id, status URL) so the chief can poll
    or attach a webhook.
    What: POSTs to MC `/api/pipelines/run` with the operator-supplied
    `pipeline_name` and `inputs` dict. MC creates a pipeline-run and
    returns its id + status endpoint. Tool surfaces both back to the
    caller as a flat dict.
    Test: test_mc_pipeline_run_happy, test_mc_pipeline_run_missing_name,
    test_mc_pipeline_run_unconfigured, test_mc_pipeline_run_4xx.
    """
    pipeline_name = args.get("pipeline_name") or args.get("pipeline")
    if not isinstance(pipeline_name, str) or not pipeline_name:
        return tool_error(
            "mc_pipeline_run: 'pipeline_name' is required (string)"
        )

    inputs = args.get("inputs") or {}
    if not isinstance(inputs, dict):
        return tool_error(
            "mc_pipeline_run: 'inputs' must be a JSON object if provided"
        )

    # Optional callback for async result delivery. MC will POST the final
    # result to this URL (must be reachable from MC — typically Hermes
    # gateway's webhook receiver).
    callback_url = args.get("callback_url")
    if callback_url is not None and not isinstance(callback_url, str):
        return tool_error("mc_pipeline_run: 'callback_url' must be a string")

    payload: dict[str, Any] = {
        "pipeline": pipeline_name,
        "inputs": inputs,
    }
    if callback_url:
        payload["callback_url"] = callback_url

    # Optional tenant/project id for multi-tenant MC instances.
    tenant = args.get("tenant")
    if isinstance(tenant, str) and tenant:
        payload["tenant"] = tenant

    try:
        result = _mc_post("/api/pipelines/run", payload)
    except RuntimeError as e:
        return tool_error(str(e))

    # Normalise the response shape so tool callers don't need to know
    # MC's exact field names. We keep the raw response under _raw for
    # debugging.
    return {
        "ok": True,
        "pipeline_name": pipeline_name,
        "job_id": result.get("id") or result.get("job_id") or result.get("run_id"),
        "status": result.get("status"),
        "status_url": result.get("status_url") or result.get("url"),
        "_raw": result,
    }


MC_PIPELINE_RUN_SCHEMA = {
    "name": "mc_pipeline_run",
    "description": (
        "Delegate a workflow to an external Mission Control (MC) "
        "execution backend by running one of its registered pipelines. "
        "Use this when a task needs multi-framework orchestration "
        "(CrewAI / LangGraph / AutoGen agents inside MC) or a heavy "
        "long-running pipeline that shouldn't run inline in the agent "
        "turn. Returns a job_id and status_url for polling. Requires "
        "HERMES_MC_BASE_URL (and optionally HERMES_MC_API_KEY) in env."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pipeline_name": {
                "type": "string",
                "description": (
                    "The MC pipeline identifier to run — must already be "
                    "registered in MC. List available via the MC web UI "
                    "(/pipelines) or `curl $HERMES_MC_BASE_URL/api/pipelines`."
                ),
            },
            "inputs": {
                "type": "object",
                "description": (
                    "JSON object of pipeline inputs. Shape depends on the "
                    "pipeline — consult the pipeline's MC definition."
                ),
            },
            "callback_url": {
                "type": "string",
                "description": (
                    "Optional. URL MC will POST the final result to when "
                    "the pipeline finishes. Useful for async patterns where "
                    "the chief doesn't want to poll. Typically the Hermes "
                    "gateway webhook endpoint."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional. Tenant/project slug for multi-tenant MC "
                    "deployments. Most home setups can omit this."
                ),
            },
        },
        "required": ["pipeline_name"],
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
