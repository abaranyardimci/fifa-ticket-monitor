#!/bin/bash
# Uninstall the StubHub repricer launchd user agent.
# Leaves the env file and Chrome profile in place (delete manually if desired).

set -uo pipefail

LABEL="com.user.stubhub-repricer"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null && echo "✓ booted out $LABEL" || echo "(was not loaded)"
if [ -f "$PLIST_DST" ]; then
    rm -f "$PLIST_DST"
    echo "✓ removed $PLIST_DST"
fi
echo "Done. (Kept ~/.config/stubhub-repricer/ and the Chrome profile.)"
