#!/usr/bin/env python3
"""workflow-engine CLI — load a workflow definition, run one step.

A workflow is supplied as a directory containing an `__init__.py` (or a
single `config.py`) that exports a `WORKFLOW` (or legacy `SKILL_CONFIG`)
constant of type `core.WorkflowConfig`.

Usage:

  python3 cli.py --workflow PATH/TO/workflow_dir \\
      --session sess-001 --user "..."

  python3 cli.py --workflow PATH --session sess-001 --status
  python3 cli.py --workflow PATH --session sess-001 --reset

Output: JSON with workflow, phase, iteration, mini_prompt,
instructions_for_bot, state_summary, state_file.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path

# Make core/ importable when running directly from a checkout.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def load_workflow_config(workflow_path: Path):
    """Load WORKFLOW (or legacy SKILL_CONFIG) constant from a workflow path.

    Two layouts accepted:
      • directory: must contain `__init__.py` exporting WORKFLOW.
        We add the parent dir to sys.path and import by directory name.
        Relative imports inside the package (e.g. `from . import prompts`)
        work naturally.
      • single file: a config.py with `WORKFLOW` at module level.
    """
    if not workflow_path.exists():
        raise SystemExit(f"workflow path does not exist: {workflow_path}")

    if workflow_path.is_dir():
        init_py = workflow_path / "__init__.py"
        if not init_py.exists():
            raise SystemExit(
                f"workflow dir missing __init__.py: {workflow_path}"
            )
        parent = workflow_path.parent
        pkg_name = workflow_path.name
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))
        try:
            mod = importlib.import_module(pkg_name)
        except ImportError as e:
            raise SystemExit(f"failed to import workflow {pkg_name!r}: {e}")
    else:
        # Single-file: load by absolute path, no package context.
        spec = importlib.util.spec_from_file_location(
            workflow_path.stem, workflow_path
        )
        if spec is None or spec.loader is None:
            raise SystemExit(f"could not load workflow file: {workflow_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    cfg = getattr(mod, "WORKFLOW", None) or getattr(mod, "SKILL_CONFIG", None)
    if cfg is None:
        raise SystemExit(
            f"workflow module {workflow_path} does not export WORKFLOW "
            f"(or legacy SKILL_CONFIG)"
        )
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--workflow",
        required=False,
        default=None,
        help="Path to workflow directory (or single .py file) exporting WORKFLOW.",
    )
    ap.add_argument("--session", default=None, help="Session id (used as state file name)")
    ap.add_argument("--user", default=None, help="Latest user message")
    ap.add_argument("--prev-bot", default=None, help="Previous bot reply for slot extraction")
    ap.add_argument("--state-dir", default=None, help="Override state dir")
    ap.add_argument("--status", action="store_true", help="Report state without advancing")
    ap.add_argument("--reset", action="store_true", help="Reset state for this session")
    args = ap.parse_args()

    if not args.workflow:
        print(json.dumps({"error": "--workflow PATH required"}), file=sys.stderr)
        return 2
    if not args.session:
        print(json.dumps({"error": "--session required"}), file=sys.stderr)
        return 2

    config = load_workflow_config(Path(args.workflow).expanduser().resolve())

    from core import run, run_status, run_reset

    state_dir = Path(args.state_dir) if args.state_dir else None

    if args.reset:
        out = run_reset(config, args.session, state_dir)
    elif args.status:
        out = run_status(config, args.session, state_dir)
    else:
        if not args.user:
            print(json.dumps({"error": "--user required for advance"}), file=sys.stderr)
            return 2
        out = run(config, args.session, args.user, args.prev_bot, state_dir)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
