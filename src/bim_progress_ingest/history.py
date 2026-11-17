"""Revision history: element identity, hierarchy and split/merge lineage.

The history is an append-only sequence of revisions.  Each applied revision
contributes a small *change* blob (added/updated/removed elements, GUID
remaps and lineage events).  Periodically a *snapshot* blob captures the full
element table so a query never has to replay the whole project.

Replaying changes up to sequence ``N`` yields a :class:`TableState`, which is
the model as it existed at revision ``N``.  Queries never look at revisions
after ``N`` -- that is what keeps historical as-of results free of pollution
from the current model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .domain import (
    Component,
    ConflictError,
    IngestError,
    RevisionHeader,
    parse_timestamp,
)

TOLERANCE = 1e-6


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=TOLERANCE)


@dataclass
class ElementState:
    guid: str
    planned: float
    unit: str = ""
    activity: Optional[str] = None
    parent: Optional[str] = None
    ifc_type: Optional[str] = None
    name: Optional[str] = None
    present: bool = True
    first_seq: int = 0
    last_seq: int = 0
    removed_seq: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "guid": self.guid,
            "planned": self.planned,
            "present": self.present,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
        }
        if self.unit:
            data["unit"] = self.unit
        if self.activity is not None:
            data["activity"] = self.activity
        if self.parent is not None:
            data["parent"] = self.parent
        if self.ifc_type is not None:
            data["ifc_type"] = self.ifc_type
        if self.name is not None:
            data["name"] = self.name
        if self.removed_seq is not None:
            data["removed_seq"] = self.removed_seq
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ElementState":
        return cls(
            guid=raw["guid"],
            planned=float(raw["planned"]),
            unit=raw.get("unit", ""),
            activity=raw.get("activity"),
            parent=raw.get("parent"),
            ifc_type=raw.get("ifc_type"),
            name=raw.get("name"),
            present=bool(raw.get("present", True)),
            first_seq=int(raw.get("first_seq", 0)),
            last_seq=int(raw.get("last_seq", 0)),
            removed_seq=raw.get("removed_seq"),
        )

    def to_component(self) -> Component:
        return Component(
            guid=self.guid,
            planned=self.planned,
            unit=self.unit,
            activity=self.activity,
            parent_guid=self.parent,
            ifc_type=self.ifc_type,
            name=self.name,
        )


@dataclass(frozen=True)
class LineageEvent:
    """``node`` was split into, or merged from, ``children``.

    ``frozen_planned`` records the planned quantity of each child at the moment
    the event happened.  Merge sources disappear from the model, so this is the
    only place their contribution survives -- which is exactly what guarantees
    that a merge never loses parent/child contribution.
    """

    kind: str  # "split" | "merge"
    node: str
    children: tuple[str, ...]
    weights: Mapping[str, float]
    seq: int
    frozen_planned: Mapping[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "node": self.node,
            "children": list(self.children),
            "weights": dict(self.weights),
            "seq": self.seq,
            "frozen_planned": dict(self.frozen_planned),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LineageEvent":
        return cls(
            kind=raw["kind"],
            node=raw["node"],
            children=tuple(raw["children"]),
            weights={k: float(v) for k, v in raw["weights"].items()},
            seq=int(raw["seq"]),
            frozen_planned={k: float(v) for k, v in raw["frozen_planned"].items()},
        )


@dataclass
class TableState:
    """The element table and identity graph as of one revision sequence."""

    elements: dict[str, ElementState] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    lineage: list[LineageEvent] = field(default_factory=list)
    seq: int = 0
    revision_id: Optional[str] = None
    content_hash: Optional[str] = None
    effective_at: Optional[str] = None

    # ------------------------------------------------------------------ ids

    def resolve(self, guid: str) -> str:
        """Follow redirect chains to the canonical element id."""

        seen: set[str] = set()
        current = guid
        while current in self.aliases and current not in seen:
            seen.add(current)
            current = self.aliases[current]
        return current

    def identifiers(self, canonical: str) -> list[str]:
        names = [canonical]
        for old, target in self.aliases.items():
            if self.resolve(target) == canonical:
                names.append(old)
        return sorted(set(names))

    # -------------------------------------------------------------- children

    def children_of(self, node: str) -> Optional[LineageEvent]:
        matches = [e for e in self.lineage if self.resolve(e.node) == node]
        return matches[-1] if matches else None

    def children_map(self) -> dict[str, LineageEvent]:
        result: dict[str, LineageEvent] = {}
        for event in self.lineage:
            result[self.resolve(event.node)] = event
        return result

    def roots(self) -> list[str]:
        """Present elements that are not a lineage child of another node."""

        child_nodes: set[str] = set()
        for event in self.lineage:
            for child in event.children:
                child_nodes.add(self.resolve(child))
        return [
            guid
            for guid, element in self.elements.items()
            if element.present and guid not in child_nodes
        ]

    # -------------------------------------------------------------- snapshots

    def snapshot_records(self) -> Iterator[dict[str, Any]]:
        yield {
            "kind": "meta",
            "seq": self.seq,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "effective_at": self.effective_at,
        }
        for element in self.elements.values():
            yield {"kind": "element", "value": element.to_dict()}
        for old, new in sorted(self.aliases.items()):
            yield {"kind": "alias", "old": old, "new": new}
        for event in self.lineage:
            yield {"kind": "lineage", "value": event.to_dict()}

    @classmethod
    def from_snapshot_records(cls, records: Iterable[dict[str, Any]]) -> "TableState":
        state = cls()
        for record in records:
            kind = record.get("kind")
            if kind == "meta":
                state.seq = int(record["seq"])
                state.revision_id = record.get("revision_id")
                state.content_hash = record.get("content_hash")
                state.effective_at = record.get("effective_at")
            elif kind == "element":
                element = ElementState.from_dict(record["value"])
                state.elements[element.guid] = element
            elif kind == "alias":
                state.aliases[record["old"]] = record["new"]
            elif kind == "lineage":
                state.lineage.append(LineageEvent.from_dict(record["value"]))
        return state

    def clone(self) -> "TableState":
        return TableState(
            elements={guid: replace(element) for guid, element in self.elements.items()},
            aliases=dict(self.aliases),
            lineage=list(self.lineage),
            seq=self.seq,
            revision_id=self.revision_id,
            content_hash=self.content_hash,
            effective_at=self.effective_at,
        )


# ---------------------------------------------------------------------------
# revision application


@dataclass
class RevisionPlan:
    """Everything needed to apply one revision, computed before mutation.

    ``components`` may be empty when the caller streams the component file and
    intends to load it lazily at apply time (large models); ``element_count``
    then carries the streamed count.
    """

    header: RevisionHeader
    components: list[Component]
    raw_digest: str
    content_hash: str
    effective_at: str
    element_count: int = 0

    def __post_init__(self) -> None:
        if not self.element_count:
            self.element_count = len(self.components)

    @property
    def seq(self) -> int:
        return self.header.seq


def build_revision_plan(
    header: RevisionHeader,
    components: Sequence[Component],
    raw_digest: str,
    revision_hash: str,
    ingested_at: str,
    previous_effective_at: Optional[str],
    element_count: int = 0,
) -> RevisionPlan:
    effective_at = header.effective_at or previous_effective_at or ingested_at
    parse_timestamp(effective_at, "effective_at")
    components = sorted(components, key=lambda c: c.guid)
    return RevisionPlan(
        header=header,
        components=list(components),
        raw_digest=raw_digest,
        content_hash=revision_hash,
        effective_at=effective_at,
        element_count=element_count or len(components),
    )


def _resolve_alias(aliases: Mapping[str, str], guid: str) -> str:
    seen: set[str] = set()
    current = guid
    while current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def _check_hierarchy(elements: Mapping[str, ElementState], aliases: Mapping[str, str]) -> None:
    for guid, element in elements.items():
        if not element.present or not element.parent:
            continue
        seen = {guid}
        current = _resolve_alias(aliases, element.parent)
        while current in elements and elements[current].present and elements[current].parent:
            if current in seen:
                raise IngestError(f"component hierarchy contains a cycle at {current}")
            seen.add(current)
            current = _resolve_alias(aliases, elements[current].parent)
        if current in seen:
            raise IngestError(f"component hierarchy contains a cycle at {current}")


def apply_revision(
    table: TableState, plan: RevisionPlan, revision_ingest_seq: int
) -> dict[str, Any]:
    """Apply ``plan`` to ``table`` in place and return the change description."""

    header = plan.header
    seq = header.seq
    if seq != table.seq + 1:
        raise IngestError(
            f"revision {seq} cannot follow revision {table.seq}; ingest order must be contiguous"
        )

    aliases = dict(table.aliases)
    alias_added: dict[str, str] = {}
    relocate_records: list[dict[str, Any]] = []
    for remap in header.remaps:
        old, new = remap.from_guid, remap.to_guid
        existing_target = _resolve_alias(aliases, old)
        new_target = _resolve_alias(aliases, new)
        if existing_target in table.elements and new_target in table.elements:
            raise ConflictError(
                f"guid remap {old}->{new} collides with two distinct live elements"
            )
        resolved_old = existing_target
        if resolved_old != new_target:
            aliases[old] = new_target
            alias_added[old] = new_target
            element = table.elements.pop(resolved_old, None)
            if element is not None:
                # Re-keying is recorded explicitly so that replaying the change
                # blob reconstructs exactly this table (and so the same payload
                # re-ingested cannot double count the element).
                element.guid = new_target
                table.elements[new_target] = element
                relocate_records.append(
                    {"kind": "relocate", "old": resolved_old, "new": new_target}
                )

    # ---- canonicalise incoming components through the alias map -----------
    incoming: dict[str, Component] = {}
    for component in plan.components:
        canonical = _resolve_alias(aliases, component.guid)
        if canonical != component.guid:
            component = Component(
                guid=canonical,
                planned=component.planned,
                unit=component.unit,
                activity=component.activity,
                parent_guid=component.parent_guid,
                ifc_type=component.ifc_type,
                name=component.name,
            )
            if canonical in alias_added:
                alias_added[component.guid] = canonical
        previous = incoming.get(canonical)
        if previous is not None and previous.digest() != component.digest():
            raise IngestError(
                f"component {canonical} appears twice in revision {seq} with different content"
            )
        incoming[canonical] = component

    new_guids = set(incoming)
    previous_snapshot = {
        guid: replace(element) for guid, element in table.elements.items()
    }

    # ---- declared lineage events ------------------------------------------
    split_parents = {_resolve_alias(aliases, s.parent_guid) for s in header.splits}
    merge_sources: set[str] = set()
    for merge in header.merges:
        merge_sources.update(_resolve_alias(aliases, s) for s in merge.source_guids)
    merge_targets = {_resolve_alias(aliases, m.target_guid) for m in header.merges}

    # ---- operation classification ----------------------------------------
    added: list[str] = []
    updated: list[str] = []
    for guid in sorted(new_guids):
        if guid in table.elements:
            if table.elements[guid].present:
                updated.append(guid)
            else:
                added.append(guid)
        else:
            added.append(guid)

    if header.mode == "full":
        disappeared = [
            guid
            for guid, element in table.elements.items()
            if element.present and guid not in new_guids
        ]
    else:
        disappears = set()
        for raw in header.removes:
            disappears.add(_resolve_alias(aliases, raw))
        disappeared = [
            guid
            for guid in sorted(disappears)
            if guid in table.elements and table.elements[guid].present
        ]

    accounted = split_parents | merge_sources
    unexpected = [
        guid
        for guid in disappeared
        if guid not in accounted and guid not in new_guids
    ]
    # Full replacement is exactly the way a model expresses deletion; it is
    # never interpreted as "complete".  The list is kept for audit.
    removed = sorted(set(disappeared))

    # ---- mutations: elements ---------------------------------------------
    change_records: list[dict[str, Any]] = list(relocate_records)
    for guid in added + updated:
        component = incoming[guid]
        existing = table.elements.get(guid)
        element = ElementState(
            guid=guid,
            planned=component.planned,
            unit=component.unit,
            activity=component.activity,
            parent=component.parent_guid,
            ifc_type=component.ifc_type,
            name=component.name,
            present=True,
            first_seq=existing.first_seq if existing else seq,
            last_seq=seq,
        )
        table.elements[guid] = element
        change_records.append(
            {
                "kind": "element",
                "change": "add" if guid in added else "update",
                "value": element.to_dict(),
            }
        )
    for guid in removed:
        element = table.elements[guid]
        element.present = False
        element.removed_seq = seq
        element.last_seq = seq
        change_records.append(
            {"kind": "element", "change": "remove", "value": element.to_dict()}
        )

    # ---- mutations: lineage ----------------------------------------------
    lineage_events: list[LineageEvent] = []
    warnings: list[str] = []
    for split in header.splits:
        parent = _resolve_alias(aliases, split.parent_guid)
        if parent in table.elements and table.elements[parent].present:
            raise IngestError(
                f"split parent {parent} is still present in revision {seq}; "
                "a split must replace the parent"
            )
        if table.children_of(parent) is not None:
            raise IngestError(f"{parent} already has lineage children; cannot split again")
        children = tuple(_resolve_alias(aliases, c) for c in split.child_guids)
        missing = [c for c in children if c not in new_guids]
        if missing:
            raise IngestError(f"split children missing from revision {seq}: {missing}")
        prior = previous_snapshot.get(parent)
        if prior is None:
            raise IngestError(f"split parent {parent} is unknown at revision {seq}")
        parent_planned = prior.planned
        child_planned = {c: incoming[c].planned for c in children}
        if split.weights is not None:
            weights = {
                _resolve_alias(aliases, key): value for key, value in split.weights.items()
            }
            unknown = set(weights) - set(children)
            if unknown:
                raise IngestError(f"split weights refer to unknown children: {sorted(unknown)}")
            missing_weights = set(children) - set(weights)
            if missing_weights:
                raise IngestError(f"split weights missing children: {sorted(missing_weights)}")
        else:
            total = sum(child_planned.values())
            if total > 0:
                weights = {c: child_planned[c] / total for c in children}
            else:
                weights = {c: 1.0 / len(children) for c in children}
        # The contribution of the parent is *its own* quantity distributed by
        # weight.  Using the children's current planned quantities here would
        # be wrong whenever a child also absorbed a merge (its planned grows but
        # the parent's share must not).
        contributions = {
            c: parent_planned * weights[c] for c in children
        }
        total_children = sum(child_planned.values())
        if parent_planned > 0 and total_children > 0 and not _close(parent_planned, total_children):
            # Not fatal: models legitimately split geometry whose modelled
            # quantity is not conserved exactly.  The parent's contribution is
            # still distributed by weight, and the mismatch is reported.
            warnings.append(
                f"split of {parent} does not conserve modelled quantity "
                f"(parent {parent_planned}, children {total_children}); "
                "parent contribution distributed by weight"
            )
        event = LineageEvent(
            kind="split",
            node=parent,
            children=children,
            weights=weights,
            seq=seq,
            frozen_planned=contributions,
        )
        table.lineage.append(event)
        lineage_events.append(event)

    for merge in header.merges:
        target = _resolve_alias(aliases, merge.target_guid)
        sources = tuple(_resolve_alias(aliases, s) for s in merge.source_guids)
        if target not in new_guids:
            raise IngestError(f"merge target {target} is missing from revision {seq}")
        # A merge source that survives the revision would keep its own planned
        # weight *and* contribute to the target, double counting the scope.
        lingering = [
            source
            for source in sources
            if source in table.elements and table.elements[source].present
        ]
        if lingering:
            raise IngestError(
                f"merge sources must leave the model in the same revision "
                f"(use 'remove' for delta revisions): {lingering}"
            )
        if table.children_of(target) is not None:
            raise IngestError(f"{target} already has lineage children; cannot merge into it again")
        frozen: dict[str, float] = {}
        missing = []
        for source in sources:
            prior = previous_snapshot.get(source)
            if prior is None or not prior.present:
                missing.append(source)
                continue
            if table.children_of(source) is not None:
                raise IngestError(f"{source} has lineage children and cannot be merged")
            frozen[source] = prior.planned
        if missing:
            raise IngestError(f"merge sources unknown at revision {seq}: {missing}")
        total = sum(frozen.values())
        if total > 0:
            weights = {s: frozen[s] / total for s in sources}
        else:
            weights = {s: 1.0 / len(sources) for s in sources}
        target_planned = incoming[target].planned
        if total > 0 and target_planned > 0 and not _close(total, target_planned):
            warnings.append(
                f"merge into {target} does not conserve modelled quantity "
                f"(sources {total}, target {target_planned})"
            )
        event = LineageEvent(
            kind="merge",
            node=target,
            children=sources,
            weights=weights,
            seq=seq,
            frozen_planned=frozen,
        )
        table.lineage.append(event)
        lineage_events.append(event)

    # ---- mutations: aliases ----------------------------------------------
    for event in lineage_events:
        change_records.append({"kind": "lineage", "value": event.to_dict()})
    for old, new in alias_added.items():
        table.aliases[old] = new
        change_records.append({"kind": "alias", "old": old, "new": new})

    _check_hierarchy(table.elements, table.aliases)

    table.seq = seq
    table.revision_id = header.revision_id
    table.content_hash = plan.content_hash
    table.effective_at = plan.effective_at

    meta = {
        "kind": "meta",
        "seq": seq,
        "revision_id": header.revision_id,
        "content_hash": plan.content_hash,
        "effective_at": plan.effective_at,
        "mode": header.mode,
        "ingest_seq": revision_ingest_seq,
    }
    return {
        "meta": meta,
        "records": [meta, *change_records],
        "added": added,
        "updated": updated,
        "removed": removed,
        "lineage": lineage_events,
        "alias_added": alias_added,
        "unexpected_removals": unexpected,
        "container_nodes": sorted(split_parents | merge_targets),
        "warnings": warnings,
        "revision_ingest_seq": revision_ingest_seq,
    }


# ---------------------------------------------------------------------------
# replay


def apply_change_records(table: TableState, records: Iterable[dict[str, Any]]) -> TableState:
    for record in records:
        kind = record.get("kind")
        if kind == "meta":
            table.seq = int(record["seq"])
            table.revision_id = record.get("revision_id")
            table.content_hash = record.get("content_hash")
            table.effective_at = record.get("effective_at")
        elif kind == "element":
            element = ElementState.from_dict(record["value"])
            table.elements[element.guid] = element
        elif kind == "relocate":
            element = table.elements.pop(record["old"], None)
            if element is not None:
                element.guid = record["new"]
                table.elements[record["new"]] = element
        elif kind == "alias":
            table.aliases[record["old"]] = record["new"]
        elif kind == "lineage":
            table.lineage.append(LineageEvent.from_dict(record["value"]))
    return table


def load_table_at(workspace: Any, project_state: Mapping[str, Any], seq: int) -> TableState:
    """Reconstruct the element table at applied revision ``seq``.

    Uses the newest snapshot at or before ``seq`` and replays only the small
    change blobs after it.  Memory is bounded by one table plus one change
    blob, not by the number of historical revisions.
    """

    snapshots = project_state.get("snapshots", {})
    candidates = sorted(int(s) for s in snapshots if int(s) <= seq)
    if candidates:
        start = candidates[-1]
        table = TableState.from_snapshot_records(
            workspace.read_jsonl_blob("snapshots", snapshots[str(start)])
        )
        if table.seq != start:
            raise IngestError(f"snapshot at {start} is inconsistent")
    else:
        start = 0
        table = TableState()

    for revision in project_state.get("revisions", []):
        rseq = int(revision["seq"])
        if start < rseq <= seq:
            apply_change_records(
                table, workspace.read_jsonl_blob("changes", revision["change_blob"])
            )
    if table.seq != seq:
        raise IngestError(f"could not reconstruct revision {seq} (got {table.seq})")
    return table


def empty_table() -> TableState:
    return TableState()
