"""F2 -- slot finder and ranking.

Tests construct ``ResolvedConflict``s directly (F2 is independent of F1; see
TEAM_PLAN.md). Fixed anchor: Monday 17 Aug 2026, a business day.

**F2 owns this file.**
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from meeting_deconflictor.models import Interval, Message, ResolvedConflict, RunInput
from meeting_deconflictor.schedule import find_slots
from meeting_deconflictor.verify import CollisionError, assert_no_collisions

MON = date(2026, 8, 17)
TUE = date(2026, 8, 18)


def _run(**kwargs) -> RunInput:
    defaults = dict(
        messages=(Message("x", "x"),),
        today=MON,
        window_business_days=1,
        duration_minutes=60,
        required=(),
        optional=(),
    )
    defaults.update(kwargs)
    return RunInput(**defaults)


def _conflict(speaker: str, start: datetime, end: datetime, hardness="hard", quote=None):
    return ResolvedConflict(
        speaker=speaker,
        intervals=(Interval(start, end),),
        hardness=hardness,
        quote=quote or f"{speaker} busy {start:%H:%M}-{end:%H:%M}",
    )


def test_grid_is_every_30_minutes_within_business_hours():
    """09:00-18:00, 30-min steps, only starts that fit a 60-min meeting."""
    run = _run(duration_minutes=60)
    slots = find_slots([], run)

    starts = sorted(s.start for s in slots)
    assert starts[0] == datetime(2026, 8, 17, 9, 0)
    assert starts[-1] == datetime(2026, 8, 17, 17, 0)  # 17:00-18:00 is the last that fits
    assert len(starts) == 17
    assert all(b - a == timedelta(minutes=30) for a, b in zip(starts, starts[1:]))


def test_slots_are_contiguous_runs_of_the_requested_duration():
    run = _run(duration_minutes=90)
    slots = find_slots([], run)
    for slot in slots:
        assert (slot.end - slot.start).total_seconds() / 60 == 90


def test_hard_conflict_excludes_from_the_cell():
    run = _run(required=("Wei",), duration_minutes=60)
    conflicts = [_conflict("Wei", datetime(2026, 8, 17, 9, 0), datetime(2026, 8, 17, 10, 0))]
    slots = {s.start: s for s in find_slots(conflicts, run)}

    overlapping = slots[datetime(2026, 8, 17, 9, 30)]
    assert "Wei" not in overlapping.free_required
    assert overlapping.broken == (("Wei", conflicts[0].quote),)

    # Half-open: a slot starting exactly when the conflict ends is untouched.
    touching = slots[datetime(2026, 8, 17, 10, 0)]
    assert "Wei" in touching.free_required
    assert touching.broken == ()


def test_soft_conflict_deducts_but_never_excludes():
    run = _run(required=("Wei",), duration_minutes=60)
    conflicts = [
        _conflict("Wei", datetime(2026, 8, 17, 9, 0), datetime(2026, 8, 17, 10, 0), hardness="soft")
    ]
    slots = {s.start: s for s in find_slots(conflicts, run)}

    touched = slots[datetime(2026, 8, 17, 9, 0)]
    assert "Wei" in touched.free_required  # soft never excludes
    assert touched.broken == ()
    assert touched.soft_broken == (("Wei", conflicts[0].quote),)

    # A later, entirely clean slot outranks the soft-brushed one, ahead of "earliest".
    # (9:00 and 9:30 both overlap the 9:00-10:00 soft conflict; 10:00 is the first clean start.)
    ranked = find_slots(conflicts, run)
    assert ranked[0].start == datetime(2026, 8, 17, 10, 0)
    assert ranked[0].soft_broken == ()


def test_ranking_prefers_all_required_free_over_more_optional_free():
    run = _run(required=("Wei",), optional=("Ravi", "Priya"), duration_minutes=60)
    conflicts = [
        # Wei is busy at 09:00 -- that slot cannot be "all required free" no matter
        # how many optionals it has free.
        _conflict("Wei", datetime(2026, 8, 17, 9, 0), datetime(2026, 8, 17, 10, 0)),
    ]
    ranked = find_slots(conflicts, run)
    top = ranked[0]
    assert top.start != datetime(2026, 8, 17, 9, 0)
    assert "Wei" in top.free_required


def test_ranking_prefers_more_optional_free_over_earliest():
    run = _run(required=("Wei",), optional=("Ravi", "Priya"), duration_minutes=60)
    conflicts = [
        # Ravi unavailable only for the earliest slot -- the later slot has both
        # optionals free and must win despite starting later.
        _conflict("Ravi", datetime(2026, 8, 17, 9, 0), datetime(2026, 8, 17, 9, 30)),
    ]
    ranked = find_slots(conflicts, run)
    top = ranked[0]
    assert set(top.free_optional) == {"Ravi", "Priya"}
    assert top.start > datetime(2026, 8, 17, 9, 0)


def test_no_slot_satisfies_both_required():
    """F2's acceptance check.

    Wei is out all day Monday; Aisyah is out all day Tuesday. No block has both
    required attendees free. The top-ranked slot must still be returned -- with
    ``broken`` naming exactly whose hard conflict it breaks -- and
    ``assert_no_collisions`` must catch it if it is ever mislabelled clean.
    """
    run = _run(
        today=MON,
        window_business_days=2,
        required=("Wei", "Aisyah"),
        duration_minutes=60,
    )
    conflicts = [
        _conflict("Wei", datetime(2026, 8, 17, 9, 0), datetime(2026, 8, 17, 18, 0), quote="Wei out Monday"),
        _conflict("Aisyah", datetime(2026, 8, 18, 9, 0), datetime(2026, 8, 18, 18, 0), quote="Aisyah out Tuesday"),
    ]

    ranked = find_slots(conflicts, run)
    top = ranked[0]

    assert not (set(("Wei", "Aisyah")) <= set(top.free_required))
    assert top.broken != ()
    broken_speakers = {speaker for speaker, _ in top.broken}
    assert broken_speakers <= {"Wei", "Aisyah"}
    # Exactly the required attendee(s) actually busy at this slot -- never both,
    # since Monday and Tuesday are each fully free for the other person.
    assert broken_speakers == ({"Wei"} if top.start.date() == MON else {"Aisyah"})

    # The real answer is honest about being broken.
    assert_no_collisions([top], conflicts)  # must not raise -- it is truthfully unclean

    # But a caller that mislabels it clean is caught.
    mislabelled = top.__class__(
        start=top.start,
        end=top.end,
        free_required=("Wei", "Aisyah"),
        free_optional=top.free_optional,
        broken=(),
    )
    with pytest.raises(CollisionError):
        assert_no_collisions([mislabelled], conflicts)
