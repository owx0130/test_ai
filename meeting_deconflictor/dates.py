"""Stage 4 -- turn day references into concrete business dates.

Technique 2 starts here. The model copies day references verbatim; this file
resolves them against ``run.today``, in code, deterministically. Anything it
cannot resolve becomes :class:`Unresolved` -- it never guesses, because a
confident wrong date is what moves a meeting twice.

SKELETON SCOPE: explicit weekday names with explicit ``HH:MM`` times. That is
all T1 needs. **F1 owns this file** and adds recurrence ("every morning"),
ordinals ("Thu 20th"), ranges ("till the 21st"), relative references
("next week"), and part-of-day defaults. The extension points are marked below.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from meeting_deconflictor.models import (
    BUSINESS_END_HOUR,
    BUSINESS_START_HOUR,
    Interval,
    ResolvedConflict,
    RunInput,
    Unresolved,
)
from meeting_deconflictor.schema import RawConflict

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
}

#: Words that signal a recurrence or a range. F1 handles these; the skeleton
#: abstains on them rather than silently resolving only the first occurrence.
_NEEDS_F1 = ("every", "each", "daily", "till", "until", "through", "next", "tomorrow")


def business_days(run: RunInput) -> list[date]:
    """The Mon-Fri dates in the window, starting at ``today``."""
    days: list[date] = []
    cursor = run.today
    while len(days) < run.window_business_days:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError):
        return None


def _matching_dates(day_reference: str, window: list[date]) -> list[date]:
    """Dates in the window whose weekday is named in the reference."""
    words = day_reference.lower().replace(",", " ").replace(".", " ").split()
    wanted = {WEEKDAYS[w] for w in words if w in WEEKDAYS}
    if not wanted:
        return []
    return [d for d in window if d.weekday() in wanted]


def resolve(
    raw: list[RawConflict], run: RunInput
) -> tuple[list[ResolvedConflict], list[Unresolved]]:
    """Map each raw conflict onto concrete intervals, or abstain."""
    window = business_days(run)
    resolved: list[ResolvedConflict] = []
    unresolved: list[Unresolved] = []

    for conflict in raw:
        if conflict.polarity == "free":
            # Skeleton models availability by subtracting busy time from
            # business hours, so an explicit "free" statement adds nothing.
            # F1 decides whether "free" should narrow the window instead.
            continue

        reference = conflict.day_reference.lower()
        if any(word in reference for word in _NEEDS_F1):
            unresolved.append(
                Unresolved(
                    speaker=conflict.speaker,
                    quote=conflict.quote,
                    reason=(
                        f'"{conflict.day_reference}" is a recurrence or range; '
                        "date expansion for these is not implemented yet"
                    ),
                )
            )
            continue

        dates = _matching_dates(conflict.day_reference, window)
        if not dates:
            unresolved.append(
                Unresolved(
                    speaker=conflict.speaker,
                    quote=conflict.quote,
                    reason=(
                        f'could not resolve "{conflict.day_reference}" to a business '
                        "day inside the window"
                    ),
                )
            )
            continue

        start_time = _parse_hhmm(conflict.time_start) or time(BUSINESS_START_HOUR, 0)
        end_time = _parse_hhmm(conflict.time_end) or time(BUSINESS_END_HOUR, 0)
        if end_time <= start_time:
            unresolved.append(
                Unresolved(
                    speaker=conflict.speaker,
                    quote=conflict.quote,
                    reason=f"time range ends before it starts ({start_time}-{end_time})",
                )
            )
            continue

        intervals = tuple(
            Interval(datetime.combine(d, start_time), datetime.combine(d, end_time))
            for d in dates
        )
        resolved.append(
            ResolvedConflict(
                speaker=conflict.speaker,
                intervals=intervals,
                hardness=conflict.hardness,
                quote=conflict.quote,
            )
        )

    return resolved, unresolved
