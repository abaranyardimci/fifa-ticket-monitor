"""Target 2 — FIFA WC2026 ticket-sales info page.

Strategy: try parsing the embedded `__NEXT_DATA__` JSON blob first (no browser).
Fall back to Playwright that hashes the rendered main content if the JSON is absent.

Excludes nav, footer, cookie banner, and dynamic chrome from the hash.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

import http_utils
import notifier
from targets import TargetResult

LOGGER = logging.getLogger(__name__)

NAME = "sales_info"
URL = "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/ticket-sales"

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "sales_info.hash"

# Regex pulls the JSON payload out of <script id="__NEXT_DATA__" type="application/json">{...}</script>
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

# Selectors to remove before hashing the rendered DOM (Playwright fallback).
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
    "[class*=skipLink]",
    "script",
    "style",
    "noscript",
    "iframe",
)


def run() -> TargetResult:
    try:
        signature, source = _compute_signature()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("sales_info: signature compute failed")
        return TargetResult(target=NAME, success=False, error=f"{type(exc).__name__}: {exc}")

    if not signature:
        return TargetResult(
            target=NAME,
            success=False,
            error="Computed empty signature — page returned no usable content.",
        )

    previous = _load_hash()
    new_hash = _hash(signature)
    info = f"source={source} hash={new_hash[:12]} prev={(previous or '<none>')[:12]}"

    if previous == new_hash:
        return TargetResult(target=NAME, success=True, info=info)

    _save_hash(new_hash)

    if previous is None:
        # First run: record baseline silently. We don't want a flood of "changed"
        # alerts the very first time the monitor sees each page.
        return TargetResult(target=NAME, success=True, info=info + " (baseline established)")

    return TargetResult(
        target=NAME,
        success=True,
        messages=[notifier.sales_info_changed(URL)],
        info=info + " CHANGED",
    )


# ---------- signature sources ----------

def _compute_signature() -> tuple[str, str]:
    """Returns (signature_text, source_label)."""
    response = http_utils.get(URL)
    response.raise_for_status()
    html = response.text

    # 1) __NEXT_DATA__ — preferred.
    next_data = _extract_next_data(html)
    if next_data:
        return next_data, "next_data"

    # 2) Fallback — Playwright render.
    LOGGER.warning("sales_info: __NEXT_DATA__ not found, falling back to Playwright")
    rendered = _render_with_playwright()
    return rendered, "playwright"


def _extract_next_data(html: str) -> Optional[str]:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("sales_info: __NEXT_DATA__ JSON parse failed")
        return None

    # Hash on the page-content portion (props.pageProps) when available; this
    # strips request-bound noise like build IDs and runtime config.
    page_props = (
        data.get("props", {}).get("pageProps") if isinstance(data, dict) else None
    )
    target = page_props if page_props is not None else data
    return json.dumps(target, sort_keys=True, ensure_ascii=False)


def _render_with_playwright() -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright not installed; run `playwright install chromium`") from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=http_utils.USER_AGENT)
            page = context.new_page()
            # `networkidle` never fires on pages with analytics/websockets.
            # Wait for DOM, then for the main content container, then settle.
            page.goto(URL, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_selector("main#main-content, main, [role=main]", timeout=15_000)
            except Exception:
                LOGGER.debug("sales_info: main selector wait timed out, continuing")
            page.wait_for_timeout(2500)
            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "lxml")
    for selector in _EXCLUDE_SELECTORS:
        for el in soup.select(selector):
            el.decompose()

    main = soup.select_one("main#main-content") or soup.select_one("main") or soup.body
    if main is None:
        return ""
    text = main.get_text(separator="\n", strip=True)
    # Collapse whitespace for stability.
    return "\n".join(line for line in (l.strip() for l in text.splitlines()) if line)


# ---------- state ----------

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


def describe_state() -> str:
    h = _load_hash() or "<none>"
    return f"sales_info: hash={h[:16]}... url={URL}"
