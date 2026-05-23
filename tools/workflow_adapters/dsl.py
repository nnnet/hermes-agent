"""Hermes Workflow DSL — backend-agnostic YAML format.

Templates live in ``infra/hermes/workflows/*.yaml`` and are compiled
to backend-specific formats by each ``WorkflowAdapter.compile()``.

Schema (minimum viable):

    name: <slug>                 # template id, kebab-case
    version: <int>               # bumps on incompatible DSL changes
    description: <str>           # human-readable, optional
    inputs:                      # optional, list of typed slots
      - name: bankroll
        type: int                # int | float | str | bool
        default: 1000
    nodes:                       # required, list of work units
      - id: fetch
        profile: research-agent  # Hermes profile to invoke per node
        task: |
          Fetch Polymarket markets via Gamma API,
          output signals.json with real conditionIds.
        depends_on: []           # optional, list of node ids
        timeout_sec: 600         # optional, default 600
        inputs:                  # optional, passed to /run-profile
          bankroll: "{{ inputs.bankroll }}"
    on_failure: notify_operator  # optional, future use

Nodes execute in dependency order. A node's ``inputs`` can reference:
- ``{{ inputs.<name> }}`` — workflow-level inputs
- ``{{ steps.<node_id>.result }}`` — string result from a prior node
- ``{{ steps.<node_id>.result.<json-path> }}`` — extracted field
  (future; for now only top-level result strings are interpolated)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Validation


class DSLValidationError(ValueError):
    """Raised on malformed DSL. Message lists all problems found."""


_VALID_NAME = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_VALID_INPUT_TYPES = {"int", "float", "str", "bool"}


def _check(cond: bool, msg: str, errs: list[str]) -> None:
    if not cond:
        errs.append(msg)


def validate(dsl: dict[str, Any]) -> None:
    """Raise :class:`DSLValidationError` with full list of problems."""
    errs: list[str] = []

    if not isinstance(dsl, dict):
        raise DSLValidationError("DSL root must be a mapping")

    # name
    name = dsl.get("name")
    _check(isinstance(name, str) and bool(_VALID_NAME.match(name or "")),
           f"name: must match {_VALID_NAME.pattern!r} (got {name!r})", errs)

    # version
    version = dsl.get("version")
    _check(isinstance(version, int) and version >= 1,
           f"version: must be a positive int (got {version!r})", errs)

    # description (optional, but if present must be string)
    desc = dsl.get("description")
    if desc is not None:
        _check(isinstance(desc, str),
               f"description: must be a string (got {type(desc).__name__})", errs)

    # inputs (optional)
    inputs = dsl.get("inputs", [])
    _check(isinstance(inputs, list), "inputs: must be a list", errs)
    input_names: set[str] = set()
    if isinstance(inputs, list):
        for i, inp in enumerate(inputs):
            if not isinstance(inp, dict):
                errs.append(f"inputs[{i}]: must be a mapping")
                continue
            iname = inp.get("name")
            itype = inp.get("type")
            _check(isinstance(iname, str) and bool(iname),
                   f"inputs[{i}].name: required string", errs)
            _check(itype in _VALID_INPUT_TYPES,
                   f"inputs[{i}].type: must be one of {sorted(_VALID_INPUT_TYPES)} (got {itype!r})", errs)
            if isinstance(iname, str):
                if iname in input_names:
                    errs.append(f"inputs[{i}].name: duplicate {iname!r}")
                input_names.add(iname)

    # nodes (required, non-empty)
    nodes = dsl.get("nodes")
    _check(isinstance(nodes, list) and len(nodes) >= 1,
           "nodes: must be a non-empty list", errs)
    node_ids: set[str] = set()
    if isinstance(nodes, list):
        for i, n in enumerate(nodes):
            if not isinstance(n, dict):
                errs.append(f"nodes[{i}]: must be a mapping")
                continue
            nid = n.get("id")
            _check(isinstance(nid, str) and bool(_VALID_NAME.match(nid or "")),
                   f"nodes[{i}].id: must match {_VALID_NAME.pattern!r} (got {nid!r})", errs)
            profile = n.get("profile")
            _check(isinstance(profile, str) and bool(profile),
                   f"nodes[{i}].profile: required string", errs)
            task = n.get("task")
            _check(isinstance(task, str) and bool(task and task.strip()),
                   f"nodes[{i}].task: required non-empty string", errs)
            timeout_sec = n.get("timeout_sec", 600)
            _check(isinstance(timeout_sec, int) and timeout_sec > 0,
                   f"nodes[{i}].timeout_sec: must be positive int (got {timeout_sec!r})", errs)
            deps = n.get("depends_on", [])
            _check(isinstance(deps, list),
                   f"nodes[{i}].depends_on: must be a list", errs)
            if isinstance(nid, str):
                if nid in node_ids:
                    errs.append(f"nodes[{i}].id: duplicate {nid!r}")
                node_ids.add(nid)

        # Dependency closure check — every depends_on must reference a real node
        for i, n in enumerate(nodes):
            if not isinstance(n, dict):
                continue
            deps = n.get("depends_on") or []
            if isinstance(deps, list):
                for d in deps:
                    if d not in node_ids:
                        errs.append(
                            f"nodes[{i}].depends_on: references unknown node {d!r}"
                        )

        # Cycle detection (Kahn's algorithm)
        if not errs:  # skip cycle check if structure is broken
            in_degree = {nid: 0 for nid in node_ids}
            edges: dict[str, list[str]] = {nid: [] for nid in node_ids}
            for n in nodes:
                for d in (n.get("depends_on") or []):
                    in_degree[n["id"]] += 1
                    edges[d].append(n["id"])
            queue = [nid for nid, deg in in_degree.items() if deg == 0]
            visited = 0
            while queue:
                cur = queue.pop()
                visited += 1
                for nxt in edges[cur]:
                    in_degree[nxt] -= 1
                    if in_degree[nxt] == 0:
                        queue.append(nxt)
            if visited != len(node_ids):
                errs.append("nodes: dependency graph contains a cycle")

    if errs:
        raise DSLValidationError(
            "DSL validation failed:\n  - " + "\n  - ".join(errs)
        )


# ---------------------------------------------------------------------------
# Topological sort — used by simple adapters (inline) to plan execution

def topological_order(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return nodes in dependency order. Assumes ``validate()`` passed."""
    by_id = {n["id"]: n for n in nodes}
    in_degree = {nid: 0 for nid in by_id}
    edges: dict[str, list[str]] = {nid: [] for nid in by_id}
    for n in nodes:
        for d in (n.get("depends_on") or []):
            in_degree[n["id"]] += 1
            edges[d].append(n["id"])
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    ordered: list[dict[str, Any]] = []
    while queue:
        cur = queue.pop(0)
        ordered.append(by_id[cur])
        for nxt in edges[cur]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    return ordered


# ---------------------------------------------------------------------------
# Interpolation — simple Jinja-like `{{ ... }}` substitution

_INTERP_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def interpolate(
    template: Any,
    *,
    inputs: dict[str, Any],
    steps: dict[str, Any],
) -> Any:
    """Replace ``{{ inputs.X }}`` and ``{{ steps.X.result }}`` in strings.

    Recursively walks lists/dicts. Non-string leaves are returned as-is.
    Unknown references substitute the empty string and warn (caller
    decides whether to fail).
    """
    if isinstance(template, str):
        def _sub(m: re.Match) -> str:
            path = m.group(1).strip().split(".")
            if not path:
                return ""
            head = path[0]
            if head == "inputs":
                cur: Any = inputs
            elif head == "steps":
                cur = steps
            else:
                return ""
            for seg in path[1:]:
                if isinstance(cur, dict) and seg in cur:
                    cur = cur[seg]
                else:
                    return ""
            return "" if cur is None else str(cur)
        return _INTERP_RE.sub(_sub, template)
    if isinstance(template, list):
        return [interpolate(x, inputs=inputs, steps=steps) for x in template]
    if isinstance(template, dict):
        return {
            k: interpolate(v, inputs=inputs, steps=steps)
            for k, v in template.items()
        }
    return template


# ---------------------------------------------------------------------------
# Load + validate from YAML file

def load(path: str) -> dict[str, Any]:
    """Read YAML file, validate, return DSL dict."""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("PyYAML is required to load workflow DSL files") from e
    with open(path, "r", encoding="utf-8") as f:
        dsl = yaml.safe_load(f)
    validate(dsl)
    return dsl
