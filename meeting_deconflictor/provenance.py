"""Stage 3 -- every conflict must trace to something a person actually wrote.

Two of the PRD's guarantees live here, and both of them are guarantees *about
the model*, so neither may be delegated to it:

* **Zero invented conflicts.** A quote that is not a verbatim substring of one
  of that speaker's own messages is dropped, logged, and reported. It never
  reaches date resolution, so it can never appear in a proposal.
* **Zero silent drops.** ``unparseable`` statements, statements whose quote we
  threw away, and attendees the model never spoke for all become
  :class:`Unresolved` entries. An unheard person is never treated as a free
  person -- absence of a conflict is not evidence of availability.

Matching rules, chosen so that "verbatim" is strict about *content* and lenient
only about how the text was re-rendered:

* whitespace is collapsed, so a re-wrapped quote still matches;
* curly quotes, dashes and ellipses are folded to their ASCII forms on *both*
  sides, so typography cannot fabricate a mismatch -- and, because the fold is
  symmetric, it cannot fabricate a match between different words either;
* case is significant. A rephrase in different case is not a quote.
* the search is per message, not against the speaker's messages concatenated,
  so two separate statements cannot be stitched into one quote.

Order matters in :func:`check_provenance`: the quote is checked *before*
``unparseable`` is honoured. An unparseable statement carrying an untraceable
quote is reported as untraceable, because printing that quote in the
needs-confirmation block would itself be an invented conflict.

F3 owns this file and widens it. Nothing here calls a model.
"""

from __future__ import annotations

import logging
import re

from meeting_deconflictor.models import Message, RunInput, Unresolved
from meeting_deconflictor.schema import Extraction, RawConflict

log = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")

#: Typographic variants folded to ASCII on both sides of the comparison.
_TYPOGRAPHY = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
    }
)

#: The reasons shown to the human. Kept as constants because the renderer (F4)
#: prints them verbatim and the eval (F6) matches abstentions against them.
REASON_NO_QUOTE = "no quote was given to trace this back to, so it was discarded"
REASON_NOT_VERBATIM = (
    "quote does not appear verbatim in this person's messages, so it was discarded"
)
REASON_NEVER_POSTED = "attributed to someone who did not post in this thread"
REASON_UNPARSEABLE = "refers to something this thread does not contain"
REASON_NO_MESSAGE = "no message from this person in the thread"
REASON_NOTHING_READ = "posted in the thread, but no availability statement was read from it"


def _canonical(text: str) -> str:
    """Fold whitespace and typography; leave words and case alone."""
    return _WHITESPACE.sub(" ", text.translate(_TYPOGRAPHY)).strip()


def _key(speaker: str) -> str:
    """Speaker identity for matching. Names are matched loosely, content is not."""
    return speaker.strip().casefold()


def _messages_from(run: RunInput, speaker: str) -> list[Message]:
    return [m for m in run.messages if _key(m.speaker) == _key(speaker)]


def _is_verbatim(quote: str, messages: list[Message]) -> bool:
    """True when the quote sits inside a single one of these messages."""
    needle = _canonical(quote)
    return any(needle in _canonical(m.text) for m in messages)


def check_provenance(
    extraction: Extraction, run: RunInput
) -> tuple[list[RawConflict], list[Unresolved]]:
    """Split the extraction into trustworthy conflicts and things to confirm.

    Returns ``(kept, unresolved)``. ``kept`` is the subset of
    ``extraction.conflicts`` that survived, unchanged -- this stage filters and
    never reinterprets polarity, hardness or the day reference. ``unresolved``
    is everything the human has to look at, in a deterministic order:
    extraction order first, then unheard attendees in declared order.
    """
    kept: list[RawConflict] = []
    unresolved: list[Unresolved] = []

    for conflict in extraction.conflicts:
        flagged = _screen(conflict, run)
        if flagged is not None:
            unresolved.append(flagged)
            continue
        kept.append(conflict)

    unresolved.extend(_unheard_attendees(run, kept, unresolved))
    return kept, unresolved


def _screen(conflict: RawConflict, run: RunInput) -> Unresolved | None:
    """Return an :class:`Unresolved` if this conflict cannot be trusted, else None."""
    messages = _messages_from(run, conflict.speaker)

    if not _canonical(conflict.quote):
        return _drop(conflict, REASON_NO_QUOTE)

    if not messages:
        return _drop(conflict, REASON_NEVER_POSTED)

    if not _is_verbatim(conflict.quote, messages):
        return _drop(conflict, REASON_NOT_VERBATIM)

    if conflict.unparseable:
        # Not a failure -- the model abstained, which is the correct move for a
        # reference to a calendar we cannot see. Scored as correct by F6.
        return Unresolved(
            speaker=conflict.speaker, quote=conflict.quote, reason=REASON_UNPARSEABLE
        )

    return None


def _drop(conflict: RawConflict, reason: str) -> Unresolved:
    """Discard a conflict, loudly. Dropping silently would hide a model failure."""
    log.warning(
        "dropped a conflict attributed to %s: %s (quote=%r)",
        conflict.speaker,
        reason,
        conflict.quote,
    )
    return Unresolved(speaker=conflict.speaker, quote=conflict.quote, reason=reason)


def _unheard_attendees(
    run: RunInput, kept: list[RawConflict], unresolved: list[Unresolved]
) -> list[Unresolved]:
    """An attendee nobody heard from is unknown, not free.

    Someone already present in ``kept`` or ``unresolved`` is accounted for and
    is not flagged twice -- a person whose only statement was unparseable has
    been surfaced already, and a second entry claiming they never spoke would
    contradict the first. When an attendee produced no conflict at all but did
    post, every one of their messages is quoted, so the human sees exactly what
    the model failed to read.
    """
    accounted = {_key(c.speaker) for c in kept} | {_key(u.speaker) for u in unresolved}
    flags: list[Unresolved] = []

    for attendee in run.attendees:
        if _key(attendee) in accounted:
            continue
        # required + optional may name the same person; flag them once.
        accounted.add(_key(attendee))

        messages = _messages_from(run, attendee)
        if not messages:
            flags.append(Unresolved(speaker=attendee, quote="", reason=REASON_NO_MESSAGE))
            continue

        flags.extend(
            Unresolved(speaker=attendee, quote=_canonical(m.text), reason=REASON_NOTHING_READ)
            for m in messages
        )

    return flags
