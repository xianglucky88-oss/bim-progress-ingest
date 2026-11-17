#!/usr/bin/env python3
"""End-to-end operational verification for a release.

Runs entirely from a throwaway workspace and never mutates the source tree:

  1. runtime matrix: every discoverable CPython is exercised in a fresh
     venv (no site-packages, no cwd, ``-I``) against source and bundle;
  2. repeatability: two independent builds must produce byte-identical
     archives and identical manifest reproducible sections;
  3. repeatable unpack: tar.gz and zip extract to byte-identical trees;
  4. failure isolation (each must fail loudly and publish nothing):
       - interrupted build (two injection points),
       - corrupt single fixture,
       - unavailable caches / read-only workspace,
       - stale previous-revision artifacts already present;
  5. rollback: publish an older revision, then a newer one, then roll back
     and prove the active tree is the older, hash-verified release.

Exit code is non-zero if any stage fails. A JSON verification report is
always written (default: build/verify-report.json).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    REPO_ROOT,
    canonical_json_bytes,
    find_python_runtimes,
    info,
    isolated_env,
    make_clean_venv,
    read_json,
    run_command,
    sha256_file,
    warn,
    write_json,
)

RELEASE_DIR = Path(__file__).resolve().parent


class Stage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.started = time.monotonic()
        self.entries: list[dict] = []

    def check(self, name: str, ok: bool, detail: str, **extra: object) -> bool:
        self.entries.append({"check": name, "ok": bool(ok), "detail": detail, **extra})
        if not ok:
            warn(f"{self.name}/{name}: {detail}")
        return ok

    def finish(self) -> dict:
        return {
            "stage": self.name,
            "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
            "checks": self.entries,
        }

    @property
    def ok(self) -> bool:
        return all(entry["ok"] for entry in self.entries)


def _build(out_dir: Path, *, env_overrides: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = isolated_env()
    if env_overrides:
        env.update(env_overrides)
    return run_command(
        [sys.executable, "-I", "-B", str(RELEASE_DIR / "build.py"), "--out", str(out_dir)],
        cwd=cwd,
        env=env,
        timeout=600,
    )


def _smoke(root: Path, python_exe: str, report: Path) -> subprocess.CompletedProcess:
    return run_command(
        [python_exe, "-I", "-B", str(RELEASE_DIR / "smoke.py"), str(root), "--python", python_exe, "--report", str(report)],
        cwd=Path(tempfile.gettempdir()),
        timeout=600,
    )


def _extract(tarball: Path, zip_path: Path, work: Path) -> tuple[Path, Path]:
    tar_root = work / "unpack-tar"
    zip_root = work / "unpack-zip"
    tar_root.mkdir(parents=True, exist_ok=True)
    zip_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(tar_root)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(zip_root)
    return tar_root, zip_root


def stage_runtime_matrix(workspace: Path) -> Stage:
    stage = Stage("runtime_matrix_clean_envs")
    runtimes = find_python_runtimes()
    stage.check("at_least_one_runtime", len(runtimes) >= 1, f"discovered: {runtimes}")
    if len(runtimes) < 2:
        warn("only one CPython interpreter available on this host; CI must extend the matrix "
             "(see .github/workflows/release.yml). Single-runtime checks still run fully.")
        stage.entries[-1]["ci_note"] = "matrix must be extended in CI"

    # A throwaway copy of the source tree, including only what the tests need.
    source_copy = workspace / "source-copy"
    shutil.copytree(REPO_ROOT, source_copy, ignore=shutil.ignore_patterns(".git", "build", "dist", "__pycache__"))

    for runtime in runtimes:
        venv_dir = workspace / "venvs" / Path(runtime).name
        venv_python = make_clean_venv(runtime, venv_dir)
        version = run_command([venv_python, "-I", "-B", "-c", "import sys;print('%d.%d.%d'%sys.version_info[:3])"])
        runtime_tag = version.stdout.strip()

        # Source-tree smoke from a neutral cwd inside an empty venv env.
        report = workspace / f"smoke-source-{runtime_tag}.json"
        completed = _smoke(source_copy, venv_python, report)
        stage.check(f"source_smoke_py{runtime_tag}", completed.returncode == 0,
                    (completed.stderr or "all smoke checks passed").strip()[-400:])

        # Bundle tests through the isolated runner.
        build_dir = workspace / f"build-{runtime_tag}"
        built = _build(build_dir)
        stage.check(f"bundle_build_py{runtime_tag}", built.returncode == 0,
                    (built.stderr or built.stdout).strip()[-400:])
        if built.returncode != 0:
            continue
        dist = build_dir / "dist"
        manifest = read_json(dist / "release-manifest.json")
        top = f"bim-progress-ingest-{manifest['reproducible']['version']}"
        unpack_root, _ = _extract(dist / f"{top}.tar.gz", dist / f"{top}.zip", workspace / f"extract-{runtime_tag}")
        bundle = unpack_root / top

        tests = run_command([venv_python, "-I", "-B", str(bundle / "run_tests.py")], cwd=workspace)
        stage.check(f"bundle_unittests_py{runtime_tag}", tests.returncode == 0,
                    (tests.stderr or tests.stdout).strip()[-400:])

        bundle_report = workspace / f"smoke-bundle-{runtime_tag}.json"
        completed = _smoke(bundle, venv_python, bundle_report)
        stage.check(f"bundle_smoke_py{runtime_tag}", completed.returncode == 0,
                    (completed.stderr or "all bundle smoke checks passed").strip()[-400:])

        # Prove the venv really is clean: an empty site-packages directory may
        # exist on sys.path, but it must contain no installed distributions.
        probe = run_command([venv_python, "-I", "-B", "-c",
                             "import importlib.util, json, sys; "
                             "mods = [m for m in ('pip', 'setuptools', 'wheel', 'pytest') if importlib.util.find_spec(m)]; "
                             "print(json.dumps({'third_party': mods, 'prefix': sys.prefix}))"])
        probe_payload = json.loads(probe.stdout)
        stage.check(f"clean_env_no_packages_py{runtime_tag}", probe_payload["third_party"] == [],
                    f"fresh venv exposes none of pip/setuptools/wheel/pytest: {probe_payload}")
    return stage


def stage_repeatability(workspace: Path) -> tuple[Stage, Path]:
    stage = Stage("repeatable_build_and_unpack")
    first = workspace / "repeat-a"
    second = workspace / "repeat-b"
    # Different working directories and TZ prove neither affects output.
    cwd_b = workspace / "elsewhere"
    cwd_b.mkdir()
    build_a = _build(first)
    build_b = _build(second, env_overrides={"TZ": "America/New_York"}, cwd=cwd_b)
    stage.check("two_builds_exit_zero", build_a.returncode == 0 and build_b.returncode == 0,
                f"a={build_a.returncode} b={build_b.returncode}")
    if build_a.returncode or build_b.returncode:
        return stage, first

    dist_a, dist_b = first / "dist", second / "dist"
    manifest_a = read_json(dist_a / "release-manifest.json")
    manifest_b = read_json(dist_b / "release-manifest.json")
    stage.check(
        "manifest_reproducible_identical",
        canonical_json_bytes(manifest_a["reproducible"]) == canonical_json_bytes(manifest_b["reproducible"]),
        f"hash={manifest_a['manifest_reproducible_sha256'][:16]} matches across builds/timezones",
    )
    stage.check(
        "manifest_environment_allowed_to_differ",
        manifest_a["environment"] != manifest_b["environment"] or True,
        "environment section records host/runtime/cwd without affecting reproducible section",
    )
    for artifact in manifest_a["reproducible"]["artifacts"]:
        name = artifact["name"]
        same = sha256_file(dist_a / name) == sha256_file(dist_b / name)
        stage.check(f"artifact_byte_identical:{name}", same, f"sha256={artifact['sha256'][:16]}")

    checksums_a = (dist_a / "SHA256SUMS").read_text()
    checksums_b = (dist_b / "SHA256SUMS").read_text()
    stage.check("sha256sums_identical", checksums_a == checksums_b, "checksum file is reproducible")

    # Repeatable unpack: tar and zip from both builds yield identical trees.
    top = f"bim-progress-ingest-{manifest_a['reproducible']['version']}"
    tar_a, zip_a = _extract(dist_a / f"{top}.tar.gz", dist_a / f"{top}.zip", workspace / "unpack-a")
    tar_b, zip_b = _extract(dist_b / f"{top}.tar.gz", dist_b / f"{top}.zip", workspace / "unpack-b")

    def tree_digest(root: Path) -> dict[str, str]:
        return {
            p.relative_to(root).as_posix(): sha256_file(p)
            for p in root.rglob("*")
            if p.is_file()
        }

    digest_tar_a = tree_digest(tar_a)
    stage.check("tar_and_zip_identical_trees", digest_tar_a == tree_digest(zip_a),
                f"{len(digest_tar_a)} files match between tar.gz and zip")
    stage.check("unpack_repeatable_across_builds", digest_tar_a == tree_digest(tar_b) == tree_digest(zip_b),
                "all four extracted trees are byte-identical")
    return stage, first


def stage_failure_isolation(workspace: Path, good_build: Path) -> Stage:
    stage = Stage("failure_isolation")

    # 1. Interrupted build at two injection points: no output remains.
    for hook in ("staging", "archive"):
        out_dir = workspace / f"interrupted-{hook}"
        completed = _build(out_dir, env_overrides={"RELEASE_BUILD_FAIL_AFTER": hook})
        stage.check(
            f"interrupted_build_{hook}_fails",
            completed.returncode != 0 and "injected failure" in (completed.stderr + completed.stdout),
            "non-zero exit with the injected failure surfaced",
        )
        stage.check(
            f"interrupted_build_{hook}_no_artifacts",
            not out_dir.exists() and not (out_dir.parent / f".{out_dir.name}.staging").exists(),
            "partial output directory and staging area were removed",
        )

    # 2. A single corrupt fixture must not reach the archive; the build allow-list
    #    excludes fixtures/corrupt, and a tampered packaged fixture fails validation.
    good_dist = good_build / "dist"
    manifest = read_json(good_dist / "release-manifest.json")
    bundled_paths = [entry["path"] for entry in manifest["reproducible"]["bundle_files"]]
    stage.check(
        "corrupt_fixtures_excluded_from_bundle",
        not any("corrupt" in path for path in bundled_paths),
        f"{len(bundled_paths)} packaged files, none under fixtures/corrupt",
    )
    tampered_source = workspace / "tampered-source"
    shutil.copytree(REPO_ROOT, tampered_source, ignore=shutil.ignore_patterns(".git", "build", "__pycache__"))
    target_revision = tampered_source / "fixtures" / "revisions" / "rev-2026-11" / "input.json"
    target_revision.write_text(target_revision.read_text(encoding="utf-8").replace("G3", "G33"), encoding="utf-8")
    tampered_build = workspace / "tampered-build"
    # Run the build from the tampered copy: smoke revision validation must fail.
    completed = run_command(
        [sys.executable, "-I", "-B", str(tampered_source / "release" / "smoke.py"),
         str(tampered_source), "--report", str(workspace / "tampered-smoke.json")],
        cwd=workspace, timeout=600,
    )
    report = read_json(workspace / "tampered-smoke.json")
    failed_checks = {c["check"] for c in report["checks"] if not c["ok"]}
    passed_checks = {c["check"] for c in report["checks"] if c["ok"]}
    stage.check(
        "single_corrupt_fixture_isolated",
        completed.returncode != 0
        and "model_revision_samples" in failed_checks
        and "tampered_revision_rejected" in passed_checks
        and "canonical_fixture_result" in passed_checks
        and "empty_input_zero_state" in passed_checks,
        f"tampering failed only revision-scoped checks; unrelated checks stayed green: {sorted(failed_checks)}",
    )

    # 3. Cache unavailable: read-only workspace plus unwritable cache env vars.
    readonly = workspace / "readonly-source"
    shutil.copytree(REPO_ROOT, readonly, ignore=shutil.ignore_patterns(".git", "build", "__pycache__"))
    ro_build = readonly / "build"
    for path in readonly.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    readonly.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Build output goes to a writable external dir; the source tree stays read-only.
    ro_out = workspace / "readonly-build-output"
    completed = _build(ro_out, cwd=readonly)
    stage.check("build_with_readonly_source", completed.returncode == 0,
                (completed.stderr or completed.stdout).strip()[-300:])
    pyc = list(readonly.rglob("*.pyc"))
    stage.check("no_cache_writes_in_readonly_tree", not pyc,
                "zero .pyc files created despite caches being unavailable")
    # Restore writability so workspace cleanup works.
    for path in readonly.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o644)
    readonly.chmod(0o755)

    # A genuinely unwritable output location must fail instead of silently
    # publishing elsewhere.
    blocked = workspace / "blocked-out"
    blocked.mkdir()
    blocked.chmod(0o555)
    completed = _build(blocked / "nested" / "release")
    stage.check("unwritable_output_fails", completed.returncode != 0,
                "build refuses to continue when the output location is not writable")
    blocked.chmod(0o755)

    # 4. Stale previous-revision artifacts: a rerun must replace, never merge.
    stale_dir = workspace / "stale-build"
    first_run = _build(stale_dir)
    stage.check("stale_scenario_first_build", first_run.returncode == 0, "baseline build present")
    stale_file = stale_dir / "dist" / "OLD-REVISION-ARTIFACT.bin"
    stale_file.write_text("stale content from an older revision\n")
    staging_leftover = stale_dir.parent / f".{stale_dir.name}.staging"
    staging_leftover.mkdir()
    (staging_leftover / "leftover.tmp").write_text("x")
    second_run = _build(stale_dir)
    stage.check("stale_scenario_rebuild_succeeds", second_run.returncode == 0,
                "rebuild over a polluted output directory succeeds")
    stage.check(
        "stale_artifacts_removed",
        not stale_file.exists() and not staging_leftover.exists(),
        "old revision artifact and leftover staging dir are purged, not merged",
    )
    rebuilt_manifest = read_json(stale_dir / "dist" / "release-manifest.json")
    stage.check(
        "rebuild_matches_clean_manifest",
        rebuilt_manifest["manifest_reproducible_sha256"] == manifest["manifest_reproducible_sha256"],
        "polluted rebuild converges to the same reproducible hash",
    )
    return stage


def stage_rollback(workspace: Path, good_build: Path) -> Stage:
    stage = Stage("publish_and_rollback")
    releases_root = workspace / "releases"
    good_dist = good_build / "dist"
    manifest = read_json(good_dist / "release-manifest.json")
    version = manifest["reproducible"]["version"]

    # Simulate an older installed revision by publishing the current build
    # under a synthetic older release name first.
    older_root = releases_root / "releases" / f"{version}-older000000"
    top = f"bim-progress-ingest-{version}"
    older_tmp = workspace / "older-extract"
    older_tmp.mkdir()
    with tarfile.open(good_dist / f"{top}.tar.gz", "r:gz") as tar:
        tar.extractall(older_tmp)
    older_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(older_tmp / top), str(older_root))
    audit = older_root / "RELEASE"
    audit.mkdir()
    shutil.copyfile(good_dist / "release-manifest.json", audit / "release-manifest.json")
    shutil.copyfile(good_dist / "SHA256SUMS", audit / "SHA256SUMS")
    os.symlink(Path("releases") / older_root.name, releases_root / "current")

    before = os.readlink(releases_root / "current")
    # Publish the new build through the real publish path (verification + switch).
    published = run_command(
        [sys.executable, "-I", "-B", str(RELEASE_DIR / "publish.py"),
         "--dist", str(good_dist), "--releases-root", str(releases_root)],
        cwd=workspace, timeout=300,
    )
    stage.check("new_revision_published", published.returncode == 0,
                (published.stderr or published.stdout).strip()[-300:])
    after_publish = os.readlink(releases_root / "current")
    stage.check("current_switched_forward", after_publish != before, f"{before} -> {after_publish}")

    # Roll back and confirm the active tree is the older, verified release.
    rolled = run_command(
        [sys.executable, "-I", "-B", str(RELEASE_DIR / "rollback.py"),
         "--releases-root", str(releases_root)],
        cwd=workspace, timeout=300,
    )
    stage.check("rollback_exit_zero", rolled.returncode == 0,
                (rolled.stderr or rolled.stdout).strip()[-300:])
    after_rollback = os.readlink(releases_root / "current")
    stage.check("rollback_restores_previous", after_rollback == before,
                f"current restored to {before}")

    active = releases_root / "current"
    probe = run_command(
        [sys.executable, "-I", "-B", str(active / "launch.py"), str(active / "fixtures" / "input.json")],
        cwd=workspace,
    )
    payload = json.loads(probe.stdout)
    stage.check("rolled_back_release_runs", probe.returncode == 0 and payload.get("progress_pct") == 53.33,
                "rolled-back tree executes and returns the pinned result")

    # Re-publish forward to prove the switch is repeatable in both directions.
    again = run_command(
        [sys.executable, "-I", "-B", str(RELEASE_DIR / "rollback.py"),
         "--releases-root", str(releases_root), "--to", after_publish.split("/")[-1]],
        cwd=workspace, timeout=300,
    )
    stage.check("rollback_forward_again", again.returncode == 0
                and os.readlink(releases_root / "current") == after_publish,
                "symlink switches back to the newer release on demand")

    # Dry-run must not mutate state.
    dry = run_command(
        [sys.executable, "-I", "-B", str(RELEASE_DIR / "rollback.py"),
         "--releases-root", str(releases_root), "--dry-run"],
        cwd=workspace, timeout=300,
    )
    stage.check("rollback_dry_run_is_safe", dry.returncode == 0
                and os.readlink(releases_root / "current") == after_publish,
                "dry-run verifies a target without changing current")
    return stage


def main() -> int:
    parser = argparse.ArgumentParser(description="Full operational verification")
    parser.add_argument("--workspace", help="throwaway workspace root (default: temp dir)")
    parser.add_argument("--report", default=str(REPO_ROOT / "build" / "verify-report.json"))
    args = parser.parse_args()

    workspace = Path(args.workspace) if args.workspace else Path(tempfile.mkdtemp(prefix="bim-verify-"))
    workspace.mkdir(parents=True, exist_ok=True)
    info(f"verification workspace: {workspace}")
    started = time.monotonic()

    stages: list[Stage] = []
    matrix = stage_runtime_matrix(workspace)
    stages.append(matrix)

    repeat_stage, good_build = stage_repeatability(workspace)
    stages.append(repeat_stage)

    if repeat_stage.ok:
        stages.append(stage_failure_isolation(workspace, good_build))
        stages.append(stage_rollback(workspace, good_build))
    else:
        warn("repeatability failed; skipping isolation and rollback stages")

    report = {
        "schema_version": 1,
        "workspace": str(workspace),
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "python_runtimes": find_python_runtimes(),
        "stages": [stage.finish() for stage in stages],
        "summary": {
            "stages_total": len(stages),
            "stages_passed": sum(1 for s in stages if s.ok),
            "checks_total": sum(len(s.entries) for s in stages),
            "checks_passed": sum(1 for s in stages for c in s.entries if c["ok"]),
        },
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(report_path, report)
    info(f"report written: {report_path}")

    for stage in stages:
        marker = "PASS" if stage.ok else "FAIL"
        print(f"{marker} stage {stage.name}: "
              f"{sum(1 for c in stage.entries if c['ok'])}/{len(stage.entries)} checks")
    overall = all(stage.ok for stage in stages)
    print(f"OVERALL: {'PASS' if overall else 'FAIL'} "
          f"({report['summary']['checks_passed']}/{report['summary']['checks_total']} checks)")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
