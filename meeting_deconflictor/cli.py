"""Command line entry point. **F4 owns this file.**

    uv run python -m meeting_deconflictor.cli tests/data/t1_input.json
    uv run python -m meeting_deconflictor.cli tests/data/t1_input.json --live
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from meeting_deconflictor.extract import Extractor, FixtureExtractor, LiveExtractor
from meeting_deconflictor.models import Message, RunInput
from meeting_deconflictor.pipeline import run_pipeline
from meeting_deconflictor.render import render


def load_run(path: str | Path) -> RunInput:
    """Read a run definition. Keys prefixed with ``_`` are notes, not input."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RunInput(
        messages=tuple(
            Message(speaker=m["speaker"], text=m["text"]) for m in data["messages"]
        ),
        today=date.fromisoformat(data["today"]),
        window_business_days=data.get("window_business_days", 10),
        duration_minutes=data.get("duration_minutes", 60),
        required=tuple(data.get("required", ())),
        optional=tuple(data.get("optional", ())),
    )


def _default_fixture(input_path: Path) -> Path:
    stem = input_path.stem.replace("_input", "")
    return Path("tests/fixtures") / f"{stem}_extraction.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="path to a run definition JSON file")
    parser.add_argument(
        "--live",
        action="store_true",
        help="call the configured gateway instead of replaying a fixture",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="recorded extraction to replay (default: tests/fixtures/<name>_extraction.json)",
    )
    args = parser.parse_args(argv)

    backend: Extractor = (
        LiveExtractor()
        if args.live
        else FixtureExtractor(args.fixture or _default_fixture(args.input))
    )

    print(render(run_pipeline(load_run(args.input), backend)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
