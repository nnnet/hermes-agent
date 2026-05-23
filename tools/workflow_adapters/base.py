"""WorkflowAdapter Protocol + registry.

Defines the swappable interface between Hermes-side workflow tools
(``workflow_run`` / ``workflow_status`` / ``workflow_cancel`` /
``workflow_list_templates``) and a concrete workflow backend
(Mission Control pipeline, inline run-profile chain, LangGraph,
Temporal, etc.).

Adding a new backend = one file in this directory implementing the
five-method contract + one line in ``_REGISTRY``. Hermes profiles,
SOUL.md, prompts don't change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Result types — backend-agnostic

@dataclass
class WorkflowRunResult:
    """Returned by ``run()`` immediately when execution starts.

    Status reflects backend's enqueue/dispatch state, not terminal
    state — caller polls via ``status()`` for completion.
    """
    run_id: str
    backend: str
    state: str = "queued"            # queued | running | done | failed | cancelled
    message: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "backend": self.backend,
            "state": self.state,
            "message": self.message,
            **({"extra": self.extra} if self.extra else {}),
        }


@dataclass
class WorkflowStatus:
    """Returned by ``status()`` — current snapshot of a run."""
    run_id: str
    backend: str
    state: str                       # queued | running | done | failed | cancelled
    current_node: Optional[str] = None
    history: list[dict[str, Any]] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "backend": self.backend,
            "state": self.state,
            "current_node": self.current_node,
            "history": self.history,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class WorkflowTemplate:
    """Returned by ``list_templates()``."""
    id: str
    name: str
    version: int
    description: Optional[str] = None
    backend: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "backend": self.backend,
        }


# ---------------------------------------------------------------------------
# Adapter Protocol

@runtime_checkable
class WorkflowAdapter(Protocol):
    """Interface every backend must implement.

    A backend is any external system that can:
    - accept a workflow definition compiled from the Hermes DSL
    - execute that workflow, calling Hermes profiles via
      `POST /api/v1/run-profile` in each node
    - report run state on demand
    - be cancelled mid-run

    The five methods below are the only surface area exposed to
    ``workflow_tools.py``. Anything backend-specific (pipeline_id,
    HTTP base_url, container name, ...) lives inside the adapter.
    """

    name: str   # short identifier: "mc", "inline", "langgraph", ...

    def run(
        self,
        template_id: str,
        inputs: Optional[dict[str, Any]] = None,
    ) -> WorkflowRunResult:
        """Start executing the named template. Return immediately with run_id."""
        ...

    def status(self, run_id: str) -> WorkflowStatus:
        """Snapshot a run by id."""
        ...

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Best-effort cancel. Return {ok: bool, message: ...}."""
        ...

    def list_templates(self) -> list[WorkflowTemplate]:
        """Enumerate templates known to this backend."""
        ...

    def compile(self, dsl: dict[str, Any]) -> str:
        """Compile a Hermes DSL dict into a backend-specific template.

        Returns the backend's template_id (str). Idempotent — re-running
        on the same DSL returns the same id (or upserts in-place).
        """
        ...


# ---------------------------------------------------------------------------
# Registry — active adapter selection

_REGISTRY: dict[str, WorkflowAdapter] = {}
_ACTIVE: Optional[str] = None


def register_adapter(adapter: WorkflowAdapter) -> None:
    """Register an adapter under its ``name``. Last registration wins."""
    _REGISTRY[adapter.name] = adapter


def get_adapter(name: Optional[str] = None) -> WorkflowAdapter:
    """Return adapter by name, or the active one.

    Resolution order when ``name`` is None:
      1. Active adapter set via ``set_active_adapter``.
      2. ``HERMES_WORKFLOW_BACKEND`` env var.
      3. "inline" (zero-config fallback — uses /api/v1/run-profile).

    Raises ``KeyError`` if the resolved name isn't registered.
    """
    import os
    chosen = (
        name
        or _ACTIVE
        or os.environ.get("HERMES_WORKFLOW_BACKEND")
        or "inline"
    )
    if chosen not in _REGISTRY:
        raise KeyError(
            f"workflow adapter {chosen!r} not registered. "
            f"Known: {sorted(_REGISTRY) or '<none>'}"
        )
    return _REGISTRY[chosen]


def set_active_adapter(name: Optional[str]) -> None:
    """Set the in-process default. ``None`` falls back to env/default."""
    global _ACTIVE
    _ACTIVE = name


def list_adapters() -> list[str]:
    """Names of all registered adapters."""
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Auto-registration on import

def _autoregister() -> None:
    """Import every adapter module so they self-register on first use.

    Lives here (not at module top) to avoid import cycles — adapter
    modules may import from ``base.py`` themselves.
    """
    from . import inline as _inline  # noqa: F401
    try:
        from . import mc as _mc  # noqa: F401
    except Exception:
        # MC adapter is optional — only available when MC is reachable.
        # Don't fail import of the registry if MC tools are unconfigured.
        pass


_autoregister()
