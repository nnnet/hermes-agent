"""Claude Agent SDK transport (single-turn delegation).

Wraps the official ``claude-agent-sdk`` Python package
(``pip install claude-agent-sdk``) as a Hermes transport.  Selected by
``provider: claude-agent-sdk`` (see
``plugins/model-providers/claude-agent-sdk/__init__.py``), which sets
``api_mode='claude_agent_sdk_single_turn'``.

Strategy: single-turn delegation
--------------------------------
The SDK's ``query()`` is an encapsulated agent loop — it spawns the
Claude Code CLI as a subprocess, runs its own multi-turn tool-use loop
internally, and yields ``Message`` objects.  This collides with Hermes'
own ``AIAgent.run()`` loop, which already owns tool execution, MCP
server lifecycle, skill injection, compression, and Hindsight memory.

To keep Hermes in control we configure the SDK with ``max_turns=1`` and
``allowed_tools=[]``: one Claude call, no SDK-side tool execution.  Tool
calls in the response are surfaced as ``ToolCall`` objects on
``NormalizedResponse`` exactly as the Anthropic Messages transport
does, then Hermes' loop executes them and calls ``query()`` again with
updated history on the next iteration.

This trades idiomatic SDK use for a drop-in replacement of the
Anthropic Messages transport's call path, so the rest of the codebase
(skills, MCP, compression, cost tracking) needs zero changes.

Why the SDK is imported lazily
------------------------------
``claude-agent-sdk`` is an optional extra
(``pip install hermes-agent[claude-agent-sdk]``).  Importing it at
module top-level would crash Hermes for every user who hasn't opted
in.  All SDK imports therefore happen inside methods, with a clear
``ImportError`` message pointing at the install command.

Async-iter handling
-------------------
``query()`` returns an ``AsyncIterator[Message]``.  Hermes' agent loop
is synchronous, so ``normalize_response()`` accepts either a pre-drained
list of messages (preferred — caller drains asynchronously) or an async
iterator (drained internally via a small ``asyncio.run`` helper).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Dict, Iterable, List, Optional

from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, ToolCall


_SDK_INSTALL_HINT = (
    "claude-agent-sdk is not installed.  Install with:\n"
    "    pip install 'hermes-agent[claude-agent-sdk]'\n"
    "or directly:\n"
    "    pip install claude-agent-sdk"
)


def _import_sdk():
    """Lazily import ``claude_agent_sdk``.

    Raises ``ImportError`` with a Hermes-flavoured install hint when the
    package is missing rather than the bare ``ModuleNotFoundError``.
    """
    try:
        import claude_agent_sdk  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised via test monkeypatch
        raise ImportError(_SDK_INSTALL_HINT) from exc
    return claude_agent_sdk


# Anthropic stop_reason -> OpenAI finish_reason.  Mirrors AnthropicTransport
# so downstream consumers see the same vocabulary regardless of which
# transport produced the response.
_STOP_REASON_MAP: Dict[str, str] = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "model_context_window_exceeded": "length",
}


def _extract_last_user_text(messages: List[Dict[str, Any]]) -> str:
    """Return the last user message's text content as a plain string.

    Tool result messages and assistant turns are skipped — the SDK's
    ``prompt`` parameter expects the *new* user turn to act on.  Prior
    conversation context is reconstructed by Hermes' agent loop via the
    full message history on subsequent ``query()`` calls.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Anthropic-style content blocks: pick text parts, skip tool_result.
            parts: List[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            if parts:
                return "\n".join(parts)
        # Empty/unknown content — keep looking earlier in history.
    return ""


def _drain_async_iter(aiter: Any) -> List[Any]:
    """Drain an ``AsyncIterator`` to a list, running a fresh event loop.

    Hermes' agent loop is synchronous; the SDK's ``query()`` is async.
    We isolate the bridge in this helper so callers can pass either a
    pre-drained list (preferred — they ran their own event loop) or the
    raw async iterator (we run a one-shot loop here).
    """

    async def _collect() -> List[Any]:
        out: List[Any] = []
        async for item in aiter:
            out.append(item)
        return out

    return asyncio.run(_collect())


class ClaudeAgentSdkTransport(ProviderTransport):
    """Transport for ``api_mode='claude_agent_sdk_single_turn'``.

    Reuses the Anthropic adapter's message and tool converters because
    the SDK speaks the same content-block vocabulary as the Anthropic
    Messages API — only the call surface (``query()`` async-iter)
    differs.
    """

    @property
    def api_mode(self) -> str:
        return "claude_agent_sdk_single_turn"

    # ── conversion ────────────────────────────────────────────────────

    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """Convert OpenAI messages to the Anthropic ``(system, messages)`` tuple.

        Reused verbatim from :mod:`agent.anthropic_adapter` — the SDK
        speaks the same message vocabulary as the Anthropic Messages API.
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        base_url = kwargs.get("base_url")
        return convert_messages_to_anthropic(messages, base_url=base_url)

    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        """Convert OpenAI tool schemas to Anthropic ``input_schema`` format.

        Reused verbatim from :mod:`agent.anthropic_adapter`.  The SDK
        does **not** consume these directly — its ``allowed_tools`` /
        ``mcp_servers`` model is different — but Hermes' agent loop
        still uses Anthropic-format tools to track call IDs and surface
        results, so we preserve the same shape.
        """
        from agent.anthropic_adapter import convert_tools_to_anthropic

        return convert_tools_to_anthropic(tools)

    # ── kwargs assembly ───────────────────────────────────────────────

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """Build kwargs for a single ``claude_agent_sdk.query()`` call.

        Returns a dict with keys consumed by the AIAgent dispatch site:

        * ``prompt`` — last user message as a plain string.
        * ``options`` — a ``ClaudeAgentOptions`` instance pinned to
          ``max_turns=1`` and ``allowed_tools=[]`` so the SDK does not
          run its own tool loop.  Carries ``model`` and ``system_prompt``.
        * ``__claude_agent_sdk__`` — sentinel for dispatch.
        * ``__anthropic_tools__`` — Anthropic-format tools, kept on the
          side because the SDK uses a different tool definition shape.
          Hermes' loop reads these to attribute tool calls to skills.
        * ``__anthropic_messages__`` — converted message history, kept
          for the same reason (single-turn delegation re-sends the
          whole history each turn via this side channel).

        params (all optional):
            base_url: str | None — passed through for completeness.
            max_turns: int — overrides the default of 1.  Use with care:
                anything > 1 lets the SDK run its own tool loop and
                breaks Hermes' control flow.
            allowed_tools: list[str] — SDK built-in tools (Read/Write/
                Bash/...).  Default ``[]``; set only if you intentionally
                want SDK-side tool execution.
            cwd: str | None — working directory for SDK CLI subprocess.
        """
        sdk = _import_sdk()

        system, anthropic_messages = self.convert_messages(
            messages, base_url=params.get("base_url")
        )
        anthropic_tools = self.convert_tools(tools) if tools else []

        # ``system`` from convert_messages_to_anthropic can be a list of
        # content blocks (with cache_control markers) or a plain string.
        # The SDK's ``system_prompt`` is a plain string only.
        system_prompt = _coerce_system_to_str(system)

        prompt_text = _extract_last_user_text(messages)

        options = sdk.ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt or None,
            allowed_tools=list(params.get("allowed_tools", [])),
            max_turns=int(params.get("max_turns", 1)),
            cwd=params.get("cwd"),
        )

        return {
            "prompt": prompt_text,
            "options": options,
            "__claude_agent_sdk__": True,
            "__anthropic_tools__": anthropic_tools,
            "__anthropic_messages__": anthropic_messages,
        }

    # ── response normalization ────────────────────────────────────────

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize the SDK's ``Message`` stream to a ``NormalizedResponse``.

        Accepts either:

        * a list/iterable of already-collected SDK ``Message`` objects, or
        * an async iterator (drained internally with :func:`_drain_async_iter`).

        Collects every ``AssistantMessage`` block: ``TextBlock`` text into
        ``content``, ``ThinkingBlock`` into ``reasoning``, ``ToolUseBlock``
        into ``tool_calls``.  Stop reason is taken from the trailing
        ``ResultMessage`` when present.
        """
        strip_tool_prefix = bool(kwargs.get("strip_tool_prefix", False))
        _MCP_PREFIX = "mcp_"

        messages = self._drain_to_list(response)

        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        finish_reason_raw: Optional[str] = None
        usage = None
        provider_data: Dict[str, Any] = {}

        for msg in messages:
            blocks = getattr(msg, "content", None)
            if isinstance(blocks, list):
                for block in blocks:
                    btype = getattr(block, "type", None) or _classname_to_type(block)
                    if btype == "text":
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
                    elif btype == "thinking":
                        thinking = getattr(block, "thinking", None) or getattr(
                            block, "text", None
                        )
                        if thinking:
                            reasoning_parts.append(thinking)
                    elif btype == "tool_use":
                        name = getattr(block, "name", "")
                        if strip_tool_prefix and name.startswith(_MCP_PREFIX):
                            name = name[len(_MCP_PREFIX):]
                        raw_input = getattr(block, "input", {}) or {}
                        try:
                            args = json.dumps(raw_input)
                        except (TypeError, ValueError):
                            args = json.dumps({})
                        tool_calls.append(
                            ToolCall(
                                id=getattr(block, "id", None),
                                name=name,
                                arguments=args,
                            )
                        )
            # ``ResultMessage`` carries terminal metadata.
            if _classname_to_type(msg) == "result":
                finish_reason_raw = getattr(msg, "stop_reason", None) or finish_reason_raw
                cost = getattr(msg, "total_cost_usd", None)
                if cost is not None:
                    provider_data["total_cost_usd"] = cost

        if finish_reason_raw is None:
            finish_reason_raw = "tool_use" if tool_calls else "end_turn"
        finish_reason = _STOP_REASON_MAP.get(finish_reason_raw, "stop")

        return NormalizedResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            reasoning="\n\n".join(reasoning_parts) if reasoning_parts else None,
            usage=usage,
            provider_data=provider_data or None,
        )

    def map_finish_reason(self, raw_reason: str) -> str:
        """Map Anthropic-style stop_reason to OpenAI finish_reason."""
        return _STOP_REASON_MAP.get(raw_reason, "stop")

    # ── internals ─────────────────────────────────────────────────────

    @staticmethod
    def _drain_to_list(response: Any) -> List[Any]:
        """Coerce response to a concrete list of SDK ``Message`` objects.

        Accepts:
          * ``list`` / tuple — used directly (test-friendly).
          * async iterator — drained via :func:`_drain_async_iter`.
          * any other iterable — eagerly listed.
        """
        if isinstance(response, list):
            return response
        if isinstance(response, tuple):
            return list(response)
        if inspect.isasyncgen(response) or hasattr(response, "__aiter__"):
            return _drain_async_iter(response)
        if isinstance(response, Iterable):
            return list(response)
        # Last resort: wrap a single message.
        return [response]


def _coerce_system_to_str(system: Any) -> str:
    """Flatten the Anthropic ``system`` parameter to a plain string.

    ``convert_messages_to_anthropic`` can return ``system`` as a list of
    content blocks (``[{"type": "text", "text": "...", "cache_control": ...}, ...]``)
    when prompt caching is enabled.  The SDK's ``system_prompt`` is a
    flat string only — cache markers are dropped here, matching the
    SDK's expectations.
    """
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: List[str] = []
        for item in system:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(system)


def _classname_to_type(obj: Any) -> str:
    """Infer SDK block/message type from class name.

    Falls back to lowercased classname so we don't depend on the SDK
    exporting a ``type`` field — older SDK versions don't.
    """
    name = type(obj).__name__
    mapping = {
        "TextBlock": "text",
        "ThinkingBlock": "thinking",
        "ToolUseBlock": "tool_use",
        "ToolResultBlock": "tool_result",
        "AssistantMessage": "assistant",
        "UserMessage": "user",
        "SystemMessage": "system",
        "ResultMessage": "result",
    }
    return mapping.get(name, name.lower())


# Auto-register on import.  Mirrors agent/transports/anthropic.py.
from agent.transports import register_transport  # noqa: E402

register_transport("claude_agent_sdk_single_turn", ClaudeAgentSdkTransport)
