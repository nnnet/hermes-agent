"""Claude-via-Meridian provider — direct Anthropic Messages over host Meridian.

Wire protocol: Anthropic Messages (x-api-key header, anthropic-version,
native /v1/messages shape) — byte-identical to native ``anthropic``,
just pointed at ``http://127.0.0.1:3456`` (host Meridian) instead of
api.anthropic.com.

Why this exists alongside ``claude-agent-sdk``:

* ``claude-agent-sdk`` carries CLI-bridge semantics (the runtime layer
  recognises that name and wraps the request in agent-CLI transport
  conventions, e.g. ``base_url`` rewriting to ``claude-agent-sdk://``).
* This profile is a plain HTTP-to-Meridian path with no special-casing
  in ``runtime_provider`` — useful when the caller wants the standard
  Anthropic SDK to hit Meridian directly without any agent-side rewriting.

Until this plugin existed, ``provider: claude-via-meridian`` in config
resolved to a generic-custom path in ``runtime_provider`` that fell back
to ``chat_completions`` (OpenAI shape) and hit ``/chat/completions`` on
Meridian, which returns 404 because Meridian only speaks Anthropic
Messages. Registering an explicit provider here makes ``api_mode:
anthropic_messages`` honoured by name-match in
``_provider_supports_explicit_api_mode`` and routes traffic to
``/v1/messages``.

Auth: Meridian does not validate the bearer — any ``api_key`` value
(commonly ``not-needed``) passes through.
"""

import logging

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


claude_via_meridian = ProviderProfile(
    name="claude-via-meridian",
    aliases=("claude-meridian", "meridian"),
    display_name="Claude-Meridian",
    description=(
        "Anthropic Messages protocol via host Meridian HTTP proxy "
        "(127.0.0.1:3456). Plain Anthropic wire-protocol — no agent-CLI "
        "bridging. Auth: bearer ignored by Meridian."
    ),
    api_mode="anthropic_messages",
    # Meridian does not validate the bearer, but ``auth.py`` only
    # synchronises plugins into ``PROVIDER_REGISTRY`` when either
    # ``auth_type == "none"`` OR ``auth_type == "api_key"`` with a
    # non-empty ``env_vars`` tuple. Without registry presence,
    # ``runtime_provider`` can't reach the api-mode honor branch and
    # falls through to default ``chat_completions``. We expose a
    # placeholder env var (``CLAUDE_VIA_MERIDIAN_API_KEY``) — Meridian
    # ignores its value, but the registry sync sees a non-empty tuple
    # and registers the profile.
    env_vars=("CLAUDE_VIA_MERIDIAN_API_KEY",),
    base_url="http://127.0.0.1:3456",
    auth_type="api_key",
    default_aux_model="claude-haiku-4-5",
    fallback_models=(
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ),
)


register_provider(claude_via_meridian)
