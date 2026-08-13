"""Wires stages 2-7. FROZEN after Task 1 -- see TEAM_PLAN.md warning 2.

This is the only module that imports every stage. Read it top to bottom and you
have read the whole product.

Note what is absent: there is no loop. Stage 2 runs once, and stages 3-7 are
pure functions of its output plus ``run.today``. An agent loop would only be
warranted if one result had to determine a later call -- it does not.
"""

from __future__ import annotations

from meeting_deconflictor.dates import resolve
from meeting_deconflictor.extract import Extractor, extract
from meeting_deconflictor.models import RunInput, RunOutput
from meeting_deconflictor.provenance import check_provenance
from meeting_deconflictor.schedule import find_slots
from meeting_deconflictor.verify import assert_no_collisions

#: How many proposals the PRD asks for.
TOP_N = 3


def run_pipeline(run: RunInput, backend: Extractor) -> RunOutput:
    extraction = extract(run, backend)                          # 2. one LLM call
    raw, unresolved_quotes = check_provenance(extraction, run)  # 3. provenance
    conflicts, unresolved_dates = resolve(raw, run)             # 4. date resolution
    ranked = find_slots(conflicts, run)                         # 5. slots + ranking

    top = tuple(ranked[:TOP_N])
    assert_no_collisions(list(top), conflicts)                  # 6. proof, or raise

    return RunOutput(                                           # 7. hand to renderer
        slots=top,
        conflicts=tuple(conflicts),
        unresolved=tuple(unresolved_quotes + unresolved_dates),
    )
