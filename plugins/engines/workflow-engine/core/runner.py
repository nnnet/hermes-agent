"""Generic runner — load state, run one decision step, build prompt, save.

Thin glue layer that:
1. Loads WorkflowState from JSON (or creates fresh)
2. Appends new user msg + previous bot reply to history
3. Constructs WorkflowMachine + calls decide() → target phase
4. Resolves target phase's prompt_builder + builds mini_prompt
5. Saves state
6. Returns a structured result dict for the CLI to JSON-serialize

Workflow-specific code lives outside this package (caller passes a
WorkflowConfig); the runner knows nothing workflow-specific.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .machine import WorkflowMachine
from .state import WorkflowState, WorkflowConfig

STATE_DIR_DEFAULT = Path(
    os.environ.get(
        "WORKFLOW_STATE_DIR",
        str(Path.home() / ".hermes" / "workflow_state"),
    )
)


def _state_path(state_dir: Path, workflow_name: str, session: str) -> Path:
    """Per-workflow subdir + session.json. Workflow isolation guaranteed."""
    return state_dir / workflow_name / f"{session}.json"


def run(
    config: WorkflowConfig,
    session: str,
    user_msg: str,
    prev_bot_msg: str | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one workflow step and return result dict.

    Args:
        config: workflow's WORKFLOW config
        session: opaque session id (used as filename)
        user_msg: latest user message
        prev_bot_msg: bot's previous reply (for slot extraction)
        state_dir: override location (default ~/.hermes/workflow_state/)

    Returns:
        {
            "workflow": <name>,
            "phase": <target phase name>,
            "iteration": <int>,
            "state_summary": {...},
            "mini_prompt": "<markdown>",
            "instructions_for_bot": "<one-liner>",
            "state_file": "<path>",
        }
    """
    sdir = Path(state_dir or STATE_DIR_DEFAULT)
    state_path = _state_path(sdir, config.name, session)
    wstate = WorkflowState.load(state_path, config.slots_cls, config.name)

    if prev_bot_msg:
        wstate.add_bot_msg(prev_bot_msg)
    if user_msg:
        wstate.add_user_msg(user_msg)

    wstate.iteration += 1

    machine = WorkflowMachine(config, wstate)
    target_phase = machine.decide(user_msg, prev_bot_msg)
    wstate.phase = target_phase

    phase_spec = config.get_phase(target_phase)
    if phase_spec is None:
        mini_prompt = f"<error: unknown phase {target_phase}>"
        instructions = "Engine error — fall back to manual flow"
    else:
        mini_prompt = phase_spec.prompt_builder(wstate, user_msg)
        if (
            target_phase == "LOCK"
            and config.mandatory_lock_phrase
            and config.mandatory_lock_phrase not in mini_prompt
        ):
            mini_prompt = (
                f"<engine-warning: LOCK template missing mandatory phrase "
                f"{config.mandatory_lock_phrase!r}>\n\n{mini_prompt}"
            )
        instructions = _instructions_for_phase(target_phase)

    wstate.action_log.append(
        f"iter={wstate.iteration} phase={target_phase} "
        f"filled={wstate.slots.filled_required()}/{len(wstate.slots.required_keys())}"
    )
    wstate.save(state_path)

    return {
        "workflow": config.name,
        "phase": target_phase,
        "iteration": wstate.iteration,
        "state_summary": wstate.summary(),
        "mini_prompt": mini_prompt,
        "instructions_for_bot": instructions,
        "state_file": str(state_path),
    }


def run_status(
    config: WorkflowConfig,
    session: str,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Return state summary without advancing the workflow."""
    sdir = Path(state_dir or STATE_DIR_DEFAULT)
    state_path = _state_path(sdir, config.name, session)
    if not state_path.exists():
        return {
            "error": "no state",
            "session": session,
            "workflow": config.name,
            "state_file": str(state_path),
        }
    wstate = WorkflowState.load(state_path, config.slots_cls, config.name)
    summary = wstate.summary()
    summary["action_log_tail"] = wstate.action_log[-10:]
    return summary


def run_reset(
    config: WorkflowConfig,
    session: str,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Delete persisted state for this session."""
    sdir = Path(state_dir or STATE_DIR_DEFAULT)
    state_path = _state_path(sdir, config.name, session)
    if state_path.exists():
        state_path.unlink()
        return {"reset": True, "session": session, "workflow": config.name}
    return {
        "reset": False,
        "session": session,
        "workflow": config.name,
        "note": "no state existed",
    }


def _instructions_for_phase(phase: str) -> str:
    """One-liner imperatives. Generic phase-name conventions.

    Workflows can override via PhaseSpec metadata in future if needed.
    """
    return {
        "INIT": "Engine is initializing; no reply yet.",
        "DECOMPOSE": "Use the template AS YOUR REPLY. Do not add tool calls; do not start work.",
        "ASK": "Use the template AS YOUR REPLY. Ask ONE focused question.",
        "REFLECT": "Use the template AS YOUR REPLY. Wait for user confirmation.",
        "LOCK": "Use the template AS YOUR REPLY. Lock phrase is MANDATORY and verbatim.",
        "DONE": "Workflow complete. Execute the action from the lock block.",
    }.get(phase, f"Phase {phase} — use mini_prompt verbatim.")
