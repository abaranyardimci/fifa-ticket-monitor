#!/bin/bash
# Install the StubHub email-command poller (APPROVE/DECLINE/MODIFY over Gmail)
# as a launchd KeepAlive agent. Generates the HMAC secret used to verify the
# one-time approve codes. Outbound IMAP only — opens no inbound port.
#
# Idempotent: re-running re-bootstraps. Run install_stubhub_repricer.sh first
# (it creates the env file with your Gmail credentials).
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
LABEL="com.user.stubhub-commander"
PLIST_TPL="$SCRIPT_DIR/$LABEL.plist.tpl"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
ENV_DIR="$HOME/.config/stubhub-repricer"
ENV_FILE="$ENV_DIR/env"

if [ "$(uname)" != "Darwin" ]; then
    echo "ERROR: launchd is macOS-only" >&2; exit 1
fi
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
    echo "ERROR: venv not set up at $REPO_DIR/.venv" >&2; exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found. Run scripts/install_stubhub_repricer.sh first." >&2; exit 1
fi

# Generate + persist an HMAC secret if not already present. This secret verifies
# the one-time approve codes; the codes themselves live only in your emails, and
# state stores only their HMAC, so a public state repo never leaks a usable code.
if ! grep -q '^export STUBHUB_CMD_HMAC_SECRET=' "$ENV_FILE"; then
    SECRET="$(openssl rand -hex 32)"
    echo "export STUBHUB_CMD_HMAC_SECRET=\"$SECRET\"" >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "✓ generated STUBHUB_CMD_HMAC_SECRET in $ENV_FILE"
else
    echo "✓ HMAC secret already present in $ENV_FILE"
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
echo "✓ Email command poller installed (KeepAlive, outbound IMAP only)."
echo "  Polls $(grep '^export GMAIL_USER=' "$ENV_FILE" | sed 's/.*=//; s/\"//g') every ~90s while the Mac is awake."
echo "  Log: ~/Library/Logs/stubhub-commander.log"
echo "  Recommendation emails now carry Approve / Decline / Modify buttons that work on any device."
echo "  NOTE: ensure Gmail IMAP is enabled (Gmail web -> Settings -> Forwarding and POP/IMAP -> Enable IMAP)."
echo "  Uninstall: launchctl bootout gui/\$UID/$LABEL && rm $PLIST_DST"
