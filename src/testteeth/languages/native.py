"""Plumbing shared by the adapters that wrap native mutation engines (StrykerJS, cargo-mutants)."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..core import build_function_grades, char_to_byte_col, span_text
from ..models import GradeReport, Mutant
from .scan import Scan, scan

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from . import ProgressCallback

_CONTEXT_START = re.compile(r"^(?:\}\s*)?(if|else|catch|for|while|switch|match|loop|try|case)\b")


def tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run_engine(cmd: list[str], cwd: Path, env_extra: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    """Run an engine command to completion (no timeout: engines enforce per-mutant timeouts themselves)."""
    from ..engine import run_tests

    env = dict(os.environ)
    env.update({"FORCE_COLOR": "0", "NO_COLOR": "1", "CARGO_TERM_COLOR": "never", "TESTTEETH_ACTIVE": "1"})
    if env_extra:
        env.update(env_extra)
    return run_tests(cmd, cwd, env, timeout=None)


def block_context(scanned: Scan, line: int, col: int = 0) -> str:
    """Header of the innermost ``if``/``catch``/``match``... block enclosing a position, e.g. ``catch (err)``."""
    masked = scanned.masked
    offset = min(scanned.index.offset(line) + col, len(masked))
    depth = 0
    k = offset - 1
    while k >= 0:
        ch = masked[k]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                j = k - 1
                while j >= 0 and masked[j] not in ";{}":
                    if masked[j] == ")":  # skip a parenthesised header such as `for (a; b; c)`
                        level = 0
                        while j >= 0:
                            level += {")": 1, "(": -1}.get(masked[j], 0)
                            if level == 0:
                                break
                            j -= 1
                    j -= 1
                header = " ".join(masked[j + 1 : k].split())
                if _CONTEXT_START.match(header):
                    header = re.sub(r"^\}\s*", "", header)
                    return header if len(header) <= 80 else header[:77] + "..."
                if header and not header.startswith(("=>", ")")):
                    return ""
            else:
                depth -= 1
        k -= 1
    return ""


@dataclass
class SourceCache:
    root: Path
    lang: str
    fallback: dict[str, str] = field(default_factory=dict)
    _sources: dict[str, str | None] = field(default_factory=dict)
    _scans: dict[str, Scan] = field(default_factory=dict)

    def source(self, rel: str) -> str | None:
        if rel not in self._sources:
            path = self.root / rel
            try:
                self._sources[rel] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._sources[rel] = self.fallback.get(rel)
        return self._sources[rel]

    def scan(self, rel: str) -> Scan | None:
        if rel not in self._scans:
            text = self.source(rel)
            if text is None:
                return None
            self._scans[rel] = scan(text, self.lang)
        return self._scans[rel]


def make_mutant(
    cache: SourceCache,
    *,
    mutant_id: str,
    rel: str,
    start: tuple[int, int],
    end: tuple[int, int],
    replacement: str,
    operator: str,
    engine_operator: str,
    description: str,
    status: str,
    function: str | None = None,
    function_line: int | None = None,
    detail: str = "",
    exception: str = "",
) -> Mutant:
    """Build a shared-model mutant from 1-based (line, column) positions with an exclusive end column."""
    source = cache.source(rel) or ""
    lines = source.splitlines()
    line, col = start
    end_line, end_col = end
    char_col, char_end = max(col - 1, 0), max(end_col - 1, 0)
    original = span_text(source, line, char_col, end_line, char_end) if source else ""
    text_at = lines[line - 1] if 0 < line <= len(lines) else ""
    end_text = lines[end_line - 1] if 0 < end_line <= len(lines) else ""
    scanned = cache.scan(rel)
    context = ""
    if scanned is not None:
        if function is None:
            func = scanned.function_at(line)
            scope = scanned.scope_at(line) if func is None else None
            if func is not None:
                function, function_line = func.qualname, func.line
            elif scope is not None:
                function, function_line = scope
            else:
                function, function_line = "<module>", 0
        inside = 1 if original.startswith("{") else 0  # a block mutant: describe the block's own header
        context = block_context(scanned, line, char_col + inside)
    return Mutant(
        id=mutant_id,
        file=rel,
        function=function or "<module>",
        function_line=function_line or 0,
        line=line,
        col=char_to_byte_col(text_at, char_col),
        end_line=end_line,
        end_col=char_to_byte_col(end_text, char_end),
        operator=operator,
        description=description,
        original=original,
        replacement=replacement,
        context=context,
        exception=exception,
        status=status,
        detail=detail,
        engine_operator=engine_operator,
    )


def new_report(settings: Settings, lang: str, engine: str, command: list[str]) -> GradeReport:
    return GradeReport(
        root=str(settings.root),
        base_ref=settings.base_ref,
        test_command=command,
        fail_under=settings.fail_under,
        lang=lang,
        engine=engine,
    )


def python_only_notes(settings: Settings, engine_name: str) -> list[str]:
    notes = []
    if settings.test_args:
        notes.append(f"--pytest-args is ignored for {engine_name}; pass engine options with --engine-args")
    if settings.operators:
        notes.append(f"--operators is ignored for {engine_name} (it uses its own mutators)")
    if settings.max_mutants:
        notes.append(f"--max-mutants is not supported by {engine_name} and was ignored")
    return notes


def finish(report: GradeReport, started: float, progress: ProgressCallback | None) -> GradeReport:
    report.functions = build_function_grades(report.mutants, report.gaps)
    if progress:
        total = len(report.mutants)
        for i, mutant in enumerate(report.mutants, start=1):
            progress(i, total, mutant)
    report.elapsed_seconds = time.perf_counter() - started
    return report


def selected_functions(cache: SourceCache, rel: str, lines: frozenset[int] | None) -> set[str] | None:
    """Qualnames of the (non-test) functions touched by ``lines``; ``None`` means every function."""
    from ..diff import ALL_LINES

    if lines is None or lines == ALL_LINES:
        return None
    scanned = cache.scan(rel)
    if scanned is None:
        return set()
    return {f.qualname for f in scanned.functions if not f.is_test and any(f.contains(n) for n in lines)}
