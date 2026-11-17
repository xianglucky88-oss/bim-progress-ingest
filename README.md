# BIM Progress Ingest

Link BIM components to schedule activities and answer, for **any model revision
and any date**, how much of the work was actually complete — with every value
traceable back to the model revision it came from and the daily report that
reported it.

The original baseline (GUID → activity weighting over a single JSON document)
still works unchanged. The revision-aware layer is purely additive.

## Quick start

```bash
# Legacy single-document calculation (behaviour unchanged)
PYTHONPATH=src python -m bim_progress_ingest.cli

# Revision-aware workspace
PYTHONPATH=src python -m bim_progress_ingest.cli ingest    --workspace /tmp/ws doc.json
PYTHONPATH=src python -m bim_progress_ingest.cli summary   --workspace /tmp/ws --project P --revision 2 --as-of 2026-03-06
PYTHONPATH=src python -m bim_progress_ingest.cli element   --workspace /tmp/ws --project P W-NEW --as-of 2026-03-06
PYTHONPATH=src python -m bim_progress_ingest.cli activity  --workspace /tmp/ws --project P A1 --revision 1
PYTHONPATH=src python -m bim_progress_ingest.cli revisions --workspace /tmp/ws --project P
PYTHONPATH=src python -m bim_progress_ingest.cli reconcile --workspace /tmp/ws --project P
PYTHONPATH=src python -m bim_progress_ingest.cli recover   --workspace /tmp/ws
PYTHONPATH=src python -m bim_progress_ingest.cli gc        --workspace /tmp/ws

# Full end-to-end demonstration (temp workspace, deterministic data)
PYTHONPATH=src python -m bim_progress_ingest.pipeline

# Tests
PYTHONPATH=src:tests python -m unittest discover -s tests -v
```

## Input documents

A single JSON object, or a JSON-Lines stream of `kind`-tagged entries:

```jsonc
{
  "project": "tower-a",
  "plans": [                               // WBS / schedule versions
    {"plan_id": "WBS-1", "effective_date": "2026-01-01",
     "activities": [{"id": "A1", "name": "Structure", "wbs_code": "1.1"}]}
  ],
  "revisions": [
    { "seq": 1, "revision_id": "R1", "mode": "full",
      "effective_at": "2026-01-02T00:00:00Z",
      "components": [{"guid": "W1", "planned": 100, "activity": "A1",
                      "parent": "LEVEL-1"}] },

    { "seq": 2, "revision_id": "R2", "mode": "delta",
      "guid_remap": [{"from": "W2", "to": "WALL-2"}],
      "splits":  [{"parent": "W1", "children": ["W1A", "W1B"],
                   "weights": {"W1A": 0.25, "W1B": 0.75}}],
      "merges":  [{"sources": ["C1"], "target": "W1B"}],
      "remove":  ["D1"],
      "components": [ /* only changed/added components */ ] }
  ],
  "reports": [                             // daily site reports
    {"report_id": "D-0106", "effective_date": "2026-01-06",
     "source": "site-daily",
     "lines": [{"guid": "W1A", "fraction": 0.8},
               {"guid": "WALL-2", "completed": 50}]}
  ]
}
```

Large component sets can live in a sidecar, which is streamed and never fully
materialised:

```jsonc
{"seq": 1, "revision_id": "R1", "components_ref": "exports/r1.jsonl"}
```

### Field reference

| Field | Meaning |
| --- | --- |
| `seq` | Contiguous revision number per project. Gaps are buffered, not applied. |
| `revision_id` | Human identifier, also accepted by `--revision`. |
| `mode` | `full` (default) replaces the element set; `delta` applies only listed changes. |
| `effective_at` | Timestamp this revision became the model of record. |
| `guid_remap` | Rename one GUID to another while keeping identity and history. |
| `splits` | Replace one component with several; `weights` default to planned-quantity shares. |
| `merges` | Fold several components into one; source contribution is preserved. |
| `remove` | Explicit removals for `delta` revisions. |
| report `completed` | Cumulative quantity complete, in the component's unit. |
| report `fraction` / `percent` | Cumulative fraction complete (0–1) or percent (0–100). |

## Guarantees

**Revision-aware as-of answers.** `summary`, `element` and `activity` accept
`--revision` and `--as-of`. The element table is reconstructed from the newest
snapshot at or before that revision, so later revisions cannot change an
already-resolved historical answer. Every result echoes the `revision_hash` and
the on-disk descriptor path.

**GUID remapping.** A remap re-keys the live element in place and records an
alias. Old and new GUIDs resolve to the same identity; reports under either name
collapse onto one statement (newest wins) rather than being summed.

**Split / merge contribution.** Lineage events store the weights *and* the frozen
planned quantities at the moment they happened. Splitting distributes the
parent's stated completion to children by weight; merging transfers a source's
measurement 1:1 to the target. A direct report on a child or target supersedes
the inherited share instead of adding to it, so nothing is counted twice.

**Partial completion.** Report values are cumulative, not additive. The value in
effect on a date is the newest report with `effective_date <= date`, ordered by
`(effective_date, ingest_seq)`.

**Deletions are not completions.** A component that disappears from a
replacement model (and is not part of a split/merge) loses its planned weight
from the denominator and contributes zero completion. Any report still aimed at
it is reported under `unallocated` rather than silently dropped.

**Idempotency.** Revisions are content-addressed over their header plus the
sorted component set, so re-parsing the same model — even with reordered lines —
is a no-op. Reports are keyed by `report_id` + content hash; re-uploading the
same bytes is a no-op and changed bytes create a new version.

**Late reports.** A late report revises only the date range it covers: it
changes answers for its own `effective_date` onward until the next report, never
an earlier day and never a later day's value.

**Crash safety.** Components are streamed into a content-addressed blob and a
compressed change blob; the visible state moves by one atomic `state.json`
replace guarded by a write-ahead journal. A crash before the swap rolls back to
the previous generation; after the swap it rolls forward. Re-running a failed
ingest therefore cannot double progress.

**Out-of-order revisions.** A revision ahead of the head is stored as a pending
delta and applied automatically once the gap is filled. `reconcile` reports gaps
and pending deltas.

## Storage layout

```
ws/
  state.json                  # atomic pointer: per-project revisions/reports/plans/snapshots
  journal.json                # write-ahead journal, absent when idle
  lock                        # single-writer advisory lock
  blobs/inputs/<sha>.jsonl.z  # normalised components per revision
  blobs/changes/<sha>.jsonl.z # per-revision change records
  blobs/snapshots/<sha>.jsonl.z # periodic full element tables
  blobs/reports/<sha>.json    # immutable report versions
  blobs/plans/<sha>.json
  revisions/<project>/<seq>-<hash>.json  # per-revision descriptor
```

Snapshots are written every 5 revisions by default (`Ingester(..., snapshot_interval=N)`
overrides this), bounding replay to at most that many small change blobs.
Reconstructing a historical table never loads more than one snapshot plus the
change blobs after it.

## Modules

| Module | Responsibility |
| --- | --- |
| `domain.py` | Immutable inputs: components, remaps, lineage, reports, plans. |
| `parser.py` | Validation, JSON/JSON-Lines loading, streamed component sources. |
| `history.py` | Element table, identity/alias graph, lineage, change replay. |
| `progress.py` | Partial completion, report precedence, allocation, provenance. |
| `ingest.py` | Transactional orchestration, idempotency, ordering, snapshots. |
| `queries.py` | As-of queries, revision resolution, reconciliation. |
| `storage.py` | Content-addressed blobs, atomic commits, journal, GC. |
| `models.py`, `engine.py`, `report.py`, `pipeline.py`, `cli.py` | Legacy path, retained unchanged in behaviour. |
