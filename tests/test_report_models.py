from __future__ import annotations

import json

from testteeth import __version__
from testteeth.models import FunctionGrade, GradeReport
from testteeth.report import render_json, render_text


def test_scores_and_counts(sample_report):
    r = sample_report
    assert (r.total, r.killed, r.score) == (4, 2, 50.0)
    assert r.count("survived", "no_coverage") == 2
    assert not r.passed_gate
    assert [m.id for m in r.surviving()] == ["m#2", "m#3"]
    assert r.mutant("m#3").status == "no_coverage" and r.mutant("zzz") is None
    grade = r.functions[0]
    assert (grade.total, grade.killed, grade.survived, grade.score) == (4, 2, 2, 50.0)


def test_gate_edges(sample_report):
    sample_report.fail_under = 50.0
    assert sample_report.passed_gate
    sample_report.fail_under = None
    assert sample_report.passed_gate
    empty = GradeReport(root="/", base_ref=None, test_command=[], fail_under=90)
    assert empty.score is None and empty.passed_gate
    assert FunctionGrade("a", "f", 1).score is None


def test_json_round_trip(sample_report):
    data = json.loads(render_json(sample_report))
    assert data["tool"] == "testteeth" and data["version"] == __version__
    assert (data["score"], data["killed"], data["timeouts"], data["survived"], data["no_coverage"]) == (50.0, 1, 1, 1, 1)
    assert data["passed_gate"] is False
    assert data["functions"][0]["surviving_mutants"] == ["m#2", "m#3"]
    assert data["failure_path_gaps"][0]["kind"] == "raise"
    assert len(data["mutants"]) == 4


def test_text_report(sample_report):
    text = render_text(sample_report)
    assert "code changed vs main" in text
    assert "pkg/mod.py" in text and "WEAK" in text
    assert "[survived]" in text and "[not covered]" in text
    assert "failure path (raise)" in text
    assert "Mutation score: 50.0%  (FAIL: --fail-under 80)" in text
    assert "testteeth suggest" in text


def test_text_report_truncates_and_passes(sample_report, make_mutant):
    grade = sample_report.functions[0]
    extra = [make_mutant(id=f"x#{i}", status="survived", line=10 + i) for i in range(12)]
    grade.mutants.extend(extra)
    sample_report.mutants.extend(extra)
    sample_report.fail_under = 0
    text = render_text(sample_report, max_listed=3)
    assert "more surviving" in text and "PASS" in text


def test_text_report_all_killed_and_empty(make_mutant):
    m = make_mutant(status="killed")
    r = GradeReport(root="/", base_ref=None, test_command=[], mutants=[m], notes=["sampled"])
    r.functions = [FunctionGrade("pkg/mod.py", "f", 1, mutants=[m])]
    text = render_text(r)
    assert "all selected code" in text and " ok" in text and "note: sampled" in text
    assert "testteeth suggest" not in text
    empty = GradeReport(root="/", base_ref="main", test_command=[], notes=["no changed Python functions to grade"])
    assert "Nothing to grade" in render_text(empty)
    assert "no changed Python functions" in render_text(empty)


def test_mutant_properties(make_mutant):
    assert make_mutant(status="timeout").detected
    assert make_mutant(status="no_coverage").undetected
    assert not make_mutant(status="pending").detected and not make_mutant(status="pending").undetected
    assert make_mutant().to_dict()["operator"] == "comparison"
