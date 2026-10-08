"""Engine tests. A fake test runner keeps these fast; tests marked `slow` run real pytest."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from conftest import git, write
from testteeth import engine
from testteeth.config import load_settings
from testteeth.engine import (
    BaselineFailed,
    EngineError,
    SuiteRun,
    discover_sources,
    discover_tests,
    grade,
    is_test_file,
    run_tests,
    select,
    resolve_test_paths,
)

CALC = """
def clamp(x, lo=0, hi=10):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
"""


@pytest.fixture
def project(tmp_path):
    write(tmp_path, "pkg/__init__.py", "")
    write(tmp_path, "pkg/calc.py", CALC)
    write(tmp_path, "tests/test_calc.py", "from pkg.calc import clamp\n\ndef test_clamp():\n    assert clamp(5) == 5\n")
    return tmp_path


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("pkg/mod.py", False), ("test_mod.py", True), ("pkg/mod_test.py", True), ("conftest.py", True),
        ("tests/helpers.py", True), ("pkg/test/util.py", True), ("tests_weak/x.py", True),
        ("testing/x.py", True), ("pkg/contest.py", False), ("latest/x.py", False),
    ],
)
def test_is_test_file(rel, expected):
    assert is_test_file(Path(rel)) is expected


def test_discover_sources_skips_tests_and_excluded_dirs(project):
    write(project, ".venv/lib/x.py", "x = 1\n")
    write(project, "build/gen.py", "x = 1\n")
    write(project, "pkg/gen_pb2.py", "x = 1\n")
    write(project, "pkg/data.txt", "nope\n")
    settings = load_settings(project, exclude=["*_pb2.py"])
    found = [p.relative_to(project).as_posix() for p in discover_sources(settings)]
    assert found == ["pkg/__init__.py", "pkg/calc.py"]


def test_discover_sources_explicit_file_and_dedup(project):
    settings = load_settings(project, paths=["pkg/calc.py", "pkg", "pkg/data.txt"])
    write(project, "pkg/data.txt", "x")
    found = [p.relative_to(project).as_posix() for p in discover_sources(settings)]
    assert found == ["pkg/calc.py", "pkg/__init__.py"]


def test_discover_sources_errors(project, tmp_path_factory):
    with pytest.raises(EngineError, match="path not found"):
        discover_sources(load_settings(project, paths=["nope"]))
    outside = tmp_path_factory.mktemp("outside")
    with pytest.raises(EngineError, match="outside the project root"):
        discover_sources(load_settings(project, paths=[str(outside)]))


def test_test_roots_and_discover_tests(project):
    write(project, "tests_other/test_o.py", "def test_o():\n    pass\n")
    write(project, "tests/conftest.py", "X = 1\n")
    assert resolve_test_paths(project, ["-m", "fast", "tests::test_calc", "missing"]) == [(project / "tests").resolve()]
    assert resolve_test_paths(project, ["-q"]) == [project]
    only = discover_tests(project, [], ["tests"])
    assert len(only) == 1 and "test_clamp" in only[0]
    assert len(discover_tests(project, [], [])) == 2


def test_select_all_code(project):
    sel = select(load_settings(project))
    assert list(sel.sources) == ["pkg/calc.py"]
    assert sel.functions["pkg/calc.py"] == {"clamp"}
    assert sel.module_lines["pkg/calc.py"] is None
    assert {m.operator for m in sel.mutants} >= {"comparison", "constant", "return-value"}


def test_select_reports_unparsable_files(project):
    write(project, "pkg/broken.py", "def (:\n")
    sel = select(load_settings(project))
    assert sel.notes == ["skipped pkg/broken.py: SyntaxError"]


def test_select_samples_evenly(project):
    full = select(load_settings(project)).mutants
    sel = select(load_settings(project, max_mutants=3))
    assert len(sel.mutants) == 3
    assert sel.mutants[0].id == full[0].id
    assert sel.notes == [f"sampled 3 of {len(full)} mutants (--max-mutants)"]


def test_select_git_diff_aware(project):
    write(project, "pkg/other.py", "LIMIT = 5\n\ndef a(x):\n    return x + 1\n\n\ndef b(x):\n    return x - 1\n")
    git(project, "init", "-q", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "init")
    write(project, "pkg/other.py", "LIMIT = 6\n\ndef a(x):\n    return x + 1\n\n\ndef b(x):\n    return x - 2\n")
    write(project, "pkg/fresh.py", "def c(x):\n    return x * 3\n")
    sel = select(load_settings(project, base_ref="main"))
    assert set(sel.sources) == {"pkg/other.py", "pkg/fresh.py"}
    assert sel.functions["pkg/other.py"] == {"b"}
    assert sel.module_lines["pkg/other.py"] == frozenset({1, 8})
    assert {(m.function, m.original) for m in sel.mutants if m.file == "pkg/other.py"} == {
        ("<module>", "6"), ("b", "x - 2"), ("b", "2"), ("b", "return x - 2"),
    }
    assert sel.module_lines["pkg/fresh.py"] is None


def test_grade_with_nothing_changed(project):
    git(project, "init", "-q", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "init")
    report = grade(load_settings(project, base_ref="main"))
    assert report.total == 0 and report.score is None
    assert report.notes == ["no changed Python functions to grade"]


def test_grade_without_sources(tmp_path):
    report = grade(load_settings(tmp_path))
    assert report.notes == ["no Python source files found"] and report.passed_gate


# ----------------------------------------------------------------- fake runner
class FakeRunner:
    """Decides each mutant's fate from the mutated source, and checks the workspace is restored."""

    def __init__(self, project: Path, baseline_rc: int = 0, covered: list[int] | None = None):
        self.project = project
        self.baseline_rc = baseline_rc
        self.covered = covered
        self.calls: list[str] = []

    def __call__(self, cmd, cwd, env, timeout):
        source = (Path(cwd) / "pkg/calc.py").read_text()
        assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["TESTTEETH_ACTIVE"] == "1"
        assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(Path(cwd) / "src"), str(cwd)]
        if timeout is None:  # baseline
            assert "testteeth._tracer" in cmd
            out = env.get("TESTTEETH_TRACE_OUT")
            if out and self.covered is not None:
                Path(out).write_text(json.dumps({str(Path(cwd) / "pkg/calc.py"): self.covered, "/other.py": [1]}))
            return SuiteRun(self.baseline_rc, 0.25, "FAILED tests/test_calc.py::test_clamp - boom\n")
        assert timeout >= 10
        self.calls.append(source)
        if "x <= lo" in source:
            return SuiteRun(None, timeout, "")
        if "return None" in source or "x >= hi" in source:
            return SuiteRun(1, 0.1, "....\nFAILED tests/test_calc.py::test_clamp - assert None == 5\n")
        return SuiteRun(0, 0.1, "1 passed")


def test_grade_classifies_mutants(project, monkeypatch):
    fake = FakeRunner(project, covered=[1, 2, 4, 6])
    monkeypatch.setattr(engine, "run_tests", fake)
    seen = []
    report = grade(load_settings(project, workers=2, fail_under=10), progress=lambda d, t, m: seen.append((d, t)))
    by = {m.original + "|" + m.replacement: m for m in report.mutants}
    assert by["x < lo|(x <= lo)"].status == "timeout"
    assert by["x > hi|(x >= hi)"].status == "killed"
    assert by["x > hi|(x >= hi)"].detail.startswith("FAILED tests/test_calc.py")
    assert by["return x|return None"].status == "killed"
    assert by["return lo|return None"].status == "no_coverage"  # line 3 never executed
    assert by["0|1"].status == "survived" and by["0|1"].detail == "all tests passed"
    assert report.baseline_seconds == 0.25
    assert [d for d, _ in seen] == list(range(1, report.total + 1))
    assert all(t == report.total for _, t in seen)
    assert len(fake.calls) == report.total - report.count("no_coverage")
    assert all(src != CALC.lstrip("\n") for src in fake.calls)  # every run saw a mutated file
    assert (project / "pkg/calc.py").read_text() == CALC.lstrip("\n")  # original untouched
    assert [f.function for f in report.functions] == ["clamp"]
    assert {g.kind for g in report.gaps} == set()
    assert report.score == round(100 * report.killed / report.total, 1)
    assert report.passed_gate


def test_grade_without_coverage_runs_every_mutant(project, monkeypatch):
    def runner(cmd, cwd, env, timeout):
        assert "testteeth._tracer" not in cmd
        return SuiteRun(0, 0.1, "")

    monkeypatch.setattr(engine, "run_tests", runner)
    report = grade(load_settings(project, coverage=False))
    assert report.count("no_coverage") == 0 and report.count("survived") == report.total
    assert report.score == 0.0


@pytest.mark.parametrize(("rc", "match"), [(1, "fails before any mutation"), (5, "collected no tests")])
def test_baseline_failure_is_an_error(project, monkeypatch, rc, match):
    monkeypatch.setattr(engine, "run_tests", FakeRunner(project, baseline_rc=rc))
    with pytest.raises(BaselineFailed, match=match) as info:
        grade(load_settings(project))
    assert "boom" in info.value.output


def test_first_failure_extraction():
    assert engine._first_failure("x\nERROR tests/a.py - ImportError\n") == "ERROR tests/a.py - ImportError"
    assert engine._first_failure("a\nb\nlast line\n") == "last line"


def test_run_tests_success_and_timeout(tmp_path):
    ok = run_tests([sys.executable, "-c", "print('hi')"], tmp_path, dict(os.environ), timeout=30)
    assert ok.returncode == 0 and ok.output.strip() == "hi"
    bad = run_tests([sys.executable, "-c", "raise SystemExit(3)"], tmp_path, dict(os.environ), timeout=30)
    assert bad.returncode == 3
    slow = run_tests([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, dict(os.environ), timeout=0.5)
    assert slow.returncode is None and slow.seconds < 10


def test_env_preserves_existing_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/already")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--pdb")
    env = engine._env(tmp_path, {"EXTRA": "1"})
    assert env["PYTHONPATH"].split(os.pathsep) == [str(tmp_path / "src"), str(tmp_path), "/already"]
    assert "PYTEST_ADDOPTS" not in env and env["EXTRA"] == "1"


def test_workspace_copy_ignores_junk(project, tmp_path_factory):
    write(project, ".git/config", "x")
    write(project, "pkg/__pycache__/calc.cpython-312.pyc", "x")
    dest = engine._make_workspace(project, tmp_path_factory.mktemp("ws"), 3)
    assert dest.name == "w3" and (dest / "pkg/calc.py").is_file()
    assert not (dest / ".git").exists() and not (dest / "pkg/__pycache__").exists()
