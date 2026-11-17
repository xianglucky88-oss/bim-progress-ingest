"""Combination scenario required by the brief.

A single project exercises GUID remap + split + merge + deletion + partial
completion + duplicate reports + late reports + out-of-order revisions + a
mid-flight failure and a recovery re-run.  The historical as-of answers are
snapshotted before the final revision is ingested and must be identical after
it, proving no pollution from the current model.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from support import WorkspaceCase


class TestCombinationHistory(WorkspaceCase):
    def build(self):
        return [
            # R1: four components, one is a container parent.
            self.rev(1, [
                {"guid": "W-OLD", "planned": 100, "activity": "A1", "parent": "LEVEL"},
                {"guid": "C-OLD", "planned": 50, "activity": "A2"},
                {"guid": "D-OLD", "planned": 10, "activity": "A1"},
                {"guid": "P-OLD", "planned": 40, "activity": "A2"},
            ], effective_at="2026-03-01T00:00:00Z"),
            # R2: W-OLD renamed, C-OLD split in two, P-OLD deleted.
            self.rev(
                2,
                [
                    {"guid": "W-NEW", "planned": 100, "activity": "A1", "parent": "LEVEL"},
                    {"guid": "C-A", "planned": 20, "activity": "A2"},
                    {"guid": "C-B", "planned": 30, "activity": "A2"},
                    {"guid": "D-OLD", "planned": 10, "activity": "A1"},
                ],
                effective_at="2026-03-05T00:00:00Z",
                remap=[{"from": "W-OLD", "to": "W-NEW"}],
                splits=[{"parent": "C-OLD", "children": ["C-A", "C-B"],
                         "weights": {"C-A": 0.4, "C-B": 0.6}}],
            ),
            # R3: the split children are re-merged into one component.
            self.rev(
                3,
                [
                    {"guid": "W-NEW", "planned": 100, "activity": "A1", "parent": "LEVEL"},
                    {"guid": "C-MERGED", "planned": 50, "activity": "A2"},
                    {"guid": "D-OLD", "planned": 10, "activity": "A1"},
                ],
                effective_at="2026-03-09T00:00:00Z",
                merges=[{"sources": ["C-A", "C-B"], "target": "C-MERGED"}],
            ),
        ]

    def reports(self):
        return [
            self.report("RPT-1", "2026-03-02", [
                {"guid": "W-OLD", "fraction": 0.5},
                {"guid": "C-OLD", "completed": 10},
                {"guid": "P-OLD", "completed": 40},
            ]),
            self.report("RPT-2", "2026-03-06", [
                {"guid": "C-A", "fraction": 0.5},
                {"guid": "C-B", "completed": 15},
            ]),
            # Duplicate of RPT-2 (same bytes) must be a no-op.
            self.report("RPT-2", "2026-03-06", [
                {"guid": "C-A", "fraction": 0.5},
                {"guid": "C-B", "completed": 15},
            ]),
            # Late report for 2026-03-03, uploaded after 03-06.
            self.report("RPT-LATE", "2026-03-03", [
                {"guid": "W-OLD", "fraction": 0.3},
            ]),
            # Report on the merged target.
            self.report("RPT-3", "2026-03-10", [
                {"guid": "C-MERGED", "completed": 45},
                {"guid": "W-NEW", "completed": 80},
            ]),
        ]

    def test_full_combination_and_history_isolation(self):
        doc = self.document(
            revisions=self.build(),
            reports=self.reports(),
            plans=[{
                "plan_id": "WBS",
                "effective_date": "2026-02-01",
                "activities": [
                    {"id": "A1", "name": "Structure", "wbs_code": "1.1"},
                    {"id": "A2", "name": "Envelope", "wbs_code": "1.2"},
                ],
            }],
        )
        first = self.ingest(doc)
        applied = first["results"][1]["applied"]
        self.assertEqual([a["seq"] for a in applied], [1, 2, 3])
        report_results = first["results"][2]["reports"]
        self.assertEqual(
            [r["status"] for r in report_results["RPT-2"]], ["stored", "duplicate"]
        )

        # Snapshot the historical answers at each revision/date.
        checkpoints = {}
        for revision, as_of in [
            (1, "2026-03-02"),
            (1, "2026-03-03"),
            (2, "2026-03-03"),
            (2, "2026-03-06"),
            (3, "2026-03-06"),
            (3, "2026-03-10"),
        ]:
            checkpoints[(revision, as_of)] = self.queries.summary("P", revision, as_of)

        # --- checks at revision 1 ----------------------------------------
        r1_0302 = checkpoints[(1, "2026-03-02")]
        self.assertEqual(r1_0302["planned"], 200)  # 100 + 50 + 10 + 40
        self.assertAlmostEqual(r1_0302["completed"], 100)  # 50 + 10 + 40
        self.assertAlmostEqual(r1_0302["activities"]["A1"]["completed"], 50)
        self.assertAlmostEqual(r1_0302["activities"]["A2"]["completed"], 50)

        # Late report revises 03-03 only; 03-02 stays put.
        self.assertAlmostEqual(
            checkpoints[(1, "2026-03-03")]["elements"]["W-OLD"]["completed"], 30
        )
        self.assertAlmostEqual(
            checkpoints[(1, "2026-03-02")]["elements"]["W-OLD"]["completed"], 50
        )

        # --- checks at revision 2 ----------------------------------------
        r2_0303 = checkpoints[(2, "2026-03-03")]
        # Identity preserved across the rename: W-NEW carries W-OLD's 30.
        self.assertAlmostEqual(r2_0303["elements"]["W-NEW"]["completed"], 30, places=6)
        self.assertEqual(r2_0303["elements"]["W-NEW"]["aliases"], ["W-NEW", "W-OLD"])
        # Split still inherits the R1 report (10 completed on C-OLD).
        self.assertAlmostEqual(r2_0303["elements"]["C-A"]["completed"], 4.0)
        self.assertAlmostEqual(r2_0303["elements"]["C-B"]["completed"], 6.0)
        # The deleted P-OLD no longer contributes planned quantity.
        self.assertEqual(r2_0303["planned"], 160)  # 100 + 20 + 30 + 10
        self.assertFalse(r2_0303["elements"]["P-OLD"]["present"])
        self.assertEqual(r2_0303["elements"]["P-OLD"]["completed"], 0.0)
        self.assertTrue(any(u["reported_guid"] == "P-OLD" for u in r2_0303["unallocated"]))

        r2_0306 = checkpoints[(2, "2026-03-06")]
        # Children now reported directly: 10 + 15 replaces the inherited 4/6.
        self.assertAlmostEqual(r2_0306["elements"]["C-A"]["completed"], 10.0)
        self.assertAlmostEqual(r2_0306["elements"]["C-B"]["completed"], 15.0)
        self.assertAlmostEqual(r2_0306["completed"], 55.0)  # 30 + 10 + 15

        # --- checks at revision 3 (merge) --------------------------------
        r3_0306 = checkpoints[(3, "2026-03-06")]
        self.assertEqual(r3_0306["planned"], 160)
        self.assertAlmostEqual(r3_0306["completed"], 55.0)
        merged = r3_0306["elements"]["C-MERGED"]
        self.assertEqual(merged["lineage_role"], "merge_target")
        self.assertEqual(merged["children"], ["C-A", "C-B"])
        relations = sorted(e["relation"] for e in merged["evidence"])
        self.assertEqual(relations, ["merge", "merge"])

        r3_0310 = checkpoints[(3, "2026-03-10")]
        self.assertEqual(
            (r3_0310["elements"]["C-MERGED"]["completed"],
             r3_0310["elements"]["W-NEW"]["completed"]),
            (45.0, 80.0),
        )
        self.assertAlmostEqual(r3_0310["completed"], 125.0)
        # Every value carries a report + revision pointer.
        for element in r3_0310["elements"].values():
            if element["present"] and element["completed"]:
                self.assertTrue(element["evidence"])
                for evidence in element["evidence"]:
                    self.assertTrue(evidence["report_id"])
                    self.assertTrue(evidence["content_hash"])
        self.assertTrue(r3_0310["revision"]["revision_hash"])

        # --- the current model must not pollute history -------------------
        # Ingest a 4th revision that rewrites quantities, plus a fresh report
        # dated *after* every checkpoint.  Historical checkpoints must be
        # byte-identical; the new report legitimately applies only from its own
        # effective date onward.
        self.ingest(self.document(
            revisions=[self.rev(4, [
                {"guid": "W-NEW", "planned": 500, "activity": "A1"},
                {"guid": "C-MERGED", "planned": 999, "activity": "A2"},
                {"guid": "D-OLD", "planned": 10, "activity": "A1"},
                {"guid": "NEW-ELEMENT", "planned": 1000, "activity": "A1"},
            ], effective_at="2026-03-20T00:00:00Z")],
            reports=[self.report("RPT-4", "2026-03-21", [
                {"guid": "W-NEW", "completed": 500},
            ])],
        ))
        for (revision, as_of), before in checkpoints.items():
            after = self.queries.summary("P", revision, as_of)
            self.assertEqual(
                after, before,
                f"historical answer for revision {revision} as of {as_of} changed",
            )
        # The head does move, as expected.
        head = self.queries.summary("P", None, "2026-03-21")
        self.assertEqual(head["revision"]["seq"], 4)
        self.assertEqual(head["planned"], 2509)

    def test_failure_midway_then_rerun_has_no_double_progress(self):
        doc = self.document(revisions=self.build(), reports=self.reports())
        self.ingest(doc)
        baseline = {
            (revision, as_of): self.queries.summary("P", revision, as_of)
            for revision, as_of in [(1, "2026-03-02"), (2, "2026-03-06"), (3, "2026-03-10")]
        }
        # Leave a stale journal to emulate a crash after the journal write.
        generation = self.workspace.state["generation"]
        (Path(self.root) / "journal.json").write_text(json.dumps({
            "expected_generation": generation,
            "target_generation": generation + 1,
            "intent": {"op": "revisions"},
        }))
        self.reopen()
        self.assertEqual(self.workspace.last_recovery["action"], "rolled-back")
        # Re-running the whole document after recovery is a no-op.
        again = self.ingest(copy.deepcopy(doc))
        revision_result = next(
            r for r in again["results"] if r["kind"] == "revisions"
        )
        self.assertEqual(revision_result["applied"], [])
        self.assertEqual(len(revision_result["duplicates"]), 3)
        for key, before in baseline.items():
            self.assertEqual(self.queries.summary("P", *key), before)


if __name__ == "__main__":
    unittest.main()
