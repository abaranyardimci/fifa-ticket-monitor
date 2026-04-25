"""Target package — shared types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TargetResult:
    """Outcome of a single target run, returned to the orchestrator."""
    target: str
    success: bool
    messages: list[str] = field(default_factory=list)
    error: Optional[str] = None
    info: str = ""
