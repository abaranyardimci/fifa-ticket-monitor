#!/bin/bash
# Install the local one-click "Approve" server as a launchd KeepAlive agent.
# Generates a secret token (once) so the email Approve links can apply prices.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
LABEL="com.user.stubhub-approver"
PLIST_TPL="$SCRIPT_DIR/$LABEL.plist.tpl"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
ENV_DIR="$HOME/.config/stubhub-repricer"
ENV_FILE="$ENV_DIR/env"
PORT="${STUBHUB_APPROVE_PORT:-8765}"

if [ "$(uname)" != "Darwin" ]; then
    echo "ERROR: launchd is macOS-only" >&2; exit 1
fi
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
    echo "ERROR: venv not set up at $REPO_DIR/.venv" >&2; exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found. Run scripts/install_stubhub_repricer.sh first." >&2; exit 1
fi

# Generate + persist a token and port if not already present.
if ! grep -q '^export STUBHUB_APPROVE_TOKEN=' "$ENV_FILE"; then
    TOKEN="$(openssl rand -hex 16)"
    {
        echo "export STUBHUB_APPROVE_TOKEN=\"$TOKEN\""
        echo "export STUBHUB_APPROVE_PORT=\"$PORT\""
    } >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "✓ generated approve token + port ($PORT) in $ENV_FILE"
else
    echo "✓ approve token already present in $ENV_FILE"
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__HOME__|$HOME|g" "$PLIST_TPL" >"$PLIST_DST"
chmod 644 "$PLIST_DST"
echo "✓ wrote $PLIST_DST"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
if launchctl bootstrap "gui/$UID" "$PLIST_DST"; then
    echo "✓ launchctl bootstrap OK"
else
    echo "ERROR: launchctl bootstrap failed" >&2; exit 1
fi

sleep 1
echo
echo "✓ Approve server installed (KeepAlive)."
echo "  Listens on http://127.0.0.1:$PORT/approve  (localhost only)"
echo "  Log: ~/Library/Logs/stubhub-approver.log"
echo "  Future recommendation emails will include a clickable 'Approve & apply' button."
echo "  Note: clicking it must be done ON THIS MAC while it's awake."
echo "  Uninstall: launchctl bootout gui/\$UID/$LABEL && rm $PLIST_DST"
