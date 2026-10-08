"""TypeScript / JavaScript adapter: wraps StrykerJS.

testteeth generates a Stryker config that limits ``mutate`` to the selected files (with ``--base``: to the line
ranges of the changed functions), runs ``npx stryker run`` and reads the JSON report
(mutation-testing-report-schema). Failure-path gaps come from the TypeScript rule pack.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import DEFAULT_EXCLUDE, ConfigError
from ..core import line_coverage, read_text, walk_files
from ..diff import ALL_LINES, changed_lines
from ..errors import BaselineFailed, EngineCrashed, EngineError, EngineNotInstalled, ReportError
from ..models import KILLED, NO_COVERAGE, SURVIVED, TIMEOUT, GradeReport, Mutant
from ..rules import analyze, test_bodies
from . import EXTENSIONS
from .native import SourceCache, finish, make_mutant, new_report, python_only_notes, run_engine, selected_functions, tail

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from . import ProgressCallback

LANG = "typescript"
ENGINE = "stryker"
ENGINE_NAME = "StrykerJS"
EXTS = EXTENSIONS[LANG]
EXCLUDE = (*DEFAULT_EXCLUDE, "coverage", "reports", ".stryker-tmp", ".next", "out", "*.d.ts", "*.config.*",
           "stryker.*", "vitest.*", "jest.*", ".eslintrc*", "eslint.config.*")

STATUS = {"Killed": KILLED, "Timeout": TIMEOUT, "Survived": SURVIVED, "NoCoverage": NO_COVERAGE}
EXCLUDED_STATUSES = frozenset({"CompileError", "RuntimeError", "Ignored", "Pending"})
RUNNERS = {
    "@stryker-mutator/vitest-runner": "vitest",
    "@stryker-mutator/jest-runner": "jest",
    "@stryker-mutator/mocha-runner": "mocha",
    "@stryker-mutator/jasmine-runner": "jasmine",
    "@stryker-mutator/karma-runner": "karma",
    "@stryker-mutator/tap-runner": "tap",
    "@stryker-mutator/cucumber-runner": "cucumber",
}
CONFIG_NAMES = (
    "stryker.config.json", ".stryker.conf.json", "stryker.conf.json", "stryker.config.mjs", "stryker.config.js",
    "stryker.config.cjs", "stryker.conf.mjs", "stryker.conf.js", "stryker.conf.cjs",
)
INSTALL_HINT = (
    "npm install --save-dev @stryker-mutator/core @stryker-mutator/vitest-runner "
    "(use @stryker-mutator/jest-runner or -mocha-runner for other test frameworks)"
)
OPERATORS = {
    "EqualityOperator": "comparison",
    "LogicalOperator": "boolean",
    "BooleanLiteral": "boolean",
    "ConditionalExpression": "condition",
    "OptionalChaining": "condition",
    "ArithmeticOperator": "arithmetic",
    "UpdateOperator": "arithmetic",
    "AssignmentOperator": "arithmetic",
    "UnaryOperator": "arithmetic",
    "StringLiteral": "string",
    "Regex": "string",
    "ArrayDeclaration": "literal",
    "ObjectLiteral": "literal",
    "ArrowFunction": "return-value",
    "MethodExpression": "method",
    "BlockStatement": "remove-block",
}
_TEST_NAME = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_TEST_DIRS = frozenset({"__tests__", "test", "tests", "__mocks__", "e2e", "spec"})


def is_test_file(rel: Path) -> bool:
    if _TEST_NAME.search(rel.name):
        return True
    return any(part in _TEST_DIRS or part.startswith(("tests_", "test_")) for part in rel.parts[:-1])


def normalize_operator(mutator: str, original: str, prefix: str) -> tuple[str, str]:
    """Map a Stryker mutator to testteeth's operator vocabulary; returns (operator, exception)."""
    if mutator == "BlockStatement":
        body = original.strip().strip("{}").strip()
        throw = re.match(r"throw\s+(?:new\s+)?([A-Za-z_$][\w$.]*)?", body)
        if throw and body.count(";") <= 1:
            return "remove-raise", throw.group(1) or "Error"
        if re.search(r"\bcatch\s*(?:\([^)]*\))?\s*$", prefix):
            return "swallow-exception", "the error"
        return "remove-block", ""
    return OPERATORS.get(mutator, re.sub(r"(?<!^)(?=[A-Z])", "-", mutator).lower()), ""


def _rel(key: str, root: Path) -> str:
    path = Path(key)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def _require(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise ReportError(f"malformed {ENGINE_NAME} report: {where} lacks {key!r}")
    return obj[key]


def parse_report(data: Any, root: Path) -> tuple[list[Mutant], int, dict[str, str]]:
    """Parse a mutation-testing-report-schema JSON document into shared-model mutants.

    Returns ``(mutants, excluded_count, test_sources)`` where ``test_sources`` maps test file -> source text
    (from the report's ``testFiles`` when present). Raises :class:`ReportError` on malformed input.
    """
    files = _require(data, "files", "the report")
    if not isinstance(files, dict):
        raise ReportError(f"malformed {ENGINE_NAME} report: 'files' must be an object")
    fallback = {_rel(k, root): v.get("source", "") for k, v in files.items() if isinstance(v, dict)}
    cache = SourceCache(root, LANG, fallback=fallback)
    mutants: list[Mutant] = []
    excluded = 0
    for key in sorted(files):
        rel = _rel(key, root)
        entries = _require(files[key], "mutants", f"file {key!r}")
        if not isinstance(entries, list):
            raise ReportError(f"malformed {ENGINE_NAME} report: mutants of {key!r} must be a list")
        source = cache.source(rel) or ""
        for i, raw in enumerate(entries):
            where = f"mutant #{i} of {key!r}"
            status_raw = _require(raw, "status", where)
            mutator = _require(raw, "mutatorName", where)
            location = _require(raw, "location", where)
            start, end = _require(location, "start", where), _require(location, "end", where)
            try:
                start_pos = (int(start["line"]), int(start["column"]))
                end_pos = (int(end["line"]), int(end["column"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ReportError(f"malformed {ENGINE_NAME} report: {where} has an invalid location") from exc
            if status_raw in EXCLUDED_STATUSES:
                excluded += 1
                continue
            if status_raw not in STATUS:
                raise ReportError(f"malformed {ENGINE_NAME} report: {where} has unknown status {status_raw!r}")
            lines = source.splitlines(keepends=True)
            prefix = ""
            if 0 < start_pos[0] <= len(lines):
                offset = sum(len(t) for t in lines[: start_pos[0] - 1]) + start_pos[1] - 1
                prefix = source[max(0, offset - 60) : offset]
            mutant = make_mutant(
                cache,
                mutant_id=f"{rel}#{raw.get('id', i)}",
                rel=rel,
                start=start_pos,
                end=end_pos,
                replacement=str(raw.get("replacement", "")),
                operator="",
                engine_operator=str(mutator),
                description="",
                status=STATUS[status_raw],
                detail=str(raw.get("statusReason") or ""),
            )
            mutant.operator, mutant.exception = normalize_operator(str(mutator), mutant.original, prefix)
            mutant.description = f"{mutator}: `{_short(mutant.original)}` -> `{_short(mutant.replacement)}`"
            if not mutant.detail and mutant.status == KILLED and raw.get("killedBy"):
                mutant.detail = f"killed by {len(raw['killedBy'])} test(s)"
            mutants.append(mutant)
    mutants.sort(key=lambda m: (m.file, m.line, m.col, m.end_line, m.end_col))
    tests: dict[str, str] = {}
    test_files = data.get("testFiles") if isinstance(data, dict) else None
    if isinstance(test_files, dict):
        for key, entry in test_files.items():
            if not key:
                continue
            rel = _rel(key, root)
            text = entry.get("source") if isinstance(entry, dict) else None
            text = text or read_text(root / rel)
            if text:
                tests[rel] = text
    return mutants, excluded, tests


def _short(text: str, width: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def find_stryker(root: Path) -> Path | None:
    for directory in [root, *root.parents]:
        candidate = directory / "node_modules" / "@stryker-mutator" / "core"
        if candidate.is_dir():
            return candidate
    return None


def check_installed(root: Path) -> str:
    npx = shutil.which("npx")
    if npx is None:
        raise EngineNotInstalled(
            f"{ENGINE_NAME} needs Node.js and npx, which were not found on PATH",
            "install Node.js 18+ (https://nodejs.org), then: " + INSTALL_HINT,
        )
    if find_stryker(root) is None:
        raise EngineNotInstalled(f"{ENGINE_NAME} (@stryker-mutator/core) is not installed in {root}", INSTALL_HINT)
    return npx


def detect_runner(root: Path) -> str:
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        package = {}
    deps: dict[str, Any] = {}
    for key in ("devDependencies", "dependencies"):
        if isinstance(package.get(key), dict):
            deps.update(package[key])
    for plugin, runner in RUNNERS.items():
        if plugin in deps:
            return runner
    return "command"


def user_config(root: Path, engine_config: str | None) -> tuple[dict[str, Any] | None, Path | None]:
    """The user's Stryker config: ``(json_dict, None)`` for JSON configs, ``(None, path)`` for JS ones."""
    path: Path | None = None
    if engine_config:
        path = (root / engine_config).resolve()
        if not path.is_file():
            raise ConfigError(f"Stryker config not found: {engine_config}")
    else:
        path = next((root / n for n in CONFIG_NAMES if (root / n).is_file()), None)
    if path is None:
        return {}, None
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ConfigError(f"{path.name}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path.name}: a Stryker config must be a JSON object")
        return data, None
    return None, path


def mutate_entries(
    root: Path, files: list[Path], changed: dict[Path, frozenset[int]] | None, cache: SourceCache
) -> dict[str, tuple[list[str], frozenset[int] | None]]:
    """Per file: Stryker ``mutate`` entries and the changed lines (``None`` = whole file)."""
    selection: dict[str, tuple[list[str], frozenset[int] | None]] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        if changed is None:
            selection[rel] = ([rel], None)
            continue
        lines = changed.get(path.resolve())
        if lines is None:
            continue
        if lines == ALL_LINES:
            selection[rel] = ([rel], None)
            continue
        scanned = cache.scan(rel)
        ranges: list[tuple[int, int]] = []
        for n in sorted(lines):
            func = scanned.outermost_at(n) if scanned else None
            ranges.append((func.line, func.end_line) if func else (n, n))
        merged: list[list[int]] = []
        for a, b in sorted(ranges):
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        if merged:
            selection[rel] = ([f"{rel}:{a}-{b}" for a, b in merged], lines)
    return selection


class StrykerAdapter:
    lang = LANG
    engine = ENGINE

    def grade(self, settings: Settings, progress: ProgressCallback | None = None) -> GradeReport:
        started = time.perf_counter()
        root = settings.root
        paths = settings.paths
        if paths == ["."]:
            paths = next(([d] for d in ("src", "lib") if (root / d).is_dir()), ["."])
        exclude = [*EXCLUDE, *settings.exclude]
        files = walk_files(root, paths, EXTS, exclude, is_test_file, want_tests=False)
        changed = changed_lines(root, settings.base_ref, tuple(f"*{e}" for e in EXTS)) if settings.base_ref else None
        cache = SourceCache(root, LANG)
        selection = mutate_entries(root, files, changed, cache)
        report = new_report(settings, LANG, ENGINE, ["npx", "stryker", "run"])
        report.notes.extend(python_only_notes(settings, ENGINE_NAME))
        if not selection:
            report.notes.append(
                "no changed TypeScript/JavaScript functions to grade" if settings.base_ref
                else "no TypeScript/JavaScript source files found"
            )
            return finish(report, started, progress)

        npx = check_installed(root)
        mutate = [entry for entries, _ in selection.values() for entry in entries]
        user_json, user_js = user_config(root, settings.engine_config)
        with tempfile.TemporaryDirectory(prefix="testteeth-stryker-") as tmp:
            tmpdir = Path(tmp)
            if user_js is not None:
                report_path = root / "reports" / "mutation" / "mutation.json"
                report_path.unlink(missing_ok=True)
                cmd = [npx, "--no-install", "stryker", "run", str(user_js), "--mutate", ",".join(mutate),
                       "--reporters", "json", "--concurrency", str(settings.workers),
                       "--timeoutFactor", str(settings.timeout_factor)]
                if not settings.coverage:
                    cmd += ["--coverageAnalysis", "off"]
                report.notes.append(f"using {user_js.name}; the report is read from reports/mutation/mutation.json")
            else:
                report_path = tmpdir / "mutation.json"
                config = dict(user_json or {})
                config.setdefault("testRunner", detect_runner(root))
                if config["testRunner"] == "command":
                    config.setdefault("commandRunner", {"command": "npm test"})
                config.update(
                    mutate=mutate,
                    reporters=["json"],
                    jsonReporter={"fileName": str(report_path)},
                    concurrency=settings.workers,
                    timeoutFactor=settings.timeout_factor,
                    tempDirName=str(tmpdir / "stryker-tmp"),
                    incremental=False,
                    thresholds={**(config.get("thresholds") or {}), "break": None},
                )
                if not settings.coverage:
                    config["coverageAnalysis"] = "off"
                config_path = tmpdir / "stryker.testteeth.json"
                config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
                cmd = [npx, "--no-install", "stryker", "run", str(config_path)]
            cmd += settings.engine_args
            report.test_command = ["npx", "stryker", "run", *cmd[4:]]
            run = run_engine(cmd, root)
            report.baseline_seconds = 0.0
            data = self._load(report_path, run.returncode, run.output)
        if data is None:
            report.notes.append(f"{ENGINE_NAME} produced no report (nothing to mutate?)")
            return finish(report, started, progress)
        if run.returncode not in (0, None):
            report.notes.append(f"{ENGINE_NAME} exited with code {run.returncode} but wrote a report")

        mutants, excluded, tests = parse_report(data, root)
        report.mutants = mutants
        report.excluded = excluded
        if excluded:
            report.notes.append(
                f"{excluded} mutant(s) did not compile or crashed the runner and are excluded from the score"
            )
        if not tests:
            tests = {
                p.relative_to(root).as_posix(): text
                for p in walk_files(root, ["."], EXTS, [*DEFAULT_EXCLUDE, "coverage", "reports", ".stryker-tmp"],
                                    is_test_file, want_tests=True)
                if (text := read_text(p)) is not None
            }
        bodies = [body for text in tests.values() for body in test_bodies(text, LANG)]
        coverage = line_coverage(mutants)
        for rel, (_, lines) in selection.items():
            scanned = cache.scan(rel)
            if scanned is None:
                continue
            report.gaps.extend(analyze(scanned, rel, bodies, selected_functions(cache, rel, lines), coverage.get(rel)))
        return finish(report, started, progress)

    @staticmethod
    def _load(report_path: Path, returncode: int | None, output: str) -> Any:
        if not report_path.is_file():
            if re.search(r"failed tests in the initial test run|initial test run (?:failed|encountered)"
                         r"|There were failed tests", output, re.I):
                raise BaselineFailed(
                    f"the test suite fails before any mutation ({ENGINE_NAME} initial test run); fix it first", output
                )
            if re.search(r"No tests were (?:executed|found)|No test files found", output, re.I):
                raise BaselineFailed(f"the test command collected no tests ({ENGINE_NAME})", output)
            if returncode == 0:
                return None
            raise EngineCrashed(
                f"{ENGINE_NAME} exited with code {returncode} without writing a report:\n{tail(output, 15)}", output
            )
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ReportError(f"{ENGINE_NAME} wrote an invalid JSON report: {exc}") from exc


__all__ = ["StrykerAdapter", "parse_report", "normalize_operator", "EngineError"]
