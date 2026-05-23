"""InlineAdapter — executes a Hermes DSL workflow in-process by
calling ``POST /api/v1/run-profile`` for each node in topological
order. No external workflow engine required.

Two roles:
1. **Zero-config default** — works out of the box on any Hermes install
   that has the /api/v1/run-profile endpoint (Phase 1).
2. **Swap-proof reference adapter** — every contract test that passes
   against MCAdapter must also pass here, demonstrating that
   ``workflow_run`` callers are bound only to the abstract interface,
   not to a specific engine.

State is kept in-memory inside the process; ``run_id`` is a UUID. Not
durable across restarts — for production durability, use MCAdapter.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

from .base import (
    WorkflowAdapter,
    WorkflowRunResult,
    WorkflowStatus,
    WorkflowTemplate,
    register_adapter,
)
from . import dsl as dsl_mod


# ---------------------------------------------------------------------------
# Storage — in-memory templates + runs

# Templates registered via compile(): name → DSL dict
_TEMPLATES: dict[str, dict[str, Any]] = {}

# Runs: run_id → state mapping (mutated by background worker thread)
_RUNS: dict[str, dict[str, Any]] = {}

# Lock — keep run-mutation thread-safe for status() readers
_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# HTTP client for /api/v1/run-profile

def _api_base_url() -> str:
    """Resolve the API server URL from env."""
    import os
    host = os.environ.get("API_SERVER_HOST") or "127.0.0.1"
    port = os.environ.get("API_SERVER_PORT") or "8642"
    return f"http://{host}:{port}"


_TERMINAL_STATUSES = {"done", "blocked", "failed", "timed_out", "crashed", "cancelled"}


def _get_run_profile(run_id: str) -> dict[str, Any]:
    """GET /api/v1/run-profile/<run_id> — poll terminal status."""
    req = urllib.request.Request(
        f"{_api_base_url()}/api/v1/run-profile/{run_id}",
        headers={"Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            raise RuntimeError(
                f"GET /api/v1/run-profile/{run_id} returned {e.code}: {body[:200]}"
            ) from e


def _post_run_profile(
    profile: str,
    title: str,
    body: str,
    inputs: dict[str, Any],
    timeout_sec: int,
) -> dict[str, Any]:
    """Dispatch a node via /api/v1/run-profile in **async mode** and
    poll the GET endpoint until terminal status or timeout.

    Async mode avoids the gateway's sync-timeout 408 path — a slow LLM
    node that legitimately takes >600s used to mark the whole workflow
    as failed when the sync endpoint returned `status=running` past
    its deadline.

    Raises ``RuntimeError`` on transport errors. Returns the final
    JSON snapshot with terminal status, or with status=running if the
    full ``timeout_sec`` elapses without completion.
    """
    payload = json.dumps({
        "profile": profile,
        "title": title,
        "body": body,
        "inputs": inputs,
        "timeout_sec": timeout_sec,
        "async": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_api_base_url()}/api/v1/run-profile",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            dispatch_resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            raise RuntimeError(
                f"/api/v1/run-profile returned {e.code}: {body[:200]}"
            ) from e
    except Exception as e:
        raise RuntimeError(f"/api/v1/run-profile call failed: {e}") from e

    run_id = dispatch_resp.get("run_id")
    if not run_id:
        # Synthetic error response (e.g. 503 profile-not-on-disk fell
        # into the 2xx parse path) — return as-is so caller can detect.
        return dispatch_resp

    deadline = time.monotonic() + max(60, int(timeout_sec))
    snap = dispatch_resp
    while time.monotonic() < deadline:
        try:
            snap = _get_run_profile(run_id)
        except Exception:
            time.sleep(5)
            continue
        if snap.get("status") in _TERMINAL_STATUSES:
            return snap
        time.sleep(3)
    return snap


# ---------------------------------------------------------------------------
# Background runner thread — executes nodes sequentially

def _run_workflow_thread(run_id: str) -> None:
    """Worker thread for one run. Mutates _RUNS[run_id]."""
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return
        dsl = run["dsl"]
        inputs = run["inputs"]
        run["state"] = "running"
        run["started_at"] = time.time()

    steps_results: dict[str, Any] = {}
    try:
        ordered = dsl_mod.topological_order(dsl["nodes"])
        for node in ordered:
            nid = node["id"]
            with _LOCK:
                if _RUNS[run_id].get("cancel_requested"):
                    _RUNS[run_id]["state"] = "cancelled"
                    _RUNS[run_id]["ended_at"] = time.time()
                    return
                _RUNS[run_id]["current_node"] = nid

            # Interpolate node task body + inputs against accumulated state
            ctx = {"inputs": inputs, "steps": steps_results}
            task_body = dsl_mod.interpolate(node["task"], **ctx)
            node_inputs = dsl_mod.interpolate(node.get("inputs", {}), **ctx)
            timeout_sec = int(node.get("timeout_sec", 600))

            resp = _post_run_profile(
                profile=node["profile"],
                title=f"[{dsl['name']}/{run_id[:8]}] {nid}",
                body=str(task_body),
                inputs=node_inputs if isinstance(node_inputs, dict) else {},
                timeout_sec=timeout_sec,
            )
            node_record = {
                "node_id": nid,
                "profile": node["profile"],
                "status": resp.get("status"),
                "result": resp.get("result"),
                "error": resp.get("error"),
                "run_id": resp.get("run_id"),
                "ended_at": time.time(),
            }
            with _LOCK:
                _RUNS[run_id]["history"].append(node_record)
                steps_results[nid] = {"result": resp.get("result")}

            # Treat non-"done" as workflow failure — stop chain
            if resp.get("status") != "done":
                with _LOCK:
                    _RUNS[run_id]["state"] = "failed"
                    _RUNS[run_id]["error"] = (
                        f"node {nid} ended with status "
                        f"{resp.get('status')!r}: {resp.get('error') or resp.get('result')}"
                    )
                    _RUNS[run_id]["ended_at"] = time.time()
                return

        with _LOCK:
            # Final result = last node's result
            last_id = ordered[-1]["id"]
            _RUNS[run_id]["state"] = "done"
            _RUNS[run_id]["result"] = steps_results.get(last_id)
            _RUNS[run_id]["current_node"] = None
            _RUNS[run_id]["ended_at"] = time.time()

    except Exception as e:
        with _LOCK:
            _RUNS[run_id]["state"] = "failed"
            _RUNS[run_id]["error"] = f"runner crash: {e}"
            _RUNS[run_id]["ended_at"] = time.time()


# ---------------------------------------------------------------------------
# Adapter

class InlineAdapter:
    name = "inline"

    def compile(self, dsl: dict[str, Any]) -> str:
        dsl_mod.validate(dsl)
        template_id = dsl["name"]
        # Re-compiling the same name = upsert
        _TEMPLATES[template_id] = dsl
        return template_id

    def run(
        self,
        template_id: str,
        inputs: Optional[dict[str, Any]] = None,
    ) -> WorkflowRunResult:
        if template_id not in _TEMPLATES:
            return WorkflowRunResult(
                run_id="",
                backend=self.name,
                state="failed",
                message=f"template {template_id!r} not registered "
                        f"(call compile() first or run sync-workflows)",
            )
        run_id = uuid.uuid4().hex
        with _LOCK:
            _RUNS[run_id] = {
                "run_id": run_id,
                "template_id": template_id,
                "dsl": _TEMPLATES[template_id],
                "inputs": inputs or {},
                "state": "queued",
                "current_node": None,
                "history": [],
                "result": None,
                "error": None,
                "created_at": time.time(),
                "cancel_requested": False,
            }
        threading.Thread(
            target=_run_workflow_thread,
            args=(run_id,),
            daemon=True,
            name=f"inline-workflow-{run_id[:8]}",
        ).start()
        return WorkflowRunResult(
            run_id=run_id,
            backend=self.name,
            state="queued",
            message=f"inline run dispatched for template {template_id!r}",
        )

    def status(self, run_id: str) -> WorkflowStatus:
        with _LOCK:
            run = _RUNS.get(run_id)
            if run is None:
                return WorkflowStatus(
                    run_id=run_id,
                    backend=self.name,
                    state="failed",
                    error=f"run_id {run_id!r} not found",
                )
            return WorkflowStatus(
                run_id=run_id,
                backend=self.name,
                state=run["state"],
                current_node=run["current_node"],
                history=list(run["history"]),
                result=run["result"],
                error=run["error"],
            )

    def cancel(self, run_id: str) -> dict[str, Any]:
        with _LOCK:
            run = _RUNS.get(run_id)
            if run is None:
                return {"ok": False, "message": f"run {run_id!r} not found"}
            if run["state"] in {"done", "failed", "cancelled"}:
                return {"ok": False, "message": f"run already {run['state']}"}
            run["cancel_requested"] = True
            return {"ok": True, "message": "cancel requested"}

    def list_templates(self) -> list[WorkflowTemplate]:
        out = []
        for name, dsl in sorted(_TEMPLATES.items()):
            out.append(WorkflowTemplate(
                id=name,
                name=name,
                version=int(dsl.get("version", 1)),
                description=dsl.get("description"),
                backend=self.name,
            ))
        return out


def _autoload_yaml_templates() -> int:
    """On import, scan ``/opt/hermes-workflows/*.yaml`` (or
    ``HERMES_WORKFLOWS_DIR``) and compile every template into the
    in-memory registry. Without this, ``workflow_run(template=X)``
    in the gateway process can't find templates that ``sync-workflows``
    registered in a different process.

    Idempotent. Returns count of templates loaded.
    """
    import os
    from pathlib import Path
    candidates = [
        os.environ.get("HERMES_WORKFLOWS_DIR"),
        "/opt/hermes-workflows",
        "/mnt/9/aimanager/infra/hermes/workflows",
    ]
    wf_dir: Optional[Path] = None
    for c in candidates:
        if c and Path(c).is_dir():
            wf_dir = Path(c)
            break
    if wf_dir is None:
        return 0
    count = 0
    adapter = InlineAdapter()
    for yaml_path in sorted(wf_dir.glob("*.yaml")):
        try:
            d = dsl_mod.load(str(yaml_path))
            adapter.compile(d)
            count += 1
        except Exception:
            # Best-effort — invalid templates surface via sync-workflows
            continue
    return count


# Self-register on import
register_adapter(InlineAdapter())
# Eager-load YAML templates so workflow_run finds them in the gateway process.
_autoload_yaml_templates()
