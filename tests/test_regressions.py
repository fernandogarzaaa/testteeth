"""Regression tests for bugs found while auditing 0.2.0."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import git, write
from testteeth import config as config_mod
from testteeth.config import ConfigError, load_settings
from testteeth.diff import changed_lines, diff_with_untracked
from testteeth.engine import grade
from testteeth.models import GradeReport
from testteeth.operators import generate_mutants
from testteeth.suggest import read_sources

PROJECT = {
    "calc.py": """
        def clamp(x, low, high):
            if x < low:
                return low
            if x > high:
                return high
            return x
        """,
    "test_calc.py": """
        from calc import clamp

        def test_clamp():
            assert clamp(5, 0, 10) == 5
            assert clamp(-1, 0, 10) == 0
            assert clamp(11, 0, 10) == 10
            assert clamp(0, 0, 10) == 0
            assert clamp(10, 0, 10) == 10
        """,
}


def make_project(root: Path, files: dict[str, str] = PROJECT) -> Path:
    for rel, text in files.items():
        write(root, rel, text)
    return root


# ---------------------------------------------------------------- coverage next to other pytest plugins
def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


@pytest.mark.slow
@pytest.mark.parametrize(
    ("plugin", "args"),
    [("pytest_cov", ["--cov=calc"]), ("xdist", ["-n", "2"])],
    ids=["pytest-cov", "pytest-xdist"],
)
def test_coverage_survives_other_plugins(tmp_path, plugin, args):
    """pytest-cov used to replace the tracer and xdist workers were never traced: every mutant was reported
    `not covered` and a strong suite scored 0%."""
    if not _has(plugin):
        pytest.skip(f"{plugin} is not installed")
    report = grade(load_settings(make_project(tmp_path), paths=["calc.py"], test_args=args))
    assert report.total > 0
    assert report.count("no_coverage") == 0
    assert report.killed >= 3  # only the two equivalent boundary mutants of `clamp` survive


def test_no_cov_is_passed_only_when_pytest_cov_would_run(monkeypatch):
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    monkeypatch.setattr(config_mod.importlib.util, "find_spec", lambda name: object())
    assert config_mod._no_cov_args(["tests"]) == ["--no-cov"]
    assert config_mod._no_cov_args(["tests", "--no-cov"]) == []
    assert config_mod._no_cov_args(["-p", "no:cov"]) == []
    assert config_mod._no_cov_args(["tests", "--disable-plugin-autoload"]) == []
    assert config_mod._no_cov_args(["--disable-plugin-autoload", "-p", "pytest_cov"]) == ["--no-cov"]
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    assert config_mod._no_cov_args(["tests"]) == []
    assert config_mod._no_cov_args(["-ppytest_cov"]) == ["--no-cov"]
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
    monkeypatch.setattr(config_mod.importlib.util, "find_spec", lambda name: None)
    assert config_mod._no_cov_args(["tests"]) == []


@pytest.mark.slow
def test_empty_trace_is_not_reported_as_zero_coverage(tmp_path, monkeypatch):
    """If the tracer records nothing at all, every mutant is run instead of being called `not covered`."""
    make_project(tmp_path)
    # A sitecustomize-style saboteur: the tracer module is replaced by one that records nothing.
    fake = write(tmp_path / "fake", "testteeth_blank_tracer.py", """
        import json, os
        def pytest_unconfigure(config):
            with open(os.environ["TESTTEETH_TRACE_OUT"], "w") as handle:
                json.dump({}, handle)
        """)
    settings = load_settings(tmp_path, paths=["calc.py"])
    monkeypatch.setenv("PYTHONPATH", str(fake.parent))
    import testteeth.engine as engine

    real_run = engine.run_tests
    monkeypatch.setattr(
        engine, "run_tests",
        lambda cmd, *a, **k: real_run([c if c != "testteeth._tracer" else "testteeth_blank_tracer" for c in cmd], *a, **k),
    )
    report = grade(settings)
    assert report.count("no_coverage") == 0 and report.killed >= 3
    assert any("line coverage recorded nothing" in note for note in report.notes)


TRACER_SCRIPT = """
import sys, threading
import testteeth._tracer as tracer
import target_mod
target_mod.f(True)
worker = threading.Thread(target=target_mod.g)
worker.start(); worker.join()
tracer.pytest_unconfigure(None)
"""


def test_tracer_follows_threads_and_writes_xdist_fragments(tmp_path):
    target = write(tmp_path, "target_mod.py", """
        def f(x):
            if x:
                return 1
            return 2

        def g():
            return 3
        """)
    out = tmp_path / "trace.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TESTTEETH_")}
    env.update(
        TESTTEETH_TRACE_FILES=str(target), TESTTEETH_TRACE_OUT=str(out), PYTEST_XDIST_WORKER="gw3",
        PYTHONPATH=os.pathsep.join([str(tmp_path), *sys.path]),
    )
    subprocess.run([sys.executable, "-c", TRACER_SCRIPT], env=env, check=True, timeout=60)
    assert not out.exists()  # a worker never writes the controller's file
    lines = set(json.loads((tmp_path / "trace.json.gw3").read_text())[str(target)])
    assert {1, 2, 3, 6, 7} <= lines and 4 not in lines


# ---------------------------------------------------------------- git diff
def _repo(root: Path) -> Path:
    git(root, "init", "-q", "-b", "main")
    write(root, "mod.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def test_diff_is_taken_from_the_merge_base(tmp_path):
    """Commits that landed on the base branch after the fork are not this branch's changes."""
    root = _repo(tmp_path)
    git(root, "checkout", "-qb", "feature")
    write(root, "mod.py", "def a():\n    return 10\n\n\ndef b():\n    return 2\n")
    git(root, "commit", "-qam", "feature edits a")
    git(root, "checkout", "-q", "main")
    write(root, "mod.py", "def a():\n    return 1\n\n\ndef b():\n    return 20\n")
    write(root, "other.py", "def c():\n    return 3\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "main moves on")
    git(root, "checkout", "-q", "feature")

    changed = changed_lines(root, "main")
    assert changed == {(root / "mod.py").resolve(): frozenset({2})}
    diff = diff_with_untracked(root, "main", ("*.py",))
    assert "return 10" in diff and "return 20" not in diff and "other.py" not in diff


def test_diff_handles_non_ascii_file_names(tmp_path):
    root = _repo(tmp_path)
    write(root, "café.py", "def d():\n    return 4\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add")
    write(root, "café.py", "def d():\n    return 5\n")
    assert changed_lines(root, "HEAD") == {(root / "café.py").resolve(): frozenset({2})}


# ---------------------------------------------------------------- settings
@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('workers = "4"', "workers must be an integer"),
        ("fail_under = true", "fail_under must be a number"),
        ("paths = 3", "paths must be a string or a list of strings"),
        ('exclude = "tests"', "exclude must be a list of strings"),
        ('coverage = "no"', "coverage must be true or false"),
        ("operators = [1]", "operators must be a list of strings"),
    ],
)
def test_wrongly_typed_settings_are_config_errors(tmp_path, line, message):
    write(tmp_path, "pyproject.toml", f"[tool.testteeth]\n{line}\n")
    with pytest.raises(ConfigError, match=message):
        load_settings(tmp_path)


def test_testteeth_toml_is_used_when_pyproject_has_no_table(tmp_path):
    write(tmp_path, "pyproject.toml", "[tool.black]\nline-length = 100\n")
    write(tmp_path, "testteeth.toml", 'lang = "rust"\nfail_under = 80\n')
    settings = load_settings(tmp_path)
    assert settings.lang == "rust" and settings.fail_under == 80


def test_pyproject_table_wins_over_testteeth_toml(tmp_path):
    write(tmp_path, "pyproject.toml", "[tool.testteeth]\nfail_under = 50\n")
    write(tmp_path, "testteeth.toml", "fail_under = 80\n")
    assert load_settings(tmp_path).fail_under == 50


def test_testteeth_toml_table_must_be_a_table(tmp_path):
    write(tmp_path, "testteeth.toml", '[tool]\ntestteeth = "nope"\n')
    with pytest.raises(ConfigError, match="must be a table"):
        load_settings(tmp_path)


# ---------------------------------------------------------------- pragma: no mutate
def test_pragma_no_mutate_skips_the_line():
    source = "def f(x):\n    if x > 1:  # pragma: no mutate\n        return x + 1\n    return round(x, 2)  # pragma: no-mutate\n"
    lines = {m.line for m in generate_mutants(source, "m.py")}
    assert lines == {3}
    assert {m.line for m in generate_mutants(source.replace("# pragma: no mutate", ""), "m.py")} == {2, 3}


# ---------------------------------------------------------------- briefs for files that vanished
def test_read_sources_skips_unreadable_files(tmp_path, make_mutant):
    write(tmp_path, "ok.py", "x = 1\n")
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe\x00")
    mutants = [make_mutant(file="ok.py"), make_mutant(file="bad.py"), make_mutant(file="gone.py")]
    report = GradeReport(root=str(tmp_path), base_ref=None, test_command=[], mutants=mutants)
    assert read_sources(tmp_path, report) == {"ok.py": "x = 1\n"}


# ---------------------------------------------------------------- plugin under pytest-xdist
@pytest.mark.slow
def test_plugin_grades_once_under_xdist(pytester):
    if not _has("xdist"):
        pytest.skip("pytest-xdist is not installed")
    pytester.makepyfile(calc=PROJECT["calc.py"], test_calc=PROJECT["test_calc.py"])
    result = pytester.runpytest_subprocess("--testteeth", "--testteeth-paths=calc.py", "-n", "2")
    result.assert_outcomes(passed=1)
    out = result.stdout.str()
    assert out.count("Mutation score:") == 1


# ---------------------------------------------------------------- failure injected by a decorator
def test_failure_set_up_in_a_test_decorator_counts():
    from testteeth.failure_paths import analyze_module

    source = "import requests\n\n\ndef fetch(url):\n    return requests.get(url).json()\n"
    test = (
        "from unittest.mock import patch\n\n\n"
        "@patch('m.requests.get', side_effect=ConnectionError('down'))\n"
        "def test_fetch_down(get):\n    assert fetch('u') is None\n"
    )
    assert analyze_module(source, "m.py", None, [test], {"fetch"}) == []
    plain = "def test_fetch():\n    assert fetch('u')\n"
    assert [g.kind for g in analyze_module(source, "m.py", None, [plain], {"fetch"})] == ["external-call"]


# ---------------------------------------------------------------- Rust --base selection
def test_rust_base_ref_selects_only_the_changed_function(tmp_path, monkeypatch):
    """Context lines of the diff given to cargo-mutants used to count as changed: neighbours got gaps."""
    from testteeth.languages import rust as rs

    root = tmp_path
    git(root, "init", "-q", "-b", "main")
    write(root, "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    body = "pub fn {n}(p: &str) -> std::io::Result<String> {{\n    std::fs::read_to_string(p){tail}\n}}\n"
    write(root, "src/lib.rs", "".join(body.format(n=n, tail="") for n in "abc"))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    text = (root / "src/lib.rs").read_text().splitlines(keepends=True)
    text[4] = "    std::fs::read_to_string(p).map(|s| s.trim().to_string())\n"
    (root / "src/lib.rs").write_text("".join(text))

    def fake_engine(cmd, cwd, env_extra=None):
        out = Path(cmd[cmd.index("--output") + 1]) / "mutants.out"
        out.mkdir(parents=True)
        (out / "outcomes.json").write_text(json.dumps({"outcomes": []}))
        from testteeth.engine import SuiteRun

        return SuiteRun(0, 0.1, "")

    monkeypatch.setattr(rs, "run_engine", fake_engine)
    monkeypatch.setattr(rs, "check_installed", lambda: "cargo")
    report = rs.CargoMutantsAdapter().grade(load_settings(root, lang="rust", base_ref="HEAD"))
    assert {gap.function for gap in report.gaps} == {"b"}


# ---------------------------------------------------------------- brace scanner (TypeScript / Rust)
def _ranges(source: str, lang: str) -> dict[str, tuple[int, int]]:
    from testteeth.languages.scan import scan

    return {f.qualname: (f.line, f.end_line) for f in scan(source, lang).functions}


@pytest.mark.parametrize(
    "signature",
    [
        "export function load(opts = {}) {",
        "function load({ strict, a }) {",
        "function load(opts: { strict: boolean }, b) {",
        "function load(opts): { a: number } {",
        "const load = ({ strict, a }) => {",
        "export default async function load(opts = {}) {",
    ],
)
def test_ts_braces_in_the_signature_do_not_end_the_function(signature):
    source = f"{signature}\n  if (opts.strict) {{\n    throw new Error('strict');\n  }}\n  return 1;\n}}\nfunction next() {{\n  return 2;\n}}\n"
    assert _ranges(source, "typescript") == {"load": (1, 6), "next": (7, 9)}


def test_ts_quotes_do_not_swallow_following_functions():
    jsx = "export function A() {\n  return <p>Don't</p>;\n}\nexport function B(x) {\n  if (x > 1) {\n    return 1;\n  }\n  return 2;\n}\n"
    assert _ranges(jsx, "typescript") == {"A": (1, 3), "B": (4, 9)}
    regex = "function a(s) {\n  return s.replace(/'/g, '');\n}\nfunction b() {\n  return 1;\n}\n"
    assert _ranges(regex, "typescript") == {"a": (1, 3), "b": (4, 6)}


def test_ts_callbacks_are_not_named_after_their_callee():
    source = "function schedule(cb) {\n  setTimeout(function () {\n    cb();\n  }, 10);\n  for (let i = 0; i < 2; i++) {\n    cb();\n  }\n}\n"
    assert _ranges(source, "typescript") == {"schedule": (1, 8)}
    assert _ranges("export default function () {\n  return 1;\n}\n", "typescript") == {"default": (1, 3)}


def test_rust_semicolons_in_the_signature_do_not_hide_the_function():
    source = (
        "pub fn load_key(path: &str, salt: [u8; 4]) -> Result<Vec<u8>, std::io::Error> {\n"
        "    let data = std::fs::read(path)?;\n    Ok(data)\n}\n"
        "fn zero() -> [u8; 4] {\n    [0; 4]\n}\n"
        "impl Key {\n    pub fn new(seed: [u8; 16]) -> Self {\n        Key { seed }\n    }\n}\n"
    )
    assert _ranges(source, "rust") == {"load_key": (1, 4), "zero": (5, 7), "Key::new": (9, 11)}


# ---------------------------------------------------------------- failure-path rule packs
def _gaps(source: str, lang: str, tests: list[str], **kw) -> list[tuple[str, str]]:
    from testteeth.languages.scan import scan
    from testteeth.rules import analyze

    return [(g.function, g.kind) for g in analyze(scan(source, lang), "f", tests, **kw)]


def test_ts_throw_on_a_covered_guard_line_is_still_a_gap():
    """`if (..) throw ..` on one line: the condition ran, the throw did not."""
    source = "export function pay(amount) {\n  if (amount <= 0) throw new RangeError('amount must be positive');\n  return amount;\n}\n"
    throw_col = source.splitlines()[1].index("throw")
    kw = {"coverage": {2: True, 3: True}}
    assert _gaps(source, "typescript", ["it('pays', () => { pay(1); })"], **kw) == []  # columns unknown: no claim
    positions = {2: [(6, True), (throw_col + 21, False)], 3: [(2, True)]}
    assert _gaps(source, "typescript", ["it('pays', () => { pay(1); })"], positions=positions, **kw) == [("pay", "throw")]
    ran = {2: [(6, True), (throw_col + 21, True)]}
    assert _gaps(source, "typescript", ["it('pays', () => { pay(1); })"], positions=ran, **kw) == []


def test_ts_uncovered_catch_block_is_reported_even_when_a_test_mentions_failure():
    source = "export async function load(client) {\n  try {\n    return await client.get('/x');\n  } catch (err) {\n    report(err);\n    return null;\n  }\n}\n"
    tests = ["it('loads', async () => { await load(c); expect(() => other()).toThrow(); })"]
    # Stryker's only verdict for the handler is its block mutant on the `catch (err) {` line: NoCoverage.
    gaps = _gaps(source, "typescript", tests, coverage={3: True, 4: False})
    assert ("load", "catch-branch") in gaps


def test_rust_err_patterns_are_not_error_returns():
    source = (
        "fn is_bad(r: Result<u8, E>) -> bool {\n    matches!(r, Err(_)) || r == Err(E::A)\n}\n"
        "fn code(r: Option<Result<u8, E>>) -> u8 {\n    match r {\n        Some(Err(_)) => 0,\n        _ => 1,\n    }\n}\n"
        "fn guarded(r: Result<u8, E>) -> u8 {\n    match r {\n        Err(e) if e.code == 2 => 0,\n        _ => 1,\n    }\n}\n"
        "fn fails() -> Result<u8, E> {\n    Err(E::A)\n}\n"
    )
    gaps = _gaps(source, "rust", ["fn t() { is_bad(x); code(x); guarded(x); fails(); }"])
    assert ("fails", "error-return") in gaps
    assert [g for g in gaps if g[1] == "error-return"] == [("fails", "error-return")]
    assert ("guarded", "match-err") in gaps and ("code", "match-err") in gaps


# ---------------------------------------------------------------- lines, columns, templates, client names
def test_form_feed_does_not_shift_python_mutants():
    """`str.splitlines` breaks on \\f; Python's parser does not."""
    from testteeth.operators import apply_mutant, matches_source

    source = "import os\n\x0c\ndef f(a, b):\n    return a < b\n"
    mutant = next(m for m in generate_mutants(source, "m.py") if m.operator == "comparison")
    assert mutant.line == 4 and mutant.original == "a < b"
    assert matches_source(source, mutant)
    assert apply_mutant(source, mutant) == source.replace("a < b", "(a <= b)")


def test_line_rules_per_language():
    from testteeth.lines import split_lines

    text = "a\x0cb c\r\nd\n"
    assert split_lines(text, "python") == ["a\x0cb c", "d"]
    assert split_lines(text, "rust") == ["a\x0cb c\r", "d"]
    assert split_lines(text, "typescript") == ["a\x0cb", "c", "d"]
    assert split_lines(text, "typescript", keepends=True) == ["a\x0cb ", "c\r\n", "d\n"]
    assert "".join(split_lines(text, "rust", keepends=True)) == text


def _stryker(source: str, start: tuple[int, int], end: tuple[int, int], tmp_path: Path):
    from testteeth.languages.typescript import parse_report

    location = {"start": {"line": start[0], "column": start[1]}, "end": {"line": end[0], "column": end[1]}}
    entry = {"id": "1", "mutatorName": "EqualityOperator", "replacement": "a >= 1", "status": "Survived",
             "location": location}
    report = {"files": {"src/a.ts": {"language": "typescript", "source": source, "mutants": [entry]}}}
    mutants, _, _ = parse_report(report, tmp_path)
    return mutants[0]


def test_stryker_columns_are_utf16_code_units(tmp_path):
    source = "export function f(a) {\n  const s = '😀😀'; return a > 1;\n}\n"
    line = source.split("\n")[1]
    start = len(line[: line.index("a > 1")].encode("utf-16-le")) // 2 + 1  # what Babel reports
    mutant = _stryker(source, (2, start), (2, start + 5), tmp_path)
    assert mutant.original == "a > 1" and mutant.function == "f"
    brief = build_one_brief(mutant, source)
    assert "return a >= 1;" in brief.mutated_code


def test_form_feed_does_not_shift_typescript_mutants(tmp_path):
    source = "// header\x0c\nexport function f(a) {\n  return a > 1;\n}\n"
    mutant = _stryker(source, (3, 10), (3, 15), tmp_path)
    assert mutant.original == "a > 1" and mutant.function == "f"
    assert "return a >= 1;" in build_one_brief(mutant, source).mutated_code


def build_one_brief(mutant, source):
    from testteeth.suggest import brief_for

    return brief_for(mutant, source, "typescript")


def test_ts_nested_template_literals_stay_balanced():
    source = (
        "export function render(xs) {\n"
        "  return `<ul>${xs.map((x) => `<li class=\"{a}\">${x.ok ? `{${x.name}}` : '}'}</li>`).join('')}</ul>`;\n"
        "}\n"
        "export function after(a) {\n  if (a) {\n    return 1;\n  }\n  return 2;\n}\n"
    )
    assert _ranges(source, "typescript") == {"render": (1, 3), "after": (4, 9)}


def test_ts_generic_arrow_functions_are_found():
    source = (
        "export const first = async <T,>(items: T[]): Promise<T> => {\n  return items[0];\n};\n"
        "const pick = <T, K extends keyof T>(o: T, k: K) => o[k];\n"
    )
    assert _ranges(source, "typescript") == {"first": (1, 3), "pick": (4, 4)}


@pytest.mark.parametrize(
    ("receiver", "external"),
    [
        ("capitals", False), ("feedback", False), ("therapist", False), ("dbase_rows", False),
        ("client", True), ("self.http_client", True), ("apiClient", True), ("db", True), ("self._db", True),
        ("connection", True), ("sessions", True), ("gateway", True),
    ],
)
def test_client_hints_match_whole_words(receiver, external):
    import ast

    from testteeth.failure_paths import external_call_name

    call = ast.parse(f"{receiver}.get('x')").body[0].value
    assert (external_call_name(call) is not None) is external


def test_ts_swallowed_error_brief_speaks_typescript(make_mutant):
    source = "function f() {\n  try {\n    g();\n  } catch (e) {\n    report(e);\n  }\n}\n"
    mutant = make_mutant(file="a.ts", function="f", line=4, col=14, end_line=6, end_col=3, operator="swallow-exception",
                         original="{\n    report(e);\n  }", replacement="{}", exception="the error")
    brief = build_one_brief(mutant, source)
    assert "`catch` block can be emptied" in brief.why_missed and "except" not in brief.why_missed


# ---------------------------------------------------------------- review findings
def test_pragma_counts_only_in_comments_and_protects_the_whole_span():
    in_string = 'def f(x):\n    return x + len("# pragma: no mutate")\n'
    assert {m.operator for m in generate_mutants(in_string, "m.py")} == {"arithmetic", "return-value"}
    upper = "def f(x):\n    return x + 1  # PRAGMA: NO MUTATE\n"
    assert generate_mutants(upper, "m.py") == []
    spanning = "def f(x):\n    return (\n        x + 1  # pragma: no mutate\n    )\n"
    assert generate_mutants(spanning, "m.py") == []  # `return None` would remove the protected expression


def test_skipped_tests_are_not_failure_evidence():
    from testteeth.failure_paths import analyze_module

    source = "import requests\n\n\ndef fetch(url):\n    return requests.get(url).json()\n"
    tests = (
        "import pytest\n\n\ndef test_fetch():\n    assert fetch('u')\n\n\n"
        "@pytest.mark.{mark}\ndef test_fetch_times_out():\n    with pytest.raises(TimeoutError):\n        fetch('u')\n"
    )
    skipped = tests.format(mark='skip(reason="TimeoutError")')
    assert [g.kind for g in analyze_module(source, "m.py", None, [skipped], {"fetch"})] == ["external-call"]
    conditional = tests.format(mark='skipif(False, reason="x")')
    assert analyze_module(source, "m.py", None, [conditional], {"fetch"}) == []


def test_acronym_client_names_are_clients():
    import ast

    from testteeth.failure_paths import external_call_name

    for receiver in ("APIClient", "self.HTTPSession", "DBConn"):
        assert external_call_name(ast.parse(f"{receiver}.get('x')").body[0].value) is not None, receiver


def test_ts_line_comment_ends_at_a_unicode_line_separator():
    source = "// note function f() {\n  return 1;\n}\n"
    assert _ranges(source, "typescript") == {"f": (2, 4)}


def test_ts_throw_gap_is_not_hidden_by_later_code_on_the_line():
    source = "export function pay(x) {\n  if (x) throw new Error('x'); return 1;\n}\n"
    line = source.splitlines()[1]
    positions = {2: [(6, True), (line.index("'x'"), False), (line.index("1;"), True)]}
    gaps = _gaps(source, "typescript", ["it('pays', () => { pay(0); })"], coverage={2: True}, positions=positions)
    assert gaps == [("pay", "throw")]


def test_unreadable_files_are_named_instead_of_reported_clean(tmp_path, make_mutant):
    from testteeth.suggest import build_briefs, render_markdown, unreadable_sources

    report = GradeReport(root=str(tmp_path), base_ref=None, test_command=[], mutants=[make_mutant(file="gone.py")])
    sources = read_sources(tmp_path, report)
    skipped = unreadable_sources(report, sources)
    assert skipped == ["gone.py"]
    text = render_markdown(build_briefs(report, sources), [], None, skipped)
    assert "gone.py" in text and "already pin this code down" not in text
    assert "already pin this code down" in render_markdown([], [])
