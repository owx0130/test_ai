"""The walking skeleton: test example T1 through all seven stages.

This is the narrowest thing that is still the whole product -- real prompt,
real schema, real date arithmetic, real ranking, real rendering. It runs
offline against a recorded extraction fixture, so stages 3-7 are exercised
against exactly the shape the model produces.
"""

from __future__ import annotations

from datetime import datetime

from meeting_deconflictor.cli import load_run
from meeting_deconflictor.extract import FixtureExtractor
from meeting_deconflictor.pipeline import run_pipeline
from meeting_deconflictor.render import render
from meeting_deconflictor.verify import assert_no_collisions

T1_INPUT = "tests/data/t1_input.json"
T1_FIXTURE = "tests/fixtures/t1_extraction.json"


def _run_t1():
    return run_pipeline(load_run(T1_INPUT), FixtureExtractor(T1_FIXTURE))


def test_t1_produces_the_hand_computed_slot():
    out = _run_t1()
    assert out.slots, "expected at least one proposed slot"
    top = out.slots[0]
    assert top.start == datetime(2026, 8, 17, 10, 0)
    assert top.end == datetime(2026, 8, 17, 11, 0)


def test_t1_has_exactly_one_clean_slot():
    """The gap is genuinely singular -- that is what makes T1 T1."""
    out = _run_t1()
    clean = [s for s in out.slots if s.is_clean]
    assert len(clean) == 1, [(s.start, s.end) for s in clean]


def test_t1_all_four_attendees_are_free():
    out = _run_t1()
    top = out.slots[0]
    assert set(top.free_required) == {"Wei", "Aisyah"}
    assert set(top.free_optional) == {"Ravi", "Priya"}
    assert top.broken == ()


def test_t1_needs_confirmation_is_empty():
    """PRD acceptance for T1: nothing unresolvable in five clean messages."""
    assert _run_t1().unresolved == ()


def test_t1_every_conflict_is_echoed():
    """Zero silent drops: all five statements come back in the echo block."""
    out = _run_t1()
    assert len(out.conflicts) == 5
    assert {c.speaker for c in out.conflicts} == {"Wei", "Aisyah", "Ravi", "Priya"}


def test_t1_proposals_collide_with_nothing():
    """The load-bearing guarantee. Raises rather than returns."""
    out = _run_t1()
    assert_no_collisions(list(out.slots), list(out.conflicts))


def test_t1_is_deterministic():
    """Same fixture in, byte-identical text out, three times running."""
    renders = {render(_run_t1()) for _ in range(3)}
    assert len(renders) == 1
