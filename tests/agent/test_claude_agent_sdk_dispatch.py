"""Tests for the AIAgent dispatch site that invokes the Claude Agent SDK.

The SDK transport (Phase 2.1-2.3) handles format conversion + response
normalization in isolation; this file covers the *call site* — the
``run_agent.py`` branches that actually invoke ``claude_agent_sdk.query()``
when ``self.api_mode == 'claude_agent_sdk_single_turn'``:

* ``AIAgent._build_api_kwargs`` — dispatches to the SDK transport's
  ``build_kwargs`` and returns the sentinel-keyed dict.
* ``AIAgent._claude_agent_sdk_create`` — drains the async-iter
  ``query()`` into a list of SDK Message objects, with operator-friendly
  error mapping for missing SDK / CLI auth failures.
* ``AIAgent._interruptible_api_call`` — routes the
  ``claude_agent_sdk_single_turn`` mode through ``_claude_agent_sdk_create``.

The real ``claude_agent_sdk`` package is optional (extra
``hermes-agent[claude-agent-sdk]``).  Tests inject a fake module into
``sys.modules`` so they run without the extra installed.  Real CLI is
never spawned.

Why: validates that flipping a profile to ``provider: claude-agent-sdk``
will route through host subscription auth via the SDK rather than the
HTTP Anthropic adapter, without breaking any other api_mode path.

Test: ``pytest tests/agent/test_claude_agent_sdk_dispatch.py -v``.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest


# Make AIAgent importable without booting the gateway. ``fire`` /
# ``firecrawl`` / ``fal_client`` are optional deps imported eagerly at
# the top of run_agent.py; the standard pattern in tests/run_agent/ is to
# stub them out before the import.
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

from run_agent import AIAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Fake SDK — duck-typed stand-in for the optional claude_agent_sdk package
# ---------------------------------------------------------------------------


class _FakeOptions:
    """Stand-in for ``claude_agent_sdk.ClaudeAgentOptions``."""

    def __init__(
        self,
        *,
        model=None,
        system_prompt=None,
        allowed_tools=None,
        max_turns=None,
        cwd=None,
        **extra,
    ):
        self.model = model
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools
        self.max_turns = max_turns
        self.cwd = cwd
        self.extra = extra


class TextBlock:
    def __init__(self, text):
        self.text = text
        self.type = "text"


class ToolUseBlock:
    def __init__(self, id_, name, input_):
        self.id = id_
        self.name = name
        self.input = input_
        self.type = "tool_use"


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, stop_reason=None, total_cost_usd=None):
        self.stop_reason = stop_reason
        self.total_cost_usd = total_cost_usd


class _CLINotFoundError(Exception):
    """Stand-in for ``claude_agent_sdk.CLINotFoundError``."""


class _CLIConnectionError(Exception):
    """Stand-in for ``claude_agent_sdk.CLIConnectionError``."""


def _install_fake_sdk(monkeypatch, *, scripted_messages=None, raise_exc=None):
    """Inject a fake ``claude_agent_sdk`` module exposing ``query()``.

    ``scripted_messages`` is an iterable of fake SDK messages the async
    generator yields in order.  ``raise_exc`` (if set) replaces the
    generator output with a single raise — useful for testing the
    auth-error path.
    """
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _FakeOptions
    fake.CLINotFoundError = _CLINotFoundError
    fake.CLIConnectionError = _CLIConnectionError

    scripted = list(scripted_messages or [])

    async def _query(*, prompt, options):  # noqa: ARG001
        # Record the call so tests can assert on prompt/options being
        # forwarded correctly from the dispatch site.
        fake.last_call = {"prompt": prompt, "options": options}
        if raise_exc is not None:
            raise raise_exc
        for msg in scripted:
            yield msg

    fake.query = _query
    fake.last_call = None
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return fake


# ---------------------------------------------------------------------------
# AIAgent fixture — bare-minimum construction without booting the gateway
# ---------------------------------------------------------------------------


def _make_agent():
    """Build an AIAgent configured for ``claude_agent_sdk_single_turn``.

    The OAuth/HTTP client paths are stubbed because Hermes only needs the
    transport + the new SDK helper for this dispatch — no Anthropic SDK
    network calls happen on this api_mode.
    """
    # Pass explicit ``base_url`` + ``api_key`` so __init__ skips the
    # provider router (which doesn't know about the SDK provider —
    # auth lives in ~/.claude/.credentials.json, not env vars).  The
    # values themselves are unused by the SDK dispatch path; we only
    # need __init__ to complete without trying to resolve credentials.
    with patch("run_agent.OpenAI"), patch(
        "hermes_cli.config.load_config", return_value={"agent": {}}
    ):
        agent = AIAgent(
            api_key="sk-unused-cli-handles-auth",
            base_url="https://localhost-unused.invalid/v1",
            model="claude-sonnet-4-5",
            provider="anthropic",  # avoid provider-router rejection of unknown "claude-agent-sdk"
            api_mode="anthropic_messages",  # avoid __init__ overrides
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    # Flip into the SDK api_mode *after* __init__ completes so the
    # provider-router branch above accepts construction (the router
    # has no knowledge of "claude_agent_sdk_single_turn" by design —
    # auth lives in ~/.claude/.credentials.json).
    agent.api_mode = "claude_agent_sdk_single_turn"
    return agent


# ---------------------------------------------------------------------------
# _build_api_kwargs — dispatches to the SDK transport
# ---------------------------------------------------------------------------


class TestBuildApiKwargsForSdk:
    def test_routes_to_sdk_transport(self, monkeypatch):
        """Why: the build-kwargs dispatcher must select the right transport
        for ``api_mode='claude_agent_sdk_single_turn'``, so the rest of
        the loop sees sentinel keys for the SDK call path.

        Test: build kwargs with a tiny conversation, assert the dispatch
        sentinel + SDK options are present and ``__anthropic_tools__``
        carries the converted tools (Hermes' loop reads these).
        """
        fake_sdk = _install_fake_sdk(monkeypatch)
        agent = _make_agent()
        agent.tools = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echoes input",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        api_messages = [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Hello"},
        ]
        kw = agent._build_api_kwargs(api_messages)
        assert kw["__claude_agent_sdk__"] is True
        assert kw["prompt"] == "Hello"
        assert isinstance(kw["options"], _FakeOptions)
        assert kw["options"].max_turns == 1
        assert kw["options"].allowed_tools == []
        assert "__anthropic_tools__" in kw
        assert any(t.get("name") == "echo" for t in kw["__anthropic_tools__"])
        assert fake_sdk.last_call is None  # build_kwargs must not invoke query


# ---------------------------------------------------------------------------
# _claude_agent_sdk_create — drains query() into a list
# ---------------------------------------------------------------------------


class TestClaudeAgentSdkCreate:
    def test_drains_query_to_message_list(self, monkeypatch):
        """Why: the agent loop is synchronous and expects a concrete
        response object, not an async iterator.  ``_claude_agent_sdk_create``
        must drain ``query()`` end-to-end and return the list so the
        downstream transport-normalize path can run.

        Test: feed scripted messages, assert the returned list preserves
        order and contains both AssistantMessage and ResultMessage.
        """
        scripted = [
            AssistantMessage(content=[TextBlock("hi from sdk")]),
            ResultMessage(stop_reason="end_turn", total_cost_usd=0.01),
        ]
        fake_sdk = _install_fake_sdk(monkeypatch, scripted_messages=scripted)
        agent = _make_agent()
        api_kwargs = {
            "prompt": "Hello",
            "options": _FakeOptions(model="claude-sonnet-4-5"),
            "__claude_agent_sdk__": True,
        }
        result = agent._claude_agent_sdk_create(api_kwargs)
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], AssistantMessage)
        assert isinstance(result[1], ResultMessage)
        # Prompt + options forwarded verbatim — the SDK call boundary
        # must not silently rewrite them.
        assert fake_sdk.last_call["prompt"] == "Hello"
        assert fake_sdk.last_call["options"].model == "claude-sonnet-4-5"

    def test_missing_sdk_raises_runtime_error_with_install_hint(self, monkeypatch):
        """Why: a missing extra is a configuration error, not a
        programming bug — the message must point operators at the right
        install command instead of bubbling up a bare ImportError.

        Test: simulate ``import claude_agent_sdk`` failing, assert the
        resulting RuntimeError mentions ``hermes-agent[claude-agent-sdk]``.
        """
        monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)
        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "claude_agent_sdk":
                raise ImportError("No module named 'claude_agent_sdk'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        agent = _make_agent()
        with pytest.raises(RuntimeError) as excinfo:
            agent._claude_agent_sdk_create(
                {"prompt": "x", "options": None}
            )
        assert "hermes-agent[claude-agent-sdk]" in str(excinfo.value)

    def test_cli_not_found_remapped_to_runtime_error(self, monkeypatch):
        """Why: the SDK's ``CLINotFoundError`` is opaque to ops users.
        Map it to a RuntimeError pointing at ``claude /login`` on host.
        """
        fake_sdk = _install_fake_sdk(
            monkeypatch,
            raise_exc=_CLINotFoundError("claude binary not on PATH"),
        )
        agent = _make_agent()
        with pytest.raises(RuntimeError) as excinfo:
            agent._claude_agent_sdk_create(
                {"prompt": "x", "options": fake_sdk.ClaudeAgentOptions()}
            )
        assert "claude /login" in str(excinfo.value)

    def test_cli_connection_error_remapped(self, monkeypatch):
        """Why: ``CLIConnectionError`` is what the SDK raises when
        ``~/.claude/.credentials.json`` is missing/unreadable.  Surface
        an operator hint instead of the raw SDK exception.
        """
        fake_sdk = _install_fake_sdk(
            monkeypatch,
            raise_exc=_CLIConnectionError("auth dropped"),
        )
        agent = _make_agent()
        with pytest.raises(RuntimeError) as excinfo:
            agent._claude_agent_sdk_create(
                {"prompt": "x", "options": fake_sdk.ClaudeAgentOptions()}
            )
        msg = str(excinfo.value)
        assert ".credentials.json" in msg
        assert "claude /login" in msg

    def test_other_exceptions_propagate(self, monkeypatch):
        """Why: only auth/CLI errors are remapped.  Unknown errors
        propagate untouched so the outer retry loop can decide.
        """
        _install_fake_sdk(
            monkeypatch,
            raise_exc=RuntimeError("transient network blip"),
        )
        agent = _make_agent()
        with pytest.raises(RuntimeError) as excinfo:
            agent._claude_agent_sdk_create(
                {"prompt": "x", "options": _FakeOptions()}
            )
        assert "transient network blip" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _interruptible_api_call — routes SDK mode through _claude_agent_sdk_create
# ---------------------------------------------------------------------------


class TestInterruptibleApiCallDispatch:
    def test_sdk_api_mode_routes_to_sdk_create(self, monkeypatch):
        """Why: this is the call site the task brief named.  When the
        active api_mode is the SDK's, ``_interruptible_api_call`` must
        delegate to ``_claude_agent_sdk_create`` rather than the
        Anthropic / chat-completions / Bedrock branches.

        Test: stub ``_claude_agent_sdk_create`` and call
        ``_interruptible_api_call`` directly; assert it returns whatever
        the stub produces (i.e. the dispatch hit the right branch).
        """
        _install_fake_sdk(monkeypatch)
        agent = _make_agent()
        captured = {}

        def _stub_sdk_create(api_kwargs):
            captured["kw"] = api_kwargs
            return [
                AssistantMessage(content=[TextBlock("dispatched")]),
                ResultMessage(stop_reason="end_turn"),
            ]

        monkeypatch.setattr(agent, "_claude_agent_sdk_create", _stub_sdk_create)
        api_kwargs = {"prompt": "Hi", "options": _FakeOptions()}
        response = agent._interruptible_api_call(api_kwargs)
        assert captured["kw"] is api_kwargs
        assert isinstance(response, list)
        assert len(response) == 2

    def test_end_to_end_normalize_preserves_tool_use(self, monkeypatch):
        """Why: this is the whole point of Strategy A.  Tool-use blocks
        emitted by the SDK must survive normalization so Hermes' loop
        can execute the tools instead of the SDK's internal loop.

        Test: route a tool_use-bearing message stream through the full
        pipeline (build_kwargs → _interruptible_api_call → transport.
        normalize_response) and assert the NormalizedResponse contains
        the tool_call.
        """
        scripted = [
            AssistantMessage(
                content=[
                    TextBlock("Calling search"),
                    ToolUseBlock("toolu_1", "search", {"q": "python 3.13"}),
                ]
            ),
            ResultMessage(stop_reason="tool_use", total_cost_usd=0.002),
        ]
        _install_fake_sdk(monkeypatch, scripted_messages=scripted)
        agent = _make_agent()
        agent.tools = None

        api_messages = [
            {"role": "user", "content": "Find python 3.13 release notes"}
        ]
        kw = agent._build_api_kwargs(api_messages)
        # Sanity: the dispatch dict is what we expect to hand to the
        # call site.
        assert kw["__claude_agent_sdk__"] is True

        response = agent._interruptible_api_call(kw)
        assert isinstance(response, list) and len(response) == 2

        # Normalize through the SDK transport — production code at
        # ``run_agent.py:_finish_reason`` branch (sdk_api_mode) uses
        # ``self._get_transport()`` which resolves to this same
        # transport when api_mode == 'claude_agent_sdk_single_turn'.
        normalized = agent._get_transport(
            "claude_agent_sdk_single_turn"
        ).normalize_response(response)
        assert normalized.content == "Calling search"
        assert normalized.tool_calls and len(normalized.tool_calls) == 1
        assert normalized.tool_calls[0].name == "search"
        assert normalized.finish_reason == "tool_calls"
        assert normalized.provider_data == {"total_cost_usd": 0.002}
