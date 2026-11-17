"""Progress evaluation.

Semantics implemented here
==========================

Partial completion
    A daily report line states *cumulative* completion for one GUID, either as
    a raw ``completed`` quantity (in the component's unit) or as a
    ``fraction`` in ``[0, 1]``.  Reports are not additive: the value in effect
    at a date is the one from the latest report that is effective on or before
    that date.  Repeated uploads of the same report are a no-op (idempotent).

Late reports
    The value in effect at date ``D`` is chosen by ``(effective_date,
    ingest_seq)`` order, never by upload order alone.  A report that arrives
    late for an earlier date therefore only changes the interval it covers; it
    cannot overwrite a later day's value.

Revision awareness
    Allocation always runs against the element table of one specific model
    revision.  A historical as-of query reconstructs that revision and nothing
    from later revisions can leak into it.

Lineage
    Split parents are distributed to their children with the recorded weights;
    merge sources are distributed to the merge target.  Because those weights
    and the frozen planned quantities live in the lineage event, the
    parent/child contribution survives even after the parent or source has
    disappeared from the model.

Deleted components
    A component that vanished from a replacement model and is not part of a
    split/merge is *not* treated as complete.  Its planned quantity leaves the
    denominator and any report aimed at it is reported as unallocated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Optional, Sequence

from .history import LineageEvent, TableState

UNASSIGNED = "UNASSIGNED"
TOLERANCE = 1e-6


def _round(value: float) -> float:
    return round(value + 0.0, 6)


def _pct(completed: float, planned: float) -> float:
    if planned <= 0:
        return 0.0
    return round(100.0 * completed / planned, 2)


@dataclass
class ReportVersion:
    report_id: str
    version: int
    effective_date: date
    ingested_at: str
    ingest_seq: int
    content_hash: str
    lines: tuple[Any, ...]
    source: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "version": self.version,
            "effective_date": self.effective_date.isoformat(),
            "ingested_at": self.ingested_at,
            "ingest_seq": self.ingest_seq,
            "content_hash": self.content_hash,
            "source": self.source,
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass
class ActiveLine:
    line: Any
    report: ReportVersion
    reported_guid: str


def active_lines(
    reports: Iterable[ReportVersion], as_of: date
) -> dict[str, ActiveLine]:
    """Select the report line in effect for each *reported* GUID at ``as_of``."""

    ordered = sorted(
        (r for r in reports if r.effective_date <= as_of),
        key=lambda r: (r.effective_date, r.ingest_seq),
    )
    active: dict[str, ActiveLine] = {}
    for report in ordered:
        for line in report.lines:
            active[line.guid] = ActiveLine(line, report, line.guid)
    return active


def _statement_key(active: ActiveLine) -> tuple:
    report = active.report
    return (report.effective_date, report.ingest_seq)


def _resolve_contributions(
    candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Collapse candidates so no physical scope is counted twice.

    Every candidate names two things:

    ``target``
        The element that currently carries the quantity in the model.
    ``origin``
        The physical component the measurement was taken on.  A report on a
        split parent fans out to several children, giving several origins; a
        report on a merged-away source has that source as its origin.

    Per ``(target, origin)`` only the newest statement survives, so a child
    reported directly supersedes the share inherited from its split parent, and
    a re-measured merge source supersedes its earlier frozen contribution.

    A *direct* statement on a target is cumulative for that whole element, so
    it also supersedes every older origin folded into it (the absorbed merge
    sources).  Origins at least as recent as the direct statement remain, so a
    merge source measured after the target's last direct report is still
    preserved rather than silently dropped.
    """

    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate["target"], candidate["origin"])
        previous = newest.get(key)
        if previous is None or candidate["key"] > previous["key"]:
            newest[key] = candidate

    direct_key: dict[str, tuple] = {}
    for candidate in newest.values():
        if candidate["relation"] == "direct":
            target = candidate["target"]
            if target not in direct_key or candidate["key"] > direct_key[target]:
                direct_key[target] = candidate["key"]

    chosen = []
    for (target, origin), candidate in newest.items():
        key = direct_key.get(target)
        if key is not None and origin != target and candidate["key"] < key:
            continue
        chosen.append(candidate)
    return chosen



# ---------------------------------------------------------------------------
# identity / allocation


class Allocator:
    def __init__(self, table: TableState):
        self.table = table
        self._split_nodes: dict[str, LineageEvent] = {
            table.resolve(e.node): e for e in table.lineage if e.kind == "split"
        }
        self._merge_sources: dict[str, LineageEvent] = {}
        for event in table.lineage:
            if event.kind == "merge":
                for child in event.children:
                    self._merge_sources[table.resolve(child)] = event

    def planned_for(self, node: str) -> float:
        element = self.table.elements.get(node)
        if element is not None and element.present:
            return element.planned
        split = self._split_nodes.get(node)
        if split is not None:
            return sum(split.frozen_planned.values())
        merge = self._merge_sources.get(node)
        if merge is not None:
            return merge.frozen_planned.get(node, 0.0)
        element = self.table.elements.get(node)
        return element.planned if element is not None else 0.0

    def canonical_root(self, node: str) -> str:
        """Walk a merge chain up to the element that now owns the quantity."""

        table = self.table
        canonical = table.resolve(node)
        seen: set[str] = set()
        while canonical in self._merge_sources and canonical not in seen:
            seen.add(canonical)
            canonical = self._merge_sources[canonical].node
        return canonical

    def allocate(
        self,
        node: str,
        quantity: float,
        *,
        weight: float = 1.0,
        relation: str = "direct",
        chain: tuple[str, ...] = (),
        origin: Optional[str] = None,
    ) -> list[tuple[str, str, float, float, str]]:
        """Return ``[(target, origin, quantity, weight, relation), ...]``.

        ``target`` is the element that carries the quantity in this revision.
        ``origin`` is the physical component scope the measurement was taken
        on -- the split child after fan-out, or the merged-away source.  The
        distinction lets the evaluator supersede inherited shares with direct
        measurements without ever double counting.
        """

        table = self.table
        canonical = table.resolve(node)
        if canonical in chain:
            raise ValueError(f"lineage cycle while allocating {node}")
        chain = chain + (canonical,)
        element = table.elements.get(canonical)
        if element is not None and element.present:
            return [(canonical, origin or canonical, quantity, weight, relation)]
        split = self._split_nodes.get(canonical)
        if split is not None:
            out: list[tuple[str, str, float, float, str]] = []
            for child in split.children:
                child_weight = split.weights.get(child, 0.0)
                out.extend(
                    self.allocate(
                        child,
                        quantity * child_weight,
                        weight=weight * child_weight,
                        relation="split",
                        chain=chain,
                    )
                )
            return out
        merge = self._merge_sources.get(canonical)
        if merge is not None:
            # A merge folds a source's scope into the target *without changing
            # its units*, so a quantity measured on the source transfers 1:1.
            # ``merge.weights`` describe the planned composition of the target
            # and must not rescale an already-measured completion.
            return self.allocate(
                merge.node,
                quantity,
                weight=weight,
                relation="merge",
                chain=chain,
                origin=origin or canonical,
            )
        return []


# ---------------------------------------------------------------------------
# result objects


@dataclass
class ElementProgress:
    guid: str
    planned: float
    completed: float
    unit: str
    activity: str
    name: Optional[str]
    ifc_type: Optional[str]
    parent: Optional[str]
    present: bool
    is_progress_unit: bool
    aliases: tuple[str, ...] = ()
    lineage_role: Optional[str] = None
    children: tuple[str, ...] = ()
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "planned": _round(self.planned),
            "completed": _round(self.completed),
            "remaining": _round(max(0.0, self.planned - self.completed)),
            "progress_pct": _pct(self.completed, self.planned),
            "unit": self.unit,
            "activity": self.activity,
            "name": self.name,
            "ifc_type": self.ifc_type,
            "parent": self.parent,
            "present": self.present,
            "is_progress_unit": self.is_progress_unit,
            "aliases": list(self.aliases),
            "lineage_role": self.lineage_role,
            "children": list(self.children),
            "evidence": list(self.evidence),
        }


@dataclass
class ActivityProgress:
    activity: str
    name: Optional[str]
    wbs_code: Optional[str]
    planned: float
    completed: float
    component_count: int
    components: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity": self.activity,
            "name": self.name,
            "wbs_code": self.wbs_code,
            "planned": _round(self.planned),
            "completed": _round(self.completed),
            "progress_pct": _pct(self.completed, self.planned),
            "component_count": self.component_count,
            "components": list(self.components),
            "evidence": list(self.evidence),
        }


@dataclass
class ProgressResult:
    project: str
    as_of: str
    revision: dict[str, Any]
    planned: float
    completed: float
    elements: dict[str, ElementProgress]
    activities: dict[str, ActivityProgress]
    unallocated: list[dict[str, Any]]
    counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "as_of": self.as_of,
            "revision": self.revision,
            "planned": _round(self.planned),
            "completed": _round(self.completed),
            "remaining": _round(max(0.0, self.planned - self.completed)),
            "progress_pct": _pct(self.completed, self.planned),
            "counts": dict(self.counts),
            "activities": {
                key: value.to_dict() for key, value in sorted(self.activities.items())
            },
            "elements": {
                key: value.to_dict() for key, value in sorted(self.elements.items())
            },
            "unallocated": list(self.unallocated),
        }


# ---------------------------------------------------------------------------
# evaluation


def _container_guids(table: TableState) -> set[str]:
    """Present elements whose present descendants carry planned quantity."""

    by_parent: dict[str, list[str]] = {}
    for guid, element in table.elements.items():
        if not element.present or not element.parent:
            continue
        parent = table.resolve(element.parent)
        by_child = by_parent.setdefault(parent, [])
        by_child.append(guid)
    containers: set[str] = set()
    for parent, children in by_parent.items():
        stack = list(children)
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            element = table.elements.get(node)
            if element is not None and element.present and element.planned > 0:
                containers.add(parent)
                break
            stack.extend(by_parent.get(node, []))
    return containers


def _resolve_activity(table: TableState, guid: str) -> str:
    seen: set[str] = set()
    current: Optional[str] = guid
    while current is not None and current not in seen:
        seen.add(current)
        element = table.elements.get(current)
        if element is None:
            break
        if element.activity:
            return element.activity
        current = table.resolve(element.parent) if element.parent else None
    return UNASSIGNED


def evaluate(
    project: str,
    table: TableState,
    reports: Sequence[ReportVersion],
    as_of: date,
    *,
    revision_ref: Mapping[str, Any],
    activities_plan: Optional[Mapping[str, Any]] = None,
) -> ProgressResult:
    allocator = Allocator(table)
    lines = active_lines(reports, as_of)

    candidates: list[dict[str, Any]] = []
    unallocated: list[dict[str, Any]] = []

    for reported_guid, active in sorted(lines.items()):
        line = active.line
        report = active.report
        identity = table.resolve(reported_guid)
        if line.completed is not None:
            quantity = float(line.completed)
        else:
            # A fraction is always relative to the physical scope that was
            # actually reported: a merged-away source keeps its own frozen
            # planned quantity, not the target's larger total.
            quantity = float(line.fraction) * allocator.planned_for(identity)
        allocations = allocator.allocate(reported_guid, quantity)
        base_evidence = {
            "report_id": report.report_id,
            "report_version": report.version,
            "effective_date": report.effective_date.isoformat(),
            "ingested_at": report.ingested_at,
            "ingest_seq": report.ingest_seq,
            "reported_guid": reported_guid,
            "reported_completed": _round(line.completed) if line.completed is not None else None,
            "reported_fraction": _round(line.fraction) if line.fraction is not None else None,
            "content_hash": report.content_hash,
            "source": report.source,
            "note": line.note,
        }
        if not allocations:
            unallocated.append(
                {
                    **base_evidence,
                    "reason": "component is not present at the queried revision",
                    "canonical_guid": identity,
                    "quantity": _round(quantity),
                }
            )
            continue
        for target, origin, allocated, weight, relation in allocations:
            candidates.append(
                {
                    "target": target,
                    "origin": origin,
                    "allocated": allocated,
                    "weight": weight,
                    "relation": relation,
                    "key": _statement_key(active),
                    "evidence": {
                        **base_evidence,
                        "target_element": target,
                        "allocated_quantity": _round(allocated),
                        "weight": _round(weight),
                        "relation": relation,
                        "fraction": _round(allocated / quantity) if quantity else 0.0,
                        "reported_quantity": _round(quantity),
                    },
                }
            )

    element_completed: dict[str, float] = {}
    evidence_by_element: dict[str, list[dict[str, Any]]] = {}
    for candidate in _resolve_contributions(candidates):
        target = candidate["target"]
        element_completed[target] = element_completed.get(target, 0.0) + candidate["allocated"]
        evidence_by_element.setdefault(target, []).append(candidate["evidence"])

    containers = _container_guids(table)
    split_nodes = {table.resolve(e.node) for e in table.lineage if e.kind == "split"}
    merge_targets = {table.resolve(e.node) for e in table.lineage if e.kind == "merge"}
    merge_children = {
        table.resolve(c): table.resolve(e.node)
        for e in table.lineage
        if e.kind == "merge"
        for c in e.children
    }

    elements: dict[str, ElementProgress] = {}
    activity_totals: dict[str, dict[str, Any]] = {}
    total_planned = 0.0
    total_completed = 0.0
    counts = {"components": 0, "progress_units": 0, "removed": 0, "containers": 0}

    # Built once: O(aliases) instead of O(elements * aliases).
    alias_index: dict[str, list[str]] = {}
    for old, target in table.aliases.items():
        alias_index.setdefault(table.resolve(target), []).append(old)
    identity_cache: dict[str, tuple[str, ...]] = {}

    def identities(guid: str) -> tuple[str, ...]:
        cached = identity_cache.get(guid)
        if cached is None:
            cached = tuple(sorted(set([guid, *alias_index.get(guid, [])])))
            identity_cache[guid] = cached
        return cached

    for guid, element in sorted(table.elements.items()):
        counts["components"] += 1
        if not element.present:
            counts["removed"] += 1
        lineage_role: Optional[str] = None
        if guid in split_nodes:
            lineage_role = "split_parent"
        elif guid in merge_targets:
            lineage_role = "merge_target"
        elif guid in merge_children:
            lineage_role = "merge_source"
        children: tuple[str, ...] = ()
        if lineage_role in {"split_parent", "merge_target"}:
            event = table.children_of(guid)
            children = tuple(event.children) if event else ()
        activity = _resolve_activity(table, guid) if element.present else UNASSIGNED
        is_unit = element.present and element.planned > 0 and guid not in containers
        completed = element_completed.get(guid, 0.0)
        if element.present:
            completed = min(completed, element.planned)
        progress = ElementProgress(
            guid=guid,
            planned=element.planned,
            completed=completed,
            unit=element.unit,
            activity=activity,
            name=element.name,
            ifc_type=element.ifc_type,
            parent=element.parent,
            present=element.present,
            is_progress_unit=is_unit,
            aliases=identities(guid),
            lineage_role=lineage_role,
            children=children,
            evidence=evidence_by_element.get(guid, []),
        )
        elements[guid] = progress
        if is_unit:
            counts["progress_units"] += 1
            total_planned += element.planned
            total_completed += completed
            bucket = activity_totals.setdefault(
                activity,
                {"planned": 0.0, "completed": 0.0, "components": [], "evidence": []},
            )
            bucket["planned"] += element.planned
            bucket["completed"] += completed
            bucket["components"].append(guid)
            bucket["evidence"].extend(evidence_by_element.get(guid, []))
        elif element.present and guid in containers:
            counts["containers"] += 1

    plan_meta: dict[str, Mapping[str, Any]] = {}
    if activities_plan:
        for activity in activities_plan.get("activities", []):
            plan_meta[activity["id"]] = activity
        for activity_id in plan_meta:
            activity_totals.setdefault(
                activity_id,
                {"planned": 0.0, "completed": 0.0, "components": [], "evidence": []},
            )

    activities: dict[str, ActivityProgress] = {}
    for activity_id, bucket in sorted(activity_totals.items()):
        meta = plan_meta.get(activity_id, {})
        activities[activity_id] = ActivityProgress(
            activity=activity_id,
            name=meta.get("name"),
            wbs_code=meta.get("wbs_code"),
            planned=float(bucket["planned"]),
            completed=float(min(bucket["completed"], bucket["planned"]))
            if bucket["planned"]
            else float(bucket["completed"]),
            component_count=len(bucket["components"]),
            components=sorted(bucket["components"]),
            evidence=list(bucket["evidence"]),
        )

    return ProgressResult(
        project=project,
        # date.max is the "no upper bound" sentinel; echo it as an explicit
        # marker rather than a misleading far-future date.
        as_of=as_of.isoformat() if as_of != date.max else "latest",
        revision=dict(revision_ref),
        planned=total_planned,
        completed=min(total_completed, total_planned) if total_planned else total_completed,
        elements=elements,
        activities=activities,
        unallocated=unallocated,
        counts=counts,
    )
