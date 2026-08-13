"""Stage 6 -- the deconflict guarantee, proven rather than trusted.

This is the sharp end of Technique 2. It runs on every single run, and it
*raises* instead of returning a flag: an answer we cannot prove is not an
answer we ship.

It checks the invariant that actually matters -- **if we said someone is free,
they must really be free**. That catches disagreement between the free-lists
and the underlying intervals, which is the failure mode a hand-written slot
finder actually has.

**F2 owns this file.**
"""

from __future__ import annotations

from meeting_deconflictor.models import Interval, ResolvedConflict, Slot


class CollisionError(AssertionError):
    """A proposed slot overlaps a hard conflict of someone we called free."""


def assert_no_collisions(slots: list[Slot], conflicts: list[ResolvedConflict]) -> None:
    """Raise if any proposal contradicts a declared hard conflict."""
    hard = [c for c in conflicts if c.hardness == "hard"]

    for slot in slots:
        span = Interval(slot.start, slot.end)
        claimed_free = set(slot.free_required) | set(slot.free_optional)

        for conflict in hard:
            if conflict.speaker not in claimed_free:
                continue
            for interval in conflict.intervals:
                if interval.overlaps(span):
                    raise CollisionError(
                        f"proposed {slot.start:%a %d %b %H:%M}-{slot.end:%H:%M} lists "
                        f"{conflict.speaker} as free, but they declared a hard conflict "
                        f"{interval.start:%a %d %b %H:%M}-{interval.end:%H:%M} "
                        f"({conflict.quote!r})"
                    )

        # Internal consistency: nobody may be listed as both free and broken.
        contradicted = {speaker for speaker, _ in slot.broken} & claimed_free
        if contradicted:
            raise CollisionError(
                f"proposed {slot.start:%a %d %b %H:%M}-{slot.end:%H:%M} lists "
                f"{', '.join(sorted(contradicted))} as both free and blocked"
            )
