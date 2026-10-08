from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from testteeth.models import FailurePathGap, GradeReport, Mutant

pytest_plugins = ["pytester"]

#: Source matching the spans of the ``sample_report`` mutants.
SAMPLE_SRC = 'def f(a, b):\n    x = a < b\n    y = a < b\n    raise ValueError("x")\n    z = a < b\n'


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def make_mutant():
    def factory(**kw) -> Mutant:
        base = dict(
            id="pkg/mod.py#1", file="pkg/mod.py", function="f", function_line=1, line=2, col=8, end_line=2,
            end_col=13, operator="comparison", description="comparison `Lt` flipped to `LtE`",
            original="a < b", replacement="(a <= b)", status="survived",
        )
        base.update(kw)
        return Mutant(**base)

    return factory


@pytest.fixture
def sample_report(make_mutant) -> GradeReport:
    from testteeth.models import FunctionGrade

    killed = make_mutant(id="m#1", status="killed")
    survived = make_mutant(id="m#2", status="survived", line=3, end_line=3)
    uncovered = make_mutant(id="m#3", status="no_coverage", line=4, end_line=4, col=4, end_col=25, operator="remove-raise",
                            original='raise ValueError("x")', replacement="pass", description="removed `raise ValueError`")
    timeout = make_mutant(id="m#4", status="timeout", line=5, end_line=5)
    gap = FailurePathGap("pkg/mod.py", "f", 1, "raise", 4, "`raise ValueError('x')` is never triggered by any test")
    report = GradeReport(root="/proj", base_ref="main", test_command=["pytest"],
                         mutants=[killed, survived, uncovered, timeout], gaps=[gap], fail_under=80.0)
    grade = FunctionGrade("pkg/mod.py", "f", 1, mutants=list(report.mutants), gaps=[gap])
    report.functions = [grade]
    return report
