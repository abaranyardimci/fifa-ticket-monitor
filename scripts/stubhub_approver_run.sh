#!/bin/bash
# Wrapper invoked by launchd to keep the StubHub approve server running.
# Sources creds + token from the env file, then execs the Python server.
set -uo pipefail

REPO_DIR="${STUBHUB_REPO_DIR:-$HOME/FIFABILET}"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
ENV_FILE="$HOME/.config/stubhub-repricer/env"
LOG_FILE="$HOME/Library/Logs/stubhub-approver.log"

mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
echo "=== stubhub-approver start @ $(date -Iseconds) ==="

if [ ! -x "$VENV_PYTHON" ]; then
    echo "ERROR: venv python not found at $VENV_PYTHON"
    exit 1
fi
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
else
    echo "ERROR: env file not found at $ENV_FILE (need STUBHUB_APPROVE_TOKEN + Gmail creds)"
    exit 1
fi

cd "$REPO_DIR"
exec "$VENV_PYTHON" stubhub_approver.py
