"""Target package — shared types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class AlertSpec:
    """Structured alert. The orchestrator translates each spec into per-channel
    message formats (Telegram Markdown, email subject+body, etc.).
    """
    kind: str           # new_article | sales_info_changed | shop_changed | monitor_broken
    url: str = ""
    title: str = ""
    error: str = ""
    target: str = ""    # only set for monitor_broken — name of the failed target


@dataclass
class TargetResult:
    """Outcome of a single target run, returned to the orchestrator."""
    target: str
    success: bool
    messages: list[AlertSpec] = field(default_factory=list)
    error: Optional[str] = None
    info: str = ""
