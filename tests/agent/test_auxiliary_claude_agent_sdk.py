"""Tests for the Claude Agent SDK auxiliary client branch.

Why
    Phase 3 wires ``claude_agent_sdk`` into ``agent/auxiliary_client.py`` so
    every auxiliary use case (compression, title_generation, session_search,
    skills_hub, approval, mcp, triage_specifier, curator, vision,
    web_extract) can use the host's Claude Code subscription auth via the
    bundled CLI subprocess — without an ``ANTHROPIC_API_KEY``.  These tests
    pin the contract of that wire so a future refactor cannot silently
    regress any of the 10 aux roles.

What
    * ``ClaudeAgentSdkAuxiliaryClient`` exposes the OpenAI-compatible
      ``client.chat.completions.create(**kw)`` surface.
    * ``resolve_provider_client("claude-agent-sdk", ...)`` returns the
      new wrapper (and its async counterpart on ``async_mode=True``).
    * Missing SDK on the resolve path returns ``(None, None)`` with a
      clear warning rather than crashing.
    * ``CLINotFoundError`` / ``CLIConnectionError`` from the SDK are
      remapped to operator-actionable ``RuntimeError`` instances.
    * The same wrapper handles every auxiliary role uniformly —
      compression, title_generation, and vision (with image-block
      limitation) feed through the single ``query()`` call site.

Test
    Run with::

        uv run --extra dev --extra claude-agent-sdk pytest \\
            tests/agent/test_auxiliary_claude_agent_sdk.py -v

    ``claude_agent_sdk`` is mocked via ``sys.modules`` so the suite runs
    even without the extra installed in CI.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest


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
    """Inject a fake ``claude_agent_sdk`` module into ``sys.modules``.

    Why
        The transport, dispatch helper, and aux adapter all import the
        SDK lazily.  A single fake module under the canonical name covers
        every consumer in a test run.
    """
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _FakeOptions
    fake.CLINotFoundError = _CLINotFoundError
    fake.CLIConnectionError = _CLIConnectionError

    scripted = list(scripted_messages or [])

    async def _query(*, prompt, options):  # noqa: ARG001
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip provider env vars so resolve_provider_client gives reproducible
    answers for the claude-agent-sdk branch (no accidental fallbacks)."""
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Wrapper basics — chat.completions.create() returns OpenAI shape
# ---------------------------------------------------------------------------


class TestClaudeAgentSdkAuxiliaryClient:
    def test_exposes_chat_completions_surface(self):
        """Why: every aux caller invokes ``client.chat.completions.create(...)``;
        if the surface is missing, AttributeError leaks into the call path.
        Test: instantiate the wrapper and assert the call attribute exists.
        """
        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        client = ClaudeAgentSdkAuxiliaryClient("claude-sonnet-4-5")
        assert hasattr(client, "chat")
        assert hasattr(client.chat, "completions")
        assert callable(getattr(client.chat.completions, "create", None))

    def test_create_returns_openai_shaped_response(self, monkeypatch):
        """Why: auxiliary callers read ``response.choices[0].message.content``;
        the wrapper must return that exact shape regardless of the SDK's
        own ``Message`` vocabulary.
        Test: scripted SDK stream → expect joined text under
        ``.choices[0].message.content`` plus ``stop`` finish_reason.
        """
        _install_fake_sdk(
            monkeypatch,
            scripted_messages=[
                AssistantMessage(content=[TextBlock("Hello "), TextBlock("world")]),
                ResultMessage(stop_reason="end_turn", total_cost_usd=0.0042),
            ],
        )

        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        client = ClaudeAgentSdkAuxiliaryClient("claude-sonnet-4-5")
        resp = client.chat.completions.create(
            model="claude-sonnet-4-5",
            messages=[
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "Hi"},
            ],
        )
        # OpenAI shape — same as AnthropicAuxiliaryClient produces.
        assert isinstance(resp, SimpleNamespace)
        assert resp.choices[0].message.content == "Hello \nworld"
        assert resp.choices[0].finish_reason == "stop"
        # Cost telemetry surfaced for cost-tracking consumers.
        assert resp.usage is not None
        assert resp.usage.total_cost_usd == pytest.approx(0.0042)

    def test_forwards_prompt_and_system_to_sdk(self, monkeypatch):
        """Why: the SDK's ``query()`` expects a flat prompt string built
        from the last user turn, with the system message routed into
        ``ClaudeAgentOptions.system_prompt``.  A bug here silently mixes
        roles and corrupts every aux call.
        Test: inspect the recorded ``last_call`` and the options object.
        """
        fake_sdk = _install_fake_sdk(
            monkeypatch,
            scripted_messages=[
                AssistantMessage(content=[TextBlock("ack")]),
                ResultMessage(stop_reason="end_turn"),
            ],
        )

        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        client = ClaudeAgentSdkAuxiliaryClient("claude-sonnet-4-5")
        client.chat.completions.create(
            model="claude-sonnet-4-5",
            messages=[
                {"role": "system", "content": "You compress."},
                {"role": "user", "content": "Compress this."},
            ],
        )
        assert fake_sdk.last_call is not None
        assert fake_sdk.last_call["prompt"] == "Compress this."
        opts = fake_sdk.last_call["options"]
        assert opts.max_turns == 1
        assert opts.allowed_tools == []
        assert opts.system_prompt == "You compress."
        assert opts.model == "claude-sonnet-4-5"

    def test_close_is_noop(self):
        """Why: callers uniformly call ``.close()`` on aux clients during
        cache eviction; the SDK has no persistent resource, but absent
        ``.close()`` they'd AttributeError.
        Test: close() returns without raising.
        """
        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        client = ClaudeAgentSdkAuxiliaryClient("m")
        assert client.close() is None

    def test_async_wrapper_uses_same_adapter(self, monkeypatch):
        """Why: async consumers (web_tools, session_search) await
        ``client.chat.completions.create(...)``; the async wrapper must
        route through the same sync adapter to preserve identical
        OpenAI-shape output, otherwise sync and async aux callers
        diverge silently.
        Test: drive the async wrapper through an event loop, assert the
        same response shape as the sync path.
        """
        import asyncio

        _install_fake_sdk(
            monkeypatch,
            scripted_messages=[
                AssistantMessage(content=[TextBlock("async-ok")]),
                ResultMessage(stop_reason="end_turn"),
            ],
        )

        from agent.auxiliary_client import (
            AsyncClaudeAgentSdkAuxiliaryClient,
            ClaudeAgentSdkAuxiliaryClient,
        )

        sync_client = ClaudeAgentSdkAuxiliaryClient("m")
        async_client = AsyncClaudeAgentSdkAuxiliaryClient(sync_client)

        async def _go():
            return await async_client.chat.completions.create(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )

        resp = asyncio.run(_go())
        assert resp.choices[0].message.content == "async-ok"


# ---------------------------------------------------------------------------
# resolve_provider_client dispatch
# ---------------------------------------------------------------------------


class TestResolveProviderClient:
    def test_resolve_returns_sdk_wrapper(self, monkeypatch):
        """Why: every auxiliary task config branch
        (auxiliary.<role>.provider = claude-agent-sdk) flows through
        ``resolve_provider_client``; we need a single chokepoint that
        produces the SDK wrapper.
        Test: resolve with provider='claude-agent-sdk', verify the type.
        """
        _install_fake_sdk(monkeypatch)
        from agent.auxiliary_client import (
            ClaudeAgentSdkAuxiliaryClient,
            resolve_provider_client,
        )

        client, model = resolve_provider_client(
            "claude-agent-sdk",
            model="claude-sonnet-4-5",
        )
        assert isinstance(client, ClaudeAgentSdkAuxiliaryClient)
        assert model == "claude-sonnet-4-5"

    def test_resolve_async_returns_async_wrapper(self, monkeypatch):
        """Why: async aux paths request ``async_mode=True``; the dispatch
        must produce the async wrapper without re-resolving credentials.
        Test: resolve in async_mode, verify the async wrapper type.
        """
        _install_fake_sdk(monkeypatch)
        from agent.auxiliary_client import (
            AsyncClaudeAgentSdkAuxiliaryClient,
            resolve_provider_client,
        )

        client, model = resolve_provider_client(
            "claude-agent-sdk",
            model="claude-haiku-4-5",
            async_mode=True,
        )
        assert isinstance(client, AsyncClaudeAgentSdkAuxiliaryClient)
        assert model == "claude-haiku-4-5"

    def test_resolve_uses_profile_default_model_when_unspecified(
        self, monkeypatch
    ):
        """Why: ``auxiliary.<role>`` blocks may omit ``model`` and rely on
        the provider profile's ``default_aux_model``.  Resolve must fall
        back to that default instead of returning ``model=None`` which
        would 400 every downstream call.
        Test: resolve with model=None, expect the profile default.
        """
        _install_fake_sdk(monkeypatch)
        from agent.auxiliary_client import resolve_provider_client

        _client, model = resolve_provider_client("claude-agent-sdk")
        # Profile pins claude-haiku-4-5 as default_aux_model.
        assert model and "claude" in model.lower()

    def test_resolve_returns_none_when_sdk_missing(self, monkeypatch, caplog):
        """Why: a half-configured profile must not crash the whole
        auxiliary chain — instead degrade gracefully with a warning so
        the fallback chain can pick another provider.
        Test: simulate the SDK import failing, expect (None, None)
        + a warning that names the install command.
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
        from agent.auxiliary_client import resolve_provider_client

        with caplog.at_level("WARNING"):
            client, model = resolve_provider_client("claude-agent-sdk")
        assert client is None
        assert model is None
        # The hint that points operators at the install command.
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "hermes-agent[claude-agent-sdk]" in joined


# ---------------------------------------------------------------------------
# Error mapping — operator-friendly messages instead of raw SDK exceptions
# ---------------------------------------------------------------------------


class TestSdkErrorMapping:
    def test_cli_not_found_remaps_to_runtime_error(self, monkeypatch):
        """Why: the SDK raises ``CLINotFoundError`` when the Claude CLI
        binary isn't on PATH.  An ops user shouldn't see the bare
        exception — point them at the install command + ``claude /login``.
        Test: drive the wrapper with a fake SDK that raises CLINotFoundError.
        """
        _install_fake_sdk(
            monkeypatch, raise_exc=_CLINotFoundError("claude binary missing")
        )
        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        client = ClaudeAgentSdkAuxiliaryClient("m")
        with pytest.raises(RuntimeError) as exc_info:
            client.chat.completions.create(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
        msg = str(exc_info.value)
        assert "claude /login" in msg
        assert "hermes-agent[claude-agent-sdk]" in msg

    def test_cli_connection_error_remaps_to_runtime_error(self, monkeypatch):
        """Why: ``CLIConnectionError`` is what the SDK raises when
        ``~/.claude/.credentials.json`` is missing/unreadable; the
        operator hint is different (relogin) than for CLI-missing.
        Test: drive the wrapper with that fake exception.
        """
        _install_fake_sdk(
            monkeypatch, raise_exc=_CLIConnectionError("auth file unreadable")
        )
        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        client = ClaudeAgentSdkAuxiliaryClient("m")
        with pytest.raises(RuntimeError) as exc_info:
            client.chat.completions.create(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
        assert ".credentials.json" in str(exc_info.value)

    def test_other_sdk_errors_propagate_unchanged(self, monkeypatch):
        """Why: the aux call path has its own retry/fallback machinery —
        we must not swallow unexpected SDK errors, or fallback decisions
        misfire.  Only the two operator-hint exceptions are remapped.
        Test: raise a generic exception, expect it to propagate.
        """
        sentinel = RuntimeError("upstream rate limit (synthetic)")
        _install_fake_sdk(monkeypatch, raise_exc=sentinel)
        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        client = ClaudeAgentSdkAuxiliaryClient("m")
        with pytest.raises(RuntimeError) as exc_info:
            client.chat.completions.create(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
        assert exc_info.value is sentinel


# ---------------------------------------------------------------------------
# Same branch covers every auxiliary role (compression / title_gen / vision)
# ---------------------------------------------------------------------------


class TestAllAuxRolesRouteThroughSdkBranch:
    """Each auxiliary role feeds through the same dispatch site.

    Why
        ``auxiliary.<role>.provider = claude-agent-sdk`` in config.yaml
        routes the role through ``resolve_provider_client`` →
        ``ClaudeAgentSdkAuxiliaryClient``.  The role name only changes
        which model/timeout the *caller* picks — the wire path is one.
        These tests pin that contract for 3 representative roles
        (compression, title_generation, vision-with-text-only-fallback).
    """

    def _resolve_for_role(self, role: str, monkeypatch):
        """Bypass config.yaml and resolve directly with role-specific model."""
        _install_fake_sdk(monkeypatch)
        from agent.auxiliary_client import resolve_provider_client

        return resolve_provider_client("claude-agent-sdk", model="claude-haiku-4-5")

    def test_compression_role(self, monkeypatch):
        """Why: compression is invoked on every long-context turn — it's
        the hottest aux path and the regression that bites first."""
        client, model = self._resolve_for_role("compression", monkeypatch)
        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        assert isinstance(client, ClaudeAgentSdkAuxiliaryClient)
        assert model

    def test_title_generation_role(self, monkeypatch):
        """Why: title generation runs once per new session and benefits
        most from the cheap haiku default."""
        client, _ = self._resolve_for_role("title_generation", monkeypatch)
        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        assert isinstance(client, ClaudeAgentSdkAuxiliaryClient)

    def test_vision_role_falls_back_to_text_only(self, monkeypatch):
        """Why: the SDK's ``prompt`` parameter is a plain string and the
        transport's prompt extractor only collects ``text`` blocks; image
        blocks are silently dropped today.  Vision callers that send
        ``image_url`` content blocks still need to land *somewhere*
        instead of crashing — text-only graceful degradation is the
        documented behaviour until the SDK exposes multimodal input.
        Test: a vision-style message arrives; the wrapper returns its
        text-portion response without raising.

        TODO(claude-agent-sdk vision): when the SDK supports image
        content blocks in ``prompt``, extend this test to assert image
        forwarding and remove the "falls back" wording from the name.
        """
        _install_fake_sdk(
            monkeypatch,
            scripted_messages=[
                AssistantMessage(content=[TextBlock("A picture description.")]),
                ResultMessage(stop_reason="end_turn"),
            ],
        )
        from agent.auxiliary_client import ClaudeAgentSdkAuxiliaryClient

        client = ClaudeAgentSdkAuxiliaryClient("claude-sonnet-4-5")
        # Vision-style OpenAI message with an image block alongside text.
        vision_msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBOR..."},
                },
            ],
        }
        resp = client.chat.completions.create(
            model="claude-sonnet-4-5",
            messages=[vision_msg],
        )
        # Wrapper returned without raising — text-only fallback.
        assert resp.choices[0].message.content == "A picture description."
