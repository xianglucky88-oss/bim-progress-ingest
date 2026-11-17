"""Model revision sample catalog.

Revision samples are the on-disk representation of a model revision used by
the release and verification flow. They are addressed by *logical* ids only:
callers pass an explicit root directory, so absolute host paths never leak
into result documents and moving the working directory cannot change output.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class RevisionError(ValueError):
    """Raised when a revision sample or its metadata is invalid."""


@dataclass(frozen=True)
class Revision:
    revision_id: str
    input_path: Path
    input_sha256: str
    metadata: dict[str, Any]

    def load_input(self) -> dict[str, Any]:
        with self.input_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def revisions_root(root: Optional[Path] = None) -> Path:
    """Return the revision catalog root without touching the current dir."""
    if root is not None:
        return Path(root)
    # src/bim_progress_ingest/revisions.py -> repo root
    return Path(__file__).resolve().parents[2] / "fixtures" / "revisions"


def load_revision(revision_id: str, root: Optional[Path] = None) -> Revision:
    """Load and validate one revision sample.

    Validates metadata shape, presence of the input file and the pinned
    SHA-256. Any failure raises :class:`RevisionError` so callers can isolate
    the broken revision instead of corrupting a batch run.
    """
    base = revisions_root(root)
    directory = base / revision_id
    metadata_path = directory / "revision.json"
    if not metadata_path.is_file():
        raise RevisionError(f"revision metadata missing: {revision_id}")
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise RevisionError(f"revision metadata unreadable: {revision_id}: {exc}") from exc

    if not isinstance(metadata, dict):
        raise RevisionError(f"revision metadata must be an object: {revision_id}")
    if metadata.get("revision_id") != revision_id:
        raise RevisionError(f"revision_id mismatch in metadata: {revision_id}")
    input_name = metadata.get("input_file")
    if not isinstance(input_name, str) or not input_name or Path(input_name).is_absolute() or ".." in Path(input_name).parts:
        raise RevisionError(f"invalid input_file in revision: {revision_id}")

    input_path = directory / input_name
    if not input_path.is_file():
        raise RevisionError(f"revision input missing: {revision_id}/{input_name}")

    actual_hash = _sha256_file(input_path)
    pinned_hash = metadata.get("input_sha256")
    if not isinstance(pinned_hash, str) or pinned_hash != actual_hash:
        raise RevisionError(
            f"revision input hash mismatch for {revision_id}: "
            f"expected {pinned_hash!r}, computed {actual_hash}"
        )

    return Revision(
        revision_id=revision_id,
        input_path=input_path,
        input_sha256=actual_hash,
        metadata=metadata,
    )


def list_revisions(root: Optional[Path] = None) -> list[str]:
    """Return sorted revision ids in the catalog (directories with metadata)."""
    base = revisions_root(root)
    if not base.is_dir():
        return []
    ids = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "revision.json").is_file():
            ids.append(child.name)
    return ids


def load_all_revisions(root: Optional[Path] = None) -> list[Revision]:
    """Load every revision, collecting per-revision failures."""
    loaded: list[Revision] = []
    errors: list[str] = []
    for revision_id in list_revisions(root):
        try:
            loaded.append(load_revision(revision_id, root))
        except RevisionError as exc:
            errors.append(str(exc))
    if errors:
        raise RevisionError("; ".join(errors))
    return loaded
