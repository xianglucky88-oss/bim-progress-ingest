#!/usr/bin/env python3
"""Offline entry point for an unpacked release bundle (and the source tree).

This launcher makes `src/` importable by absolute path, so it works under
`python -I` (which ignores PYTHONPATH and the current directory) and from any
working directory. The CLI default input resolves relative to the package,
never relative to the caller's cwd.

Two supported layouts are detected automatically:
  * bundle:   <root>/launch.py + <root>/src/bim_progress_ingest/
  * source:   <root>/release/launcher.py + <root>/src/bim_progress_ingest/
"""

from __future__ import annotations

import sys
from pathlib import Path

here = Path(__file__).resolve().parent
candidates = [here / "src", here.parent / "src"]
for candidate in candidates:
    if (candidate / "bim_progress_ingest").is_dir():
        src_dir = candidate
        break
else:  # pragma: no cover - operational misconfiguration
    raise SystemExit(f"could not locate src/ next to launcher {here}")
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from bim_progress_ingest.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
