"""MCAdapter — thin wrapper over the existing ``mc_pipeline_*`` tools
in ``tools/mc_tools.py``.

Maps the WorkflowAdapter interface onto MC's HTTP API. Reuses the
already-tested ``_handle_mc_pipeline_run/status/cancel/list`` handlers
under the hood — no duplicate HTTP code, no duplicate error handling.

Compile semantics: MC has a native ``workflow_templates`` table. The
adapter sends the Hermes DSL to a future "compile" route on MC, or as
a fallback resolves the template id by name from existing MC templates.
For now (Phase 2) ``compile()`` only resolves an existing MC template
by name; PUSH-from-DSL lands in Phase 3 alongside ``sync-workflows``.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import (
    WorkflowAdapter,
    WorkflowRunResult,
    WorkflowStatus,
    WorkflowTemplate,
    register_adapter,
)


def _import_mc():
    """Lazy import — mc_tools may not be available outside the gateway
    runtime (e.g. unit-test contexts)."""
    from tools import mc_tools
    return mc_tools


class MCAdapter:
    name = "mc"

    # -------- compile --------

    def compile(self, dsl: dict[str, Any]) -> str:
        """Resolve / create the MC pipeline_template for this DSL.

        Phase 2 behaviour: look up existing MC template by ``dsl['name']``.
        If found → return its id. If missing → raise; full DSL-push lands
        in Phase 3.
        """
        mc = _import_mc()
        # Cheap implementation that reuses _resolve_pipeline_id (already
        # in mc_tools). Falls back to listing if not found.
        name = dsl.get("name")
        if not name:
            raise ValueError("DSL must have a 'name' field")
        try:
            pid = mc._resolve_pipeline_id(name)
            return str(pid)
        except Exception as e:
            raise NotImplementedError(
                f"MCAdapter.compile: cannot create MC template from DSL "
                f"yet (Phase 3). Existing MC template with name {name!r} "
                f"not found: {e}. Use `make workflow-sync` once Phase 3 "
                f"lands, or pre-create the template in MC manually."
            ) from e

    # -------- run --------

    def run(
        self,
        template_id: str,
        inputs: Optional[dict[str, Any]] = None,
    ) -> WorkflowRunResult:
        mc = _import_mc()
        # MC's pipeline_id is an int. Coerce — chiefs / DSL routinely
        # pass strings.
        try:
            pid: Any = int(template_id)
        except (TypeError, ValueError):
            pid = template_id  # let MC try to resolve by name
        args: dict[str, Any] = {
            "pipeline_id": pid,
            "wait_for_completion": False,
        }
        if inputs:
            args["inputs"] = inputs
        try:
            resp = mc._handle_mc_pipeline_run(args)
        except Exception as e:
            return WorkflowRunResult(
                run_id="",
                backend=self.name,
                state="failed",
                message=f"mc_pipeline_run raised: {e}",
            )
        # mc_tools handler returns either a tool_error JSON string OR a
        # dict. tool_error strings come back from _handle_mc_pipeline_run
        # as plain JSON strings (e.g. '{"error":"..."}'). Normalise.
        if isinstance(resp, str):
            try:
                import json as _json
                resp = _json.loads(resp)
            except Exception:
                return WorkflowRunResult(
                    run_id="", backend=self.name, state="failed",
                    message=f"mc_pipeline_run returned non-JSON: {resp[:200]}",
                )
        if not isinstance(resp, dict):
            return WorkflowRunResult(
                run_id="", backend=self.name, state="failed",
                message=f"mc_pipeline_run returned unexpected type "
                        f"{type(resp).__name__}",
            )
        # mc_tools uses "ok" not "success". Fall back to either.
        ok = resp.get("ok", resp.get("success"))
        if ok is False:
            return WorkflowRunResult(
                run_id="", backend=self.name, state="failed",
                message=resp.get("error", "mc_pipeline_run failed"),
            )
        run_data = resp.get("_raw") or resp.get("run") or {}
        return WorkflowRunResult(
            run_id=str(resp.get("run_id") or run_data.get("id") or ""),
            backend=self.name,
            state=resp.get("status") or run_data.get("status", "queued"),
            message=resp.get("message"),
            extra=run_data,
        )

    # -------- status --------

    def status(self, run_id: str) -> WorkflowStatus:
        mc = _import_mc()
        # MC's run_id is an int; chiefs hand back strings.
        try:
            rid: Any = int(run_id)
        except (TypeError, ValueError):
            rid = run_id
        try:
            resp = mc._handle_mc_pipeline_status({"run_id": rid})
        except Exception as e:
            return WorkflowStatus(
                run_id=run_id, backend=self.name,
                state="failed", error=f"mc_pipeline_status raised: {e}",
            )
        if isinstance(resp, str):
            try:
                import json as _json
                resp = _json.loads(resp)
            except Exception:
                return WorkflowStatus(
                    run_id=run_id, backend=self.name, state="failed",
                    error=f"mc_pipeline_status returned non-JSON: {resp[:200]}",
                )
        if not isinstance(resp, dict):
            return WorkflowStatus(
                run_id=run_id, backend=self.name, state="failed",
                error=f"mc_pipeline_status returned unexpected type "
                      f"{type(resp).__name__}",
            )
        ok = resp.get("ok", resp.get("success"))
        if ok is False:
            return WorkflowStatus(
                run_id=run_id, backend=self.name, state="failed",
                error=resp.get("error", "mc_pipeline_status failed"),
            )
        # Run data can live at top level or under "_raw"/"run".
        run = resp.get("_raw") or resp.get("run") or resp
        mc_state = run.get("status", "unknown") if isinstance(run, dict) else "unknown"
        normalized = {
            "queued": "queued",
            "running": "running",
            "completed": "done",
            "failed": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }.get(mc_state, mc_state)
        if not isinstance(run, dict):
            run = {}
        return WorkflowStatus(
            run_id=run_id,
            backend=self.name,
            state=normalized,
            current_node=str(run.get("current_step")) if run.get("current_step") is not None else None,
            history=run.get("steps_snapshot") or run.get("steps", []) or [],
            result=run.get("output") or run.get("result"),
            error=run.get("error"),
        )

    # -------- cancel --------

    def cancel(self, run_id: str) -> dict[str, Any]:
        mc = _import_mc()
        try:
            resp = mc._handle_mc_pipeline_cancel({"run_id": run_id})
        except Exception as e:
            return {"ok": False, "message": f"mc_pipeline_cancel raised: {e}"}
        return {
            "ok": bool(resp.get("success")),
            "message": resp.get("message") or resp.get("error"),
        }

    # -------- list_templates --------

    def list_templates(self) -> list[WorkflowTemplate]:
        mc = _import_mc()
        try:
            resp = mc._handle_mc_pipeline_list({})
        except Exception:
            return []
        if not resp.get("success"):
            return []
        out: list[WorkflowTemplate] = []
        for p in resp.get("pipelines", []) or []:
            out.append(WorkflowTemplate(
                id=str(p.get("id")),
                name=p.get("name", ""),
                version=int(p.get("version", 1)),
                description=p.get("description"),
                backend=self.name,
            ))
        return out


# Self-register on import — but only if mc_tools is importable.
# (Done in the try/except inside ``base._autoregister``.)
register_adapter(MCAdapter())
