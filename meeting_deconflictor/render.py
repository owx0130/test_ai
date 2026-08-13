"""Stage 7 -- the three-block text output. **F4 owns this file.**

The format is fixed by the worked example in `PRD.md`, and
`tests/test_render.py::test_matches_prd_example` compares against that block
character for character. So the punctuation here is spec, not decoration: an
en dash inside a time range, an em dash before a list, a middle dot between one
speaker's several conflicts.

Two things this module refuses to do:

* **Invent a roster.** `RunOutput` records who is *free*, never who was *asked*,
  so "1 of 2 optional" is not derivable from it alone. Pass the `RunInput` and
  the counts are exact; omit it and the annotation states only what it can see.
* **Overstate a span.** A conflict's intervals are collapsed into a phrase
  ("daily 09:00-10:00", "through Fri 21 Aug") only when the intervals justify
  it, and the window is read off the dates present rather than assumed.
  Anything else is listed date by date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from meeting_deconflictor.models import (
    BUSINESS_END_HOUR,
    BUSINESS_START_HOUR,
    Interval,
    ResolvedConflict,
    RunInput,
    RunOutput,
    Slot,
)

EN_DASH = "–"  # inside a time range: 10:00-11:00
EM_DASH = "—"  # between a label and its contents
MIDDLE_DOT = "·"  # between one speaker's conflicts

DATE_FORMAT = "%a %d %b"
#: Column the parenthetical free-count starts in, measured off the PRD example.
ANNOTATION_COLUMN = 25
#: Width of the speaker column in the conflict echo, likewise.
SPEAKER_COLUMN = 8
#: Indent for a slot's continuation lines, sitting under its text.
CONTINUATION = " " * 5


def _padded(text: str, width: int) -> str:
    """Pad to `width`, but never let the next field touch the text."""
    return text.ljust(width) if len(text) < width else text + " "


def _day(moment: datetime | date) -> str:
    return format(moment, DATE_FORMAT)


def _clock(interval: Interval) -> str:
    return f"{interval.start:%H:%M}{EN_DASH}{interval.end:%H:%M}"


def _is_all_day(interval: Interval) -> bool:
    return (
        interval.start.date() == interval.end.date()
        and (interval.start.hour, interval.start.minute) == (BUSINESS_START_HOUR, 0)
        and (interval.end.hour, interval.end.minute) == (BUSINESS_END_HOUR, 0)
    )


def _business_days(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _observed_window(out: RunOutput) -> tuple[date, date] | None:
    """The span of dates this output actually mentions.

    The renderer is handed no window, so it infers one rather than assuming ten
    business days. Used only to decide whether a run of days reaches the edge of
    what we can see -- never to extend a conflict.
    """
    days = [iv.start.date() for c in out.conflicts for iv in c.intervals]
    days += [slot.start.date() for slot in out.slots]
    return (min(days), max(days)) if days else None


def _one_interval(interval: Interval) -> str:
    if _is_all_day(interval):
        return f"all day {_day(interval.start)}"
    if interval.start.date() != interval.end.date():
        return (
            f"{_day(interval.start)} {interval.start:%H:%M} "
            f"{EN_DASH} {_day(interval.end)} {interval.end:%H:%M}"
        )
    return f"{_day(interval.start)} {_clock(interval)}"


def _summarise(conflict: ResolvedConflict, window: tuple[date, date] | None) -> str:
    """Collapse a conflict's intervals into the shortest phrase they support."""
    intervals = sorted(conflict.intervals)
    if len(intervals) == 1:
        return _one_interval(intervals[0])

    days = [iv.start.date() for iv in intervals]
    first, last = days[0], days[-1]
    one_clock = len({(iv.start.time(), iv.end.time()) for iv in intervals}) == 1
    consecutive = days == _business_days(first, last)

    if one_clock and consecutive and all(_is_all_day(iv) for iv in intervals):
        if window and first == window[0]:
            return f"through {_day(last)}"
        # Starting mid-window, "through X" would hide when the leave began.
        return f"all day {_day(first)} through {_day(last)}"
    if one_clock and consecutive:
        if window == (first, last):
            return f"daily {_clock(intervals[0])}"
        # Short of the window: say which days, or we claim days nobody mentioned.
        return f"daily {_clock(intervals[0])}, {_day(first)} through {_day(last)}"
    if one_clock and len({day.weekday() for day in days}) == 1:
        return f"every {first:%a} {_clock(intervals[0])}"
    return ", ".join(_one_interval(iv) for iv in intervals)


def _annotation(slot: Slot, run: RunInput | None) -> str:
    """The parenthetical free-count: exact with a roster, modest without one."""
    if run is None:
        parts = [f"{len(slot.free_required)} required free"]
        if slot.free_optional:
            parts.append(f"{len(slot.free_optional)} optional free")
        return ", ".join(parts)

    parts = []
    if run.required:
        free = len(slot.free_required)
        if free < len(run.required):
            parts.append(f"{free} of {len(run.required)} required free")
        elif free == 2:
            parts.append("both required free")
        else:
            parts.append("all required free")
    if run.optional:
        parts.append(f"{len(slot.free_optional)} of {len(run.optional)} optional")
    return ", ".join(parts)


def _slot_lines(index: int, slot: Slot, annotation: str) -> list[str]:
    attendees = ", ".join(slot.free_required + slot.free_optional) or "nobody"
    head = (
        f"{index}. {_day(slot.start)}, {slot.start:%H:%M}{EN_DASH}{slot.end:%H:%M}"
        f"  {EM_DASH} "
    )
    if annotation:
        head += f"{_padded(attendees, ANNOTATION_COLUMN)}({annotation})"
    else:
        head += attendees

    lines = [head]
    for label, pairs in (("breaks", slot.broken), ("brushes", slot.soft_broken)):
        if pairs:
            named = "; ".join(f'{who} {EM_DASH} "{quote}"' for who, quote in pairs)
            lines.append(f"{CONTINUATION}{label}: {named}")
    return lines


def _proposal_block(out: RunOutput, run: RunInput | None) -> list[str]:
    if not out.slots:
        return ["No slot in the window fits the requested duration."]

    lines: list[str] = []
    # `find_slots` ranks every candidate, so the top N can trail broken options
    # behind a clean one. Acceptance criterion 1 forbids *proposing* those, so
    # they are shown only when there is nothing clean to propose (T3).
    proposals = [slot for slot in out.slots if slot.is_clean]
    if not proposals:
        proposals = list(out.slots)
        lines += ["No slot works for every required attendee. Nearest options:", ""]

    previous = None
    for index, slot in enumerate(proposals, start=1):
        annotation = _annotation(slot, run)
        # The PRD states the free-count once and repeats it only when it changes.
        lines += _slot_lines(index, slot, "" if annotation == previous else annotation)
        previous = annotation
    return lines


def _echo_block(out: RunOutput) -> list[str]:
    """Every conflict, grouped by speaker in the order they were read."""
    grouped: dict[str, list[str]] = {}
    window = _observed_window(out)
    for conflict in out.conflicts:
        grouped.setdefault(conflict.speaker, []).append(
            f"{_summarise(conflict, window)} ({conflict.hardness})"
        )

    if not grouped:
        return ["  (none)"]
    joiner = f" {MIDDLE_DOT} "
    return [
        f"  {_padded(speaker, SPEAKER_COLUMN)}{EM_DASH} {joiner.join(entries)}"
        for speaker, entries in grouped.items()
    ]


def _confirmation_block(out: RunOutput) -> list[str]:
    if not out.unresolved:
        return ["  (nothing)"]
    return [
        f'  {item.speaker} {EM_DASH} "{item.quote}" ({item.reason})'
        if item.quote  # an unheard person has no quote to show, only a reason
        else f"  {item.speaker} {EM_DASH} {item.reason}"
        for item in out.unresolved
    ]


def render(out: RunOutput, run: RunInput | None = None) -> str:
    """Render the three blocks of `PRD.md`'s Output section.

    `run` is optional and supplies only the attendee roster, so the free-count
    can read "1 of 2 optional" instead of guessing a denominator that
    `RunOutput` does not carry. Omitted, the annotation states only counts.
    """
    lines = _proposal_block(out, run)
    lines += ["", "Conflicts read from the thread:"] + _echo_block(out)
    lines += ["", "Needs confirmation:"] + _confirmation_block(out)
    return "\n".join(lines) + "\n"
