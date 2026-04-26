"""American-Turkish Conference (ATC) re-announcement monitor.

Polls https://www.americanturkishconference.org and alerts when:
  1. The page content hash changes (catch-all signal).
  2. A high-signal keyword appears for the first time (registration link,
     speakers list, year tokens, etc.) — fired separately with higher emphasis.

Completely standalone from the FIFA monitor: separate state files, separate
workflow, separate failure counter. Reuses the generic Telegram/email/HTTP
primitives from `notifier`, `emailer`, `http_utils` but writes its own message
templates with an `[ATC Monitor]` subject prefix.

CLI:
    python atc_monitor.py                # normal run
    python atc_monitor.py --test         # send a test message via every channel
    python atc_monitor.py --list-state   # print stored state
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from bs4 import BeautifulSoup

import emailer
import http_utils
import notifier

LOGGER = logging.getLogger("atc_monitor")

URL = "https://www.americanturkishconference.org"
NAME = "atc_page"
SUBJECT_PREFIX = "[ATC Monitor]"

STATE_DIR = Path(__file__).resolve().parent / "state"
HASH_STATE_FILE = STATE_DIR / "atc_page.hash"
KEYWORDS_STATE_FILE = STATE_DIR / "atc_keywords_seen.json"
FAILURE_STATE_FILE = STATE_DIR / "atc_failures.json"

FAILURE_LIMIT = 3

# Selectors stripped before hashing/keyword-scanning to reduce noise from
# slideshow indicators, navigation chrome, scripts, etc.
_EXCLUDE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "nav",
    "footer",
    "header",
    "[role=navigation]",
    "[role=banner]",
    "[role=contentinfo]",
    "[id*=cookie]",
    "[class*=cookie]",
    "[id*=consent]",
    "[class*=consent]",
)

# Slideshow noise: "Slide 1", "Slide 1 (current slide)", etc. Stripped before
# hashing so the hash doesn't churn from carousel state.
_SLIDE_NOISE_RE = re.compile(r"\bSlide \d+( \(current slide\))?\b", re.IGNORECASE)

# High-signal keywords / phrases. When one appears on the page that wasn't
# present before, it fires a "new keyword" alert in addition to (or instead
# of) the catch-all "page changed" alert.
#
# Format: (display_label, regex_pattern). Patterns are case-insensitive and
# matched against the cleaned page text.
KEYWORD_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Registration link/CTA", re.compile(r"\b(register now|registration is open|sign up|rsvp|register here)\b", re.IGNORECASE)),
    ("Generic 'register'", re.compile(r"\bregister\b", re.IGNORECASE)),
    ("Tickets mentioned", re.compile(r"\btickets?\b", re.IGNORECASE)),
    ("Speakers section", re.compile(r"\bspeakers?\b", re.IGNORECASE)),
    ("Program / agenda published", re.compile(r"\b(program|agenda)\b(?!\s+will be published)", re.IGNORECASE)),
    ("Year 2026", re.compile(r"\b2026\b")),
    ("Year 2027", re.compile(r"\b2027\b")),
    ("Rescheduled / new date", re.compile(r"\b(rescheduled|new date|postponed to|will now (take place|be held))\b", re.IGNORECASE)),
    ("Conference confirmed", re.compile(r"\b(confirmed|now confirmed|officially announced)\b", re.IGNORECASE)),
    ("Live stream / virtual", re.compile(r"\b(live ?stream|virtual attendance|online attendance)\b", re.IGNORECASE)),
)


# ---------- channels (mirrors monitor.py shape) ----------

@dataclass
class Channel:
    name: str
    send_alert: Callable[[str, str, str], None]  # (subject, text, html)
    send_test: Callable[[], None]


def _build_channels() -> List[Channel]:
    """Detect configured channels. Reuses the same env vars as the FIFA monitor."""
    channels: List[Channel] = []

    try:
        tg_cfg = notifier.TelegramConfig.from_env()

        def _tg_send(subject: str, text: str, _html: str) -> None:
            # Telegram uses Markdown text — `subject` and `text` are concatenated.
            notifier.send(f"*{notifier.md_escape(subject)}*\n{text}", config=tg_cfg)

        channels.append(Channel(
            name="telegram",
            send_alert=_tg_send,
            send_test=lambda: notifier.send(
                "✅ *ATC monitor test*\nPipeline OK — Telegram delivery works.",
                config=tg_cfg,
            ),
        ))
    except notifier.TelegramConfigError as exc:
        LOGGER.warning("telegram channel disabled: %s", exc)

    try:
        em_cfg = emailer.EmailConfig.from_env()

        def _email_send(subject: str, text: str, html: str) -> None:
            emailer.send(subject, text, html, config=em_cfg)

        channels.append(Channel(
            name="email",
            send_alert=_email_send,
            send_test=lambda: emailer.send(
                f"{SUBJECT_PREFIX} ✅ Test",
                "ATC monitor email pipeline OK. If you're reading this, Gmail SMTP delivery works.",
                "<p>ATC monitor email pipeline OK.</p>"
                "<p>If you're reading this, Gmail SMTP delivery works.</p>",
                config=em_cfg,
            ),
        ))
    except emailer.EmailConfigError as exc:
        LOGGER.warning("email channel disabled: %s", exc)

    return channels


# ---------- message templates ----------

def _msg_page_changed(url: str, hash_short: str) -> tuple[str, str, str]:
    subject = f"{SUBJECT_PREFIX} 📝 ATC homepage changed"
    text = (
        "The American-Turkish Conference homepage content changed.\n"
        f"URL: {url}\n"
        f"New content hash: {hash_short}"
    )
    html = (
        "<p>The American-Turkish Conference homepage content changed.</p>"
        f'<p><a href="{_html_escape(url)}">Open page</a></p>'
        f"<p style=\"color:#666;font-size:12px\">New content hash: {hash_short}</p>"
    )
    return subject, text, html


def _msg_new_keyword(label: str, snippet: str, url: str) -> tuple[str, str, str]:
    subject = f"{SUBJECT_PREFIX} 🚨 NEW SIGNAL: {label}"
    text = (
        f"A new high-signal phrase appeared on the ATC homepage: {label}\n\n"
        f"Snippet: {snippet}\n\n"
        f"URL: {url}"
    )
    html = (
        f"<p>A new high-signal phrase appeared on the ATC homepage: <b>{_html_escape(label)}</b></p>"
        f"<blockquote style=\"border-left:3px solid #ccc;padding-left:10px;color:#444\">"
        f"{_html_escape(snippet)}</blockquote>"
        f'<p><a href="{_html_escape(url)}">Open page</a></p>'
    )
    return subject, text, html


def _msg_monitor_broken(error_summary: str) -> tuple[str, str, str]:
    subject = f"{SUBJECT_PREFIX} ⚠️ Monitor broken"
    text = (
        f"The ATC monitor failed {FAILURE_LIMIT} consecutive runs.\n"
        f"Last error: {error_summary[:500]}"
    )
    html = (
        f"<p>The ATC monitor failed {FAILURE_LIMIT} consecutive runs.</p>"
        f"<pre style=\"background:#f4f4f4;padding:8px;font-size:12px\">"
        f"{_html_escape(error_summary[:500])}</pre>"
    )
    return subject, text, html


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------- core run ----------

@dataclass
class RunResult:
    success: bool
    error: Optional[str] = None
    info: str = ""
    alerts: List[tuple[str, str, str]] = None  # list of (subject, text, html)

    def __post_init__(self) -> None:
        if self.alerts is None:
            self.alerts = []


def _run_once() -> RunResult:
    try:
        text = _fetch_clean_text()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("ATC fetch failed")
        return RunResult(success=False, error=f"{type(exc).__name__}: {exc}")

    if not text:
        return RunResult(
            success=False,
            error="Fetched ATC page returned no usable content.",
        )

    new_hash = _hash(text)
    previous_hash = _load_hash()
    info = f"hash={new_hash[:12]} prev={(previous_hash or '<none>')[:12]} text_len={len(text)}"

    alerts: List[tuple[str, str, str]] = []

    # Keyword diff (always runs, even on first run — but no alerts on first run).
    seen_keywords = _load_seen_keywords()
    is_first_run = previous_hash is None and not seen_keywords
    new_keyword_hits: List[tuple[str, str]] = []  # (label, snippet)

    for label, pattern in KEYWORD_SIGNALS:
        match = pattern.search(text)
        if not match:
            continue
        if label in seen_keywords:
            continue
        snippet = _extract_snippet(text, match)
        new_keyword_hits.append((label, snippet))
        seen_keywords[label] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not is_first_run:
        for label, snippet in new_keyword_hits:
            alerts.append(_msg_new_keyword(label, snippet, URL))

    # Hash diff.
    if previous_hash != new_hash:
        _save_hash(new_hash)
        if not is_first_run:
            # Avoid duplicate alert if the only change was a keyword we already
            # alerted on — keyword alerts are more specific.
            if not new_keyword_hits:
                alerts.append(_msg_page_changed(URL, new_hash[:12]))
            info += " HASH-CHANGED"
        else:
            info += " (baseline established)"

    if new_keyword_hits:
        info += f" new_keywords={[k for k, _ in new_keyword_hits]}"

    _save_seen_keywords(seen_keywords)

    return RunResult(success=True, info=info, alerts=alerts)


def _fetch_clean_text() -> str:
    response = http_utils.get(URL)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    for selector in _EXCLUDE_SELECTORS:
        for el in soup.select(selector):
            el.decompose()
    main = soup.body or soup
    text = main.get_text(separator="\n", strip=True)
    text = _SLIDE_NOISE_RE.sub("", text)
    # Collapse whitespace for stability across spurious DOM reflows.
    return "\n".join(line for line in (l.strip() for l in text.splitlines()) if line)


def _extract_snippet(text: str, match: re.Match[str], pad: int = 80) -> str:
    start = max(0, match.start() - pad)
    end = min(len(text), match.end() + pad)
    snippet = text[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


# ---------- dispatch / entrypoint ----------

def _dispatch(alert: tuple[str, str, str], channels: List[Channel]) -> None:
    subject, text, html = alert
    if not channels:
        LOGGER.info("[DRY RUN] would send: %s", subject)
        return
    for channel in channels:
        try:
            channel.send_alert(subject, text, html)
            LOGGER.info("[%s] alert dispatched: %s", channel.name, subject)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] send failed: %s", channel.name, exc)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    if args.test:
        return _cmd_test()
    if args.list_state:
        return _cmd_list_state()
    return _cmd_run()


def _cmd_test() -> int:
    channels = _build_channels()
    if not channels:
        LOGGER.error(
            "No notification channels configured. Set TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID "
            "and/or GMAIL_USER+GMAIL_APP_PASSWORD."
        )
        return 1
    failures = 0
    for channel in channels:
        try:
            channel.send_test()
            LOGGER.info("[%s] test sent", channel.name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] test FAILED: %s", channel.name, exc)
            failures += 1
    return 1 if failures else 0


def _cmd_list_state() -> int:
    print(f"Target URL: {URL}")
    print(f"Hash: {_load_hash() or '<none>'}")
    print()
    print("Seen keyword signals:")
    seen = _load_seen_keywords()
    if not seen:
        print("  (none)")
    else:
        for label, when in sorted(seen.items()):
            print(f"  - {label}  (first seen: {when})")
    print()
    print(f"Consecutive failures: {_load_failures()}")
    print()
    print("Channels configured:")
    channels = _build_channels()
    if not channels:
        print("  (none — DRY RUN mode)")
    else:
        for ch in channels:
            print(f"  - {ch.name}")
    return 0


def _cmd_run() -> int:
    channels = _build_channels()
    if not channels:
        LOGGER.warning("DRY RUN: no notification channels configured. Alerts will be logged only.")

    result = _run_once()

    failures = _load_failures()
    if result.success:
        if failures:
            LOGGER.info("recovered after %s failures", failures)
        failures = 0
    else:
        failures += 1
        LOGGER.warning("FAILED (%s consecutive). Error: %s", failures, result.error)

    if result.info:
        LOGGER.info(result.info)

    for alert in result.alerts:
        _dispatch(alert, channels)

    if failures >= FAILURE_LIMIT:
        _dispatch(_msg_monitor_broken(result.error or "unknown"), channels)
        LOGGER.warning("sent 'monitor broken' alert; resetting counter")
        failures = 0

    _save_failures(failures)
    # Always exit 0 so the workflow proceeds to commit-state and reschedule.
    return 0


# ---------- state ----------

def _load_hash() -> Optional[str]:
    if not HASH_STATE_FILE.exists():
        return None
    try:
        return HASH_STATE_FILE.read_text().strip() or None
    except OSError:
        return None


def _save_hash(value: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HASH_STATE_FILE.write_text(value + "\n")


def _load_seen_keywords() -> dict[str, str]:
    if not KEYWORDS_STATE_FILE.exists():
        return {}
    try:
        with KEYWORDS_STATE_FILE.open() as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_seen_keywords(seen: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with KEYWORDS_STATE_FILE.open("w") as fh:
        json.dump(seen, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _load_failures() -> int:
    if not FAILURE_STATE_FILE.exists():
        return 0
    try:
        with FAILURE_STATE_FILE.open() as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return int(data.get(NAME, 0))
        return 0
    except (json.JSONDecodeError, OSError, ValueError):
        return 0


def _save_failures(count: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with FAILURE_STATE_FILE.open("w") as fh:
        json.dump({NAME: count}, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- CLI ----------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ATC re-announcement monitor")
    parser.add_argument("--test", action="store_true",
                        help="Send a test message to every configured channel and exit")
    parser.add_argument("--list-state", action="store_true",
                        help="Print stored state and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging (DEBUG)")
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


if __name__ == "__main__":
    sys.exit(main())
