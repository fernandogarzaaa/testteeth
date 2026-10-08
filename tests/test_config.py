from __future__ import annotations

import sys

import pytest

from conftest import write
from testteeth.config import ConfigError, load_settings, read_pyproject


def test_defaults(tmp_path):
    s = load_settings(tmp_path)
    assert s.root == tmp_path.resolve()
    assert s.paths == ["."] and s.base_ref is None and s.coverage
    assert 1 <= s.workers <= 4
    assert s.test_command[:3] == [sys.executable, "-m", "pytest"]
    assert "-x" in s.test_command and "no:cacheprovider" in s.test_command


def test_pyproject_values_and_overrides(tmp_path):
    write(tmp_path, "pyproject.toml", """
        [tool.testteeth]
        paths = "src/pkg"
        test-args = "tests -m fast"
        fail_under = 75
        base_ref = "main"
    """)
    s = load_settings(tmp_path)
    assert s.paths == ["src/pkg"] and s.test_args == ["tests", "-m", "fast"]
    assert s.fail_under == 75 and s.base_ref == "main"
    assert s.test_command[-3:] == ["tests", "-m", "fast"]
    s2 = load_settings(tmp_path, fail_under=90, base_ref=None, paths=["lib"])
    assert s2.fail_under == 90 and s2.base_ref == "main" and s2.paths == ["lib"]


def test_no_tool_table(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    assert read_pyproject(tmp_path) == {}


@pytest.mark.parametrize(
    ("toml", "match"),
    [
        ("[tool.testteeth\n", "invalid TOML"),
        ("[tool]\ntesteeth = 1\ntestteeth = 3\n", "must be a table"),
        ("[tool.testteeth]\nbogus = 1\n", "unknown \\[tool.testteeth\\] key"),
        ("[tool.testteeth]\nfail_under = 101\n", "between 0 and 100"),
        ("[tool.testteeth]\nworkers = 0\n", "workers"),
        ("[tool.testteeth]\ntimeout_factor = 0\n", "positive"),
        ("[tool.testteeth]\nmax_mutants = 0\n", "max_mutants"),
        ("[tool.testteeth]\noperators = ['comparison', 'nope']\n", "unknown operator"),
    ],
)
def test_invalid_config(tmp_path, toml, match):
    write(tmp_path, "pyproject.toml", toml)
    with pytest.raises(ConfigError, match=match):
        load_settings(tmp_path)


def test_unknown_override_and_missing_root(tmp_path):
    with pytest.raises(ConfigError, match="unknown setting"):
        load_settings(tmp_path, colour="red")
    with pytest.raises(ConfigError, match="not a directory"):
        load_settings(tmp_path / "missing")


def test_fail_under_bounds_inclusive(tmp_path):
    assert load_settings(tmp_path, fail_under=0).fail_under == 0
    assert load_settings(tmp_path, fail_under=100).fail_under == 100
    with pytest.raises(ConfigError):
        load_settings(tmp_path, fail_under=-0.1)
