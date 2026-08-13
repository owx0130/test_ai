"""Record a real extraction fixture from the gateway.

Replaces a hand-authored fixture with genuine model output, so every test
downstream runs against exactly the shape the provider produces.

    uv run python scripts/record_fixture.py tests/data/t1_input.json tests/fixtures/t1_extraction.json

Requires OPENAI_KEY and MD_MODEL (run scripts/probe_provider.py first).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from meeting_deconflictor.cli import load_run
from meeting_deconflictor.extract import LiveExtractor, build_prompts
from meeting_deconflictor.schema import Extraction


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    input_path, fixture_path = Path(argv[0]), Path(argv[1])
    run = load_run(input_path)
    backend = LiveExtractor()

    system, user = build_prompts(run)
    started = time.monotonic()
    raw = backend(system, user)
    elapsed = time.monotonic() - started

    # Validate before writing -- never record a fixture we could not parse.
    extraction = Extraction.model_validate_json(raw)

    payload = {
        "_source": "recorded",
        "_model": backend.model,
        "_base_url": backend.base_url,
        "_input": str(input_path).replace("\\", "/"),
        "_elapsed_seconds": round(elapsed, 2),
        **extraction.model_dump(),
    }
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(
        f"recorded {len(extraction.conflicts)} conflicts from {len(run.messages)} "
        f"messages in {elapsed:.1f}s -> {fixture_path}"
    )
    if elapsed > 30:
        print(f"WARNING: {elapsed:.1f}s exceeds the PRD's 30-second budget", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
