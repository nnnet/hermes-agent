"""Workflow adapter package.

Public surface for ``tools/workflow_tools.py`` and external callers
who want backend-agnostic workflow orchestration. Adapter modules
(``inline``, ``mc``, ...) self-register on import.
"""

from .base import (
    WorkflowAdapter,
    WorkflowRunResult,
    WorkflowStatus,
    WorkflowTemplate,
    register_adapter,
    get_adapter,
    set_active_adapter,
    list_adapters,
)

__all__ = [
    "WorkflowAdapter",
    "WorkflowRunResult",
    "WorkflowStatus",
    "WorkflowTemplate",
    "register_adapter",
    "get_adapter",
    "set_active_adapter",
    "list_adapters",
]
