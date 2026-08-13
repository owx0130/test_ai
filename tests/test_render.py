"""F4 -- the renderer and the CLI.

Every fixture here is built by hand rather than pumped through the pipeline. That
is deliberate: F4's contract is `RunOutput` in, text out, so these tests only
break when the contract really changes (TEAM_PLAN.md merge-conflict warning 5).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path

from meeting_deconflictor.cli import main
from meeting_deconflictor.models import (
    Interval,
    Message,
    ResolvedConflict,
    RunInput,
    RunOutput,
    Slot,
    Unresolved,
)
from meeting_deconflictor.render import render

#: The ten business days of the PRD example's window: Mon 17 -- Fri 28 Aug 2026.
WINDOW = tuple(date(2026, 8, d) for d in (17, 18, 19, 20, 21, 24, 25, 26, 27, 28))


def _span(day: date, start: tuple[int, int], end: tuple[int, int]) -> Interval:
    return Interval(datetime.combine(day, time(*start)), datetime.combine(day, time(*end)))


def _prd_example() -> tuple[RunOutput, RunInput]:
    """The worked example from PRD.md, as data."""
    run = RunInput(
        messages=(
            Message("Wei", "standups every morning till 10, and I'm out Thu 20th"),
            Message("Aisyah", "client call Wed afternoon, avoid lunch hour pls"),
            Message("Ravi", "on leave till the 21st"),
            Message("Priya", "any time after the sprint review"),
        ),
        today=date(2026, 8, 17),
        window_business_days=10,
        duration_minutes=60,
        required=("Wei", "Aisyah"),
        optional=("Ravi", "Priya"),
    )
    out = RunOutput(
        slots=tuple(
            Slot(
                start=datetime(2026, 8, day, 10, 0),
                end=datetime(2026, 8, day, 11, 0),
                free_required=("Wei", "Aisyah"),
                free_optional=("Ravi",),
            )
            for day in (24, 25, 26)
        ),
        conflicts=(
            ResolvedConflict(
                "Wei",
                tuple(_span(d, (9, 0), (10, 0)) for d in WINDOW),
                "hard",
                "standups every morning till 10",
            ),
            ResolvedConflict(
                "Wei",
                (_span(date(2026, 8, 20), (9, 0), (18, 0)),),
                "hard",
                "I'm out Thu 20th",
            ),
            ResolvedConflict(
                "Aisyah",
                (_span(date(2026, 8, 19), (13, 0), (18, 0)),),
                "hard",
                "client call Wed afternoon",
            ),
            ResolvedConflict(
                "Aisyah",
                tuple(_span(d, (12, 0), (13, 0)) for d in WINDOW),
                "soft",
                "avoid lunch hour pls",
            ),
            ResolvedConflict(
                "Ravi",
                tuple(_span(d, (9, 0), (18, 0)) for d in WINDOW[:5]),
                "hard",
                "on leave till the 21st",
            ),
        ),
        unresolved=(
            Unresolved("Priya", "after the sprint review", "refers to a calendar I can't see"),
        ),
    )
    return out, run


def _prd_output_block() -> str:
    """The literal Output block from PRD.md -- the spec, read from the spec."""
    prd = Path("PRD.md").read_text(encoding="utf-8")
    match = re.search(r"\*\*Output\*\*\s*\n```\n(.*?)```", prd, re.S)
    assert match, "could not find the Output block in PRD.md"
    return match.group(1)


def _normalised(text: str) -> list[str]:
    """Lines with trailing whitespace and trailing blank lines removed."""
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def test_matches_prd_example():
    """F4's acceptance check: the PRD's worked example, rendered byte for byte."""
    out, run = _prd_example()
    assert _normalised(render(out, run)) == _normalised(_prd_output_block())


def _t3_no_clean_slot() -> tuple[RunOutput, RunInput]:
    """T3: nothing suits both required attendees, and one message is unreadable."""
    run = RunInput(
        messages=(
            Message("Wei", "I'm heads-down all Tuesday"),
            Message("Aisyah", "Tuesday morning is the only time I have"),
            Message("Priya", "any time after the sprint review"),
        ),
        today=date(2026, 8, 17),
        window_business_days=1,
        duration_minutes=60,
        required=("Wei", "Aisyah"),
        optional=("Priya",),
    )
    out = RunOutput(
        slots=(
            Slot(
                start=datetime(2026, 8, 18, 9, 0),
                end=datetime(2026, 8, 18, 10, 0),
                free_required=("Aisyah",),
                free_optional=(),
                broken=(("Wei", "I'm heads-down all Tuesday"),),
            ),
        ),
        conflicts=(
            ResolvedConflict(
                "Wei",
                (_span(date(2026, 8, 18), (9, 0), (18, 0)),),
                "hard",
                "I'm heads-down all Tuesday",
            ),
        ),
        unresolved=(
            Unresolved("Priya", "after the sprint review", "refers to a calendar I can't see"),
        ),
    )
    return out, run


def test_no_clean_slot_says_so_and_names_whose_conflict_breaks():
    """T3's requirement: state that nothing is clean, then name the cost by name."""
    out, run = _t3_no_clean_slot()
    text = render(out, run)
    assert "No slot works for every required attendee" in text
    assert 'breaks: Wei — "I\'m heads-down all Tuesday"' in text
    assert "1 of 2 required free" in text


def test_no_slots_at_all_is_stated_not_left_blank():
    """An empty proposal list is a finding, not an empty screen."""
    out, run = _t3_no_clean_slot()
    text = render(RunOutput((), out.conflicts, out.unresolved), run)
    assert text.startswith("No slot in the window fits the requested duration.")
    assert "Conflicts read from the thread:" in text


def test_unheard_person_is_reported_without_an_empty_quote():
    """F3 flags a silent attendee, who by definition has no quote to show. Zero
    silent drops means they still get a line -- just not an empty pair of quotes."""
    out, run = _prd_example()
    silent = Unresolved("Aisyah", "", "no message from this person")
    text = render(RunOutput(out.slots, out.conflicts, (silent,)), run)
    assert "  Aisyah — no message from this person" in text
    assert '""' not in text


def test_recurrence_short_of_the_window_keeps_its_range():
    """"Standups this week" is not "daily". Summarising it as "daily" would claim
    a conflict on days the speaker never mentioned -- an invented conflict."""
    out, run = _prd_example()
    week_one = ResolvedConflict(
        "Wei",
        tuple(_span(d, (9, 0), (10, 0)) for d in WINDOW[:5]),
        "hard",
        "standups every morning this week",
    )
    # The lunch conflict still reaches Fri 28, so the observed window is the full ten days.
    text = render(RunOutput(out.slots, (week_one, out.conflicts[3]), out.unresolved), run)
    assert "daily 09:00–10:00, Mon 17 Aug through Fri 21 Aug (hard)" in text


def test_weekly_recurrence_names_its_weekday():
    """F1 resolves "each Tuesday" to Tuesdays only. "daily" would be a lie and a
    date-by-date list would bury the pattern."""
    out, run = _prd_example()
    tuesdays = ResolvedConflict(
        "Wei",
        (_span(date(2026, 8, 18), (14, 0), (15, 0)), _span(date(2026, 8, 25), (14, 0), (15, 0))),
        "hard",
        "each Tuesday I have the guild meeting",
    )
    text = render(RunOutput(out.slots, (tuesdays,), out.unresolved), run)
    assert "every Tue 14:00–15:00 (hard)" in text


def test_leave_starting_mid_window_keeps_its_first_day():
    """"through Fri 28 Aug" is only honest when the leave runs from the window's
    edge. Starting later, both ends have to be shown."""
    out, run = _prd_example()
    late_leave = ResolvedConflict(
        "Ravi",
        tuple(_span(d, (9, 0), (18, 0)) for d in WINDOW[5:]),
        "hard",
        "on leave the whole of next week",
    )
    text = render(RunOutput(out.slots, (out.conflicts[0], late_leave), out.unresolved), run)
    assert "all day Mon 24 Aug through Fri 28 Aug (hard)" in text
    assert "through Fri 28 Aug (hard)" in text  # and never the bare form


def test_soft_conflict_a_proposal_brushes_is_surfaced():
    """A soft conflict only deducts, so the slot stays proposable -- but hiding
    that it brushes someone's lunch would be a silent drop."""
    out, run = _prd_example()
    brushing = Slot(
        start=datetime(2026, 8, 24, 12, 0),
        end=datetime(2026, 8, 24, 13, 0),
        free_required=("Wei", "Aisyah"),
        free_optional=("Ravi",),
        soft_broken=(("Aisyah", "avoid lunch hour pls"),),
    )
    text = render(RunOutput((brushing,), out.conflicts, out.unresolved), run)
    assert 'brushes: Aisyah — "avoid lunch hour pls"' in text


def test_interval_spanning_two_days_shows_both_ends():
    """Printing only the start date would understate the conflict."""
    out, run = _prd_example()
    overnight = ResolvedConflict(
        "Ravi",
        (Interval(datetime(2026, 8, 24, 9, 0), datetime(2026, 8, 25, 18, 0)),),
        "hard",
        "away Monday and Tuesday",
    )
    text = render(RunOutput(out.slots, (overnight,), out.unresolved), run)
    assert "Mon 24 Aug 09:00 – Tue 25 Aug 18:00 (hard)" in text


def test_renders_from_run_output_alone_without_inventing_a_roster():
    """`render(out)` is the frozen contract, and `RunOutput` records who is free,
    never who was asked. Without a roster there is no denominator to state."""
    out, _ = _prd_example()
    first = render(out).split("\n")[0]
    assert "of 2 optional" not in first
    assert "2 required free, 1 optional free" in first


def test_cli_renders_t1_with_the_roster_counts(capsys):
    """The CLI has the RunInput in hand, so its free-count should be the exact
    form -- "2 of 2 optional", not a bare tally."""
    assert main(["tests/data/t1_input.json"]) == 0
    text = capsys.readouterr().out
    assert "1. Mon 17 Aug, 10:00–11:00" in text
    assert "(both required free, 2 of 2 optional)" in text
    assert "Needs confirmation:\n  (nothing)" in text


def test_broken_slots_are_not_offered_beside_a_clean_one():
    """`find_slots` ranks every candidate, so the top 3 can include slots that
    break a hard conflict. Acceptance criterion 1 says no *proposed* slot may
    overlap one, so a broken slot is only ever shown when nothing is clean."""
    out, run = _prd_example()
    clean = out.slots[0]
    broken = Slot(
        start=datetime(2026, 8, 24, 9, 0),
        end=datetime(2026, 8, 24, 10, 0),
        free_required=("Aisyah",),
        free_optional=("Ravi",),
        broken=(("Wei", "standups every morning till 10"),),
    )
    text = render(RunOutput((clean, broken), out.conflicts, out.unresolved), run)
    assert "breaks:" not in text
    assert "2. " not in text
    assert "1. Mon 24 Aug, 10:00–11:00" in text
