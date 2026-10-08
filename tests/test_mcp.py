from __future__ import annotations

import asyncio

import pytest

from conftest import SAMPLE_SRC, write
from testteeth import mcp_server
from testteeth.engine import BaselineFailed


@pytest.fixture(autouse=True)
def clear_cache():
    mcp_server._cache.clear()
    yield
    mcp_server._cache.clear()


@pytest.fixture
def project(tmp_path, sample_report):
    write(tmp_path, "pkg/mod.py", SAMPLE_SRC)
    for m in sample_report.mutants:
        m.file = "pkg/mod.py"
    return tmp_path


@pytest.fixture
def counted(monkeypatch, sample_report):
    calls = []

    def fake(settings, progress=None):
        calls.append(settings)
        return sample_report

    monkeypatch.setattr(mcp_server, "grade", fake)
    return calls


def test_grade_tests_summary(project, counted):
    data = mcp_server.grade_tests(str(project), base_ref="main", pytest_args="tests -q", paths=["pkg"], max_mutants=5)
    assert data["score"] == 50.0 and data["surviving_mutant_count"] == 2
    assert "mutants" not in data and "surviving_mutants" not in data["functions"][0]
    assert "suggest_missing_tests" in data["next_step"]
    s = counted[0]
    assert (s.base_ref, s.test_args, s.paths, s.max_mutants) == ("main", ["tests", "-q"], ["pkg"], 5)


def test_list_and_suggest_reuse_cached_grade(project, counted):
    mcp_server.grade_tests(str(project))
    listed = mcp_server.list_surviving_mutants(str(project))
    assert listed["count"] == 2 and [m["id"] for m in listed["mutants"]] == ["m#2", "m#3"]
    suggested = mcp_server.suggest_missing_tests(str(project), limit=1)
    assert len(counted) == 1  # both reused the cached run
    assert suggested["total_briefs"] == 2 and len(suggested["briefs"]) == 1
    assert suggested["briefs"][0]["failure_path"] is True
    assert suggested["markdown"].startswith("# Missing tests")
    assert suggested["failure_path_gaps"][0]["must_assert"]
    mcp_server.list_surviving_mutants(str(project), refresh=True)
    assert len(counted) == 2


def test_list_runs_grade_when_cache_empty(project, counted):
    assert mcp_server.list_surviving_mutants(str(project))["score"] == 50.0
    assert len(counted) == 1


def test_all_killed_next_step(project, monkeypatch, sample_report):
    for m in sample_report.mutants:
        m.status = "killed"
    sample_report.gaps = []
    monkeypatch.setattr(mcp_server, "grade", lambda s, progress=None: sample_report)
    assert mcp_server.grade_tests(str(project))["next_step"].startswith("All mutants killed")


def test_errors_are_returned_not_raised(project, monkeypatch, tmp_path):
    def red(settings, progress=None):
        raise BaselineFailed("suite fails", "E   assert 1 == 2\nFAILED t.py")

    monkeypatch.setattr(mcp_server, "grade", red)
    for tool in (mcp_server.grade_tests, mcp_server.list_surviving_mutants, mcp_server.suggest_missing_tests):
        data = tool(str(project))
        assert data["error"] == "BaselineFailed: suite fails"
        assert "FAILED t.py" in data["test_output_tail"] and "pass first" in data["hint"]
    missing = mcp_server.grade_tests(str(tmp_path / "nope"))
    assert missing["error"].startswith("ConfigError") and "test_output_tail" not in missing


def test_server_registers_tools():
    server = mcp_server.build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"grade_tests", "list_surviving_mutants", "suggest_missing_tests"}
    grade_tool = next(t for t in tools if t.name == "grade_tests")
    schema = grade_tool.inputSchema if hasattr(grade_tool, "inputSchema") else grade_tool.input_schema
    assert {"path", "base_ref", "pytest_args", "paths", "max_mutants"} <= set(schema["properties"])
    assert "mutation testing" in (grade_tool.description or "")
