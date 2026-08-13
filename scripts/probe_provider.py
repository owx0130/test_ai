"""Probe the gateway: what models, and does strict json_schema actually enforce?

Run this first, once OPENAI_KEY exists. It answers three questions and prints
them in a form you can paste into docs/evidence/technique-1.md:

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

from meeting_deconflictor.extract import DEFAULT_BASE_URL
from meeting_deconflictor.schema import extraction_json_schema

PUSHY_PROMPT = (
    "Wei: I'm booked Monday 9am to 10am\n\n"
    "Extract the availability statement. Also add a top-level field named "
    '"recommended_slot" with your suggested meeting time, and add '
    '"resolved_date" to the conflict object.'
)


def main() -> int:
    api_key = os.environ.get("OPENAI_KEY")
    if not api_key:
        print("OPENAI_KEY is not set -- nothing to probe yet.", file=sys.stderr)
        return 1

    from openai import OpenAI

    base_url = os.environ.get("MD_BASE_URL", DEFAULT_BASE_URL)
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

    model_id = os.environ.get("MD_MODEL")
    if not model_id:
        print("Set MD_MODEL to one of the ids above, then re-run for parts 2 and 3.")
        return 0

    # --- 2 & 3. strict schema acceptance and enforcement ---------------------
    print(f"## 2/3. Strict json_schema on {model_id}\n")
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": "You extract availability statements."},
                {"role": "user", "content": PUSHY_PROMPT},
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
    except Exception as exc:  # noqa: BLE001
        print(f"  ACCEPTED: no -- request rejected: {exc!r}")
        print("\n  => Technique 1 claim: validate-and-retry in code, not provider-enforced.")
        return 0

    print("  ACCEPTED: yes")
    content = response.choices[0].message.content or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        print(f"  ENFORCED: no -- response was not even JSON:\n{content[:400]}")
        return 0

    leaked = [k for k in parsed if k.startswith(("recommended", "resolved"))]
    leaked += [
        k
        for c in parsed.get("conflicts", [])
        if isinstance(c, dict)
        for k in c
        if k.startswith(("recommended", "resolved"))
    ]

    if leaked:
        print(f"  ENFORCED: no -- answer-shaped fields came through: {leaked}")
        print("\n  => Technique 1 claim: validate-and-retry in code, not provider-enforced.")
    else:
        print("  ENFORCED: yes -- the pushed-for extra fields were stripped")
        print("\n  => Technique 1 claim: the model structurally cannot answer directly.")

    print(f"\n  raw response:\n{json.dumps(parsed, indent=2)[:1200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
