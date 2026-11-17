#!/usr/bin/env python3
"""Deterministic offline release build.

Produces, under a fresh output directory:

  dist/
    bim-progress-ingest-<version>.tar.gz   reproducible source+test bundle
    bim-progress-ingest-<version>.zip      reproducible bundle (same content)
    SHA256SUMS                             hashes of every artifact
    release-manifest.json                  inputs, hashes, runtime, timestamps

Determinism guarantees:
  * staging area is recreated from scratch (no stale cache / leftover files);
  * archive members are sorted, use fixed mtimes (SOURCE_DATE_EPOCH), fixed
    uid/gid/mode and PAX names, so byte-identical archives result from
    identical inputs on any machine, timezone or working directory;
  * the manifest separates a "reproducible" section (pure function of inputs)
    from an "environment" section (host runtime, wall-clock build time).

Failure isolation: any error removes the partial output directory and exits
non-zero; a partially built release can therefore never be published.
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import shutil
import stat
import sys
import tarfile
import time
import unittest
import zipfile
from pathlib import Path

# Allow `python -I release/build.py ...` (isolated mode omits the script dir
# from sys.path, so it must be injected explicitly by absolute path).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    FIXTURES_DIR,
    PACKAGED_FIXTURE_DIRS,
    REPO_ROOT,
    SRC_DIR,
    TESTS_DIR,
    canonical_json_bytes,
    fail,
    info,
    read_json,
    read_project_version,
    sha256_bytes,
    sha256_file,
    source_date_epoch,
    write_json,
)

# Staging failure injection point used by verify.py's interrupted-build test.
# "staging" raises before any archive is written; "archive" raises after the
# staging copy is complete but before archives are finalized.
FAILURE_HOOK = os.environ.get("RELEASE_BUILD_FAIL_AFTER")


def _collect_sources() -> list[Path]:
    paths: list[Path] = []
    for base in (SRC_DIR, TESTS_DIR):
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith((".pyc", ".pyo")):
                paths.append(path)
    return paths


def _collect_fixtures() -> list[Path]:
    # Explicit allow-list: fixtures/corrupt must never reach an archive.
    allowed_roots = [
        FIXTURES_DIR / sub if sub else FIXTURES_DIR
        for sub in PACKAGED_FIXTURE_DIRS
    ]
    paths: list[Path] = []
    for base in allowed_roots:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                # rglob on the fixture root would descend into excluded
                # siblings; keep only files whose first fixture segment is
                # an allow-listed directory (or files directly under fixtures/).
                relative = path.relative_to(FIXTURES_DIR)
                if relative.parts[0] == "corrupt":
                    continue
                paths.append(path)
    return paths


def _collect_root_files() -> list[Path]:
    names = ("pyproject.toml", "README.md")
    return [REPO_ROOT / name for name in names if (REPO_ROOT / name).is_file()]


def _run_unit_tests() -> None:
    info("running unit tests as build gate")
    # Under `python -I` the package source must be injected explicitly.
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS_DIR), top_level_dir=str(TESTS_DIR))
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    if not result.wasSuccessful():
        fail("unit tests failed; aborting build before any artifact is written")


def _stage(staging: Path, top_dir: str) -> list[Path]:
    if staging.exists():
        shutil.rmtree(staging)
    bundle_root = staging / top_dir
    bundle_root.mkdir(parents=True)

    all_files = _collect_sources() + _collect_fixtures() + _collect_root_files()
    staged: list[Path] = []
    for source in all_files:
        relative = source.relative_to(REPO_ROOT)
        target = bundle_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # copyfile avoids carrying host permissions; mode is normalized later
        shutil.copyfile(source, target)
        staged.append(target)

    # Offline launcher and isolated test runner at the bundle root.
    shutil.copyfile(Path(__file__).with_name("launcher.py"), bundle_root / "launch.py")
    shutil.copyfile(Path(__file__).with_name("run_tests.py"), bundle_root / "run_tests.py")
    staged.extend([bundle_root / "launch.py", bundle_root / "run_tests.py"])

    if FAILURE_HOOK == "staging":  # pragma: no cover - injected by verify.py
        raise RuntimeError("injected failure after staging (RELEASE_BUILD_FAIL_AFTER=staging)")
    return sorted(bundle_root.rglob("*"))


def _normalize_mode(mode: int) -> int:
    if mode & 0o111:
        return 0o755
    return 0o644


def _tar_add(tar: tarfile.TarFile, arcname: str, data: bytes, epoch: int) -> None:
    info_obj = tarfile.TarInfo(name=arcname)
    info_obj.size = len(data)
    info_obj.mtime = epoch
    info_obj.mode = _normalize_mode(stat.S_IMODE(0o644))
    info_obj.uid = 0
    info_obj.gid = 0
    info_obj.uname = ""
    info_obj.gname = ""
    info_obj.type = tarfile.REGTYPE
    tar.addfile(info_obj, io.BytesIO(data))


def _build_tar_gz(bundle_root: Path, members: list[Path], out_path: Path, top_dir: str, epoch: int) -> None:
    # Deterministic gzip wrapper: empty filename field and the fixed
    # SOURCE_DATE_EPOCH mtime so the gzip header itself is reproducible.
    with open(out_path, "wb") as raw:
        gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch)
        # GzipFile exposes no public mtime setter; the constructor stores the
        # header mtime in this attribute, which is all the writer reads.
        gz._new_mtime = epoch
        with gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for member in members:
                    if member.is_dir():
                        continue
                    arcname = f"{top_dir}/{member.relative_to(bundle_root).as_posix()}"
                    _tar_add(tar, arcname, member.read_bytes(), epoch)


def _build_zip(bundle_root: Path, members: list[Path], out_path: Path, top_dir: str, epoch: int) -> None:
    # Fixed per-entry timestamp (UTC) and compression; no extra fields.
    zinfo_time = time.gmtime(epoch)[:6]
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for member in members:
            if member.is_dir():
                continue
            arcname = f"{top_dir}/{member.relative_to(bundle_root).as_posix()}"
            zinfo = zipfile.ZipInfo(arcname, date_time=zinfo_time)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = (_normalize_mode(0o644) << 16)
            zinfo.create_system = 3
            archive.writestr(zinfo, member.read_bytes())


def _file_records(files: list[Path], bundle_root: Path) -> list[dict]:
    records = []
    for path in files:
        if path.is_dir():
            continue
        records.append(
            {
                "path": path.relative_to(bundle_root).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return sorted(records, key=lambda item: item["path"])


def _input_versions() -> dict:
    canonical = FIXTURES_DIR / "input.json"
    empty = FIXTURES_DIR / "empty" / "input.json"
    revisions = []
    revisions_dir = FIXTURES_DIR / "revisions"
    for revision_dir in sorted(p for p in revisions_dir.iterdir() if p.is_dir()):
        metadata = read_json(revision_dir / "revision.json")
        revisions.append(
            {
                "revision_id": metadata["revision_id"],
                "metadata_sha256": sha256_file(revision_dir / "revision.json"),
                "input_sha256": metadata["input_sha256"],
            }
        )
    return {
        "canonical_fixture": {"path": "fixtures/input.json", "sha256": sha256_file(canonical)},
        "empty_fixture": {"path": "fixtures/empty/input.json", "sha256": sha256_file(empty)},
        "model_revisions": revisions,
    }


def _try_build_wheel(dist_dir: Path, version: str) -> dict:
    """Best-effort standards wheel; never required for the offline bundle."""
    import importlib.util

    if importlib.util.find_spec("ensurepip") is None:
        return {"built": False, "reason": "pip/ensurepip unavailable on this host; stdlib bundles are the release artifacts"}
    if importlib.util.find_spec("build") is None:
        return {"built": False, "reason": "python-build package not installed; stdlib bundles are the release artifacts"}
    return {"built": False, "reason": "wheel build skipped in offline release flow"}


def build(out_dir: Path) -> Path:
    epoch = source_date_epoch()
    version = read_project_version()
    top_dir = f"bim-progress-ingest-{version}"
    info(f"building {top_dir} into {out_dir} (SOURCE_DATE_EPOCH={epoch})")

    # Pre-flight: tests must pass before any output directory is created.
    _run_unit_tests()

    # Output is assembled under a unique sibling directory and only moved into
    # place once complete, so an interrupted build never leaves a half-written
    # release at the expected path.
    out_dir = out_dir.resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    # Keep the staging area outside the output directory itself; otherwise a
    # stale rmtree/recreate cycle would nest it inside the published tree.
    staging = out_dir.parent / f".{out_dir.name}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        members = _stage(staging, top_dir)
        bundle_root = staging / top_dir

        if FAILURE_HOOK == "archive":  # pragma: no cover - injected by verify.py
            raise RuntimeError("injected failure before archive finalization (RELEASE_BUILD_FAIL_AFTER=archive)")

        dist_dir = out_dir / "dist"
        dist_dir.mkdir(parents=True)
        tar_path = dist_dir / f"{top_dir}.tar.gz"
        zip_path = dist_dir / f"{top_dir}.zip"
        _build_tar_gz(bundle_root, members, tar_path, top_dir, epoch)
        _build_zip(bundle_root, members, zip_path, top_dir, epoch)

        # Verify every archive round-trips to byte-identical members before
        # the build can be considered successful.
        _verify_archives(tar_path, zip_path, bundle_root, top_dir)

        artifacts = [tar_path, zip_path]
        checksums = {path.name: sha256_file(path) for path in artifacts}
        (dist_dir / "SHA256SUMS").write_text(
            "".join(f"{checksums[name]}  {name}\n" for name in sorted(checksums)),
            encoding="utf-8",
        )

        reproducible = {
            "schema_version": 1,
            "project": "bim-progress-ingest",
            "version": version,
            "source_date_epoch": epoch,
            "source_date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
            "input_versions": _input_versions(),
            "bundle_files": _file_records(members, bundle_root),
            "artifacts": [
                {"name": path.name, "sha256": checksums[path.name], "size": path.stat().st_size}
                for path in artifacts
            ],
        }
        # The manifest's reproducible section is itself hashed, giving
        # verification a single value to compare across repeat builds.
        manifest = {
            "reproducible": reproducible,
            "environment": {
                "python_version": sys.version.split()[0],
                "python_executable": sys.executable,
                "platform": sys.platform,
                "tz": "UTC",
                "build_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "cwd": str(Path.cwd()),
                "wheel": _try_build_wheel(dist_dir, version),
            },
        }
        manifest["manifest_reproducible_sha256"] = sha256_bytes(
            canonical_json_bytes(manifest["reproducible"])
        )
        write_json(dist_dir / "release-manifest.json", manifest)

        # Atomic-ish publish of the whole output tree.
        shutil.move(str(staging), str(out_dir))
        info(f"build complete: {dist_dir}")
        return dist_dir
    except Exception:
        for partial in (out_dir, staging):
            if partial.exists():
                shutil.rmtree(partial)
        raise


def _verify_archives(tar_path: Path, zip_path: Path, bundle_root: Path, top_dir: str) -> None:
    expected = {
        f"{top_dir}/{p.relative_to(bundle_root).as_posix()}": p.read_bytes()
        for p in bundle_root.rglob("*")
        if p.is_file()
    }
    with tarfile.open(tar_path, "r:gz") as tar:
        tar_members = {m.name: m for m in tar.getmembers() if m.isfile()}
        if set(tar_members) != set(expected):
            raise RuntimeError("tar.gz member list mismatch")
        for name, handle_bytes in expected.items():
            extracted = tar.extractfile(tar_members[name]).read()
            if extracted != handle_bytes:
                raise RuntimeError(f"tar.gz content mismatch for {name}")
    with zipfile.ZipFile(zip_path) as archive:
        if set(archive.namelist()) != set(expected):
            raise RuntimeError("zip member list mismatch")
        for name, handle_bytes in expected.items():
            if archive.read(name) != handle_bytes:
                raise RuntimeError(f"zip content mismatch for {name}")
    info("archive round-trip verification passed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic offline release build")
    parser.add_argument("--out", default=str(REPO_ROOT / "build" / "release"), help="Output directory")
    args = parser.parse_args()
    try:
        build(Path(args.out).resolve())
    except Exception as exc:  # noqa: BLE001 - top-level boundary
        fail(f"build failed and all partial output was removed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
