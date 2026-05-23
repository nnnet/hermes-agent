"""Pure-logic tests for the workflow DSL: validation, topo-sort,
interpolation. No network, no kanban, no MC required.
"""

from __future__ import annotations

import pytest

from tools.workflow_adapters import dsl as dsl_mod


# ---------------------------------------------------------------------------
# Validation


def _good_dsl():
    return {
        "name": "demo-pipeline",
        "version": 1,
        "description": "test",
        "inputs": [
            {"name": "bankroll", "type": "int", "default": 1000},
        ],
        "nodes": [
            {"id": "fetch", "profile": "research-agent", "task": "do thing"},
            {"id": "score", "profile": "trading-expert",
             "task": "score {{ steps.fetch.result }}",
             "depends_on": ["fetch"]},
        ],
    }


def test_validate_good():
    dsl_mod.validate(_good_dsl())  # no exception


def test_validate_missing_name():
    d = _good_dsl(); del d["name"]
    with pytest.raises(dsl_mod.DSLValidationError) as e:
        dsl_mod.validate(d)
    assert "name" in str(e.value)


def test_validate_bad_name():
    d = _good_dsl(); d["name"] = "BadName"  # uppercase invalid
    with pytest.raises(dsl_mod.DSLValidationError):
        dsl_mod.validate(d)


def test_validate_missing_version():
    d = _good_dsl(); del d["version"]
    with pytest.raises(dsl_mod.DSLValidationError) as e:
        dsl_mod.validate(d)
    assert "version" in str(e.value)


def test_validate_empty_nodes():
    d = _good_dsl(); d["nodes"] = []
    with pytest.raises(dsl_mod.DSLValidationError) as e:
        dsl_mod.validate(d)
    assert "nodes" in str(e.value)


def test_validate_duplicate_node_id():
    d = _good_dsl()
    d["nodes"].append({"id": "fetch", "profile": "x", "task": "y"})
    with pytest.raises(dsl_mod.DSLValidationError) as e:
        dsl_mod.validate(d)
    assert "duplicate" in str(e.value)


def test_validate_unknown_dep():
    d = _good_dsl()
    d["nodes"][1]["depends_on"] = ["nonexistent"]
    with pytest.raises(dsl_mod.DSLValidationError) as e:
        dsl_mod.validate(d)
    assert "unknown node" in str(e.value)


def test_validate_cycle():
    d = _good_dsl()
    d["nodes"] = [
        {"id": "alpha", "profile": "x", "task": "y", "depends_on": ["beta"]},
        {"id": "beta", "profile": "x", "task": "y", "depends_on": ["alpha"]},
    ]
    with pytest.raises(dsl_mod.DSLValidationError) as e:
        dsl_mod.validate(d)
    assert "cycle" in str(e.value)


def test_validate_input_type():
    d = _good_dsl()
    d["inputs"] = [{"name": "x", "type": "complex"}]
    with pytest.raises(dsl_mod.DSLValidationError):
        dsl_mod.validate(d)


# ---------------------------------------------------------------------------
# Topological order


def test_topo_simple():
    nodes = [
        {"id": "gamma", "depends_on": ["beta"]},
        {"id": "beta", "depends_on": ["alpha"]},
        {"id": "alpha"},
    ]
    out = dsl_mod.topological_order(nodes)
    assert [n["id"] for n in out] == ["alpha", "beta", "gamma"]


def test_topo_parallel():
    """When nodes have no inter-dependencies, all are equally valid in any
    order — but topo_order must still emit them all exactly once."""
    nodes = [
        {"id": "alpha"},
        {"id": "beta"},
        {"id": "gamma"},
    ]
    out = dsl_mod.topological_order(nodes)
    assert sorted(n["id"] for n in out) == ["alpha", "beta", "gamma"]


def test_topo_diamond():
    nodes = [
        {"id": "alpha"},
        {"id": "beta", "depends_on": ["alpha"]},
        {"id": "gamma", "depends_on": ["alpha"]},
        {"id": "delta", "depends_on": ["beta", "gamma"]},
    ]
    out = [n["id"] for n in dsl_mod.topological_order(nodes)]
    assert out[0] == "alpha"
    assert out[-1] == "delta"
    assert set(out[1:3]) == {"beta", "gamma"}


# ---------------------------------------------------------------------------
# Interpolation


def test_interpolate_inputs():
    s = dsl_mod.interpolate(
        "bankroll = {{ inputs.bankroll }}",
        inputs={"bankroll": 1000},
        steps={},
    )
    assert s == "bankroll = 1000"


def test_interpolate_steps_result():
    s = dsl_mod.interpolate(
        "data: {{ steps.fetch.result }}",
        inputs={},
        steps={"fetch": {"result": "[1,2,3]"}},
    )
    assert s == "data: [1,2,3]"


def test_interpolate_unknown_renders_empty():
    s = dsl_mod.interpolate(
        "missing: {{ inputs.nope }}",
        inputs={},
        steps={},
    )
    assert s == "missing: "


def test_interpolate_recursive_dict_list():
    out = dsl_mod.interpolate(
        {"a": "x {{ inputs.x }} y", "b": ["{{ inputs.x }}", 42, True]},
        inputs={"x": 7},
        steps={},
    )
    assert out == {"a": "x 7 y", "b": ["7", 42, True]}


def test_interpolate_passthrough_nonstring():
    assert dsl_mod.interpolate(42, inputs={}, steps={}) == 42
    assert dsl_mod.interpolate(None, inputs={}, steps={}) is None
    assert dsl_mod.interpolate(True, inputs={}, steps={}) is True
