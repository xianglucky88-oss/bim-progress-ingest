import json
import unittest
from pathlib import Path
from bim_progress_ingest.pipeline import run
from bim_progress_ingest.parser import load_records
from bim_progress_ingest.engine import calculate

def fixture():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "input.json").read_text())

class TestCore(unittest.TestCase):
    def test_pipeline_and_modules(self):
        payload = fixture()
        rows = load_records(payload)
        self.assertTrue(rows)
        data = calculate(rows, payload)
        self.assertIsInstance(data, dict)
        self.assertTrue(data)
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["project"], 'bim-progress-ingest')

        assert result["progress_pct"] == 53.33
