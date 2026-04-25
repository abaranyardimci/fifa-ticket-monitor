"""Shared HTTP session + retry helper.

Realistic Chrome User-Agent, exponential backoff on 429/5xx, honors Retry-After.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Retry policy: up to 4 attempts, base delay 1s, doubling. Caps at 8s.
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 8.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def build_session() -> requests.Session:
    """Build a requests.Session with browser-like default headers."""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def get(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: float = 30.0,
    extra_headers: Optional[dict] = None,
) -> requests.Response:
    """GET with retry on 429/5xx and network errors.

    Returns the final Response (raises only on the final failed attempt).
    Honors a Retry-After header on 429 if present and reasonable (<= 60s).
    """
    sess = session or build_session()
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    last_exc: Optional[BaseException] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = sess.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            delay = _compute_delay(attempt)
            LOGGER.warning(
                "GET %s attempt %s/%s failed: %s. Sleeping %.1fs.",
                url, attempt, MAX_ATTEMPTS, exc, delay,
            )
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(delay)
            continue

        if response.status_code in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
            delay = _retry_after_or_default(response, attempt)
            LOGGER.warning(
                "GET %s attempt %s/%s returned HTTP %s. Sleeping %.1fs.",
                url, attempt, MAX_ATTEMPTS, response.status_code, delay,
            )
            time.sleep(delay)
            continue

        return response

    # Should be unreachable; guard for type checker.
    if last_exc:
        raise last_exc
    raise RuntimeError("GET retry loop exited without response")


def _compute_delay(attempt: int) -> float:
    return min(BASE_DELAY_SECONDS * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)


def _retry_after_or_default(response: requests.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            seconds = float(retry_after)
            if 0 < seconds <= 60:
                return seconds
        except ValueError:
            pass
    return _compute_delay(attempt)
