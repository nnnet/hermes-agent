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
        # template_id is the MC pipeline id (int as string, or name)
        args = {"pipeline_id": template_id, "wait_for_completion": False}
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
        if not resp.get("success"):
            return WorkflowRunResult(
                run_id="",
                backend=self.name,
                state="failed",
                message=resp.get("error", "mc_pipeline_run failed"),
            )
        run_data = resp.get("run") or {}
        return WorkflowRunResult(
            run_id=str(run_data.get("run_id") or resp.get("run_id") or ""),
            backend=self.name,
            state=run_data.get("status", "queued"),
            message=resp.get("message"),
            extra=run_data,
        )

    # -------- status --------

    def status(self, run_id: str) -> WorkflowStatus:
        mc = _import_mc()
        try:
            resp = mc._handle_mc_pipeline_status({"run_id": run_id})
        except Exception as e:
            return WorkflowStatus(
                run_id=run_id, backend=self.name,
                state="failed", error=f"mc_pipeline_status raised: {e}",
            )
        if not resp.get("success"):
            return WorkflowStatus(
                run_id=run_id, backend=self.name,
                state="failed",
                error=resp.get("error", "mc_pipeline_status failed"),
            )
        run = resp.get("run") or {}
        # MC has its own state vocabulary — pass it through but also
        # provide a normalized state for the caller.
        mc_state = run.get("status", "unknown")
        normalized = {
            "queued": "queued",
            "running": "running",
            "completed": "done",
            "failed": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }.get(mc_state, mc_state)
        return WorkflowStatus(
            run_id=run_id,
            backend=self.name,
            state=normalized,
            current_node=run.get("current_step"),
            history=run.get("steps", []) or [],
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
