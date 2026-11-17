from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import run


def _default_input() -> Path:
    # Resolved from the module location, never from the current working
    # directory, so `cd` cannot change which input is processed.
    return Path(__file__).resolve().parents[2] / "fixtures" / "input.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Link BIM components to schedule activities and calculate revision-aware progress."
    )
    parser.add_argument("input", nargs="?", default=None, help="Path to an input JSON file")
    parser.add_argument("--revision", help="Run the packaged model revision sample with this id")
    args = parser.parse_args(argv)

    try:
        if args.revision:
            from .revisions import load_revision

            revision = load_revision(args.revision)
            input_path = revision.input_path
        else:
            input_path = Path(args.input) if args.input else _default_input()
        with input_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        result = run(payload)
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error": "input_not_found", "detail": str(exc)}), file=sys.stderr)
        return 2
    except (json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": "invalid_input", "detail": str(exc)}), file=sys.stderr)
        return 2

    # sort_keys + fixed indent + ensure_ascii=False gives byte-identical output
    # across machines and timezones; no timestamps or paths are emitted.
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
