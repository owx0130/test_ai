"""Stage 4 -- turn day references into concrete business dates.

Technique 2 lives here. The model copies day references verbatim; this file
resolves them against ``run.today``, in code, deterministically. Anything it
cannot resolve becomes :class:`Unresolved` -- it never guesses, because a
confident wrong date is what moves a meeting twice.

Nothing in this module calls a model, reads the network, or consults the clock.
``resolve`` is a pure function of its two arguments.

**Forms understood** (F1 scope):

===========================  ====================================================
``every morning``            every business day in the window, 09:00-12:00
``daily`` / ``every day``    every business day in the window
``each Tuesday``             every Tuesday in the window
``Thu 20th`` / ``the 21st``  one dated business day
``Mon 24 Aug``               one dated business day, month named
``Wednesday``               the *next* Wednesday on or after today
``next Monday``              the Monday of the following week
``till the 21st``            today through that date, inclusive
``20th to 25th``             a dated range, inclusive
``next week`` / ``this week``  the business days of that week
``morning``/``afternoon``/``lunch``  part-of-day time defaults
===========================  ====================================================

Everything else abstains. Weekend dates and dates outside the window are
dropped -- but *reported*, never dropped silently (PRD acceptance criterion 4).
"""

from __future__ import annotations

import re
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
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "weds": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

#: Words that make a reference recurring rather than a single occurrence.
RECURRING = ("every", "each", "daily", "everyday")

#: Words that open the closing half of a range.
RANGE_WORDS = ("till", "til", "until", "through", "thru", "to", "-", "–", "—")

BUSINESS_START = time(BUSINESS_START_HOUR, 0)
BUSINESS_END = time(BUSINESS_END_HOUR, 0)

#: Part-of-day defaults, checked in this order so "lunch hour" beats "afternoon".
PART_OF_DAY: tuple[tuple[str, time, time], ...] = (
    ("lunchtime", time(12, 0), time(13, 0)),
    ("lunch", time(12, 0), time(13, 0)),
    ("morning", time(9, 0), time(12, 0)),
    ("morn", time(9, 0), time(12, 0)),
    ("afternoon", time(12, 0), time(18, 0)),
    ("all day", BUSINESS_START, BUSINESS_END),
)

#: A day-of-month written as an ordinal: "20th", "1st", "the 3rd".
_ORDINAL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b")
#: "the 21" -- a bare number is only a date when "the" or a month name marks it.
_THE_NUMBER = re.compile(r"\bthe\s+(\d{1,2})\b")
#: "till 10", "until 10:30", "till 6pm" -- a *time* bound, not a date bound.
#: The lookaheads reject "till the 21st" and "till 21st", which are dates.
_TIME_BOUND = re.compile(
    r"\b(?:till|til|until|through|thru|by)\s+(?!the\b)(\d{1,2})"
    r"(?!st\b|nd\b|rd\b|th\b)(?::(\d{2}))?\s*(am|pm)?"
)

#: How far ahead to look for a bare day-of-month. Any day number recurs within
#: 31 days; the slack covers month-length edges without ever reaching month+2.
_ORDINAL_SEARCH_DAYS = 40


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def business_days(run: RunInput) -> list[date]:
    """The Mon-Fri dates in the window, starting at ``today``.

    Part of the stage-4 contract: ``schedule.py`` builds its grid from this.
    """
    days: list[date] = []
    cursor = run.today
    while len(days) < run.window_business_days:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------


def _normalise(reference: str) -> str:
    """Lowercase, strip punctuation that never carries meaning, collapse space."""
    text = reference.lower()
    text = re.sub(r"[,.;!?()\[\]/\"']+", " ", text)
    text = re.sub(r"\b(\d{1,2})\s+(st|nd|rd|th)\b", r"\1\2", text)  # "20 th" -> "20th"
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return text.split()


def _weekday_in(tokens: list[str]) -> int | None:
    for token in tokens:
        if token in WEEKDAYS:
            return WEEKDAYS[token]
    return None


def _month_in(tokens: list[str]) -> int | None:
    for token in tokens:
        if token in MONTHS:
            return MONTHS[token]
    return None


def _day_of_month_in(text: str, tokens: list[str]) -> int | None:
    """The day number, but only where the text actually marks one as such."""
    match = _ORDINAL.search(text)
    if match:
        return int(match.group(1))
    match = _THE_NUMBER.search(text)
    if match:
        return int(match.group(1))
    if _month_in(tokens) is not None:
        for token in tokens:
            if token.isdigit() and 1 <= int(token) <= 31:
                return int(token)
    return None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


class _Abstain(Exception):
    """Raised when a reference cannot be resolved without guessing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _from_day_of_month(day: int, month: int | None, today: date) -> date:
    """The next occurrence of this day-of-month on or after ``today``."""
    if month is not None:
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError as exc:
                raise _Abstain(f"there is no day {day} in month {month}") from exc
            if candidate >= today:
                return candidate
        raise _Abstain(f"day {day} of month {month} is in the past")

    for offset in range(_ORDINAL_SEARCH_DAYS + 1):
        candidate = today + timedelta(days=offset)
        if candidate.day == day:
            return candidate
    raise _Abstain(f"no date with day-of-month {day} within reach of today")


def _next_weekday(weekday: int, today: date, *, next_week: bool) -> date:
    if next_week:
        following_monday = today + timedelta(days=7 - today.weekday())
        return following_monday + timedelta(days=weekday)
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def _single_date(text: str, run: RunInput) -> date | None:
    """One concrete calendar date, or ``None`` if the text names none."""
    tokens = _tokens(text)
    if not tokens:
        return None

    weekday = _weekday_in(tokens)
    month = _month_in(tokens)
    day = _day_of_month_in(text, tokens)

    if day is not None:
        resolved = _from_day_of_month(day, month, run.today)
        if weekday is not None and resolved.weekday() != weekday:
            raise _Abstain(
                f"{resolved:%a %d %b %Y} is a {resolved:%A}, "
                "which contradicts the weekday named"
            )
        return resolved

    if weekday is not None:
        return _next_weekday(weekday, run.today, next_week="next" in tokens)

    if "tomorrow" in tokens:
        return run.today + timedelta(days=1)
    if "today" in tokens or "tonight" in tokens:
        return run.today
    return None


def _week_dates(tokens: list[str], run: RunInput) -> list[date] | None:
    """The Mon-Fri dates of "this week" / "next week"."""
    if "week" not in tokens:
        return None
    monday = run.today - timedelta(days=run.today.weekday())
    if "next" in tokens:
        monday += timedelta(days=7)
    elif "this" in tokens or "the" in tokens or len(tokens) == 1:
        pass
    else:
        return None
    week = [monday + timedelta(days=i) for i in range(5)]
    if "next" not in tokens:
        week = [d for d in week if d >= run.today]
    return week


def _range_dates(text: str, run: RunInput) -> list[date] | None:
    """Every calendar date of an inclusive range, or ``None`` if this is not one."""
    tokens = _tokens(text)
    for index, token in enumerate(tokens):
        if token not in RANGE_WORDS:
            continue
        closing = _single_date(" ".join(tokens[index + 1 :]), run)
        if closing is None:
            continue  # e.g. "till 10" -- a time bound, handled by _resolve_times
        opening = _single_date(" ".join(tokens[:index]), run) or run.today
        if closing < opening:
            raise _Abstain(
                f"the range ends ({closing:%d %b}) before it starts ({opening:%d %b})"
            )
        span = (closing - opening).days
        return [opening + timedelta(days=i) for i in range(span + 1)]
    return None


def _candidate_dates(text: str, run: RunInput) -> tuple[list[date], bool]:
    """Every calendar date the reference names, plus whether it recurs.

    Recurring references are generated from the window, so they are already
    business days. Dated references are literal and get filtered afterwards.
    """
    tokens = _tokens(text)
    window = business_days(run)

    if any(word in tokens for word in RECURRING):
        weekday = _weekday_in(tokens)
        dates = [d for d in window if weekday is None or d.weekday() == weekday]
        bound = _recurrence_bound(text, run)
        if bound is not None:
            dates = [d for d in dates if d <= bound]
        if not dates:
            raise _Abstain("the recurrence covers no business day inside the window")
        return dates, True

    week = _week_dates(tokens, run)
    if week is not None:
        return week, False

    ranged = _range_dates(text, run)
    if ranged is not None:
        return ranged, False

    single = _single_date(text, run)
    if single is not None:
        return [single], False

    raise _Abstain("no day reference in it could be resolved to a date")


def _recurrence_bound(text: str, run: RunInput) -> date | None:
    """The closing date of "every morning till the 21st", if one is written."""
    tokens = _tokens(text)
    for index, token in enumerate(tokens):
        if token in RANGE_WORDS:
            return _single_date(" ".join(tokens[index + 1 :]), run)
    return None


# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------


def _parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError):
        return None


def _bare_hour(hour: int, minute: int, meridiem: str | None) -> time:
    """"till 10" is 10:00; "till 5" is 17:00 -- business hours disambiguate."""
    if meridiem == "am":
        hour = 0 if hour == 12 else hour
    elif meridiem == "pm":
        hour = 12 if hour == 12 else (hour + 12) % 24
    elif hour < BUSINESS_START_HOUR - 1:  # 1-7 can only mean the afternoon
        hour += 12
    return time(min(hour, 23), minute)


def _time_bound(text: str) -> time | None:
    match = _TIME_BOUND.search(text)
    if not match:
        return None
    hour, minute, meridiem = match.group(1), match.group(2), match.group(3)
    return _bare_hour(int(hour), int(minute or 0), meridiem)


def _part_of_day(text: str) -> tuple[time, time] | None:
    for keyword, start, end in PART_OF_DAY:
        if keyword in text:
            return start, end
    return None


def _resolve_times(conflict: RawConflict, text: str) -> tuple[time, time]:
    """Precedence: explicit fields > a written "till" bound > part of day > all day."""
    start = _parse_hhmm(conflict.time_start)
    end = _parse_hhmm(conflict.time_end)

    if end is None:
        end = _time_bound(text)

    part = _part_of_day(text)
    if part is not None:
        start = start if start is not None else part[0]
        end = end if end is not None else part[1]

    start = start if start is not None else BUSINESS_START
    end = end if end is not None else BUSINESS_END

    clamped_start = max(start, BUSINESS_START)
    clamped_end = min(end, BUSINESS_END)
    if clamped_end <= clamped_start:
        raise _Abstain(
            f"the time range {start:%H:%M}-{end:%H:%M} is empty inside business hours"
        )
    return clamped_start, clamped_end


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def resolve(
    raw: list[RawConflict], run: RunInput
) -> tuple[list[ResolvedConflict], list[Unresolved]]:
    """Map each raw conflict onto concrete intervals, or abstain.

    A ``free`` statement never becomes a conflict -- claiming availability adds
    no busy time, and inferring "busy at all other times" from it would be an
    invented conflict. It is still *resolved*, so an unresolvable one is
    reported rather than dropped in silence.
    """
    window = business_days(run)
    in_window = set(window)
    resolved: list[ResolvedConflict] = []
    unresolved: list[Unresolved] = []

    def abstain(conflict: RawConflict, reason: str) -> None:
        unresolved.append(
            Unresolved(speaker=conflict.speaker, quote=conflict.quote, reason=reason)
        )

    for conflict in raw:
        reference = _normalise(conflict.day_reference)
        try:
            dates, recurring = _candidate_dates(reference, run)
            start_time, end_time = _resolve_times(conflict, reference)
        except _Abstain as exc:
            abstain(conflict, f'"{conflict.day_reference}" -- {exc.reason}')
            continue

        kept = [d for d in dates if d in in_window]
        if not kept:
            abstain(conflict, _dropped_reason(conflict, dates, run, recurring))
            continue

        if conflict.polarity == "free":
            continue

        intervals = tuple(
            Interval(
                datetime.combine(day, start_time),
                datetime.combine(day, end_time),
            )
            for day in sorted(kept)
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


def _dropped_reason(
    conflict: RawConflict, dates: list[date], run: RunInput, recurring: bool
) -> str:
    """Say *why* a date was dropped. Dropping is fine; dropping quietly is not."""
    shown = ", ".join(f"{d:%a %d %b %Y}" for d in sorted(dates)[:3])
    if dates and all(d.weekday() >= 5 for d in dates):
        return (
            f'"{conflict.day_reference}" resolves to {shown}, which is not a '
            "business day (Mon-Fri only)"
        )
    last = business_days(run)[-1]
    return (
        f'"{conflict.day_reference}" resolves to {shown}, outside the '
        f"{run.window_business_days}-business-day window "
        f"({run.today:%a %d %b} to {last:%a %d %b})"
    )
