# BIM Progress Ingest

Link BIM components to schedule activities and calculate revision-aware progress.

## Modules

- `src/bim_progress_ingest/models.py`: immutable result contract.
- `src/bim_progress_ingest/parser.py`: input validation and record extraction.
- `src/bim_progress_ingest/engine.py`: domain calculation only.
- `src/bim_progress_ingest/report.py`: stable output envelope.
- `src/bim_progress_ingest/pipeline.py`: orchestration boundary.
- `src/bim_progress_ingest/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m bim_progress_ingest.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.
