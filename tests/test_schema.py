"""Technique 1's structural evidence, plus the prompt/extraction contracts.

These tests assert the model *cannot* answer the question directly, which is
the whole reason extraction is schema-constrained rather than free-text. The
second half asserts the things F5 hardened: that the prompt and the schema
cannot drift apart, and that an extraction we could not validate is never
allowed through.

Everything here is offline -- no key, no network.

**F5 owns this file.**
"""

from __future__ import annotations

import json
import re
from datetime import date

import pytest

from meeting_deconflictor.extract import (
    PROMPT_DIR,
    ExtractionError,
    FixtureExtractor,
    build_prompts,
    extract,
)
from meeting_deconflictor.models import Message, RunInput
from meeting_deconflictor.schema import (
    FORBIDDEN_FIELD_SUBSTRINGS,
    Extraction,
    RawConflict,
    extraction_json_schema,
)

SYSTEM_PROMPT = (PROMPT_DIR / "extraction.system.md").read_text(encoding="utf-8")

RUN = RunInput(
    messages=(
        Message("Wei", "standups every morning till 10"),
        Message("Aisyah", "avoid lunch hour pls"),
    ),
    today=date(2026, 8, 17),
    window_business_days=10,
    duration_minutes=60,
    required=("Wei", "Aisyah"),
    optional=("Ravi",),
)


def _all_object_schemas(node: object) -> list[dict]:
    """Every ``type: object`` subschema, including those under ``$defs``."""
    found: list[dict] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_all_object_schemas(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_all_object_schemas(value))
    return found


def _all_property_names(node: object) -> set[str]:
    names: set[str] = set()
    for obj in _all_object_schemas(node):
        names |= set(obj.get("properties", {}))
    return names


def test_every_object_forbids_additional_properties():
    """No object may accept fields we did not design for."""
    objects = _all_object_schemas(extraction_json_schema())
    assert objects, "expected at least one object schema"
    for obj in objects:
        assert obj.get("additionalProperties") is False, obj


def test_no_answer_shaped_field_exists():
    """The schema must offer nowhere to put a recommendation."""
    for name in _all_property_names(extraction_json_schema()):
        lowered = name.lower()
        for banned in FORBIDDEN_FIELD_SUBSTRINGS:
            assert banned not in lowered, (
                f"field {name!r} would let the model answer directly instead of extract"
            )


def test_every_property_is_required():
    """OpenAI strict mode requires it, and it stops the model omitting hard fields."""
    for obj in _all_object_schemas(extraction_json_schema()):
        properties = set(obj.get("properties", {}))
        if not properties:
            continue
        assert properties == set(obj.get("required", [])), obj


def test_extra_fields_are_rejected_at_validation_time():
    """Belt and braces: even if a provider ignores strict mode, we reject."""
    payload = {
        "conflicts": [
            {
                "speaker": "Wei",
                "polarity": "busy",
                "day_reference": "Monday",
                "time_start": "09:00",
                "time_end": "10:00",
                "hardness": "hard",
                "quote": "booked Monday 9am to 10am",
                "unparseable": False,
                "recommended_slot": "Tue 10:00",  # the thing we must never accept
            }
        ]
    }
    with pytest.raises(Exception):
        Extraction.model_validate(payload)


def test_unparseable_is_expressible():
    """Abstention must be representable, or Technique 3 has nothing to score."""
    conflict = RawConflict(
        speaker="Priya",
        polarity="free",
        day_reference="after the sprint review",
        time_start=None,
        time_end=None,
        hardness="soft",
        quote="any time after the sprint review",
        unparseable=True,
    )
    assert conflict.unparseable is True


# ---------------------------------------------------------------------------
# The prompt and the schema must not drift apart
# ---------------------------------------------------------------------------


def _prompt_example() -> dict:
    """The worked JSON object the system prompt shows the model."""
    fenced = re.search(r"```json\n(.*?)\n```", SYSTEM_PROMPT, re.DOTALL)
    assert fenced, "the system prompt must show a worked JSON example"
    return json.loads(fenced.group(1))


def test_prompt_example_uses_exactly_the_schema_fields():
    """A prompt that teaches a field the schema rejects poisons every response.

    This is the drift that would otherwise be found only by a live call: add a
    field to schema.py, forget the prompt, and every extraction fails
    validation twice and raises.
    """
    shown = set(_prompt_example()["conflicts"][0])
    assert shown == set(RawConflict.model_fields)


def test_prompt_example_validates_against_the_schema():
    """The example we hand the model must itself be a legal response."""
    Extraction.model_validate(_prompt_example())


def test_prompt_documents_every_field():
    """No field may be left for the model to guess the meaning of."""
    for name in RawConflict.model_fields:
        assert name in SYSTEM_PROMPT, f"{name} is in the schema but not in the prompt"


def test_prompt_never_invites_an_answer_shaped_field():
    """The prompt must not offer what the schema forbids."""
    example_keys = set(_prompt_example()["conflicts"][0])
    for key in example_keys:
        for banned in FORBIDDEN_FIELD_SUBSTRINGS:
            assert banned not in key.lower()


def test_prompt_teaches_the_self_contained_day_reference():
    """The failure this hardening exists to prevent.

    ``dates.py`` reads the part of day and the recurrence out of
    ``day_reference`` alone -- it never sees the message. A bare "lunch hour"
    abstains, and a bare "every day" is worse: it blocks 09:00-18:00 all week.
    Only "lunch hour every day" resolves to the daily 12:00-13:00 the PRD wants,
    so the prompt has to say so.
    """
    assert "lunch hour every day" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_user_prompt_renders_every_message_numbered():
    _, user = build_prompts(RUN)
    assert "1. Wei: standups every morning till 10" in user
    assert "2. Aisyah: avoid lunch hour pls" in user


def test_user_prompt_states_today_in_full():
    _, user = build_prompts(RUN)
    assert "Monday 17 August 2026" in user


def test_prompts_are_files_not_string_literals():
    """TEAM_PLAN: prompts live in prompts/*.md, never inline in Python."""
    system, _ = build_prompts(RUN)
    assert system == SYSTEM_PROMPT
    assert len(system) > 500


# ---------------------------------------------------------------------------
# An extraction we could not validate is never allowed through
# ---------------------------------------------------------------------------


class _Backend:
    """A scripted stand-in for the gateway. Records how often it was called."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, system: str, user: str) -> str:  # noqa: ARG002
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


_VALID = json.dumps(
    {
        "conflicts": [
            {
                "speaker": "Wei",
                "polarity": "busy",
                "day_reference": "every morning",
                "time_start": None,
                "time_end": "10:00",
                "hardness": "hard",
                "quote": "standups every morning till 10",
                "unparseable": False,
            }
        ]
    }
)


def test_invalid_output_raises_rather_than_returning_something_partial():
    backend = _Backend("not json at all")
    with pytest.raises(ExtractionError):
        extract(RUN, backend)


def test_an_answer_shaped_field_is_refused_even_if_the_provider_allows_it():
    """On this gateway our validation is the *only* schema enforcement."""
    smuggled = json.dumps(
        {"conflicts": [{**json.loads(_VALID)["conflicts"][0], "resolved_date": "2026-08-17"}]}
    )
    backend = _Backend(smuggled)
    with pytest.raises(ExtractionError):
        extract(RUN, backend)


def test_it_retries_exactly_once_then_gives_up():
    backend = _Backend("{}garbage")
    with pytest.raises(ExtractionError):
        extract(RUN, backend)
    assert backend.calls == 2, "one retry, then fail loudly -- not a loop"


def test_a_retry_that_succeeds_is_used():
    backend = _Backend("truncated {", _VALID)
    extraction = extract(RUN, backend)
    assert backend.calls == 2
    assert extraction.conflicts[0].speaker == "Wei"


def test_a_fenced_response_is_unwrapped():
    """The ladder's bottom rung is unconstrained, where fences reappear."""
    backend = _Backend(f"```json\n{_VALID}\n```")
    assert extract(RUN, backend).conflicts[0].day_reference == "every morning"


def test_fixture_provenance_keys_do_not_break_strict_validation(tmp_path):
    """A fixture documents where it came from without loosening extra='forbid'."""
    path = tmp_path / "f.json"
    path.write_text(
        json.dumps({"_source": "hand-authored", "_model": None, **json.loads(_VALID)})
    )
    assert FixtureExtractor(path).source() == "hand-authored"
    assert extract(RUN, FixtureExtractor(path)).conflicts[0].speaker == "Wei"
