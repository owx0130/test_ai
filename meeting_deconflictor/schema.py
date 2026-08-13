"""The strict JSON schema that is the model's ONLY output surface.

Technique 1 lives here. The point of this file is what it does *not* contain:
there is no ``recommended_slot``, no ``resolved_date``, no free-text summary.
The model's only expressible output is per-message extraction, so it cannot
propose an answer even if it wants to. ``tests/test_schema.py`` enforces that.

Every field is required (``str | None`` rather than a default) because OpenAI
strict ``json_schema`` mode requires every property to appear in ``required``;
nullability is expressed in the type, not by omission.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Property names that would let the model answer the question directly rather
#: than extract. tests/test_schema.py asserts none of these appear.
FORBIDDEN_FIELD_SUBSTRINGS = ("slot", "recommend", "answer", "resolved", "suggest", "propose")


class RawConflict(BaseModel):
    """One statement of availability, exactly as the model read it.

    Nothing here is resolved against a calendar. ``day_reference`` is copied
    verbatim from the message; turning it into dates is code's job (stage 4).
    """

    model_config = ConfigDict(extra="forbid")

    speaker: str = Field(description="Who said it, exactly as it appears in the thread.")
    polarity: Literal["busy", "free"] = Field(
        description="Whether this statement declares unavailability or availability."
    )
    day_reference: str = Field(
        description=(
            "The day this refers to, copied VERBATIM from the message "
            '(e.g. "every morning", "Thu 20th", "till the 21st"). Never resolve it '
            "to a real date -- that is done downstream in code."
        )
    )
    time_start: str | None = Field(
        description='Start of the time range as "HH:MM" 24-hour, or null if not stated.'
    )
    time_end: str | None = Field(
        description='End of the time range as "HH:MM" 24-hour, or null if not stated.'
    )
    hardness: Literal["hard", "soft"] = Field(
        description=(
            '"hard" if the person cannot move it (a meeting, leave, a call); '
            '"soft" if it is a preference ("avoid lunch pls", "prefer mornings").'
        )
    )
    quote: str = Field(
        description=(
            "The exact substring of the speaker's message this was read from. "
            "Must appear character-for-character in the input; it is checked in code."
        )
    )
    unparseable: bool = Field(
        description=(
            "True if this statement cannot be resolved from the thread alone -- for "
            'example it refers to a calendar we cannot see ("after the sprint review"). '
            "Flagging is CORRECT and is scored as correct; guessing is scored as wrong."
        )
    )


class Extraction(BaseModel):
    """The whole model response. One entry per availability statement read."""

    model_config = ConfigDict(extra="forbid")

    conflicts: list[RawConflict] = Field(
        description="One entry per availability statement found in the thread."
    )


def extraction_json_schema() -> dict[str, Any]:
    """The schema sent to the provider as ``response_format.json_schema.schema``."""
    return Extraction.model_json_schema()
