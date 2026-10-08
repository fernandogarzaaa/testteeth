"""Rust adapter: wraps cargo-mutants.

testteeth runs ``cargo mutants`` (with ``--in-diff`` for ``--base`` and ``--file`` for explicit paths) and reads
``mutants.out/outcomes.json``. Failure-path gaps come from the Rust rule pack.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import DEFAULT_EXCLUDE
from ..core import read_text, walk_files
from ..diff import diff_with_untracked, parse_unified_diff
from ..errors import BaselineFailed, EngineCrashed, EngineNotInstalled, ReportError
from ..models import KILLED, SURVIVED, TIMEOUT, GradeReport, Mutant
from ..rules import analyze
from ..rules.rust import rust_test_bodies
from .native import SourceCache, finish, make_mutant, new_report, python_only_notes, run_engine, selected_functions, tail

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from . import ProgressCallback

LANG = "rust"
ENGINE = "cargo-mutants"
ENGINE_NAME = "cargo-mutants"
INSTALL_HINT = "cargo install --locked cargo-mutants   (or: cargo binstall cargo-mutants)"
STATUS = {"CaughtMutant": KILLED, "MissedMutant": SURVIVED, "Timeout": TIMEOUT}
EXCLUDED_STATUSES = frozenset({"Unviable"})
COMPARISON = frozenset({"==", "!=", "<", ">", "<=", ">="})
BOOLEAN = frozenset({"&&", "||"})
EXCLUDE = (*DEFAULT_EXCLUDE, "target", "mutants.out", "mutants.out.old")


def normalize_operator(genre: str, original: str, replacement: str) -> str:
    """Map a cargo-mutants genre to testteeth's operator vocabulary."""
    token = original.strip()
    if genre == "FnValue":
        return "return-value"
    if genre == "BinaryOperator":
        base = token[:-1] if token.endswith("=") and token not in COMPARISON else token
        if token in COMPARISON:
            return "comparison"
        if token in BOOLEAN or base in BOOLEAN:
            return "boolean"
        return "arithmetic"
    if genre == "UnaryOperator":
        return "boolean" if token == "!" else "arithmetic"
    if genre == "MatchArm":
        return "remove-branch"
    if genre == "MatchArmGuard":
        return "condition"
    if genre == "StructField":
        return "literal"
    return re.sub(r"(?<!^)(?=[A-Z])", "-", genre).lower() or "other"


def _require(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise ReportError(f"malformed {ENGINE_NAME} report: {where} lacks {key!r}")
    return obj[key]


def _pos(span: Any, which: str, where: str) -> tuple[int, int]:
    point = _require(span, which, where)
    try:
        return int(point["line"]), int(point["column"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportError(f"malformed {ENGINE_NAME} report: {where} has an invalid span") from exc


def parse_outcomes(data: Any, root: Path) -> tuple[list[Mutant], int, dict[str, Any] | None]:
    """Parse ``mutants.out/outcomes.json`` into shared-model mutants.

    Returns ``(mutants, excluded_count, baseline_outcome_or_None)``. Raises :class:`ReportError` on malformed
    input.
    """
    outcomes = _require(data, "outcomes", "the report")
    if not isinstance(outcomes, list):
        raise ReportError(f"malformed {ENGINE_NAME} report: 'outcomes' must be a list")
    cache = SourceCache(root, LANG)
    mutants: list[Mutant] = []
    excluded = 0
    baseline: dict[str, Any] | None = None
    per_file: dict[str, int] = {}
    for i, outcome in enumerate(outcomes):
        where = f"outcome #{i}"
        scenario = _require(outcome, "scenario", where)
        summary = _require(outcome, "summary", where)
        if scenario == "Baseline":
            baseline = outcome
            continue
        raw = _require(scenario, "Mutant", where)
        rel = str(_require(raw, "file", where)).replace(os.sep, "/")
        span = _require(raw, "span", where)
        start, end = _pos(span, "start", where), _pos(span, "end", where)
        replacement = str(_require(raw, "replacement", where))
        genre = str(raw.get("genre", "Unknown"))
        if summary in EXCLUDED_STATUSES:
            excluded += 1
            continue
        if summary not in STATUS:
            raise ReportError(f"malformed {ENGINE_NAME} report: {where} has unknown summary {summary!r}")
        function = raw.get("function") if isinstance(raw.get("function"), dict) else None
        func_name = function.get("function_name") if function else None
        func_line = None
        if function and isinstance(function.get("span"), dict):
            try:
                func_line = int(function["span"]["start"]["line"])
            except (KeyError, TypeError, ValueError):
                func_line = None
        name = str(raw.get("name", ""))
        description = name.split(": ", 1)[1] if ": " in name else name or f"{genre} -> {replacement}"
        per_file[rel] = per_file.get(rel, 0) + 1
        mutant = make_mutant(
            cache,
            mutant_id=f"{rel}#{per_file[rel]}",
            rel=rel,
            start=start,
            end=end,
            replacement=replacement,
            operator="",
            engine_operator=genre,
            description=description,
            status=STATUS[summary],
            function=func_name,
            function_line=func_line,
        )
        mutant.operator = normalize_operator(genre, mutant.original, replacement)
        if genre == "FnValue" and function and "Result" in str(function.get("return_type", "")):
            mutant.exception = "Err"
        mutants.append(mutant)
    return mutants, excluded, baseline


def find_cargo() -> str | None:
    cargo = shutil.which("cargo")
    if cargo:
        return cargo
    home = Path.home() / ".cargo" / "bin" / "cargo"
    return str(home) if home.is_file() else None


def check_installed() -> str:
    cargo = find_cargo()
    if cargo is None:
        raise EngineNotInstalled(
            "cargo was not found on PATH (cargo-mutants needs a Rust toolchain)",
            "install Rust from https://rustup.rs, then: " + INSTALL_HINT,
        )
    try:
        proc = subprocess.run([cargo, "mutants", "--version"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineNotInstalled(f"could not run `cargo mutants --version`: {exc}", INSTALL_HINT) from exc
    if proc.returncode != 0:
        raise EngineNotInstalled("cargo-mutants is not installed", INSTALL_HINT)
    return cargo


def test_target_names(engine_args: list[str]) -> list[str] | None:
    """Integration-test targets selected with ``-- --test NAME``; ``None`` when all tests run."""
    if "--" not in engine_args:
        return None
    after = engine_args[engine_args.index("--") + 1 :]
    names = [after[i + 1] for i, arg in enumerate(after[:-1]) if arg == "--test"]
    names += [arg.split("=", 1)[1] for arg in after if arg.startswith("--test=")]
    return names or None


def gather_test_bodies(root: Path, engine_args: list[str], exclude: list[str]) -> list[str]:
    targets = test_target_names(engine_args)
    files: list[Path] = []
    if targets is not None:
        for name in targets:
            for candidate in (root / "tests" / f"{name}.rs", root / "tests" / name / "main.rs"):
                if candidate.is_file():
                    files.append(candidate)
        bodies: list[str] = []
        for path in files:
            text = read_text(path)
            if text:
                bodies.extend(rust_test_bodies(text))
        return bodies
    bodies = []
    for path in walk_files(root, ["."], (".rs",), exclude):
        text = read_text(path)
        if not text:
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in ("tests", "benches") or path.name.endswith("_test.rs"):
            bodies.extend(rust_test_bodies(text))
        elif "#[test]" in text or "#[cfg(test)]" in text or "::test]" in text:
            found = rust_test_bodies(text)
            if found != [text]:
                bodies.extend(found)
    return bodies


class CargoMutantsAdapter:
    lang = LANG
    engine = ENGINE

    def grade(self, settings: Settings, progress: ProgressCallback | None = None) -> GradeReport:
        started = time.perf_counter()
        root = settings.root
        report = new_report(settings, LANG, ENGINE, ["cargo", "mutants"])
        report.notes.extend(python_only_notes(settings, ENGINE_NAME))
        exclude = [*EXCLUDE, *settings.exclude]
        cache = SourceCache(root, LANG)
        changed: dict[str, frozenset[int]] | None = None
        diff_text = ""
        if settings.base_ref:
            diff_text = diff_with_untracked(root, settings.base_ref, ("*.rs",))
            changed = {rel: frozenset(lines) for rel, lines in parse_unified_diff(diff_text).items()}
            if not changed:
                report.notes.append("no changed Rust functions to grade")
                return finish(report, started, progress)
        if not (root / "Cargo.toml").is_file():
            raise EngineCrashed(f"no Cargo.toml in {root}; point --root at the crate or workspace")

        cargo = check_installed()
        with tempfile.TemporaryDirectory(prefix="testteeth-cargo-mutants-") as tmp:
            tmpdir = Path(tmp)
            cmd = [cargo, "mutants", "--no-shuffle", "--output", str(tmpdir), "--jobs", str(settings.workers),
                   "--timeout-multiplier", str(settings.timeout_factor)]
            if diff_text:
                diff_path = tmpdir / "changes.diff"
                diff_path.write_text(diff_text, encoding="utf-8")
                cmd += ["--in-diff", str(diff_path)]
            if settings.paths != ["."]:
                for raw in settings.paths:
                    target = (root / raw).resolve()
                    if not target.exists():
                        raise EngineCrashed(f"path not found: {raw}")
                    rel = target.relative_to(root).as_posix()
                    cmd += ["--file", rel if target.is_file() else f"{rel.rstrip('/')}/**"]
            cmd += settings.engine_args
            report.test_command = ["cargo", "mutants", *settings.engine_args]
            run = run_engine(cmd, root)
            outcomes_path = tmpdir / "mutants.out" / "outcomes.json"
            baseline_log = tmpdir / "mutants.out" / "log" / "baseline.log"
            baseline_text = read_text(baseline_log) or ""
            if not outcomes_path.is_file():
                if run.returncode == 4 or re.search(r"baseline.*fail|FAILED.*baseline", run.output, re.I):
                    raise BaselineFailed(
                        f"the test suite fails before any mutation ({ENGINE_NAME} baseline); fix it first",
                        baseline_text or run.output,
                    )
                if run.returncode == 0:
                    report.notes.append(f"{ENGINE_NAME} found nothing to mutate")
                    return finish(report, started, progress)
                raise EngineCrashed(
                    f"{ENGINE_NAME} exited with code {run.returncode} without writing outcomes.json:\n"
                    f"{tail(run.output, 15)}",
                    run.output,
                )
            try:
                data = json.loads(outcomes_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ReportError(f"{ENGINE_NAME} wrote an invalid outcomes.json: {exc}") from exc

        mutants, excluded, baseline = parse_outcomes(data, root)
        if baseline is not None and baseline.get("summary") != "Success":
            raise BaselineFailed(
                f"the test suite fails before any mutation ({ENGINE_NAME} baseline: {baseline.get('summary')}); "
                "fix it first",
                baseline_text or run.output,
            )
        if baseline is not None:
            report.baseline_seconds = sum(
                float(p.get("duration", 0.0)) for p in baseline.get("phase_results", []) if isinstance(p, dict)
            )
        if run.returncode not in (0, 2, 3):
            report.notes.append(f"{ENGINE_NAME} exited with code {run.returncode} but wrote outcomes.json")
        report.mutants = mutants
        report.excluded = excluded
        if excluded:
            report.notes.append(f"{excluded} unviable mutant(s) (did not compile) are excluded from the score")

        bodies = gather_test_bodies(root, settings.engine_args, exclude)
        files = sorted({m.file for m in mutants} | set(changed or {}))
        for rel in files:
            if not rel.endswith(".rs") or Path(rel).parts[:1] in (("tests",), ("benches",)):
                continue
            scanned = cache.scan(rel)
            if scanned is None:
                continue
            lines = changed.get(rel) if changed is not None else None
            if changed is not None and lines is None:
                continue
            report.gaps.extend(analyze(scanned, rel, bodies, selected_functions(cache, rel, lines)))
        return finish(report, started, progress)
