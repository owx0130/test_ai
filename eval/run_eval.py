"""Technique 3 -- the golden-set eval, where abstention is scored as correct.

    uv run python eval/run_eval.py --report

Four gated metrics, plus the PRD's top-slot check on the dated test logs:

1. **Extraction accuracy** -- speaker + polarity + hardness, exact match, >= 90%.
2. **Resolution accuracy** -- resolved dates + time range, exact match, >= 85%.
3. **Abstention** -- a calendar-relative message must be *flagged*. Flagging
   scores 1, guessing a date scores 0. Must be 100%. Reported alongside it:
   out-of-window and weekend dates must be dropped *and said out loud*, because
   a silent drop is the same failure wearing a different hat.
4. **Zero collisions** -- ``assert_no_collisions`` over every slot the finder
   produces, not just the three we would show, on the golden run and on T1-T3.

Exits non-zero if any of those misses its bar, so this is usable as a gate.

**Offline by default.** The extraction is replayed from ``eval/fixtures/`` so the
scorer needs no key and no network. ``--live`` records a fresh extraction from
the gateway first (exactly one call) and then scores that. A fixture that is
still ``hand-authored`` makes metric 1 circular -- it is then measuring the
transcription, not the model -- so every run prints the provenance of what it
scored, and ``--require-recorded`` turns a hand-authored fixture into a failure.

**F6 owns this file.**
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from meeting_deconflictor.cli import load_run
from meeting_deconflictor.extract import (
    Extractor,
    FixtureExtractor,
    LiveExtractor,
    _unfence,
    build_prompts,
    extract,
    prompt_fingerprint,
)
from meeting_deconflictor.models import Message, ResolvedConflict, RunInput, RunOutput
from meeting_deconflictor.pipeline import run_pipeline
from meeting_deconflictor.schedule import find_slots
from meeting_deconflictor.schema import Extraction
from meeting_deconflictor.verify import CollisionError, assert_no_collisions

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "golden.jsonl"
GOLDEN_RUN = ROOT / "eval" / "golden_run.json"
GOLDEN_FIXTURE = ROOT / "eval" / "fixtures" / "golden_extraction.json"
BASELINE = ROOT / "eval" / "baseline.json"

#: The dated test logs from the PRD. T1's top slot is asserted in
#: tests/test_end_to_end.py; here all three are collision-checked, and the two
#: that declare ``expected_top_slot`` have it verified (PRD acceptance 5).
TEST_LOGS = ("t1", "t2", "t3")

BAR_EXTRACTION = 0.90
BAR_RESOLUTION = 0.85
BAR_ABSTENTION = 1.00

_WHITESPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().lower()


# ---------------------------------------------------------------------------
# The golden set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenRow:
    """One hand-labelled message and what the system must do with it."""

    id: str
    speaker: str
    text: str
    expect: dict

    @property
    def outcome(self) -> str:
        return self.expect["outcome"]


def load_golden() -> tuple[list[GoldenRow], RunInput]:
    rows = [
        GoldenRow(
            id=obj["id"], speaker=obj["speaker"], text=obj["text"], expect=obj["expect"]
        )
        for obj in (
            json.loads(line)
            for line in GOLDEN.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    ]
    config = json.loads(GOLDEN_RUN.read_text(encoding="utf-8"))
    run = RunInput(
        messages=tuple(Message(speaker=r.speaker, text=r.text) for r in rows),
        today=date.fromisoformat(config["today"]),
        window_business_days=config["window_business_days"],
        duration_minutes=config["duration_minutes"],
        required=tuple(config["required"]),
        optional=tuple(config["optional"]),
    )
    return rows, run


# ---------------------------------------------------------------------------
# Matching an extracted / resolved item back to the row it came from
# ---------------------------------------------------------------------------


def _quote_belongs_to(quote: str, row: GoldenRow, speaker: str) -> bool:
    """Every golden message holds exactly one statement, so a verbatim quote
    from that speaker that appears inside its text identifies it uniquely."""
    return speaker.lower() == row.speaker.lower() and _norm(quote) in _norm(row.text)


def _extracted_for(row: GoldenRow, extraction: Extraction):
    for conflict in extraction.conflicts:
        if _quote_belongs_to(conflict.quote, row, conflict.speaker):
            return conflict
    return None


def _resolved_for(row: GoldenRow, out: RunOutput) -> ResolvedConflict | None:
    for conflict in out.conflicts:
        if _quote_belongs_to(conflict.quote, row, conflict.speaker):
            return conflict
    return None


def _unresolved_for(row: GoldenRow, out: RunOutput):
    for item in out.unresolved:
        if item.quote and _quote_belongs_to(item.quote, row, item.speaker):
            return item
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class Score:
    """A hit/total tally that remembers what went wrong."""

    name: str
    hits: int = 0
    total: int = 0
    misses: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.misses = []

    def add(self, ok: bool, detail: str) -> None:
        self.total += 1
        if ok:
            self.hits += 1
        else:
            self.misses.append(detail)

    @property
    def rate(self) -> float:
        return self.hits / self.total if self.total else 1.0

    def line(self, bar: float) -> str:
        verdict = "PASS" if self.rate >= bar else "FAIL"
        return (
            f"  {self.name:<42} {self.hits:>2}/{self.total:<2} "
            f"{self.rate:>6.1%}  (bar {bar:.0%})  {verdict}"
        )


def score_extraction(rows: list[GoldenRow], extraction: Extraction) -> Score:
    """Speaker, polarity and hardness -- what the model is solely responsible for."""
    score = Score("Extraction (speaker/polarity/hardness)")
    for row in rows:
        found = _extracted_for(row, extraction)
        if found is None:
            score.add(False, f"{row.id}: no extracted statement matched this message")
            continue
        wrong = [
            f"{field}={getattr(found, field)!r} want {row.expect[field]!r}"
            for field in ("polarity", "hardness")
            if getattr(found, field) != row.expect[field]
        ]
        if found.speaker.lower() != row.speaker.lower():
            wrong.append(f"speaker={found.speaker!r} want {row.speaker!r}")
        score.add(not wrong, f"{row.id}: " + "; ".join(wrong))
    return score


def score_resolution(rows: list[GoldenRow], out: RunOutput) -> Score:
    """Resolved dates and time range -- what deterministic code is responsible for."""
    score = Score("Resolution (dates + time range)")
    for row in rows:
        if row.outcome != "resolved":
            continue
        found = _resolved_for(row, out)
        if found is None:
            score.add(False, f"{row.id}: expected a resolved conflict, got none")
            continue

        got_dates = sorted({i.start.date().isoformat() for i in found.intervals})
        want_dates = sorted(row.expect["dates"])
        got_times = sorted({(f"{i.start:%H:%M}", f"{i.end:%H:%M}") for i in found.intervals})
        want_times = [(row.expect["time_start"], row.expect["time_end"])]

        problems = []
        if got_dates != want_dates:
            problems.append(
                f"dates {_summarise(got_dates)} want {_summarise(want_dates)}"
            )
        if got_times != want_times:
            problems.append(f"times {got_times} want {want_times}")
        score.add(not problems, f"{row.id}: " + "; ".join(problems))
    return score


def _summarise(dates: list[str]) -> str:
    """Long date lists are noise in a failure message; show the shape."""
    if len(dates) <= 3:
        return "[" + ", ".join(dates) + "]"
    return f"[{dates[0]} .. {dates[-1]}, {len(dates)} dates]"


def score_abstention(rows: list[GoldenRow], out: RunOutput) -> tuple[Score, Score, Score]:
    """Three ways of not inventing an answer, scored separately then gated together."""
    abstained = Score("Abstention (calendar-relative, flagged not guessed)")
    dropped = Score("Reported drops (weekend / out of window)")
    free = Score("Free statements add no invented busy time")

    for row in rows:
        if row.outcome == "abstain":
            guessed = _resolved_for(row, out) is not None
            flagged = _unresolved_for(row, out) is not None
            detail = (
                f"{row.id}: guessed a date instead of flagging"
                if guessed
                else f"{row.id}: neither flagged nor resolved -- silently dropped"
            )
            abstained.add(flagged and not guessed, detail)

        elif row.outcome == "dropped":
            item = _unresolved_for(row, out)
            if item is None:
                dropped.add(False, f"{row.id}: dropped without saying so")
                continue
            wanted = row.expect.get("reason_contains", "")
            ok = wanted.lower() in item.reason.lower()
            dropped.add(ok, f"{row.id}: reason {item.reason!r} lacks {wanted!r}")

        elif row.outcome == "free":
            invented = _resolved_for(row, out)
            free.add(
                invented is None,
                f"{row.id}: a 'free' statement became a busy interval",
            )

    return abstained, dropped, free


# ---------------------------------------------------------------------------
# Collisions -- the load-bearing guarantee, over every slot, not just the top 3
# ---------------------------------------------------------------------------


def check_collisions(label: str, out: RunOutput, run: RunInput) -> str | None:
    """``None`` when clean, else the failure text."""
    every_slot = find_slots(list(out.conflicts), run)
    try:
        assert_no_collisions(every_slot, list(out.conflicts))
    except CollisionError as exc:
        return f"{label}: {exc}"
    return None


# ---------------------------------------------------------------------------
# Baseline -- what "no regression" is measured against
# ---------------------------------------------------------------------------
#
# The PRD's bars are percentages over a 15-row set, so they are coarse: at 90%
# over 15 rows, one newly-wrong row still passes. That is fine as an absolute
# floor and useless as a regression detector, which is exactly what F5's prompt
# tuning needs. So the scored rates are also compared against the last recorded
# baseline, and *any* drop is a failure however far above the bar it lands.


def _fixture_path(name: str) -> Path:
    if name == "golden":
        return GOLDEN_FIXTURE
    return ROOT / "tests" / "fixtures" / f"{name}_extraction.json"


def stale_fixture_notes(sources: dict[str, str]) -> list[str]:
    """Flag recorded fixtures whose prompt has moved on since they were taken.

    A hand-authored fixture is already flagged as such; this is about the
    subtler case, a genuinely recorded one that no longer reflects what the
    current prompt would produce.
    """
    current = prompt_fingerprint()
    notes: list[str] = []
    for name, source in sources.items():
        if source != "recorded":
            continue
        recorded = FixtureExtractor(_fixture_path(name)).recorded_fingerprint()
        if recorded is None:
            notes.append(
                f"{name} was recorded before prompts were fingerprinted, so it may "
                f"predate the current prompt ({current}). Re-record to be sure."
            )
        elif recorded != current:
            notes.append(
                f"{name} was recorded under prompt {recorded}, but the prompt is now "
                f"{current}. It no longer shows what this prompt produces."
            )
    return notes


def read_baseline() -> dict | None:
    if not BASELINE.exists():
        return None
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def write_baseline(rates: dict[str, float], provenance: dict[str, str]) -> None:
    payload = {
        "_what": "Scores from the last accepted run. Any metric below these is a "
                 "regression, even when it still clears the PRD's absolute bar.",
        "_how": "Refresh deliberately with --save-baseline once a change is "
                "reviewed and accepted; never to make a red run go green.",
        "_scored_fixtures": provenance,
        "rates": rates,
    }
    BASELINE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def compare_to_baseline(
    rates: dict[str, float], provenance: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Returns (failures, notes). A drop is a failure; a rise is worth saying."""
    baseline = read_baseline()
    if baseline is None:
        return [], ["no baseline recorded yet -- run with --save-baseline to set one"]

    was = baseline.get("_scored_fixtures", {})
    notes: list[str] = []
    if was and was != provenance:
        notes.append(
            f"baseline was recorded against {was}, this run scored {provenance} "
            "-- the comparison is only meaningful once both are the same kind"
        )

    failures: list[str] = []
    for name, rate in rates.items():
        before = baseline.get("rates", {}).get(name)
        if before is None:
            notes.append(f"{name}: no baseline entry, nothing to compare")
        elif rate < before - 1e-9:
            failures.append(
                f"REGRESSION {name}: {rate:.1%} < previous {before:.1%}"
            )
        elif rate > before + 1e-9:
            notes.append(f"{name} improved: {before:.1%} -> {rate:.1%}")
    return failures, notes


# ---------------------------------------------------------------------------
# Live recording -- one call, written to a fixture, then replayed
# ---------------------------------------------------------------------------


def record(run: RunInput, fixture_path: Path, source_label: str) -> float:
    """Call the gateway once and write the fixture. Returns elapsed seconds.

    Recording then replaying is what keeps the run to exactly one LLM call
    even though the scorer needs both the raw extraction and the pipeline's
    output.
    """
    backend = LiveExtractor()
    system, user = build_prompts(run)
    started = time.monotonic()
    raw = backend(system, user)
    elapsed = time.monotonic() - started

    extraction = Extraction.model_validate_json(_unfence(raw))
    mode = backend.used_response_format
    payload = {
        "_source": "recorded",
        "_model": backend.model,
        "_base_url": backend.base_url,
        "_response_format": (mode or {}).get("type", "none (unconstrained)"),
        "_input": source_label,
        "_elapsed_seconds": round(elapsed, 2),
        "_messages": len(run.messages),
        "_prompt_sha256": prompt_fingerprint(),
        **extraction.model_dump(),
    }
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return elapsed


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the golden set.")
    parser.add_argument(
        "--report", action="store_true", help="print the scored table (default)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="record a fresh extraction from the gateway (one call) and score that",
    )
    parser.add_argument(
        "--require-recorded",
        action="store_true",
        help="fail if any fixture scored is hand-authored rather than model output",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="record this run's scores as the bar future runs must not fall below",
    )
    args = parser.parse_args(argv)

    rows, run = load_golden()
    print(f"Golden set: {len(rows)} messages, today {run.today:%a %d %b %Y}, "
          f"window {run.window_business_days} business days")

    elapsed = None
    if args.live:
        print("Recording a fresh extraction from the gateway (one call)...")
        elapsed = record(run, GOLDEN_FIXTURE, "eval/golden.jsonl")
        print(f"  recorded in {elapsed:.1f}s")

    backend: Extractor = FixtureExtractor(GOLDEN_FIXTURE)
    extraction = extract(run, backend)
    out = run_pipeline(run, backend)

    # ---- provenance of what we just scored --------------------------------
    sources = {"golden": FixtureExtractor(GOLDEN_FIXTURE).source()}
    for name in TEST_LOGS:
        sources[name] = FixtureExtractor(ROOT / "tests" / "fixtures" / f"{name}_extraction.json").source()
    hand = sorted(k for k, v in sources.items() if v != "recorded")

    print("\nFixture provenance: " + ", ".join(f"{k}={v}" for k, v in sources.items()))
    if hand:
        print(
            "  ! " + ", ".join(hand) + " are hand-authored, not model output.\n"
            "    The extraction metric below therefore scores a transcription against\n"
            "    its own labels and proves nothing about the model. The resolution,\n"
            "    abstention and collision metrics still measure real code.\n"
            "    Re-record with scripts/record_fixture.py once OPENAI_KEY exists."
        )
    for note in stale_fixture_notes(sources):
        print("  ! " + note)

    # ---- metrics ----------------------------------------------------------
    extraction_score = score_extraction(rows, extraction)
    resolution_score = score_resolution(rows, out)
    abstained, dropped, free = score_abstention(rows, out)

    collisions = [c for c in [check_collisions("golden", out, run)] if c]
    top_slot_problems: list[str] = []
    for name in TEST_LOGS:
        log_input = ROOT / "tests" / "data" / f"{name}_input.json"
        log_run = load_run(log_input)
        log_out = run_pipeline(
            log_run, FixtureExtractor(ROOT / "tests" / "fixtures" / f"{name}_extraction.json")
        )
        problem = check_collisions(name, log_out, log_run)
        if problem:
            collisions.append(problem)
        top_slot_problems.extend(_check_declared_answer(name, log_input, log_out))

    combined_abstention = Score("Abstention, drops and free statements (combined gate)")
    combined_abstention.hits = abstained.hits + dropped.hits + free.hits
    combined_abstention.total = abstained.total + dropped.total + free.total
    combined_abstention.misses = abstained.misses + dropped.misses + free.misses

    # ---- report -----------------------------------------------------------
    print("\nMetrics")
    print(extraction_score.line(BAR_EXTRACTION))
    print(resolution_score.line(BAR_RESOLUTION))
    print(abstained.line(BAR_ABSTENTION))
    print(f"    {dropped.name:<40} {dropped.hits}/{dropped.total}")
    print(f"    {free.name:<40} {free.hits}/{free.total}")
    print(
        f"  {'Zero collisions (every slot, golden + T1-T3)':<42} "
        f"{'0 found' if not collisions else str(len(collisions)) + ' FOUND':<11}"
        f"       (bar 0)     {'PASS' if not collisions else 'FAIL'}"
    )
    print(
        f"  {'Top slot matches hand-computed (PRD 5)':<42} "
        f"{'ok' if not top_slot_problems else str(len(top_slot_problems)) + ' wrong':<11}"
        f"       (bar 0)     {'PASS' if not top_slot_problems else 'FAIL'}"
    )
    if elapsed is not None:
        budget = "within" if elapsed <= 30 else "OVER"
        print(
            f"  {'One call, ' + str(len(rows)) + ' messages':<42} "
            f"{elapsed:>5.1f}s                  (bar 30s)   {budget.upper()}"
        )

    failures: list[str] = []
    for score, bar in (
        (extraction_score, BAR_EXTRACTION),
        (resolution_score, BAR_RESOLUTION),
        (combined_abstention, BAR_ABSTENTION),
    ):
        if score.rate < bar:
            failures.append(f"{score.name} {score.rate:.1%} < {bar:.0%}")
    failures.extend(collisions)
    failures.extend(top_slot_problems)

    # ---- regression against the last accepted run -------------------------
    rates = {
        "extraction": extraction_score.rate,
        "resolution": resolution_score.rate,
        "abstention": combined_abstention.rate,
    }
    if args.save_baseline:
        write_baseline(rates, sources)
        print(f"\nBaseline written to {BASELINE.relative_to(ROOT)}")
    else:
        regressions, notes = compare_to_baseline(rates, sources)
        failures.extend(regressions)
        if notes:
            print("\nAgainst baseline")
            for note in notes:
                print(f"  {note}")
    if args.require_recorded and hand:
        failures.append(
            f"--require-recorded: {', '.join(hand)} still hand-authored"
        )

    all_misses = (
        extraction_score.misses
        + resolution_score.misses
        + combined_abstention.misses
    )
    if all_misses:
        print("\nMissed rows")
        for miss in all_misses:
            print(f"  - {miss}")

    if failures:
        print("\nFAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nPASS -- every metric at or above its bar")
    return 0


def _check_declared_answer(name: str, input_path: Path, out: RunOutput) -> list[str]:
    """Verify the hand-computed answer a test log declares about itself."""
    data = json.loads(input_path.read_text(encoding="utf-8"))
    problems: list[str] = []

    want_top = data.get("expected_top_slot")
    if want_top:
        if not out.slots:
            problems.append(f"{name}: expected a top slot, got none")
        else:
            top = out.slots[0]
            want_start = datetime.fromisoformat(want_top["start"])
            want_end = datetime.fromisoformat(want_top["end"])
            if (top.start, top.end) != (want_start, want_end):
                problems.append(
                    f"{name}: top slot {top.start:%a %d %b %H:%M}-{top.end:%H:%M} "
                    f"want {want_start:%a %d %b %H:%M}-{want_end:%H:%M}"
                )

    want_breaks = data.get("expected_top_slot_breaks")
    if want_breaks is not None and out.slots:
        got = sorted({speaker for speaker, _ in out.slots[0].broken})
        if got != sorted(want_breaks):
            problems.append(f"{name}: top slot breaks {got} want {sorted(want_breaks)}")

    for speaker in data.get("expected_unresolved_speakers", []):
        if not any(u.speaker.lower() == speaker.lower() for u in out.unresolved):
            problems.append(f"{name}: {speaker} should be in needs-confirmation")

    return problems


if __name__ == "__main__":
    sys.exit(main())
