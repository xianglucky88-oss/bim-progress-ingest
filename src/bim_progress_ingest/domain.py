"""Immutable domain objects for revision-aware progress ingestion.

The legacy model in :mod:`models` is intentionally left untouched.  Everything
here is additive so the original GUID -> activity calculator keeps working.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional


class IngestError(ValueError):
    """Raised when an input document is not valid for ingestion."""


class ConflictError(IngestError):
    """Raised when an immutable identity is reused with different content."""


# ---------------------------------------------------------------------------
# small helpers


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for content addressing."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_date(value: Any, field_name: str = "date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str) or not value:
        raise IngestError(f"{field_name} must be an ISO date string")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:  # pragma: no cover - defensive
        raise IngestError(f"{field_name} is not a valid ISO date: {value!r}") from exc


def parse_timestamp(value: Any, field_name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise IngestError(f"{field_name} is not a valid ISO timestamp: {value!r}") from exc
    else:
        raise IngestError(f"{field_name} must be an ISO timestamp string")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IngestError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise IngestError(f"{field_name} must be finite")
    if number < 0:
        raise IngestError(f"{field_name} must be >= 0")
    return number


def clean_guid(value: Any, field_name: str = "guid") -> str:
    if not isinstance(value, str) or not value.strip():
        raise IngestError(f"{field_name} must be a non-empty string")
    return value.strip()


# ---------------------------------------------------------------------------
# model revision entities


@dataclass(frozen=True)
class Component:
    """A component as it appears in one model revision.

    ``planned`` is the baseline quantity (m2, m3, tonnes, count...) used as the
    progress weight.  ``parent_guid`` describes the IFC/spatial hierarchy;
    ``activity`` is the WBS linkage inherited down that hierarchy when absent.
    """

    guid: str
    planned: float
    unit: str = ""
    activity: Optional[str] = None
    parent_guid: Optional[str] = None
    ifc_type: Optional[str] = None
    name: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Component":
        if not isinstance(raw, Mapping):
            raise IngestError("component must be an object")
        guid = clean_guid(raw.get("guid"))
        planned = as_float(raw.get("planned", 0), f"planned for {guid}")
        unit = raw.get("unit") or ""
        if not isinstance(unit, str):
            raise IngestError(f"unit for {guid} must be a string")
        activity = raw.get("activity")
        if activity is not None and (not isinstance(activity, str) or not activity):
            raise IngestError(f"activity for {guid} must be a non-empty string")
        parent = raw.get("parent")
        if parent is None:
            parent = raw.get("parent_guid")
        if parent is not None:
            parent = clean_guid(parent, f"parent for {guid}")
            if parent == guid:
                raise IngestError(f"component {guid} cannot be its own parent")
        ifc_type = raw.get("ifc_type") or raw.get("type")
        if ifc_type is not None and not isinstance(ifc_type, str):
            raise IngestError(f"ifc_type for {guid} must be a string")
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise IngestError(f"name for {guid} must be a string")
        return cls(
            guid=guid,
            planned=planned,
            unit=unit,
            activity=activity,
            parent_guid=parent,
            ifc_type=ifc_type,
            name=name,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"guid": self.guid, "planned": self.planned}
        if self.unit:
            data["unit"] = self.unit
        if self.activity is not None:
            data["activity"] = self.activity
        if self.parent_guid is not None:
            data["parent"] = self.parent_guid
        if self.ifc_type is not None:
            data["ifc_type"] = self.ifc_type
        if self.name is not None:
            data["name"] = self.name
        return data

    def digest(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class GuidRemap:
    """``from_guid`` is an old export name, ``to_guid`` the name used now."""

    from_guid: str
    to_guid: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "GuidRemap":
        if not isinstance(raw, Mapping):
            raise IngestError("guid_remap entry must be an object")
        old = clean_guid(raw.get("from") or raw.get("old"), "guid_remap.from")
        new = clean_guid(raw.get("to") or raw.get("new"), "guid_remap.to")
        if old == new:
            raise IngestError("guid_remap from and to must differ")
        return cls(old, new)

    def to_dict(self) -> dict[str, str]:
        return {"from": self.from_guid, "to": self.to_guid}


@dataclass(frozen=True)
class SplitEvent:
    """A component was split into two or more successor components."""

    parent_guid: str
    child_guids: tuple[str, ...]
    weights: Optional[Mapping[str, float]] = None
    declared_seq: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], seq: int) -> "SplitEvent":
        if not isinstance(raw, Mapping):
            raise IngestError("split entry must be an object")
        parent = clean_guid(raw.get("parent") or raw.get("from"), "split.parent")
        children_raw = raw.get("children") or raw.get("to")
        if not isinstance(children_raw, (list, tuple)) or len(children_raw) < 2:
            raise IngestError("split.children must list at least two GUIDs")
        children = tuple(clean_guid(c, "split.child") for c in children_raw)
        if len(set(children)) != len(children):
            raise IngestError("split.children must be unique")
        if parent in children:
            raise IngestError("split.parent cannot also be a child")
        weights_raw = raw.get("weights")
        weights: Optional[Mapping[str, float]] = None
        if weights_raw is not None:
            if not isinstance(weights_raw, Mapping):
                raise IngestError("split.weights must be an object")
            weights = {}
            total = 0.0
            for key, value in weights_raw.items():
                weight = as_float(value, f"split weight {key}")
                if weight > 1:
                    raise IngestError("split weights must be fractions in [0, 1]")
                weights[clean_guid(key, "split weight key")] = weight
                total += weight
            if not math.isclose(total, 1.0, abs_tol=1e-6):
                raise IngestError("split weights must sum to 1")
            if set(weights) - set(children):
                raise IngestError("split weights refer to unknown children")
        return cls(parent, children, weights, seq)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": "split",
            "parent": self.parent_guid,
            "children": list(self.child_guids),
        }
        if self.weights is not None:
            data["weights"] = dict(self.weights)
        return data


@dataclass(frozen=True)
class MergeEvent:
    """Two or more components were merged into a single successor."""

    source_guids: tuple[str, ...]
    target_guid: str
    declared_seq: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], seq: int) -> "MergeEvent":
        if not isinstance(raw, Mapping):
            raise IngestError("merge entry must be an object")
        sources_raw = raw.get("sources") or raw.get("from")
        if not isinstance(sources_raw, (list, tuple)) or len(sources_raw) < 1:
            raise IngestError("merge.sources must list at least one GUID")
        sources = tuple(clean_guid(s, "merge.source") for s in sources_raw)
        if len(set(sources)) != len(sources):
            raise IngestError("merge.sources must be unique")
        target = clean_guid(raw.get("target") or raw.get("to"), "merge.target")
        if target in sources:
            raise IngestError("merge.target cannot also be a source")
        return cls(sources, target, seq)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "merge",
            "sources": list(self.source_guids),
            "target": self.target_guid,
        }


@dataclass(frozen=True)
class Activity:
    id: str
    name: Optional[str] = None
    wbs_code: Optional[str] = None
    planned: Optional[float] = None
    unit: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Activity":
        if not isinstance(raw, Mapping):
            raise IngestError("activity must be an object")
        activity_id = raw.get("id") or raw.get("activity") or raw.get("code")
        activity_id = clean_guid(activity_id, "activity.id")
        name = raw.get("name")
        wbs = raw.get("wbs") or raw.get("wbs_code")
        planned = raw.get("planned")
        if planned is not None:
            planned = as_float(planned, f"planned for activity {activity_id}")
        unit = raw.get("unit") or ""
        return cls(activity_id, name, wbs, planned, unit)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id}
        if self.name is not None:
            data["name"] = self.name
        if self.wbs_code is not None:
            data["wbs_code"] = self.wbs_code
        if self.planned is not None:
            data["planned"] = self.planned
        if self.unit:
            data["unit"] = self.unit
        return data


@dataclass(frozen=True)
class RevisionHeader:
    project: str
    seq: int
    revision_id: str
    effective_at: Optional[str] = None
    mode: str = "full"
    activities: tuple[Activity, ...] = ()
    remaps: tuple[GuidRemap, ...] = ()
    splits: tuple[SplitEvent, ...] = ()
    merges: tuple[MergeEvent, ...] = ()
    removes: tuple[str, ...] = ()
    label: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], default_project: Optional[str] = None) -> "RevisionHeader":
        if not isinstance(raw, Mapping):
            raise IngestError("revision header must be an object")
        project = raw.get("project", default_project)
        if project is None:
            raise IngestError(
                "project is required (set it on the document, on each entry, "
                "or add a {'kind': 'project'} header line)"
            )
        project = clean_guid(project, "project")
        seq = raw.get("seq", raw.get("revision_seq", raw.get("revision")))
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise IngestError("revision seq must be a positive integer")
        revision_id = raw.get("revision_id") or raw.get("id") or f"R{seq}"
        revision_id = clean_guid(revision_id, "revision_id")
        effective_at = raw.get("effective_at")
        if effective_at is not None:
            parse_timestamp(effective_at, "effective_at")
        mode = raw.get("mode", "full")
        if mode not in {"full", "delta"}:
            raise IngestError("revision mode must be 'full' or 'delta'")

        activities: list[Activity] = []
        for item in raw.get("activities", []) or []:
            activities.append(Activity.from_mapping(item))
        remaps: list[GuidRemap] = []
        for item in raw.get("guid_remap", raw.get("remaps", [])) or []:
            remaps.append(GuidRemap.from_mapping(item))
        splits: list[SplitEvent] = []
        for item in raw.get("splits", []) or []:
            splits.append(SplitEvent.from_mapping(item, seq))
        merges: list[MergeEvent] = []
        for item in raw.get("merges", []) or []:
            merges.append(MergeEvent.from_mapping(item, seq))
        removes: list[str] = []
        for item in raw.get("remove", raw.get("removed", [])) or []:
            removes.append(clean_guid(item, "remove"))
        label = raw.get("label")
        if label is not None and not isinstance(label, str):
            raise IngestError("label must be a string")

        return cls(
            project=project,
            seq=seq,
            revision_id=revision_id,
            effective_at=effective_at,
            mode=mode,
            activities=tuple(activities),
            remaps=tuple(remaps),
            splits=tuple(splits),
            merges=tuple(merges),
            removes=tuple(removes),
            label=label,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "project": self.project,
            "seq": self.seq,
            "revision_id": self.revision_id,
            "mode": self.mode,
            "activities": [a.to_dict() for a in self.activities],
            "guid_remap": [r.to_dict() for r in self.remaps],
            "splits": [s.to_dict() for s in self.splits],
            "merges": [m.to_dict() for m in self.merges],
        }
        if self.effective_at is not None:
            data["effective_at"] = self.effective_at
        if self.removes:
            data["remove"] = list(self.removes)
        if self.label is not None:
            data["label"] = self.label
        return data

    def digest(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))


# ---------------------------------------------------------------------------
# daily reports


@dataclass(frozen=True)
class ReportLine:
    guid: str
    completed: Optional[float] = None
    fraction: Optional[float] = None
    unit: str = ""
    note: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReportLine":
        if not isinstance(raw, Mapping):
            raise IngestError("report line must be an object")
        guid = clean_guid(raw.get("guid"))
        has_completed = "completed" in raw and raw.get("completed") is not None
        has_fraction = (
            ("fraction" in raw and raw.get("fraction") is not None)
            or ("percent" in raw and raw.get("percent") is not None)
            or ("pct" in raw and raw.get("pct") is not None)
        )
        if has_completed == has_fraction:
            raise IngestError(
                f"report line for {guid} must contain exactly one of 'completed' or 'fraction'"
            )
        completed: Optional[float] = None
        fraction: Optional[float] = None
        if has_completed:
            completed = as_float(raw.get("completed"), f"completed for {guid}")
        else:
            raw_fraction = raw.get("fraction", raw.get("percent", raw.get("pct")))
            fraction = as_float(raw_fraction, f"fraction for {guid}")
            if "percent" in raw or "pct" in raw:
                fraction /= 100.0
            if fraction > 1:
                raise IngestError(f"fraction for {guid} must be in [0, 1]")
        unit = raw.get("unit") or ""
        if not isinstance(unit, str):
            raise IngestError(f"unit for report line {guid} must be a string")
        note = raw.get("note")
        if note is not None and not isinstance(note, str):
            raise IngestError("report line note must be a string")
        return cls(guid, completed, fraction, unit, note)

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


@dataclass(frozen=True)
class DailyReport:
    project: str
    report_id: str
    effective_date: date
    lines: tuple[ReportLine, ...]
    ingested_at: datetime
    source: Optional[str] = None
    ingest_seq: int = 0
    content_hash: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], default_project: Optional[str] = None) -> "DailyReport":
        if not isinstance(raw, Mapping):
            raise IngestError("report must be an object")
        project = clean_guid(raw.get("project", default_project), "project")
        report_id = clean_guid(raw.get("report_id") or raw.get("id"), "report_id")
        effective = parse_date(raw.get("effective_date") or raw.get("date"), "effective_date")
        ingested_at = raw.get("ingested_at")
        if ingested_at is not None:
            ingested_at = parse_timestamp(ingested_at, "ingested_at")
        else:
            ingested_at = datetime.now(timezone.utc)
        lines_raw = raw.get("lines")
        if not isinstance(lines_raw, (list, tuple)) or not lines_raw:
            raise IngestError("report lines must be a non-empty list")
        lines = tuple(ReportLine.from_mapping(item) for item in lines_raw)
        guids = [line.guid for line in lines]
        if len(set(guids)) != len(guids):
            raise IngestError(f"report {report_id} contains duplicate line GUIDs")
        source = raw.get("source")
        if source is not None and not isinstance(source, str):
            raise IngestError("report source must be a string")
        report = cls(project, report_id, effective, lines, ingested_at, source)
        return cls(
            project=project,
            report_id=report_id,
            effective_date=effective,
            lines=lines,
            ingested_at=ingested_at,
            source=source,
            ingest_seq=0,
            content_hash=report.content_digest(),
        )

    def content_digest(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "project": self.project,
                    "report_id": self.report_id,
                    "effective_date": self.effective_date.isoformat(),
                    "source": self.source,
                    "lines": [line.to_dict() for line in self.lines],
                }
            )
        )

    def to_dict(self, include_ingest: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "project": self.project,
            "report_id": self.report_id,
            "effective_date": self.effective_date.isoformat(),
            "lines": [line.to_dict() for line in self.lines],
        }
        if self.source is not None:
            data["source"] = self.source
        if include_ingest:
            data["ingested_at"] = self.ingested_at.isoformat()
            data["ingest_seq"] = self.ingest_seq
            data["content_hash"] = self.content_hash
        return data

    @classmethod
    def from_stored(cls, raw: Mapping[str, Any]) -> "DailyReport":
        return cls(
            project=raw["project"],
            report_id=raw["report_id"],
            effective_date=parse_date(raw["effective_date"], "effective_date"),
            lines=tuple(ReportLine.from_mapping(line) for line in raw["lines"]),
            ingested_at=parse_timestamp(raw["ingested_at"], "ingested_at"),
            source=raw.get("source"),
            ingest_seq=int(raw["ingest_seq"]),
            content_hash=raw["content_hash"],
        )


@dataclass(frozen=True)
class PlanVersion:
    project: str
    plan_id: str
    effective_date: date
    activities: tuple[Activity, ...]
    ingested_at: datetime
    ingest_seq: int = 0
    content_hash: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], default_project: Optional[str] = None) -> "PlanVersion":
        if not isinstance(raw, Mapping):
            raise IngestError("plan must be an object")
        project = clean_guid(raw.get("project", default_project), "project")
        plan_id = clean_guid(raw.get("plan_id") or raw.get("id"), "plan_id")
        effective = parse_date(raw.get("effective_date") or raw.get("date"), "effective_date")
        activities_raw = raw.get("activities")
        if not isinstance(activities_raw, (list, tuple)) or not activities_raw:
            raise IngestError("plan activities must be a non-empty list")
        activities = tuple(Activity.from_mapping(item) for item in activities_raw)
        ids = [a.id for a in activities]
        if len(set(ids)) != len(ids):
            raise IngestError(f"plan {plan_id} contains duplicate activity ids")
        ingested_at = raw.get("ingested_at")
        ingested_at = parse_timestamp(ingested_at, "ingested_at") if ingested_at else datetime.now(timezone.utc)
        plan = cls(project, plan_id, effective, activities, ingested_at)
        return cls(project, plan_id, effective, activities, ingested_at, 0, plan.content_digest())

    def content_digest(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "project": self.project,
                    "plan_id": self.plan_id,
                    "effective_date": self.effective_date.isoformat(),
                    "activities": [a.to_dict() for a in self.activities],
                }
            )
        )

    def to_dict(self, include_ingest: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "project": self.project,
            "plan_id": self.plan_id,
            "effective_date": self.effective_date.isoformat(),
            "activities": [a.to_dict() for a in self.activities],
        }
        if include_ingest:
            data["ingested_at"] = self.ingested_at.isoformat()
            data["ingest_seq"] = self.ingest_seq
            data["content_hash"] = self.content_hash
        return data

    @classmethod
    def from_stored(cls, raw: Mapping[str, Any]) -> "PlanVersion":
        return cls(
            project=raw["project"],
            plan_id=raw["plan_id"],
            effective_date=parse_date(raw["effective_date"], "effective_date"),
            activities=tuple(Activity.from_mapping(item) for item in raw["activities"]),
            ingested_at=parse_timestamp(raw["ingested_at"], "ingested_at"),
            ingest_seq=int(raw["ingest_seq"]),
            content_hash=raw["content_hash"],
        )


# ---------------------------------------------------------------------------
# provenance helpers


@dataclass(frozen=True)
class ReportEvidence:
    report_id: str
    effective_date: str
    ingested_at: str
    ingest_seq: int
    reported_guid: str
    target_element: str
    allocated_quantity: float
    weight: float
    relation: str
    content_hash: str
    source: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "report_id": self.report_id,
            "effective_date": self.effective_date,
            "ingested_at": self.ingested_at,
            "ingest_seq": self.ingest_seq,
            "reported_guid": self.reported_guid,
            "target_element": self.target_element,
            "allocated_quantity": round(self.allocated_quantity, 6),
            "weight": round(self.weight, 6),
            "relation": self.relation,
            "content_hash": self.content_hash,
        }
        if self.source is not None:
            data["source"] = self.source
        if self.note is not None:
            data["note"] = self.note
        return data


def revision_ref(header: Mapping[str, Any], content_hash: str) -> dict[str, Any]:
    return {
        "project": header["project"],
        "seq": header["seq"],
        "revision_id": header["revision_id"],
        "revision_hash": content_hash,
        "effective_at": header.get("effective_at"),
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
