"""StubHub repricer — email command channel (APPROVE / DECLINE / MODIFY).

Lets you act on a recommendation from ANY device (iPhone, iPad, anything with
email) by replying to the recommendation email. The recommendation email carries
pre-composed Approve / Decline / Modify buttons; tapping one opens a reply whose
SUBJECT is the command + a one-time code. This daemon polls the Gmail mailbox
over IMAP (OUTBOUND only — no inbound port, nothing exposed to the network),
verifies the command, and runs the money-safe apply path.

Security model (defence in depth):
  1. Sender allow-list   — the From address must be EMAIL_TO or GMAIL_USER.
  2. Single-use nonce     — the subject carries a code; we compare its HMAC to the
                            one stored for that listing (state holds only the HMAC,
                            so a public state repo never leaks a usable token), and
                            consume it so a command can't be replayed.
  3. Freshness            — the recommendation must be < MAX_AGE_HOURS old.
  4. Apply-time gates      — the apply path still re-checks the live market, refuses
                            to net below cost, and (APPROVE) refuses if the price
                            drifted materially from what you approved.

Subjects (created by the email buttons; '<n>' is the one-time code):
    APPROVE <key> <n>            -> apply the recommended price you were emailed
    MODIFY  <key> <n> <allin>    -> apply YOUR chosen all-in price (still floor-gated)
    DECLINE <key> <n>            -> drop the recommendation, no price change

Env (from ~/.config/stubhub-repricer/env):
    GMAIL_USER / GMAIL_APP_PASSWORD   mailbox to poll (IMAP) + send results (SMTP)
    EMAIL_TO                          optional; defaults to GMAIL_USER
    STUBHUB_CMD_HMAC_SECRET           required; secret used to verify nonces
    STUBHUB_CMD_POLL_SECONDS          optional; default 90
"""
from __future__ import annotations

import email
import email.utils
import hmac
import imaplib
import logging
import os
import re
import subprocess
import time
from email.header import decode_header, make_header
from pathlib import Path

import emailer
from stubhub_repricer import (
    PRICES_STATE_FILE,
    REPO_DIR,
    _acquire_lock,
    _load_prices,
    _save_prices,
    hmac_nonce,
)

LOGGER = logging.getLogger("stubhub_commander")

VENV_PY = REPO_DIR / ".venv" / "bin" / "python"
REPRICER = REPO_DIR / "stubhub_repricer.py"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_SECONDS = int(os.environ.get("STUBHUB_CMD_POLL_SECONDS", "90"))
MAX_AGE_HOURS = 36

# Strict subject grammar. Tolerates a leading "Re:"/"Fwd:" in case the user
# replies instead of using the button. Key is alnum/underscore; nonce is the
# url-safe token; trailing integer is the MODIFY price.
_SUBJECT_RE = re.compile(
    r"^\s*(?:re|fwd)?\s*:?\s*(APPROVE|DECLINE|MODIFY)\s+([A-Za-z0-9_]+)\s+([A-Za-z0-9_\-]+)"
    r"(?:\s+\$?([\d,]+))?\s*$",
    re.IGNORECASE,
)


def _allowed_senders() -> set[str]:
    user = os.environ.get("GMAIL_USER", "").strip().lower()
    to = os.environ.get("EMAIL_TO", "").strip().lower() or user
    return {a for a in (user, to) if a}


def _decode(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:  # noqa: BLE001
        return raw or ""


def _email_result(subject: str, body: str) -> None:
    try:
        tail = body[-3500:]
        emailer.send(subject, tail,
                     f"<pre style='font-size:12px;white-space:pre-wrap'>{_esc(tail)}</pre>")
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("could not send result email: %s", exc)


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _run_apply(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [str(VENV_PY), str(REPRICER), "--apply", *args],
            capture_output=True, text=True, timeout=900, cwd=str(REPO_DIR),
        )
        body = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return ("✓ list set" in body), body
    except Exception as exc:  # noqa: BLE001
        return False, f"apply crashed: {exc}"


def _consume_nonce(key: str, nonce: str) -> tuple[bool, str, dict]:
    """Under the engine lock: verify the nonce for `key` and consume it.

    Returns (ok, reason, pending) where pending has the approved price. Returns
    ok=False with a human reason if the lock is busy (retry next poll), the nonce
    is missing/wrong, or the recommendation is stale.
    """
    lock = _acquire_lock()
    if lock is None:
        return False, "busy", {}
    try:
        state = _load_prices()
        entry = state.get(key)
        if not entry:
            return False, f"no such listing {key!r}", {}
        stored = entry.get("pending_nonce_hmac")
        if not stored:
            return False, f"no pending recommendation for {key} (already acted on, or expired)", {}
        if not hmac.compare_digest(stored, hmac_nonce(nonce)):
            return False, "code does not match the latest recommendation", {}
        pending_at = entry.get("pending_at", "")
        if _too_old(pending_at):
            _clear_pending(entry)
            state[key] = entry
            _save_prices(state)
            return False, f"recommendation from {pending_at} is older than {MAX_AGE_HOURS}h", {}
        pending = {"allin": entry.get("pending_allin"), "list": entry.get("pending_list"),
                   "at": pending_at}
        _clear_pending(entry)               # single-use: consume immediately
        state[key] = entry
        _save_prices(state)
        return True, "ok", pending
    finally:
        lock.close()


def _clear_pending(entry: dict) -> None:
    for k in ("pending_nonce_hmac", "pending_allin", "pending_list", "pending_at"):
        entry.pop(k, None)


def _too_old(iso: str) -> bool:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() > MAX_AGE_HOURS * 3600
    except Exception:  # noqa: BLE001
        return False


def _handle(action: str, key: str, nonce: str, price_raw: str | None) -> None:
    action = action.upper()
    ok, reason, pending = _consume_nonce(key, nonce)
    if not ok:
        if reason == "busy":
            raise _Busy()                   # leave message unread; retry next poll
        LOGGER.warning("rejected %s %s: %s", action, key, reason)
        _email_result(f"[StubHub Repricer] ✖︎ {action} {key} rejected", f"Rejected: {reason}")
        return

    if action == "DECLINE":
        LOGGER.info("declined %s", key)
        _email_result(f"[StubHub Repricer] ✖︎ Declined {key}",
                      f"Recommendation for {key} declined; no price change. "
                      "You'll get a fresh recommendation when the market moves.")
        return

    if action == "APPROVE":
        allin = pending.get("allin")
        if allin is None:
            _email_result(f"[StubHub Repricer] ⚠ {key} approve issue",
                          "No stored approved price; aborting.")
            return
        LOGGER.info("approving %s at all-in $%s (drift-checked)", key, allin)
        success, body = _run_apply([key, "--price", str(int(allin)), "--check-drift"])
    elif action == "MODIFY":
        n = int((price_raw or "").replace(",", "")) if price_raw else None
        if not n:
            _email_result(f"[StubHub Repricer] ⚠ {key} modify issue",
                          "MODIFY needs an all-in price in the subject, e.g. "
                          f"'MODIFY {key} <code> 1450'. No change made.")
            return
        LOGGER.info("modifying %s to all-in $%s", key, n)
        success, body = _run_apply([key, "--price", str(n)])
    else:
        return

    subj = f"[StubHub Repricer] {'✓ applied' if success else '⚠ apply issue'}: {action} {key}"
    _email_result(subj, body)


class _Busy(Exception):
    pass


def _poll_once(imap: imaplib.IMAP4_SSL) -> None:
    imap.select("INBOX")
    senders = _allowed_senders()
    # Only fetch unread messages whose subject carries one of our verbs.
    typ, data = imap.search(None, "UNSEEN", "OR", "OR",
                            "SUBJECT", "APPROVE", "SUBJECT", "DECLINE", "SUBJECT", "MODIFY")
    if typ != "OK":
        return
    for num in (data[0].split() if data and data[0] else []):
        typ, msgdata = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
        if typ != "OK" or not msgdata or not msgdata[0]:
            continue
        msg = email.message_from_bytes(msgdata[0][1])
        subject = _decode(msg.get("Subject", ""))
        m = _SUBJECT_RE.match(subject)
        if not m:
            continue                        # not one of our commands; leave it alone
        _, from_addr = email.utils.parseaddr(_decode(msg.get("From", "")))
        if from_addr.lower() not in senders:
            LOGGER.warning("ignoring command from unauthorised sender %r", from_addr)
            imap.store(num, "+FLAGS", "\\Seen")
            continue
        action, key, nonce, price_raw = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            _handle(action, key, nonce, price_raw)
        except _Busy:
            LOGGER.info("engine busy; will retry %s %s next poll", action, key)
            continue                        # leave UNSEEN so we pick it up again
        imap.store(num, "+FLAGS", "\\Seen")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")
    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not user or not pw:
        raise SystemExit("GMAIL_USER / GMAIL_APP_PASSWORD not set; refusing to start.")
    if not os.environ.get("STUBHUB_CMD_HMAC_SECRET", "").strip() \
            and not os.environ.get("STUBHUB_APPROVE_TOKEN", "").strip():
        raise SystemExit("STUBHUB_CMD_HMAC_SECRET not set; refusing to start "
                         "(run scripts/install_stubhub_commander.sh).")
    if not PRICES_STATE_FILE:  # pragma: no cover - import sanity
        raise SystemExit("state path unavailable")

    LOGGER.info("stubhub-commander polling %s every %ss (senders: %s)",
                user, POLL_SECONDS, ", ".join(sorted(_allowed_senders())))
    while True:
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            imap.login(user, pw)
            try:
                _poll_once(imap)
            finally:
                try:
                    imap.logout()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("poll cycle failed: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
