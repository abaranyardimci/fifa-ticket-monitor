#!/bin/bash
# Keep the Mac awake (on AC power) so the repricer's scheduled reads fire and the
# email command poller stays responsive 24/7. Installs a no-sudo launchd agent
# running `caffeinate -s`. Optionally enables lid-closed (clamshell) operation,
# which requires one sudo command.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
LABEL="com.user.stubhub-caffeinate"
PLIST_TPL="$SCRIPT_DIR/$LABEL.plist.tpl"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$(uname)" != "Darwin" ]; then
    echo "ERROR: macOS-only" >&2; exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__HOME__|$HOME|g" "$PLIST_TPL" >"$PLIST_DST"
chmod 644 "$PLIST_DST"
echo "✓ wrote $PLIST_DST"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
if launchctl bootstrap "gui/$UID" "$PLIST_DST"; then
    echo "✓ launchctl bootstrap OK (caffeinate -s running)"
else
    echo "ERROR: launchctl bootstrap failed" >&2; exit 1
fi

echo
echo "✓ Mac will stay awake WHILE PLUGGED IN. Keep it on AC power for 24/7 monitoring."
echo "  (caffeinate -s does not assert on battery — on battery it will still sleep.)"
echo
echo "For lid-closed / clamshell operation on AC, run this ONCE (needs sudo):"
echo "    sudo pmset -c disablesleep 1"
echo "  To undo later:   sudo pmset -c disablesleep 0"
echo
echo "  Uninstall: launchctl bootout gui/\$UID/$LABEL && rm $PLIST_DST"
