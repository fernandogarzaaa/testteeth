"""Shared plumbing for the rule packs."""

from __future__ import annotations

import re
from collections.abc import Callable

from ..languages.scan import Function, Scan
from ..models import FailurePathGap

RETRY_WORDS = re.compile(r"retr(y|ies)|attempt|backoff|tries", re.IGNORECASE)


def short_name(qualname: str) -> str:
    return re.split(r"::|\.", qualname)[-1]


def failure_tested(qualname: str, bodies: list[str], failure_words: re.Pattern[str]) -> bool:
    """True if some test mentions the function and also sets up or expects a failure."""
    pattern = re.compile(rf"\b{re.escape(short_name(qualname))}\b")
    return any(pattern.search(body) and failure_words.search(body) for body in bodies)


class GapCollector:
    """Attributes construct positions to functions and collects gaps (one per kind per function by default)."""

    def __init__(self, scan: Scan, rel_path: str, selected: set[str] | None, lang: str) -> None:
        self.scan = scan
        self.rel = rel_path
        self.selected = selected
        self.lang = lang
        self.gaps: list[FailurePathGap] = []
        self._seen: set[tuple[str, str]] = set()

    def owner(self, offset: int) -> Function | None:
        func = self.scan.function_at(self.scan.index.line(offset))
        if func is None or (self.selected is not None and func.qualname not in self.selected):
            return None
        return func

    def add(self, func: Function, kind: str, line: int, detail: str, once: bool = True) -> None:
        key = (func.qualname, kind)
        if once and key in self._seen:
            return
        self._seen.add(key)
        self.gaps.append(FailurePathGap(self.rel, func.qualname, func.line, kind, line, detail, lang=self.lang))

    def per_function(self, pattern: re.Pattern[str], masked: str) -> dict[str, list[tuple[Function, int, re.Match[str]]]]:
        found: dict[str, list[tuple[Function, int, re.Match[str]]]] = {}
        for match in pattern.finditer(masked):
            func = self.owner(match.start())
            if func is not None:
                found.setdefault(func.qualname, []).append((func, self.scan.index.line(match.start()), match))
        return found

    def result(self) -> list[FailurePathGap]:
        return sorted(self.gaps, key=lambda g: (g.function_line, g.line, g.kind))


def first_names(hits: list[tuple[Function, int, re.Match[str]]], name: Callable[[re.Match[str]], str]) -> str:
    names: list[str] = []
    for _, _, match in hits:
        text = name(match)
        if text not in names:
            names.append(text)
    return "`, `".join(names[:3])
