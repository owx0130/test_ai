# Meeting Deconflictor — Team Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paste a thread of scheduling messages; get three dated meeting slots that collide with nothing anyone declared, plus an echo of every conflict read and a list of everything unresolved.

**Architecture:** One LLM call does extraction only, constrained by a strict JSON schema. Everything after it — quote provenance, date resolution, recurrence expansion, slot finding, ranking, collision checking — is plain Python. There is no agent loop: no extracted result determines a subsequent tool call or model call, so a loop would add failure modes without adding capability.

**Tech Stack:** Python 3.12, `uv`, `pydantic` (schema), `openai` SDK pointed at the **opencode zen** gateway (`OPENAI_BASE_URL=https://opencode.ai/zen/go/v1`, `OPENAI_KEY`), `pytest`. No web server, no auth, no calendar integration, no persistence.

**Spec:** `PRD.md` (in this repo — executors read both).

---

## Global Constraints

Copied verbatim from `PRD.md`; every task's requirements implicitly include this section.

- Single timezone. Business hours **Mon–Fri 09:00–18:00**. **30-minute** granularity. No calendar access. **Up to 25 messages** per run.
- **Exactly one LLM call per run.** Extraction only — the model never resolves a date and never proposes a slot.
- **Provider:** OpenAI-compatible client against `https://opencode.ai/zen/go/v1`, credential from `OPENAI_KEY`. Base URL and model ID are config values (`MD_BASE_URL`, `MD_MODEL`), never hardcoded in a call site.
- **Zero collisions.** No proposed slot overlaps any declared hard conflict, checked in code across the whole golden set.
- **Zero invented conflicts.** Every extracted conflict traces to a verbatim quote in the input, and all of them appear in the echo block.
- **Zero silent drops.** Anything unresolved appears in needs-confirmation. An unheard person is never treated as a free person.
- Ranking, in strict order: all required attendees free → most optional attendees free → earliest.
- 25 messages processed in under 30 seconds.
- Prompts live in `prompts/*.md`, never inline in Python.
- Deterministic validation lives outside the model, in code.
- Non-goals stay non-goals: no calendar integration, invites, holds, rooms, timezones, recurring series, seniority weighting, agendas, reminders, >25 messages.

---

## The smallest complete input-to-output path

Seven stages. Only stage 2 involves the model.

```
 1. INPUT      messages[] + today + window(10 business days) + duration + required[] + optional[]
                    │                                                        cli.py, models.py
                    ▼
 2. EXTRACT    ONE LLM call, strict JSON schema  ─────────── TECHNIQUE 1
               each message → {speaker, busy|free, day_reference verbatim,
                               time range, hard|soft, quote, unparseable}
                    │                                    extract.py, prompts/, schema.py
                    ▼
 3. PROVENANCE every quote must be a verbatim substring of an input message.
               Non-verbatim → dropped + logged. unparseable → needs-confirmation.
                    │                                                     provenance.py
                    ▼
 4. RESOLVE    day_reference → concrete business dates against today.  ─── TECHNIQUE 2
               "every morning" → every business day in window.
               "till the 21st" → each business day through Fri 21 Aug.
               Unresolvable → needs-confirmation (never guessed).
                    │                                                          dates.py
                    ▼
 5. SLOTS      30-min grid over the window. Hard conflicts exclude,
               soft conflicts deduct. Contiguous blocks of `duration`.
               Rank: all required free → most optional free → earliest.
                    │                                                       schedule.py
                    ▼
 6. VERIFY     assert no proposed slot overlaps any hard conflict.  ────── TECHNIQUE 2
               Raises rather than returns — an unprovable answer is not shipped.
                    │                                                         verify.py
                    ▼
 7. OUTPUT     top 3 dated slots + attendee lists
               + conflict echo (every conflict, with its quote)
               + needs-confirmation list
                                                                    render.py, cli.py
```

**The narrowest thing that is still the whole product** is this path run on test example T1 (five clean messages, one obvious gap): real prompt, real schema, real date arithmetic, real ranking, real rendering. Everything after T1 widens stages 4 and 5; it does not add stages.

---

## Where each course technique belongs, and the evidence for it

### Technique 1 — Structured output to a strict JSON schema

**Where:** stage 2 only. `meeting_deconflictor/schema.py` (the Pydantic model → JSON schema), `meeting_deconflictor/extract.py` (the single `client.chat.completions.create(..., response_format={"type": "json_schema", "json_schema": {"name": "extraction", "strict": True, "schema": ...}})` call against the gateway), `prompts/extraction.system.md` and `prompts/extraction.user.md.j2`.

**Provider caveat — must be probed in Task 1.** Strict `json_schema` response-format enforcement is a property of the gateway *and* the model behind it, not something we can assume. Task 1 Step 0 probes it. If the gateway enforces strict schema, Technique 1's guarantee is "the model structurally cannot emit an answer". If it only honours `json_schema` best-effort, the guarantee downgrades to "validate with Pydantic, one retry, then fail loudly" — still deterministic and still outside the model, but a materially weaker claim. **Whichever holds, the README states it plainly rather than overclaiming.**

**Why it is here and not elsewhere:** the schema is the mechanism that stops the model proposing an answer directly. There is no `recommended_slot` field, no `resolved_date` field, no free-text summary field. The model's only expressible output is per-message extraction.

**Evidence that it is useful:**

| Evidence | How it is produced | Bar |
|---|---|---|
| Schema conformance rate | `eval/run_eval.py` reports parses that validated on first attempt across the 15-message golden set | 15/15 validate; 0 retries needed |
| The model *cannot* answer directly | `tests/test_schema.py` asserts the generated JSON schema has `additionalProperties: false` and that no property name matches `slot|recommend|answer|resolved_date` | Test passes |
| Field-typed output beats free text | Before/after comparison recorded in `docs/evidence/technique-1.md`: same prompt run with `response_format` omitted, parsed with a regex/JSON-guess fallback | Free-text run shows ≥1 unparseable response or ≥1 invented slot recommendation; schema run shows 0 |
| Schema enforcement is real, not assumed | `scripts/probe_provider.py` output pasted into `docs/evidence/technique-1.md`: does the gateway reject a response violating the schema, or pass it through? | Recorded either way; the README's claim matches the probe |

### Technique 2 — Deterministic post-processing

**Where:** stages 3–6. `provenance.py`, `dates.py`, `schedule.py`, `verify.py`. Not one line of these consults the model.

**Why it is here:** the deconflict guarantee is the product. A guarantee that depends on the model is a hope; a guarantee computed in code over an explicit interval set is provable and re-runnable.

**Evidence that it is useful:**

| Evidence | How it is produced | Bar |
|---|---|---|
| Zero collisions, proven | `verify.assert_no_collisions()` runs on **every** run, including in `eval/run_eval.py` over the whole golden set and all three test logs | 0 collisions; the assertion is load-bearing, not decorative |
| Recurrence actually expands | `tests/test_dates.py::test_daily_standup_expands_to_every_business_day` — "every morning till 10" on a 10-business-day window yields 10 intervals, not 1 | 10 intervals, correct dates, weekends absent |
| Code catches what the model got wrong | `docs/evidence/technique-2.md` records every case in the golden set where the raw extraction was date-ambiguous and code resolved or abstained correctly | ≥1 concrete case with the raw JSON and the resolved output side by side |
| Determinism | `tests/test_end_to_end.py` runs the full pipeline on a fixed extraction fixture 3× and asserts byte-identical rendered output | Identical |

### Technique 3 — Golden-set eval with required abstention

**Where:** `eval/golden.jsonl` (15 hand-labelled messages), `eval/run_eval.py`, `eval/fixtures/`.

**Why it is here:** abstention is scored as *correct*. A message like "any time after the sprint review" refers to a calendar the app cannot see; flagging it scores 1, inventing a plausible date scores 0. This is what stops the system optimising toward confident wrongness.

**Evidence that it is useful:**

| Evidence | How it is produced | Bar |
|---|---|---|
| Extraction accuracy | `eval/run_eval.py` field-level scoring vs. hand labels | **≥90%** exact match on speaker + polarity + hardness |
| Resolution accuracy | same, on resolved date + time range | **≥85%** |
| Abstention scored explicitly | golden set contains ≥3 messages labelled `unresolvable: true`; guessing one scores 0, flagging it scores 1; reported as its own line | 3/3 abstained; **0 guesses** |
| Regression detection | `docs/evidence/technique-3.md` records a deliberately degraded prompt variant re-scored against the same golden set | Degraded variant scores measurably lower — the eval detects the regression rather than passing everything |

---

## No agent loop — and why

The rule is: add a loop only when one result must control the next tool call or model call. It does not here.

- Extraction is one shot over all messages; message N's extraction does not change how message N+1 is extracted.
- Date resolution, slot finding, and verification are pure functions of the extraction plus `today`. They are code, not calls.
- Unresolvable references are *surfaced to the human*, not re-queried from the model. Re-querying would be the model guessing at a calendar it cannot see — the exact failure the PRD forbids.

If a future version needed to ask a clarifying question and re-extract based on the answer, that is the point at which a loop earns its place. Not before.

---

## File structure

Frozen at the end of Task 1. Every later feature owns a disjoint set.

| File | Responsibility | Owner after Task 1 |
|---|---|---|
| `meeting_deconflictor/models.py` | All shared dataclasses. **Frozen after Task 1.** | nobody — change needs agreement |
| `meeting_deconflictor/schema.py` | Pydantic extraction model → JSON schema | F5 |
| `meeting_deconflictor/extract.py` | The one LLM call + `Extractor` backends | F5 |
| `meeting_deconflictor/provenance.py` | Verbatim quote check, unparseable → unresolved | F3 |
| `meeting_deconflictor/dates.py` | Day-reference → concrete intervals | F1 |
| `meeting_deconflictor/schedule.py` | Grid, contiguity, ranking | F2 |
| `meeting_deconflictor/verify.py` | Zero-collision assertion | F2 |
| `meeting_deconflictor/render.py` | Text output | F4 |
| `meeting_deconflictor/pipeline.py` | Wires stages 2–7. **Frozen after Task 1.** | nobody |
| `meeting_deconflictor/cli.py` | Arg parsing, file loading | F4 |
| `prompts/extraction.system.md` | System prompt | F5 |
| `prompts/extraction.user.md.j2` | User-turn template | F5 |
| `eval/golden.jsonl` | 15 hand-labelled messages | F6 |
| `eval/run_eval.py` | Scorer | F6 |
| `tests/fixtures/*.json` | Recorded extractions | F6 |

**Offline-first testing.** `OPENAI_KEY` will be supplied later, so `extract.py` exposes two backends behind one protocol:

- `FixtureExtractor(path)` — replays a recorded extraction JSON. Every test and the whole eval run use this. **No network, no key, fully deterministic.**
- `LiveExtractor(base_url, model, api_key)` — the real gateway call, used by `--live`, by `scripts/probe_provider.py`, and by `scripts/record_fixture.py`. Reads `MD_BASE_URL` (default `https://opencode.ai/zen/go/v1`), `MD_MODEL`, and `OPENAI_KEY` from the environment.

Until the key arrives, fixtures are hand-authored to the schema so stages 3–7 are honestly tested; `scripts/record_fixture.py` then replaces them with real gateway output in one command.

This is not a mock of our own logic — the fixtures are *real recorded model output*, so stages 3–7 are exercised against exactly what the model produces.

---

## Task 1 — Walking skeleton: T1 end to end (do this before splitting the work)

**One person. Nothing else starts until this is green.** This is the whole path at its narrowest, not a scaffold.

**Files:**
- Create: `pyproject.toml`, `meeting_deconflictor/{__init__,models,schema,extract,provenance,dates,schedule,verify,render,pipeline,cli}.py`
- Create: `prompts/extraction.system.md`, `prompts/extraction.user.md.j2`
- Create: `tests/test_end_to_end.py`, `tests/fixtures/t1_extraction.json`, `tests/data/t1_input.json`
- Create: `scripts/record_fixture.py`, `scripts/probe_provider.py`

**Interfaces produced** (every other feature consumes these; they are the contract):

```python
# models.py — FROZEN after this task
@dataclass(frozen=True)
class Message:      speaker: str; text: str

@dataclass(frozen=True)
class RunInput:
    messages: tuple[Message, ...]        # <= 25
    today: date
    window_business_days: int = 10
    duration_minutes: int = 60
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

@dataclass(frozen=True)
class Interval:     start: datetime; end: datetime        # half-open [start, end)

@dataclass(frozen=True)
class ResolvedConflict:
    speaker: str; intervals: tuple[Interval, ...]
    hardness: Literal["hard", "soft"]; quote: str

@dataclass(frozen=True)
class Unresolved:   speaker: str; quote: str; reason: str

@dataclass(frozen=True)
class Slot:
    start: datetime; end: datetime
    free_required: tuple[str, ...]; free_optional: tuple[str, ...]
    broken: tuple[tuple[str, str], ...]   # (speaker, quote) pairs this slot collides with

@dataclass(frozen=True)
class RunOutput:
    slots: tuple[Slot, ...]
    conflicts: tuple[ResolvedConflict, ...]
    unresolved: tuple[Unresolved, ...]
```

```python
# stage functions — signatures are the integration contract
extract(run: RunInput, backend: Extractor) -> Extraction                    # extract.py
check_provenance(ex: Extraction, run: RunInput) -> tuple[list[RawConflict], list[Unresolved]]
resolve(raw: list[RawConflict], run: RunInput) -> tuple[list[ResolvedConflict], list[Unresolved]]
find_slots(conflicts: list[ResolvedConflict], run: RunInput) -> list[Slot]  # schedule.py
assert_no_collisions(slots: list[Slot], conflicts: list[ResolvedConflict]) -> None
render(out: RunOutput) -> str                                               # render.py
```

- [ ] **Step 0: Write the provider probe** (runs later, when `OPENAI_KEY` exists)

`scripts/probe_provider.py` answers three questions and prints them for `docs/evidence/technique-1.md`:
1. What model IDs does the gateway expose? (`client.models.list()`)
2. Does `response_format={"type":"json_schema", ..., "strict": True}` get accepted, or rejected as unsupported?
3. When accepted, does it actually enforce — does a prompt deliberately pushing for an extra field get that field stripped, or passed through?

Its output sets `MD_MODEL` and determines whether Technique 1's claim is "structurally cannot" or "validate-and-retry".

- [ ] **Step 1: Project setup**

```bash
uv init --lib --name meeting-deconflictor --python 3.12
uv add pydantic openai jinja2
uv add --dev pytest
```

- [ ] **Step 2: Write `models.py`** — exactly the dataclasses above, nothing more.

- [ ] **Step 3: Write `schema.py`** — the Pydantic extraction model.

```python
class RawConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker: str
    polarity: Literal["busy", "free"]
    day_reference: str        # verbatim, as written
    time_start: str | None    # "HH:MM" or null
    time_end: str | None
    hardness: Literal["hard", "soft"]
    quote: str                # verbatim substring of an input message
    unparseable: bool

class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conflicts: list[RawConflict]
```

- [ ] **Step 4: Write the failing end-to-end test**

```python
# tests/test_end_to_end.py
def test_t1_produces_one_correct_slot_and_empty_needs_confirmation():
    run = load_run("tests/data/t1_input.json")
    out = run_pipeline(run, FixtureExtractor("tests/fixtures/t1_extraction.json"))
    assert out.unresolved == ()
    assert out.slots[0].start == datetime(2026, 8, 18, 10, 0)
    assert set(out.slots[0].free_required) == {"Wei", "Aisyah"}
    assert_no_collisions(list(out.slots), list(out.conflicts))   # must not raise
```

- [ ] **Step 5: Run it, confirm it fails**

Run: `uv run pytest tests/test_end_to_end.py -v`
Expected: FAIL — `ModuleNotFoundError` / `NameError: run_pipeline`.

- [ ] **Step 6: Write the prompt files** — `prompts/extraction.system.md` states the extraction-only job, the `unparseable` rule, and that day references are copied *as written* and never resolved. `prompts/extraction.user.md.j2` renders `today`, the attendee lists, and the numbered messages.

- [ ] **Step 7: Write `extract.py`** — `Extractor` protocol, `FixtureExtractor`, and `LiveExtractor`:

```python
client = OpenAI(base_url=os.environ.get("MD_BASE_URL", "https://opencode.ai/zen/go/v1"),
                api_key=os.environ["OPENAI_KEY"])
resp = client.chat.completions.create(
    model=os.environ["MD_MODEL"],
    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    response_format={"type": "json_schema",
                     "json_schema": {"name": "extraction", "strict": True,
                                     "schema": Extraction.model_json_schema()}},
)
return Extraction.model_validate_json(resp.choices[0].message.content)   # validate regardless
```

The `model_validate_json` is **not** redundant with `strict` — it is the deterministic check that holds even if the gateway's enforcement is best-effort. One retry on `ValidationError`, then raise.

- [ ] **Step 8: Write minimal `provenance.py`, `dates.py`, `schedule.py`, `verify.py`, `render.py`, `pipeline.py`** — only enough for T1: exact-date and simple time ranges; no recurrence yet (F1 adds it), simple linear scan for slots (F2 replaces it).

- [ ] **Step 9: Produce the T1 fixture**

Without `OPENAI_KEY` yet: hand-author `tests/fixtures/t1_extraction.json` to the schema. Mark it in-file with `"_source": "hand-authored"`.

Once `OPENAI_KEY` is supplied, replace it with real gateway output in one command and re-run the suite:

```bash
uv run python scripts/probe_provider.py          # sets MD_MODEL, records enforcement behaviour
uv run python scripts/record_fixture.py tests/data/t1_input.json tests/fixtures/t1_extraction.json
uv run pytest
```

**A fixture still marked `hand-authored` when the README is written is a known limit that gets stated, not glossed.**

- [ ] **Step 10: Run the test, confirm it passes**

Run: `uv run pytest tests/test_end_to_end.py -v`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add -A && git commit -m "feat: end-to-end walking skeleton, test example T1 passing"
```

**Acceptance check for Task 1:** `uv run python -m meeting_deconflictor.cli tests/data/t1_input.json` prints one correct slot and an empty needs-confirmation list, and `uv run pytest` is green.

---

## Independent features (parallelisable after Task 1)

Each is sized so a reviewer could reject it while approving its neighbours.

### F1 — Date resolution engine

| | |
|---|---|
| **May change** | `meeting_deconflictor/dates.py`, `tests/test_dates.py` |
| **Must not change** | `models.py`, `pipeline.py`, anything owned by another feature |
| **Consumes** | `RawConflict`, `RunInput`, `Interval`, `ResolvedConflict`, `Unresolved` |
| **Produces** | `resolve(raw, run) -> (list[ResolvedConflict], list[Unresolved])` — signature already fixed in Task 1 |
| **Scope** | Recurring ("every morning", "daily", "each Tuesday"), dated ("Thu 20th", "the 21st"), ranges ("till the 21st", "through Friday"), relative ("next week", "tomorrow"), part-of-day defaults ("morning" → 09:00–12:00, "afternoon" → 12:00–18:00, "lunch" → 12:00–13:00). Weekend and out-of-window dates dropped. Anything else → `Unresolved`, never a guess. |
| **Acceptance check** | `uv run pytest tests/test_dates.py` green, including `test_daily_standup_expands_to_every_business_day`: `"standups every morning till 10"` with `today=2026-08-17`, `window=10` yields **exactly 10** `Interval`s of 09:00–10:00, one per business day, no Sat/Sun. |
| **Depends on** | Task 1 only |

### F2 — Slot finder, ranking, and the zero-collision verifier

| | |
|---|---|
| **May change** | `meeting_deconflictor/schedule.py`, `meeting_deconflictor/verify.py`, `tests/test_schedule.py`, `tests/test_verify.py` |
| **Must not change** | `models.py`, `dates.py`, `pipeline.py` |
| **Consumes** | `ResolvedConflict`, `RunInput`, `Interval`, `Slot` |
| **Produces** | `find_slots(conflicts, run) -> list[Slot]`, `assert_no_collisions(slots, conflicts) -> None` (raises `CollisionError`) |
| **Scope** | 30-min grid over Mon–Fri 09:00–18:00 across the window. Hard conflicts exclude a person from a cell; soft conflicts deduct from the score but do not exclude. Contiguous runs of `duration_minutes`. Rank strictly: all-required-free → most-optional-free → earliest. When no all-required-free slot exists, return the nearest option with `broken` populated (T3's requirement). |
| **Acceptance check** | `uv run pytest tests/test_schedule.py::test_no_slot_satisfies_both_required` — a run where Wei and Aisyah have no common free block returns a slot with non-empty `broken` naming exactly whose conflict it breaks, and `assert_no_collisions` raises `CollisionError` if that slot is ever presented as clean. |
| **Depends on** | Task 1. Independent of F1 — tests construct `ResolvedConflict`s directly. |

### F3 — Provenance and abstention

| | |
|---|---|
| **May change** | `meeting_deconflictor/provenance.py`, `tests/test_provenance.py` |
| **Must not change** | `models.py`, `dates.py`, `schedule.py`, `pipeline.py` |
| **Consumes** | `Extraction`, `RawConflict`, `RunInput`, `Unresolved` |
| **Produces** | `check_provenance(ex, run) -> (list[RawConflict], list[Unresolved])` |
| **Scope** | Every `quote` must be a verbatim substring of some input message by that speaker (whitespace-normalised, case-sensitive on content). Non-verbatim quotes are **dropped and reported**, never trusted. `unparseable: true` → `Unresolved`. A speaker who appears in `required`/`optional` but produced no conflict at all → `Unresolved("no message from this person")`, so an unheard person is never treated as free. |
| **Acceptance check** | `uv run pytest tests/test_provenance.py::test_invented_conflict_is_dropped_and_reported` — an extraction containing a quote absent from the input yields 0 surviving conflicts and 1 `Unresolved`; and `test_silent_person_is_not_free` — a required attendee with no message appears in `Unresolved`. |
| **Depends on** | Task 1 |

### F4 — Renderer and CLI

| | |
|---|---|
| **May change** | `meeting_deconflictor/render.py`, `meeting_deconflictor/cli.py`, `tests/test_render.py` |
| **Must not change** | any stage module, `models.py`, `pipeline.py` |
| **Consumes** | `RunOutput` only |
| **Produces** | `render(out) -> str`; `python -m meeting_deconflictor.cli <input.json> [--live]` |
| **Scope** | The exact three-block output in `PRD.md`: top 3 dated slots with attendee lists and a parenthetical free-count; a conflict echo listing **every** conflict with its speaker, resolved times, hardness; a needs-confirmation block quoting each unresolved item with its reason. When no clean slot exists, say so plainly and name whose conflict the nearest option breaks. |
| **Acceptance check** | `uv run pytest tests/test_render.py::test_matches_prd_example` — rendering the PRD's worked example `RunOutput` produces output matching the PRD's Output block (normalised for trailing whitespace). |
| **Depends on** | Task 1 (`RunOutput` shape). Independent of F1–F3 — tests construct `RunOutput` by hand. |

### F5 — Prompt and extraction hardening

| | |
|---|---|
| **May change** | `prompts/extraction.system.md`, `prompts/extraction.user.md.j2`, `meeting_deconflictor/extract.py`, `meeting_deconflictor/schema.py`, `tests/test_schema.py` |
| **Must not change** | any stage module downstream of extraction, `models.py`, `pipeline.py`, `tests/fixtures/*` |
| **Consumes** | `RunInput` |
| **Produces** | `Extraction` conforming to `schema.py`; `extract(run, backend)` unchanged in signature |
| **Scope** | Tighten the system prompt so day references are copied verbatim and never resolved; make `unparseable` the explicit escape hatch rather than a guess; add few-shot examples for recurrence and leave-ranges. Add `tests/test_schema.py` asserting `additionalProperties: false` and the absence of any answer-shaped field. Timing check: 25 messages, one call, under 30s. |
| **Acceptance check** | `uv run pytest tests/test_schema.py` green **and** `uv run python eval/run_eval.py --report` shows no regression vs. the previous recorded score. |
| **Depends on** | Task 1; needs **F6** in place to measure anything; **needs `OPENAI_KEY`** — prompt tuning against hand-authored fixtures would only be tuning against my own guesses. Start F5 only after F6's scorer runs *and* the key exists. |

### F6 — Golden set and eval harness

| | |
|---|---|
| **May change** | `eval/golden.jsonl`, `eval/run_eval.py`, `eval/fixtures/`, `tests/fixtures/*.json`, `tests/data/t2_input.json`, `tests/data/t3_input.json`, `scripts/record_fixture.py` |
| **Must not change** | anything under `meeting_deconflictor/` |
| **Consumes** | `Extraction`, `RunInput`, the whole pipeline as a black box |
| **Produces** | `eval/run_eval.py --report` → a scored table |
| **Scope** | 15 hand-labelled messages, of which **≥3 are genuinely unresolvable** (calendar-relative, like "after the sprint review"). Scorer reports: speaker/polarity/hardness exact-match %, resolved-date/time-range exact-match %, abstention correctness (flagged = 1, guessed = 0), and runs `assert_no_collisions` over every produced slot set. Also owns recording `t2`/`t3` fixtures. |
| **Acceptance check** | `uv run python eval/run_eval.py --report` prints all four metrics and exits non-zero if speaker/polarity/hardness < 90%, date/range < 85%, abstention < 100%, or any collision is found. |
| **Depends on** | Task 1. Blocks F5. |

---

## ⚠️ Merge-conflict warnings

Read this before assigning work.

**High risk — will conflict if not managed:**

1. **`models.py` is touched by every feature's imports and by nobody's edits.** It is frozen at the end of Task 1. If a feature needs a field added, that is a **separate one-line PR merged first**, not a change bundled into the feature branch. Two features each adding a field to `Slot` in parallel is the single most likely painful conflict in this plan.

2. **`pipeline.py` is the same story.** It is the only file that imports all six stage modules. Frozen after Task 1. Any signature change ripples into it and therefore into everyone.

3. **`tests/fixtures/*.json` — F5 and F6 will fight over these.** F5 changes the prompt, which changes what the model emits, which invalidates recorded fixtures. F6 owns fixture recording. **Serialise them: F6 lands first, then F5 re-records via F6's script.** Running these two in parallel means F5's fixture regeneration silently breaks F6's golden-set baseline mid-review, and neither branch looks wrong on its own.

**Moderate risk:**

4. **F1 and F2 both reason about `Interval`.** They will not conflict textually — different files — but they *will* conflict semantically if they disagree on whether intervals are half-open. Task 1 fixes it: **`[start, end)`, half-open, always.** Write it in a docstring on `Interval` so neither has to guess.

5. **F4 (renderer) reads every field of `RunOutput`.** If F2 changes what it puts in `Slot.broken`, F4's golden-string test breaks without F4 changing a line. Mitigation: F4's test constructs its own `RunOutput` rather than running the pipeline, so it only breaks on a real contract change.

6. **`pyproject.toml`** — any feature adding a dependency touches it. Low-severity but frequent. Add all four dependencies in Task 1 so nobody needs to.

**Low risk:** F1, F2, F3, F4 own disjoint files with disjoint tests. These four genuinely parallelise.

**Suggested ordering:**

```
        Task 1  (one person, blocking)
           │
   ┌───────┼───────┬───────┐
   ▼       ▼       ▼       ▼
  F1      F2      F3      F4        ← fully parallel
   └───────┴───────┴───────┘
           │
           ▼
          F6  (golden set + scorer)
           │
           ▼
          F5  (prompt tuning, measured against F6)
```

---

## Verification plan (after features land)

1. Run all three test examples: `uv run pytest tests/test_end_to_end.py -v` covering T1, T2, T3.
2. Run the eval: `uv run python eval/run_eval.py --report`.
3. **Show the first important failure before fixing it** — capture the actual failing output verbatim in `docs/evidence/first-failure.md`, then fix, then capture the passing output. T2 (recurrence applied to every day, not just one) is the likeliest first real failure.
4. **One measured improvement with before-and-after evidence** — most likely a prompt change in F5 scored against F6's golden set, recorded in `docs/evidence/technique-3.md` with both numbers.
5. Write `README.md`: setup, demonstration steps, the three techniques, the evidence for each, known limits, next safe improvement.

---

## Provider status and what it gates

**Decided:** OpenAI-compatible client → `https://opencode.ai/zen/go/v1`, credential `OPENAI_KEY`, supplied later.

| Blocked on the key | Not blocked |
|---|---|
| `scripts/probe_provider.py` — model IDs, strict-schema support | Task 1 Steps 0–8, 10, 11 |
| Recording real fixtures (`t1`, `t2`, `t3`, golden set) | F1, F2, F3, F4 in full — they never call the model |
| F5 (prompt tuning) and the live half of F6 | F6's scorer, written against hand-authored fixtures |
| Technique 1's enforcement claim + the free-text comparison | Techniques 2 and 3's evidence, on hand-authored fixtures |

Everything proceeds against hand-authored fixtures; the key converts them to real ones and unblocks F5. **Two things are stated as pending in the README until the key lands:** whether the gateway enforces strict schema, and whether the prompt actually elicits schema-conforming output from a real model.

`MD_MODEL` is unset until the probe runs — I won't guess an opencode zen model ID.
