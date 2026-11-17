"""Daily report semantics: idempotency, late reports, revisions, provenance."""

from __future__ import annotations

import unittest

from support import WorkspaceCase


class TestReports(WorkspaceCase):
    def _base(self, reports):
        return self.document(
            revisions=[
                self.rev(1, [
                    {"guid": "W1", "planned": 100, "activity": "A1"},
                    {"guid": "W2", "planned": 100, "activity": "A2"},
                ]),
            ],
            reports=reports,
        )

    def test_duplicate_report_upload_is_idempotent(self):
        report = self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}])
        first = self.ingest(self._base([report]))
        self.assertEqual(first["results"][-1]["reports"]["D1"][0]["status"], "stored")
        second = self.ingest(self._base([report]))
        self.assertEqual(second["results"][-1]["reports"]["D1"][0]["status"], "duplicate")
        self.assertEqual(self.queries.summary("P")["completed"], 40)
        self.assertEqual(len(self.queries.reports("P")), 1)

    def test_duplicate_inside_one_document_is_idempotent(self):
        report = self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}])
        result = self.ingest(self._base([report, dict(report)]))
        statuses = [r["status"] for r in result["results"][-1]["reports"]["D1"]]
        self.assertEqual(statuses, ["stored", "duplicate"])
        self.assertEqual(self.queries.summary("P")["completed"], 40)

    def test_late_report_only_alters_its_effective_range(self):
        self.ingest(
            self._base([
                self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 20}]),
                self.report("D3", "2026-02-04", [{"guid": "W1", "completed": 60}]),
            ])
        )
        self.assertEqual(self.queries.element("P", "W1", None, "2026-02-04")["completed"], 60)
        # A late report for 2026-02-03 (uploaded last) revises only that day.
        self.ingest(
            self._base([
                self.report("D2", "2026-02-03", [{"guid": "W1", "completed": 30}]),
            ])
        )
        self.assertEqual(self.queries.element("P", "W1", None, "2026-02-02")["completed"], 20)
        self.assertEqual(self.queries.element("P", "W1", None, "2026-02-03")["completed"], 30)
        self.assertEqual(self.queries.element("P", "W1", None, "2026-02-04")["completed"], 60)

    def test_report_revised_content_creates_new_version(self):
        self.ingest(self._base([
            self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}]),
        ]))
        self.ingest(self._base([
            self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 55}]),
        ]))
        versions = self.queries.reports("P")
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(self.queries.element("P", "W1", None, "2026-02-02")["completed"], 55)

    def test_late_report_cannot_overwrite_newer_effective_date(self):
        self.ingest(self._base([
            self.report("NEW", "2026-02-10", [{"guid": "W1", "completed": 90}]),
        ]))
        # Arrives afterwards but is effective earlier.
        self.ingest(self._base([
            self.report("OLD", "2026-02-01", [{"guid": "W1", "completed": 10}]),
        ]))
        self.assertEqual(self.queries.element("P", "W1", None, "2026-02-10")["completed"], 90)
        self.assertEqual(self.queries.element("P", "W1", None, "2026-02-01")["completed"], 10)
        # Between the two dates the older report is the effective one.
        self.assertEqual(self.queries.element("P", "W1", None, "2026-02-05")["completed"], 10)

    def test_report_on_unknown_guid_is_surfaced_not_ignored(self):
        self.ingest(self._base([
            self.report("D1", "2026-02-02", [{"guid": "NOPE", "completed": 50}]),
        ]))
        summary = self.queries.summary("P")
        self.assertEqual(summary["completed"], 0)
        self.assertEqual(len(summary["unallocated"]), 1)
        self.assertEqual(summary["unallocated"][0]["reported_guid"], "NOPE")

    def test_fraction_and_percent_forms(self):
        self.ingest(self._base([
            self.report("D1", "2026-02-02", [
                {"guid": "W1", "fraction": 0.25},
                {"guid": "W2", "percent": 50},
            ]),
        ]))
        summary = self.queries.summary("P")
        self.assertEqual(summary["elements"]["W1"]["completed"], 25)
        self.assertEqual(summary["elements"]["W2"]["completed"], 50)

    def test_completion_is_clamped_to_planned(self):
        self.ingest(self._base([
            self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 250}]),
        ]))
        element = self.queries.element("P", "W1", None, "2026-02-02")
        self.assertEqual(element["completed"], 100)
        self.assertLessEqual(element["progress_pct"], 100)

    def test_provenance_points_back_to_revision_and_report(self):
        self.ingest(self._base([
            self.report("D1", "2026-02-02", [{"guid": "W1", "completed": 40}],
                        source="pdf-upload"),
        ]))
        element = self.queries.element("P", "W1", None, "2026-02-02")
        revision = element["revision"]
        self.assertEqual(revision["revision_id"], "R1")
        self.assertIn("revision_hash", revision)
        self.assertIn("input_blob", revision)
        from pathlib import Path
        self.assertTrue((Path(self.root) / revision["descriptor_path"]).exists())
        evidence = element["evidence"][0]
        self.assertEqual(evidence["report_id"], "D1")
        self.assertEqual(evidence["source"], "pdf-upload")
        self.assertEqual(evidence["content_hash"], self.queries.reports("P")[0]["content_hash"])

    def test_summary_and_element_evidence_are_consistent(self):
        self.ingest(self._base([
            self.report("D1", "2026-02-02", [
                {"guid": "W1", "completed": 40},
                {"guid": "W2", "completed": 20},
            ]),
        ]))
        summary = self.queries.summary("P", None, "2026-02-02")
        element = self.queries.element("P", "W1", None, "2026-02-02")
        entry = {k: v for k, v in element.items() if k in summary["elements"]["W1"]}
        self.assertEqual(summary["elements"]["W1"], entry)
        self.assertEqual(summary["revision"]["revision_hash"], element["revision"]["revision_hash"])


if __name__ == "__main__":
    unittest.main()
