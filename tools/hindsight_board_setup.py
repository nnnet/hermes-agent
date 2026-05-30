"""hindsight_board_setup — per-board Hindsight bank + project mental-model.

Hooked into the chief-board lifecycle alongside git/workspace creation
(see hermes_cli/kanban_db.py:_maybe_init_github_mirror). Whenever a
board's GitHub mirror is initialised, this module also makes sure the
matching Hindsight bank and project-overview mental-model exist.

Division of labour between git and the mental-model
---------------------------------------------------
By operator policy 2026-05-23, the two stores cover DIFFERENT slices of
project state — they are deliberately non-overlapping to keep them
from drifting against each other:

    GIT (workspaces/<board>/...)
        →  WHAT was done — commits, file changes, code structure.
           Source of truth for artefacts. Read via `git log`,
           `git diff`, file reads. Mechanically reconstructible.

    HINDSIGHT mental-model `project-overview` in bank `hermes-board-<slug>`
        →  WHY / WHAT-FOR — motivation, current goals, decisions and
           their rationale, rejected alternatives (with reasoning),
           operator-imposed prohibitions, open questions, blockers.
           NOT a description of files or commits — that's git's job.

This split means recall'ing the project's mental-model gives Hermes
the intent layer; the workspace dir gives him the artefact layer.
Together they cover «что нужно знать про этот проект» without
duplication.

Behaviour
---------
Idempotent: bank/model existence is checked before create. Failures
are best-effort — they log a warning and return False, never raise,
so a Hindsight outage cannot break workspace/git creation.

Cache: a process-level set of initialised board slugs short-circuits
repeat calls (same pattern as github_app_workspace's mirror cache).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "http://127.0.0.1:8888"
_BANK_PREFIX = "hermes-board-"
_PROJECT_MODEL_ID = "project-overview"

# Process-level cache: slugs we've already attempted (success or
# permanent failure). Transient failures clear from the cache so the
# next call retries.
_initialised: set[str] = set()


def _api_url() -> str:
    """Hindsight API URL — env override → fallback to loopback (host net)."""
    return os.environ.get("HINDSIGHT_API_URL", _DEFAULT_API_URL).rstrip("/")


def _bank_id_for(board_slug: str) -> str:
    """Stable bank id derivation: ``hermes-board-<sanitized-slug>``.

    Slug is already sanitized by callers (see github_app_workspace
    ``_normalize_board_slug``) — we just prefix.
    """
    return f"{_BANK_PREFIX}{board_slug}"


def _project_overview_source_query(board_slug: str) -> str:
    """Source query for the project-overview mental-model.

    Deliberately scoped to intent / rationale / prohibitions / goals —
    the things git CANNOT tell you. The model is INSTRUCTED to skip
    file-level descriptions and commit summaries.
    """
    return (
        f"Проект «{board_slug}». Изложи структурированно ТОЛЬКО следующие "
        f"аспекты проекта (не описывай файлы и коммиты — это в git'е):\n\n"
        f"1. ЦЕЛЬ проекта — для чего он существует, какую задачу решает, "
        f"какой пользовательский результат должен быть.\n"
        f"2. ТЕКУЩИЕ ПРИОРИТЕТЫ — что именно сейчас в работе, в каком "
        f"порядке, какой следующий milestone.\n"
        f"3. ПРИНЯТЫЕ РЕШЕНИЯ С ОБОСНОВАНИЕМ — какие архитектурные / "
        f"процессные / технические решения приняты и ПОЧЕМУ именно так "
        f"(rationale, trade-offs).\n"
        f"4. ОТВЕРГНУТЫЕ АЛЬТЕРНАТИВЫ — что попробовали или рассмотрели "
        f"и отказались, какова причина отказа (важно чтобы не "
        f"возвращаться).\n"
        f"5. ЗАПРЕТЫ И ОГРАНИЧЕНИЯ — что оператор явно ЗАПРЕТИЛ "
        f"делать в этом проекте (правила-табу).\n"
        f"6. ОТКРЫТЫЕ ВОПРОСЫ — что ещё не решено и блокирует прогресс.\n"
        f"\n"
        f"Если по какому-то пункту нет данных — напиши «—». НЕ выдумывай."
    )


def _http_json(method: str, path: str, body: Optional[dict] = None,
               timeout: float = 5.0) -> tuple[int, Optional[dict]]:
    """Minimal JSON HTTP — returns (status_code, parsed_body_or_None).

    Avoid pulling httpx into this hook path; stdlib urllib keeps the
    chief-board init lean. On any transport error returns (0, None) —
    caller treats it as «could not reach Hindsight».
    """
    url = f"{_api_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, None
    except Exception as exc:
        logger.debug("hindsight_board_setup: %s %s failed: %s", method, path, exc)
        return 0, None


def _ensure_bank(bank_id: str, *, board_slug: str) -> bool:
    """Create the bank if it doesn't exist. Returns True if bank exists at end."""
    status, body = _http_json("GET", f"/v1/default/banks/{bank_id}")
    if status == 200:
        return True  # already exists
    if status == 0:
        return False  # Hindsight unreachable
    # Anything else (404, 405, ...) — try to create.
    payload = {
        "bank_id": bank_id,
        "name": board_slug,
        "mission": (
            f"Per-board memory for «{board_slug}». Keeps WHY / goals / "
            f"rejected alternatives / prohibitions. The WHAT (commits, "
            f"files) lives in the matching GitHub mirror repo."
        ),
        "disposition": {"skepticism": 4, "literalism": 4, "empathy": 2},
    }
    status, _ = _http_json("POST", "/v1/default/banks", payload, timeout=8.0)
    if 200 <= status < 300:
        logger.info("hindsight_board_setup: created bank %s for board %s",
                    bank_id, board_slug)
        return True
    logger.warning("hindsight_board_setup: failed to create bank %s — HTTP %s",
                   bank_id, status)
    return False


def _ensure_project_overview(bank_id: str, board_slug: str) -> bool:
    """Create the project-overview mental-model in the bank."""
    status, _ = _http_json(
        "GET",
        f"/v1/default/banks/{bank_id}/mental-models/{_PROJECT_MODEL_ID}?detail=metadata",
    )
    if status == 200:
        return True  # already there
    if status == 0:
        return False  # Hindsight unreachable

    payload = {
        "id": _PROJECT_MODEL_ID,
        "name": f"Project Overview — {board_slug} (intent / rationale / goals)",
        "source_query": _project_overview_source_query(board_slug),
        "max_tokens": 2000,
        "trigger": {
            "mode": "full",
            "refresh_after_consolidation": True,
        },
        "tags": [f"board:{board_slug}", "project-overview"],
    }
    status, _ = _http_json(
        "POST",
        f"/v1/default/banks/{bank_id}/mental-models",
        payload,
        timeout=8.0,
    )
    if 200 <= status < 300:
        logger.info(
            "hindsight_board_setup: created project-overview mental-model "
            "in bank %s", bank_id,
        )
        return True
    logger.warning(
        "hindsight_board_setup: failed to create project-overview model in "
        "%s — HTTP %s", bank_id, status,
    )
    return False


def init_board_hindsight(board_slug: str) -> bool:
    """Ensure ``hermes-board-<slug>`` bank + ``project-overview`` model exist.

    Idempotent. Best-effort: a Hindsight outage logs a warning and
    returns False, never raises — chief-board lifecycle must NOT
    block on memory infrastructure.

    Process-cached on success so subsequent calls within the same
    dispatcher process short-circuit.

    NOTE — incomplete routing (2026-05-23):
        The bank + model are created here, but worker profiles do NOT
        yet retain observations INTO this bank. ``bank_id_template`` in
        the operator's hindsight config is ``hermes-{profile}`` and has
        no ``{board}`` placeholder, so retains flow to per-profile banks
        instead. Result: today the project-overview model is a stub
        synthesised from an empty observation set.

        Two routing options (operator pick required):
          (a) Add ``{board}`` placeholder + resolver in
              ``plugins/memory/hindsight._resolve_bank_id_template`` so
              workers attached to a board write into the board-bank
              directly.
          (b) Keep per-profile banks; add a cross-bank recall wrapper
              that pulls from per-profile bank AND the board-bank's
              ``project-overview`` model by tag (``board:<slug>``).

        See hermes_cli/kanban_db.py:_maybe_init_github_mirror docblock
        and infra/hermes/docs/hindsight-memory-guide.md §5 for the
        full split + routing rationale.
    """
    if not board_slug:
        return False
    if board_slug in _initialised:
        return True

    bank_id = _bank_id_for(board_slug)
    if not _ensure_bank(bank_id, board_slug=board_slug):
        return False
    if not _ensure_project_overview(bank_id, board_slug):
        # Bank exists but model didn't — don't cache, retry next time.
        return False

    _initialised.add(board_slug)
    return True


__all__ = [
    "init_board_hindsight",
]
