"""Palm Beach County court docket monitor.

Polls https://appsgp.mypalmbeachclerk.com/eCaseView/ for new docket entries on
a specific case (default: 50-2025-DR-006596-XXXA-SB) and emails the user when
new entries appear.

The clerk site requires a guest-login click + form-driven case search, so we
use Playwright (Chromium headless) rather than plain HTTP. Each docket entry
gets a stable ID = sha1(normalized(date + type + description))[:16] so cosmetic
re-formatting doesn't trigger false "new entry" alerts.

Completely standalone from the FIFA / ATC monitors: separate state files,
separate workflow, separate failure counter. Reuses `emailer` and `http_utils`
primitives. Email-only by design (user preference); no Telegram delivery.

CLI:
    python pbcourt_monitor.py                # normal run
    python pbcourt_monitor.py --test         # send a test email
    python pbcourt_monitor.py --list-state   # print stored state
    python pbcourt_monitor.py -v             # verbose logging
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from bs4 import BeautifulSoup

import emailer
import http_utils

LOGGER = logging.getLogger("pbcourt_monitor")

# Case-specific config. The case number is public record (Florida court
# dockets are publicly searchable), so hardcoding is fine. Override via
# PB_COURT_CASE_NUMBER env var if you ever want to monitor a different case.
CASE_NUMBER = os.environ.get(
    "PB_COURT_CASE_NUMBER", "50-2025-DR-006596-XXXA-SB"
).strip()

BASE_URL = "https://appsgp.mypalmbeachclerk.com/eCaseView/"
CASE_URL = (
    "https://appsgp.mypalmbeachclerk.com/eCaseView/CaseData/Dockets"
    f"?CaseNumber={CASE_NUMBER}"
)

NAME = "pbcourt"
SUBJECT_PREFIX = "[PB Court Monitor]"

STATE_DIR = Path(__file__).resolve().parent / "state"
DOCKETS_STATE_FILE = STATE_DIR / "pbcourt_dockets_seen.json"
FAILURE_STATE_FILE = STATE_DIR / "pbcourt_failures.json"

FAILURE_LIMIT = 3

# Date-like patterns we expect in the first column of a docket row.
# Florida clerks commonly format as MM/DD/YYYY but we also accept YYYY-MM-DD
# and "Mon DD, YYYY" to be defensive.
_DATE_PATTERNS = (
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),
    re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$"),
    re.compile(r"^[A-Za-z]{3,9} \d{1,2},? \d{4}$"),
)


# ---------- data ----------

@dataclass(frozen=True)
class DocketEntry:
    id: str
    date: str
    type: str
    description: str


# ---------- channels ----------

@dataclass
class Channel:
    name: str
    send_alert: Callable[[str, str, str], None]  # (subject, text, html)
    send_test: Callable[[], None]


def _build_channels() -> List[Channel]:
    """Email-only by design. Skip Telegram even if configured."""
    channels: List[Channel] = []
    try:
        em_cfg = emailer.EmailConfig.from_env()

        def _email_send(subject: str, text: str, html: str) -> None:
            emailer.send(subject, text, html, config=em_cfg)

        channels.append(Channel(
            name="email",
            send_alert=_email_send,
            send_test=lambda: emailer.send(
                f"{SUBJECT_PREFIX} ✅ Test",
                f"PB Court monitor email pipeline OK.\n"
                f"Monitoring case: {CASE_NUMBER}\n"
                f"If you're reading this, Gmail SMTP delivery works.",
                "<p>PB Court monitor email pipeline OK.</p>"
                f"<p>Monitoring case: <b>{_html_escape(CASE_NUMBER)}</b></p>"
                "<p>If you're reading this, Gmail SMTP delivery works.</p>",
                config=em_cfg,
            ),
        ))
    except emailer.EmailConfigError as exc:
        LOGGER.warning("email channel disabled: %s", exc)
    return channels


# ---------- core run ----------

@dataclass
class RunResult:
    success: bool
    error: Optional[str] = None
    info: str = ""
    alerts: List[tuple[str, str, str]] = field(default_factory=list)


def _run_once() -> RunResult:
    try:
        entries = _fetch_dockets()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("pbcourt fetch failed")
        return RunResult(success=False, error=f"{type(exc).__name__}: {exc}")

    seen = _load_seen()

    # Guard: if parser returns 0 entries but we previously had entries,
    # treat as a fetch failure (likely WAF block, login regression, or
    # restricted-access change) rather than wiping state and re-baselining.
    if not entries and seen:
        return RunResult(
            success=False,
            error=(
                f"Parsed 0 docket entries but state has {len(seen)}. "
                "Likely login/parsing regression or restricted access. "
                "Not updating state."
            ),
        )

    if not entries:
        # First run with no entries — could be legitimate (empty docket) or
        # a parsing problem. Log it but don't alert.
        return RunResult(
            success=True,
            info="No docket entries parsed (and no prior state). Treating as empty baseline.",
        )

    is_first_run = not seen
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_entries: List[DocketEntry] = []
    for entry in entries:
        if entry.id in seen:
            continue
        new_entries.append(entry)
        seen[entry.id] = {
            "date": entry.date,
            "type": entry.type,
            "description": entry.description,
            "first_seen": now_iso,
        }

    _save_seen(seen)

    info = (
        f"parsed={len(entries)} seen={len(seen)} "
        f"new={len(new_entries)} first_run={is_first_run}"
    )

    alerts: List[tuple[str, str, str]] = []
    if new_entries and not is_first_run:
        alerts.append(_msg_new_entries(new_entries))
    elif is_first_run:
        info += " (baseline established)"

    return RunResult(success=True, info=info, alerts=alerts)


# ---------- fetcher (Playwright) ----------

def _fetch_dockets() -> List[DocketEntry]:
    """Log in as guest, navigate to the case docket page, parse entries.

    Strategy: click "Login as Guest User" on the home page (which establishes
    a session cookie), then navigate directly to the docket URL with the case
    number in the query string. If the direct URL doesn't yield a docket
    table, fall back to looking for a case-search form.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright not installed; run `playwright install chromium`"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=http_utils.USER_AGENT,
                locale="en-US",
                viewport={"width": 1280, "height": 900},
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": '"Chromium";v="126", "Not.A/Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"macOS"',
                },
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()

            # Step 1: home page → click guest login.
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
            _click_guest_login(page)

            # Step 2: navigate directly to the case docket URL.
            page.goto(CASE_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2000)
            html = page.content()
            entries = _parse_dockets(html)

            if entries:
                LOGGER.info("pbcourt: direct CASE_URL yielded %s entries", len(entries))
            else:
                LOGGER.warning(
                    "pbcourt: direct CASE_URL yielded no entries; "
                    "trying case-search form fallback"
                )
                entries = _try_search_form_fallback(page)

            return entries
        finally:
            browser.close()


def _click_guest_login(page) -> None:
    """Try several strategies to click the guest-login link/button."""
    # Strategy 1: visible link/button text "Guest".
    selectors = (
        "a:has-text('Login as Guest')",
        "button:has-text('Login as Guest')",
        "a:has-text('Guest User')",
        "a:has-text('Guest')",
        "input[type=submit][value*='Guest' i]",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                locator.click(timeout=10_000)
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1500)
                LOGGER.debug("pbcourt: guest-login clicked via %s", selector)
                return
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("pbcourt: guest-login selector %s failed: %s", selector, exc)

    # If nothing matched, log and continue — direct CASE_URL navigation may
    # still work if the session is already considered guest-authenticated.
    LOGGER.warning("pbcourt: no guest-login button found; continuing anyway")


def _try_search_form_fallback(page) -> List[DocketEntry]:
    """Look for a case-number search input and submit it."""
    # Common ASP.NET clerk-site input names.
    input_candidates = (
        "input[name*='CaseNumber' i]",
        "input[id*='CaseNumber' i]",
        "input[name*='case' i][type=text]",
        "input[placeholder*='case' i]",
    )
    for selector in input_candidates:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                locator.fill(CASE_NUMBER)
                # Try Enter, then look for a submit button.
                try:
                    locator.press("Enter")
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                html = page.content()
                entries = _parse_dockets(html)
                if entries:
                    LOGGER.info(
                        "pbcourt: search-form fallback via %s yielded %s entries",
                        selector, len(entries),
                    )
                    return entries
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("pbcourt: search-form selector %s failed: %s", selector, exc)
    return []


# ---------- parser ----------

def _parse_dockets(html: str) -> List[DocketEntry]:
    """Find the docket table in rendered HTML and extract entries.

    Defensive strategy: scan all <table> elements, score each by how many
    rows have a date-like first column, pick the highest-scoring table.
    """
    soup = BeautifulSoup(html, "lxml")
    best_table = None
    best_score = 0
    for table in soup.find_all("table"):
        score = _score_docket_table(table)
        if score > best_score:
            best_score = score
            best_table = table

    if best_table is None or best_score < 1:
        return []

    entries: List[DocketEntry] = []
    seen_ids: set[str] = set()
    for row in best_table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        # Skip header rows (cells are all <th>).
        if all(cell.name == "th" for cell in cells):
            continue
        texts = [_clean_text(c.get_text(separator=" ", strip=True)) for c in cells]
        if not _looks_like_date(texts[0]):
            continue
        date = texts[0]
        if len(texts) >= 3:
            type_ = texts[1]
            description = " ".join(texts[2:]).strip()
        else:
            type_ = ""
            description = texts[1]
        if not description:
            continue
        entry_id = _entry_id(date, type_, description)
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        entries.append(DocketEntry(
            id=entry_id,
            date=date,
            type=type_,
            description=description,
        ))
    return entries


def _score_docket_table(table) -> int:
    """Count rows in this table whose first cell looks like a date."""
    count = 0
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        first = _clean_text(cells[0].get_text(separator=" ", strip=True))
        if _looks_like_date(first):
            count += 1
    return count


def _looks_like_date(text: str) -> bool:
    if not text:
        return False
    candidate = text.strip()
    return any(p.match(candidate) for p in _DATE_PATTERNS)


def _clean_text(text: str) -> str:
    """Collapse whitespace; strip leading/trailing."""
    return " ".join(text.split())


def _entry_id(date: str, type_: str, description: str) -> str:
    """Stable per-entry ID. Normalized so cosmetic re-formatting doesn't churn."""
    norm = "|".join((
        _clean_text(date).lower(),
        _clean_text(type_).lower(),
        _clean_text(description).lower(),
    ))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# ---------- message templates ----------

def _msg_new_entries(entries: List[DocketEntry]) -> tuple[str, str, str]:
    n = len(entries)
    noun = "entry" if n == 1 else "entries"
    subject = f"{SUBJECT_PREFIX} {n} new docket {noun} — {CASE_NUMBER}"

    text_lines = [
        f"{n} new docket {noun} detected on case {CASE_NUMBER}.",
        "",
    ]
    for entry in entries:
        text_lines.append(f"- {entry.date}  [{entry.type or 'N/A'}]  {entry.description}")
    text_lines += ["", f"Case URL: {CASE_URL}"]
    text_body = "\n".join(text_lines)

    rows_html = "".join(
        f"<tr>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;white-space:nowrap'>{_html_escape(e.date)}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;white-space:nowrap'>{_html_escape(e.type) or '&mdash;'}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd'>{_html_escape(e.description)}</td>"
        f"</tr>"
        for e in entries
    )
    html_body = (
        "<html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "color:#111;max-width:760px;\">"
        f"<p><b>{n} new docket {noun}</b> detected on case "
        f"<code>{_html_escape(CASE_NUMBER)}</code>.</p>"
        "<table style='border-collapse:collapse;font-size:14px;margin-top:8px'>"
        "<thead><tr>"
        "<th style='padding:6px 10px;border:1px solid #ddd;background:#f4f4f4;text-align:left'>Date</th>"
        "<th style='padding:6px 10px;border:1px solid #ddd;background:#f4f4f4;text-align:left'>Type</th>"
        "<th style='padding:6px 10px;border:1px solid #ddd;background:#f4f4f4;text-align:left'>Description</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        f'<p style="margin-top:14px"><a href="{_html_escape(CASE_URL)}">Open case docket</a></p>'
        "</body></html>"
    )
    return subject, text_body, html_body


def _msg_monitor_broken(error_summary: str) -> tuple[str, str, str]:
    subject = f"{SUBJECT_PREFIX} ⚠️ Monitor broken"
    text = (
        f"The PB Court monitor failed {FAILURE_LIMIT} consecutive runs on case "
        f"{CASE_NUMBER}.\nLast error: {error_summary[:500]}"
    )
    html = (
        f"<p>The PB Court monitor failed <b>{FAILURE_LIMIT}</b> consecutive runs on case "
        f"<code>{_html_escape(CASE_NUMBER)}</code>.</p>"
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


# ---------- dispatch ----------

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


# ---------- CLI commands ----------

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
            "No email channel configured. Set GMAIL_USER + GMAIL_APP_PASSWORD "
            "(and optionally EMAIL_TO)."
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
    print(f"Case number: {CASE_NUMBER}")
    print(f"Case URL:    {CASE_URL}")
    print()
    seen = _load_seen()
    print(f"Docket entries seen: {len(seen)}")
    if seen:
        # Show the most recent 10 by first_seen.
        items = sorted(
            seen.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True
        )[:10]
        for entry_id, meta in items:
            date = meta.get("date", "?")
            type_ = meta.get("type", "") or "—"
            desc = meta.get("description", "")
            if len(desc) > 90:
                desc = desc[:87] + "..."
            print(f"  [{entry_id}] {date} [{type_}] {desc}")
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
        LOGGER.warning(
            "DRY RUN: no email channel configured. Alerts will be logged only."
        )

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

def _load_seen() -> dict[str, dict]:
    if not DOCKETS_STATE_FILE.exists():
        return {}
    try:
        with DOCKETS_STATE_FILE.open() as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            LOGGER.warning("%s is not a dict; resetting.", DOCKETS_STATE_FILE.name)
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        LOGGER.warning("%s is corrupt; resetting.", DOCKETS_STATE_FILE.name)
        return {}


def _save_seen(seen: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with DOCKETS_STATE_FILE.open("w") as fh:
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


# ---------- CLI ----------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Palm Beach Court docket monitor")
    parser.add_argument("--test", action="store_true",
                        help="Send a test email and exit")
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
