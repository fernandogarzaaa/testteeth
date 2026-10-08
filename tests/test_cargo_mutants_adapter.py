"""Rust adapter (cargo-mutants), driven by a recorded outcomes.json of examples/rust-payments.

The fixture ``fixtures/cargo-mutants-weak-outcomes.json`` is a real cargo-mutants 27.1 ``outcomes.json`` for
examples/rust-payments graded with ``-- --test weak``; only absolute paths and timestamps were removed.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from conftest import git
from testteeth import cli
from testteeth.config import load_settings
from testteeth.engine import SuiteRun
from testteeth.errors import BaselineFailed, EngineCrashed, EngineNotInstalled, ReportError
from testteeth.languages import grade_project
from testteeth.languages import rust as rs
from testteeth.operators import matches_source
from testteeth.suggest import build_briefs, render_markdown

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = Path(__file__).parents[1] / "examples" / "rust-payments"


def outcomes() -> dict:
    return json.loads((FIXTURES / "cargo-mutants-weak-outcomes.json").read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "crate"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("target", "mutants.out*"))
    return root


class FakeCargoMutants:
    def __init__(self, data: dict | str | None = None, returncode: int = 2, output: str = "",
                 baseline_log: str = "") -> None:
        self.data = outcomes() if data is None else data
        self.returncode = returncode
        self.output = output
        self.baseline_log = baseline_log
        self.calls: list[list[str]] = []
        self.diffs: list[str] = []

    def __call__(self, cmd, cwd, env_extra=None):
        self.calls.append(list(cmd))
        out = Path(cmd[cmd.index("--output") + 1]) / "mutants.out"
        if "--in-diff" in cmd:
            self.diffs.append(Path(cmd[cmd.index("--in-diff") + 1]).read_text())
        out.mkdir(parents=True, exist_ok=True)
        if self.baseline_log:
            (out / "log").mkdir(exist_ok=True)
            (out / "log" / "baseline.log").write_text(self.baseline_log)
        if self.data != "":
            text = self.data if isinstance(self.data, str) else json.dumps(self.data)
            (out / "outcomes.json").write_text(text)
        return SuiteRun(self.returncode, 2.0, self.output)


@pytest.fixture
def fake(monkeypatch) -> FakeCargoMutants:
    runner = FakeCargoMutants()
    monkeypatch.setattr(rs, "run_engine", runner)
    monkeypatch.setattr(rs, "check_installed", lambda: "cargo")
    return runner


# ------------------------------------------------------------------------------------------- parsing
def test_parse_recorded_outcomes(project):
    mutants, excluded, baseline = rs.parse_outcomes(outcomes(), project)
    assert len(mutants) == 54 and excluded == 0 and baseline["summary"] == "Success"
    assert sum(m.status == "killed" for m in mutants) == 27
    assert sum(m.status == "survived" for m in mutants) == 27
    sources = {f: (project / f).read_text() for f in {m.file for m in mutants}}
    assert all(matches_source(sources[m.file], m) for m in mutants)
    funcs = {m.function for m in mutants}
    assert {"charge_with_retry", "Ledger::record", "Ledger::balance", "order_total", "shipping_fee", "round2"} == funcs
    ops = {(m.engine_operator, m.operator) for m in mutants}
    assert {("FnValue", "return-value"), ("BinaryOperator", "comparison"), ("BinaryOperator", "arithmetic")} <= ops
    fn_value = next(m for m in mutants if m.function == "order_total" and m.engine_operator == "FnValue")
    assert fn_value.exception == "Err" and fn_value.replacement.startswith("Ok(")
    cmp = next(m for m in mutants if m.function == "charge_with_retry" and m.line == 31 and m.replacement == "<=")
    assert cmp.original == "<" and cmp.status == "survived" and cmp.description.startswith("replace < with <=")


def test_unviable_mutants_are_excluded(project):
    data = outcomes()
    data["outcomes"][1]["summary"] = "Unviable"
    data["outcomes"][2]["summary"] = "Timeout"
    mutants, excluded, _ = rs.parse_outcomes(data, project)
    assert excluded == 1 and len(mutants) == 53 and mutants[0].status == "timeout"


@pytest.mark.parametrize(
    "mangle, message",
    [
        (lambda d: d.pop("outcomes"), "lacks 'outcomes'"),
        (lambda d: d.__setitem__("outcomes", {}), "must be a list"),
        (lambda d: d["outcomes"][1].pop("summary"), "lacks 'summary'"),
        (lambda d: d["outcomes"][1]["scenario"]["Mutant"].pop("span"), "lacks 'span'"),
        (lambda d: d["outcomes"][1]["scenario"]["Mutant"]["span"]["start"].pop("line"), "invalid span"),
        (lambda d: d["outcomes"][1].__setitem__("summary", "Exploded"), "unknown summary"),
        (lambda d: d["outcomes"][1].__setitem__("scenario", {"Other": 1}), "lacks 'Mutant'"),
    ],
)
def test_malformed_outcomes_raise(project, mangle, message):
    data = outcomes()
    mangle(data)
    with pytest.raises(ReportError, match=message):
        rs.parse_outcomes(data, project)


@pytest.mark.parametrize(
    "genre, original, expected",
    [
        ("FnValue", "{ body }", "return-value"),
        ("BinaryOperator", "<=", "comparison"),
        ("BinaryOperator", "&&", "boolean"),
        ("BinaryOperator", "+=", "arithmetic"),
        ("UnaryOperator", "!", "boolean"),
        ("UnaryOperator", "-", "arithmetic"),
        ("MatchArm", "Err(e) => x", "remove-branch"),
        ("MatchArmGuard", "x > 1", "condition"),
        ("StructField", "a: 1", "literal"),
        ("FutureGenre", "?", "future-genre"),
    ],
)
def test_operator_normalisation(genre, original, expected):
    assert rs.normalize_operator(genre, original, "") == expected


def test_test_target_names():
    assert rs.test_target_names(["--", "--test", "weak"]) == ["weak"]
    assert rs.test_target_names(["-v", "--", "--test=strong", "--test", "x"]) == ["x", "strong"]
    assert rs.test_target_names(["--", "--lib"]) is None
    assert rs.test_target_names([]) is None


# ------------------------------------------------------------------------------------------- adapter
def test_grade_with_recorded_outcomes(project, fake):
    report = grade_project(load_settings(project, engine_args=["--", "--test", "weak"], workers=2))
    assert (report.lang, report.engine, report.total, report.score) == ("rust", "cargo-mutants", 54, 50.0)
    cmd = fake.calls[0]
    assert cmd[:3] == ["cargo", "mutants", "--no-shuffle"] and cmd[-3:] == ["--", "--test", "weak"]
    assert cmd[cmd.index("--jobs") + 1] == "2" and "--in-diff" not in cmd
    kinds = {(g.function, g.kind) for g in report.gaps}
    assert {("charge_with_retry", "retry"), ("charge_with_retry", "match-err"), ("charge_with_retry", "timeout"),
            ("charge_with_retry", "external-call"), ("Ledger::record", "error-return"),
            ("order_total", "error-return")} <= kinds
    assert report.baseline_seconds > 0
    briefs = build_briefs(report, {f: (project / f).read_text() for f in {m.file for m in report.mutants}})
    assert len(briefs) == 27 and all(b.lang == "rust" for b in briefs)
    first = briefs[0]
    assert first.function == "charge_with_retry" and first.original_code == "if attempts < 1 {"
    assert first.test_skeleton.startswith("#[test]\nfn charge_with_retry_comparison_line_31()")
    md = render_markdown(briefs, report.gaps, limit=2)
    assert "```rust" in md and "mockall" in md


def test_strong_suite_text_silences_rust_gaps(project, fake):
    report = grade_project(load_settings(project, engine_args=["--", "--test", "strong"]))
    assert report.gaps == []  # the strong tests simulate every failure path (scores are from the canned report)


def test_base_ref_passes_a_relative_diff_with_untracked_files(project, fake):
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "init")
    ledger = project / "src" / "ledger.rs"
    ledger.write_text(ledger.read_text().replace("(sum * 100.0).round() / 100.0", "(sum * 100.0).round() / 100.0 + 0.0"))
    (project / "src" / "fresh.rs").write_text("pub fn f() -> u8 { 1 }\n")
    report = grade_project(load_settings(project, base_ref="HEAD"))
    diff = fake.diffs[0]
    assert "+++ b/src/ledger.rs" in diff and "+++ b/src/fresh.rs" in diff
    assert {g.function for g in report.gaps} <= {"Ledger::balance", "f"}


def test_base_ref_without_rust_changes_skips_the_engine(project, fake):
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "init")
    (project / "README.txt").write_text("docs only")
    report = grade_project(load_settings(project, base_ref="HEAD", lang="rust"))
    assert fake.calls == [] and report.notes[-1] == "no changed Rust functions to grade"


def test_explicit_paths_become_file_filters(project, fake):
    grade_project(load_settings(project, paths=["src/ledger.rs", "src"], lang="rs"))
    cmd = fake.calls[0]
    files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--file"]
    assert files == ["src/ledger.rs", "src/**"]


# ------------------------------------------------------------------------------------------- failures
def test_cargo_missing(monkeypatch, project):
    monkeypatch.setattr(rs, "find_cargo", lambda: None)
    with pytest.raises(EngineNotInstalled) as info:
        grade_project(load_settings(project))
    assert "rustup.rs" in info.value.hint and "cargo install --locked cargo-mutants" in info.value.hint


def test_cargo_mutants_missing(monkeypatch, project, tmp_path):
    fake_cargo = tmp_path / "cargo"
    fake_cargo.write_text("#!/bin/sh\necho 'error: no such command: `mutants`' >&2\nexit 101\n")
    fake_cargo.chmod(0o755)
    monkeypatch.setattr(rs, "find_cargo", lambda: str(fake_cargo))
    with pytest.raises(EngineNotInstalled, match="cargo-mutants is not installed"):
        grade_project(load_settings(project))


def test_baseline_failure_from_exit_code(monkeypatch, project):
    monkeypatch.setattr(rs, "run_engine", FakeCargoMutants(data="", returncode=4, baseline_log="test x ... FAILED"))
    monkeypatch.setattr(rs, "check_installed", lambda: "cargo")
    with pytest.raises(BaselineFailed) as info:
        grade_project(load_settings(project))
    assert "FAILED" in info.value.output


def test_baseline_failure_from_outcomes(monkeypatch, project):
    data = outcomes()
    data["outcomes"][0]["summary"] = "Failure"
    monkeypatch.setattr(rs, "run_engine", FakeCargoMutants(data=data, returncode=4))
    monkeypatch.setattr(rs, "check_installed", lambda: "cargo")
    with pytest.raises(BaselineFailed, match="baseline: Failure"):
        grade_project(load_settings(project))


def test_engine_crash(monkeypatch, project):
    monkeypatch.setattr(rs, "run_engine", FakeCargoMutants(data="", returncode=70, output="thread 'main' panicked"))
    monkeypatch.setattr(rs, "check_installed", lambda: "cargo")
    with pytest.raises(EngineCrashed, match="exited with code 70"):
        grade_project(load_settings(project))


def test_invalid_outcomes_json(monkeypatch, project):
    monkeypatch.setattr(rs, "run_engine", FakeCargoMutants(data="{oops"))
    monkeypatch.setattr(rs, "check_installed", lambda: "cargo")
    with pytest.raises(ReportError, match="invalid outcomes.json"):
        grade_project(load_settings(project))


def test_nothing_to_mutate(monkeypatch, project):
    monkeypatch.setattr(rs, "run_engine", FakeCargoMutants(data="", returncode=0))
    monkeypatch.setattr(rs, "check_installed", lambda: "cargo")
    report = grade_project(load_settings(project))
    assert report.total == 0 and "nothing to mutate" in report.notes[-1]


def test_unexpected_exit_code_with_outcomes_is_noted(monkeypatch, project):
    data = copy.deepcopy(outcomes())
    monkeypatch.setattr(rs, "run_engine", FakeCargoMutants(data=data, returncode=1))
    monkeypatch.setattr(rs, "check_installed", lambda: "cargo")
    report = grade_project(load_settings(project))
    assert report.total == 54 and any("exited with code 1" in n for n in report.notes)


def test_missing_cargo_toml(project, fake):
    (project / "Cargo.toml").unlink()
    with pytest.raises(EngineCrashed, match="no Cargo.toml"):
        grade_project(load_settings(project, lang="rust"))


def test_cli_rust_json_output(project, fake, monkeypatch, capsys):
    monkeypatch.chdir(project)
    assert cli.main(["run", "-q", "--engine-args", "-- --test weak", "--json", "-"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert (data["lang"], data["engine"], data["score"], data["excluded"]) == ("rust", "cargo-mutants", 50.0, 0)
    assert all(g["lang"] == "rust" for g in data["failure_path_gaps"])
