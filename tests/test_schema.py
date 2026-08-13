"""Technique 1's structural evidence.

These tests assert the model *cannot* answer the question directly, which is
the whole reason extraction is schema-constrained rather than free-text.
"""

from __future__ import annotations

import pytest

from meeting_deconflictor.schema import (
    FORBIDDEN_FIELD_SUBSTRINGS,
    Extraction,
    RawConflict,
    extraction_json_schema,
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
