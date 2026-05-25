"""Claude Agent SDK provider — host CLI via meridian HTTP proxy.

Registers ``provider: claude-agent-sdk``. Inference is delegated to
the host's Claude Code CLI (which is signed in to a real Anthropic
subscription) via the Meridian HTTP proxy on
``http://127.0.0.1:3456``. The container reaches it because
``hermes-core`` runs in ``network_mode: host``.

Why a proxy and not the ``claude-agent-sdk`` Python package directly:
Anthropic refuses OAuth tokens originating from a Docker environment
(returns ``401 INVALID_USER_TOKEN`` for /v1/messages requests). The
upstream Python package spawns the CLI as a subprocess, which inherits
the container's environment, so it hits the same 401. Meridian
running on the host is unaffected — its outbound calls look like a
normal desktop Claude Code session.

Wire-protocol-wise this profile is identical to ``anthropic_custom``,
just pointed at a different base_url. Auth is a placeholder
(``api_key: not-needed``) because Meridian does not gate on the
caller's bearer.
"""

import logging

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


claude_agent_sdk = ProviderProfile(
    name="claude-agent-sdk",
    display_name="Claude Agent SDK",
    description="Host Claude Code CLI via Meridian HTTP proxy (127.0.0.1:3456)",
    aliases=("claude-sdk", "agent-sdk"),
    api_mode="anthropic_messages",
    env_vars=(),  # No env auth — Meridian doesn't validate
    base_url="http://127.0.0.1:3456",
    auth_type="api_key",
    default_aux_model="claude-haiku-4-5",
    fallback_models=(
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ),
)


register_provider(claude_agent_sdk)
