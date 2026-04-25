"""Target 3 — FIFA WC2026 news articles index (highest-priority signal).

Strategy: try the XML sitemap API first (no browser needed, fastest, most stable).
Fall back to Playwright on the HTML news page if the API returns nothing usable.

Detects new articles by URL, alerts when a new article URL or title matches any
ticket-related keyword. State persisted as a JSON map of seen URLs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import http_utils
from targets import AlertSpec, TargetResult

LOGGER = logging.getLogger(__name__)

NAME = "news"

XML_SITEMAP_URL = "https://cxm-api.fifa.com/fifaplusweb/api/sitemaps/articles/0"
NEWS_PAGE_URL = "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/news"

# Articles must be WC2026-scoped. The sitemap also contains other tournaments;
# we discard anything that doesn't match this URL substring.
WC2026_PATH_MARKER = "/canadamexicousa2026/"

# Case-insensitive substring match against URL or title triggers an alert.
KEYWORDS = (
    "ticket",
    "tickets",
    "drop",
    "sales-phase",
    "last-minute",
    "resale",
    "marketplace",
)

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "news_seen.json"


@dataclass(frozen=True)
class Article:
    url: str
    title: str


def run() -> TargetResult:
    """Fetch the news index, diff against seen state, return alert messages."""
    try:
        articles, source = _fetch_articles()
    except Exception as exc:  # noqa: BLE001 — top-level fence
        LOGGER.exception("news: fetch failed")
        return TargetResult(target=NAME, success=False, error=f"{type(exc).__name__}: {exc}")

    if not articles:
        return TargetResult(
            target=NAME,
            success=False,
            error="No articles parsed from any source.",
        )

    seen = _load_seen()
    messages: list[str] = []
    new_count = 0
    matched_count = 0
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for article in articles:
        if article.url in seen:
            continue
        new_count += 1
        seen[article.url] = {"title": article.title, "first_seen": now_iso}
        if _matches_keywords(article):
            matched_count += 1
            messages.append(
                AlertSpec(kind="new_article", title=article.title, url=article.url)
            )

    _save_seen(seen)

    info = (
        f"source={source} parsed={len(articles)} new={new_count} matched={matched_count}"
    )
    return TargetResult(target=NAME, success=True, messages=messages, info=info)


# ---------- fetchers ----------

def _fetch_articles() -> tuple[list[Article], str]:
    """Try XML sitemap, fall back to Playwright. Returns (articles, source_label)."""
    try:
        articles = _fetch_via_xml()
        if articles:
            return articles, "xml"
        LOGGER.warning("news: XML sitemap returned 0 WC2026 articles; falling back to Playwright")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("news: XML sitemap fetch failed (%s); falling back to Playwright", exc)

    articles = _fetch_via_playwright()
    return articles, "playwright"


def _fetch_via_xml() -> list[Article]:
    response = http_utils.get(XML_SITEMAP_URL)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    articles: list[Article] = []
    for url_el in root.findall("sm:url", SITEMAP_NS):
        loc_el = url_el.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        loc = loc_el.text.strip()
        if WC2026_PATH_MARKER not in loc:
            continue
        title = _title_from_url(loc)
        articles.append(Article(url=loc, title=title))
    return articles


def _fetch_via_playwright() -> list[Article]:
    """Render the news index page and extract article links."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright not installed; run `playwright install chromium`") from exc

    articles: list[Article] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=http_utils.USER_AGENT)
            page = context.new_page()
            page.goto(NEWS_PAGE_URL, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_selector(
                    f'a[href*="{WC2026_PATH_MARKER}articles/"]', timeout=15_000
                )
            except Exception:
                LOGGER.debug("news: article-link wait timed out, continuing")
            page.wait_for_timeout(2000)

            # Article links contain the WC2026 articles path.
            anchors = page.eval_on_selector_all(
                f'a[href*="{WC2026_PATH_MARKER}articles/"]',
                "els => els.map(e => ({ href: e.href, text: e.innerText.trim() }))",
            )
        finally:
            browser.close()

    seen_urls: set[str] = set()
    for item in anchors:
        href = (item.get("href") or "").split("?")[0].split("#")[0]
        if not href or WC2026_PATH_MARKER not in href or "/articles/" not in href:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        text = (item.get("text") or "").strip()
        title = text if text else _title_from_url(href)
        articles.append(Article(url=href, title=title))
    return articles


# ---------- helpers ----------

def _matches_keywords(article: Article) -> bool:
    haystack = f"{article.url}\n{article.title}".lower()
    return any(kw in haystack for kw in KEYWORDS)


def _title_from_url(url: str) -> str:
    """Derive a human-readable title from the URL slug."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        return url
    words = slug.replace("_", "-").split("-")
    # Title-case but keep all-numeric segments as-is (e.g. years).
    return " ".join(w.capitalize() if not w.isdigit() else w for w in words)


def _load_seen() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open() as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            LOGGER.warning("news_seen.json is not a dict; resetting.")
            return {}
        return data
    except json.JSONDecodeError:
        LOGGER.warning("news_seen.json is corrupt; resetting.")
        return {}


def _save_seen(seen: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as fh:
        json.dump(seen, fh, indent=2, sort_keys=True)
        fh.write("\n")


def describe_state() -> str:
    seen = _load_seen()
    matched = sum(1 for entry in seen.values() if any(
        kw in (entry.get("title", "") + " ").lower() for kw in KEYWORDS
    ))
    return (
        f"news: seen={len(seen)} previously-matched-by-title={matched} "
        f"keywords={','.join(KEYWORDS)}"
    )
