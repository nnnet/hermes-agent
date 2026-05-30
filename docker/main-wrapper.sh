#!/command/with-contenv sh
# shellcheck shell=sh
# /opt/hermes/docker/main-wrapper.sh — wraps the container's CMD with
# the same argument-routing logic the pre-s6 entrypoint.sh used. Runs
# as /init's "main program" (Docker CMD) so it inherits stdin/stdout/
# stderr from the container.
#
# IMPORTANT — the shebang MUST be ``#!/command/with-contenv sh`` (NOT
# bare ``#!/bin/sh``).  s6-overlay stores container env vars (those
# populated by Docker's ``env_file:`` / ``environment:`` blocks) into
# /run/s6/container_environment/<NAME> files, not into PID 1's actual
# environ.  Plain ``sh`` inherits PID 1's environ → minimal env (just
# PATH + a couple of basics).  The ``with-contenv`` wrapper reads those
# files first, then execs sh with the full env applied, so the gateway
# process actually sees TELEGRAM_BOT_TOKEN, HERMES_HOME, OPENAI_API_KEY
# etc.  Without this, the gateway logs "No messaging platforms enabled"
# at boot and the bot never connects, even though the env_file is loaded
# and visible via ``docker exec hermes printenv``.
# (Upstream phrasing 2026-05-30: /init scrubs env before invoking CMD,
# so `#!/bin/sh` wrapper sees an empty environ — same root cause.)
#
# Routing:
#   no args                       → exec `hermes` (the default)
#   first arg is an executable    → exec it directly (sleep, bash, sh, …)
#   first arg is anything else    → exec `hermes <args>` (subcommand passthrough)
#
# We drop to the hermes user via `s6-setuidgid` so the supervised
# workload runs unprivileged (UID 10000 by default).
set -e

# Pin HOME to the hermes user's actual home dir so libraries that
# expand ``~`` / ``$HOME`` (notably the gateway's per-bot-token lock
# file at $HOME/.local/state/hermes/gateway-locks/, and upstream's
# discord lockfile under XDG_STATE_HOME) write into the bind-mounted
# data dir and not into PID 1's leaked /root path.
# Without this the gateway dies on TG connect with:
#   PermissionError: [Errno 13] Permission denied:
#   '/root/.local/state/hermes/gateway-locks/telegram-bot-token-*.lock'
# Dockerfile creates hermes with useradd -u 10000 -m -d /opt/data,
# so /opt/data is the canonical home (and the bind-mount target for
# host's .hermes state dir).
export HOME=/opt/data

cd /opt/data
# shellcheck disable=SC1091
. /opt/hermes/.venv/bin/activate

if [ $# -eq 0 ]; then
    exec s6-setuidgid hermes hermes
fi

if command -v "$1" >/dev/null 2>&1; then
    # Bare executable — pass through directly.
    exec s6-setuidgid hermes "$@"
fi

# Hermes subcommand pass-through.
exec s6-setuidgid hermes hermes "$@"
