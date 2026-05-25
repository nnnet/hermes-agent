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


def _artifact_path(state_dir: Path, workflow_name: str, session: str) -> Path:
    """Per-session final YAML artifact (P5).

    Lives in ``<state_dir>/<workflow>/artifacts/<session>.yaml`` so that
    state JSON (engine internal) and artifact YAML (downstream-skill
    payload) coexist without name collision.
    """
    return state_dir / workflow_name / "artifacts" / f"{session}.yaml"


def _compute_clarity_score(wstate, schema) -> float:
    """Programmatic clarity_score from schema weights.

    Components (each 0..1):
      completeness — filled_required / required_total
      confidence   — mean confidence over required slots
      stability    — 1 if no contradictions, scaled by contradictions per turn
      grounding    — placeholder 1.0 until P4 anti-pattern detectors land
      contradictions — 1 - min(1, len(contradictions)/iteration)
    """
    from .schema import SchemaSlots
    slots = wstate.slots
    weights = schema.clarity_score.weights or {}

    completeness = slots.completeness() if hasattr(slots, "completeness") else 0.0

    # Mean confidence over required slots only
    if isinstance(slots, SchemaSlots):
        req = slots.required_keys()
        conf = slots.confidence_dict()
        confidence = (sum(conf.get(k, 0.0) for k in req) / len(req)) if req else 0.0
    else:
        confidence = 0.0

    contradictions = 1.0 - min(1.0, len(wstate.contradictions) / max(1, wstate.iteration))
    stability = contradictions          # rough proxy for now
    grounding = 1.0                     # P4 will refine

    components = {
        "completeness": completeness,
        "stability": stability,
        "confidence": confidence,
        "grounding": grounding,
        "contradictions": contradictions,
    }
    weighted_sum = 0.0
    weight_total = 0.0
    for key, w in weights.items():
        weighted_sum += components.get(key, 0.0) * float(w)
        weight_total += float(w)
    if weight_total <= 0:
        return 0.0
    return max(0.0, min(1.0, weighted_sum / weight_total))


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

    # ── P2: LLM extractor ────────────────────────────────────────────
    # Before routing, ask a small fast LLM to (re)read the conversation
    # against the schema and fill / revise slots with per-slot
    # confidence. Skipped silently when:
    #   - workflow uses the legacy dataclass SlotsBase (no schema)
    #   - WORKFLOW_EXTRACTOR_ENABLED=0
    #   - extractor call fails (network, anthropic SDK missing, etc.)
    # The legacy regex-based detector in decide_fn keeps running either
    # way — extractor is additive, not a replacement.
    try:
        from .schema import SchemaSlots
        if isinstance(wstate.slots, SchemaSlots) and getattr(config, "schema", None) is not None:
            from .extractor import extract_slots as _extract, apply_results
            results = _extract(
                config.schema,
                wstate.slots,
                wstate.user_history,
                wstate.bot_history,
            )
            if results:
                changes = apply_results(
                    wstate.slots, results, config.schema,
                    log=wstate.action_log,
                )
                if changes == 0:
                    wstate.action_log.append("extractor: no changes")
            else:
                wstate.action_log.append("extractor: skipped / no results")
    except Exception as e:
        # Never let extractor failure stop the engine — log and continue.
        wstate.action_log.append(f"extractor: error ({type(e).__name__}: {e})")

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

    # ── P5: final artifact ───────────────────────────────────────────
    # When the workflow reaches its terminal phase (typically DONE),
    # emit a YAML artifact next to the state file. The artifact is what
    # downstream skills consume; `желание[]` (raw user-wishes audit) is
    # included for traceability but `export.fields` defines the subset
    # that crosses the skill boundary (only `goal` etc.).
    artifact_path = _artifact_path(sdir, config.name, session)
    artifact_data: dict[str, Any] | None = None
    schema = getattr(config, "schema", None)
    is_terminal = bool(
        schema and target_phase == schema.phases.terminal
    )
    if is_terminal and schema is not None:
        from .schema import SchemaSlots, build_final_artifact
        if isinstance(wstate.slots, SchemaSlots):
            clarity = _compute_clarity_score(wstate, schema)
            artifact_data = build_final_artifact(
                schema,
                wstate.slots,
                session_id=session,
                turns_used=wstate.iteration,
                clarity_score=clarity,
                желание=wstate.user_history,
                bot_history=wstate.bot_history,
            )
            try:
                import yaml as _yaml
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    _yaml.safe_dump(
                        artifact_data,
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
                wstate.action_log.append(
                    f"artifact: wrote {artifact_path.name} "
                    f"clarity={clarity:.3f}"
                )
                # Re-save state so the action_log line lands on disk too
                wstate.save(state_path)
            except Exception as e:
                wstate.action_log.append(
                    f"artifact: write failed ({type(e).__name__}: {e})"
                )

    result = {
        "workflow": config.name,
        "phase": target_phase,
        "iteration": wstate.iteration,
        "state_summary": wstate.summary(),
        "mini_prompt": mini_prompt,
        "instructions_for_bot": instructions,
        "state_file": str(state_path),
    }
    if artifact_data is not None:
        result["artifact_file"] = str(artifact_path)
        result["artifact"] = artifact_data
    return result


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
