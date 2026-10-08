"""Settings, optionally read from ``[tool.testteeth]`` in the project's ``pyproject.toml``."""

from __future__ import annotations

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
            *self.test_args,
        ]


def read_pyproject(root: Path) -> dict[str, Any]:
    """Return the ``[tool.testteeth]`` table of ``root/pyproject.toml`` (empty if absent)."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    table = data.get("tool", {}).get("testteeth", {})
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [tool.testteeth] must be a table")
    return {key.replace("-", "_"): value for key, value in table.items()}


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
    if isinstance(values.get("paths"), str):
        values["paths"] = [values["paths"]]
    if isinstance(values.get("test_args"), str):
        values["test_args"] = values["test_args"].split()
    return Settings(root=root_path, **values).validate()
