"""Input validation and record extraction.

The legacy ``load_records`` entry point is retained unchanged.  The new
functions in this module feed the revision-aware ingester and are designed to
keep memory bounded for large component sets:

* a document may be a single JSON object or a JSON-Lines stream;
* a revision may inline ``components`` or point at a ``components_ref`` file
  (``.jsonl``) which is consumed one record at a time and never retained.

Anything that is parsed is validated eagerly; invalid documents raise
:class:`IngestError` before any state is mutated.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .domain import (
    Component,
    DailyReport,
    GuidRemap,
    IngestError,
    PlanVersion,
    RevisionHeader,
)


def load_records(payload: dict) -> list[dict]:
    rows = payload.get("components", [])
    if not isinstance(rows, list):
        raise ValueError("components must be a list")
    return [dict(row) for row in rows if isinstance(row, dict)]


# ---------------------------------------------------------------------------
# document reading


@dataclass(frozen=True)
class Document:
    """A parsed ingest document with lazily-readable component sources."""

    project: Optional[str]
    revisions: tuple[Mapping[str, Any], ...]
    reports: tuple[Mapping[str, Any], ...]
    plans: tuple[Mapping[str, Any], ...]
    base_dir: Path

    @property
    def is_empty(self) -> bool:
        return not (self.revisions or self.reports or self.plans)


_DOCUMENT_KEYS = {"project", "revisions", "reports", "plans", "format", "version"}


def _coerce_object(raw: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise IngestError(f"{label} entries must be objects")
    return raw


def parse_document_text(text: str, base_dir: Path) -> Document:
    """Parse a JSON document or a JSON-Lines stream into a :class:`Document`."""

    stripped = text.lstrip()
    if not stripped:
        raise IngestError("document is empty")
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            # A JSON-Lines document also starts with '{'; fall back only when
            # the content clearly spans multiple objects.
            if exc.pos and text.count("\n") > 0:
                return _parse_jsonl(text, base_dir)
            raise IngestError(f"invalid JSON document: {exc}") from exc
        return _document_from_object(payload, base_dir)
    return _parse_jsonl(text, base_dir)


def _parse_jsonl(text: str, base_dir: Path) -> Document:
    revisions: list[Mapping[str, Any]] = []
    reports: list[Mapping[str, Any]] = []
    plans: list[Mapping[str, Any]] = []
    project: Optional[str] = None
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IngestError(f"invalid JSON on line {number}: {exc}") from exc
        if not isinstance(entry, Mapping):
            raise IngestError(f"line {number} must be a JSON object")
        kind = entry.get("kind") or entry.get("type") or "revision"
        if kind == "revision":
            revisions.append(entry)
        elif kind in {"report", "daily_report"}:
            reports.append(entry)
        elif kind in {"plan", "wbs"}:
            plans.append(entry)
        elif kind == "project":
            project = entry.get("project")
        else:
            raise IngestError(f"line {number} has unknown kind {kind!r}")
    return Document(project, tuple(revisions), tuple(reports), tuple(plans), base_dir)


def _document_from_object(payload: Any, base_dir: Path) -> Document:
    if not isinstance(payload, Mapping):
        raise IngestError("document must be a JSON object")
    if "revisions" in payload or "reports" in payload or "plans" in payload:
        unknown = set(payload) - _DOCUMENT_KEYS
        if unknown:
            raise IngestError(f"unknown document keys: {sorted(unknown)}")
        project = payload.get("project")
        revisions = payload.get("revisions", []) or []
        reports = payload.get("reports", []) or []
        plans = payload.get("plans", []) or []
        for label, rows in (("revisions", revisions), ("reports", reports), ("plans", plans)):
            if not isinstance(rows, list):
                raise IngestError(f"{label} must be a list")
        return Document(
            project,
            tuple(_coerce_object(r, "revisions") for r in revisions),
            tuple(_coerce_object(r, "reports") for r in reports),
            tuple(_coerce_object(r, "plans") for r in plans),
            base_dir,
        )
    # Backwards-compatible single revision/report/plan shorthand.
    project = payload.get("project")
    if "components" in payload:
        return Document(project, (dict(payload),), (), (), base_dir)
    if "lines" in payload:
        return Document(project, (), (dict(payload),), (), base_dir)
    if "activities" in payload:
        return Document(project, (), (), (dict(payload),), base_dir)
    raise IngestError("document does not contain revisions, reports or plans")


def load_document(path: os.PathLike[str] | str) -> Document:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return parse_document_text(text, path.parent)


# ---------------------------------------------------------------------------
# component streams


def _resolve_source(ref: Mapping[str, Any], base_dir: Path) -> Iterator[Mapping[str, Any]]:
    inline = ref.get("components")
    ref_path = ref.get("components_ref") or ref.get("components_file")
    if inline is not None and ref_path is not None:
        raise IngestError("revision may not set both components and components_ref")
    if inline is not None:
        if not isinstance(inline, (list, tuple)):
            raise IngestError("revision components must be a list")
        for item in inline:
            if not isinstance(item, Mapping):
                raise IngestError("component must be an object")
            yield item
        return
    if ref_path is None:
        return
    if not isinstance(ref_path, str) or not ref_path:
        raise IngestError("components_ref must be a path string")
    target = Path(ref_path)
    if not target.is_absolute():
        target = base_dir / target
    if not target.exists():
        raise IngestError(f"components_ref does not exist: {target}")
    with target.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IngestError(f"invalid component JSON in {target} line {number}: {exc}") from exc
            if not isinstance(item, Mapping):
                raise IngestError(f"component in {target} line {number} must be an object")
            yield item


def iter_revision_components(
    revision: Mapping[str, Any], base_dir: Path
) -> Iterator[Component]:
    """Yield validated components for a revision, one at a time."""

    seen: dict[str, str] = {}
    duplicate_lines = 0
    for raw in _resolve_source(revision, base_dir):
        component = Component.from_mapping(raw)
        digest = component.digest()
        previous = seen.get(component.guid)
        if previous is None:
            seen[component.guid] = digest
            yield component
        elif previous == digest:
            duplicate_lines += 1
            continue
        else:
            raise IngestError(
                f"component {component.guid} appears twice in revision "
                f"{revision.get('revision_id') or revision.get('seq')} with different content"
            )


def count_components(revision: Mapping[str, Any], base_dir: Path) -> int:
    return sum(1 for _ in iter_revision_components(revision, base_dir))


def revision_header(revision: Mapping[str, Any], default_project: Optional[str]) -> RevisionHeader:
    return RevisionHeader.from_mapping(revision, default_project)


def parse_report(raw: Mapping[str, Any], default_project: Optional[str]) -> DailyReport:
    return DailyReport.from_mapping(raw, default_project)


def parse_plan(raw: Mapping[str, Any], default_project: Optional[str]) -> PlanVersion:
    return PlanVersion.from_mapping(raw, default_project)


def parse_remaps(revision: Mapping[str, Any]) -> tuple[GuidRemap, ...]:
    return RevisionHeader.from_mapping(revision).remaps
