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
    din: str          # Docket Index Number — stable per-entry identifier
    date: str         # Effective Date (MM/DD/YYYY as displayed)
    description: str
    notes: str


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
            "din": entry.din,
            "date": entry.date,
            "description": entry.description,
            "notes": entry.notes,
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

# Selectors discovered by reverse-engineering the eCaseView pages.
# If the site changes its markup, update these.
SEL_GUEST_LOGIN = "button:has-text('Login as Guest')"
SEL_CASE_NUMBER_INPUT = "#SearchRequest_CaseNumber"
SEL_SEARCH_SUBMIT = "#btnBeginSearch"
SEL_CASE_RESULT_BUTTON = "button.case-number"


def _fetch_dockets() -> List[DocketEntry]:
    """Login as guest, search by case number, open the case, parse dockets.

    reCAPTCHA v3 on the eCaseView guest-login flow blocks Playwright's bundled
    Chromium AND any headless mode (system Chrome included). The only reliable
    bypass is system Chrome with `headless=False`. We position the window
    offscreen so it doesn't steal focus.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright not installed; run `playwright install chromium`"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",      # system Chrome — bundled Chromium gets fingerprinted
            headless=False,        # headless is fingerprinted; offscreen hides the window
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-position=-2400,-2400",
                "--window-size=1280,900",
            ],
        )
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()

            # 1. Home → click guest login. Mouse moves help reCAPTCHA score.
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2000)
            _human_pause(page)
            page.locator(SEL_GUEST_LOGIN).first.click(timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            page.wait_for_timeout(2000)
            if "/Search" not in page.url:
                raise RuntimeError(
                    f"Guest login failed (reCAPTCHA likely blocked). "
                    f"Landed on {page.url} instead of /Search"
                )
            LOGGER.info("pbcourt: guest login OK on %s", page.url)

            # 2. Fill case number, submit search.
            page.locator(SEL_CASE_NUMBER_INPUT).fill(CASE_NUMBER)
            page.wait_for_timeout(500)
            page.locator(SEL_SEARCH_SUBMIT).click(timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            page.wait_for_timeout(2500)
            if "SearchResults" not in page.url:
                raise RuntimeError(f"Search submit failed; landed on {page.url}")

            # 3. Click the case in results.
            case_btn = page.locator(SEL_CASE_RESULT_BUTTON).first
            if case_btn.count() == 0:
                LOGGER.warning("pbcourt: case %s not in search results", CASE_NUMBER)
                return []
            case_btn.click(timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            page.wait_for_timeout(3000)
            LOGGER.info("pbcourt: case-detail url=%s", page.url)

            # 4. Click the "Dockets & Documents" tab.
            dockets_tab = page.locator("a:has-text('Dockets & Documents')").first
            if dockets_tab.count() == 0:
                raise RuntimeError("Dockets & Documents tab not found on case-detail page")
            dockets_tab.click(timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            page.wait_for_timeout(3000)
            LOGGER.info("pbcourt: dockets url=%s", page.url)

            # 5. Page size: default shows ~25 entries; we want ALL so we don't
            # miss newer entries that scroll off page 1. Try the DataTables
            # length selector with several common names/values. Falls back to
            # whatever default the page uses if no selector matches.
            _set_pagination_to_all(page)

            html = page.content()
            entries = _parse_dockets(html)
            LOGGER.info("pbcourt: parsed %s docket entries", len(entries))
            return entries
        finally:
            browser.close()


def _human_pause(page) -> None:
    """Small mouse movements to look human-ish before clicking reCAPTCHA buttons."""
    page.mouse.move(400, 300)
    page.wait_for_timeout(300)
    page.mouse.move(600, 400)
    page.wait_for_timeout(400)


def _set_pagination_to_all(page) -> None:
    """Switch DataTables pagination to show all entries on one page.

    The eCaseView docket table is a standard DataTables instance. The length
    selector is typically a <select> with id like '<tableId>_length' or name
    like '<tableId>_length'. We try a few patterns; if none work, we use what
    the page gave us (and may miss entries past page 1).
    """
    selectors = (
        "select[name='docketTable_length']",
        "select#docketTable_length",
        "select[name*='length' i]",
    )
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            # DataTables uses -1 for "All". Some apps use a literal '-1' option
            # value; others use the visible text 'All'. Try both.
            try:
                loc.select_option(value="-1")
            except Exception:
                try:
                    loc.select_option(label="All")
                except Exception:
                    continue
            page.wait_for_load_state("networkidle", timeout=15_000)
            page.wait_for_timeout(1500)
            LOGGER.info("pbcourt: pagination set to All via %s", selector)
            return
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("pbcourt: pagination selector %s failed: %s", selector, exc)
    LOGGER.warning(
        "pbcourt: could not switch pagination to 'All'; "
        "may only see first page of entries"
    )


# ---------- parser ----------

def _parse_dockets(html: str) -> List[DocketEntry]:
    """Extract docket entries from the eCaseView Dockets & Documents page.

    Target table: <table id="docketTable"> with columns
        [icon][icon][DIN][Effective Date][Description][Notes][icon][icon].
    Falls back to a defensive scan if id="docketTable" isn't found, looking
    for any table where multiple rows have a date in column index 3.
    """
    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", id="docketTable")
    if table is None:
        # Defensive fallback in case the id changes.
        best, best_score = None, 0
        for candidate in soup.find_all("table"):
            score = _score_docket_table(candidate)
            if score > best_score:
                best, best_score = candidate, score
        table = best
        if table is None or best_score < 1:
            return []

    entries: List[DocketEntry] = []
    seen_ids: set[str] = set()
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 6:
            continue
        if all(cell.name == "th" for cell in cells):
            continue
        texts = [_clean_text(c.get_text(separator=" ", strip=True)) for c in cells]
        din = texts[2]
        date = texts[3]
        description = texts[4]
        notes = texts[5]
        if not din or not _looks_like_date(date) or not description:
            continue
        entry_id = _entry_id(din, date, description)
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        entries.append(DocketEntry(
            id=entry_id,
            din=din,
            date=date,
            description=description,
            notes=notes,
        ))
    return entries


def _score_docket_table(table) -> int:
    """Count rows where column index 3 looks like a date (fallback heuristic)."""
    count = 0
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 4:
            continue
        date_cell = _clean_text(cells[3].get_text(separator=" ", strip=True))
        if _looks_like_date(date_cell):
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


def _entry_id(din: str, date: str, description: str) -> str:
    """Stable per-entry ID. DIN is the primary stable identifier; date + a
    small slice of description disambiguate in case DINs are ever reused."""
    norm = "|".join((
        _clean_text(din).lower(),
        _clean_text(date).lower(),
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
        line = f"- DIN {entry.din}  {entry.date}  {entry.description}"
        if entry.notes:
            line += f"  [Notes: {entry.notes}]"
        text_lines.append(line)
    text_lines += ["", f"Case search: {BASE_URL}"]
    text_body = "\n".join(text_lines)

    rows_html = "".join(
        f"<tr>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;white-space:nowrap;text-align:right'>{_html_escape(e.din)}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;white-space:nowrap'>{_html_escape(e.date)}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd'>{_html_escape(e.description)}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;color:#555;font-size:13px'>{_html_escape(e.notes) or '&mdash;'}</td>"
        f"</tr>"
        for e in entries
    )
    html_body = (
        "<html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "color:#111;max-width:900px;\">"
        f"<p><b>{n} new docket {noun}</b> detected on case "
        f"<code>{_html_escape(CASE_NUMBER)}</code>.</p>"
        "<table style='border-collapse:collapse;font-size:14px;margin-top:8px'>"
        "<thead><tr>"
        "<th style='padding:6px 10px;border:1px solid #ddd;background:#f4f4f4;text-align:right'>DIN</th>"
        "<th style='padding:6px 10px;border:1px solid #ddd;background:#f4f4f4;text-align:left'>Date</th>"
        "<th style='padding:6px 10px;border:1px solid #ddd;background:#f4f4f4;text-align:left'>Description</th>"
        "<th style='padding:6px 10px;border:1px solid #ddd;background:#f4f4f4;text-align:left'>Notes</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        f"<p style='margin-top:14px;font-size:13px;color:#555'>"
        f"To view: go to <a href='{_html_escape(BASE_URL)}'>{_html_escape(BASE_URL)}</a>, "
        f"click <i>Login as Guest User</i>, then search for case <code>{_html_escape(CASE_NUMBER)}</code>."
        f"</p>"
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
    print(f"Entry URL:   {BASE_URL} (login as guest, then search)")
    print()
    seen = _load_seen()
    print(f"Docket entries seen: {len(seen)}")
    if seen:
        # Show the most recent 10 by first_seen.
        items = sorted(
            seen.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True
        )[:10]
        for entry_id, meta in items:
            din = meta.get("din", "?")
            date = meta.get("date", "?")
            desc = meta.get("description", "")
            if len(desc) > 90:
                desc = desc[:87] + "..."
            print(f"  [{entry_id}] DIN {din:>4}  {date}  {desc}")
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
