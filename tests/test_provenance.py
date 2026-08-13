"""Stage 3: zero invented conflicts, zero silent drops.

These two guarantees are the ones an LLM cannot be trusted with, so they are
tested adversarially. Every test here either hands ``check_provenance`` an
extraction that a plausible-but-wrong model would produce, or an attendee the
system could quietly assume is free.

The two acceptance checks named in TEAM_PLAN.md's F3 row are
``test_invented_conflict_is_dropped_and_reported`` and
``test_silent_person_is_not_free``.

**F3 owns this file** and widens it. Nothing here calls a model.
"""

from __future__ import annotations

from datetime import date

from meeting_deconflictor.cli import load_run
from meeting_deconflictor.extract import FixtureExtractor, extract
from meeting_deconflictor.models import Message, RunInput
from meeting_deconflictor.provenance import check_provenance
from meeting_deconflictor.schema import Extraction, RawConflict

T1_INPUT = "tests/data/t1_input.json"
T1_FIXTURE = "tests/fixtures/t1_extraction.json"

TODAY = date(2026, 8, 17)


def _run(*messages: tuple[str, str], required=("Wei",), optional=()) -> RunInput:
    return RunInput(
        messages=tuple(Message(speaker=s, text=t) for s, t in messages),
        today=TODAY,
        required=required,
        optional=optional,
    )


def _conflict(speaker: str, quote: str, *, unparseable: bool = False, **kw) -> RawConflict:
    return RawConflict(
        speaker=speaker,
        polarity=kw.get("polarity", "busy"),
        day_reference=kw.get("day_reference", "Monday"),
        time_start=kw.get("time_start", "09:00"),
        time_end=kw.get("time_end", "10:00"),
        hardness=kw.get("hardness", "hard"),
        quote=quote,
        unparseable=unparseable,
    )


def _extraction(*conflicts: RawConflict) -> Extraction:
    return Extraction(conflicts=list(conflicts))


# --------------------------------------------------------------------------
# Zero invented conflicts
# --------------------------------------------------------------------------


def test_verbatim_quote_survives():
    run = _run(("Wei", "I'm booked Monday 9am to 10am"))
    kept, unresolved = check_provenance(
        _extraction(_conflict("Wei", "booked Monday 9am to 10am")), run
    )

    assert [c.quote for c in kept] == ["booked Monday 9am to 10am"]
    assert unresolved == []


def test_invented_conflict_is_dropped_and_reported():
    """F3 acceptance check. A quote nobody wrote yields no conflict and one flag."""
    run = _run(("Wei", "I'm booked Monday 9am to 10am"))
    kept, unresolved = check_provenance(
        _extraction(_conflict("Wei", "I'm on leave all next week")), run
    )

    assert kept == []
    assert len(unresolved) == 1
    assert unresolved[0].speaker == "Wei"
    assert unresolved[0].quote == "I'm on leave all next week"
    assert "verbatim" in unresolved[0].reason


def test_quote_wrapped_across_lines_still_matches():
    """Whitespace is normalised: a re-wrapped quote is still the same words."""
    run = _run(("Wei", "I'm booked Monday\n   9am to 10am"))
    kept, _ = check_provenance(_extraction(_conflict("Wei", "booked Monday 9am to 10am")), run)

    assert len(kept) == 1


def test_quote_borrowed_from_another_speaker_is_dropped():
    """Provenance is per speaker: Wei cannot be assigned Aisyah's words."""
    run = _run(
        ("Wei", "I'm booked Monday 9am to 10am"),
        ("Aisyah", "I have a client call Monday 11am to 6pm"),
        required=("Wei", "Aisyah"),
    )
    kept, unresolved = check_provenance(
        _extraction(_conflict("Wei", "client call Monday 11am to 6pm")), run
    )

    assert kept == []
    assert [u.speaker for u in unresolved if "verbatim" in u.reason] == ["Wei"]


def test_quote_spanning_two_messages_is_dropped():
    """Stitching two separate messages into one quote is an invention."""
    run = _run(
        ("Wei", "I'm booked Monday 9am to 10am"),
        ("Wei", "and I'm in a workshop Monday 11am to 6pm"),
    )
    stitched = "I'm booked Monday 9am to 10am and I'm in a workshop Monday 11am to 6pm"
    kept, unresolved = check_provenance(_extraction(_conflict("Wei", stitched)), run)

    assert kept == []
    assert len(unresolved) == 1


def test_case_changed_quote_is_dropped():
    """Verbatim is case-sensitive on content -- a rephrase is not a quote."""
    run = _run(("Wei", "I'm booked Monday 9am to 10am"))
    kept, unresolved = check_provenance(
        _extraction(_conflict("Wei", "BOOKED MONDAY 9AM TO 10AM")), run
    )

    assert kept == []
    assert len(unresolved) == 1


def test_empty_quote_is_dropped_and_reported():
    """An empty string is a substring of everything, so it must be special-cased."""
    run = _run(("Wei", "I'm booked Monday 9am to 10am"))
    kept, unresolved = check_provenance(_extraction(_conflict("Wei", "   ")), run)

    assert kept == []
    assert len(unresolved) == 1
    assert "no quote" in unresolved[0].reason


def test_typographic_apostrophe_does_not_break_provenance():
    """A curly apostrophe is the same words re-rendered, not a different claim."""
    run = _run(("Wei", "I'm booked Monday 9am to 10am"))
    kept, _ = check_provenance(_extraction(_conflict("Wei", "I’m booked Monday")), run)

    assert len(kept) == 1


def test_speaker_name_case_does_not_break_matching():
    run = _run(("Wei", "I'm booked Monday 9am to 10am"))
    kept, unresolved = check_provenance(
        _extraction(_conflict("wei", "booked Monday 9am to 10am")), run
    )

    assert len(kept) == 1
    assert unresolved == []


def test_conflict_attributed_to_someone_who_never_posted_is_dropped():
    run = _run(("Wei", "I'm booked Monday 9am to 10am"))
    kept, unresolved = check_provenance(
        _extraction(_conflict("Bilal", "Bilal is away all week")), run
    )

    assert kept == []
    assert any(u.speaker == "Bilal" and "did not post" in u.reason for u in unresolved)


def test_speaker_outside_the_attendee_lists_is_still_kept():
    """Someone who posted but was not invited still constrains nothing -- but
    dropping their statement silently would hide it from the echo block."""
    run = _run(
        ("Wei", "I'm booked Monday 9am to 10am"),
        ("Bilal", "I'm out Monday afternoon"),
        required=("Wei",),
    )
    kept, unresolved = check_provenance(
        _extraction(
            _conflict("Wei", "booked Monday 9am to 10am"),
            _conflict("Bilal", "out Monday afternoon"),
        ),
        run,
    )

    assert {c.speaker for c in kept} == {"Wei", "Bilal"}
    assert unresolved == []


def test_kept_conflicts_are_returned_unchanged():
    """Stage 3 filters; it never reinterprets polarity, hardness or day reference."""
    run = _run(("Aisyah", "avoid lunch hour pls"), required=("Aisyah",))
    original = _conflict(
        "Aisyah",
        "avoid lunch hour pls",
        polarity="busy",
        day_reference="every day",
        time_start="12:00",
        time_end="13:00",
        hardness="soft",
    )
    kept, _ = check_provenance(_extraction(original), run)

    assert kept == [original]


# --------------------------------------------------------------------------
# Zero silent drops
# --------------------------------------------------------------------------


def test_silent_person_is_not_free():
    """F3 acceptance check. A required attendee who said nothing is unknown."""
    run = _run(("Wei", "I'm booked Monday 9am to 10am"), required=("Wei", "Aisyah"))
    kept, unresolved = check_provenance(
        _extraction(_conflict("Wei", "booked Monday 9am to 10am")), run
    )

    assert len(kept) == 1
    assert [u.speaker for u in unresolved] == ["Aisyah"]
    assert "no message" in unresolved[0].reason


def test_silent_optional_attendee_is_also_flagged():
    run = _run(("Wei", "I'm booked Monday 9am to 10am"), optional=("Ravi",))
    _, unresolved = check_provenance(
        _extraction(_conflict("Wei", "booked Monday 9am to 10am")), run
    )

    assert [u.speaker for u in unresolved] == ["Ravi"]


def test_unparseable_becomes_unresolved_and_is_not_kept():
    run = _run(("Priya", "any time after the sprint review"), required=("Priya",))
    kept, unresolved = check_provenance(
        _extraction(
            _conflict("Priya", "after the sprint review", unparseable=True)
        ),
        run,
    )

    assert kept == []
    assert len(unresolved) == 1
    assert unresolved[0].speaker == "Priya"
    assert unresolved[0].quote == "after the sprint review"


def test_unparseable_person_is_reported_exactly_once():
    """Priya in the PRD example gets one needs-confirmation line, not two.

    She is not silent -- she posted -- so the silent-attendee sweep must not
    add a second, contradictory entry saying she never spoke.
    """
    run = _run(("Priya", "any time after the sprint review"), required=("Priya",))
    _, unresolved = check_provenance(
        _extraction(
            _conflict("Priya", "after the sprint review", unparseable=True)
        ),
        run,
    )

    assert [u.speaker for u in unresolved] == ["Priya"]


def test_attendee_who_posted_but_had_nothing_extracted_is_flagged():
    """The model dropping a whole message is a silent drop unless we catch it."""
    run = _run(
        ("Wei", "I'm booked Monday 9am to 10am"),
        ("Aisyah", "hmm, my week is a mess"),
        required=("Wei", "Aisyah"),
    )
    kept, unresolved = check_provenance(
        _extraction(_conflict("Wei", "booked Monday 9am to 10am")), run
    )

    assert len(kept) == 1
    assert [u.speaker for u in unresolved] == ["Aisyah"]
    assert "no availability statement" in unresolved[0].reason


def test_an_attendee_with_a_dropped_quote_is_not_also_called_silent():
    """One flag per person per reason. A dropped quote already surfaces them."""
    run = _run(("Wei", "I'm booked Monday 9am to 10am"))
    _, unresolved = check_provenance(
        _extraction(_conflict("Wei", "I'm on leave all next week")), run
    )

    assert [u.speaker for u in unresolved] == ["Wei"]


def test_nothing_extracted_at_all_flags_every_attendee():
    run = _run(
        ("Wei", "I'm booked Monday 9am to 10am"),
        required=("Wei", "Aisyah"),
        optional=("Ravi",),
    )
    kept, unresolved = check_provenance(_extraction(), run)

    assert kept == []
    assert [u.speaker for u in unresolved] == ["Wei", "Aisyah", "Ravi"]


def test_an_attendee_listed_twice_is_flagged_once():
    """``required + optional`` can name the same person; the flag must not double."""
    run = _run(("Zoe", "nothing on"), required=("Aisyah",), optional=("Aisyah",))
    _, unresolved = check_provenance(_extraction(), run)

    assert [u.speaker for u in unresolved] == ["Aisyah"]


def test_silent_attendees_follow_the_declared_order():
    """Deterministic output: required in order, then optional in order."""
    run = _run(("Zoe", "nothing on"), required=("Wei", "Aisyah"), optional=("Ravi", "Priya"))
    _, unresolved = check_provenance(_extraction(), run)

    assert [u.speaker for u in unresolved] == ["Wei", "Aisyah", "Ravi", "Priya"]


# --------------------------------------------------------------------------
# Against the real recorded extraction
# --------------------------------------------------------------------------


def test_t1_recorded_extraction_passes_provenance_intact():
    """The live model's five quotes are all genuinely verbatim."""
    run = load_run(T1_INPUT)
    extraction = extract(run, FixtureExtractor(T1_FIXTURE))
    kept, unresolved = check_provenance(extraction, run)

    assert len(kept) == 5
    assert unresolved == []


def test_every_kept_quote_appears_in_the_input_text():
    """The guarantee stated as a property rather than a case."""
    run = load_run(T1_INPUT)
    kept, _ = check_provenance(extract(run, FixtureExtractor(T1_FIXTURE)), run)

    for conflict in kept:
        assert any(conflict.quote in m.text for m in run.messages), conflict.quote


def test_check_provenance_is_deterministic():
    run = load_run(T1_INPUT)
    extraction = extract(run, FixtureExtractor(T1_FIXTURE))
    first = check_provenance(extraction, run)
    second = check_provenance(extraction, run)

    assert first == second
