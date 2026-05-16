#!/bin/bash
# Wrapper invoked by launchd to run the PB Court monitor on macOS.
#
# Responsibilities:
#   1. cd into the repo
#   2. Pull latest main (so our state isn't stale relative to remote)
#   3. Source env vars (GMAIL_USER / GMAIL_APP_PASSWORD / EMAIL_TO)
#   4. Run pbcourt_monitor.py using the repo's venv
#   5. Commit + push any state file changes
#
# All output (stdout + stderr) is appended to LOG_FILE; launchd also captures
# stdout/stderr to its own log paths defined in the plist.

set -uo pipefail

REPO_DIR="${PBCOURT_REPO_DIR:-$HOME/Documents/Claude/FIFABILET}"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
ENV_FILE="$HOME/.config/pbcourt-monitor/env"
LOG_FILE="$HOME/Library/Logs/pbcourt-monitor.log"

mkdir -p "$(dirname "$LOG_FILE")"

# Everything below goes to the log file (append, with timestamps via tee).
exec >>"$LOG_FILE" 2>&1
echo
echo "=== pbcourt-monitor run @ $(date -Iseconds) ==="

if [ ! -d "$REPO_DIR" ]; then
    echo "ERROR: REPO_DIR not found: $REPO_DIR"
    exit 1
fi
if [ ! -x "$VENV_PYTHON" ]; then
    echo "ERROR: venv python not found at: $VENV_PYTHON"
    echo "  Run: cd $REPO_DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m playwright install chromium"
    exit 1
fi

cd "$REPO_DIR"

# Pull latest so we don't fight with manual commits or other monitors.
# Fail-soft: if pull fails (offline, merge conflict), still attempt the run
# with the local state — we'd rather get the alert email than skip a check.
if git pull --rebase --autostash origin main 2>&1 | tail -5; then
    echo "git pull: ok"
else
    echo "git pull: FAILED, continuing with local state"
fi

# Source env vars (Gmail creds). The file uses shell `export` syntax,
# e.g. `export GMAIL_USER=you@gmail.com`. chmod 600 enforced by install script.
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
    echo "env: sourced $ENV_FILE"
else
    echo "WARNING: env file not found at $ENV_FILE — running in dry-run mode"
fi

echo "running pbcourt_monitor.py..."
"$VENV_PYTHON" pbcourt_monitor.py 2>&1
RC=$?
echo "pbcourt_monitor.py exit=$RC"

# Commit + push state changes (if any).
git add state/pbcourt_dockets_seen.json state/pbcourt_failures.json 2>/dev/null || true
if git diff --cached --quiet; then
    echo "state: no changes to commit"
else
    git -c user.name="pbcourt-monitor-bot" \
        -c user.email="pbcourt-monitor-bot@localhost" \
        commit -m "chore(state): update pbcourt monitor state [skip ci]"
    for attempt in 1 2 3; do
        if git pull --rebase --autostash origin main && git push origin main; then
            echo "state: pushed on attempt $attempt"
            break
        fi
        echo "state push: attempt $attempt failed; retrying in 5s"
        sleep 5
    done
fi

echo "=== run end @ $(date -Iseconds) rc=$RC ==="
exit 0
