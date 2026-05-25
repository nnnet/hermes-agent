"""Claude Agent SDK provider profile.

Registers `provider: claude-agent-sdk` which delegates inference to the
local Claude Code CLI via the official `claude-agent-sdk` Python package.

Unlike the native Anthropic provider (HTTP API + x-api-key header) or
Meridian (HTTP proxy at :3456 that wraps CLI), this provider invokes
the CLI subprocess directly through the SDK. Credentials come from
the host's `~/.claude/.credentials.json` — no API key, no proxy.

The actual call path is handled by `agent/transports/claude_agent_sdk.py`
(transport for api_mode='claude_agent_sdk_single_turn'), which is added
in a follow-up commit. This file is metadata-only — registering the
profile in the provider registry.

See docs/claude-agent-sdk-integration.md for the full integration plan.
"""

import logging

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


claude_agent_sdk = ProviderProfile(
    name="claude-agent-sdk",
    display_name="Claude Agent SDK",
    description="Claude via local CLI subscription (auth from ~/.claude/.credentials.json)",
    aliases=("claude-sdk", "agent-sdk"),
    api_mode="claude_agent_sdk_single_turn",
    env_vars=(),  # No env auth — SDK reads ~/.claude/.credentials.json
    base_url="",  # No base_url — CLI handles routing
    auth_type="none",  # Marker: auth is CLI-internal, no key/header needed
    default_aux_model="claude-haiku-4-5",
    # Curated list — what the SDK can route through the host's Claude
    # Code CLI. Same models the subscription exposes. Picker needs at
    # least one entry to show the provider as selectable.
    fallback_models=(
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ),
)


register_provider(claude_agent_sdk)
