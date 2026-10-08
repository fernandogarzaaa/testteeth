"""The mutation-testing engine: select code, run the baseline, run mutants, grade."""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_EXCLUDE, Settings
from .diff import ALL_LINES, changed_lines
from .errors import BaselineFailed, EngineError
from .failure_paths import analyze_module
from .models import KILLED, NO_COVERAGE, SURVIVED, TIMEOUT, FunctionGrade, GradeReport, Mutant
from .operators import apply_mutant, generate_mutants, iter_functions

ProgressCallback = Callable[[int, int, Mutant], None]


__all__ = ["BaselineFailed", "EngineError", "grade", "select", "discover_sources", "discover_tests"]


# ---------------------------------------------------------------------------- selection
def is_test_file(rel: Path) -> bool:
    name = rel.name
    if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(part in {"tests", "test", "testing"} or part.startswith("tests_") for part in rel.parts[:-1])


def _excluded(rel: Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in patterns) or any(
        fnmatch.fnmatch(rel.as_posix(), pat) for pat in patterns
    )


def _walk_py(root: Path, start: Path, patterns: list[str]) -> list[Path]:
    if start.is_file():
        return [start] if start.suffix == ".py" else []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(start):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not _excluded((here / d).relative_to(root), patterns))
        found.extend(here / f for f in sorted(filenames) if f.endswith(".py"))
    return found


def discover_sources(settings: Settings) -> list[Path]:
    """Absolute paths of non-test Python files under the configured paths."""
    patterns = [*DEFAULT_EXCLUDE, *settings.exclude]
    files: list[Path] = []
    for raw in settings.paths:
        start = (settings.root / raw).resolve()
        if not start.exists():
            raise EngineError(f"path not found: {raw}")
        try:
            start.relative_to(settings.root)
        except ValueError as exc:
            raise EngineError(f"path {raw} is outside the project root {settings.root}") from exc
        for path in _walk_py(settings.root, start, patterns):
            rel = path.relative_to(settings.root)
            if not is_test_file(rel) and not _excluded(rel, patterns) and path not in files:
                files.append(path)
    return files


def resolve_test_paths(root: Path, test_args: list[str]) -> list[Path]:
    """Test paths named in the pytest arguments (e.g. ``tests/unit``), or ``[root]`` when none are."""
    named: list[Path] = []
    for arg in test_args:
        if arg.startswith("-"):
            continue
        candidate = (root / arg.split("::", 1)[0]).resolve()
        if candidate.exists():
            named.append(candidate)
    return named or [root]


def discover_tests(root: Path, exclude: list[str], test_args: list[str] | None = None) -> list[str]:
    """Source text of the test files the configured test run would use."""
    patterns = [*DEFAULT_EXCLUDE, *exclude]
    sources: list[str] = []
    seen: set[Path] = set()
    for start in resolve_test_paths(root, test_args or []):
        for path in _walk_py(root, start, patterns):
            if path in seen:
                continue
            seen.add(path)
            rel = path.relative_to(root)
            if path.name != "conftest.py" and is_test_file(rel):
                try:
                    sources.append(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    continue
    return sources


@dataclass
class Selection:
    """What will be mutated: per file, its source and the selected function names."""

    sources: dict[str, str] = field(default_factory=dict)  # rel posix path -> source
    functions: dict[str, set[str]] = field(default_factory=dict)  # rel -> selected qualnames
    module_lines: dict[str, frozenset[int] | None] = field(default_factory=dict)  # None = all module-level code
    mutants: list[Mutant] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def select(settings: Settings) -> Selection:
    """Pick files, functions and mutants according to the paths and the optional base ref."""
    selection = Selection()
    files = discover_sources(settings)
    changed: dict[Path, frozenset[int]] | None = None
    if settings.base_ref:
        changed = changed_lines(settings.root, settings.base_ref)
    ops = frozenset(settings.operators) if settings.operators else None
    for path in files:
        rel = path.relative_to(settings.root).as_posix()
        if changed is not None and path.resolve() not in changed:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError) as exc:
            selection.notes.append(f"skipped {rel}: {exc.__class__.__name__}")
            continue
        lines = changed.get(path.resolve()) if changed is not None else None
        everything = lines is None or lines == ALL_LINES
        funcs: set[str] = set()
        for qualname, node in iter_functions(tree):
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno or node.lineno
            if everything or (lines and any(start <= n <= end for n in lines)):
                funcs.add(qualname)
        module_lines: frozenset[int] | None = None if everything else (lines or frozenset())
        mutants = [
            m
            for m in generate_mutants(source, rel, ops)
            if m.function in funcs or (m.function == "<module>" and (module_lines is None or m.line in module_lines))
        ]
        if not funcs and not mutants:
            continue
        selection.sources[rel] = source
        selection.functions[rel] = funcs
        selection.module_lines[rel] = module_lines
        selection.mutants.extend(mutants)
    if settings.max_mutants and len(selection.mutants) > settings.max_mutants:
        total = len(selection.mutants)
        step = total / settings.max_mutants
        selection.mutants = [selection.mutants[int(i * step)] for i in range(settings.max_mutants)]
        selection.notes.append(f"sampled {settings.max_mutants} of {total} mutants (--max-mutants)")
    return selection


# ---------------------------------------------------------------------------- execution
_COPY_IGNORE = shutil.ignore_patterns(*DEFAULT_EXCLUDE, "*.pyc")


def _make_workspace(root: Path, parent: Path, index: int) -> Path:
    target = parent / f"w{index}"
    shutil.copytree(root, target, ignore=_COPY_IGNORE, symlinks=True)
    return target


def _env(workspace: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(workspace / "src"), str(workspace)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    # Never reuse bytecode: a mutant can have the same size and mtime as the original file.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TESTTEETH_ACTIVE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    if extra:
        env.update(extra)
    return env


@dataclass
class SuiteRun:
    returncode: int | None  # None = timed out
    seconds: float
    output: str


def run_tests(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float | None) -> SuiteRun:
    """Run the test command; on timeout kill the whole process group."""
    start = time.perf_counter()
    kwargs: dict[str, object] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(  # noqa: S603 - command is built from sys.executable + user test args
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs  # type: ignore[call-overload]
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
        else:  # pragma: no cover
            proc.kill()
        out, _ = proc.communicate()
        return SuiteRun(None, time.perf_counter() - start, out or "")
    return SuiteRun(proc.returncode, time.perf_counter() - start, out or "")


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def baseline(settings: Settings, workspace: Path, sources: list[str]) -> tuple[SuiteRun, dict[str, set[int]] | None]:
    """Run the untouched suite once (with the line tracer) and return its run and coverage."""
    cmd = settings.test_command
    extra: dict[str, str] = {}
    trace_out = workspace.parent / "trace.json"
    if settings.coverage:
        cmd = [*cmd[:3], "-p", "testteeth._tracer", *cmd[3:]]
        extra = {
            "TESTTEETH_TRACE_FILES": os.pathsep.join(str(workspace / rel) for rel in sources),
            "TESTTEETH_TRACE_OUT": str(trace_out),
        }
    run = run_tests(cmd, workspace, _env(workspace, extra), timeout=None)
    if run.returncode == 5:
        raise BaselineFailed("the test command collected no tests", run.output)
    if run.returncode != 0:
        raise BaselineFailed(
            f"the test suite fails before any mutation (exit code {run.returncode}); fix it first", run.output
        )
    coverage: dict[str, set[int]] | None = None
    if settings.coverage and trace_out.is_file():
        raw = json.loads(trace_out.read_text(encoding="utf-8"))
        coverage = {rel: set() for rel in sources}
        real = {os.path.realpath(workspace / rel): rel for rel in sources}
        for filename, lines in raw.items():
            rel = real.get(os.path.realpath(filename))
            if rel is not None:
                coverage[rel].update(lines)
    return run, coverage


def grade(settings: Settings, progress: ProgressCallback | None = None) -> GradeReport:
    """Grade the test suite against mutants of the selected code."""
    started = time.perf_counter()
    selection = select(settings)
    report = GradeReport(
        root=str(settings.root),
        base_ref=settings.base_ref,
        test_command=settings.test_command,
        fail_under=settings.fail_under,
        notes=list(selection.notes),
        lang="python",
        engine="testteeth",
    )
    if not selection.sources:
        report.notes.append(
            "no changed Python functions to grade" if settings.base_ref else "no Python source files found"
        )
        report.elapsed_seconds = time.perf_counter() - started
        return report

    with tempfile.TemporaryDirectory(prefix="testteeth-") as tmp:
        parent = Path(tmp)
        workspaces = [_make_workspace(settings.root, parent, 0)]
        base_run, coverage = baseline(settings, workspaces[0], list(selection.sources))
        report.baseline_seconds = base_run.seconds
        timeout = max(settings.min_timeout, base_run.seconds * settings.timeout_factor + 2.0)

        to_run: list[Mutant] = []
        for mutant in selection.mutants:
            if coverage is not None and mutant.line not in coverage.get(mutant.file, set()):
                mutant.status = NO_COVERAGE
                mutant.detail = "no test executes this line"
            else:
                to_run.append(mutant)

        workers = min(settings.workers, max(1, len(to_run)))
        for index in range(1, workers):
            workspaces.append(_make_workspace(settings.root, parent, index))
        pool: queue.Queue[Path] = queue.Queue()
        for workspace in workspaces:
            pool.put(workspace)
        done = 0
        total = len(selection.mutants)
        for mutant in selection.mutants:
            if mutant.status == NO_COVERAGE:
                done += 1
                if progress:
                    progress(done, total, mutant)

        def execute(mutant: Mutant) -> Mutant:
            workspace = pool.get()
            target = workspace / mutant.file
            original = selection.sources[mutant.file]
            try:
                target.write_text(apply_mutant(original, mutant), encoding="utf-8")
                run = run_tests(settings.test_command, workspace, _env(workspace), timeout)
            finally:
                target.write_text(original, encoding="utf-8")
                pool.put(workspace)
            mutant.duration = round(run.seconds, 3)
            if run.returncode is None:
                mutant.status, mutant.detail = TIMEOUT, f"suite exceeded {timeout:.0f}s"
            elif run.returncode == 0:
                mutant.status, mutant.detail = SURVIVED, "all tests passed"
            else:
                mutant.status = KILLED
                mutant.detail = _first_failure(run.output)
            return mutant

        with ThreadPoolExecutor(max_workers=workers) as executor:
            for mutant in executor.map(execute, to_run):
                done += 1
                if progress:
                    progress(done, total, mutant)

    report.mutants = selection.mutants
    test_sources = discover_tests(settings.root, settings.exclude, settings.test_args)
    for rel, source in selection.sources.items():
        covered = coverage.get(rel) if coverage is not None else None
        report.gaps.extend(analyze_module(source, rel, covered, test_sources, selection.functions[rel]))
    report.functions = _group(selection, report)
    report.elapsed_seconds = time.perf_counter() - started
    return report


def _first_failure(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAILED ", "ERROR ")):
            return stripped[:200]
    return _tail(output, 1)[:200]


def _group(selection: Selection, report: GradeReport) -> list[FunctionGrade]:
    grades: dict[tuple[str, str], FunctionGrade] = {}
    for rel, source in selection.sources.items():
        tree = ast.parse(source)
        for qualname, node in iter_functions(tree):
            if qualname in selection.functions[rel]:
                grades[(rel, qualname)] = FunctionGrade(rel, qualname, node.lineno)
    for mutant in report.mutants:
        key = (mutant.file, mutant.function)
        if key not in grades:
            grades[key] = FunctionGrade(mutant.file, mutant.function, mutant.function_line)
        grades[key].mutants.append(mutant)
    for gap in report.gaps:
        key = (gap.file, gap.function)
        if key not in grades:  # pragma: no cover - gaps are only computed for selected functions
            grades[key] = FunctionGrade(gap.file, gap.function, gap.function_line)
        grades[key].gaps.append(gap)
    return sorted(
        (g for g in grades.values() if g.mutants or g.gaps), key=lambda g: (g.file, g.line, g.function)
    )
