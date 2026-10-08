"""MCP server (stdio) exposing testteeth to coding agents such as Claude Code and Cursor.

Tools:

* ``grade_tests`` - run mutation testing and return the score per function plus failure-path gaps.
* ``list_surviving_mutants`` - the mutants the tests did not catch (from the last grade, or a fresh run).
* ``suggest_missing_tests`` - one precise brief per surviving mutant that an agent can turn into a test.

The tool functions are plain Python and are unit-tested directly; :func:`build_server` registers
them with the MCP Python SDK (v1 ``FastMCP`` or v2 ``MCPServer``).
"""

from __future__ import annotations

import shlex
import threading
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings, load_settings
from .diff import GitError
from .errors import BaselineFailed, EngineCrashed, EngineError, EngineNotInstalled
from .languages import grade_project as grade
from .models import GradeReport
from .suggest import build_briefs, gap_brief, render_markdown

INSTRUCTIONS = (
    "testteeth grades a project's tests with mutation testing (Python built in; TypeScript/JavaScript via StrykerJS; "
    "Rust via cargo-mutants; the language is auto-detected, override with lang). "
    "Call grade_tests(path, base_ref) after writing "
    "or changing code and tests; then call suggest_missing_tests and write one new test per brief. Tests must "
    "assert behaviour from the specification (boundaries, error paths, retries, idempotency), not mirror the "
    "implementation. Re-run grade_tests to confirm the mutants are now killed."
)

_cache: dict[tuple[Any, ...], tuple[Settings, GradeReport]] = {}
_lock = threading.Lock()


def _key(settings: Settings) -> tuple[Any, ...]:
    return (str(settings.root), settings.base_ref, tuple(settings.paths), tuple(settings.test_args), settings.lang,
            tuple(settings.engine_args))


def _settings(path: str, base_ref: str | None, pytest_args: str | None, paths: list[str] | None,
              max_mutants: int | None, lang: str | None = None, engine_args: str | None = None) -> Settings:
    root = Path(path).expanduser().resolve()
    return load_settings(
        root,
        base_ref=base_ref or None,
        test_args=shlex.split(pytest_args) if pytest_args else None,
        paths=paths or None,
        max_mutants=max_mutants,
        lang=None if not lang or lang == "auto" else lang,
        engine_args=shlex.split(engine_args) if engine_args else None,
    )


def _run(path: str, base_ref: str | None, pytest_args: str | None, paths: list[str] | None,
         max_mutants: int | None, refresh: bool, lang: str | None = None,
         engine_args: str | None = None) -> tuple[Settings, GradeReport]:
    settings = _settings(path, base_ref, pytest_args, paths, max_mutants, lang, engine_args)
    key = _key(settings)
    with _lock:
        if not refresh and key in _cache:
            return _cache[key]
    report = grade(settings)
    with _lock:
        _cache[key] = (settings, report)
    return settings, report


def _error(exc: Exception) -> dict[str, Any]:
    data: dict[str, Any] = {"error": f"{exc.__class__.__name__}: {exc}"}
    if isinstance(exc, BaselineFailed) and exc.output:
        data["test_output_tail"] = "\n".join(exc.output.strip().splitlines()[-30:])
        data["hint"] = "Make the existing test suite pass first; mutation results are meaningless otherwise."
    if isinstance(exc, EngineNotInstalled):
        data["install_hint"] = exc.hint
    if isinstance(exc, EngineCrashed) and exc.output:
        data["engine_output_tail"] = "\n".join(exc.output.strip().splitlines()[-30:])
    return data


def _summary(report: GradeReport) -> dict[str, Any]:
    data = report.to_dict()
    data.pop("mutants")
    for func in data["functions"]:
        func.pop("surviving_mutants")
    data["surviving_mutant_count"] = len(report.surviving())
    data["next_step"] = (
        "Call suggest_missing_tests for agent-ready briefs." if report.surviving() or report.gaps else
        "All mutants killed and no failure-path gaps."
    )
    return data


def grade_tests(path: str = ".", base_ref: str | None = None, pytest_args: str | None = None,
                paths: list[str] | None = None, max_mutants: int | None = None, lang: str | None = None,
                engine_args: str | None = None) -> dict[str, Any]:
    """Grade the tests of the project at `path` by mutation testing.

    Args:
        path: project root (directory containing the tests / pyproject.toml).
        base_ref: git ref (e.g. "main", "origin/main", "HEAD~1"); only functions changed since it are graded.
            Omit to grade all code.
        pytest_args: extra pytest arguments, e.g. "tests/unit -m 'not slow'".
        paths: source files/dirs (relative to path) to mutate; default is the whole project.
        max_mutants: cap on mutants (evenly sampled) for a faster run (Python only).
        lang: "python", "typescript"/"javascript" (StrykerJS) or "rust" (cargo-mutants); omit to auto-detect.
        engine_args: extra arguments for the native engine, e.g. "-- --test unit" for cargo-mutants.
    Returns the mutation score, per-function scores, counts and failure-path gaps.
    """
    try:
        _, report = _run(path, base_ref, pytest_args, paths, max_mutants, refresh=True, lang=lang,
                         engine_args=engine_args)
    except (ConfigError, EngineError, GitError) as exc:
        return _error(exc)
    return _summary(report)


def list_surviving_mutants(path: str = ".", base_ref: str | None = None, pytest_args: str | None = None,
                           paths: list[str] | None = None, max_mutants: int | None = None,
                           refresh: bool = False, lang: str | None = None,
                           engine_args: str | None = None) -> dict[str, Any]:
    """List mutants the tests failed to detect (reuses the last grade_tests run with the same arguments).

    Each entry has the file, function, line, operator, original and mutated code and whether the line was
    executed by any test at all (status "no_coverage") or executed but not asserted (status "survived").
    """
    try:
        _, report = _run(path, base_ref, pytest_args, paths, max_mutants, refresh, lang, engine_args)
    except (ConfigError, EngineError, GitError) as exc:
        return _error(exc)
    return {
        "lang": report.lang,
        "score": report.score,
        "count": len(report.surviving()),
        "mutants": [m.to_dict() for m in report.surviving()],
    }


def suggest_missing_tests(path: str = ".", base_ref: str | None = None, pytest_args: str | None = None,
                          paths: list[str] | None = None, max_mutants: int | None = None,
                          limit: int = 25, refresh: bool = False, lang: str | None = None,
                          engine_args: str | None = None) -> dict[str, Any]:
    """Return one agent-ready brief per surviving mutant, plus failure-path gaps.

    Each brief gives the mutated line, why the current tests miss it, and exactly which behaviour or failure
    path a new test must assert. Write each test from the spec so it fails on the mutated code and passes on
    the original, then call grade_tests again to confirm. Briefs for TypeScript and Rust include a test
    skeleton in that ecosystem (vitest/jest, #[test]/#[tokio::test]/mockall).
    """
    try:
        settings, report = _run(path, base_ref, pytest_args, paths, max_mutants, refresh, lang, engine_args)
    except (ConfigError, EngineError, GitError) as exc:
        return _error(exc)
    sources = {f: (settings.root / f).read_text(encoding="utf-8") for f in {m.file for m in report.mutants}}
    briefs = build_briefs(report, sources)
    shown = briefs[:limit] if limit else briefs
    return {
        "lang": report.lang,
        "score": report.score,
        "total_briefs": len(briefs),
        "briefs": [b.to_dict() for b in shown],
        "failure_path_gaps": [gap_brief(g) for g in report.gaps],
        "markdown": render_markdown(briefs, report.gaps, limit or None),
    }


TOOLS = (grade_tests, list_surviving_mutants, suggest_missing_tests)


def build_server() -> Any:
    """Create the MCP server object with all tools registered."""
    try:  # MCP Python SDK v2
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # MCP Python SDK v1
        try:
            from mcp.server.fastmcp import FastMCP as Server
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise SystemExit("The MCP server needs the MCP SDK: pip install 'testteeth[mcp]'") from exc
    server = Server(name="testteeth", instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.tool()(tool)
    return server


def main() -> None:
    build_server().run()  # stdio transport by default


if __name__ == "__main__":  # pragma: no cover
    main()
