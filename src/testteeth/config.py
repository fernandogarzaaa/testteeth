"""Settings, optionally read from ``[tool.testteeth]`` in the project's ``pyproject.toml``."""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 in CI
    import tomli as tomllib

DEFAULT_EXCLUDE = (
    ".git",
    ".hg",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".nox",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "*.egg-info",
    ".eggs",
)


class ConfigError(ValueError):
    """Invalid settings."""


@dataclass
class Settings:
    root: Path
    paths: list[str] = field(default_factory=lambda: ["."])
    base_ref: str | None = None
    test_args: list[str] = field(default_factory=list)
    fail_under: float | None = None
    workers: int = field(default_factory=lambda: max(1, min(4, os.cpu_count() or 1)))
    timeout_factor: float = 3.0
    min_timeout: float = 10.0
    max_mutants: int | None = None
    operators: list[str] | None = None
    exclude: list[str] = field(default_factory=list)
    coverage: bool = True
    lang: str | None = None  # None/"auto" = detect from the repo (pyproject.toml / package.json / Cargo.toml)
    engine_args: list[str] = field(default_factory=list)  # extra args for stryker / cargo-mutants
    engine_config: str | None = None  # a StrykerJS config file to start from (TypeScript/JavaScript)

    def validate(self) -> Settings:
        from .operators import ALL_OPERATORS

        if not self.root.is_dir():
            raise ConfigError(f"root {self.root} is not a directory")
        if self.fail_under is not None and not 0 <= self.fail_under <= 100:
            raise ConfigError("fail_under must be between 0 and 100")
        if self.workers < 1:
            raise ConfigError("workers must be >= 1")
        if self.timeout_factor <= 0 or self.min_timeout <= 0:
            raise ConfigError("timeouts must be positive")
        if self.max_mutants is not None and self.max_mutants < 1:
            raise ConfigError("max_mutants must be >= 1")
        if self.lang is not None:
            from .languages import normalize_lang

            self.lang = normalize_lang(self.lang)
        if isinstance(self.engine_args, str):
            self.engine_args = self.engine_args.split()
        if self.operators is not None:
            unknown = sorted(set(self.operators) - set(ALL_OPERATORS))
            if unknown:
                raise ConfigError(f"unknown operator(s): {', '.join(unknown)}; choose from {', '.join(ALL_OPERATORS)}")
        return self

    @property
    def test_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "pytest",
            "-x",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *_no_cov_args(self.test_args),
            *self.test_args,
        ]


def _no_cov_args(test_args: list[str]) -> list[str]:
    """``["--no-cov"]`` when pytest-cov would otherwise measure coverage during testteeth's runs.

    Coverage of mutated code is meaningless, slows every mutant down, lets ``--cov-fail-under`` fail the
    baseline, and (before Python 3.12) replaces the line tracer testteeth relies on.
    """
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") or "--no-cov" in test_args:
        return []
    if any(arg in ("no:cov", "no:pytest_cov") or arg in ("-pno:cov", "-pno:pytest_cov") for arg in test_args):
        return []
    try:
        installed = importlib.util.find_spec("pytest_cov") is not None
    except (ImportError, ValueError):  # pragma: no cover - a broken installation
        installed = False
    return ["--no-cov"] if installed else []


def read_pyproject(root: Path) -> dict[str, Any]:
    """Return the ``[tool.testteeth]`` table of ``root/pyproject.toml`` (empty if absent).

    Projects without a ``pyproject.toml`` or without that table (TypeScript, Rust) can use a ``testteeth.toml`` with the same keys,
    either at top level or under ``[tool.testteeth]``.
    """
    table: Any = None
    path = root / "pyproject.toml"
    if path.is_file():
        table = _load_toml(path).get("tool", {}).get("testteeth")
    if table is None:
        # No pyproject.toml, or one without [tool.testteeth] (e.g. a TypeScript repo with Python tooling).
        path = root / "testteeth.toml"
        if not path.is_file():
            return {}
        data = _load_toml(path)
        tool = data.get("tool")
        table = tool["testteeth"] if isinstance(tool, dict) and "testteeth" in tool else data
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [tool.testteeth] must be a table")
    return {key.replace("-", "_"): value for key, value in table.items()}


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_strings(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)


#: setting -> (predicate, what a valid value looks like)
_TYPES: dict[str, tuple[Any, str]] = {
    "paths": (lambda v: isinstance(v, str) or _is_strings(v), "a string or a list of strings"),
    "base_ref": (lambda v: isinstance(v, str), "a string"),
    "test_args": (lambda v: isinstance(v, str) or _is_strings(v), "a string or a list of strings"),
    "fail_under": (_is_number, "a number"),
    "workers": (lambda v: isinstance(v, int) and not isinstance(v, bool), "an integer"),
    "timeout_factor": (_is_number, "a number"),
    "min_timeout": (_is_number, "a number"),
    "max_mutants": (lambda v: isinstance(v, int) and not isinstance(v, bool), "an integer"),
    "operators": (_is_strings, "a list of strings"),
    "exclude": (_is_strings, "a list of strings"),
    "coverage": (lambda v: isinstance(v, bool), "true or false"),
    "lang": (lambda v: isinstance(v, str), "a string"),
    "engine_args": (lambda v: isinstance(v, str) or _is_strings(v), "a string or a list of strings"),
    "engine_config": (lambda v: isinstance(v, str), "a string"),
}


def _check_types(values: dict[str, Any]) -> None:
    """Reject wrongly typed settings with a clear message instead of a TypeError deep in the engine."""
    for key, value in values.items():
        check, expected = _TYPES[key]
        if not check(value):
            raise ConfigError(f"{key} must be {expected}, got {value!r}")


def load_settings(root: str | os.PathLike[str] = ".", **overrides: Any) -> Settings:
    """Build settings from pyproject defaults, then explicit ``overrides`` (``None`` = not given)."""
    root_path = Path(root).resolve()
    known = {f.name for f in fields(Settings)} - {"root"}
    values = read_pyproject(root_path) if root_path.is_dir() else {}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ConfigError(f"unknown [tool.testteeth] key(s): {', '.join(unknown)}")
    for key, value in overrides.items():
        if key not in known:
            raise ConfigError(f"unknown setting {key!r}")
        if value is not None:
            values[key] = value
    _check_types(values)
    if isinstance(values.get("paths"), str):
        values["paths"] = [values["paths"]]
    if isinstance(values.get("test_args"), str):
        values["test_args"] = values["test_args"].split()
    if values.get("lang") in ("auto", ""):
        values["lang"] = None
    return Settings(root=root_path, **values).validate()
