"""End-to-end demonstration of revision-aware ingestion.

Run with::

    PYTHONPATH=src python -m bim_progress_ingest.pipeline

It builds a workspace in a temporary directory, ingests three model revisions
(including a GUID remap, a split, a merge and a deletion), two WBS plans and a
set of daily reports that arrive out of order, then prints as-of answers.
Everything is deterministic apart from ingest timestamps.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .parser import load_records
from .engine import calculate
from .report import build_result
from .storage import Workspace


def run(payload: dict) -> dict:
    """Legacy entry point: unchanged behaviour for the original fixture."""

    records = load_records(payload)
    return build_result('bim-progress-ingest', calculate(records, payload))


DEMO_DOCUMENT: dict[str, Any] = {
    "project": "tower-a",
    "plans": [
        {
            "plan_id": "WBS-1",
            "effective_date": "2026-01-01",
            "activities": [
                {"id": "A1", "name": "Structure", "wbs_code": "1.1"},
                {"id": "A2", "name": "Facade", "wbs_code": "1.2"},
            ],
        }
    ],
    "revisions": [
        {
            "seq": 1,
            "revision_id": "R1",
            "effective_at": "2026-01-02T00:00:00Z",
            "components": [
                {"guid": "W1", "planned": 100, "activity": "A1", "parent": "LEVEL-1"},
                {"guid": "W2", "planned": 100, "activity": "A1", "parent": "LEVEL-1"},
                {"guid": "C1", "planned": 50, "activity": "A2"},
                {"guid": "D1", "planned": 10, "activity": "A1"},
            ],
        },
        {
            "seq": 2,
            "revision_id": "R2",
            "effective_at": "2026-01-05T00:00:00Z",
            "guid_remap": [{"from": "W2", "to": "WALL-2"}],
            "splits": [
                {"parent": "W1", "children": ["W1A", "W1B"], "weights": {"W1A": 0.25, "W1B": 0.75}}
            ],
            "merges": [{"sources": ["C1"], "target": "W1B"}],
            "components": [
                {"guid": "WALL-2", "planned": 100, "activity": "A1", "parent": "LEVEL-1"},
                {"guid": "W1A", "planned": 25, "activity": "A1", "parent": "LEVEL-1"},
                {"guid": "W1B", "planned": 125, "activity": "A1", "parent": "LEVEL-1"},
                {"guid": "D1", "planned": 10, "activity": "A1"},
            ],
        },
        {
            "seq": 3,
            "revision_id": "R3",
            "effective_at": "2026-01-09T00:00:00Z",
            "components": [
                {"guid": "WALL-2", "planned": 100, "activity": "A1", "parent": "LEVEL-1"},
                {"guid": "W1A", "planned": 25, "activity": "A1", "parent": "LEVEL-1"},
                {"guid": "W1B", "planned": 125, "activity": "A1", "parent": "LEVEL-1"},
            ],
        },
    ],
    "reports": [
        {
            "report_id": "D-0103",
            "effective_date": "2026-01-03",
            "source": "site-daily",
            "lines": [
                {"guid": "W1", "fraction": 0.4},
                {"guid": "W2", "completed": 20},
                {"guid": "C1", "fraction": 0.5},
            ],
        },
        {
            "report_id": "D-0106",
            "effective_date": "2026-01-06",
            "source": "site-daily",
            "lines": [
                {"guid": "W1A", "fraction": 0.8},
                {"guid": "WALL-2", "completed": 50},
                {"guid": "W1B", "fraction": 0.2},
            ],
        },
        {
            "report_id": "D-0104",
            "effective_date": "2026-01-04",
            "source": "site-daily-late",
            "lines": [
                {"guid": "W1", "fraction": 0.5},
                {"guid": "W2", "completed": 30},
            ],
        },
    ],
}


def demo(root: str | Path) -> dict[str, Any]:
    from .ingest import Ingester
    from .queries import QueryService

    root = Path(root)
    workspace = Workspace(root)
    summary = Ingester(workspace).ingest_document(_demo_document())
    service = QueryService(workspace)
    answers = {
        "ingest": summary,
        "head": service.summary("tower-a"),
        "as_of_2026-01-03_r1": service.summary("tower-a", 1, "2026-01-03"),
        "as_of_2026-01-06_r2": service.summary("tower-a", 2, "2026-01-06"),
        "reconcile": service.reconcile("tower-a"),
    }
    return answers


def _demo_document():
    from .parser import parse_document_text

    text = json.dumps(DEMO_DOCUMENT)
    return parse_document_text(text, Path("."))


if __name__ == "__main__":  # pragma: no cover
    with tempfile.TemporaryDirectory() as tmp:
        print(json.dumps(demo(tmp), indent=2, sort_keys=True, default=str))
