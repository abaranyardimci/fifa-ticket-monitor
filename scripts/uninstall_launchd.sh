#!/bin/bash
# Uninstall the PB Court monitor launchd agent.
# Leaves the env file and state files in place.

set -uo pipefail

LABEL="com.user.pbcourt-monitor"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    if launchctl bootout "gui/$UID/$LABEL"; then
        echo "✓ launchctl bootout OK"
    else
        echo "WARNING: launchctl bootout failed; continuing" >&2
    fi
else
    echo "(job was not loaded — nothing to bootout)"
fi

if [ -f "$PLIST_DST" ]; then
    rm "$PLIST_DST"
    echo "✓ removed $PLIST_DST"
else
    echo "(plist already absent at $PLIST_DST)"
fi

echo
echo "Uninstalled. The env file (~/.config/pbcourt-monitor/env) and the"
echo "repo state files were left alone in case you reinstall later."
