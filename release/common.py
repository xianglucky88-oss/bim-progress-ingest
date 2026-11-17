"""Shared helpers for the deterministic release tooling.

Stdlib-only and Python 3.9+ compatible so the whole release flow runs on an
offline CI runner or a field machine without pip.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

# Repository root: release/common.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
FIXTURES_DIR = REPO_ROOT / "fixtures"
TESTS_DIR = REPO_ROOT / "tests"

# Fixed build clock. All archive entry mtimes and the manifest's build epoch
# derive from SOURCE_DATE_EPOCH so wall-clock and timezone never affect bytes.
DEFAULT_SOURCE_DATE_EPOCH = 1793491200  # 2026-11-01T00:00:00Z

# Fixture directories allowed inside a release archive. Everything else
# (notably fixtures/corrupt) is excluded by construction.
PACKAGED_FIXTURE_DIRS = ("", "empty", "revisions")


def source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw:
        return int(raw)
    return DEFAULT_SOURCE_DATE_EPOCH


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, no whitespace variation."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_project_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("could not read project version from pyproject.toml")


def log(level: str, message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] {level:<5} {message}", flush=True)


def info(message: str) -> None:
    log("INFO", message)


def warn(message: str) -> None:
    log("WARN", message)


def fail(message: str, code: int = 1) -> "NoReturn":  # pragma: no cover - control flow
    log("FAIL", message)
    raise SystemExit(code)


def isolated_env() -> dict[str, str]:
    """Environment that pins UTC, disables caches and ignores host overrides."""
    env = dict(os.environ)
    env["TZ"] = "UTC"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def find_python_runtimes() -> list[str]:
    """Discover available CPython interpreters, newest first, de-duplicated."""
    candidates = [sys.executable]
    for minor in range(13, 8, -1):
        candidates.append(f"python3.{minor}")
    candidates.append("python3")
    seen: set[str] = set()
    runtimes: list[str] = []
    for candidate in candidates:
        try:
            completed = subprocess.run(
                [candidate, "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                timeout=30,
                env=isolated_env(),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        resolved = Path(completed.stdout.strip()).resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        runtimes.append(key)
    return runtimes


def make_clean_venv(python_exe: str, venv_dir: Path) -> str:
    """Create an isolated venv without pip (works on offline machines)."""
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [python_exe, "-m", "venv", "--without-pip", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
        env=isolated_env(),
    )
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    return str(bin_dir / ("python.exe" if os.name == "nt" else "python"))


def run_command(
    cmd: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int = 300,
    check: bool = False,
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=env or isolated_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(cmd)}\n"
            f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
        )
    return completed
