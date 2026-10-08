from __future__ import annotations

import json

import pytest

from conftest import SAMPLE_SRC, write
from testteeth import cli
from testteeth.diff import GitError
from testteeth.engine import BaselineFailed, EngineError
from testteeth.models import GradeReport




@pytest.fixture
def project(tmp_path, monkeypatch):
    write(tmp_path, "pkg/mod.py", SAMPLE_SRC)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake_grade(monkeypatch, sample_report):
    calls = {}

    def fake(settings, progress=None):
        calls["settings"] = settings
        sample_report.fail_under = settings.fail_under
        if progress:
            for i, m in enumerate(sample_report.mutants, start=1):
                progress(i, len(sample_report.mutants), m)
        return sample_report

    monkeypatch.setattr(cli, "grade", fake)
    return calls


def test_run_text_and_gate_failure(project, fake_grade, capsys):
    code = cli.main(["run", "pkg", "--fail-under", "80", "--base", "main", "--workers", "2",
                     "--pytest-args", "tests -m 'not slow'", "--operators", "comparison, constant",
                     "--max-mutants", "9", "--timeout-factor", "2", "--no-coverage"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_GATE
    assert "Mutation score: 50.0%  (FAIL: --fail-under 80)" in out
    assert err.strip() == ".S-T 4 mutants"
    s = fake_grade["settings"]
    assert s.paths == [str(project / "pkg")] and s.base_ref == "main" and s.workers == 2
    assert s.test_args == ["tests", "-m", "not slow"] and s.operators == ["comparison", "constant"]
    assert (s.max_mutants, s.timeout_factor, s.coverage) == (9, 2.0, False)


def test_run_passes_gate_and_writes_json(project, fake_grade, capsys):
    code = cli.main(["run", "-q", "--fail-under", "50", "--json", "out.json"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK and err == ""
    assert json.loads((project / "out.json").read_text())["score"] == 50.0
    assert "Mutation score" in out


def test_run_json_to_stdout(project, fake_grade, capsys):
    assert cli.main(["run", "-q", "--json", "-"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["total"] == 4


def test_suggest_markdown_and_json(project, fake_grade, capsys, sample_report):
    for m in sample_report.mutants:
        m.file = "pkg/mod.py"
    assert cli.main(["suggest", "-q"]) == cli.EXIT_OK
    md = capsys.readouterr().out
    assert md.startswith("# Missing tests") and "pkg/mod.py:4" in md
    assert cli.main(["suggest", "-q", "--format", "json", "--limit", "1"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["score"] == 50.0 and len(data["briefs"]) == 1
    assert data["briefs"][0]["operator"] == "remove-raise"  # failure paths first
    assert data["failure_path_gaps"][0]["must_assert"]


@pytest.mark.parametrize(
    ("exc", "code", "message"),
    [
        (BaselineFailed("suite is red", "line1\nFAILED test_x"), cli.EXIT_ERROR, "FAILED test_x"),
        (EngineError("path not found: x"), cli.EXIT_ERROR, "path not found"),
        (GitError("unknown base ref 'z'"), cli.EXIT_ERROR, "unknown base ref"),
    ],
)
def test_errors_map_to_exit_codes(project, monkeypatch, capsys, exc, code, message):
    def boom(settings, progress=None):
        raise exc

    monkeypatch.setattr(cli, "grade", boom)
    assert cli.main(["run"]) == code
    assert message in capsys.readouterr().err


def test_config_error_is_usage_error(project, capsys):
    write(project, "pyproject.toml", "[tool.testteeth]\nworkers = 0\n")
    assert cli.main(["run"]) == cli.EXIT_USAGE
    assert "configuration error" in capsys.readouterr().err


def test_empty_report_passes(project, monkeypatch, capsys):
    monkeypatch.setattr(cli, "grade", lambda s, progress=None: GradeReport(str(project), None, [], notes=["no Python source files found"]))
    assert cli.main(["run", "--fail-under", "99"]) == cli.EXIT_OK
    assert "Nothing to grade" in capsys.readouterr().out


def test_parser_requires_command_and_has_version(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main([])
    assert info.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "testteeth 0." in capsys.readouterr().out


def test_mcp_subcommand_dispatches(monkeypatch):
    from testteeth import mcp_server

    called = []
    monkeypatch.setattr(mcp_server, "main", lambda: called.append(True))
    assert cli.main(["mcp"]) == cli.EXIT_OK and called == [True]
