"""Stage 7 -- the three-block text output.

**F4 owns this file** and matches the PRD's example byte for byte. The skeleton
version below is faithful in structure and content: top slots, an echo of every
conflict read, and a needs-confirmation block.
"""

from __future__ import annotations

from meeting_deconflictor.models import Interval, ResolvedConflict, RunOutput, Slot


def _span(interval: Interval) -> str:
    return (
        f"{interval.start:%a %d %b} {interval.start:%H:%M}-{interval.end:%H:%M}"
        if interval.start.date() == interval.end.date()
        else f"{interval.start:%a %d %b %H:%M} - {interval.end:%a %d %b %H:%M}"
    )


def _slot_line(index: int, slot: Slot) -> str:
    attendees = ", ".join(slot.free_required + slot.free_optional) or "nobody"
    head = (
        f"{index}. {slot.start:%a %d %b}, {slot.start:%H:%M}-{slot.end:%H:%M}"
        f"  - {attendees}"
    )
    if slot.broken:
        broken = "; ".join(f"{who} ({quote})" for who, quote in slot.broken)
        return f"{head}\n     breaks: {broken}"
    return head


def _conflict_line(conflict: ResolvedConflict) -> str:
    spans = " . ".join(_span(i) for i in conflict.intervals)
    return f"  {conflict.speaker:<8}- {spans} ({conflict.hardness})"


def render(out: RunOutput) -> str:
    lines: list[str] = []

    if not out.slots:
        lines.append("No slot in the window fits the requested duration.")
    elif not any(slot.is_clean for slot in out.slots):
        lines.append("No slot works for every required attendee. Nearest options:")
        lines.append("")
        lines.extend(_slot_line(i, s) for i, s in enumerate(out.slots, start=1))
    else:
        lines.extend(_slot_line(i, s) for i, s in enumerate(out.slots, start=1))

    lines.append("")
    lines.append("Conflicts read from the thread:")
    if out.conflicts:
        lines.extend(_conflict_line(c) for c in out.conflicts)
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Needs confirmation:")
    if out.unresolved:
        for item in out.unresolved:
            quoted = f'"{item.quote}" - ' if item.quote else ""
            lines.append(f"  {item.speaker} - {quoted}{item.reason}")
    else:
        lines.append("  (nothing)")

    return "\n".join(lines) + "\n"
