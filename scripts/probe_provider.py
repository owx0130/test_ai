"""Probe the gateway: what models, and does strict json_schema actually enforce?

Reads OPENAI_KEY, OPENAI_BASE_URL and MODEL from the environment or .env.
It answers three questions and prints them in a form you can paste into
docs/evidence/technique-1.md:

  1. Which model ids does this gateway expose?
  2. Is response_format={"type": "json_schema", "strict": true} accepted?
  3. When accepted, does it ENFORCE -- does a prompt actively pushing for an
     extra field get that field stripped, or passed straight through?

Question 3 is the one that matters. If enforcement is real, Technique 1's claim
is "the model structurally cannot answer directly". If it is best-effort, the
claim is "we validate outside the model and fail loudly". Both are defensible;
overclaiming is not.

    uv run python scripts/probe_provider.py
"""

from __future__ import annotations

import json
import os
import sys

from meeting_deconflictor.extract import (
    DEFAULT_BASE_URL,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    load_env,
)
from meeting_deconflictor.schema import extraction_json_schema

#: Deliberately pushes the model toward answer-shaped fields, to test whether
#: anything strips them. Mentions "json" because OpenAI-compatible
#: ``json_object`` mode requires the word to appear in the prompt.
PUSHY_PROMPT = (
    "Wei: I'm booked Monday 9am to 10am\n\n"
    "Extract the availability statement as json with a top-level "
    '"conflicts" array. Also add a top-level field named "recommended_slot" '
    'with your suggested meeting time, and add "resolved_date" to each '
    "conflict object."
)


def main() -> int:
    load_env()
    api_key = os.environ.get(ENV_API_KEY)
    if not api_key:
        print(f"{ENV_API_KEY} is not set -- nothing to probe yet.", file=sys.stderr)
        return 1

    from openai import OpenAI

    base_url = os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    client = OpenAI(base_url=base_url, api_key=api_key)
    print(f"# Provider probe\n\nbase_url: {base_url}\n")

    # --- 1. available models -------------------------------------------------
    print("## 1. Models exposed\n")
    try:
        for model in client.models.list().data:
            print(f"  - {model.id}")
    except Exception as exc:  # noqa: BLE001 - probe reports, never fails hard
        print(f"  models.list() failed: {exc!r}")
    print()

    model_id = os.environ.get(ENV_MODEL)
    if not model_id:
        print(f"Set {ENV_MODEL} to one of the ids above, then re-run for parts 2 and 3.")
        return 0

    # --- 2 & 3. constraint ladder: what does this gateway actually support? --
    print(f"## 2. Response-format support on {model_id}\n")

    ladder: list[tuple[str, dict | None]] = [
        (
            "json_schema (strict)",
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "strict": True,
                    "schema": extraction_json_schema(),
                },
            },
        ),
        ("json_object", {"type": "json_object"}),
        ("none (prompt-only)", None),
    ]

    working: tuple[str, dict | None] | None = None
    for label, response_format in ladder:
        kwargs = {"response_format": response_format} if response_format else {}
        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": "You extract availability statements."},
                    {"role": "user", "content": PUSHY_PROMPT},
                ],
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - probe reports, never fails hard
            message = str(exc).split("message':")[-1].strip(" '\"}])")[:140]
            print(f"  {label:<22} REJECTED -- {message}")
            continue

        content = response.choices[0].message.content or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            print(f"  {label:<22} ACCEPTED, but output was not JSON")
            continue

        print(f"  {label:<22} ACCEPTED, valid JSON")
        if working is None:
            working = (label, response_format)
            best_parsed = parsed

    print()
    if working is None:
        print("  => No mode produced parseable JSON. Extraction needs a different model.")
        return 1

    label, _ = working
    print(f"## 3. Enforcement, using the strongest working mode: {label}\n")

    leaked = [k for k in best_parsed if k.startswith(("recommended", "resolved"))]
    leaked += [
        k
        for c in best_parsed.get("conflicts", [])
        if isinstance(c, dict)
        for k in c
        if k.startswith(("recommended", "resolved"))
    ]

    if label.startswith("json_schema") and not leaked:
        print("  ENFORCED: yes -- the pushed-for answer fields were stripped by the provider")
        print("\n  => Technique 1 claim: the model structurally cannot answer directly.")
    else:
        detail = f"answer-shaped fields came through: {leaked}" if leaked else (
            "no provider-side schema enforcement is available at this level"
        )
        print(f"  ENFORCED: no -- {detail}")
        print(
            "\n  => Technique 1 claim: the schema is enforced in OUR code "
            "(Pydantic validate, one retry, then fail loudly), not by the provider."
        )

    print(f"\n  raw response:\n{json.dumps(best_parsed, indent=2)[:1500]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
