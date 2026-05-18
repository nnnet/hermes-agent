---
name: self-critique
description: Mandatory pre-completion self-review pattern for any kanban worker. Forces the worker to enumerate the gaps in its own work BEFORE calling kanban_complete, catching the "I'm tired, ship it" failure mode without spinning up a separate reviewer agent. Pair with aegis_review (Tier-B LLM review) for two-layer quality.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, quality, review, self-critique, reflexion]
    related_skills: [kanban-worker, kanban-orchestrator, chief-manager]
---

# Self-Critique — Pre-Completion Checklist

> Load this skill when you're a worker (kanban-worker, chief-manager, or domain expert) about to mark a task `done`. It's a forcing function: you cannot call `kanban_complete` until you've explicitly answered the questions below.

## Why

Workers consistently mark tasks `done` while skipping non-obvious acceptance criteria, especially under iteration-budget pressure. The Reflexion paper (Shinn et al. 2023) showed that a single LLM-generated self-critique step before final answer improves task success by 10-25% across coding, reasoning, and decision tasks. This skill enforces that step.

## When to invoke

**Mandatory** before EVERY `kanban_complete` call on a non-trivial task. Skip allowed only for:
- Task body says "trivial" / "noop" / "smoke" explicitly
- You're the dispatcher claiming back a stale task (not a real worker)

## Procedure (do this in your reasoning, then act)

Answer all four questions HONESTLY. If any answer reveals a gap — do NOT complete; either fix the gap or `kanban_block` with the gap as the reason.

### 1. Acceptance criteria coverage

Re-read the task body. Enumerate every acceptance criterion (explicit or implicit). For each:
- ✅ "I produced X (file/output/comment) which evidences this criterion is met"
- ❌ "Not addressed because [reason]" → this is a blocker

If the body is vague (e.g. "fix the bug"), state your interpretation of "fixed" and ask: does the evidence really show that, or only that "the error went away in one test run"?

### 2. Skipped paths

What did you NOT try? Specifically:
- Tools you considered using but skipped (why?)
- Edge cases mentioned in the brief that your solution doesn't cover
- Configuration variants (different OS, providers, profiles) the brief implied
- Tests / verification steps you cut for time

Be specific. "I didn't test on Windows" is acceptable; "I tested everything" is a lie and self-disqualifies.

### 3. Bailout audit

Did you complete because the work is done, or because:
- ❌ Iteration budget ran out (`Iteration budget exhausted` in events)
- ❌ A tool kept failing and you gave up retrying
- ❌ The brief became unclear and you guessed
- ❌ You hit a sandbox / permission / IP-block / quota wall

If yes to any — DO NOT complete. Either:
- `kanban_block` with a precise reason + recovery options (e.g. "needs --cookies-from-browser firefox:/opt/firefox-profile flag — see task body footnote")
- Escalate via `kanban_comment` with a "@main:manager: I need X to proceed" prefix

### 4. Hallucination audit

For every artifact you claim to have produced:
- Path on disk — does it exist? (`ls`, `stat`, `cat` to verify)
- Code change — was it actually committed/saved? (`git status` / `git diff`)
- External effect (PR opened, message sent, etc.) — do you have the response receipt?

Workers under pressure routinely claim "wrote file X to /tmp/y.py" without actually writing it. Catch this before reviewer does.

## Output of self-critique

Post the result as a `kanban_comment` on your task BEFORE calling `kanban_complete`. Format:

```
SELF-CRITIQUE (pre-complete):
1. Acceptance: [✓ N/M criteria met] [✗ list any not addressed]
2. Skipped: <bullet of what you didn't do, with reasons>
3. Bailout: <"clean completion" | reason if blocked by budget/tool/etc>
4. Artifacts verified: <list of files/commits/external effects with timestamps>
Final confidence: <0.0..1.0> that this work meets the brief.
```

If `Final confidence < 0.7` — strongly consider another iteration or escalation instead of completing.

## Combining with Aegis Tier-B review

After self-critique passes, optionally call:

```python
aegis_review(task_id=<your task>)
```

This runs a separate LLM (different model, different perspective) over your task body + comments + result. If verdict=APPROVED → proceed to kanban_complete. If REJECTED → take the feedback as a list of gaps, address them, then self-critique again.

Two layers of review catch ~70% more issues than one. The cost is one extra LLM call per task (~$0.001 with haiku-class auxiliary model).

## Anti-patterns

❌ **Skipping when tired.** You're the most likely to miss issues when tired. The skill exists for exactly this moment.

❌ **Self-critique theatre.** Writing "1. Met. 2. Nothing. 3. Clean. 4. All verified." without actually checking is worse than not doing it at all — it creates a false signal in audit log.

❌ **Vague confidence.** "Confidence: high" is not a number. Give a real 0.0-1.0 estimate. Calibration improves over time.

❌ **Burying the bailout.** If you exhausted iteration budget, that is the headline reason in the block — not a footnote.

## Reference

- Reflexion: Language Agents with Verbal Reinforcement Learning (Shinn et al. 2023) — https://arxiv.org/abs/2303.11366
- Aegis Tier-A (deterministic): `plugins/aegis_attestation/attestation.py`
- Aegis Tier-B (LLM): `plugins/aegis_attestation/llm_review.py`, MCP tool `aegis_review`
