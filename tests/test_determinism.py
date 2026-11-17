import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from bim_progress_ingest.cli import main
from bim_progress_ingest.engine import calculate
from bim_progress_ingest.parser import load_records
from bim_progress_ingest.pipeline import run

FIXTURES = Path(__file__).parents[1] / "fixtures"


class TestDeterminismAndEdges(unittest.TestCase):
    def test_output_is_byte_stable_across_repeated_runs(self):
        payload = json.loads((FIXTURES / "input.json").read_text())
        first = json.dumps(run(payload), ensure_ascii=False, indent=2, sort_keys=True)
        second = json.dumps(run(payload), ensure_ascii=False, indent=2, sort_keys=True)
        self.assertEqual(first, second)

    def test_duplicate_update_rows_do_not_double_count(self):
        payload = json.loads((FIXTURES / "input.json").read_text())
        payload["updates"] = payload["updates"] + payload["updates"]
        rows = load_records(payload)
        data = calculate(rows, payload)
        self.assertEqual(data["actual"], 80.0)
        self.assertEqual(data["progress_pct"], 53.33)

    def test_empty_input_is_zero_state(self):
        result = run({"components": [], "updates": []})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["component_count"], 0)
        self.assertEqual(result["actual"], 0)
        self.assertEqual(result["progress_pct"], 0)
        self.assertEqual(result["missing_updates"], [])

    def test_missing_updates_are_sorted_and_reported(self):
        payload = {
            "components": [
                {"guid": "Z9", "planned": 10},
                {"guid": "A1", "planned": 10},
            ],
            "updates": [],
        }
        data = calculate(load_records(payload), payload)
        self.assertEqual(data["missing_updates"], ["A1", "Z9"])

    def test_cli_missing_file_returns_structured_error(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([str(FIXTURES / "nope.json")])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"], "input_not_found")
        self.assertEqual(stdout.getvalue(), "")

    def test_cli_corrupt_file_returns_structured_error(self):
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = main([str(FIXTURES / "corrupt" / "truncated.json")])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["status"], "error")

    def test_cli_revision_flag_runs_sample(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(["--revision", "rev-2026-11"])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["component_count"], 3)
        # planned 350; min(planned, actual): 100 + 10 + 80 = 190
        self.assertEqual(result["actual"], 190.0)
        self.assertEqual(result["progress_pct"], 54.29)
        self.assertEqual(result["missing_updates"], [])


if __name__ == "__main__":
    unittest.main()
