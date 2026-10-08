"""Language adapters.

Every adapter turns a :class:`~testteeth.config.Settings` into the shared
:class:`~testteeth.models.GradeReport`:

* ``python`` - testteeth's own AST mutation engine (:mod:`testteeth.engine`).
* ``typescript`` (also JavaScript) - wraps StrykerJS (``npx stryker run``) and reads its
  mutation-testing-report-schema JSON.
* ``rust`` - wraps cargo-mutants and reads ``mutants.out/outcomes.json``.

The language is detected from the repository (``pyproject.toml`` / ``package.json`` / ``Cargo.toml``) and can
be overridden with ``--lang``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..models import GradeReport, Mutant

LANGUAGES = ("python", "typescript", "rust")
ALIASES = {
    "python": "python",
    "py": "python",
    "typescript": "typescript",
    "ts": "typescript",
    "javascript": "typescript",
    "js": "typescript",
    "node": "typescript",
    "rust": "rust",
    "rs": "rust",
}
ENGINES = {"python": "testteeth", "typescript": "stryker", "rust": "cargo-mutants"}
FENCE = {"python": "python", "typescript": "ts", "rust": "rust"}

MARKERS: dict[str, tuple[str, ...]] = {
    "rust": ("Cargo.toml",),
    "typescript": ("package.json",),
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"),
}
EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "typescript": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    "rust": (".rs",),
}
_SKIP_DIRS = frozenset(
    {".git", "node_modules", "target", ".venv", "venv", "dist", "build", "__pycache__", ".tox", "coverage", "reports",
     ".stryker-tmp", "mutants.out", "mutants.out.old"}
)

ProgressCallback = Callable[[int, int, "Mutant"], None]


class Adapter(Protocol):
    lang: str
    engine: str

    def grade(self, settings: Settings, progress: ProgressCallback | None = None) -> GradeReport: ...


def normalize_lang(value: str) -> str:
    """Canonical language name for ``value`` (``ts`` -> ``typescript``...); raises ConfigError if unknown."""
    from ..config import ConfigError

    key = value.strip().lower()
    if key not in ALIASES:
        raise ConfigError(
            f"unknown language {value!r}; supported: python, typescript (alias: ts, javascript, js), rust (alias: rs)"
        )
    return ALIASES[key]


def _count_sources(root: Path, limit: int = 5000) -> dict[str, int]:
    counts = dict.fromkeys(LANGUAGES, 0)
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            seen += 1
            if name.endswith(".d.ts"):
                continue
            for lang, exts in EXTENSIONS.items():
                if name.endswith(exts):
                    counts[lang] += 1
        if seen > limit:
            break
    return counts


def detect_language(root: Path) -> tuple[str, str | None]:
    """Return ``(language, note)`` for the project at ``root``.

    One marker file decides it. With several (e.g. a Python package with a ``package.json`` for docs tooling),
    the language with the most source files wins and the note says so. Without any marker, source files
    decide; an empty directory is treated as Python (the historical default).
    """
    found = [lang for lang, files in MARKERS.items() if any((root / f).is_file() for f in files)]
    if len(found) == 1:
        return found[0], None
    counts = _count_sources(root)
    candidates = found or [lang for lang in LANGUAGES if counts[lang]]
    if not candidates:
        return "python", None
    best = max(candidates, key=lambda lang: (counts[lang], -LANGUAGES.index(lang)))
    if len(found) > 1:
        others = ", ".join(lang for lang in found if lang != best)
        return best, f"detected {best} (also found {others} project files); override with --lang"
    return best, None


def get_adapter(lang: str) -> Adapter:
    lang = normalize_lang(lang)
    if lang == "typescript":
        from .typescript import StrykerAdapter

        return StrykerAdapter()
    if lang == "rust":
        from .rust import CargoMutantsAdapter

        return CargoMutantsAdapter()
    from .python import PythonAdapter

    return PythonAdapter()


def grade_project(settings: Settings, progress: ProgressCallback | None = None) -> GradeReport:
    """Grade the project with the adapter for its (detected or configured) language."""
    note = None
    lang = settings.lang
    if lang is None:
        lang, note = detect_language(settings.root)
    report = get_adapter(lang).grade(settings, progress)
    if note:
        report.notes.insert(0, note)
    return report
