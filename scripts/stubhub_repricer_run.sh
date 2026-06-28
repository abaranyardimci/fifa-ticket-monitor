#!/bin/bash
# Wrapper invoked by launchd to run the StubHub repricer on macOS.
#
# Responsibilities:
#   1. cd into the repo
#   2. Pull latest main (so state isn't stale relative to remote)
#   3. Source env vars (GMAIL_USER / GMAIL_APP_PASSWORD / EMAIL_TO)
#   4. Run stubhub_repricer.py using the repo's venv (recommend + email only;
#      this scheduled path NEVER changes a price — that's the on-demand --apply)
#   5. Commit + push any state file changes
#
# All output goes to LOG_FILE (append); launchd also captures stdout/stderr to
# its own log paths defined in the plist.

set -uo pipefail

REPO_DIR="${STUBHUB_REPO_DIR:-$HOME/Documents/Claude/FIFABILET}"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
ENV_FILE="$HOME/.config/stubhub-repricer/env"
LOG_FILE="$HOME/Library/Logs/stubhub-repricer.log"

mkdir -p "$(dirname "$LOG_FILE")"

exec >>"$LOG_FILE" 2>&1
echo
echo "=== stubhub-repricer run @ $(date -Iseconds) ==="

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
if git pull --rebase --autostash origin main 2>&1 | tail -5; then
    echo "git pull: ok"
else
    echo "git pull: FAILED, continuing with local state"
fi

# Source env vars (Gmail creds). Shell `export` syntax; chmod 600 by installer.
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
    echo "env: sourced $ENV_FILE"
else
    echo "WARNING: env file not found at $ENV_FILE — running in dry-run mode"
fi

echo "running stubhub_repricer.py..."
"$VENV_PYTHON" stubhub_repricer.py 2>&1
RC=$?
echo "stubhub_repricer.py exit=$RC"

# Commit + push state changes (if any). Specific files only — never `git add .`.
git add state/stubhub_prices.json state/stubhub_repricer_failures.json 2>/dev/null || true
if git diff --cached --quiet; then
    echo "state: no changes to commit"
else
    git -c user.name="stubhub-repricer-bot" \
        -c user.email="stubhub-repricer-bot@localhost" \
        commit -m "chore(state): update stubhub repricer state [skip ci]"
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
