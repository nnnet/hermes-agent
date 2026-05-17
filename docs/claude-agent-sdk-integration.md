# Integration plan: claude-agent-sdk as Hermes provider

**Branch:** `feat/claude-agent-sdk-provider`
**Goal:** Add `provider: claude-agent-sdk` option to Hermes so worker processes can use
host's Claude Code subscription auth via the official `claude-agent-sdk` Python
package — replacing the Meridian HTTP-proxy chain.

## Why

Meridian (`http://127.0.0.1:3456`, host-side `claude-code-router`) is an
HTTP-proxy that translates Anthropic-SDK requests to local Claude CLI calls.
It works but is fragile (quota errors, host-only, third-party). The
[`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python) is
Anthropic's official Python library that wraps the local Claude CLI in
agent-loop semantics (tool use, streaming, multi-turn) using host subscription
auth from `~/.claude/.credentials.json` — no API key, no HTTP proxy.

## Architecture findings

### Hermes provider stack (current)

1. `plugins/model-providers/<name>/` — registers `ProviderProfile` metadata
   (name, aliases, api_mode, base_url, auth_type). Plugin is **metadata-only**,
   no API logic.
2. `agent/transports/<api_mode>.py` — implements `ProviderTransport` ABC:
   `convert_messages`, `convert_tools`, `build_kwargs`, `normalize_response`.
   Transport owns format conversion, **not** client lifecycle.
3. `agent/anthropic_adapter.py` — actual HTTP call code path for
   `api_mode='anthropic_messages'` (used by both native Anthropic and
   Meridian proxy at `127.0.0.1:3456`).
4. `agent/AIAgent.run()` — the **agent loop**: call API → parse → execute tool
   calls → loop. Each iteration is a single API call.

### claude-agent-sdk model

```python
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ToolUseBlock

async for message in query(
    prompt="Do X",
    options=ClaudeAgentOptions(
        model="claude-sonnet-4-5",
        system_prompt="…",
        max_turns=10,
        allowed_tools=["Read", "Write", "Bash"],
        cwd="/path",
    ),
): ...
```

`query()` is an **encapsulated agent loop** — it spawns Claude CLI as
subprocess, the CLI runs its own multi-turn tool-use loop internally, and
yields messages back. **It is NOT a single API call.**

### Integration strategies (trade-offs)

#### Strategy A — single-turn delegation (recommended)
Set `max_turns=1` and `allowed_tools=[]`. SDK does one Claude call, returns
result. Hermes' existing `AIAgent.run()` loop executes any tool_use, then
calls `query()` again with updated message history. Hermes keeps full
control: skills, MCP servers, compression, Hindsight, all unchanged.

Pros: Drop-in replacement for the current Anthropic adapter call path.
Cons: SDK was designed for full delegation — single-turn use is non-idiomatic
and may have rough edges (session state handling across calls).

#### Strategy B — full delegation
Hermes spawns `query()` with full system prompt, all skills as `allowed_tools`,
and lets SDK run the whole agent loop. Hermes becomes a thin wrapper around
SDK sessions.

Pros: Idiomatic SDK usage.
Cons: Major refactor — Hermes loses control over tool execution, MCP server
lifecycle, compression triggers, skill injection mid-loop. Not viable
without rewriting `AIAgent` core.

**Decision: Strategy A.**

## Concrete implementation plan

### Phase 1 — plumbing (small, mergeable)
1. Add `"claude-agent-sdk"` as optional extra in `pyproject.toml`:
   ```toml
   [project.optional-dependencies]
   claude-agent-sdk = ["claude-agent-sdk==<version>"]
   ```
   Use the latest pinned version per project rule. Regenerate `uv.lock`.

2. Add lazy-install hook in `tools/lazy_deps.py` keyed on
   `provider == "claude-agent-sdk"`.

3. Create `plugins/model-providers/claude-agent-sdk/plugin.yaml`:
   ```yaml
   name: claude-agent-sdk-provider
   kind: model-provider
   version: 1.0.0
   description: Claude via official Anthropic Agent SDK (host subscription auth)
   author: nnnet/AiManager
   ```

4. Create `plugins/model-providers/claude-agent-sdk/__init__.py`:
   ```python
   from providers import register_provider
   from providers.base import ProviderProfile

   claude_sdk = ProviderProfile(
       name="claude-agent-sdk",
       aliases=("claude-sdk", "agent-sdk"),
       api_mode="claude_agent_sdk_single_turn",
       env_vars=(),  # No env auth — uses ~/.claude/.credentials.json
       base_url="",  # No base_url — CLI handles
       auth_type="none",  # Marker: CLI-handled
       default_aux_model="claude-haiku-4-5",
   )
   register_provider(claude_sdk)
   ```

### Phase 2 — transport adapter
5. Create `agent/transports/claude_agent_sdk.py`:
   - Subclass `ProviderTransport`
   - `api_mode = "claude_agent_sdk_single_turn"`
   - `convert_messages` — same as anthropic_messages (system, messages tuple)
   - `convert_tools` — same as anthropic
   - `build_kwargs` — produce `ClaudeAgentOptions` instead of httpx-style dict
   - `normalize_response` — async-iter messages from `query()`, collect into
     same `NormalizedResponse` shape that AnthropicTransport returns

6. Wire `AIAgent.run()` to call `claude_agent_sdk.query()` when transport
   `api_mode == "claude_agent_sdk_single_turn"`. Need to handle async
   iteration: `query()` returns `AsyncIterator[Message]`, must be drained
   synchronously inside the loop (via `anyio.run` or asyncio.run on a small
   helper).

### Phase 3 — config validation + tests
7. Add config schema for `provider: claude-agent-sdk` (no `api_key` needed,
   `base_url` optional, validate Claude CLI exists at one of known paths).

8. Unit tests in `tests/providers/test_claude_agent_sdk.py`:
   - Format conversion round-trip
   - Mock `query()` to ensure single-turn invocation
   - Auth-not-needed path
   - Error handling when CLI missing

### Phase 4 — Docker image
9. Bake Claude CLI into hermes-agent Docker image (uncertain — package
   bundles CLI automatically when `pip install claude-agent-sdk` per docs,
   but verify in container).

10. Mount host `~/.claude/` into container — `/mnt/9/aimanager/infra/hermes/docker-compose.yml` already mounts `~/.claude → /opt/data/.claude`. Need to set `CLAUDE_CONFIG_DIR=/opt/data/.claude` so SDK finds credentials.

### Phase 5 — pilot & cutover (in `aimanager` repo, separate PR)
11. Pilot on ONE Hermes profile (e.g. `metadata-validator` — lowest stakes).
    Flip `provider: anthropic` → `provider: claude-agent-sdk`. Run for 24h.

12. Cut over `~/.hermes/config.yaml` if pilot passes.

13. Decommission Meridian — remove `scripts/meridian.sh` from `hermes restart`
    chain in `Makefile`, document in `MEMORY.md`.

## Critical files (this branch)

- `pyproject.toml` — add extra
- `plugins/model-providers/claude-agent-sdk/{plugin.yaml,__init__.py}` — new
- `agent/transports/claude_agent_sdk.py` — new
- `agent/transports/__init__.py` — register transport
- `tools/lazy_deps.py` — add lazy install
- `tests/providers/test_claude_agent_sdk.py` — new

## Companion changes (aimanager repo, branch `feat/claude-agent-sdk-replace-meridian`)

- `infra/hermes/docker-compose.yml` — add `CLAUDE_CONFIG_DIR` env var
- `~/.hermes/profiles/metadata-validator/config.yaml` — pilot flip
- `MEMORY.md` — when Meridian retired, mark `hermes_meridian_host_only.md`
  obsolete

## Open questions

- Does `claude-agent-sdk-python==<latest>` work with mounted credentials inside
  Docker? (Test before Phase 2.) macOS uses Keychain fallback; Linux uses
  `~/.claude/.credentials.json` — should work with bind mount, but unverified.
- Streaming: SDK returns async iterator. Hermes uses sync calls inside
  `AIAgent.run()`. May need a small sync wrapper helper.
- Cost tracking: SDK provides `ResultMessage.total_cost_usd` per `query()`.
  Need to integrate with Hermes' token-cost accounting.

## How to continue

Pick up at Phase 1 step 1 — `pyproject.toml` extra. Each phase is independently
shippable. Do NOT skip phases — Phase 1 is plumbing only and validates the
plugin registration before any agent-loop integration risk.
