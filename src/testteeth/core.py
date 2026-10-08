"""Language-agnostic helpers shared by every adapter: grouping by function, coverage from engine
statuses, file discovery for non-Python languages and span conversion."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from .errors import EngineError
from .models import NO_COVERAGE, FailurePathGap, FunctionGrade, Mutant


def build_function_grades(mutants: Iterable[Mutant], gaps: Iterable[FailurePathGap]) -> list[FunctionGrade]:
    """Group mutants and failure-path gaps by (file, function), sorted by file and line."""
    grades: dict[tuple[str, str], FunctionGrade] = {}
    for mutant in mutants:
        key = (mutant.file, mutant.function)
        if key not in grades:
            grades[key] = FunctionGrade(mutant.file, mutant.function, mutant.function_line)
        grades[key].mutants.append(mutant)
    for gap in gaps:
        key = (gap.file, gap.function)
        if key not in grades:
            grades[key] = FunctionGrade(gap.file, gap.function, gap.function_line)
        grades[key].gaps.append(gap)
    return sorted(grades.values(), key=lambda g: (g.file, g.line, g.function))


def line_coverage(mutants: Iterable[Mutant]) -> dict[str, dict[int, bool]]:
    """Per file, per line: was the line executed by some test?

    Derived from engine statuses (StrykerJS reports ``NoCoverage``): a line is known-covered if any mutant
    starting on it ran against a test, known-uncovered if every mutant starting on it had no coverage.
    Lines without mutants are absent (unknown).
    """
    result: dict[str, dict[int, bool]] = {}
    for mutant in mutants:
        lines = result.setdefault(mutant.file, {})
        covered = mutant.status != NO_COVERAGE
        lines[mutant.line] = lines.get(mutant.line, False) or covered
    return result


def char_to_byte_col(line_text: str, char_col: int) -> int:
    """Convert a 0-based character column to the UTF-8 byte column the span helpers expect."""
    return len(line_text[: max(char_col, 0)].encode("utf-8"))


def span_text(source: str, line: int, col: int, end_line: int, end_col: int) -> str:
    """Text of a span given 1-based lines and 0-based *character* columns."""
    lines = source.splitlines(keepends=True)
    if not 1 <= line <= end_line <= len(lines):
        return ""
    start = sum(len(t) for t in lines[: line - 1]) + col
    end = sum(len(t) for t in lines[: end_line - 1]) + end_col
    return source[start:end]


def _excluded(rel: Path, patterns: Iterable[str]) -> bool:
    pats = list(patterns)
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in pats) or any(
        fnmatch.fnmatch(rel.as_posix(), pat) for pat in pats
    )


def walk_files(
    root: Path,
    paths: Iterable[str],
    extensions: tuple[str, ...],
    exclude: Iterable[str],
    is_test: Callable[[Path], bool] | None = None,
    want_tests: bool = False,
) -> list[Path]:
    """Files under ``paths`` (relative to ``root``) with one of ``extensions``.

    ``is_test`` classifies a relative path as a test file; with ``want_tests`` only test files are returned,
    otherwise only non-test files.
    """
    patterns = list(exclude)
    found: list[Path] = []
    for raw in paths:
        start = (root / raw).resolve()
        if not start.exists():
            raise EngineError(f"path not found: {raw}")
        try:
            start.relative_to(root)
        except ValueError as exc:
            raise EngineError(f"path {raw} is outside the project root {root}") from exc
        candidates: list[Path] = []
        if start.is_file():
            candidates = [start]
        else:
            for dirpath, dirnames, filenames in os.walk(start):
                here = Path(dirpath)
                dirnames[:] = sorted(d for d in dirnames if not _excluded((here / d).relative_to(root), patterns))
                candidates.extend(here / f for f in sorted(filenames))
        for path in candidates:
            rel = path.relative_to(root)
            if not path.name.endswith(extensions) or _excluded(rel, patterns) or path in found:
                continue
            if is_test is not None and is_test(rel) != want_tests:
                continue
            found.append(path)
    return found


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
