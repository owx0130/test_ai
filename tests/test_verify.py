"""Proof that the zero-collision guarantee is load-bearing, not decorative.

An assertion that never fires proves nothing. These tests deliberately feed
``assert_no_collisions`` a contradictory slot and require it to raise.

**F2 owns this file** and widens it.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from meeting_deconflictor.models import Interval, ResolvedConflict, Slot
from meeting_deconflictor.verify import CollisionError, assert_no_collisions

MON_10 = datetime(2026, 8, 17, 10, 0)
MON_11 = datetime(2026, 8, 17, 11, 0)


def _busy(speaker: str, start: datetime, end: datetime, hardness="hard"):
    return ResolvedConflict(
        speaker=speaker,
        intervals=(Interval(start, end),),
        hardness=hardness,
        quote=f"{speaker} is busy",
    )


def test_raises_when_a_free_person_is_actually_busy():
    """The failure this whole file exists to catch."""
    slot = Slot(start=MON_10, end=MON_11, free_required=("Wei",), free_optional=())
    conflicts = [_busy("Wei", MON_10, MON_11)]

    with pytest.raises(CollisionError, match="Wei"):
        assert_no_collisions([slot], conflicts)


def test_raises_when_someone_is_both_free_and_blocked():
    slot = Slot(
        start=MON_10,
        end=MON_11,
        free_required=("Wei",),
        free_optional=(),
        broken=(("Wei", "Wei is busy"),),
    )
    with pytest.raises(CollisionError, match="both free and blocked"):
        assert_no_collisions([slot], [])


def test_touching_intervals_do_not_collide():
    """Half-open [start, end): a 09:00-10:00 conflict does not block 10:00-11:00."""
    slot = Slot(start=MON_10, end=MON_11, free_required=("Wei",), free_optional=())
    conflicts = [_busy("Wei", datetime(2026, 8, 17, 9, 0), MON_10)]
    assert_no_collisions([slot], conflicts)  # must not raise


def test_soft_conflicts_never_collide():
    """Soft conflicts deduct from ranking; they do not exclude."""
    slot = Slot(start=MON_10, end=MON_11, free_required=("Wei",), free_optional=())
    conflicts = [_busy("Wei", MON_10, MON_11, hardness="soft")]
    assert_no_collisions([slot], conflicts)  # must not raise


def test_a_person_we_never_claimed_free_is_not_checked():
    """Someone excluded from the slot cannot contradict it."""
    slot = Slot(start=MON_10, end=MON_11, free_required=(), free_optional=())
    conflicts = [_busy("Wei", MON_10, MON_11)]
    assert_no_collisions([slot], conflicts)  # must not raise
