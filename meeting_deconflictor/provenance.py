"""Stage 3 -- every conflict must trace to something a person actually wrote.

Two of the PRD's guarantees live here:

* **Zero invented conflicts.** A quote that is not a verbatim substring of that
  speaker's message is dropped and reported, never trusted.
* **Zero silent drops.** ``unparseable`` statements, and attendees who said
  nothing at all, become :class:`Unresolved` entries. An unheard person is
  never treated as a free person.

F3 owns this file and widens it. Nothing here calls a model.
"""

from __future__ import annotations

import re

from meeting_deconflictor.models import RunInput, Unresolved
from meeting_deconflictor.schema import Extraction, RawConflict

_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Collapse whitespace so a wrapped quote still matches, but keep the words."""
    return _WHITESPACE.sub(" ", text).strip()


def _said_by(run: RunInput, speaker: str) -> str:
    """Everything this speaker wrote, normalised, joined."""
    return " \n ".join(
        _normalise(m.text) for m in run.messages if m.speaker.lower() == speaker.lower()
    )


def check_provenance(
    extraction: Extraction, run: RunInput
) -> tuple[list[RawConflict], list[Unresolved]]:
    """Split the extraction into trustworthy conflicts and things to confirm."""
    kept: list[RawConflict] = []
    unresolved: list[Unresolved] = []

    for conflict in extraction.conflicts:
        haystack = _said_by(run, conflict.speaker)
        if not haystack:
            unresolved.append(
                Unresolved(
                    speaker=conflict.speaker,
                    quote=conflict.quote,
                    reason="attributed to someone who did not post in this thread",
                )
            )
            continue

        if _normalise(conflict.quote) not in haystack:
            unresolved.append(
                Unresolved(
                    speaker=conflict.speaker,
                    quote=conflict.quote,
                    reason="quote does not appear verbatim in the thread, so it was discarded",
                )
            )
            continue

        if conflict.unparseable:
            unresolved.append(
                Unresolved(
                    speaker=conflict.speaker,
                    quote=conflict.quote,
                    reason="refers to something not in this thread",
                )
            )
            continue

        kept.append(conflict)

    unresolved.extend(_silent_attendees(run, kept))
    return kept, unresolved


def _silent_attendees(run: RunInput, kept: list[RawConflict]) -> list[Unresolved]:
    """An attendee nobody heard from is unknown, not free."""
    heard = {c.speaker.lower() for c in kept}
    return [
        Unresolved(
            speaker=attendee,
            quote="",
            reason="no availability statement from this person in the thread",
        )
        for attendee in run.attendees
        if attendee.lower() not in heard
    ]
