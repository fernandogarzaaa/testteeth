"""Command line interface: ``testteeth run``, ``testteeth suggest`` and ``testteeth mcp``."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import ConfigError, Settings, load_settings
from .diff import GitError
from .engine import BaselineFailed, EngineError, grade
from .models import GradeReport, Mutant
from .report import render_json, render_text
from .suggest import build_briefs, gap_brief, render_markdown

EXIT_OK = 0
EXIT_GATE = 1
EXIT_USAGE = 2
EXIT_ERROR = 3


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="*", help="files/directories to mutate (default: [tool.testteeth] paths or .)")
    parser.add_argument("--root", default=".", help="project root containing the tests (default: .)")
    parser.add_argument("--base", "--base-ref", dest="base_ref", help="only grade functions changed vs this git ref")
    parser.add_argument(
        "--pytest-args", help="extra arguments for the test run, e.g. \"tests/unit -m 'not slow'\""
    )
    parser.add_argument("--workers", type=int, help="parallel test workers (default: min(4, cpus))")
    parser.add_argument("--max-mutants", type=int, help="cap the number of mutants (evenly sampled)")
    parser.add_argument("--operators", help="comma-separated subset of mutation operators")
    parser.add_argument("--timeout-factor", type=float, help="mutant timeout = baseline time x factor (default 3)")
    parser.add_argument("--no-coverage", action="store_true", help="do not trace the baseline run")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testteeth",
        description="Grade (AI-written) tests with mutation testing and get briefs for the tests they are missing.",
    )
    parser.add_argument("--version", action="version", version=f"testteeth {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="mutation-test the selected code and report the score")
    _common(run)
    run.add_argument("--fail-under", type=float, help="exit 1 if the mutation score is below this percentage")
    run.add_argument("--json", dest="json_out", help="also write the JSON report to this file ('-' = stdout only)")

    suggest = sub.add_parser("suggest", help="print an agent-ready brief for each surviving mutant")
    _common(suggest)
    suggest.add_argument("--format", choices=("markdown", "json"), default="markdown")
    suggest.add_argument("--limit", type=int, help="show at most this many briefs")

    sub.add_parser("mcp", help="run the MCP server on stdio")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    return load_settings(
        args.root,
        paths=[str(Path(p).resolve()) for p in args.paths] or None,
        base_ref=args.base_ref,
        test_args=shlex.split(args.pytest_args) if args.pytest_args else None,
        fail_under=getattr(args, "fail_under", None),
        workers=args.workers,
        max_mutants=args.max_mutants,
        operators=[o.strip() for o in args.operators.split(",") if o.strip()] if args.operators else None,
        timeout_factor=args.timeout_factor,
        coverage=False if args.no_coverage else None,
    )


def _progress(quiet: bool):  # type: ignore[no-untyped-def]
    if quiet:
        return None

    def show(done: int, total: int, mutant: Mutant) -> None:
        mark = {"killed": ".", "timeout": "T", "survived": "S", "no_coverage": "-"}.get(mutant.status, "?")
        sys.stderr.write(mark)
        if done == total:
            sys.stderr.write(f" {total} mutants\n")
        sys.stderr.flush()

    return show


def _grade(args: argparse.Namespace) -> tuple[Settings, GradeReport]:
    settings = settings_from_args(args)
    return settings, grade(settings, progress=_progress(args.quiet))


def _sources(settings: Settings, report: GradeReport) -> dict[str, str]:
    files = {m.file for m in report.mutants}
    return {f: (settings.root / f).read_text(encoding="utf-8") for f in files}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return EXIT_OK
    try:
        settings, report = _grade(args)
    except ConfigError as exc:
        print(f"testteeth: configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BaselineFailed as exc:
        print(f"testteeth: {exc}", file=sys.stderr)
        if exc.output:
            print("\n".join(exc.output.strip().splitlines()[-25:]), file=sys.stderr)
        return EXIT_ERROR
    except (EngineError, GitError) as exc:
        print(f"testteeth: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.command == "run":
        if args.json_out == "-":
            print(render_json(report))
        else:
            print(render_text(report), end="")
            if args.json_out:
                Path(args.json_out).write_text(render_json(report), encoding="utf-8")
        return EXIT_OK if report.passed_gate else EXIT_GATE

    briefs = build_briefs(report, _sources(settings, report))
    if args.format == "json":
        payload = {
            "score": report.score,
            "briefs": [b.to_dict() for b in (briefs[: args.limit] if args.limit else briefs)],
            "failure_path_gaps": [gap_brief(g) for g in report.gaps],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_markdown(briefs, report.gaps, args.limit), end="")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
