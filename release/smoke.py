#!/usr/bin/env python3
"""Smoke checks for a source tree or an unpacked release bundle.

Every check runs the CLI through `python -I` in a clean, cache-free
environment and from a neutral working directory, proving:

  * repeated runs on the same input produce byte-identical output
    (duplicate reporting is idempotent);
  * missing updates are surfaced, including a no-updates-at-all input;
  * the empty fixture is handled without a crash;
  * changing the working directory never changes the result;
  * a corrupt fixture is isolated to its own check and never aborts the run;
  * every packaged model revision sample validates and runs;
  * timezone and hash-seed changes do not affect output bytes.

Exit code is non-zero if any check fails; the machine-readable report is
written to ``--report`` when provided.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Allow `python -I release/smoke.py ...` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    REPO_ROOT,
    isolated_env,
    read_json,
    run_command,
    sha256_bytes,
)


class SmokeTarget:
    """How to invoke the CLI for one deployable layout."""

    def __init__(self, name: str, root: Path, launch_script: Path):
        self.name = name
        self.root = root
        self.launch_script = launch_script

    @property
    def fixtures(self) -> Path:
        return self.root / "fixtures"

    def cmd(self, python_exe: str, *extra: str) -> list[str]:
        # -B: never write .pyc (PYTHONDONTWRITEBYTECODE is ignored under -I).
        return [python_exe, "-I", "-B", str(self.launch_script), *extra]


def make_targets(root: Path) -> list[SmokeTarget]:
    """Return runnable targets found below ``root`` (bundle or source tree)."""
    targets: list[SmokeTarget] = []
    launch = root / "launch.py"
    if launch.is_file():
        targets.append(SmokeTarget("bundle", root, launch))
    repo_launch = root / "release" / "launcher.py"
    if repo_launch.is_file() and (root / "src").is_dir():
        targets.append(SmokeTarget("source-tree", root, repo_launch))
    if not targets:
        raise SystemExit(f"no launch entry point found under {root}")
    return targets


class CheckRecorder:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def record(self, name: str, ok: bool, detail: str, **extra: object) -> bool:
        entry = {"check": name, "ok": bool(ok), "detail": detail, "elapsed_ms": None}
        started = time.monotonic()
        entry.update(extra)
        entry["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        self.results.append(entry)
        return ok

    @property
    def passed(self) -> bool:
        return all(item["ok"] for item in self.results)


def _run_cli(
    target: SmokeTarget,
    python_exe: str,
    *extra: str,
    cwd: Path | None = None,
    env_overrides: dict | None = None,
) -> tuple[int, str, str]:
    env = isolated_env()
    if env_overrides:
        env.update(env_overrides)
    completed = run_command(target.cmd(python_exe, *extra), cwd=cwd, env=env, timeout=120)
    return completed.returncode, completed.stdout, completed.stderr


def run_smoke(target: SmokeTarget, python_exe: str, scratch: Path) -> CheckRecorder:
    recorder = CheckRecorder()
    work = scratch / target.name
    work.mkdir(parents=True, exist_ok=True)
    fixtures = target.fixtures
    canonical = fixtures / "input.json"
    empty = fixtures / "empty" / "input.json"

    # 1. Canonical run succeeds and matches the pinned domain result.
    code, out, err = _run_cli(target, python_exe, str(canonical), cwd=work)
    expected = {
        "project": "bim-progress-ingest",
        "status": "ok",
        "component_count": 2,
        "planned": 150.0,
        "actual": 80.0,
        "progress_pct": 53.33,
        "missing_updates": [],
    }
    ok = code == 0 and json.loads(out) == expected
    recorder.record("canonical_fixture_result", ok, "pinned 53.33% progress result", stdout=out.strip())

    # 2. Duplicate reporting: two runs on identical input give identical bytes.
    code_a, out_a, _ = _run_cli(target, python_exe, str(canonical), cwd=work)
    code_b, out_b, _ = _run_cli(target, python_exe, str(canonical), cwd=work)
    recorder.record(
        "duplicate_report_byte_identical",
        code_a == 0 and code_b == 0 and out_a == out_b,
        f"sha256={sha256_bytes(out_a.encode('utf-8'))[:16]} repeated identically",
    )

    # 2b. Duplicated update rows inside one payload must not double-count.
    dedupe_payload = read_json(canonical)
    dedupe_payload["updates"] = dedupe_payload["updates"] + list(dedupe_payload["updates"])
    dedupe_path = work / "duplicated_updates.json"
    dedupe_path.write_text(json.dumps(dedupe_payload), encoding="utf-8")
    code, out_dedupe, _ = _run_cli(target, python_exe, str(dedupe_path), cwd=work)
    # min(planned, actual) per guid makes duplicates harmless: same totals.
    recorder.record(
        "duplicate_update_rows_idempotent",
        code == 0 and json.loads(out_dedupe) == expected,
        "repeated update rows do not change totals",
    )

    # 3. Missing updates: G2 update removed, plus a payload with no updates.
    missing_payload = read_json(canonical)
    missing_payload["updates"] = [u for u in missing_payload["updates"] if u["guid"] != "G2"]
    missing_path = work / "missing_updates.json"
    missing_path.write_text(json.dumps(missing_payload), encoding="utf-8")
    code, out_missing, _ = _run_cli(target, python_exe, str(missing_path), cwd=work)
    parsed = json.loads(out_missing) if code == 0 else {}
    recorder.record(
        "missing_update_is_listed",
        code == 0 and parsed.get("missing_updates") == ["G2"] and parsed.get("actual") == 60.0,
        "G2 appears in missing_updates and contributes zero actual",
    )
    no_updates_payload = {"components": read_json(canonical)["components"], "updates": []}
    no_updates_path = work / "no_updates.json"
    no_updates_path.write_text(json.dumps(no_updates_payload), encoding="utf-8")
    code, out_none, _ = _run_cli(target, python_exe, str(no_updates_path), cwd=work)
    parsed_none = json.loads(out_none) if code == 0 else {}
    recorder.record(
        "no_updates_at_all",
        code == 0 and parsed_none.get("missing_updates") == ["G1", "G2"] and parsed_none.get("actual") == 0.0,
        "all guids reported missing when no updates are supplied",
    )

    # 4. Empty input is a valid zero-state result, not an error.
    code, out_empty, err_empty = _run_cli(target, python_exe, str(empty), cwd=work)
    parsed_empty = json.loads(out_empty) if code == 0 else {}
    recorder.record(
        "empty_input_zero_state",
        code == 0
        and parsed_empty.get("status") == "ok"
        and parsed_empty.get("component_count") == 0
        and parsed_empty.get("progress_pct") == 0
        and parsed_empty.get("missing_updates") == [],
        err_empty.strip() or "empty components list yields zero-state envelope",
    )

    # 5. Working-directory switch: run from /tmp and from the bundle root with
    #    the same absolute input; outputs must be byte-identical and contain
    #    no path or cwd leakage.
    code_tmp, out_tmp, _ = _run_cli(target, python_exe, str(canonical), cwd=Path(tempfile.gettempdir()))
    code_root, out_root, _ = _run_cli(target, python_exe, str(canonical), cwd=target.root)
    leak = str(target.root) in out_tmp or str(work) in out_tmp
    recorder.record(
        "working_directory_switch_invariant",
        code_tmp == 0 and code_root == 0 and out_tmp == out_root and not leak,
        "output identical from /tmp and target root; no absolute paths embedded",
    )

    # 5b. Default input (no positional arg) resolves from package location,
    #     not from cwd — must work even when cwd has no fixtures.
    code_default, _, _ = _run_cli(target, python_exe, cwd=work)
    recorder.record(
        "default_input_independent_of_cwd",
        code_default == 0,
        "CLI default fixture resolves without referencing the working directory",
    )

    # 6. Corrupt inputs are isolated: non-zero exit + structured error, while
    #    the overall smoke run continues and its prior checks stay valid.
    corrupt_dir = work / "corrupt"
    corrupt_source = REPO_ROOT / "fixtures" / "corrupt"
    isolated_failures = 0
    if corrupt_source.is_dir():
        corrupt_dir.mkdir(exist_ok=True)
        for corrupt in sorted(corrupt_source.glob("*.json")):
            staged = corrupt_dir / corrupt.name
            shutil.copyfile(corrupt, staged)
            code_bad, _, err_bad = _run_cli(target, python_exe, str(staged), cwd=work)
            try:
                err_payload = json.loads(err_bad)
            except json.JSONDecodeError:
                err_payload = {}
            if code_bad == 2 and err_payload.get("status") == "error":
                isolated_failures += 1
        recorder.record(
            "corrupt_fixture_isolated",
            isolated_failures == len(list(corrupt_source.glob("*.json"))),
            f"{isolated_failures} corrupt fixture(s) rejected with structured errors; run continued",
        )
    else:
        recorder.record("corrupt_fixture_isolated", False, "fixtures/corrupt not found in source tree")

    # 6b. Missing input file is an isolated operational error, not a crash.
    code_missing, _, err_missing = _run_cli(
        target, python_exe, str(work / "does_not_exist.json"), cwd=work
    )
    try:
        missing_err = json.loads(err_missing)
    except json.JSONDecodeError:
        missing_err = {}
    recorder.record(
        "missing_input_file_is_error",
        code_missing == 2 and missing_err.get("error") == "input_not_found",
        "absent input returns exit 2 with an error envelope",
    )

    # 7. Model revision samples: metadata hash validation + distinct outputs.
    revisions_dir = fixtures / "revisions"
    revision_outputs: dict[str, str] = {}
    revision_failures: list[str] = []
    if revisions_dir.is_dir():
        for revision_dir in sorted(p for p in revisions_dir.iterdir() if p.is_dir()):
            revision_id = revision_dir.name
            code_rev, out_rev, err_rev = _run_cli(
                target, python_exe, "--revision", revision_id, cwd=work
            )
            if code_rev != 0:
                revision_failures.append(f"{revision_id}: {err_rev.strip()}")
                continue
            revision_outputs[revision_id] = sha256_bytes(out_rev.encode("utf-8"))
        distinct = len(set(revision_outputs.values())) == len(revision_outputs) and len(revision_outputs) >= 2
        recorder.record(
            "model_revision_samples",
            not revision_failures and distinct,
            "; ".join(revision_failures) or f"{len(revision_outputs)} revisions validated with distinct outputs",
            revisions=revision_outputs,
        )
        # Tamper test: alter a *copy* of a revision catalog and confirm hash
        # validation rejects it, proving a corrupt revision cannot silently pass.
        tampered_root = work / "tampered-revisions"
        if revisions_dir.is_dir():
            sample = sorted(p for p in revisions_dir.iterdir() if p.is_dir())[0]
            target_copy = tampered_root / sample.name
            shutil.copytree(sample, target_copy)
            input_copy = target_copy / "input.json"
            input_copy.write_text(input_copy.read_text(encoding="utf-8").replace("G1", "G9"), encoding="utf-8")
            validate_script = work / "validate_tampered_revision.py"
            validate_script.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(target.root / 'src')!r})\n"
                "from bim_progress_ingest.revisions import load_revision, RevisionError\n"
                f"try:\n    load_revision({sample.name!r}, root={str(tampered_root)!r})\n"
                "except RevisionError:\n    print('REJECTED')\n"
                "else:\n    raise SystemExit(3)\n",
                encoding="utf-8",
            )
            completed = run_command(
                [python_exe, "-I", "-B", str(validate_script)], cwd=work, env=isolated_env(), timeout=60
            )
            recorder.record(
                "tampered_revision_rejected",
                completed.returncode == 0 and "REJECTED" in completed.stdout,
                (completed.stderr or completed.stdout).strip()[:200],
            )
    else:
        recorder.record("model_revision_samples", False, "no revisions catalog in target")

    # 8. Timezone and hash-seed invariance.
    tz_variants = [
        {"TZ": "America/New_York"},
        {"TZ": "Pacific/Kiritimati"},
        {"TZ": "UTC", "PYTHONHASHSEED": "12345"},
    ]
    invariant = True
    baseline = out_tmp
    for overrides in tz_variants:
        code_v, out_v, _ = _run_cli(target, python_exe, str(canonical), cwd=work, env_overrides=overrides)
        if code_v != 0 or out_v != baseline:
            invariant = False
    recorder.record(
        "timezone_hashseed_invariant",
        invariant,
        "output unchanged across TZ=America/New_York, Pacific/Kiritimati and PYTHONHASHSEED variation",
    )

    # 9. No bytecode caches may be written into the target tree during smoke.
    pyc_files = [p for p in target.root.rglob("*.pyc") if "__pycache__" in p.parts]
    recorder.record(
        "no_bytecode_cache_written",
        not pyc_files,
        "PYTHONDONTWRITEBYTECODE kept the target tree free of __pycache__",
    )

    return recorder


def main() -> int:
    parser = argparse.ArgumentParser(description="Run release smoke checks")
    parser.add_argument("root", nargs="?", default=str(REPO_ROOT), help="Source tree or unpacked bundle root")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter to test")
    parser.add_argument("--report", help="Write a JSON report to this path")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="bim-smoke-"))
    all_records: list[dict] = []
    overall_ok = True
    try:
        for target in make_targets(root):
            recorder = run_smoke(target, args.python, scratch)
            for item in recorder.results:
                item["target"] = target.name
            all_records.extend(recorder.results)
            overall_ok = overall_ok and recorder.passed
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    report = {
        "schema_version": 1,
        "python": args.python,
        "root": str(root),
        "summary": {
            "total": len(all_records),
            "passed": sum(1 for r in all_records if r["ok"]),
            "failed": sum(1 for r in all_records if not r["ok"]),
        },
        "checks": all_records,
    }
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in all_records:
        marker = "PASS" if item["ok"] else "FAIL"
        print(f"{marker} [{item['target']}] {item['check']}: {item['detail']}")
    print(f"smoke summary: {report['summary']['passed']}/{report['summary']['total']} passed")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
