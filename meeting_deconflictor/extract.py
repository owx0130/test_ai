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

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv
from jinja2 import Template
from pydantic import ValidationError

from meeting_deconflictor.models import RunInput
from meeting_deconflictor.schema import Extraction, extraction_json_schema

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"

#: Environment variables the live backend reads. Kept in one place so the
#: provider is configuration, never a hardcoded call site.
ENV_BASE_URL = "OPENAI_BASE_URL"
ENV_MODEL = "MODEL"
ENV_API_KEY = "OPENAI_KEY"


def load_env() -> None:
    """Load ``.env`` from the project root if present. Never overrides a real env var."""
    load_dotenv(PROMPT_DIR.parent / ".env", override=False)


def prompt_fingerprint() -> str:
    """A short hash of the two prompt files.

    Recorded into every fixture so a later run can tell whether the prompt has
    moved underneath it. Tuning the prompt changes what the model emits, which
    silently invalidates fixtures recorded under the old one -- the hazard
    TEAM_PLAN.md flags between F5 and F6. Storing this turns "silently" into a
    warning.
    """
    digest = hashlib.sha256()
    for name in ("extraction.system.md", "extraction.user.md.j2"):
        digest.update((PROMPT_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


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

    def recorded_fingerprint(self) -> str | None:
        """The prompt this was recorded under, or ``None`` if it predates the stamp."""
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data.get("_prompt_sha256")

    def __call__(self, system: str, user: str) -> str:  # noqa: ARG002 - protocol shape
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return json.dumps({k: v for k, v in data.items() if not k.startswith("_")})


class LiveExtractor:
    """Call the configured OpenAI-compatible gateway (opencode zen by default).

    Reads ``OPENAI_BASE_URL``, ``MODEL`` and ``OPENAI_KEY`` from the environment
    (or ``.env``) so the provider is never hardcoded at a call site.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        load_env()
        self.base_url = base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
        self.model = model or os.environ.get(ENV_MODEL)
        self.api_key = api_key or os.environ.get(ENV_API_KEY)
        if not self.model:
            raise ExtractionError(
                f"{ENV_MODEL} is not set. Run scripts/probe_provider.py to list the "
                "model ids this gateway exposes -- do not guess one."
            )
        if not self.api_key:
            raise ExtractionError(f"{ENV_API_KEY} is not set.")
        #: Which rung of the ladder actually worked, recorded into fixtures.
        self.used_response_format: dict | None = None

    def _response_format_ladder(self) -> list[dict | None]:
        """Constraint modes, strongest first.

        Support varies by gateway, so we degrade rather than assume. Probed on
        opencode zen (see docs/evidence/technique-1.md): ``json_schema`` is
        rejected outright, ``json_object`` works. The last rung is no
        constraint at all, which still leaves our own validation in place.
        """
        return [
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "strict": True,
                    "schema": extraction_json_schema(),
                },
            },
            {"type": "json_object"},
            None,
        ]

    def __call__(self, system: str, user: str) -> str:
        from openai import BadRequestError, OpenAI

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        last_error: Exception | None = None
        for response_format in self._response_format_ladder():
            kwargs = {"response_format": response_format} if response_format else {}
            try:
                response = client.chat.completions.create(
                    model=self.model, messages=messages, **kwargs
                )
            except BadRequestError as exc:
                # This gateway does not offer this constraint mode. Step down.
                last_error = exc
                continue

            self.used_response_format = response_format
            content = response.choices[0].message.content
            if not content:
                raise ExtractionError("provider returned an empty message")
            return content

        raise ExtractionError(
            f"no supported response format for model {self.model!r}: {last_error}"
        ) from last_error


def _unfence(raw: str) -> str:
    """Strip a ``` fence if the model wrapped its JSON in one.

    Needed because the constraint ladder can fall through to unconstrained
    mode on gateways that offer no JSON mode at all.
    """
    text = raw.strip()
    if not text.startswith("```"):
        return text
    body = text.split("\n", 1)[-1] if "\n" in text else ""
    return body.rsplit("```", 1)[0].strip()


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
            return Extraction.model_validate_json(_unfence(raw))
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = exc

    raise ExtractionError(
        f"provider output failed schema validation twice: {last_error}"
    ) from last_error
