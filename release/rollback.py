#!/usr/bin/env python3
"""Roll the ``current`` symlink back to a previously installed release.

Rollback never rebuilds anything: it only switches between complete,
hash-verified release trees retained under ``releases/``. A target revision is
re-verified against its own manifest before the symlink moves, so activating a
corrupted old tree is impossible.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import canonical_json_bytes, fail, info, read_json, sha256_bytes, sha256_file, warn  # noqa: E402


def list_releases(releases_root: Path) -> list[Path]:
    store = releases_root / "releases"
    if not store.is_dir():
        return []
    return sorted(p for p in store.iterdir() if p.is_dir())


def _verify(release_dir: Path) -> bool:
    manifest_path = release_dir / "RELEASE" / "release-manifest.json"
    if not manifest_path.is_file():
        warn(f"no manifest in {release_dir}; cannot verify")
        return False
    manifest = read_json(manifest_path)
    if sha256_bytes(canonical_json_bytes(manifest["reproducible"])) != manifest["manifest_reproducible_sha256"]:
        warn(f"manifest hash mismatch in {release_dir}")
        return False
    for entry in manifest["reproducible"]["bundle_files"]:
        candidate = release_dir / entry["path"]
        if not candidate.is_file() or sha256_file(candidate) != entry["sha256"]:
            warn(f"file verification failed: {entry['path']}")
            return False
    return True


def rollback(releases_root: Path, target: str | None = None, dry_run: bool = False) -> Path:
    releases = list_releases(releases_root)
    if len(releases) < 1:
        fail(f"no installed releases under {releases_root}")

    current_link = releases_root / "current"
    current_target = os.readlink(current_link) if current_link.is_symlink() else None

    if target is None:
        # Default: newest verified release that is not the current one.
        candidates = [r for r in reversed(releases) if r.name != (current_target or "").split("/")[-1]]
        if not candidates:
            fail("only one release installed; nothing to roll back to")
    else:
        candidates = [r for r in releases if r.name == target or r.name.startswith(target)]
        if not candidates:
            fail(f"no release matching {target!r}; installed: {[r.name for r in releases]}")

    chosen = None
    for candidate in candidates:
        if _verify(candidate):
            chosen = candidate
            break
    if chosen is None:
        fail("no hash-verified candidate available for rollback")

    info(f"rollback target verified: {chosen.name}")
    if dry_run:
        info("dry-run: symlink not changed")
        return chosen

    new_link = releases_root / "current.new"
    if new_link.exists() or new_link.is_symlink():
        new_link.unlink()
    os.symlink(Path("releases") / chosen.name, new_link)
    os.replace(new_link, current_link)
    info(f"current -> {os.readlink(current_link)} (was {current_target})")
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description="Roll back to a previous release")
    parser.add_argument("--releases-root", required=True)
    parser.add_argument("--to", help="release name or prefix; defaults to the previous verified release")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true", help="list installed releases and exit")
    args = parser.parse_args()

    releases_root = Path(args.releases_root).resolve()
    if args.list:
        for release_dir in list_releases(releases_root):
            print(release_dir.name, "VERIFIED" if _verify(release_dir) else "UNVERIFIED")
        return 0
    rollback(releases_root, args.to, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
