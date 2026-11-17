"""Durable, crash-safe workspace storage.

Design summary
--------------
Everything an ingest touches is *content addressed*.  A blob is written once
under ``blobs/<kind>/<sha256>.jsonl.z`` (zlib-compressed JSON Lines) or
``blobs/<kind>/<sha256>.json`` for small objects.  Identical input therefore
deduplicates automatically: re-parsing the same model revision produces the
same blob names and cannot create a second copy of the facts.

Commits are single atomic pointer swaps of ``state.json``:

1. ``begin`` writes a write-ahead journal describing the intended generation.
2. Blobs referenced by the transaction are streamed to disk first.  They are
   invisible to readers until the state pointer moves.
3. ``commit`` writes ``state.json`` through a temporary file, fsyncs it and
   ``os.replace``-es it into place.
4. The journal is removed.

A crash can therefore leave one of two worlds: the old generation with a stale
journal (roll back) or the new generation (roll forward).  Either way no
progress is counted twice and no partially written revision is visible.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

from .domain import IngestError, canonical_json

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
_CHUNK = 1024 * 1024


class StorageError(RuntimeError):
    """Raised for corrupt or unreadable workspace state."""


class LockTimeout(StorageError):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iter_jsonl(blob: bytes) -> Iterator[dict[str, Any]]:
    for line in blob.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)


class Workspace:
    """A local, single-writer workspace holding one or more projects."""

    def __init__(self, root: os.PathLike[str] | str, *, create: bool = True):
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.journal_path = self.root / "journal.json"
        self.lock_path = self.root / "lock"
        self._state: Optional[dict[str, Any]] = None
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "blobs").mkdir(exist_ok=True)
            (self.root / "revisions").mkdir(exist_ok=True)
            (self.root / "index").mkdir(exist_ok=True)
        self.last_recovery = self.recover()

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> dict[str, Any]:
        if self._state is None:
            self._state = self._load_state()
        return self._state

    def reload(self) -> dict[str, Any]:
        self._state = self._load_state()
        return self._state

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "generation": 0,
                "ingest_seq": 0,
                "projects": {},
            }
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"state.json is corrupt: {exc}") from exc
        if data.get("schema_version") != SCHEMA_VERSION:
            raise StorageError(
                f"unsupported workspace schema {data.get('schema_version')!r}"
            )
        return data

    def project_state(self, project: str) -> dict[str, Any]:
        projects = self.state.setdefault("projects", {})
        return projects.setdefault(
            project,
            {
                "revisions": [],
                "head_seq": 0,
                "reports": {},
                "plans": {},
                "bindings": {},
                "pending_deltas": [],
                "element_count": 0,
                "snapshot_interval": 5,
            },
        )

    def has_project(self, project: str) -> bool:
        return project in self.state.get("projects", {})

    # ------------------------------------------------------------------ locks

    @contextmanager
    def writer_lock(self, timeout: float = 10.0) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(
                            f"workspace {self.root} is locked by another writer"
                        )
                    time.sleep(0.02)
            # Reload inside the lock so we never commit over stale state.
            self._state = self._load_state()
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    # ------------------------------------------------------------------ blobs

    def _blob_path(self, kind: str, digest: str, compressed: bool) -> Path:
        suffix = ".jsonl.z" if compressed else ".json"
        return self.root / "blobs" / kind / f"{digest}{suffix}"

    def write_jsonl_blob(
        self, kind: str, records: Iterable[Mapping[str, Any]]
    ) -> tuple[str, int]:
        digest, count, _ = self.write_jsonl_blob_hashed(kind, records)
        return digest, count

    def write_jsonl_blob_hashed(
        self, kind: str, records: Iterable[Mapping[str, Any]]
    ) -> tuple[str, int, str]:
        """Stream records to a compressed content-addressed blob.

        Returns ``(blob_digest, record_count, content_digest)``.  The content
        digest hashes the *sorted* canonical records, so it identifies the
        record set regardless of input ordering -- useful for deciding whether
        a revision is genuinely new before writing anything else.  Memory use is
        bounded by one serialized record plus the zlib output buffer.
        """

        target_dir = self.root / "blobs" / kind
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=target_dir)
        hasher = hashlib.sha256()
        compressor = zlib.compressobj(6)
        count = 0
        record_digests: list[bytes] = []
        try:
            with os.fdopen(fd, "wb") as handle:
                def emit(chunk: bytes) -> None:
                    hasher.update(chunk)
                    out = compressor.compress(chunk)
                    if out:
                        handle.write(out)

                for record in records:
                    line = canonical_json(_plain(record)).encode("utf-8")
                    record_digests.append(hashlib.sha256(line).digest())
                    emit(line + b"\n")
                    count += 1
                out = compressor.flush()
                if out:
                    handle.write(out)
                handle.flush()
                os.fsync(handle.fileno())
            digest = hasher.hexdigest()
            final = self._blob_path(kind, digest, compressed=True)
            if final.exists():
                os.unlink(tmp_name)
            else:
                os.replace(tmp_name, final)
            # Order-independent identity of the record *set*: 32 bytes per
            # record is far cheaper than retaining whole records.
            record_digests.sort()
            content_hasher = hashlib.sha256()
            for item in record_digests:
                content_hasher.update(item)
            return digest, count, content_hasher.hexdigest()
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def write_json_blob(self, kind: str, payload: Any) -> str:
        data = canonical_json(_plain(payload)).encode("utf-8")
        digest = _sha256_bytes(data)
        target_dir = self.root / "blobs" / kind
        target_dir.mkdir(parents=True, exist_ok=True)
        final = self._blob_path(kind, digest, compressed=False)
        if final.exists():
            return digest
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=target_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, final)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return digest

    def read_jsonl_blob(self, kind: str, digest: str) -> Iterator[dict[str, Any]]:
        path = self._blob_path(kind, digest, compressed=True)
        if not path.exists():
            raise StorageError(f"missing blob {kind}/{digest}")
        with path.open("rb") as handle:
            decompressor = zlib.decompressobj()
            pending = b""
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                pending += decompressor.decompress(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    line = pending[:newline]
                    pending = pending[newline + 1 :]
                    if line:
                        yield json.loads(line)
            pending += decompressor.flush()
            for line in pending.splitlines():
                if line.strip():
                    yield json.loads(line)

    def read_json_blob(self, kind: str, digest: str) -> Any:
        path = self._blob_path(kind, digest, compressed=False)
        if not path.exists():
            raise StorageError(f"missing blob {kind}/{digest}")
        return json.loads(path.read_text(encoding="utf-8"))

    def blob_exists(self, kind: str, digest: str, compressed: bool = True) -> bool:
        return self._blob_path(kind, digest, compressed).exists()

    # --------------------------------------------------------------- revision

    def revision_descriptor_path(self, project: str, seq: int, digest: str) -> Path:
        safe = _safe_project(project)
        return self.root / "revisions" / safe / f"{seq:08d}-{digest[:12]}.json"

    def write_revision_descriptor(self, project: str, descriptor: Mapping[str, Any]) -> str:
        path = self.revision_descriptor_path(
            project, int(descriptor["seq"]), str(descriptor["content_hash"])
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, descriptor)
        return str(path.relative_to(self.root))

    def read_revision_descriptor(self, descriptor_rel: str) -> dict[str, Any]:
        return json.loads((self.root / descriptor_rel).read_text(encoding="utf-8"))

    # ------------------------------------------------------------- journaling

    def begin_transaction(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        if self.journal_path.exists():
            raise StorageError(
                "a previous transaction journal is still present; run recover()"
            )
        state = self.state
        journal = {
            "started_at": intent.get("started_at"),
            "expected_generation": state["generation"],
            "target_generation": state["generation"] + 1,
            "intent": dict(intent),
            "pid": os.getpid(),
        }
        _atomic_write_json(self.journal_path, journal)
        return journal

    def commit(self, new_state: dict[str, Any], journal: Mapping[str, Any]) -> None:
        if new_state["generation"] != int(journal["target_generation"]):
            raise StorageError("commit generation does not match journal target")
        if self.state["generation"] != int(journal["expected_generation"]):
            raise StorageError("state changed underneath this transaction")
        new_state["ingest_seq"] = int(new_state["ingest_seq"])
        _atomic_write_json(self.state_path, new_state)
        self._state = new_state
        self._clear_journal()

    def abort(self) -> None:
        self._clear_journal()

    def _clear_journal(self) -> None:
        try:
            self.journal_path.unlink()
        except FileNotFoundError:
            pass
        self._fsync_dir(self.root)

    def recover(self) -> dict[str, Any]:
        """Roll a crashed transaction forward or back.

        Returns a report describing what happened.  This is safe to call at any
        time and is a no-op when there is no journal.
        """

        report: dict[str, Any] = {"journal": False, "action": None, "generation": None}
        if not self.journal_path.exists():
            return report
        report["journal"] = True
        try:
            journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A torn journal can only mean the transaction never committed
            # (commit replaces state.json first, then deletes the journal).
            self.journal_path.unlink(missing_ok=True)
            report["action"] = "discarded-corrupt-journal"
            return report
        state_generation = self._load_state()["generation"]
        report["generation"] = state_generation
        if state_generation >= int(journal.get("target_generation", 0)):
            report["action"] = "rolled-forward"
        else:
            report["action"] = "rolled-back"
        self.journal_path.unlink(missing_ok=True)
        self._fsync_dir(self.root)
        self._state = self._load_state()
        return report

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:  # pragma: no cover - not supported everywhere
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def referenced_blobs(self) -> set[tuple[str, str, bool]]:
        refs: set[tuple[str, str, bool]] = set()
        for project, pstate in self.state.get("projects", {}).items():
            for rev in pstate.get("revisions", []):
                refs.add(("inputs", rev["input_blob"], True))
                if rev.get("element_blob"):
                    refs.add(("elements", rev["element_blob"], True))
                if rev.get("change_blob"):
                    refs.add(("changes", rev["change_blob"], True))
            for digest in pstate.get("snapshots", {}).values():
                refs.add(("snapshots", digest, True))
            for bucket in ("reports", "plans"):
                for entry in pstate.get(bucket, {}).values():
                    refs.add((bucket, entry["blob"], False))
        return refs

    def garbage_collect(self) -> dict[str, Any]:
        """Remove unreferenced blobs and stale temporary files."""

        referenced = self.referenced_blobs()
        removed = 0
        freed = 0
        for kind_dir in (self.root / "blobs").iterdir():
            if not kind_dir.is_dir():
                continue
            kind = kind_dir.name
            compressed = kind in {"inputs", "elements", "changes", "snapshots"}
            for path in kind_dir.iterdir():
                if path.name.startswith(".tmp-"):
                    freed += path.stat().st_size
                    path.unlink()
                    removed += 1
                    continue
                digest = path.name.split(".")[0]
                if (kind, digest, compressed) not in referenced:
                    freed += path.stat().st_size
                    path.unlink()
                    removed += 1
        return {"removed": removed, "freed_bytes": freed}


def _safe_project(project: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in project)
    if not cleaned or cleaned.startswith("."):
        raise IngestError(f"invalid project name {project!r}")
    return cleaned


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(_plain(payload)).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        Workspace._fsync_dir(path.parent)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
