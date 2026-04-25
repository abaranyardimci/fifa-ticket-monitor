"""Telegram notifier.

Sends Markdown messages via the Bot API with retry on 429/5xx.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

LOGGER = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram MarkdownV1 special characters that need escaping in dynamic text.
# We use legacy Markdown (parse_mode=Markdown) since it's permissive on '!' and '.'
# but still requires escaping the structural chars below if they appear literally.
_MD_ESCAPE_RE = re.compile(r"([_*`\[])")

MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 8.0


class TelegramConfigError(RuntimeError):
    """Raised when bot token / chat id are missing."""


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise TelegramConfigError(
                "Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID environment variables."
            )
        return cls(token=token, chat_id=chat_id)


def md_escape(text: str) -> str:
    """Escape Markdown special chars in user-controlled text."""
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


def send(text: str, *, config: Optional[TelegramConfig] = None) -> None:
    """Send a Markdown-formatted message. Retries on 429/5xx, raises on final failure."""
    cfg = config or TelegramConfig.from_env()
    url = TELEGRAM_API.format(token=cfg.token)
    payload = {
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(url, data=payload, timeout=15)
        except requests.RequestException as exc:
            delay = _delay(attempt)
            LOGGER.warning(
                "Telegram POST attempt %s/%s network error: %s. Sleeping %.1fs.",
                attempt, MAX_ATTEMPTS, exc, delay,
            )
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(delay)
            continue

        if response.status_code == 200:
            return

        if response.status_code == 429:
            retry_after = _retry_after(response, attempt)
            LOGGER.warning(
                "Telegram rate-limited (429). Sleeping %.1fs.", retry_after,
            )
            time.sleep(retry_after)
            continue

        if 500 <= response.status_code < 600 and attempt < MAX_ATTEMPTS:
            delay = _delay(attempt)
            LOGGER.warning(
                "Telegram %s attempt %s/%s. Sleeping %.1fs. Body: %s",
                response.status_code, attempt, MAX_ATTEMPTS, delay, response.text[:200],
            )
            time.sleep(delay)
            continue

        # Non-retryable failure (e.g. 400 bad request — bad markdown / chat_id).
        raise RuntimeError(
            f"Telegram sendMessage failed: HTTP {response.status_code} {response.text[:300]}"
        )

    raise RuntimeError("Telegram sendMessage exhausted retries")


# ---------- message templates ----------

def shop_changed(url: str) -> str:
    return (
        "🛒 *Shop changed*\n"
        f"Target: ticket shop\n"
        f"[Open shop]({url})"
    )


def sales_info_changed(url: str) -> str:
    return (
        "📄 *Sales info page changed*\n"
        f"Target: ticket-sales info page\n"
        f"[Open page]({url})"
    )


def new_ticket_article(title: str, url: str) -> str:
    return (
        "🚨 *NEW TICKET ARTICLE*\n"
        f"*{md_escape(title)}*\n"
        f"[Read article]({url})"
    )


def monitor_broken(target: str, error_summary: str) -> str:
    return (
        f"⚠️ *Monitor broken: {md_escape(target)}*\n"
        f"3 consecutive failures.\n"
        f"Last error: `{md_escape(error_summary)[:300]}`"
    )


def test_message() -> str:
    return (
        "✅ *FIFA monitor test*\n"
        "Pipeline OK — Telegram delivery works."
    )


# ---------- internal ----------

def _delay(attempt: int) -> float:
    return min(BASE_DELAY_SECONDS * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)


def _retry_after(response: requests.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            seconds = float(raw)
            if 0 < seconds <= 120:
                return seconds
        except ValueError:
            pass
    # Telegram also returns retry_after in JSON parameters.
    try:
        params = response.json().get("parameters") or {}
        seconds = float(params.get("retry_after", 0))
        if 0 < seconds <= 120:
            return seconds
    except (ValueError, requests.JSONDecodeError):
        pass
    return _delay(attempt)
