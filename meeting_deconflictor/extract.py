"""Stage 2 -- the one and only LLM call.

Technique 1 lives at this boundary. Nothing downstream of here talks to a model,
and nothing here does date arithmetic.

Two backends sit behind one protocol:

* :class:`FixtureExtractor` replays recorded JSON. Every test and the whole eval
  run use it -- no network, no key, fully deterministic.
* :class:`LiveExtractor` calls the configured OpenAI-compatible gateway.

There is deliberately no loop here. One shot over all messages; message N's
extraction does not change how message N+1 is extracted, so a loop would add
failure modes without adding capability.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from jinja2 import Template
from pydantic import ValidationError

from meeting_deconflictor.models import RunInput
from meeting_deconflictor.schema import Extraction, extraction_json_schema

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"


class ExtractionError(RuntimeError):
    """The provider returned something we could not validate, twice."""


def build_prompts(run: RunInput) -> tuple[str, str]:
    """Render the system and user turns. Prompts live in files, never inline."""
    system = (PROMPT_DIR / "extraction.system.md").read_text(encoding="utf-8")
    template = Template(
        (PROMPT_DIR / "extraction.user.md.j2").read_text(encoding="utf-8"),
        keep_trailing_newline=True,
    )
    user = template.render(
        today_long=run.today.strftime("%A %d %B %Y"),
        required=list(run.required),
        optional=list(run.optional),
        duration_minutes=run.duration_minutes,
        window_business_days=run.window_business_days,
        messages=run.messages,
    )
    return system, user


class Extractor(Protocol):
    """Anything that can turn a rendered prompt pair into raw JSON text."""

    def __call__(self, system: str, user: str) -> str: ...


class FixtureExtractor:
    """Replay a recorded extraction. Deterministic, offline, no key required.

    Top-level keys beginning with ``_`` are stripped before validation, so a
    fixture can carry its own provenance (``_source: hand-authored`` vs
    ``recorded``) without loosening the schema's ``extra="forbid"``.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def source(self) -> str:
        """Where this fixture came from. ``hand-authored`` is a known limit."""
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data.get("_source", "unknown")

    def __call__(self, system: str, user: str) -> str:  # noqa: ARG002 - protocol shape
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return json.dumps({k: v for k, v in data.items() if not k.startswith("_")})


class LiveExtractor:
    """Call the configured OpenAI-compatible gateway (opencode zen by default).

    Reads ``MD_BASE_URL``, ``MD_MODEL`` and ``OPENAI_KEY`` from the environment
    so the provider is never hardcoded at a call site.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url or os.environ.get("MD_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.environ.get("MD_MODEL")
        self.api_key = api_key or os.environ.get("OPENAI_KEY")
        if not self.model:
            raise ExtractionError(
                "MD_MODEL is not set. Run scripts/probe_provider.py to list the model "
                "ids this gateway exposes -- do not guess one."
            )
        if not self.api_key:
            raise ExtractionError("OPENAI_KEY is not set.")

    def __call__(self, system: str, user: str) -> str:
        from openai import OpenAI

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "strict": True,
                    "schema": extraction_json_schema(),
                },
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise ExtractionError("provider returned an empty message")
        return content


def extract(run: RunInput, backend: Extractor) -> Extraction:
    """Run stage 2 and validate the result in code.

    The Pydantic validation is not redundant with the provider's ``strict``
    mode. Whether this gateway enforces the schema is a property of the gateway,
    probed rather than assumed -- so the deterministic check runs regardless.
    One retry, then fail loudly. We never ship an unvalidated extraction.
    """
    system, user = build_prompts(run)

    last_error: Exception | None = None
    for _ in range(2):
        raw = backend(system, user)
        try:
            return Extraction.model_validate_json(raw)
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = exc

    raise ExtractionError(
        f"provider output failed schema validation twice: {last_error}"
    ) from last_error
