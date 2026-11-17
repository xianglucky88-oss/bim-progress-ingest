"""Semantics of revisions, GUID identity, splits, merges and partial progress."""

from __future__ import annotations

import unittest

from support import WorkspaceCase


class TestRevisionsAndIdentity(WorkspaceCase):
    def test_full_revision_ingest_and_progress(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [
                        {"guid": "W1", "planned": 100, "activity": "A1"},
                        {"guid": "W2", "planned": 100, "activity": "A1"},
                    ], effective_at="2026-02-01T00:00:00Z"),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [
                        {"guid": "W1", "completed": 40},
                        {"guid": "W2", "fraction": 0.5},
                    ]),
                ],
            )
        )
        summary = self.queries.summary("P")
        self.assertEqual(summary["planned"], 200)
        self.assertEqual(summary["completed"], 90)
        self.assertEqual(summary["progress_pct"], 45.0)
        self.assertEqual(summary["revision"]["seq"], 1)
        self.assertEqual(summary["revision"]["revision_id"], "R1")

    def test_guid_remap_preserves_identity_and_history(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [{"guid": "OLD", "planned": 100, "activity": "A1"}]),
                    self.rev(
                        2,
                        [{"guid": "NEW", "planned": 100, "activity": "A1"}],
                        remap=[{"from": "OLD", "to": "NEW"}],
                    ),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [{"guid": "OLD", "completed": 30}]),
                    self.report("D2", "2026-02-03", [{"guid": "NEW", "completed": 55}]),
                ],
            )
        )
        # Old GUID still resolvable on the historical revision.
        at_one = self.queries.element("P", "OLD", 1, "2026-02-02")
        self.assertEqual(at_one["guid"], "OLD")
        self.assertEqual(at_one["completed"], 30)
        # After the remap the same identity answers under either name, and
        # only the most recent statement counts (no 30 + 55 double count).
        new_view = self.queries.element("P", "NEW", 2, "2026-02-03")
        self.assertEqual(new_view["completed"], 55)
        old_view = self.queries.element("P", "OLD", 2, "2026-02-03")
        self.assertEqual(old_view["canonical_guid"], "NEW")
        self.assertEqual(old_view["completed"], 55)
        self.assertEqual(old_view["aliases"], ["NEW", "OLD"])
        # Historical as-of answer is unaffected by later revisions.
        self.assertEqual(self.queries.summary("P", 1, "2026-02-02")["completed"], 30)
        self.assertEqual(self.queries.summary("P", 1, "2026-02-02")["planned"], 100)

    def test_split_distributes_parent_completion_by_weight(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [{"guid": "W", "planned": 100, "activity": "A1"}]),
                    self.rev(
                        2,
                        [
                            {"guid": "W-A", "planned": 25, "activity": "A1"},
                            {"guid": "W-B", "planned": 75, "activity": "A1"},
                        ],
                        splits=[{"parent": "W", "children": ["W-A", "W-B"],
                                 "weights": {"W-A": 0.25, "W-B": 0.75}}],
                    ),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [{"guid": "W", "fraction": 0.4}]),
                ],
            )
        )
        at_rev2 = self.queries.summary("P", 2, "2026-02-02")
        self.assertEqual(at_rev2["planned"], 100)
        self.assertAlmostEqual(at_rev2["completed"], 40.0)
        self.assertAlmostEqual(at_rev2["elements"]["W-A"]["completed"], 10.0)
        self.assertAlmostEqual(at_rev2["elements"]["W-B"]["completed"], 30.0)
        # Parent is retained for audit with its lineage role, not as progress.
        parent = at_rev2["elements"]["W"]
        self.assertFalse(parent["present"])
        self.assertEqual(parent["lineage_role"], "split_parent")
        self.assertEqual(parent["children"], ["W-A", "W-B"])
        self.assertFalse(parent["is_progress_unit"])
        # Nothing is counted twice: progress units are the two children only.
        self.assertEqual(at_rev2["counts"]["progress_units"], 2)

    def test_child_reports_supersede_inherited_split_share(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [{"guid": "W", "planned": 100}]),
                    self.rev(
                        2,
                        [
                            {"guid": "W-A", "planned": 50},
                            {"guid": "W-B", "planned": 50},
                        ],
                        splits=[{"parent": "W", "children": ["W-A", "W-B"],
                                 "weights": {"W-A": 0.5, "W-B": 0.5}}],
                    ),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [{"guid": "W", "completed": 30}]),
                    self.report("D2", "2026-02-05", [
                        {"guid": "W-A", "completed": 40},
                        {"guid": "W-B", "completed": 10},
                    ]),
                ],
            )
        )
        early = self.queries.summary("P", 2, "2026-02-02")
        self.assertAlmostEqual(early["completed"], 30.0)
        late = self.queries.summary("P", 2, "2026-02-05")
        self.assertAlmostEqual(late["completed"], 50.0)
        self.assertAlmostEqual(late["elements"]["W-A"]["completed"], 40.0)
        self.assertAlmostEqual(late["elements"]["W-B"]["completed"], 10.0)

    def test_merge_preserves_source_contribution(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [
                        {"guid": "A", "planned": 60, "activity": "A1"},
                        {"guid": "B", "planned": 40, "activity": "A1"},
                    ]),
                    self.rev(
                        2,
                        [{"guid": "AB", "planned": 100, "activity": "A1"}],
                        merges=[{"sources": ["A", "B"], "target": "AB"}],
                    ),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [
                        {"guid": "A", "completed": 30},
                        {"guid": "B", "completed": 40},
                    ]),
                ],
            )
        )
        at_rev2 = self.queries.summary("P", 2, "2026-02-02")
        self.assertEqual(at_rev2["planned"], 100)
        self.assertAlmostEqual(at_rev2["completed"], 70.0)
        target = at_rev2["elements"]["AB"]
        self.assertEqual(target["lineage_role"], "merge_target")
        self.assertEqual(sorted(target["children"]), ["A", "B"])
        relations = sorted(e["relation"] for e in target["evidence"])
        self.assertEqual(relations, ["merge", "merge"])
        # Sources remain visible for audit but contribute no weight.
        self.assertFalse(at_rev2["elements"]["A"]["present"])
        self.assertEqual(at_rev2["counts"]["progress_units"], 1)

    def test_merge_target_direct_report_supersedes_absorbed_sources(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [
                        {"guid": "A", "planned": 60},
                        {"guid": "B", "planned": 40},
                    ]),
                    self.rev(
                        2,
                        [{"guid": "AB", "planned": 100}],
                        merges=[{"sources": ["A", "B"], "target": "AB"}],
                    ),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [
                        {"guid": "A", "completed": 30},
                        {"guid": "B", "completed": 40},
                    ]),
                    self.report("D2", "2026-02-04", [{"guid": "AB", "completed": 80}]),
                ],
            )
        )
        early = self.queries.summary("P", 2, "2026-02-02")
        self.assertAlmostEqual(early["completed"], 70.0)
        late = self.queries.summary("P", 2, "2026-02-04")
        self.assertAlmostEqual(late["completed"], 80.0)

    def test_disappearing_component_is_deleted_not_completed(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [
                        {"guid": "KEEP", "planned": 100, "activity": "A1"},
                        {"guid": "GONE", "planned": 100, "activity": "A1"},
                    ]),
                    self.rev(2, [{"guid": "KEEP", "planned": 100, "activity": "A1"}]),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [
                        {"guid": "KEEP", "completed": 100},
                        {"guid": "GONE", "completed": 100},
                    ]),
                ],
            )
        )
        at_rev1 = self.queries.summary("P", 1, "2026-02-02")
        self.assertEqual(at_rev1["planned"], 200)
        self.assertEqual(at_rev1["completed"], 200)
        at_rev2 = self.queries.summary("P", 2, "2026-02-02")
        # The denominator drops the deleted component; it is never counted as
        # complete work that no longer exists in the model.
        self.assertEqual(at_rev2["planned"], 100)
        self.assertEqual(at_rev2["completed"], 100)
        self.assertFalse(at_rev2["elements"]["GONE"]["present"])
        self.assertEqual(at_rev2["elements"]["GONE"]["completed"], 0.0)
        # A report still aimed at the deleted component is surfaced, not lost.
        self.assertEqual(len(at_rev2["unallocated"]), 1)
        self.assertEqual(at_rev2["unallocated"][0]["reported_guid"], "GONE")

    def test_partial_completion_is_cumulative_not_additive(self):
        self.ingest(
            self.document(
                revisions=[self.rev(1, [{"guid": "W", "planned": 100}])],
                reports=[
                    self.report("D1", "2026-02-02", [{"guid": "W", "completed": 20}]),
                    self.report("D2", "2026-02-03", [{"guid": "W", "completed": 35}]),
                    self.report("D3", "2026-02-04", [{"guid": "W", "completed": 50}]),
                ],
            )
        )
        self.assertEqual(self.queries.element("P", "W", None, "2026-02-02")["completed"], 20)
        self.assertEqual(self.queries.element("P", "W", None, "2026-02-03")["completed"], 35)
        self.assertEqual(self.queries.element("P", "W", None, "2026-02-04")["completed"], 50)
        self.assertEqual(self.queries.summary("P", None, "2026-02-04")["progress_pct"], 50.0)

    def test_hierarchy_inherits_activity_and_containers_are_not_counted(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [
                        {"guid": "LEVEL", "planned": 0, "activity": "A1"},
                        {"guid": "W1", "planned": 60, "parent": "LEVEL"},
                        {"guid": "W2", "planned": 40, "parent": "LEVEL"},
                    ]),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [
                        {"guid": "W1", "completed": 30},
                        {"guid": "W2", "completed": 20},
                    ]),
                ],
            )
        )
        summary = self.queries.summary("P")
        self.assertEqual(summary["planned"], 100)
        self.assertEqual(summary["completed"], 50)
        self.assertEqual(summary["counts"]["containers"], 1)
        self.assertEqual(summary["counts"]["progress_units"], 2)
        self.assertEqual(summary["activities"]["A1"]["planned"], 100)
        self.assertEqual(summary["activities"]["A1"]["completed"], 50)

    def test_nested_split_then_merge_chain_preserves_contribution(self):
        self.ingest(self.document(
            revisions=[
                self.rev(1, [{"guid": "W", "planned": 1000, "activity": "A1"}]),
                self.rev(2, [
                    {"guid": "WA", "planned": 400, "activity": "A1"},
                    {"guid": "WB", "planned": 600, "activity": "A1"},
                ], splits=[{"parent": "W", "children": ["WA", "WB"]}]),
                self.rev(3, [{"guid": "WX", "planned": 1000, "activity": "A1"}],
                         merges=[{"sources": ["WA", "WB"], "target": "WX"}]),
            ],
            reports=[
                self.report("D1", "2026-05-02", [{"guid": "W", "fraction": 0.5}]),
            ],
        ))
        # The same 50% survives the split and the re-merge at every revision.
        for revision in (1, 2, 3):
            summary = self.queries.summary("P", revision, "2026-05-02")
            self.assertEqual(summary["planned"], 1000)
            self.assertAlmostEqual(summary["completed"], 500.0)

        # A direct child report supersedes only that child's inherited share.
        self.ingest(self.document(
            reports=[self.report("D2", "2026-05-03", [{"guid": "WA", "fraction": 1.0}])],
        ))
        rev2 = self.queries.summary("P", 2, "2026-05-03")
        self.assertAlmostEqual(rev2["elements"]["WA"]["completed"], 400.0)
        self.assertAlmostEqual(rev2["elements"]["WB"]["completed"], 300.0)
        self.assertAlmostEqual(rev2["completed"], 700.0)
        # The chain still resolves through the merge into the single target.
        rev3 = self.queries.summary("P", 3, "2026-05-03")
        self.assertAlmostEqual(rev3["elements"]["WX"]["completed"], 700.0)

    def test_activity_rollup_and_unknown_activity_bucket(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [
                        {"guid": "W1", "planned": 100, "activity": "A1"},
                        {"guid": "X1", "planned": 50},
                    ]),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [
                        {"guid": "W1", "completed": 60},
                        {"guid": "X1", "completed": 10},
                    ]),
                ],
            )
        )
        summary = self.queries.summary("P")
        self.assertEqual(summary["activities"]["A1"]["completed"], 60)
        self.assertEqual(summary["activities"]["UNASSIGNED"]["completed"], 10)
        self.assertEqual(self.queries.activity("P", "A1")["planned"], 100)

    def test_delta_revisions_accumulate(self):
        self.ingest(
            self.document(
                revisions=[
                    self.rev(1, [{"guid": "A", "planned": 10, "activity": "A1"},
                                 {"guid": "B", "planned": 20, "activity": "A1"}]),
                    self.rev(2, [{"guid": "C", "planned": 30, "activity": "A2"}],
                             mode="delta"),
                    self.rev(3, [], mode="delta", remove=["A"]),
                ],
                reports=[
                    self.report("D1", "2026-02-02", [
                        {"guid": "A", "completed": 10},
                        {"guid": "B", "completed": 10},
                        {"guid": "C", "completed": 0},
                    ]),
                ],
            )
        )
        summary = self.queries.summary("P", 3, "2026-02-02")
        self.assertEqual(summary["planned"], 50)  # 20 + 30, A removed
        self.assertEqual(summary["completed"], 10)
        self.assertFalse(summary["elements"]["A"]["present"])

    def test_custom_weights_must_sum_to_one(self):
        with self.assertRaises(Exception):
            self.ingest(
                self.document(
                    revisions=[
                        self.rev(1, [{"guid": "W", "planned": 100}]),
                        self.rev(2, [
                            {"guid": "A", "planned": 50},
                            {"guid": "B", "planned": 50},
                        ], splits=[{"parent": "W", "children": ["A", "B"],
                                    "weights": {"A": 0.9, "B": 0.9}}]),
                    ],
                )
            )

    def test_hierarchy_cycle_is_rejected(self):
        with self.assertRaises(Exception):
            self.ingest(
                self.document(
                    revisions=[
                        self.rev(1, [
                            {"guid": "A", "planned": 10, "parent": "B"},
                            {"guid": "B", "planned": 10, "parent": "A"},
                        ]),
                    ],
                )
            )

    def test_merge_sources_must_leave_the_model(self):
        with self.assertRaises(Exception):
            self.ingest(
                self.document(
                    revisions=[
                        self.rev(1, [
                            {"guid": "A", "planned": 10},
                            {"guid": "B", "planned": 10},
                        ]),
                        # B is still present although it was merged into AB.
                        self.rev(2, [
                            {"guid": "AB", "planned": 20},
                            {"guid": "B", "planned": 10},
                        ], mode="delta",
                        merges=[{"sources": ["A"], "target": "AB"}]),
                    ],
                )
            )

    def test_element_lifecycle_sequences_are_recorded(self):
        self.ingest(self.document(revisions=[
            self.rev(1, [
                {"guid": "A", "planned": 10},
                {"guid": "B", "planned": 10},
            ]),
            self.rev(2, [{"guid": "A", "planned": 20}]),
        ]))
        from bim_progress_ingest.history import load_table_at

        pstate = self.workspace.state["projects"]["P"]
        table = load_table_at(self.workspace, pstate, 2)
        self.assertEqual(table.elements["A"].first_seq, 1)
        self.assertEqual(table.elements["A"].last_seq, 2)
        self.assertEqual(table.elements["A"].planned, 20)
        self.assertFalse(table.elements["B"].present)
        self.assertEqual(table.elements["B"].removed_seq, 2)
        self.assertEqual(table.elements["B"].first_seq, 1)
        # Historical revision still sees B alive.
        table1 = load_table_at(self.workspace, pstate, 1)
        self.assertTrue(table1.elements["B"].present)
        self.assertEqual(table1.elements["A"].planned, 10)


if __name__ == "__main__":
    unittest.main()
