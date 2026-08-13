# Technique 1 — Structured output to a strict JSON schema

**Provider:** opencode zen gateway, `https://opencode.ai/zen/go/v1`
**Model:** `deepseek-v4-flash`
**Probe:** `uv run python scripts/probe_provider.py`

---

## The claim, corrected by measurement

The plan assumed the provider would enforce a strict JSON schema, which would
have made the claim *"the model structurally cannot answer directly."*

**It does not.** The probe settled it:

```
## 2. Response-format support on deepseek-v4-flash

  json_schema (strict)   REJECTED -- Upstream request failed:
                         [invalid_request_error] This response_format type is unavailable now
  json_object            ACCEPTED, valid JSON
  none (prompt-only)     ACCEPTED, valid JSON
```

So the honest claim is narrower:

> The provider guarantees **syntactically valid JSON** (`json_object` mode).
> The **schema** is enforced in our code — Pydantic with `extra="forbid"`,
> one retry, then fail loudly. Nothing unvalidated reaches stage 3.

A first probe run also reported `json_object` as REJECTED. That was a fault in
the probe, not the gateway: OpenAI-compatible `json_object` mode requires the
word "json" to appear in the prompt, and the probe's prompt did not contain it.
Fixed in `scripts/probe_provider.py`; the prompt now says "as json".

## Why the guard is load-bearing, not decorative

The probe deliberately pushes the model toward answer-shaped fields. Under the
strongest available mode (`json_object`), it complied:

```json
{
  "conflicts": [
    { "start": "Monday 9am", "end": "Monday 10am", "resolved_date": "Monday 10am" }
  ],
  "recommended_slot": "Monday 10am to 11am"
}
```

`recommended_slot` is the model proposing an answer. `resolved_date` is the
model doing date arithmetic against a calendar it cannot see. **Both are exactly
what the PRD forbids, and both came through the provider untouched.**

`Extraction` sets `extra="forbid"`, so this payload raises `ValidationError`
and never reaches stage 3. On this provider that check is the only thing
standing between the model and an invented answer — it is not belt-and-braces.

## What this changed in the code

| Before (assumed) | After (measured) |
|---|---|
| `response_format={"type": "json_schema", "strict": true}` | Ladder: try `json_schema` → `json_object` → unconstrained, stepping down on `BadRequestError` |
| Schema shape carried by the API | Schema shape stated explicitly in `prompts/extraction.system.md`, since nothing else tells the model |
| Pydantic validation as a backstop | Pydantic validation as the **primary** enforcement |
| — | `_unfence()` strips markdown fences, because the bottom rung has no JSON guarantee |

## Structural evidence (provider-independent)

`tests/test_schema.py`, 5 tests, all passing:

- every object is `additionalProperties: false`
- every property appears in `required`
- **no field name contains** `slot`, `recommend`, `answer`, `resolved`,
  `suggest`, or `propose` — there is nowhere to put a recommendation
- a payload containing `recommended_slot` is rejected at validation time
- `unparseable` is expressible, so abstention has somewhere to go

## Timing

| Run | Elapsed |
|---|---|
| `record_fixture.py` (5 messages) | 14.1 s |
| `cli --live` (5 messages, full pipeline) | 9.5 s |

Both inside the PRD's 30-second budget, though that budget is specified for 25
messages — **not yet measured at that size.**

## Still open

- Only `deepseek-v4-flash` was probed. Other gateway models may support
  `json_schema`; if one does, the ladder picks it up with no code change.
- The free-text comparison (same prompt, no `response_format`, regex-parsed) is
  not yet run as a controlled A/B on the golden set. The probe's unconstrained
  rung is suggestive but is a single sample.
