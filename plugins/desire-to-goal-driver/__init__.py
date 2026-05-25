"""desire-to-goal-driver — gateway plugin that drives the workflow-engine.

Closes the structural gap between the SKILL.md instruction
("call /opt/workflow-engine/cli.py") and what the main bot actually
does (often ignores the skill and answers from training).

Mechanism — pure plugin code, no upstream gateway/agent modification:

* Registers a ``pre_llm_call`` hook (lifecycle event documented in
  ``hermes_cli/plugins.py:VALID_HOOKS``).
* On every turn, checks the user's latest message against a copy of
  the upstream ``VAGUE_DESIRE_PATTERNS`` regex.
* When matched, shells to the workflow-engine CLI with the user's
  message + session id, captures the JSON result (phase, slots,
  confidence, mini_prompt, instructions, artifact path), and returns
  ``{"context": "<engine block>"}`` so the conversation_loop injects
  the block into the current turn's user message.

The bot then sees the engine's verbatim output in context and is
instructed to forward mini_prompt to TG. No tool call required on the
bot's side; the slot extractor, anti-pattern detectors and phase
routing have already run.

Why hook here vs gateway/run.py
    The upstream ``gateway/run.py`` does inject a static
    DESIRE_TO_GOAL_PRELOAD_NOTE when the F1-gate trigger fires, but
    that note still depends on the bot calling ``skill_view`` and then
    ``terminal_run`` against the engine CLI — two tool-call steps the
    bot often skips. ``pre_llm_call`` runs the engine eagerly so the
    output is in the conversation BEFORE the model decides whether to
    call any tool.

    Doing this via a plugin keeps upstream files untouched — important
    for fork hygiene; merges from NousResearch don't conflict on
    gateway/run.py.

Failure mode
    Engine subprocess timeout / non-zero exit / JSON parse error →
    hook returns ``None`` and the turn proceeds normally with just the
    upstream PRELOAD_NOTE. Container logs carry the engine error for
    diagnosis.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Copy of upstream VAGUE_DESIRE_PATTERNS (gateway/run.py). Kept in sync
# manually — if upstream extends its list, mirror it here. Slight
# divergence is acceptable; the upstream regex remains authoritative
# for the F1 gate's sticky-state activation, this one only gates the
# engine invocation from the plugin side.
_VAGUE_DESIRE_PATTERNS = [
    # Quality adverbs
    r"\bприбыльн", r"\bэффективн", r"\bудобн", r"\bпонятн",
    r"\bнадёжн", r"\bнадежн", r"\bкрасив", r"\bбыстр",
    # Build-verb means (the proposed tool)
    r"\bбот[\s,]", r"\bбота\b", r"\bдашборд", r"\bсайт",
    r"\bмагазин", r"\bсистем", r"\bинструмент", r"\bсервис",
    r"\bприложени", r"\bпайплайн", r"\bпроект", r"\bкоманд",
    r"\bкурс\b", r"\bканал\b",
    # Vague verbs (imperative)
    r"\bпомоги", r"\bспроектируй", r"\bпридумай", r"\bразработай",
    r"\bоптимизируй", r"\bулучши", r"\bавтоматизируй",
    r"\bразберись", r"\bспланируй", r"\bнайди мне\b",
    r"\bподбирай", r"\bмониторь",
    # Multi-step "сделай мне X" / "нужна штука для Y"
    r"\bсделай мне\b", r"\bнужна штука\b", r"\bнужн[ао] что-то",
    r"\bвыучи меня",
    # Extension: infinitive forms missed by upstream regex
    # (regular complaint — "автоматизировать" vs "автоматизируй").
    r"\bавтоматизировать", r"\bспроектировать", r"\bразработать",
    r"\bоптимизировать",
]
_VAGUE_DESIRE_RE = re.compile("|".join(_VAGUE_DESIRE_PATTERNS), re.IGNORECASE)


WORKFLOW_DIR = os.environ.get(
    "DESIRE_TO_GOAL_WORKFLOW_DIR",
    "/opt/data/skills/orientation/desire-to-goal/workflow",
)
ENGINE_CLI = os.environ.get(
    "DESIRE_TO_GOAL_ENGINE_CLI",
    "/opt/workflow-engine/cli.py",
)
PYTHON_BIN = os.environ.get(
    "DESIRE_TO_GOAL_PYTHON",
    "python3",
)
SUBPROCESS_TIMEOUT_SECONDS = float(
    os.environ.get("DESIRE_TO_GOAL_TIMEOUT", "75")
)


def _build_engine_block(payload: dict[str, Any]) -> str:
    """Format the engine's JSON return into a context block the bot
    should treat as authoritative for this turn."""
    mp = (payload.get("mini_prompt") or "").strip()
    phase = payload.get("phase", "")
    summary = payload.get("state_summary", {}) or {}
    slots = summary.get("slots") or {}
    instr = (payload.get("instructions_for_bot") or "").strip()
    iteration = summary.get("iteration", 0)
    completeness = summary.get("completeness", 0.0)

    block = (
        "[desire-to-goal workflow-engine — pre-computed reply]\n"
        f"Phase: {phase}\n"
        f"Iteration: {iteration}\n"
        f"Completeness: {completeness:.2f}\n"
        f"Slots: {json.dumps(slots, ensure_ascii=False)}\n"
        "\n"
        "**Your reply MUST be the mini_prompt below, verbatim — "
        "substitute `<placeholders>` from slots when slot value is "
        "present; otherwise keep the placeholder text. The engine "
        "already ran the slot extractor, programmatic anti-pattern "
        "detectors and phase routing for this turn. Do NOT re-derive "
        "slots. Do NOT brand-list. Do NOT add tool calls in this turn.**\n"
        "\n"
        f"<mini_prompt>\n{mp}\n</mini_prompt>\n"
    )
    if instr:
        block += f"\nEngine instruction: {instr}\n"
    if payload.get("artifact_file"):
        block += f"\nArtifact YAML written: {payload['artifact_file']}\n"
    return block


def _engine_session_active(session_id: str) -> bool:
    """True iff a workflow state file exists for this session and the
    workflow hasn't reached the terminal phase yet.

    Used to keep invoking the engine on follow-up turns even when the
    user's message no longer matches the vague-desire regex — once a
    clarification is in flight we must drive it to LOCK/DONE, not
    bail back to the bot's training defaults.
    """
    # Resolve state directory the same way runner.py does.
    state_dir = Path(
        os.environ.get(
            "WORKFLOW_STATE_DIR",
            os.path.join(
                os.environ.get("HERMES_HOME", "/opt/data"),
                "workflow_state",
            ),
        )
    )
    state_path = state_dir / "desire-to-goal" / f"{session_id}.json"
    if not state_path.exists():
        return False
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return raw.get("phase") not in {"DONE", None}


def _on_pre_llm_call(**kwargs: Any) -> Optional[dict[str, str]]:
    """pre_llm_call hook entry point.

    Returns ``{"context": "<engine block>"}`` so the conversation loop
    appends the block to the current turn's user message. Returns
    ``None`` on no-match or engine failure (degrades cleanly).

    Trigger conditions (ANY of):
      1. user_message matches the vague-desire regex (cold start of a
         clarification workflow), OR
      2. a workflow state file exists for this session and the
         workflow hasn't reached its terminal phase (continuation of
         an in-flight clarification — the upstream F1 gate's sticky
         flag handles this on its side but the engine call needs its
         own trigger because the regex is for INTAKE, not continuation).
    """
    user_msg = str(kwargs.get("user_message") or "").strip()
    if not user_msg:
        return None
    session_id = str(kwargs.get("session_id") or "default")

    regex_match = bool(_VAGUE_DESIRE_RE.search(user_msg))
    session_active = _engine_session_active(session_id)
    if not (regex_match or session_active):
        return None

    argv = [
        PYTHON_BIN, ENGINE_CLI,
        "--workflow", WORKFLOW_DIR,
        "--session", session_id,
        "--user", user_msg,
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "desire-to-goal-driver: engine timeout after %ss for session=%s",
            SUBPROCESS_TIMEOUT_SECONDS, session_id,
        )
        return None
    except Exception as exc:
        logger.warning("desire-to-goal-driver: subprocess failed: %s", exc)
        return None

    if result.returncode != 0 or not (result.stdout or "").strip():
        logger.warning(
            "desire-to-goal-driver: engine rc=%s session=%s stderr=%r",
            result.returncode, session_id,
            (result.stderr or "")[:300],
        )
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning(
            "desire-to-goal-driver: engine returned non-JSON: %s",
            exc,
        )
        return None

    block = _build_engine_block(payload)
    logger.info(
        "desire-to-goal-driver: injected engine block "
        "(phase=%s iter=%s session=%s)",
        payload.get("phase"),
        payload.get("iteration"),
        session_id,
    )
    return {"context": block}


def register(ctx) -> None:
    """Plugin entry point — called by the loader at startup."""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    logger.info("desire-to-goal-driver plugin registered pre_llm_call hook")
