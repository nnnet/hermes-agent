"""Anthropic-compatible custom-endpoint provider profile.

Subclass of the native Anthropic provider that targets a configurable
base_url instead of api.anthropic.com. Wire protocol stays Anthropic
Messages (x-api-key header, anthropic-version, native /v1/messages
shape), so the SDK transport, streaming, prompt caching and tool-use
encoding are byte-identical to the upstream Anthropic provider.

Initial concrete deployment: CLR Gateway at https://llm.ketlu.com,
keyed by CLR_GATEWAY_API_KEY. Adding another Anthropic-compatible
proxy is a config-only change — set
``providers.anthropic_custom.base_url`` and ``api_key`` in
``~/.hermes/config.yaml`` and Hermes will use the same code path.

Replaces the previous ``clr-gateway`` plugin. The change is motivated
by the fact that any custom Anthropic-compatible proxy is structurally
identical to native Anthropic for the SDK — they only differ in
``base_url`` and credential. Encoding "anthropic-protocol + custom
endpoint" as a separate provider profile keeps the SDK setup path
honest: native Anthropic auth tokens (OAuth) never leak into
proxy-bound clients, and the proxy never has to pretend to be the
native provider.
"""

import json
import logging
import urllib.request

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


class AnthropicCustomProfile(ProviderProfile):
    """Anthropic protocol against a non-anthropic.com base_url."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Anthropic-protocol /v1/models with x-api-key header.

        Some proxies (CLR Gateway included) do not implement /v1/models
        or return an unfiltered superset. Caller falls back to
        ``fallback_models`` from the profile if this returns None.
        """
        if not api_key:
            return None
        base = (self.base_url or "https://api.anthropic.com").rstrip("/")
        try:
            req = urllib.request.Request(f"{base}/v1/models")
            req.add_header("x-api-key", api_key)
            req.add_header("anthropic-version", "2023-06-01")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            return [
                m["id"]
                for m in data.get("data", [])
                if isinstance(m, dict) and "id" in m
            ]
        except Exception as exc:
            logger.debug("fetch_models(anthropic_custom): %s", exc)
            return None


anthropic_custom = AnthropicCustomProfile(
    name="anthropic_custom",
    aliases=("anthropic-custom",),
    display_name="Anthropic-custom",
    description=(
        "Anthropic Messages protocol against a configurable base_url. "
        "Default target: CLR Gateway (llm.ketlu.com)."
    ),
    api_mode="anthropic_messages",
    env_vars=("CLR_GATEWAY_API_KEY", "ANTHROPIC_CUSTOM_API_KEY"),
    base_url="https://llm.ketlu.com",
    auth_type="api_key",
    default_aux_model="claude-haiku-4-5",
    fallback_models=(
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ),
)


register_provider(anthropic_custom)
