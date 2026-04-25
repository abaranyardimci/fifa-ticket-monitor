"""Email notifier — Gmail SMTP via App Password.

Sends multipart text+HTML messages with retry on transient SMTP errors.
Mirrors the shape of `notifier.py` (config dataclass, send fn, template helpers)
so the orchestrator in `monitor.py` can treat both channels uniformly.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional

LOGGER = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL

MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 8.0
SMTP_TIMEOUT_SECONDS = 20

# SMTP failure codes that are worth retrying. 421 = service not available,
# 450 = mailbox temporarily unavailable, 451 = local error, 452 = insufficient
# system storage, 4xx in general = transient. 5xx = permanent (don't retry).
_RETRY_STATUS_PREFIXES = ("4",)


@dataclass(frozen=True)
class EmailConfig:
    user: str
    app_password: str
    recipient: str
    sender_name: str = "FIFA Monitor"

    @classmethod
    def from_env(cls) -> "EmailConfig":
        user = os.environ.get("GMAIL_USER", "").strip()
        password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
        recipient = os.environ.get("EMAIL_TO", "").strip() or user
        if not user or not password:
            raise EmailConfigError(
                "Missing GMAIL_USER and/or GMAIL_APP_PASSWORD environment variables."
            )
        if "@" not in user or "@" not in recipient:
            raise EmailConfigError(
                "GMAIL_USER and EMAIL_TO (if set) must be valid email addresses."
            )
        return cls(user=user, app_password=password, recipient=recipient)


class EmailConfigError(RuntimeError):
    """Raised when SMTP creds are missing or malformed."""


def send(
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    *,
    config: Optional[EmailConfig] = None,
) -> None:
    """Send a multipart email. Retries on transient SMTP errors, raises on final failure."""
    cfg = config or EmailConfig.from_env()

    msg = EmailMessage()
    msg["From"] = formataddr((cfg.sender_name, cfg.user))
    msg["To"] = cfg.recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="fifa-monitor.local")
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    context = ssl.create_default_context()

    last_exc: Optional[BaseException] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with smtplib.SMTP_SSL(
                SMTP_HOST, SMTP_PORT, context=context, timeout=SMTP_TIMEOUT_SECONDS
            ) as smtp:
                smtp.login(cfg.user, cfg.app_password)
                smtp.send_message(msg)
            return
        except smtplib.SMTPResponseException as exc:
            last_exc = exc
            code_str = str(exc.smtp_code)
            if any(code_str.startswith(p) for p in _RETRY_STATUS_PREFIXES) and attempt < MAX_ATTEMPTS:
                delay = _delay(attempt)
                LOGGER.warning(
                    "SMTP transient error %s on attempt %s/%s: %s. Sleeping %.1fs.",
                    exc.smtp_code, attempt, MAX_ATTEMPTS, exc.smtp_error, delay,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"SMTP send failed: {exc.smtp_code} {exc.smtp_error!r}"
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            last_exc = exc
            delay = _delay(attempt)
            LOGGER.warning(
                "SMTP attempt %s/%s failed (%s: %s). Sleeping %.1fs.",
                attempt, MAX_ATTEMPTS, type(exc).__name__, exc, delay,
            )
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(delay)

    # Defensive — loop should have returned or raised.
    if last_exc:
        raise last_exc
    raise RuntimeError("SMTP send exhausted retries")


# ---------- message templates ----------
# Each helper returns (subject, body_text, body_html).

def _envelope(subject_tail: str, lines: list[str], link: Optional[tuple[str, str]] = None) -> tuple[str, str, str]:
    """Build (subject, text, html) given the alert fields."""
    subject = f"[FIFA Monitor] {subject_tail}"
    text_body = "\n".join(lines)
    if link:
        label, url = link
        text_body += f"\n\n{label}: {url}"

    html_lines = [f"<p>{_html_escape(line)}</p>" for line in lines]
    if link:
        label, url = link
        html_lines.append(f'<p><a href="{_html_escape(url)}">{_html_escape(label)}</a></p>')
    html_body = (
        "<html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "color:#111;max-width:560px;\">"
        + "".join(html_lines)
        + "</body></html>"
    )
    return subject, text_body, html_body


def shop_changed(url: str) -> tuple[str, str, str]:
    return _envelope(
        "🛒 Shop changed",
        ["The FIFA ticket shop / waiting room page changed.", "Target: ticket shop"],
        link=("Open shop", url),
    )


def sales_info_changed(url: str) -> tuple[str, str, str]:
    return _envelope(
        "📄 Sales info page changed",
        ["The FIFA ticket-sales info page was edited.", "Target: ticket-sales info page"],
        link=("Open page", url),
    )


def new_ticket_article(title: str, url: str) -> tuple[str, str, str]:
    return _envelope(
        f"🚨 NEW TICKET ARTICLE: {title}",
        ["FIFA published a new article matching ticket-related keywords.", f"Title: {title}"],
        link=("Read article", url),
    )


def monitor_broken(target: str, error_summary: str) -> tuple[str, str, str]:
    return _envelope(
        f"⚠️ Monitor broken: {target}",
        [
            f"The {target} target failed 3 consecutive runs.",
            f"Last error: {error_summary[:500]}",
        ],
    )


def test_message() -> tuple[str, str, str]:
    return _envelope(
        "✅ Test",
        [
            "FIFA monitor email pipeline OK.",
            "If you're reading this, Gmail SMTP delivery works.",
        ],
    )


# ---------- internal ----------

def _delay(attempt: int) -> float:
    return min(BASE_DELAY_SECONDS * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
