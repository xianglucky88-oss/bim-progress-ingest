#!/usr/bin/env python3
"""Publish a verified build into an on-machine releases directory.

Layout on the target machine::

    <releases-root>/
        releases/<version>-<short-sha>/   unpacked, immutable release
        current -> releases/<version>-<short-sha>

Properties:
  * an unpacked release is verified against the manifest hashes before it is
    made current, so a corrupted download is isolated and never activated;
  * switching ``current`` is a single symlink replacement (atomic on POSIX);
  * old revisions are retained, which is what makes rollback a pointer change
    rather than a rebuild;
  * ``--dry-run`` performs every check without touching ``current``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    canonical_json_bytes,
    fail,
    info,
    read_json,
    sha256_bytes,
    sha256_file,
    warn,
)


def _verify_manifest(manifest: dict) -> None:
    reproducible = manifest["reproducible"]
    recomputed = sha256_bytes(canonical_json_bytes(reproducible))
    if recomputed != manifest["manifest_reproducible_sha256"]:
        raise RuntimeError("release-manifest.json failed its own reproducible hash check")
    for artifact in reproducible["artifacts"]:
        if artifact["name"].endswith((".tar.gz", ".zip")):
            continue
    info("manifest reproducible hash verified")


def _verify_unpacked(unpacked_root: Path, manifest: dict) -> None:
    by_path = {entry["path"]: entry for entry in manifest["reproducible"]["bundle_files"]}
    actual = {p.relative_to(unpacked_root).as_posix(): p for p in unpacked_root.rglob("*") if p.is_file()}
    missing = sorted(set(by_path) - set(actual))
    extra = sorted(set(actual) - set(by_path))
    if missing or extra:
        raise RuntimeError(f"unpacked tree mismatch; missing={missing} extra={extra}")
    for rel, entry in by_path.items():
        if sha256_file(actual[rel]) != entry["sha256"]:
            raise RuntimeError(f"hash mismatch after unpack: {rel}")
    info(f"unpacked tree verified against manifest ({len(by_path)} files)")


def publish(build_dist: Path, releases_root: Path, dry_run: bool = False) -> Path:
    manifest_path = build_dist / "release-manifest.json"
    if not manifest_path.is_file():
        fail(f"release-manifest.json missing in {build_dist}; refusing to publish")
    manifest = read_json(manifest_path)
    _verify_manifest(manifest)

    version = manifest["reproducible"]["version"]
    short_sha = manifest["manifest_reproducible_sha256"][:12]
    release_name = f"{version}-{short_sha}"
    target_dir = releases_root / "releases" / release_name
    current_link = releases_root / "current"

    tarball = build_dist / f"bim-progress-ingest-{version}.tar.gz"
    if not tarball.is_file():
        fail(f"release tarball missing: {tarball}")

    if target_dir.exists():
        warn(f"release already installed: {target_dir} (re-verifying in place)")
        _verify_unpacked(target_dir, manifest)
    else:
        with tempfile.TemporaryDirectory(prefix="bim-publish-") as tmp:
            tmp_path = Path(tmp)
            with tarfile.open(tarball, "r:gz") as tar:
                # Reject absolute paths and traversal members before extraction.
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in Path(member.name).parts:
                        fail(f"unsafe archive member rejected: {member.name}")
                tar.extractall(tmp_path)
            unpacked = tmp_path / f"bim-progress-ingest-{version}"
            _verify_unpacked(unpacked, manifest)
            if dry_run:
                info("dry-run: verification passed, not installing")
                return unpacked
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(unpacked), str(target_dir))

    # Publish the manifest and checksums next to the release for audit.
    audit_dir = target_dir / "RELEASE"
    audit_dir.mkdir(exist_ok=True)
    shutil.copyfile(manifest_path, audit_dir / "release-manifest.json")
    checksums = build_dist / "SHA256SUMS"
    if checksums.is_file():
        shutil.copyfile(checksums, audit_dir / "SHA256SUMS")

    if dry_run:
        info("dry-run: install verified, current symlink not changed")
        return target_dir

    new_link = releases_root / "current.new"
    if new_link.exists() or new_link.is_symlink():
        new_link.unlink()
    os.symlink(Path("releases") / release_name, new_link)
    os.replace(new_link, current_link)
    info(f"current -> {os.readlink(current_link)}")
    return target_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a verified release build")
    parser.add_argument("--dist", required=True, help="build dist directory containing release-manifest.json")
    parser.add_argument("--releases-root", required=True, help="machine-local releases directory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    publish(Path(args.dist).resolve(), Path(args.releases_root).resolve(), args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
