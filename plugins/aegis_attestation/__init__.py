"""aegis-attestation plugin — Tier-A deterministic deliverable verifier.

Why: Closes the "did the worker actually write the files it claims?" gap
between a `kanban_block(reason='review-required:')` handoff and a human
reviewer's eyeball-pass. Pure stdlib, no LLM, no network — runs in
microseconds per task and produces a tamper-evident (HMAC) comment that
anyone can re-verify.

What: Registers the `hermes aegis` CLI subcommand (with `tick`, `status`,
and `config` sub-subcommands). The plugin does NOT start a background
thread on session-start — operators are expected to invoke `hermes aegis
tick` from cron / systemd / `make` so the lifecycle stays explicit.

Test: `hermes aegis tick --board <slug>` on a board with a synthetic
blocked task; see plugins/aegis_attestation/tests/ for unit coverage
(`pytest plugins/aegis_attestation/tests/ -v -o "addopts="`).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from . import attestation as _att

logger = logging.getLogger("hermes.aegis_attestation")


# ---------------------------------------------------------------------------
# YAML config loader (optional overrides from ~/.hermes/config.yaml)
# ---------------------------------------------------------------------------

def _load_yaml_section() -> dict[str, Any]:
    """Why: Operators want to tweak reason_prefix / required_keys / unblock
    behaviour without editing source — but the plugin must still work on a
    fresh box where neither ~/.hermes/config.yaml nor hermes_cli are present.
    What: Reads the ``aegis_attestation`` section from the global hermes
    config via ``hermes_cli.config.load_config``; falls back to a direct
    ``yaml.safe_load`` of ``~/.hermes/config.yaml``; returns ``{}`` on any
    failure so callers can always ``.get(...)``.
    Test: ``test_no_yaml_section_uses_defaults`` (mock load_config -> {})
    and ``test_load_config_returns_none_safe`` (mock load_config -> None).
    """
    try:
        from hermes_cli.config import load_config  # type: ignore  # noqa: PLC0415

        cfg = load_config()
        if not isinstance(cfg, dict):
            return {}
        section = cfg.get("aegis_attestation")
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # pragma: no cover - exercised via direct-yaml fallback
        logger.debug("hermes_cli.config.load_config unavailable: %s", exc)

    # Fallback: read ~/.hermes/config.yaml directly with stdlib + PyYAML.
    try:
        import yaml  # type: ignore  # noqa: PLC0415

        path = Path("~/.hermes/config.yaml").expanduser()
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text()) or {}
        section = data.get("aegis_attestation") if isinstance(data, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("direct yaml fallback failed: %s", exc)
        return {}


def _build_cfg(args: argparse.Namespace) -> _att.AegisConfig:
    """Why: Single source of truth for "how does the tick command decide its
    config?" — CLI flags take precedence over YAML, YAML over dataclass
    defaults. Keeping this out of ``_cmd_tick`` makes it unit-testable
    without an argparse subprocess.
    What: Loads the optional ``aegis_attestation:`` section, maps known
    fields onto :class:`AegisConfig` kwargs (list->tuple for required_keys,
    str->Path for workspace_root_override), then applies the ``--no-unblock``
    CLI override last.
    Test: ``TestYamlConfigLoader`` covers each branch (override, type-cast,
    CLI-wins, None-safe).
    """
    section = _load_yaml_section()
    kwargs: dict[str, Any] = {}

    if isinstance(section.get("reason_prefix"), str):
        kwargs["reason_prefix"] = section["reason_prefix"]
    if isinstance(section.get("handoff_marker"), str):
        kwargs["handoff_marker"] = section["handoff_marker"]
    if isinstance(section.get("required_keys"), (list, tuple)):
        kwargs["required_keys"] = tuple(
            str(k) for k in section["required_keys"] if isinstance(k, str)
        )
    if isinstance(section.get("auto_unblock_on_pass"), bool):
        kwargs["auto_unblock_on_pass"] = section["auto_unblock_on_pass"]
    if isinstance(section.get("hmac_secret_env"), str):
        kwargs["hmac_secret_env"] = section["hmac_secret_env"]
    wro = section.get("workspace_root_override")
    if isinstance(wro, str) and wro.strip():
        kwargs["workspace_root_override"] = Path(wro).expanduser()

    # CLI --no-unblock always wins over YAML (defensive default = True).
    if getattr(args, "no_unblock", False):
        kwargs["auto_unblock_on_pass"] = False

    return _att.AegisConfig(**kwargs)


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def _build_argparse(subparser: argparse.ArgumentParser) -> None:
    """Why: PluginContext.register_cli_command hands us a subparser; we own
    the inner argument tree so the user sees `hermes aegis tick [--board X]`
    not a flat option soup.
    What: Adds three sub-subcommands — tick, status, config — each with its
    own argparse defaults so `func(args)` dispatch works.
    Test: argparse.parse_args(['aegis', 'tick', '--board', 'cookai']) gives
    args.aegis_command == 'tick' and args.board == 'cookai'.
    """
    subs = subparser.add_subparsers(dest="aegis_command")

    tick_p = subs.add_parser(
        "tick",
        help="Scan kanban for review-required blocked tasks, verify deliverables, "
             "post attestation comments, optionally unblock on pass.",
    )
    tick_p.add_argument(
        "--board", default=None,
        help="Kanban board slug (defaults to the active board).",
    )
    tick_p.add_argument(
        "--no-unblock", action="store_true",
        help="Verify and comment, but do not unblock on pass.",
    )
    tick_p.add_argument(
        "--json", action="store_true",
        help="Emit the TickSummary as JSON to stdout (for cron / dashboards).",
    )
    tick_p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Set log level to DEBUG.",
    )
    tick_p.set_defaults(func=_cmd_tick)

    status_p = subs.add_parser(
        "status",
        help="Print effective config and a count of currently-blocked review-required tasks.",
    )
    status_p.add_argument("--board", default=None)
    status_p.set_defaults(func=_cmd_status)

    cfg_p = subs.add_parser(
        "config",
        help="Print the default AegisConfig as JSON.",
    )
    cfg_p.set_defaults(func=_cmd_config)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_tick(args: argparse.Namespace) -> int:
    """Why: One-shot entry the cron task hits; must be idempotent.
    What: Builds an AegisConfig honouring --no-unblock, calls attestation.tick,
    prints either a human-friendly line or a JSON blob.
    Test: Mock attestation.tick to return a fake TickSummary, assert exit
    code 0 and the expected stdout shape.
    """
    if getattr(args, "verbose", False):
        logging.getLogger("hermes.aegis_attestation").setLevel(logging.DEBUG)

    cfg = _build_cfg(args)

    try:
        summary = _att.tick(board=getattr(args, "board", None), cfg=cfg)
    except Exception as exc:
        logger.exception("Aegis tick failed: %s", exc)
        return 2

    if getattr(args, "json", False):
        payload = {
            "inspected": summary.inspected,
            "skipped": summary.skipped,
            "attested": summary.attested,
            "passed": summary.passed,
            "failed": summary.failed,
            "unblocked": summary.unblocked,
            "results": [r.to_payload() for r in summary.results],
        }
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(
            f"aegis-attest tick: inspected={summary.inspected} "
            f"attested={summary.attested} passed={summary.passed} "
            f"failed={summary.failed} unblocked={summary.unblocked} "
            f"skipped={summary.skipped}"
        )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Why: Quick "is the plugin alive and seeing my tasks?" check without
    actually mutating the DB.
    What: Counts blocked tasks with matching reason prefix; prints config.
    Test: Run against a kanban with zero matching tasks, assert 'blocked
    review-required tasks: 0' in output.
    """
    cfg = _att.AegisConfig()
    print(f"AegisConfig: {json.dumps(_config_to_dict(cfg), indent=2, sort_keys=True)}")
    try:
        from hermes_cli import kanban_db  # type: ignore  # noqa: PLC0415
    except Exception as exc:
        print(f"kanban_db unavailable: {exc}", file=sys.stderr)
        return 1
    board = getattr(args, "board", None)
    db_path = kanban_db.kanban_db_path(board)
    if not db_path.exists():
        print(f"kanban db not found: {db_path}")
        return 0
    conn = kanban_db.connect(db_path)
    blocked = kanban_db.list_tasks(conn, status="blocked")
    matching = 0
    for task in blocked:
        if _att._task_matches_reason_prefix(conn, kanban_db, task, cfg.reason_prefix):
            matching += 1
    conn.close()
    print(f"blocked review-required tasks: {matching} / {len(blocked)} total blocked")
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    """Why: Operators wiring up cron need a quick way to see what defaults
    look like without grepping the source.
    What: Dumps AegisConfig() as JSON to stdout.
    Test: Exit code 0, stdout contains the key 'reason_prefix'.
    """
    print(json.dumps(_config_to_dict(_att.AegisConfig()), indent=2, sort_keys=True))
    return 0


def _config_to_dict(cfg: _att.AegisConfig) -> dict[str, Any]:
    """Why: Path objects aren't JSON-serialisable; AegisConfig holds one.
    What: Returns a dict view of cfg with Path coerced to str.
    Test: Set workspace_root_override=Path('/tmp/x'); assert the resulting
    dict has 'workspace_root_override' == '/tmp/x'.
    """
    d = asdict(cfg)
    if cfg.workspace_root_override is not None:
        d["workspace_root_override"] = str(cfg.workspace_root_override)
    # tuples don't survive JSON nicely — list-ify for output stability
    d["required_keys"] = list(d.get("required_keys") or [])
    return d


# ---------------------------------------------------------------------------
# Plugin entrypoints
# ---------------------------------------------------------------------------

def cli_main(argv: Optional[list[str]] = None) -> int:
    """Why: Lets the plugin be invoked as ``python -m hermes_plugins.aegis_attestation``
    when, for some reason, the hermes CLI is unavailable (rescue path).
    What: Builds a standalone argparse parser, dispatches to handlers.
    Test: cli_main(['config']) prints JSON and returns 0.
    """
    parser = argparse.ArgumentParser(prog="aegis", description="Aegis attestation CLI")
    _build_argparse(parser)
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args) or 0)


def register(ctx: Any) -> None:
    """Why: Hermes plugin loader calls this with a PluginContext; we hook
    `register_cli_command` to expose `hermes aegis ...`.
    What: Wires the argparse subtree built by _build_argparse.
    Test: After ctx.register_cli_command is mocked, assert called with
    name='aegis' and setup_fn=_build_argparse.
    """
    try:
        ctx.register_cli_command(
            name="aegis",
            help="Tier-A deterministic attestation for kanban deliverables.",
            setup_fn=_build_argparse,
            description=(
                "Run `hermes aegis tick` (typically from cron) to scan "
                "review-required blocked tasks, verify declared deliverables "
                "via sha256, post an HMAC-signed attestation comment, and "
                "optionally unblock on pass."
            ),
        )
        logger.debug("aegis-attestation: registered `hermes aegis` CLI command")
    except AttributeError:
        # Older plugin loaders may not expose register_cli_command — degrade
        # gracefully so the plugin still imports cleanly. Operators can fall
        # back to `python -m hermes_plugins.aegis_attestation`.
        logger.warning(
            "PluginContext.register_cli_command unavailable; "
            "aegis-attestation reachable only via `python -m hermes_plugins.aegis_attestation`"
        )
