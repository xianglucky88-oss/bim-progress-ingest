import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bim_progress_ingest.revisions import (
    RevisionError,
    list_revisions,
    load_all_revisions,
    load_revision,
    revisions_root,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


class TestRevisions(unittest.TestCase):
    def test_catalog_lists_packaged_revisions(self):
        ids = list_revisions()
        self.assertEqual(ids, ["rev-2026-10", "rev-2026-11"])

    def test_pinned_hashes_match_files(self):
        for revision in load_all_revisions():
            self.assertEqual(len(revision.input_sha256), 64)
            payload = revision.load_input()
            self.assertIn("components", payload)

    def test_unknown_revision_is_isolated_error(self):
        with self.assertRaises(RevisionError):
            load_revision("does-not-exist")

    def test_tampered_input_fails_hash_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "revisions"
            shutil.copytree(revisions_root(), catalog)
            sample = catalog / "rev-2026-10"
            input_path = sample / "input.json"
            input_path.write_text(input_path.read_text(encoding="utf-8").replace("G1", "G9"))
            with self.assertRaises(RevisionError) as ctx:
                load_revision("rev-2026-10", root=catalog)
            self.assertIn("hash mismatch", str(ctx.exception))

    def test_metadata_id_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "revisions"
            shutil.copytree(revisions_root(), catalog)
            metadata_path = catalog / "rev-2026-10" / "revision.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["revision_id"] = "rev-wrong"
            metadata_path.write_text(json.dumps(metadata))
            with self.assertRaises(RevisionError):
                load_revision("rev-2026-10", root=catalog)

    def test_path_traversal_input_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "revisions"
            shutil.copytree(revisions_root(), catalog)
            metadata_path = catalog / "rev-2026-10" / "revision.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["input_file"] = "../escape.json"
            metadata_path.write_text(json.dumps(metadata))
            with self.assertRaises(RevisionError):
                load_revision("rev-2026-10", root=catalog)


if __name__ == "__main__":
    unittest.main()
