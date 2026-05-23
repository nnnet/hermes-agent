"""Tests for ClaudeAgentSdkTransport (api_mode='claude_agent_sdk_single_turn').

The real ``claude_agent_sdk`` package is an optional extra and is *not*
required to import or unit-test the transport — all SDK access is
lazy.  These tests use a fake SDK injected via ``sys.modules`` so the
suite runs even when the extra isn't installed in CI.

Coverage:
  * api_mode property is the canonical string.
  * convert_messages / convert_tools delegate to anthropic_adapter
    (we don't re-test the adapter — just that the call is wired through).
  * build_kwargs pins max_turns=1 and allowed_tools=[], puts the
    converted history under sentinel keys, and produces a
    ClaudeAgentOptions instance.
  * normalize_response collects text + thinking + tool_use blocks
    across multiple AssistantMessage objects and reads ResultMessage's
    stop_reason / total_cost_usd.
  * normalize_response drains async iterators.
  * Missing-SDK error path surfaces the install hint, not bare
    ModuleNotFoundError.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

# Auto-discover by api_mode is the public entry point we want to exercise.
import agent.transports.claude_agent_sdk as cas_module  # noqa: E402
from agent.transports import get_transport
from agent.transports.claude_agent_sdk import ClaudeAgentSdkTransport
from agent.transports.types import NormalizedResponse


# ---------------------------------------------------------------------------
# Fake SDK
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


def _install_fake_sdk(monkeypatch):
    """Inject a fake ``claude_agent_sdk`` module into sys.modules."""
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _FakeOptions
    fake.query = lambda **kw: None  # not used directly by the transport
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return fake


# ---------------------------------------------------------------------------
# Fake SDK message objects (duck-typed — transport reads via getattr/classname)
# ---------------------------------------------------------------------------


class TextBlock:
    def __init__(self, text):
        self.text = text
        self.type = "text"


class ThinkingBlock:
    def __init__(self, thinking):
        self.thinking = thinking
        self.type = "thinking"


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport():
    return ClaudeAgentSdkTransport()


@pytest.fixture
def fake_sdk(monkeypatch):
    return _install_fake_sdk(monkeypatch)


# ---------------------------------------------------------------------------
# api_mode + registration
# ---------------------------------------------------------------------------


class TestApiMode:
    def test_api_mode_string(self, transport):
        assert transport.api_mode == "claude_agent_sdk_single_turn"

    def test_registered_in_registry(self):
        # Discovery happens lazily on first get_transport() call.
        instance = get_transport("claude_agent_sdk_single_turn")
        assert instance is not None
        assert isinstance(instance, ClaudeAgentSdkTransport)

    def test_matches_provider_profile_api_mode(self):
        """api_mode MUST equal the string declared by the provider profile."""
        from providers import get_provider_profile

        profile = get_provider_profile("claude-agent-sdk")
        assert profile is not None, (
            "claude-agent-sdk provider profile not discovered; "
            "Phase 1 (plugin metadata) must land before Phase 2."
        )
        assert ClaudeAgentSdkTransport().api_mode == profile.api_mode


# ---------------------------------------------------------------------------
# convert_messages / convert_tools — delegation only
# ---------------------------------------------------------------------------


class TestConvertDelegation:
    def test_convert_messages_delegates(self, transport, monkeypatch):
        called = {}

        def fake_convert(messages, base_url=None):
            called["messages"] = messages
            called["base_url"] = base_url
            return ("SYSTEM", [{"role": "user", "content": "x"}])

        monkeypatch.setattr(
            "agent.anthropic_adapter.convert_messages_to_anthropic", fake_convert
        )
        msgs = [{"role": "user", "content": "x"}]
        result = transport.convert_messages(msgs, base_url="https://example")
        assert result == ("SYSTEM", [{"role": "user", "content": "x"}])
        assert called["messages"] is msgs
        assert called["base_url"] == "https://example"

    def test_convert_tools_delegates(self, transport, monkeypatch):
        called = {}

        def fake_convert(tools):
            called["tools"] = tools
            return [{"name": "echo", "input_schema": {}}]

        monkeypatch.setattr(
            "agent.anthropic_adapter.convert_tools_to_anthropic", fake_convert
        )
        tools_in = [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {}, "description": "d"},
            }
        ]
        result = transport.convert_tools(tools_in)
        assert called["tools"] is tools_in
        assert result == [{"name": "echo", "input_schema": {}}]


# ---------------------------------------------------------------------------
# build_kwargs
# ---------------------------------------------------------------------------


class TestBuildKwargs:
    def test_pins_single_turn_and_empty_allowed_tools(self, transport, fake_sdk):
        msgs = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]
        kw = transport.build_kwargs(model="claude-sonnet-4-5", messages=msgs)
        opts = kw["options"]
        assert isinstance(opts, _FakeOptions)
        assert opts.max_turns == 1
        assert opts.allowed_tools == []
        assert opts.model == "claude-sonnet-4-5"

    def test_prompt_is_last_user_message(self, transport, fake_sdk):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "intermediate"},
            {"role": "user", "content": "second"},
        ]
        kw = transport.build_kwargs(model="m", messages=msgs)
        assert kw["prompt"] == "second"

    def test_prompt_extracts_text_from_content_blocks(self, transport, fake_sdk):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                    {"type": "tool_result", "tool_use_id": "x", "content": "ignored"},
                ],
            }
        ]
        kw = transport.build_kwargs(model="m", messages=msgs)
        assert kw["prompt"] == "hello \nworld"

    def test_dispatch_sentinel_present(self, transport, fake_sdk):
        msgs = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(model="m", messages=msgs)
        assert kw["__claude_agent_sdk__"] is True
        assert "__anthropic_messages__" in kw
        assert "__anthropic_tools__" in kw

    def test_tools_passed_in_anthropic_format(self, transport, fake_sdk):
        msgs = [{"role": "user", "content": "Hi"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo input",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        kw = transport.build_kwargs(model="m", messages=msgs, tools=tools)
        # Anthropic-format conversion preserves the function name.
        assert any(t.get("name") == "echo" for t in kw["__anthropic_tools__"])

    def test_max_turns_override(self, transport, fake_sdk):
        msgs = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(model="m", messages=msgs, max_turns=3)
        assert kw["options"].max_turns == 3

    def test_cwd_passthrough(self, transport, fake_sdk):
        msgs = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(model="m", messages=msgs, cwd="/tmp/wd")
        assert kw["options"].cwd == "/tmp/wd"

    def test_system_prompt_flattens_block_list(self, transport, fake_sdk, monkeypatch):
        """When prompt caching wraps system in blocks, SDK gets flat string."""

        def fake_convert(messages, base_url=None):
            return (
                [{"type": "text", "text": "Block A"}, {"type": "text", "text": "Block B"}],
                [{"role": "user", "content": "Hi"}],
            )

        monkeypatch.setattr(
            "agent.anthropic_adapter.convert_messages_to_anthropic", fake_convert
        )
        kw = transport.build_kwargs(
            model="m", messages=[{"role": "user", "content": "Hi"}]
        )
        assert kw["options"].system_prompt == "Block A\nBlock B"


# ---------------------------------------------------------------------------
# normalize_response
# ---------------------------------------------------------------------------


class TestNormalizeResponse:
    def test_collects_text_blocks(self, transport):
        messages = [
            AssistantMessage(content=[TextBlock("Hello "), TextBlock("world")]),
            ResultMessage(stop_reason="end_turn"),
        ]
        nr = transport.normalize_response(messages)
        assert isinstance(nr, NormalizedResponse)
        assert nr.content == "Hello \nworld"
        assert nr.tool_calls is None
        assert nr.finish_reason == "stop"

    def test_collects_tool_use_blocks(self, transport):
        messages = [
            AssistantMessage(
                content=[
                    TextBlock("Calling tool"),
                    ToolUseBlock("toolu_1", "search", {"query": "python"}),
                ]
            ),
            ResultMessage(stop_reason="tool_use"),
        ]
        nr = transport.normalize_response(messages)
        assert nr.content == "Calling tool"
        assert nr.tool_calls and len(nr.tool_calls) == 1
        tc = nr.tool_calls[0]
        assert tc.id == "toolu_1"
        assert tc.name == "search"
        assert json.loads(tc.arguments) == {"query": "python"}
        assert nr.finish_reason == "tool_calls"

    def test_collects_thinking(self, transport):
        messages = [
            AssistantMessage(
                content=[ThinkingBlock("Let me think..."), TextBlock("Answer.")]
            ),
            ResultMessage(stop_reason="end_turn"),
        ]
        nr = transport.normalize_response(messages)
        assert nr.reasoning == "Let me think..."
        assert nr.content == "Answer."

    def test_strip_mcp_prefix(self, transport):
        messages = [
            AssistantMessage(
                content=[ToolUseBlock("id1", "mcp_search", {})]
            ),
            ResultMessage(stop_reason="tool_use"),
        ]
        nr = transport.normalize_response(messages, strip_tool_prefix=True)
        assert nr.tool_calls[0].name == "search"

    def test_total_cost_in_provider_data(self, transport):
        messages = [
            AssistantMessage(content=[TextBlock("hi")]),
            ResultMessage(stop_reason="end_turn", total_cost_usd=0.0123),
        ]
        nr = transport.normalize_response(messages)
        assert nr.provider_data == {"total_cost_usd": 0.0123}

    def test_drains_async_iterator(self, transport):
        prepared = [
            AssistantMessage(content=[TextBlock("from-async")]),
            ResultMessage(stop_reason="end_turn"),
        ]

        async def gen():
            for m in prepared:
                yield m

        nr = transport.normalize_response(gen())
        assert nr.content == "from-async"
        assert nr.finish_reason == "stop"

    def test_finish_reason_inferred_when_no_result_message(self, transport):
        # No ResultMessage — tool_use present => tool_calls.
        messages = [
            AssistantMessage(content=[ToolUseBlock("id1", "search", {})]),
        ]
        nr = transport.normalize_response(messages)
        assert nr.finish_reason == "tool_calls"

    def test_finish_reason_default_to_stop(self, transport):
        messages = [AssistantMessage(content=[TextBlock("hello")])]
        nr = transport.normalize_response(messages)
        assert nr.finish_reason == "stop"

    def test_map_finish_reason(self, transport):
        assert transport.map_finish_reason("end_turn") == "stop"
        assert transport.map_finish_reason("tool_use") == "tool_calls"
        assert transport.map_finish_reason("max_tokens") == "length"
        assert transport.map_finish_reason("unknown") == "stop"


# ---------------------------------------------------------------------------
# Missing SDK error path
# ---------------------------------------------------------------------------


class TestMissingSdk:
    def test_build_kwargs_raises_clear_install_hint(self, transport, monkeypatch):
        """When claude_agent_sdk is unimportable, surface a Hermes install hint."""

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "claude_agent_sdk":
                raise ImportError("No module named 'claude_agent_sdk'")
            return real_import(name, *args, **kwargs)

        # Ensure no cached fake_sdk lingers from another test.
        monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)
        monkeypatch.setattr("builtins.__import__", fake_import)

        with pytest.raises(ImportError) as excinfo:
            transport.build_kwargs(
                model="m",
                messages=[{"role": "user", "content": "Hi"}],
            )
        assert "hermes-agent[claude-agent-sdk]" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Module-level lazy import helper
# ---------------------------------------------------------------------------


class TestLazyImport:
    def test_import_sdk_returns_module(self, fake_sdk):
        sdk = cas_module._import_sdk()
        assert sdk is fake_sdk
        # Just verifying our fake is usable through the same path
        # that build_kwargs uses.
        assert sdk.ClaudeAgentOptions(model="x").model == "x"
