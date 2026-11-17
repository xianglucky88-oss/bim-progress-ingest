#!/usr/bin/env python3
"""Run the unit-test suite against an unpacked release bundle.

Usable with `python -I` because it injects the bundle's `src/` directory by
absolute path instead of relying on PYTHONPATH or the current directory.
Exits non-zero on any test failure so CI and field verification treat test
failures as release-blocking.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parent
SRC = BUNDLE_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(BUNDLE_ROOT / "tests"), top_level_dir=str(BUNDLE_ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
