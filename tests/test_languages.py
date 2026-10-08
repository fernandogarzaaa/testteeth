"""Language detection, --lang handling, dispatch, MCP `lang`, briefs per ecosystem, and (when the native engines
are installed) real end-to-end runs of the TypeScript and Rust examples."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from testteeth import cli, mcp_server
from testteeth.config import ConfigError, load_settings
from testteeth.languages import detect_language, get_adapter, grade_project, normalize_lang
from testteeth.models import GradeReport, Mutant
from testteeth.suggest import brief_for, gap_brief
from testteeth.models import FailurePathGap

ROOT = Path(__file__).parents[1]
#: CI's engines job sets this so a missing engine fails instead of silently skipping.
REQUIRE_ENGINES = bool(os.environ.get("TESTTEETH_REQUIRE_ENGINES"))


def touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")


@pytest.mark.parametrize(
    "files, expected",
    [
        (["Cargo.toml", "src/lib.rs"], "rust"),
        (["package.json", "src/index.ts"], "typescript"),
        (["pyproject.toml", "pkg/a.py"], "python"),
        (["setup.py"], "python"),
        (["a.py"], "python"),
        (["src/main.rs"], "rust"),
        (["index.js"], "typescript"),
        ([], "python"),
    ],
)
def test_detect_language(tmp_path, files, expected):
    touch(tmp_path, *files)
    assert detect_language(tmp_path) == (expected, None)


def test_detection_with_several_markers_counts_sources(tmp_path):
    touch(tmp_path, "pyproject.toml", "package.json", "a.py", "src/a.ts", "src/b.ts", "node_modules/x/y.py")
    lang, note = detect_language(tmp_path)
    assert lang == "typescript" and "also found python" in note and "--lang" in note


def test_detection_note_reaches_the_report(tmp_path, monkeypatch):
    touch(tmp_path, "Cargo.toml", "src/lib.rs", "src/main.rs", "setup.py")
    from testteeth.languages import rust

    monkeypatch.setattr(rust.CargoMutantsAdapter, "grade", lambda self, s, p=None: GradeReport(str(tmp_path), None, []))
    report = grade_project(load_settings(tmp_path))
    assert report.notes and report.notes[0].startswith("detected rust")


@pytest.mark.parametrize("alias, lang", [("ts", "typescript"), ("JS", "typescript"), ("javascript", "typescript"),
                                         ("rs", "rust"), ("Python", "python"), ("py", "python")])
def test_aliases(alias, lang):
    assert normalize_lang(alias) == lang
    assert get_adapter(alias).lang == lang


def test_unknown_lang_is_a_config_error(tmp_path, capsys, monkeypatch):
    with pytest.raises(ConfigError, match="unknown language 'go'"):
        load_settings(tmp_path, lang="go")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "--lang", "go"]) == cli.EXIT_USAGE
    assert "supported: python, typescript" in capsys.readouterr().err


def test_lang_from_testteeth_toml(tmp_path):
    (tmp_path / "testteeth.toml").write_text('lang = "ts"\nengine_args = ["--logLevel", "warn"]\nfail_under = 70\n')
    settings = load_settings(tmp_path)
    assert (settings.lang, settings.engine_args, settings.fail_under) == ("typescript", ["--logLevel", "warn"], 70)
    (tmp_path / "testteeth.toml").write_text('[tool.testteeth]\nlang = "auto"\n')
    assert load_settings(tmp_path).lang is None
    (tmp_path / "testteeth.toml").write_text("lang = ")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_settings(tmp_path)


def test_cli_passes_lang_and_engine_options(tmp_path, monkeypatch):
    seen = {}

    def fake(settings, progress=None):
        seen["s"] = settings
        return GradeReport(str(tmp_path), None, [])

    monkeypatch.setattr(cli, "grade", fake)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "-q", "--lang", "rs", "--engine-args", "-- --test weak", "--engine-config", "s.json"]) == 0
    s = seen["s"]
    assert (s.lang, s.engine_args, s.engine_config) == ("rust", ["--", "--test", "weak"], "s.json")
    assert cli.main(["run", "-q", "--lang", "auto"]) == 0 and seen["s"].lang is None


def test_mcp_tools_accept_lang(tmp_path, monkeypatch):
    seen = []

    def fake(settings, progress=None):
        seen.append(settings)
        return GradeReport(str(tmp_path), None, [], lang=settings.lang or "python")

    monkeypatch.setattr(mcp_server, "grade", fake)
    mcp_server._cache.clear()
    assert "error" not in mcp_server.grade_tests(str(tmp_path), lang="typescript", engine_args="--logLevel warn")
    assert mcp_server.list_surviving_mutants(str(tmp_path), lang="typescript", engine_args="--logLevel warn")["lang"] \
        == "typescript"
    assert len(seen) == 1  # cached per language
    assert mcp_server.suggest_missing_tests(str(tmp_path), lang="rust")["lang"] == "rust"
    assert [s.lang for s in seen] == ["typescript", "rust"]
    bad = mcp_server.grade_tests(str(tmp_path), lang="cobol")
    assert "unknown language" in bad["error"]


def test_mcp_reports_install_hint(tmp_path, monkeypatch):
    from testteeth.errors import EngineNotInstalled

    def boom(settings, progress=None):
        raise EngineNotInstalled("cargo-mutants is not installed", "cargo install --locked cargo-mutants")

    monkeypatch.setattr(mcp_server, "grade", boom)
    data = mcp_server.grade_tests(str(tmp_path), lang="rust")
    assert data["install_hint"] == "cargo install --locked cargo-mutants"


# ------------------------------------------------------------------------------------------- briefs
def _mutant(**kw) -> Mutant:
    base = dict(id="m#1", file="src/a.ts", function="chargeWithRetry", function_line=1, line=2, col=2, end_line=2,
                end_col=24, operator="remove-raise", description="BlockStatement", original="{ throw new X(); }",
                replacement="{}", status="survived", exception="X")
    base.update(kw)
    return Mutant(**base)


def test_typescript_brief_uses_vitest_idioms():
    source = "export async function chargeWithRetry() {\n  { throw new X(); }\n}\n"
    brief = brief_for(_mutant(), source, "typescript")
    assert "toThrow(X)" in brief.must_assert and "rejects" in brief.must_assert
    assert brief.failure_path and brief.lang == "typescript"
    assert 'it("chargeWithRetry kills the remove-raise mutant at line 2"' in brief.test_skeleton
    assert "mockRejectedValueOnce" in brief.test_skeleton and "await expect" in brief.test_skeleton


def test_typescript_swallow_brief_mentions_vi_fn():
    source = "function f() {\n  try { g(); } catch (e) { log(e); }\n}\n"
    m = _mutant(function="f", operator="swallow-exception", original="{ log(e); }", col=26, end_col=37,
                context="catch (e)", exception="the error")
    brief = brief_for(m, source, "typescript")
    assert "vi.fn().mockRejectedValueOnce" in brief.must_assert and brief.failure_path
    assert "expect(() => f(" in brief.test_skeleton  # sync function: toThrow form


def test_rust_brief_uses_test_attributes_and_mockall():
    source = "pub async fn fetch(c: &Client) -> Result<u8, E> {\n    c.get().await\n}\n"
    m = _mutant(file="src/lib.rs", function="fetch", operator="return-value", original="c.get().await", col=4,
                end_col=17, replacement="Ok(0)", exception="Err", description="replace fetch with Ok(0)")
    brief = brief_for(m, source, "rust")
    assert brief.suggested_test_name == "fetch_return_value_line_2"
    assert "#[tokio::test]" in brief.test_skeleton and "async fn fetch_return_value_line_2" in brief.test_skeleton
    assert "mockall" in brief.test_skeleton and ".await" in brief.test_skeleton
    assert "`Err` paths" in brief.why_missed and "Err(..)" in brief.must_assert


def test_rust_operator_token_is_shown_in_its_line():
    source = "fn f(a: u8) -> bool {\n    a < 1\n}\n"
    m = _mutant(file="src/lib.rs", function="f", operator="comparison", original="<", replacement="<=", col=6,
                end_col=7, description="replace < with <=")
    brief = brief_for(m, source, "rust")
    assert "`<` in `a < 1`" in brief.why_missed and "boundary" in brief.why_missed
    assert brief.mutated_code == "a <= 1" and "#[test]" in brief.test_skeleton


@pytest.mark.parametrize("op", ["condition", "remove-block", "string", "literal", "method", "remove-branch"])
def test_native_only_operators_have_specific_briefs(op):
    brief = brief_for(_mutant(operator=op, original="a", replacement="b", col=2, end_col=3),
                      "function chargeWithRetry() {\n  a;\n}\n", "typescript")
    assert "was not detected" not in brief.why_missed


def test_gap_guidance_per_language():
    assert "useFakeTimers" in gap_brief(FailurePathGap("a.ts", "f", 1, "timeout", 2, "d", lang="typescript"))["must_assert"]
    assert "should_panic" in gap_brief(FailurePathGap("a.rs", "f", 1, "unwrap", 2, "d", lang="rust"))["must_assert"]
    assert "pytest.raises" in gap_brief(FailurePathGap("a.py", "f", 1, "raise", 2, "d"))["must_assert"]
    assert gap_brief(FailurePathGap("a.rs", "f", 1, "new-kind", 2, "d", lang="rust"))["heuristic"] is True


# ------------------------------------------------------------------------------------------- real engines
def _has_stryker() -> bool:
    return shutil.which("npx") is not None and (ROOT / "examples/ts-payments/node_modules/@stryker-mutator/core").is_dir()


def _has_cargo_mutants() -> bool:
    from testteeth.languages.rust import find_cargo

    cargo = find_cargo()
    if not cargo:
        return False
    return subprocess.run([cargo, "mutants", "--version"], capture_output=True).returncode == 0


@pytest.mark.slow
@pytest.mark.engines
@pytest.mark.skipif(not REQUIRE_ENGINES and not _has_stryker(), reason="StrykerJS not installed in examples/ts-payments (npm ci)")
@pytest.mark.parametrize("suite, score", [("weak", 32.3), ("strong", 100.0)])
def test_real_stryker_on_ts_example(suite, score, capsys):
    code = cli.main(["run", "-q", "--root", str(ROOT / "examples/ts-payments"), "--engine-config",
                     f"stryker.{suite}.json", "--json", "-"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["lang"] == "typescript" and data["score"] == score
    assert (len(data["failure_path_gaps"]) > 0) == (suite == "weak")


@pytest.mark.slow
@pytest.mark.engines
@pytest.mark.skipif(not REQUIRE_ENGINES and not _has_cargo_mutants(), reason="cargo-mutants not installed")
@pytest.mark.parametrize("suite, score", [("weak", 50.0), ("strong", 100.0)])
def test_real_cargo_mutants_on_rust_example(suite, score, capsys):
    code = cli.main(["run", "-q", "--root", str(ROOT / "examples/rust-payments"), "--engine-args",
                     f"-- --test {suite}", "--json", "-"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["lang"] == "rust" and data["score"] == score
    assert (len(data["failure_path_gaps"]) > 0) == (suite == "weak")
