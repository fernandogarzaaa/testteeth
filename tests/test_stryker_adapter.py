"""TypeScript/JavaScript adapter (StrykerJS), driven by a recorded Stryker report of examples/ts-payments.

The fixture ``fixtures/stryker-weak.json`` is a real StrykerJS 9 JSON report (mutation-testing-report-schema)
of examples/ts-payments graded with its weak suite; only the absolute paths were removed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import git
from testteeth import cli
from testteeth.config import ConfigError, load_settings
from testteeth.engine import SuiteRun
from testteeth.errors import BaselineFailed, EngineCrashed, EngineNotInstalled, ReportError
from testteeth.languages import grade_project
from testteeth.languages import typescript as ts
from testteeth.languages.native import SourceCache
from testteeth.operators import matches_source
from testteeth.suggest import build_briefs, render_markdown

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = Path(__file__).parents[1] / "examples" / "ts-payments"


def fixture_report() -> dict:
    return json.loads((FIXTURES / "stryker-weak.json").read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    for name in ("src", "tests_weak"):
        shutil.copytree(EXAMPLE / name, root / name)
    for name in ("package.json", "stryker.weak.json", "vitest.weak.config.ts"):
        shutil.copy(EXAMPLE / name, root / name)
    return root


class FakeStryker:
    """Stands in for `npx stryker run`: records the command and writes a canned report."""

    def __init__(self, report: dict | str | None = None, returncode: int = 0, output: str = "") -> None:
        self.report = fixture_report() if report is None else report
        self.returncode = returncode
        self.output = output
        self.calls: list[list[str]] = []
        self.configs: list[dict] = []

    def __call__(self, cmd, cwd, env_extra=None):
        self.calls.append(list(cmd))
        target = Path(cwd) / "reports" / "mutation" / "mutation.json"
        if cmd[4].endswith(".json") and "stryker.testteeth" in cmd[4]:
            config = json.loads(Path(cmd[4]).read_text(encoding="utf-8"))
            self.configs.append(config)
            target = Path(config["jsonReporter"]["fileName"])
        if self.report != "":
            target.parent.mkdir(parents=True, exist_ok=True)
            text = self.report if isinstance(self.report, str) else json.dumps(self.report)
            if self.report is not False:
                target.write_text(text, encoding="utf-8")
        return SuiteRun(self.returncode, 1.0, self.output)


@pytest.fixture
def fake(monkeypatch) -> FakeStryker:
    runner = FakeStryker()
    monkeypatch.setattr(ts, "run_engine", runner)
    monkeypatch.setattr(ts, "check_installed", lambda root: "npx")
    return runner


# ------------------------------------------------------------------------------------------- parsing
def test_parse_recorded_report(project):
    mutants, excluded, tests = ts.parse_report(fixture_report(), project)
    assert len(mutants) == 99 and excluded == 0
    counts = {s: sum(m.status == s for m in mutants) for s in ("killed", "survived", "no_coverage", "timeout")}
    assert counts == {"killed": 32, "survived": 22, "no_coverage": 45, "timeout": 0}
    assert list(tests) == ["tests_weak/payments.test.ts"]
    sources = {f: (project / f).read_text() for f in {m.file for m in mutants}}
    assert all(matches_source(sources[m.file], m) for m in mutants)  # spans line up with the real files
    funcs = {m.function for m in mutants}
    assert {"chargeWithRetry", "Ledger.record", "orderTotal", "shippingFee", "round2", "realSleep"} <= funcs
    ops = {m.operator for m in mutants}
    assert {"comparison", "condition", "remove-raise", "swallow-exception", "string", "arithmetic"} <= ops
    boundary = next(m for m in mutants if m.original == "total >= BULK_THRESHOLD" and m.replacement.startswith("total >"))
    assert boundary.status == "survived" and boundary.engine_operator == "EqualityOperator"
    swallow = next(m for m in mutants if m.operator == "swallow-exception")
    assert swallow.function == "chargeWithRetry" and swallow.context.startswith("catch")


def test_parse_uses_report_source_when_file_is_missing(tmp_path):
    mutants, _, tests = ts.parse_report(fixture_report(), tmp_path)
    assert len(mutants) == 99 and mutants[0].original
    assert "chargeWithRetry" in tests["tests_weak/payments.test.ts"]


def test_excluded_statuses_and_absolute_paths(project):
    data = fixture_report()
    first = data["files"]["src/pricing.ts"]["mutants"]
    first[0]["status"] = "CompileError"
    first[1]["status"] = "Ignored"
    data["files"] = {str(project / "src" / "pricing.ts"): data["files"]["src/pricing.ts"]}
    mutants, excluded, _ = ts.parse_report(data, project)
    assert excluded == 2 and {m.file for m in mutants} == {"src/pricing.ts"}


@pytest.mark.parametrize(
    "mangle, message",
    [
        (lambda d: d.pop("files"), "lacks 'files'"),
        (lambda d: d.__setitem__("files", []), "'files' must be an object"),
        (lambda d: d["files"]["src/pricing.ts"].pop("mutants"), "lacks 'mutants'"),
        (lambda d: d["files"]["src/pricing.ts"]["mutants"][0].pop("status"), "lacks 'status'"),
        (lambda d: d["files"]["src/pricing.ts"]["mutants"][0].__setitem__("status", "Weird"), "unknown status"),
        (lambda d: d["files"]["src/pricing.ts"]["mutants"][0]["location"]["start"].pop("line"), "invalid location"),
    ],
)
def test_malformed_reports_raise(project, mangle, message):
    data = fixture_report()
    mangle(data)
    with pytest.raises(ReportError, match=message):
        ts.parse_report(data, project)


def test_not_a_dict_report():
    with pytest.raises(ReportError):
        ts.parse_report([1, 2], Path("."))


@pytest.mark.parametrize(
    "mutator, original, prefix, expected",
    [
        ("BlockStatement", '{ throw new RangeError("x"); }', "if (a) ", ("remove-raise", "RangeError")),
        ("BlockStatement", "{ log(err); }", "} catch (err) ", ("swallow-exception", "the error")),
        ("BlockStatement", "{ a(); b(); }", "function f() ", ("remove-block", "")),
        ("EqualityOperator", "a < b", "", ("comparison", "")),
        ("ConditionalExpression", "a", "", ("condition", "")),
        ("SomeNewMutator", "x", "", ("some-new-mutator", "")),
    ],
)
def test_operator_normalisation(mutator, original, prefix, expected):
    assert ts.normalize_operator(mutator, original, prefix) == expected


# ------------------------------------------------------------------------------------------- adapter
def test_grade_with_recorded_report(project, fake):
    settings = load_settings(project, lang="ts", engine_config="stryker.weak.json")
    report = grade_project(settings)
    assert (report.lang, report.engine, report.total, report.score) == ("typescript", "stryker", 99, 32.3)
    config = fake.configs[0]
    assert config["testRunner"] == "vitest" and config["vitest"]["configFile"] == "vitest.weak.config.ts"
    assert sorted(config["mutate"]) == ["src/client.ts", "src/ledger.ts", "src/pricing.ts"]
    assert config["reporters"] == ["json"] and config["thresholds"]["break"] is None
    assert fake.calls[0][:4] == ["npx", "--no-install", "stryker", "run"]
    kinds = {(g.function, g.kind) for g in report.gaps}
    assert {("chargeWithRetry", "retry"), ("chargeWithRetry", "catch-branch"), ("chargeWithRetry", "external-call"),
            ("chargeWithRetry", "timeout"), ("orderTotal", "throw")} <= kinds
    grades = {g.function: g for g in report.functions}
    assert grades["round2"].score == 100.0 and grades["chargeWithRetry"].survived > 0
    briefs = build_briefs(report, {f: (project / f).read_text() for f in {m.file for m in report.mutants}})
    assert briefs and all(b.lang == "typescript" for b in briefs)
    assert briefs[0].failure_path  # failure paths first
    md = render_markdown(briefs, report.gaps, limit=3)
    assert "```ts" in md and "vitest" in md and "(heuristic)" in md


def test_base_ref_limits_mutate_to_changed_functions(project, fake):
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "init")
    path = project / "src" / "pricing.ts"
    path.write_text(path.read_text().replace("return round2(4.99 + 1.5 * extraKg);",
                                             "return round2(4.99 + 1.5 * extraKg + 0);"))
    report = grade_project(load_settings(project, base_ref="HEAD", engine_config="stryker.weak.json"))
    assert fake.configs[0]["mutate"] == ["src/pricing.ts:30-36"]  # the whole shippingFee function
    assert report.lang == "typescript"  # detected from package.json
    assert {g.function for g in report.gaps} <= {"shippingFee"}


def test_no_changes_skips_the_engine(project, fake):
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "init")
    report = grade_project(load_settings(project, base_ref="HEAD"))
    assert fake.calls == [] and "no changed TypeScript/JavaScript functions" in report.notes[-1]


def test_mutate_entries_merge_ranges(project):
    cache = SourceCache(project, "typescript")
    files = [project / "src" / "pricing.ts"]
    changed = {files[0].resolve(): frozenset({17, 18, 31, 2})}
    entries = ts.mutate_entries(project, files, changed, cache)
    assert entries["src/pricing.ts"][0] == ["src/pricing.ts:2-2", "src/pricing.ts:16-28", "src/pricing.ts:30-36"]


def test_js_config_is_passed_through_with_cli_overrides(project, fake):
    (project / "stryker.config.mjs").write_text("export default { testRunner: 'vitest' };\n")
    (project / "stryker.weak.json").unlink()
    report = grade_project(load_settings(project, lang="typescript", workers=2, coverage=False))
    cmd = fake.calls[0]
    assert cmd[4].endswith("stryker.config.mjs")
    assert cmd[cmd.index("--mutate") + 1].split(",") == ["src/client.ts", "src/ledger.ts", "src/pricing.ts"]
    assert cmd[cmd.index("--concurrency") + 1] == "2" and "--coverageAnalysis" in cmd
    assert report.total == 99


def test_python_only_options_are_noted(project, fake):
    report = grade_project(load_settings(project, lang="js", test_args=["-k", "x"], max_mutants=5))
    assert any("--pytest-args is ignored" in n for n in report.notes)
    assert any("--max-mutants" in n for n in report.notes)


def test_detect_runner(project):
    assert ts.detect_runner(project) == "vitest"
    (project / "package.json").write_text('{"devDependencies": {"@stryker-mutator/jest-runner": "9"}}')
    assert ts.detect_runner(project) == "jest"
    (project / "package.json").write_text("not json")
    assert ts.detect_runner(project) == "command"


def test_command_runner_default(project, fake):
    (project / "package.json").write_text("{}")
    (project / "stryker.weak.json").unlink()
    grade_project(load_settings(project, lang="ts"))
    assert fake.configs[0]["testRunner"] == "command"
    assert fake.configs[0]["commandRunner"] == {"command": "npm test"}


def test_invalid_user_config(project):
    (project / "stryker.config.json").write_text("{nope")
    with pytest.raises(ConfigError, match="invalid JSON"):
        ts.user_config(project, None)
    with pytest.raises(ConfigError, match="not found"):
        ts.user_config(project, "missing.json")
    (project / "stryker.config.json").write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        ts.user_config(project, None)


# ------------------------------------------------------------------------------------------- failures
def test_engine_missing_without_node(project, monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda name: None)
    with pytest.raises(EngineNotInstalled) as info:
        grade_project(load_settings(project, lang="ts"))
    assert "nodejs.org" in info.value.hint and "@stryker-mutator/core" in str(info.value)


def test_engine_missing_without_stryker(project, monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda name: "/usr/bin/npx")
    monkeypatch.setattr(ts, "find_stryker", lambda root: None)
    with pytest.raises(EngineNotInstalled, match="not installed"):
        grade_project(load_settings(project, lang="ts"))


def test_engine_crash_without_report(project, monkeypatch):
    monkeypatch.setattr(ts, "run_engine", FakeStryker(report="", returncode=1, output="Error: plugin exploded"))
    monkeypatch.setattr(ts, "check_installed", lambda root: "npx")
    with pytest.raises(EngineCrashed, match="exited with code 1") as info:
        grade_project(load_settings(project, lang="ts"))
    assert "plugin exploded" in info.value.output


def test_initial_test_run_failure_is_a_baseline_failure(project, monkeypatch):
    out = "ERROR DryRunExecutor There were failed tests in the initial test run:\n  payments > x"
    monkeypatch.setattr(ts, "run_engine", FakeStryker(report="", returncode=1, output=out))
    monkeypatch.setattr(ts, "check_installed", lambda root: "npx")
    with pytest.raises(BaselineFailed, match="initial test run"):
        grade_project(load_settings(project, lang="ts"))


def test_invalid_json_report(project, monkeypatch):
    monkeypatch.setattr(ts, "run_engine", FakeStryker(report="{truncated"))
    monkeypatch.setattr(ts, "check_installed", lambda root: "npx")
    with pytest.raises(ReportError, match="invalid JSON"):
        grade_project(load_settings(project, lang="ts"))


def test_no_report_and_exit_zero_means_nothing_to_mutate(project, monkeypatch):
    monkeypatch.setattr(ts, "run_engine", FakeStryker(report="", returncode=0))
    monkeypatch.setattr(ts, "check_installed", lambda root: "npx")
    report = grade_project(load_settings(project, lang="ts"))
    assert report.total == 0 and "no report" in report.notes[-1]


def test_nonzero_exit_with_report_is_noted(project, monkeypatch):
    monkeypatch.setattr(ts, "run_engine", FakeStryker(returncode=1))
    monkeypatch.setattr(ts, "check_installed", lambda root: "npx")
    report = grade_project(load_settings(project, lang="ts"))
    assert report.total == 99 and any("exited with code 1" in n for n in report.notes)


def test_cli_reports_engine_errors(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    monkeypatch.setattr(ts.shutil, "which", lambda name: None)
    assert cli.main(["run", "--lang", "ts"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "install hint" in err and "npm install --save-dev @stryker-mutator/core" in err


def test_cli_run_and_suggest_typescript(project, fake, monkeypatch, capsys):
    monkeypatch.chdir(project)
    assert cli.main(["run", "-q", "--engine-config", "stryker.weak.json", "--fail-under", "50"]) == cli.EXIT_GATE
    out = capsys.readouterr().out
    assert "[typescript via StrykerJS]" in out and "Mutation score: 32.3%" in out
    assert cli.main(["suggest", "-q", "--format", "json", "--limit", "1"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["briefs"][0]["lang"] == "typescript"
    assert "it(" in payload["briefs"][0]["test_skeleton"]
    assert payload["failure_path_gaps"][0]["heuristic"] is True
