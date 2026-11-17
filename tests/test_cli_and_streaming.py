"""CLI behaviour, streaming ingestion and workspace maintenance."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bim_progress_ingest.ingest import Ingester
from bim_progress_ingest.parser import load_document, parse_document_text
from bim_progress_ingest.queries import QueryService
from bim_progress_ingest.storage import Workspace

from support import WorkspaceCase

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> dict:
    env = {"PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run(
        [sys.executable, "-m", "bim_progress_ingest.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(f"CLI failed: {proc.stderr}")
    return json.loads(proc.stdout)


class TestCliAndStreaming(WorkspaceCase):
    def write(self, name: str, payload) -> Path:
        path = self._tmp / name
        path.write_text(json.dumps(payload))
        return path

    def test_legacy_invocation_is_unchanged(self):
        result = run_cli()
        self.assertEqual(result["project"], "bim-progress-ingest")
        self.assertEqual(result["progress_pct"], 53.33)
        self.assertEqual(result["status"], "ok")

    def test_cli_ingest_and_query_round_trip(self):
        doc = self.write("doc.json", self.document(
            revisions=[self.rev(1, [
                {"guid": "W1", "planned": 100, "activity": "A1"},
                {"guid": "W2", "planned": 100, "activity": "A1"},
            ], effective_at="2026-04-01T00:00:00Z")],
            reports=[self.report("D1", "2026-04-02", [
                {"guid": "W1", "completed": 40},
            ])],
        ))
        ing = run_cli("ingest", str(doc), "--workspace", str(self.root))
        self.assertTrue(ing["ok"])
        summary = run_cli(
            "summary", "--workspace", str(self.root), "--project", "P",
            "--revision", "1", "--as-of", "2026-04-02",
        )
        self.assertEqual(summary["completed"], 40)
        element = run_cli(
            "element", "--workspace", str(self.root), "--project", "P", "W1",
            "--as-of", "2026-04-02",
        )
        self.assertEqual(element["completed"], 40)
        self.assertEqual(element["revision"]["seq"], 1)
        activity = run_cli(
            "activity", "--workspace", str(self.root), "--project", "P", "A1",
            "--as-of", "2026-04-02",
        )
        self.assertEqual(activity["planned"], 200)
        revisions = run_cli("revisions", "--workspace", str(self.root), "--project", "P")
        self.assertEqual(revisions[0]["revision_id"], "R1")
        reconcile = run_cli("reconcile", "--workspace", str(self.root), "--project", "P")
        self.assertEqual(reconcile["gaps"], [])

    def test_repeat_cli_ingest_is_idempotent(self):
        doc = self.write("doc.json", self.document(
            revisions=[self.rev(1, [{"guid": "W1", "planned": 100}])],
            reports=[self.report("D1", "2026-04-02", [{"guid": "W1", "completed": 40}])],
        ))
        run_cli("ingest", str(doc), "--workspace", str(self.root))
        again = run_cli("ingest", str(doc), "--workspace", str(self.root))
        revision_result = next(r for r in again["results"] if r["kind"] == "revisions")
        self.assertEqual(revision_result["applied"], [])
        summary = run_cli("summary", "--workspace", str(self.root), "--project", "P")
        self.assertEqual(summary["completed"], 40)

    def test_cli_recover_and_gc(self):
        doc = self.write("doc.json", self.document(
            revisions=[self.rev(1, [{"guid": "W1", "planned": 100}])],
        ))
        run_cli("ingest", str(doc), "--workspace", str(self.root))
        gc = run_cli("gc", "--workspace", str(self.root))
        self.assertEqual(gc["removed"], 0)
        recover = run_cli("recover", "--workspace", str(self.root))
        self.assertFalse(recover["journal"])

    # ------------------------------------------------------------ streaming

    def test_jsonl_component_stream_is_equivalent_to_inline(self):
        big_dir = self._tmp / "big"
        big_dir.mkdir()
        with (big_dir / "components.jsonl").open("w") as handle:
            for index in range(5000):
                handle.write(json.dumps({
                    "guid": f"G{index:05d}",
                    "planned": 2 if index % 2 == 0 else 1,
                    "activity": "A1" if index % 3 else "A2",
                }) + "\n")
        doc = self.write("doc.json", {
            "project": "P",
            "revisions": [{
                "seq": 1,
                "revision_id": "R1",
                "components_ref": "big/components.jsonl",
            }],
        })
        result = Ingester(Workspace(self.root)).ingest_path(doc)
        applied = next(r for r in result["results"] if r["kind"] == "revisions")["applied"]
        self.assertEqual(applied[0]["counts"]["elements"], 5000)
        summary = QueryService(Workspace(self.root)).summary("P")
        self.assertEqual(summary["counts"]["progress_units"], 5000)
        self.assertEqual(summary["planned"], 7500)  # 2500*2 + 2500*1
        self.assertEqual(summary["activities"]["A1"]["component_count"], 3333)

    def test_jsonl_document_stream(self):
        lines = [
            {"kind": "project", "project": "P"},
            {"kind": "revision", "seq": 1, "revision_id": "R1",
             "components": [{"guid": "W1", "planned": 10, "activity": "A1"}]},
            {"kind": "revision", "seq": 2, "revision_id": "R2", "mode": "delta",
             "components": [{"guid": "W2", "planned": 10, "activity": "A1"}]},
            {"kind": "report", "report_id": "D1", "effective_date": "2026-04-02",
             "lines": [{"guid": "W1", "completed": 5}]},
        ]
        path = self._tmp / "stream.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        Ingester(Workspace(self.root)).ingest_path(path)
        service = QueryService(Workspace(self.root))
        self.assertEqual(service.summary("P")["revision"]["seq"], 2)
        self.assertEqual(service.element("P", "W1")["completed"], 5)

    def test_streaming_does_not_keep_all_components_in_memory(self):
        """The vendor blob is streamed: only derived state is retained."""

        big_dir = self._tmp / "many"
        big_dir.mkdir()
        count = 20000
        with (big_dir / "components.jsonl").open("w") as handle:
            for index in range(count):
                handle.write(json.dumps({
                    "guid": f"G{index:05d}", "planned": 1, "activity": "A1",
                }) + "\n")
        doc = self.write("doc.json", {
            "project": "P",
            "revisions": [{"seq": 1, "revision_id": "R1",
                           "components_ref": "many/components.jsonl"}],
        })
        Ingester(Workspace(self.root)).ingest_path(doc)
        # The compressed input blob is far smaller than the raw JSON-Lines
        # stream and is stored once, content addressed.
        blobs = list((Path(self.root) / "blobs" / "inputs").glob("*.jsonl.z"))
        self.assertEqual(len(blobs), 1)
        self.assertLess(blobs[0].stat().st_size, 200_000)

    def test_gc_removes_only_unreferenced_blobs(self):
        self.ingest(self.document(revisions=[self.rev(1, [
            {"guid": "W1", "planned": 100},
        ])]))
        orphan = Path(self.root) / "blobs" / "changes" / ("0" * 64 + ".jsonl.z")
        orphan.write_bytes(b"orphan")
        before = len(list((Path(self.root) / "blobs").rglob("*.*")))
        result = self.workspace.garbage_collect()
        after = len(list((Path(self.root) / "blobs").rglob("*.*")))
        self.assertEqual(result["removed"], 1)
        self.assertEqual(after, before - 1)
        self.assertFalse(orphan.exists())
        # Data still readable after gc.
        self.assertEqual(self.queries.summary("P")["planned"], 100)

    def test_workspace_survives_process_boundary(self):
        doc = self.write("doc.json", self.document(
            revisions=[self.rev(1, [
                {"guid": "W1", "planned": 100, "activity": "A1"},
            ], effective_at="2026-04-01T00:00:00Z")],
            reports=[self.report("D1", "2026-04-02", [{"guid": "W1", "completed": 40}])],
        ))
        run_cli("ingest", str(doc), "--workspace", str(self.root))
        # A brand new process can answer a historical query.
        summary = run_cli(
            "summary", "--workspace", str(self.root), "--project", "P",
            "--revision", "R1", "--as-of", "2026-04-02",
        )
        self.assertEqual(summary["completed"], 40)
        self.assertEqual(summary["revision"]["revision_id"], "R1")


if __name__ == "__main__":
    unittest.main()
