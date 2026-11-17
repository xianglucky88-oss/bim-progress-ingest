"""Read-side API: as-of queries with full provenance.

Every query pins two coordinates and resolves them explicitly:

``revision``
    ``None`` means the current head; a number or revision id selects an
    historical model revision.  Revisions added later can never change the
    answer for an already-resolved revision.

``as_of``
    A date selecting which daily reports are in effect.  Only reports with
    ``effective_date <= as_of`` participate, selected by
    ``(effective_date, ingest_seq)``.  A late-arriving report changes only the
    date range it covers.

Both are echoed in the result next to the revision's content hash, and every
element/activity value carries report evidence so an auditor can trace it back
to the exact model revision blob and the original upload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional

from .domain import IngestError, parse_date
from .history import TableState, load_table_at
from .progress import ProgressResult, ReportVersion, evaluate
from .storage import Workspace


def _report_versions(pstate: Mapping[str, Any]) -> list[ReportVersion]:
    versions: list[ReportVersion] = []
    for report_id, bucket in pstate.get("reports", {}).items():
        for record in bucket.get("versions", []):
            versions.append(
                ReportVersion(
                    report_id=report_id,
                    version=int(record["version"]),
                    effective_date=parse_date(record["effective_date"]),
                    ingested_at=record["ingested_at"],
                    ingest_seq=int(record["ingest_seq"]),
                    content_hash=record["content_hash"],
                    lines=tuple(_line_from(entry) for entry in record["lines"]),
                    source=record.get("source"),
                )
            )
    return versions


class _LineView:
    __slots__ = ("guid", "completed", "fraction", "unit", "note")

    def __init__(self, raw: Mapping[str, Any]):
        self.guid = raw["guid"]
        self.completed = raw.get("completed")
        self.fraction = raw.get("fraction")
        self.unit = raw.get("unit", "")
        self.note = raw.get("note")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"guid": self.guid}
        if self.completed is not None:
            data["completed"] = self.completed
        if self.fraction is not None:
            data["fraction"] = self.fraction
        if self.unit:
            data["unit"] = self.unit
        if self.note is not None:
            data["note"] = self.note
        return data


def _line_from(raw: Mapping[str, Any]) -> _LineView:
    return _LineView(raw)


class QueryService:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    # ------------------------------------------------------------- resolution

    def projects(self) -> list[str]:
        return sorted(self.workspace.state.get("projects", {}))

    def _project_state(self, project: str) -> Mapping[str, Any]:
        if project not in self.workspace.state.get("projects", {}):
            raise IngestError(f"unknown project {project!r}")
        return self.workspace.state["projects"][project]

    def revision_index(self, project: str) -> list[dict[str, Any]]:
        pstate = self._project_state(project)
        return [
            {
                "seq": int(rev["seq"]),
                "revision_id": rev["revision_id"],
                "content_hash": rev["content_hash"],
                "effective_at": rev.get("effective_at"),
                "ingested_at": rev.get("ingested_at"),
                "ingest_seq": rev.get("ingest_seq"),
                "mode": rev.get("mode"),
                "counts": rev.get("counts"),
            }
            for rev in pstate.get("revisions", [])
        ]

    def resolve_revision(self, project: str, revision: Any) -> tuple[dict[str, Any], int]:
        pstate = self._project_state(project)
        revisions = pstate.get("revisions", [])
        if not revisions:
            raise IngestError(f"project {project!r} has no revisions")
        if revision is None or revision == "head":
            rev = revisions[-1]
            return rev, int(rev["seq"])
        if isinstance(revision, bool):
            raise IngestError("revision must be a sequence number or revision id")
        if isinstance(revision, int):
            matches = [r for r in revisions if int(r["seq"]) == revision]
        else:
            text = str(revision)
            if text.isdigit():
                matches = [r for r in revisions if int(r["seq"]) == int(text)]
            else:
                matches = [
                    r
                    for r in revisions
                    if r["revision_id"] == text
                    or str(r["content_hash"]).startswith(text)
                ]
        if not matches:
            raise IngestError(
                f"revision {revision!r} not found in project {project!r}; "
                f"head is {revisions[-1]['seq']}"
            )
        if len(matches) > 1:
            raise IngestError(f"revision {revision!r} is ambiguous")
        rev = matches[0]
        return rev, int(rev["seq"])

    def table(self, project: str, revision: Any = None) -> tuple[TableState, dict[str, Any]]:
        pstate = self._project_state(project)
        rev, seq = self.resolve_revision(project, revision)
        table = load_table_at(self.workspace, pstate, seq)
        ref = {
            "project": project,
            "seq": seq,
            "revision_id": rev["revision_id"],
            "revision_hash": rev["content_hash"],
            "effective_at": rev.get("effective_at"),
            "ingested_at": rev.get("ingested_at"),
            "input_blob": rev.get("input_blob"),
            "descriptor_path": rev.get("descriptor_path"),
        }
        return table, ref

    def _as_of(self, as_of: Any) -> date:
        if as_of is None or as_of == "head":
            return date.max
        return parse_date(as_of, "as_of")

    def _plan_for(
        self, pstate: Mapping[str, Any], revision_seq: int, as_of: date
    ) -> Optional[dict[str, Any]]:
        plans = pstate.get("plans", {})
        candidates = []
        for plan_id, record in plans.items():
            effective = parse_date(record["effective_date"], "effective_date")
            # A plan version must not precede its own revision by more than the
            # fact that plans are project level; the latest version effective on
            # or before as_of wins.
            if effective <= as_of:
                candidates.append((effective, int(record["ingest_seq"]), record))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[-1][2]

    # ----------------------------------------------------------------- query

    def progress(
        self, project: str, revision: Any = None, as_of: Any = None
    ) -> ProgressResult:
        pstate = self._project_state(project)
        table, ref = self.table(project, revision)
        return self._evaluate(project, pstate, table, ref, as_of)

    def _evaluate(
        self,
        project: str,
        pstate: Mapping[str, Any],
        table: TableState,
        ref: Mapping[str, Any],
        as_of: Any,
    ) -> ProgressResult:
        as_of_date = self._as_of(as_of)
        reports = _report_versions(pstate)
        plan = self._plan_for(pstate, ref["seq"], as_of_date) if as_of_date != date.max else (
            sorted(
                pstate.get("plans", {}).values(),
                key=lambda r: (r["effective_date"], int(r["ingest_seq"])),
            )[-1]
            if pstate.get("plans")
            else None
        )
        return evaluate(
            project,
            table,
            reports,
            as_of_date,
            revision_ref=dict(ref),
            activities_plan=plan,
        )

    def element(
        self, project: str, guid: str, revision: Any = None, as_of: Any = None
    ) -> dict[str, Any]:
        pstate = self._project_state(project)
        table, ref = self.table(project, revision)
        result = self._evaluate(project, pstate, table, ref, as_of)
        canonical = table.resolve(guid)
        element = result.elements.get(canonical)
        if element is None:
            known = sorted(result.elements)
            raise IngestError(
                f"guid {guid!r} is not present in revision {ref['seq']} of {project!r}"
            )
        payload = element.to_dict()
        payload["requested_guid"] = guid
        payload["canonical_guid"] = canonical
        payload["revision"] = ref
        payload["as_of"] = result.as_of
        # Per-report history for this identity, oldest first, so a reviewer can
        # see exactly which uploads produced the current value.
        if element.evidence:
            payload["evidence"] = sorted(
                element.evidence,
                key=lambda e: (e["effective_date"], e["ingest_seq"]),
            )
        return payload

    def activity(
        self, project: str, activity: str, revision: Any = None, as_of: Any = None
    ) -> dict[str, Any]:
        pstate = self._project_state(project)
        table, ref = self.table(project, revision)
        result = self._evaluate(project, pstate, table, ref, as_of)
        bucket = result.activities.get(activity)
        if bucket is None:
            raise IngestError(
                f"activity {activity!r} has no components at revision "
                f"{result.revision['seq']} of {project!r}"
            )
        payload = bucket.to_dict()
        payload["revision"] = result.revision
        payload["as_of"] = result.as_of
        return payload

    def summary(self, project: str, revision: Any = None, as_of: Any = None) -> dict[str, Any]:
        return self.progress(project, revision, as_of).to_dict()

    def revisions(self, project: str) -> list[dict[str, Any]]:
        return self.revision_index(project)

    def reports(self, project: str) -> list[dict[str, Any]]:
        pstate = self._project_state(project)
        out: list[dict[str, Any]] = []
        for report_id, bucket in sorted(pstate.get("reports", {}).items()):
            for record in bucket.get("versions", []):
                out.append(
                    {
                        "report_id": report_id,
                        "version": record["version"],
                        "effective_date": record["effective_date"],
                        "ingested_at": record["ingested_at"],
                        "ingest_seq": record["ingest_seq"],
                        "source": record.get("source"),
                        "content_hash": record["content_hash"],
                        "lines": len(record["lines"]),
                    }
                )
        out.sort(key=lambda r: (r["effective_date"], r["ingest_seq"]))
        return out

    def reconcile(self, project: str) -> dict[str, Any]:
        """Audit the workspace: gaps, pending deltas, unallocated reports."""

        pstate = self._project_state(project)
        revisions = pstate.get("revisions", [])
        seqs = [int(r["seq"]) for r in revisions]
        gaps = []
        for expected in range(1, (max(seqs) if seqs else 0) + 1):
            if expected not in seqs:
                gaps.append(expected)
        head = self.progress(project) if revisions else None
        return {
            "project": project,
            "head_seq": pstate.get("head_seq", 0),
            "revision_seqs": seqs,
            "gaps": gaps,
            "pending": [
                {"seq": e["seq"], "revision_id": e["revision_id"]}
                for e in pstate.get("pending_deltas", [])
            ],
            "reports": len(self.reports(project)),
            "unallocated_reports": len(head.unallocated) if head else 0,
            "generation": self.workspace.state.get("generation"),
            "journal_present": self.workspace.journal_path.exists(),
        }
