"""Out-of-order revisions, crash recovery and repeat-parse safety."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bim_progress_ingest.history import load_table_at
from bim_progress_ingest.storage import Workspace

from support import WorkspaceCase


class TestOrderingAndRecovery(WorkspaceCase):
    def _rev(self, seq, planned, guid=None, **kwargs):
        return self.rev(seq, [{"guid": guid or f"W{seq}", "planned": planned,
                               "activity": "A1"}], **kwargs)

    def test_out_of_order_revisions_are_buffered_then_applied(self):
        self.ingest(self.document(
            revisions=[self._rev(1, 100)],
            reports=[self.report("D1", "2026-02-02", [
                {"guid": "W1", "completed": 40},
            ])],
        ))
        # Revision 3 arrives before revision 2: it must not be applied.
        result = self.ingest(self.document(
            revisions=[self.rev(3, [{"guid": "W3", "planned": 300, "activity": "A1"}],
                                mode="delta")],
            project="P",
        ))
        self.assertEqual(len(result["results"][0]["pending"]), 1)
        self.assertEqual(self.queries.summary("P")["revision"]["seq"], 1)
        # Revision 2 fills the gap; 3 is then drained automatically.
        self.ingest(self.document(
            revisions=[self.rev(2, [{"guid": "W2", "planned": 200, "activity": "A1"}],
                                mode="delta")],
            project="P",
        ))
        summary = self.queries.summary("P")
        self.assertEqual(summary["revision"]["seq"], 3)
        self.assertEqual(summary["planned"], 600)
        self.assertEqual(self.queries.reconcile("P")["pending"], [])
        self.assertEqual(self.queries.reconcile("P")["gaps"], [])

    def test_duplicate_revision_does_not_double_progress(self):
        doc = self.document(
            revisions=[self._rev(1, 100)],
            reports=[self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}])],
            project="P",
        )
        first = self.ingest(doc)
        self.assertEqual(len(first["results"][0]["applied"]), 1)
        second = self.ingest(doc)
        self.assertEqual(len(second["results"][0]["applied"]), 0)
        self.assertEqual(len(second["results"][0]["duplicates"]), 1)
        summary = self.queries.summary("P")
        self.assertEqual(summary["planned"], 100)
        self.assertEqual(summary["completed"], 40)
        self.assertEqual(summary["generation"] if "generation" in summary else 0, 0)

    def test_same_revision_content_in_reordered_file_is_duplicate(self):
        components = [
            {"guid": "A", "planned": 10, "activity": "A1"},
            {"guid": "B", "planned": 20, "activity": "A1"},
        ]
        self.ingest(self.document(revisions=[self.rev(1, components)]), )
        result = self.ingest(self.document(revisions=[self.rev(1, list(reversed(components)))]))
        self.assertEqual(len(result["results"][0]["duplicates"]), 1)
        self.assertEqual(self.queries.summary("P")["planned"], 30)

    def test_conflicting_revision_content_is_rejected(self):
        self.ingest(self.document(revisions=[self._rev(1, 100)]))
        with self.assertRaises(Exception):
            self.ingest(self.document(revisions=[self._rev(1, 999)]))

    def test_reingest_after_reopen_is_still_idempotent(self):
        doc = self.document(
            revisions=[self._rev(1, 100)],
            reports=[self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}])],
        )
        self.ingest(doc)
        self.reopen()
        again = self.ingest(doc)
        self.assertEqual(len(again["results"][0]["applied"]), 0)
        self.assertEqual(self.queries.summary("P")["completed"], 40)

    def test_crash_before_commit_rolls_back(self):
        self.ingest(self.document(
            revisions=[self._rev(1, 100)],
            reports=[self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}])],
        ))
        before = self.queries.summary("P")
        state_path = Path(self.root) / "state.json"
        snapshot = state_path.read_text()
        journal = Path(self.root) / "journal.json"
        # Simulate a writer that died after writing its journal but before the
        # atomic state swap.
        journal.write_text(json.dumps({
            "expected_generation": before["revision"]["seq"] and 2,
            "target_generation": 3,
            "intent": {"op": "revisions"},
        }))
        report = self.workspace.recover()
        self.assertEqual(report["action"], "rolled-back")
        self.assertFalse(journal.exists())
        self.assertEqual(self.queries.summary("P")["completed"], before["completed"])
        self.assertEqual(state_path.read_text(), snapshot)

    def test_crash_after_state_swap_rolls_forward(self):
        self.ingest(self.document(
            revisions=[self._rev(1, 100)],
            reports=[self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}])],
        ))
        generation = self.workspace.state["generation"]
        journal = Path(self.root) / "journal.json"
        journal.write_text(json.dumps({
            "expected_generation": generation - 1,
            "target_generation": generation,
            "intent": {"op": "revisions"},
        }))
        report = self.workspace.recover()
        self.assertEqual(report["action"], "rolled-forward")
        self.assertFalse(journal.exists())
        self.assertEqual(self.queries.summary("P")["completed"], 40)

    def test_corrupt_journal_is_discarded(self):
        journal = Path(self.root) / "journal.json"
        journal.write_text("{not json")
        report = self.workspace.recover()
        self.assertEqual(report["action"], "discarded-corrupt-journal")
        self.assertFalse(journal.exists())

    def test_failed_ingest_leaves_no_partial_state(self):
        self.ingest(self.document(revisions=[self._rev(1, 100)]))
        before = json.dumps(self.workspace.state, sort_keys=True)
        # Ambiguous split (parent still present) must fail atomically.
        with self.assertRaises(Exception):
            self.ingest(self.document(revisions=[self.rev(
                2,
                [{"guid": "W1", "planned": 100},
                 {"guid": "X", "planned": 50},
                 {"guid": "Y", "planned": 50}],
                splits=[{"parent": "X", "children": ["Y", "W1"]}],
            )]))
        self.assertEqual(json.dumps(self.workspace.state, sort_keys=True), before)
        self.assertFalse((Path(self.root) / "journal.json").exists())
        # A subsequent valid revision still applies cleanly.
        self.ingest(self.document(revisions=[self.rev(2, [
            {"guid": "W1", "planned": 100},
            {"guid": "W2", "planned": 50},
        ])]))
        self.assertEqual(self.queries.summary("P")["revision"]["seq"], 2)

    def test_snapshot_boundary_reconstructs_history(self):
        # Force the snapshot interval so replay crosses the boundary.
        self.make_ingester(snapshot_interval=1)
        revisions = [
            self.rev(i, [{"guid": f"W{i}", "planned": 10 * i, "activity": "A1"}],
                     mode="delta" if i > 1 else "full")
            for i in range(1, 8)
        ]
        self.ingest(self.document(revisions=revisions))
        pstate = self.workspace.state["projects"]["P"]
        self.assertGreaterEqual(len(pstate["snapshots"]), 6)
        for seq in range(1, 8):
            table = load_table_at(self.workspace, pstate, seq)
            self.assertEqual(table.seq, seq)
            present = [g for g, e in table.elements.items() if e.present]
            self.assertEqual(len(present), seq)
        self.assertEqual(
            self.queries.summary("P", 4)["planned"], sum(10 * i for i in range(1, 5))
        )

    def test_historical_model_is_immune_to_later_revisions(self):
        self.ingest(self.document(
            revisions=[self._rev(1, 100)],
            reports=[self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}])],
        ))
        baseline = self.queries.summary("P", 1, "2026-02-02")
        self.assertEqual(baseline["planned"], 100)
        self.assertEqual(baseline["completed"], 40)
        self.ingest(self.document(
            revisions=[self.rev(2, [
                {"guid": "W1", "planned": 500, "activity": "A1"},
                {"guid": "W9", "planned": 900, "activity": "A1"},
            ])],
        ))
        after = self.queries.summary("P", 1, "2026-02-02")
        # The later model cannot change the element set, quantities or
        # progress of an already-resolved historical revision.
        self.assertEqual(after["planned"], baseline["planned"])
        self.assertEqual(after["completed"], baseline["completed"])
        self.assertEqual(after["elements"], baseline["elements"])
        self.assertEqual(after["counts"], baseline["counts"])
        self.assertEqual(after["revision"]["revision_hash"],
                         baseline["revision"]["revision_hash"])

    def test_late_report_revises_only_its_date_range(self):
        self.ingest(self.document(
            revisions=[self._rev(1, 100)],
            reports=[
                self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}]),
                self.report("D9", "2026-02-10", [{"guid": "W1", "completed": 90}]),
            ],
        ))
        # A correction for 2026-02-05 arrives last; it is the truth for that
        # date only and leaves the surrounding days untouched.
        self.ingest(self.document(
            reports=[self.report("D5", "2026-02-05", [{"guid": "W1", "completed": 60}])],
        ))
        self.assertEqual(self.queries.element("P", "W1", 1, "2026-02-02")["completed"], 40)
        self.assertEqual(self.queries.element("P", "W1", 1, "2026-02-05")["completed"], 60)
        self.assertEqual(self.queries.element("P", "W1", 1, "2026-02-10")["completed"], 90)


if __name__ == "__main__":
    unittest.main()
