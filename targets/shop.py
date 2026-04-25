"""Target 1 — FIFA WC2026 Last-Minute Sales ticket shop / waiting room.

Strategy: Playwright always (the page is fully JS-rendered + behind a queue).
Hash the rendered main content with chrome stripped (nav, footer, cookies,
dynamic timers/timestamps) so we only react to real state transitions —
e.g. waiting-room → shop-open, or new match listings.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

import http_utils
import notifier
from targets import TargetResult

LOGGER = logging.getLogger(__name__)

NAME = "shop"
URL = "https://fwc26-shop-usd.tickets.fifa.com/"

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "shop.hash"

# Strip these before hashing — chrome and dynamic noise that would cause
# spurious "changed" alerts.
_EXCLUDE_SELECTORS = (
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
    "[class*=onetrust]",
    "[id*=onetrust]",
    "script",
    "style",
    "noscript",
    "iframe",
    # Dynamic queue elements — without stripping, the hash flips every render.
    "[class*=timer]",
    "[class*=Timer]",
    "[class*=countdown]",
    "[class*=Countdown]",
    "[id*=timer]",
    "[data-testid*=timer]",
    "[data-testid*=countdown]",
    "[aria-live]",
)

# Strip text patterns that look like timestamps / queue position numbers
# (these can churn even when the page state is unchanged).
_NOISE_PATTERNS = (
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),       # HH:MM(:SS)
    re.compile(r"\b\d{1,3}(?:[,.]\d{3})*\s*(?:in line|users?|people)\b", re.I),
    re.compile(r"\b(?:estimated|approx)\.?\s+\d+\s*(?:min|minutes|sec|seconds)\b", re.I),
)

# Akamai/WAF block-page signatures. When detected we treat the run as a
# failure so the per-run hash never reflects a transient block (which would
# otherwise produce false-positive "shop changed" alerts and hide the real
# signal). After 3 consecutive blocks the "monitor broken" alert escalates.
_WAF_BLOCK_MARKERS = (
    "Bad request",
    "A critical request has been detected and therefore blocked",
    "Access Denied",
    "Reference #",
    "FIFA Service Desk",
)


def run() -> TargetResult:
    try:
        signature = _render_signature()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("shop: render failed")
        return TargetResult(target=NAME, success=False, error=f"{type(exc).__name__}: {exc}")

    if not signature:
        return TargetResult(
            target=NAME,
            success=False,
            error="Rendered page produced empty signature.",
        )

    if _looks_like_waf_block(signature):
        return TargetResult(
            target=NAME,
            success=False,
            error=f"WAF block page detected (len={len(signature)}). "
                  "Datacenter IP likely blocked by Akamai.",
        )

    previous = _load_hash()
    new_hash = _hash(signature)
    info = f"hash={new_hash[:12]} prev={(previous or '<none>')[:12]} sig_len={len(signature)}"

    if previous == new_hash:
        return TargetResult(target=NAME, success=True, info=info)

    _save_hash(new_hash)

    if previous is None:
        return TargetResult(target=NAME, success=True, info=info + " (baseline established)")

    return TargetResult(
        target=NAME,
        success=True,
        messages=[notifier.shop_changed(URL)],
        info=info + " CHANGED",
    )


def _render_signature() -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright not installed; run `playwright install chromium`") from exc

    with sync_playwright() as pw:
        # Mild stealth: disable the AutomationControlled flag that WAFs key on.
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
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": '"Chromium";v="126", "Not.A/Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"macOS"',
                },
            )
            # Hide the navigator.webdriver flag (cheap, sometimes helps).
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            # `networkidle` is unreliable on shop/queue pages with telemetry.
            # Use domcontentloaded + a generous settle, plus a best-effort
            # wait for any prominent content container.
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_selector(
                    "main, [role=main], #root > div, body > div > div",
                    timeout=15_000,
                )
            except Exception:
                LOGGER.debug("shop: content selector wait timed out, continuing")
            # Waiting-room pages can take several seconds to populate state.
            page.wait_for_timeout(4000)
            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "lxml")
    for selector in _EXCLUDE_SELECTORS:
        for el in soup.select(selector):
            el.decompose()

    main = (
        soup.select_one("main")
        or soup.select_one("[role=main]")
        or soup.select_one("#root")
        or soup.body
    )
    if main is None:
        return ""

    text = main.get_text(separator="\n", strip=True)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for pattern in _NOISE_PATTERNS:
            line = pattern.sub("", line)
        line = " ".join(line.split())
        if line:
            lines.append(line)
    return "\n".join(lines)


def _load_hash() -> Optional[str]:
    if not STATE_FILE.exists():
        return None
    try:
        return STATE_FILE.read_text().strip() or None
    except OSError:
        return None


def _save_hash(value: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(value + "\n")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _looks_like_waf_block(signature: str) -> bool:
    if len(signature) > 600:
        # Real shop pages are much larger than block error pages.
        return False
    matches = sum(1 for marker in _WAF_BLOCK_MARKERS if marker.lower() in signature.lower())
    return matches >= 2


def describe_state() -> str:
    h = _load_hash() or "<none>"
    return f"shop: hash={h[:16]}... url={URL}"
