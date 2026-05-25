"""CLR Gateway provider profile.

Third-party Anthropic-compatible proxy at https://llm.ketlu.com. Wire
protocol = Anthropic Messages (x-api-key header, anthropic-version,
native /v1/messages shape). Routes to all 3 latest Claude models
(haiku-4-5, sonnet-4-6, opus-4-7).

Previously this lived in config.yaml as `provider: anthropic` with
a base_url override + `${ANTHROPIC_BASE_URL}` env template. Two pain
points the override caused:

1. config.yaml's `_normalize_custom_provider_entry` validates base_url
   with urlparse BEFORE env vars are expanded, so `${ANTHROPIC_BASE_URL}`
   gets dropped (see fix branch on hermes-agent).
2. The override couples provider auth (CLR_GATEWAY_API_KEY) to a
   provider profile that defaults to ANTHROPIC_API_KEY — confusing.

Encoding the proxy as its own profile here makes the routing explicit:
just `provider: clr-gateway` in config.yaml, no URL juggling.

Privacy: full conversation content flows to llm.ketlu.com — same
posture as opengateway.gitlawb.com. Don't enable for strict on-prem
workloads. Verified 2026-05-22 with free unlimited key.

Known issue (2026-05-25): native Anthropic SDK streaming returns 401
INVALID_USER_TOKEN against this endpoint while plain `curl` SSE works.
Likely an Authorization-header conflict the SDK adds. Use this provider
for non-streaming first; track the streaming fix separately.
"""

import logging

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


clr_gateway = ProviderProfile(
    name="clr-gateway",
    aliases=("ketlu", "clr"),
    display_name="CLR Gateway",
    description="Third-party Anthropic-compatible proxy at llm.ketlu.com",
    api_mode="anthropic_messages",
    env_vars=("CLR_GATEWAY_API_KEY",),
    base_url="https://llm.ketlu.com",
    auth_type="api_key",
    default_aux_model="claude-haiku-4-5",
    # Curated list — shown in /model picker. The gateway's /v1/models
    # endpoint may not be implemented or may return more than agentic-capable
    # models, so we keep the safe set explicit. Verified working 2026-05-22.
    fallback_models=(
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ),
)


register_provider(clr_gateway)
