"""workflow-engine.core — generic primitives for stateful workflows.

Public surface: WorkflowConfig + PhaseSpec + TransitionSpec + SlotsBase
let a workflow declare its state graph. The engine handles persistence,
state-machine wiring, and prompt building.
"""
from .state import SlotsBase, WorkflowState, WorkflowConfig, PhaseSpec, TransitionSpec
from .machine import WorkflowMachine
from .runner import run, run_status, run_reset
from . import detectors

__all__ = [
    "SlotsBase",
    "WorkflowState",
    "WorkflowConfig",
    "PhaseSpec",
    "TransitionSpec",
    "WorkflowMachine",
    "run",
    "run_status",
    "run_reset",
    "detectors",
]
