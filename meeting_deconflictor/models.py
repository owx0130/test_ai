"""Shared data types for the meeting deconflictor.

FROZEN after Task 1. Every stage module imports from here and none of them may
edit it. Adding a field is a separate one-line change agreed by the team, never
bundled into a feature branch -- see TEAM_PLAN.md, merge-conflict warning 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Hardness = Literal["hard", "soft"]

#: Business hours, inclusive start / exclusive end, applied Mon-Fri only.
BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 18

#: Scheduling granularity. Every interval boundary snaps to this grid.
GRANULARITY_MINUTES = 30


@dataclass(frozen=True)
class Message:
    """One line of the pasted thread."""

    speaker: str
    text: str


@dataclass(frozen=True)
class RunInput:
    """Everything the user supplies for a single run."""

    messages: tuple[Message, ...]
    today: date
    window_business_days: int = 10
    duration_minutes: int = 60
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.messages) > 25:
            raise ValueError(
                f"at most 25 messages per run (got {len(self.messages)}) -- see PRD non-goals"
            )

    @property
    def attendees(self) -> tuple[str, ...]:
        return self.required + self.optional


@dataclass(frozen=True, order=True)
class Interval:
    """A busy or free span.

    HALF-OPEN: ``[start, end)``. Two intervals that merely touch -- one ending
    exactly where the next begins -- do NOT overlap. F1 and F2 both depend on
    this; it is stated here so neither has to guess (TEAM_PLAN.md warning 4).
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"interval must be non-empty: {self.start} -> {self.end}")

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class ResolvedConflict:
    """A declared conflict, mapped onto concrete dates by code (never the model)."""

    speaker: str
    intervals: tuple[Interval, ...]
    hardness: Hardness
    quote: str


@dataclass(frozen=True)
class Unresolved:
    """Something we refused to guess at. Always surfaced to the human."""

    speaker: str
    quote: str
    reason: str


@dataclass(frozen=True)
class Slot:
    """A candidate meeting time."""

    start: datetime
    end: datetime
    free_required: tuple[str, ...]
    free_optional: tuple[str, ...]
    #: (speaker, quote) pairs whose HARD conflict this slot breaks. Empty means clean.
    broken: tuple[tuple[str, str], ...] = ()
    #: (speaker, quote) pairs whose SOFT conflict this slot brushes. Advisory only.
    soft_broken: tuple[tuple[str, str], ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.broken


@dataclass(frozen=True)
class RunOutput:
    """What the renderer turns into text."""

    slots: tuple[Slot, ...]
    conflicts: tuple[ResolvedConflict, ...]
    unresolved: tuple[Unresolved, ...]
