"""Deterministic JSON CLI.

The original invocation (``python -m bim_progress_ingest.cli [input]``) still
prints the legacy summary.  New subcommands operate on a workspace::

    cli.py ingest   --workspace WS  input.json
    cli.py summary  --workspace WS  --project P [--revision R] [--as-of D]
    cli.py element  --workspace WS  --project P GUID [--revision R] [--as-of D]
    cli.py activity --workspace WS  --project P ACTIVITY [--revision R] [--as-of D]
    cli.py revisions --workspace WS --project P
    cli.py reconcile --workspace WS --project P
    cli.py recover  --workspace WS
    cli.py gc       --workspace WS
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .pipeline import run
from .storage import Workspace

SUBCOMMANDS = {
    "ingest",
    "summary",
    "element",
    "activity",
    "revisions",
    "reports",
    "reconcile",
    "recover",
    "gc",
}


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Link BIM components to schedule activities and calculate revision-aware progress."
    )
    sub = parser.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--workspace", required=True)
        p.add_argument("--project")

    p_ingest = sub.add_parser("ingest", help="ingest a revision/report/plan document")
    p_ingest.add_argument("document")
    p_ingest.add_argument("--workspace", required=True)
    p_ingest.add_argument("--project")

    for name in ("summary", "element", "activity"):
        p = sub.add_parser(name)
        common(p)
        if name in {"element", "activity"}:
            p.add_argument("key")
        p.add_argument("--revision")
        p.add_argument("--as-of")

    for name in ("revisions", "reports", "reconcile"):
        common(sub.add_parser(name))

    p_recover = sub.add_parser("recover")
    p_recover.add_argument("--workspace", required=True)

    p_gc = sub.add_parser("gc")
    p_gc.add_argument("--workspace", required=True)

    return parser


def _build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Link BIM components to schedule activities and calculate revision-aware progress."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=str(Path(__file__).parents[2] / "fixtures" / "input.json"),
        help="legacy single-document input (no subcommand)",
    )
    return parser


def _project(args: argparse.Namespace, service) -> str:
    if args.project:
        return args.project
    projects = service.projects()
    if len(projects) == 1:
        return projects[0]
    raise SystemExit("--project is required when the workspace has several projects")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        args = _build_parser().parse_args(argv)
    else:
        args = _build_legacy_parser().parse_args(argv)

    command = getattr(args, "command", None)
    if command is None:
        payload = json.loads(Path(args.input).read_text())
        _emit(run(payload))
        return

    from .ingest import Ingester
    from .queries import QueryService

    workspace = Workspace(args.workspace)
    if command == "recover":
        _emit(workspace.recover())
        return
    if command == "gc":
        _emit(workspace.garbage_collect())
        return
    if command == "ingest":
        ingester = Ingester(workspace)
        _emit(ingester.ingest_path(args.document))
        return

    service = QueryService(workspace)
    project = _project(args, service)
    revision = getattr(args, "revision", None)
    as_of = getattr(args, "as_of", None)
    if command == "summary":
        _emit(service.summary(project, revision, as_of))
    elif command == "element":
        _emit(service.element(project, args.key, revision, as_of))
    elif command == "activity":
        _emit(service.activity(project, args.key, revision, as_of))
    elif command == "revisions":
        _emit(service.revisions(project))
    elif command == "reports":
        _emit(service.reports(project))
    elif command == "reconcile":
        _emit(service.reconcile(project))
    else:  # pragma: no cover - argparse guards this
        raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    main()
