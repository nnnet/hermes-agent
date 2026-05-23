"""Workflow tools — backend-agnostic orchestration surface for chief /
main-manager profiles.

Four tools:
  - workflow_run(template, inputs?, backend?)            → {run_id, state, ...}
  - workflow_status(run_id, backend?)                     → {state, current_node, history, result, error}
  - workflow_cancel(run_id, backend?)                     → {ok, message}
  - workflow_list_templates(backend?)                     → [{id, name, version, description, backend}]

The active backend is resolved per-call via
``workflow_adapters.get_adapter(name_or_None)``. Default fallback is
``inline`` (zero-config). Override via the ``backend`` argument or
``HERMES_WORKFLOW_BACKEND`` env var.

Same toolset gating as kanban / mc tools — only profiles that
opt into ``kanban`` or ``workflow`` toolsets see these.
"""

from __future__ import annotations

from typing import Any, Optional

from tools.registry import registry, tool_error

from .workflow_adapters import (
    get_adapter,
    list_adapters,
)


# ---------------------------------------------------------------------------
# Schemas

WORKFLOW_RUN_SCHEMA = {
    "name": "workflow_run",
    "description": (
        "Start a workflow template by name on the active backend "
        "(MC pipeline, inline, LangGraph, ...). Returns run_id "
        "immediately — poll workflow_status to track completion. "
        "This is the backend-agnostic alternative to mc_pipeline_run."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "template": {
                "type": "string",
                "description": (
                    "Template name (DSL `name` field) or backend-specific "
                    "template id."
                ),
            },
            "inputs": {
                "type": "object",
                "description": (
                    "Workflow-level inputs (any JSON object). Passed to "
                    "the first node and interpolated into node tasks as "
                    "`{{ inputs.<name> }}`."
                ),
                "additionalProperties": True,
            },
            "backend": {
                "type": "string",
                "description": (
                    "Override the active workflow backend (e.g. 'mc', "
                    "'inline'). Omit to use HERMES_WORKFLOW_BACKEND or "
                    "the default."
                ),
            },
        },
        "required": ["template"],
        "additionalProperties": False,
    },
}


WORKFLOW_STATUS_SCHEMA = {
    "name": "workflow_status",
    "description": (
        "Poll the current state of a workflow run by run_id. Returns "
        "{state, current_node, history, result, error}. State is "
        "normalized across backends: queued | running | done | failed "
        "| cancelled."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "backend": {"type": "string"},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
}


WORKFLOW_CANCEL_SCHEMA = {
    "name": "workflow_cancel",
    "description": (
        "Best-effort cancel of an in-flight workflow run. Already-"
        "terminal runs are not affected."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "backend": {"type": "string"},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
}


WORKFLOW_LIST_TEMPLATES_SCHEMA = {
    "name": "workflow_list_templates",
    "description": (
        "Enumerate workflow templates available on the active "
        "backend. Use to discover what can be run."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "backend": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Handlers

def _resolve_backend(args: dict[str, Any]):
    """Get adapter from explicit `backend` arg or active default."""
    return get_adapter(args.get("backend"))


def _handle_workflow_run(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    template = args.get("template")
    if not template or not isinstance(template, str):
        return tool_error("workflow_run: 'template' (string) is required")
    try:
        adapter = _resolve_backend(args)
    except KeyError as e:
        return tool_error(
            f"workflow_run: {e}. Available backends: {list_adapters()}"
        )
    inputs = args.get("inputs") or {}
    if not isinstance(inputs, dict):
        return tool_error("workflow_run: 'inputs' must be a JSON object")
    try:
        result = adapter.run(template, inputs)
    except Exception as e:
        return tool_error(f"workflow_run: backend {adapter.name!r} raised: {e}")
    return {"success": True, **result.to_dict()}


def _handle_workflow_status(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    run_id = args.get("run_id")
    if not run_id or not isinstance(run_id, str):
        return tool_error("workflow_status: 'run_id' (string) is required")
    try:
        adapter = _resolve_backend(args)
    except KeyError as e:
        return tool_error(f"workflow_status: {e}")
    try:
        status = adapter.status(run_id)
    except Exception as e:
        return tool_error(f"workflow_status: backend {adapter.name!r} raised: {e}")
    return {"success": True, **status.to_dict()}


def _handle_workflow_cancel(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    run_id = args.get("run_id")
    if not run_id or not isinstance(run_id, str):
        return tool_error("workflow_cancel: 'run_id' (string) is required")
    try:
        adapter = _resolve_backend(args)
    except KeyError as e:
        return tool_error(f"workflow_cancel: {e}")
    try:
        result = adapter.cancel(run_id)
    except Exception as e:
        return tool_error(f"workflow_cancel: backend {adapter.name!r} raised: {e}")
    return {"success": bool(result.get("ok")), **result}


def _handle_workflow_list_templates(args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
    try:
        adapter = _resolve_backend(args)
    except KeyError as e:
        return tool_error(f"workflow_list_templates: {e}")
    try:
        tpls = adapter.list_templates()
    except Exception as e:
        return tool_error(
            f"workflow_list_templates: backend {adapter.name!r} raised: {e}"
        )
    return {
        "success": True,
        "backend": adapter.name,
        "templates": [t.to_dict() for t in tpls],
    }


# ---------------------------------------------------------------------------
# Registry

def _check_workflow_available() -> bool:
    """At least one adapter must be registered for the toolset to gate-on."""
    return len(list_adapters()) > 0


registry.register(
    name="workflow_run",
    toolset="kanban",
    schema=WORKFLOW_RUN_SCHEMA,
    handler=_handle_workflow_run,
    check_fn=_check_workflow_available,
    emoji="🌀",
)

registry.register(
    name="workflow_status",
    toolset="kanban",
    schema=WORKFLOW_STATUS_SCHEMA,
    handler=_handle_workflow_status,
    check_fn=_check_workflow_available,
    emoji="🔁",
)

registry.register(
    name="workflow_cancel",
    toolset="kanban",
    schema=WORKFLOW_CANCEL_SCHEMA,
    handler=_handle_workflow_cancel,
    check_fn=_check_workflow_available,
    emoji="✋",
)

registry.register(
    name="workflow_list_templates",
    toolset="kanban",
    schema=WORKFLOW_LIST_TEMPLATES_SCHEMA,
    handler=_handle_workflow_list_templates,
    check_fn=_check_workflow_available,
    emoji="📋",
)
