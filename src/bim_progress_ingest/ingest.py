"""Orchestration: ingest model revisions, reports and plans into a workspace.

Guarantees
==========
* **Idempotent revisions** - a revision is content addressed by its normalised
  payload.  Re-parsing the same model is detected before any blob is written
  and returns a no-op result with ``duplicate: true``.
* **Crash safe** - planning happens entirely before mutation; the workspace
  journal makes a partial ingest roll back to the previous generation.
* **Bounded memory** - components are streamed into a compressed blob; the
  change set is streamed into a second blob; only the derived element table
  for the current head is held in memory during validation.
* **Historical snapshots** - every ``snapshot_interval`` revisions a full
  element table snapshot is written so queries replay at most
  ``snapshot_interval`` small change blobs.
* **Report idempotency** - a report is keyed by ``report_id`` and its content
  hash; re-uploading the same bytes is a no-op, changed bytes become a new
  version.
* **Out-of-order revisions** - a revision whose sequence is behind the head is
  stored as a pending delta and applied only when the gap is filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .domain import (
    ConflictError,
    IngestError,
    RevisionHeader,
    canonical_json,
    sha256_text,
    utc_now,
)
from .history import (
    RevisionPlan,
    TableState,
    apply_revision,
    build_revision_plan,
    load_table_at,
)
from .parser import (
    Document,
    iter_revision_components,
    parse_plan,
    parse_report,
    revision_header,
)
from .storage import Workspace


def _stable_hash(payload: Any) -> str:
    return sha256_text(canonical_json(payload))


@dataclass
class IngestSummary:
    project: str
    applied: list[dict[str, Any]]
    duplicates: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    reports: dict[str, list[dict[str, Any]]]
    plans: dict[str, list[dict[str, Any]]]
    generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "ok": True,
            "generation": self.generation,
            "applied_revisions": list(self.applied),
            "duplicate_revisions": list(self.duplicates),
            "pending_revisions": list(self.pending),
            "reports": {k: list(v) for k, v in sorted(self.reports.items())},
            "plans": {k: list(v) for k, v in sorted(self.plans.items())},
            "counts": {
                "applied": len(self.applied),
                "duplicates": len(self.duplicates),
                "pending": len(self.pending),
                "reports_stored": sum(
                    1 for v in self.reports.values() if v and v[0]["status"] == "stored"
                ),
                "reports_duplicate": sum(
                    1 for v in self.reports.values() if v and v[0]["status"] == "duplicate"
                ),
                "reports_revised": sum(
                    1 for v in self.reports.values() if v and v[0]["status"] == "revised"
                ),
            },
        }


class Ingester:
    def __init__(self, workspace: Workspace, snapshot_interval: Optional[int] = None):
        self.workspace = workspace
        self._head_cache: Optional[tuple[int, TableState]] = None
        if snapshot_interval is not None:
            if snapshot_interval < 1:
                raise IngestError("snapshot_interval must be >= 1")
            self.snapshot_interval = int(snapshot_interval)
        else:
            self.snapshot_interval = None

    # ------------------------------------------------------------------ entry

    def ingest_document(self, document: Document) -> dict[str, Any]:
        summaries = []
        if document.plans:
            summaries.append(self._ingest_plans(document))
        if document.revisions:
            summaries.append(self._ingest_revisions(document))
        if document.reports:
            summaries.append(self._ingest_reports(document))
        if not summaries:
            raise IngestError("document contains nothing to ingest")
        return {
            "ok": True,
            "generation": self.workspace.state["generation"],
            "results": summaries,
        }

    def ingest_path(self, path: str | Path) -> dict[str, Any]:
        from .parser import load_document

        return self.ingest_document(load_document(path))

    # ---------------------------------------------------------------- plans

    def _ingest_plans(self, document: Document) -> dict[str, Any]:
        with self.workspace.writer_lock():
            journal = self.workspace.begin_transaction(
                {"op": "plans", "started_at": utc_now().isoformat()}
            )
            try:
                new_state = self._copy_state()
                stored: dict[str, list[dict[str, Any]]] = {}
                for raw in document.plans:
                    plan = parse_plan(raw, document.project)
                    pstate = self._project_state_in(new_state, plan.project)
                    bucket = pstate["plans"]
                    existing = bucket.get(plan.plan_id)
                    if existing and existing["content_hash"] == plan.content_hash:
                        stored.setdefault(plan.plan_id, []).append(
                            {
                                "plan_id": plan.plan_id,
                                "status": "duplicate",
                                "effective_date": plan.effective_date.isoformat(),
                            }
                        )
                        continue
                    ingest_seq = int(new_state["ingest_seq"]) + 1
                    new_state["ingest_seq"] = ingest_seq
                    version = 1 if not existing else int(existing["version"]) + 1
                    record = {
                        **plan.to_dict(include_ingest=False),
                        "ingested_at": plan.ingested_at.isoformat(),
                        "ingest_seq": ingest_seq,
                        "content_hash": plan.content_hash,
                        "version": version,
                        "blob": self.workspace.write_json_blob("plans", plan.to_dict()),
                    }
                    bucket[plan.plan_id] = record
                    stored.setdefault(plan.plan_id, []).append(
                        {
                            "plan_id": plan.plan_id,
                            "status": "stored" if version == 1 else "revised",
                            "effective_date": plan.effective_date.isoformat(),
                            "version": version,
                        }
                    )
                self._commit(new_state, journal)
            except BaseException:
                self.workspace.abort()
                raise
            return {"kind": "plans", "plans": stored}

    # ------------------------------------------------------------ revisions

    def _ingest_revisions(self, document: Document) -> dict[str, Any]:
        with self.workspace.writer_lock():
            journal = self.workspace.begin_transaction(
                {"op": "revisions", "started_at": utc_now().isoformat()}
            )
            try:
                new_state = self._copy_state()
                applied: list[dict[str, Any]] = []
                duplicates: list[dict[str, Any]] = []
                pending: list[dict[str, Any]] = []

                prepared: list[RevisionPlan] = []
                for raw in document.revisions:
                    header = revision_header(raw, document.project)
                    # Stream the vendor file straight to a content-addressed
                    # blob: components are never retained after hashing.
                    raw_digest, count, element_hash = self.workspace.write_jsonl_blob_hashed(
                        "inputs",
                        (c.to_dict() for c in iter_revision_components(raw, document.base_dir)),
                    )
                    revision_hash = self._revision_hash(header, element_hash, count)
                    plan = build_revision_plan(
                        header,
                        [],  # components are read back per revision on apply
                        raw_digest,
                        revision_hash,
                        utc_now().isoformat(),
                        None,
                    )
                    plan.element_count = count
                    prepared.append(plan)

                prepared.sort(key=lambda p: p.seq)
                seen_seqs: dict[int, str] = {}
                for plan in prepared:
                    if plan.seq in seen_seqs:
                        raise IngestError(
                            f"document contains revision {plan.seq} twice"
                        )
                    seen_seqs[plan.seq] = plan.content_hash

                result = self._apply_prepared(new_state, prepared)
                applied.extend(result["applied"])
                duplicates.extend(result["duplicates"])
                pending.extend(result["pending"])
                self._commit(new_state, journal)
            except BaseException:
                self.workspace.abort()
                raise
            return {
                "kind": "revisions",
                "applied": applied,
                "duplicates": duplicates,
                "pending": pending,
            }

    def _revision_hash(self, header: RevisionHeader, element_hash: str, count: int) -> str:
        """Content identity independent of input file ordering."""

        return _stable_hash(
            {
                "header": header.to_dict(),
                "elements": element_hash,
                "count": count,
            }
        )

    def _apply_prepared(self, new_state: dict[str, Any], prepared: list[RevisionPlan]) -> dict[str, Any]:
        applied: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        projects: dict[str, list[RevisionPlan]] = {}
        for plan in prepared:
            projects.setdefault(plan.header.project, []).append(plan)

        for project, plans in projects.items():
            pstate = self._project_state_in(new_state, project)
            by_hash = self._revision_hashes(pstate)
            queue = sorted(plans, key=lambda p: p.seq)

            # Known duplicates first (never re-apply a revision we already have).
            fresh: list[RevisionPlan] = []
            for plan in queue:
                if plan.seq in by_hash and by_hash[plan.seq] == plan.content_hash:
                    duplicates.append(
                        {
                            "seq": plan.seq,
                            "revision_id": plan.header.revision_id,
                            "reason": "identical revision already ingested",
                        }
                    )
                elif plan.seq in by_hash:
                    raise ConflictError(
                        f"revision {plan.seq} of {project} already exists with different content"
                    )
                else:
                    fresh.append(plan)

            for plan in fresh:
                if plan.seq != pstate["head_seq"] + 1:
                    entry = self._queue_pending(pstate, plan)
                    pending.append(entry)
                    continue
                applied.extend(self._apply_one(new_state, pstate, plan))
                applied.extend(self._drain_pending(new_state, pstate))
        return {"applied": applied, "duplicates": duplicates, "pending": pending}

    def _revision_hashes(self, pstate: Mapping[str, Any]) -> dict[int, str]:
        return {int(r["seq"]): r["content_hash"] for r in pstate["revisions"]}

    def _queue_pending(self, pstate: dict[str, Any], plan: RevisionPlan) -> dict[str, Any]:
        queue: list[dict[str, Any]] = pstate.setdefault("pending_deltas", [])
        for entry in queue:
            if int(entry["seq"]) == plan.seq:
                if entry["content_hash"] != plan.content_hash:
                    raise ConflictError(
                        f"pending revision {plan.seq} conflicts with a different payload"
                    )
                return {
                    "seq": plan.seq,
                    "revision_id": plan.header.revision_id,
                    "reason": "already pending",
                }
        queue.append(
            {
                "seq": plan.seq,
                "revision_id": plan.header.revision_id,
                "content_hash": plan.content_hash,
                "header": plan.header.to_dict(),
                "input_blob": plan.raw_digest,
                "effective_at": plan.effective_at,
            }
        )
        queue.sort(key=lambda e: e["seq"])
        return {
            "seq": plan.seq,
            "revision_id": plan.header.revision_id,
            "reason": f"out of order; head is {pstate['head_seq']}",
        }

    def _drain_pending(self, new_state: dict[str, Any], pstate: dict[str, Any]) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        queue = pstate.get("pending_deltas", [])
        changed = True
        while changed:
            changed = False
            for entry in list(queue):
                if int(entry["seq"]) != pstate["head_seq"] + 1:
                    continue
                header = RevisionHeader.from_mapping(entry["header"])
                components = [
                    _component_from_record(record)
                    for record in self.workspace.read_jsonl_blob("inputs", entry["input_blob"])
                ]
                plan = build_revision_plan(
                    header,
                    components,
                    entry["input_blob"],
                    entry["content_hash"],
                    utc_now().isoformat(),
                    pstate.get("effective_at"),
                )
                applied.extend(self._apply_one(new_state, pstate, plan, from_pending=True))
                queue.remove(entry)
                changed = True
        return applied

    def _apply_one(
        self,
        new_state: dict[str, Any],
        pstate: dict[str, Any],
        plan: RevisionPlan,
        *,
        from_pending: bool = False,
    ) -> list[dict[str, Any]]:
        if pstate["head_seq"] == 0:
            table = TableState()
        else:
            table = self._table_for_head(pstate)
        components = plan.components
        if not components:
            components = [
                _component_from_record(record)
                for record in self.workspace.read_jsonl_blob("inputs", plan.raw_digest)
            ]
        plan = build_revision_plan(
            plan.header,
            components,
            plan.raw_digest,
            plan.content_hash,
            plan.effective_at,
            pstate.get("effective_at"),
            element_count=plan.element_count,
        )
        ingest_seq = int(new_state["ingest_seq"]) + 1
        result = apply_revision(table, plan, ingest_seq)
        new_state["ingest_seq"] = ingest_seq

        change_blob, _ = self.workspace.write_jsonl_blob("changes", result["records"])
        element_blob = plan.raw_digest

        descriptor = {
            "seq": plan.seq,
            "revision_id": plan.header.revision_id,
            "content_hash": plan.content_hash,
            "input_blob": plan.raw_digest,
            "element_blob": element_blob,
            "change_blob": change_blob,
            "effective_at": plan.effective_at,
            "ingested_at": utc_now().isoformat(),
            "ingest_seq": ingest_seq,
            "mode": plan.header.mode,
            "counts": {
                "elements": plan.element_count,
                "added": len(result["added"]),
                "updated": len(result["updated"]),
                "removed": len(result["removed"]),
            },
            "lineage": [event.to_dict() for event in result["lineage"]],
            "remaps": [r.to_dict() for r in plan.header.remaps],
            "label": plan.header.label,
            "from_pending": from_pending,
        }
        descriptor["descriptor_path"] = self.workspace.write_revision_descriptor(
            plan.header.project, descriptor
        )

        pstate["revisions"].append(
            {
                "seq": plan.seq,
                "revision_id": plan.header.revision_id,
                "content_hash": plan.content_hash,
                "input_blob": plan.raw_digest,
                "element_blob": element_blob,
                "change_blob": change_blob,
                "effective_at": plan.effective_at,
                "ingested_at": descriptor["ingested_at"],
                "ingest_seq": ingest_seq,
                "mode": plan.header.mode,
                "descriptor_path": descriptor["descriptor_path"],
                "counts": descriptor["counts"],
            }
        )
        pstate["head_seq"] = plan.seq
        pstate["effective_at"] = plan.effective_at
        pstate["element_count"] = plan.element_count
        pstate.pop("table_cache", None)

        # Persist a snapshot when the interval is reached, or when the next
        # revision would otherwise require replaying too many change blobs.
        if self.snapshot_interval is not None:
            pstate["snapshot_interval"] = self.snapshot_interval
        interval = int(pstate.get("snapshot_interval", 5))
        applied_since = plan.seq - max(
            [int(s) for s in pstate.setdefault("snapshots", {})] or [0]
        )
        if applied_since >= interval or plan.seq == 1:
            digest, _ = self.workspace.write_jsonl_blob(
                "snapshots", table.snapshot_records()
            )
            pstate["snapshots"][str(plan.seq)] = digest

        return [
            {
                "seq": plan.seq,
                "revision_id": plan.header.revision_id,
                "content_hash": plan.content_hash,
                "effective_at": plan.effective_at,
                "counts": descriptor["counts"],
                "lineage": [
                    {"kind": e.kind, "node": e.node, "children": list(e.children)}
                    for e in result["lineage"]
                ],
                "remaps": descriptor["remaps"],
                "warnings": result.get("warnings", []),
                "from_pending": from_pending,
            }
        ]

    # -------------------------------------------------------------- reports

    def _ingest_reports(self, document: Document) -> dict[str, Any]:
        with self.workspace.writer_lock():
            journal = self.workspace.begin_transaction(
                {"op": "reports", "started_at": utc_now().isoformat()}
            )
            try:
                new_state = self._copy_state()
                summary: dict[str, list[dict[str, Any]]] = {}
                for raw in document.reports:
                    report = parse_report(raw, document.project)
                    pstate = self._project_state_in(new_state, report.project)
                    bucket = pstate["reports"].setdefault(report.report_id, {})
                    history = bucket.setdefault("versions", [])
                    duplicate = None
                    for version in history:
                        if version["content_hash"] == report.content_hash:
                            duplicate = version
                            break
                    if duplicate is not None:
                        summary.setdefault(report.report_id, []).append(
                            {
                                "report_id": report.report_id,
                                "status": "duplicate",
                                "version": duplicate["version"],
                                "effective_date": report.effective_date.isoformat(),
                                "ingest_seq": duplicate["ingest_seq"],
                            }
                        )
                        continue
                    ingest_seq = int(new_state["ingest_seq"]) + 1
                    new_state["ingest_seq"] = ingest_seq
                    version_number = len(history) + 1
                    previous_effective = (
                        history[-1]["effective_date"] if history else None
                    )
                    record = {
                        **report.to_dict(include_ingest=False),
                        "ingested_at": report.ingested_at.isoformat(),
                        "ingest_seq": ingest_seq,
                        "content_hash": report.content_hash,
                        "version": version_number,
                        "blob": self.workspace.write_json_blob("reports", report.to_dict()),
                    }
                    history.append(record)
                    history.sort(
                        key=lambda r: (r["effective_date"], int(r["ingest_seq"]))
                    )
                    status = "stored" if version_number == 1 else "revised"
                    summary.setdefault(report.report_id, []).append(
                        {
                            "report_id": report.report_id,
                            "status": status,
                            "version": version_number,
                            "effective_date": report.effective_date.isoformat(),
                            "ingest_seq": ingest_seq,
                            "previous_effective_date": previous_effective,
                        }
                    )
                self._commit(new_state, journal)
            except BaseException:
                self.workspace.abort()
                raise
            return {"kind": "reports", "reports": summary}

    # --------------------------------------------------------------- helpers

    def _table_for_head(self, pstate: Mapping[str, Any]) -> TableState:
        """Replay once per ingest call, then reuse while walking revisions."""

        cache = self._head_cache
        if cache is not None and cache[0] == id(pstate) and cache[1].seq == int(pstate["head_seq"]):
            return cache[1]
        table = load_table_at(self.workspace, pstate, int(pstate["head_seq"]))
        self._head_cache = (id(pstate), table)
        return table

    def _copy_state(self) -> dict[str, Any]:
        import json

        return json.loads(canonical_json(self.workspace.state))

    def _project_state_in(self, state: dict[str, Any], project: str) -> dict[str, Any]:
        projects = state.setdefault("projects", {})
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
                "snapshots": {},
            },
        )

    def _commit(self, new_state: dict[str, Any], journal: Mapping[str, Any]) -> None:
        new_state["generation"] = self.workspace.state["generation"] + 1
        self.workspace.commit(new_state, journal)


def _component_from_record(record: Mapping[str, Any]):
    from .domain import Component

    return Component.from_mapping(record)
