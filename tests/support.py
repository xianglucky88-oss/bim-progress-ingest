"""Helpers shared by the revision-aware test suite."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Optional

from bim_progress_ingest.ingest import Ingester
from bim_progress_ingest.parser import parse_document_text
from bim_progress_ingest.queries import QueryService
from bim_progress_ingest.storage import Workspace


class WorkspaceCase(unittest.TestCase):
    """A test case with a fresh workspace and small document builders."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="bpi-test-"))
        self.root = self._tmp / "ws"
        self.workspace = Workspace(self.root)
        self.ingester = Ingester(self.workspace)
        self.queries = QueryService(self.workspace)

    def make_ingester(self, snapshot_interval: int) -> Ingester:
        self.ingester = Ingester(self.workspace, snapshot_interval=snapshot_interval)
        return self.ingester

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ----------------------------------------------------------------- docs

    def ingest(self, document: Mapping[str, Any]) -> dict[str, Any]:
        doc = parse_document_text(json.dumps(document), self._tmp)
        return self.ingester.ingest_document(doc)

    def reopen(self) -> None:
        self.workspace = Workspace(self.root)
        self.ingester = Ingester(self.workspace)
        self.queries = QueryService(self.workspace)

    # ------------------------------------------------------------- builders

    @staticmethod
    def rev(
        seq: int,
        components: list[dict[str, Any]],
        *,
        revision_id: Optional[str] = None,
        effective_at: Optional[str] = None,
        mode: str = "full",
        remap: Optional[list[dict[str, str]]] = None,
        splits: Optional[list[dict[str, Any]]] = None,
        merges: Optional[list[dict[str, Any]]] = None,
        remove: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        revision: dict[str, Any] = {
            "seq": seq,
            "revision_id": revision_id or f"R{seq}",
            "mode": mode,
            "components": components,
        }
        if effective_at:
            revision["effective_at"] = effective_at
        if remap:
            revision["guid_remap"] = remap
        if splits:
            revision["splits"] = splits
        if merges:
            revision["merges"] = merges
        if remove:
            revision["remove"] = remove
        return revision

    @staticmethod
    def report(
        report_id: str,
        effective_date: str,
        lines: list[dict[str, Any]],
        *,
        source: str = "daily",
    ) -> dict[str, Any]:
        return {
            "report_id": report_id,
            "effective_date": effective_date,
            "source": source,
            "lines": lines,
        }

    @staticmethod
    def document(
        revisions: Optional[list[dict[str, Any]]] = None,
        reports: Optional[list[dict[str, Any]]] = None,
        plans: Optional[list[dict[str, Any]]] = None,
        project: str = "P",
    ) -> dict[str, Any]:
        doc: dict[str, Any] = {"project": project}
        if revisions is not None:
            doc["revisions"] = revisions
        if reports is not None:
            doc["reports"] = reports
        if plans is not None:
            doc["plans"] = plans
        return doc
