#!/bin/bash
# Wrapper invoked by launchd to keep the StubHub email-command poller running.
# Sources Gmail creds + the HMAC secret from the env file, then execs the poller.
# The poller connects OUTBOUND only (Gmail IMAP) — it opens no inbound port.
set -uo pipefail

REPO_DIR="${STUBHUB_REPO_DIR:-$HOME/FIFABILET}"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
ENV_FILE="$HOME/.config/stubhub-repricer/env"
LOG_FILE="$HOME/Library/Logs/stubhub-commander.log"

mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
echo "=== stubhub-commander start @ $(date -Iseconds) ==="

if [ ! -x "$VENV_PYTHON" ]; then
    echo "ERROR: venv python not found at $VENV_PYTHON"
    exit 1
fi
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
else
    echo "ERROR: env file not found at $ENV_FILE (need Gmail creds + STUBHUB_CMD_HMAC_SECRET)"
    exit 1
fi

cd "$REPO_DIR"
exec "$VENV_PYTHON" stubhub_commander.py
