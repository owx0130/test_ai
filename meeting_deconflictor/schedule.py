"""Stage 5 -- find contiguous blocks and rank them.

Ranking is the PRD's strict order:

1. all required attendees free
2. most optional attendees free
3. earliest

Soft conflicts deduct (they break ties before "earliest") but never exclude.
Hard conflicts exclude.

**F2 owns this file**, together with ``verify.py``.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from meeting_deconflictor.models import (
    BUSINESS_END_HOUR,
    BUSINESS_START_HOUR,
    GRANULARITY_MINUTES,
    Interval,
    ResolvedConflict,
    RunInput,
    Slot,
)
from meeting_deconflictor.dates import business_days


def candidate_starts(run: RunInput) -> list[datetime]:
    """Every grid position a meeting of this length could start at."""
    step = timedelta(minutes=GRANULARITY_MINUTES)
    length = timedelta(minutes=run.duration_minutes)
    starts: list[datetime] = []
    for day in business_days(run):
        cursor = datetime.combine(day, time(BUSINESS_START_HOUR, 0))
        day_end = datetime.combine(day, time(BUSINESS_END_HOUR, 0))
        while cursor + length <= day_end:
            starts.append(cursor)
            cursor += step
    return starts


def _clashes(conflict: ResolvedConflict, span: Interval) -> bool:
    return any(i.overlaps(span) for i in conflict.intervals)


def find_slots(conflicts: list[ResolvedConflict], run: RunInput) -> list[Slot]:
    """All candidate slots, best first. Callers take the top N."""
    length = timedelta(minutes=run.duration_minutes)
    hard = [c for c in conflicts if c.hardness == "hard"]
    soft = [c for c in conflicts if c.hardness == "soft"]

    slots: list[Slot] = []
    for start in candidate_starts(run):
        span = Interval(start, start + length)

        busy_hard = {c.speaker for c in hard if _clashes(c, span)}

        free_required = tuple(a for a in run.required if a not in busy_hard)
        free_optional = tuple(a for a in run.optional if a not in busy_hard)

        broken = tuple(
            (c.speaker, c.quote)
            for c in hard
            if c.speaker in run.required and _clashes(c, span)
        )
        soft_broken = tuple(
            (c.speaker, c.quote)
            for c in soft
            if c.speaker in run.attendees and _clashes(c, span)
        )

        slots.append(
            Slot(
                start=span.start,
                end=span.end,
                free_required=free_required,
                free_optional=free_optional,
                broken=broken,
                soft_broken=soft_broken,
            )
        )

    slots.sort(key=lambda s: _rank(s, run))
    return slots


def _rank(slot: Slot, run: RunInput) -> tuple[int, int, int, datetime]:
    """Lower sorts first. Mirrors the PRD's strict ordering."""
    all_required_free = len(slot.free_required) == len(run.required)
    return (
        0 if all_required_free else 1,      # 1. all required free
        -len(slot.free_optional),           # 2. most optional free
        len(slot.soft_broken),              # soft conflicts deduct
        slot.start,                         # 3. earliest
    )
