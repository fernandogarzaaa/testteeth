"""Edge cases found by running testteeth on itself (dogfooding): boundaries, truncation, rare syntax."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import testteeth
from conftest import write
from testteeth import engine
from testteeth.config import load_settings
from testteeth.engine import SuiteRun, grade, select
from testteeth.failure_paths import _has_timeout, analyze_module
from testteeth.models import FailurePathGap, GradeReport
from testteeth.operators import generate_mutants
from testteeth.suggest import build_briefs, render_markdown


def ops(src: str, operators: set[str] | None = None):
    return generate_mutants(src, "m.py", frozenset(operators) if operators else None)


# ------------------------------------------------------------------ operators
def test_long_context_headers_are_truncated_at_80():
    cond = "a" * 74  # "if " + 74 + ":" == 78 chars -> kept as is
    long_cond = "b" * 90
    src = f"def f({cond}, {long_cond}):\n    if {cond}:\n        x = 1\n    if {long_cond}:\n        y = 2\n"
    contexts = [m.context for m in ops(src, {"constant"})]
    assert contexts[0] == f"if {cond}:"
    assert contexts[1] == f"if {long_cond}:"[:77] + "..." and len(contexts[1]) == 80
    exact = "c" * 76  # "if " + 76 + ":" == 80 chars -> kept as is
    (m,) = ops(f"def f({exact}):\n    if {exact}:\n        x = 1\n", {"constant"})
    assert m.context == f"if {exact}:"


def test_long_call_names_are_truncated_at_60():
    short = "o." + "a" * 58  # exactly 60 chars
    long = "o." + "b" * 70
    calls = [m.call for m in ops(f"def f(o):\n    {short}()\n    {long}()\n", {"remove-call"})]
    assert calls[0] == short
    assert calls[1] == long[:57] + "..." and len(calls[1]) == 60


def test_loops_with_try_else_finally_and_match_are_walked():
    src = (
        "def f(xs, cm):\n"
        "    while xs > 1:\n        xs -= 2\n    else:\n        xs = 3\n"
        "    for x in range(4):\n        y = x + 5\n    else:\n        y = 6\n"
        "    with cm(7) as c:\n        z = 8\n"
        "    try:\n        w = 9\n    except KeyError:\n        w = 10\n    else:\n        w = 11\n    finally:\n        v = 12\n"
        "    match xs:\n        case 13 if xs > 16:\n            u = 14\n"
        "    return 15\n"
    )
    constants = {m.original: m.context for m in ops(src, {"constant"})}
    assert set(constants) >= {str(i) for i in range(1, 13)} | {"14", "15"}
    assert constants["2"] == "while xs > 1:"
    assert constants["5"] == "for x in range(4):"
    assert constants["10"] == "except KeyError:"
    assert constants["12"] == "finally:"
    assert constants["11"] == "" and constants["8"] == ""
    assert constants["14"] == "case 13:" and constants["16"] == ""
    assert {m.original for m in ops(src, {"arithmetic"})} >= {"xs -= 2", "x + 5"}


def test_scope_is_restored_after_class_and_nested_function():
    src = "class A:\n    class B:\n        def m(self):\n            return 1\n    def n(self):\n        return 2\ndef top():\n    return 3\n"
    funcs = {m.original: m.function for m in ops(src, {"constant"})}
    assert funcs == {"1": "A.B.m", "2": "A.n", "3": "top"}


def test_handler_triviality_rules():
    def swallowed(body: str) -> int:
        src = f"def f(g):\n    try:\n        g()\n    except KeyError:\n{body}"
        return len(ops(src, {"swallow-exception"}))

    assert swallowed("        ...\n") == 0
    assert swallowed("        'ignored'\n") == 0
    assert swallowed("        pass\n        g()\n") == 1
    assert swallowed("        return 1\n") == 1
    assert swallowed("        g()\n") == 1


# ------------------------------------------------------------------ config boundaries
def test_config_minimum_valid_values(tmp_path):
    s = load_settings(tmp_path, workers=1, max_mutants=1, timeout_factor=0.01, min_timeout=0.01)
    assert (s.workers, s.max_mutants) == (1, 1)


# ------------------------------------------------------------------ selection
def test_select_includes_files_with_functions_but_no_mutants_and_constant_only_modules(tmp_path):
    write(tmp_path, "a.py", "def f():\n    pass\n")
    write(tmp_path, "b.py", "LIMIT = 3\n")
    write(tmp_path, "c.py", "import os\n")
    sel = select(load_settings(tmp_path))
    assert set(sel.sources) == {"a.py", "b.py"}
    assert sel.functions["a.py"] == {"f"} and sel.functions["b.py"] == set()


def test_select_sampling_indices_and_no_sampling_at_exact_cap(tmp_path):
    write(tmp_path, "m.py", "def f():\n    return [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]\n")
    full = select(load_settings(tmp_path, operators=["constant"])).mutants
    assert len(full) == 10
    capped = select(load_settings(tmp_path, operators=["constant"], max_mutants=10))
    assert len(capped.mutants) == 10 and capped.notes == []
    sampled = select(load_settings(tmp_path, operators=["constant"], max_mutants=4)).mutants
    assert [m.original for m in sampled] == ["1", "3", "6", "8"]  # indices 0, 2.5, 5, 7.5 -> int


def test_select_changed_line_ranges_include_decorators_and_last_line(tmp_path, monkeypatch):
    src = "import functools\n\n@functools.cache\ndef a(x):\n    return x + 1\n\ndef b(x):\n    return x - 1\n"
    write(tmp_path, "m.py", src)
    path = (tmp_path / "m.py").resolve()

    def pick(lines):
        monkeypatch.setattr(engine, "changed_lines", lambda root, ref: {path: frozenset(lines)})
        return select(load_settings(tmp_path, base_ref="x")).functions.get("m.py")

    assert pick({3}) == {"a"}  # decorator line
    assert pick({5}) == {"a"}  # last line of a
    assert pick({6}) is None  # blank line between functions: nothing selected
    assert pick({7}) == {"b"}
    assert pick({2}) is None


# ------------------------------------------------------------------ grade internals
CODE = "def f(x):\n    if x < 0:\n        raise ValueError('neg')\n    return x + 1\n"


class Recorder:
    def __init__(self, base_seconds=0.25):
        self.base_seconds = base_seconds
        self.timeouts: list[float | None] = []
        self.cwds: set[str] = set()

    def __call__(self, cmd, cwd, env, timeout):
        self.timeouts.append(timeout)
        if timeout is None:
            out = env.get("TESTTEETH_TRACE_OUT")
            if out:
                Path(out).write_text(json.dumps({str(Path(cwd) / "m.py"): [1, 2, 4]}))
            return SuiteRun(0, self.base_seconds, "")
        self.cwds.add(str(cwd))
        return SuiteRun(1, 0.01, "x\n" * 3 + "E" * 300)


def test_timeout_formula_workers_gaps_and_grouping(tmp_path, monkeypatch):
    write(tmp_path, "m.py", CODE)
    rec = Recorder(base_seconds=0.5)
    monkeypatch.setattr(engine, "run_tests", rec)
    report = grade(load_settings(tmp_path, workers=2, min_timeout=1, timeout_factor=4))
    assert rec.timeouts[0] is None
    assert set(rec.timeouts[1:]) == {4.0}  # 0.5 * 4 + 2
    assert len(rec.cwds) == 2  # two isolated workspaces were used
    assert all(m.detail == "E" * 200 for m in report.mutants if m.status == "killed")
    assert [(g.kind, g.line) for g in report.gaps] == [("raise", 3)]
    (fn,) = report.functions
    assert fn.function == "f" and fn.gaps == report.gaps
    assert report.elapsed_seconds > 0
    assert all(m.status in {"killed", "no_coverage"} for m in report.mutants)
    statuses = {m.line: m.status for m in report.mutants}
    assert statuses[3] == "no_coverage"


def test_min_timeout_floor_and_single_worker(tmp_path, monkeypatch):
    write(tmp_path, "m.py", CODE)
    rec = Recorder(base_seconds=0.1)
    monkeypatch.setattr(engine, "run_tests", rec)
    grade(load_settings(tmp_path, workers=1, min_timeout=7, timeout_factor=1))
    assert set(rec.timeouts[1:]) == {7}
    assert len(rec.cwds) == 1


def test_returned_mutants_are_the_executed_objects(tmp_path, monkeypatch):
    write(tmp_path, "m.py", CODE)
    monkeypatch.setattr(engine, "run_tests", Recorder())
    seen = []
    report = grade(load_settings(tmp_path, workers=1), progress=lambda d, t, m: seen.append(m))
    assert {id(m) for m in seen} == {id(m) for m in report.mutants}
    assert all(m.status != "pending" for m in seen)
    assert all(m.duration == 0.01 for m in seen if m.status == "killed")


def test_group_includes_function_with_only_gaps(tmp_path, monkeypatch):
    write(tmp_path, "m.py", "import requests\n\ndef fetch(u):\n    requests.get(u)\n\ndef other():\n    pass\n")
    monkeypatch.setattr(engine, "run_tests", Recorder())
    report = grade(load_settings(tmp_path, operators=["constant"], coverage=False))
    assert report.total == 0
    assert [(f.function, len(f.gaps)) for f in report.functions] == [("fetch", 1)]


def test_tail_helper():
    text = "\n".join(str(i) for i in range(40))
    assert engine._tail(text).splitlines() == [str(i) for i in range(15, 40)]
    assert engine._tail(text, 2) == "38\n39"


# ------------------------------------------------------------------ failure paths
def test_has_timeout_detection():
    def fn(src):
        return ast.parse(src).body[0]

    assert _has_timeout(fn("def f(deadline):\n    pass\n"))
    assert _has_timeout(fn("def f():\n    g(read_timeout=1)\n"))
    assert _has_timeout(fn("def f():\n    return TIMEOUT\n"))
    assert _has_timeout(fn("def f():\n    g(timeout=1)\n"))
    assert _has_timeout(fn("def f(x):\n    return x\n")) is False


def test_raise_detail_truncation_and_bare_except():
    long = "x" * 70
    src = f"def f(g):\n    try:\n        g()\n    except:\n        g()\n    raise ValueError('{long}')\n"
    gaps = analyze_module(src, "m.py", {1, 2, 3}, [])
    assert gaps[0].detail == "`except BaseException` branch is never executed by any test"
    what = f"ValueError('{long}')"
    assert gaps[1].detail == f"`raise {what[:57]}...` is never triggered by any test"
    short = "def f():\n    raise ValueError\n"
    assert analyze_module(short, "m.py", {1}, [])[0].detail == "`raise ValueError` is never triggered by any test"
    sixty = "Err" + "x" * 57  # exactly 60 chars
    assert f"`raise {sixty}`" in analyze_module(f"def f():\n    raise {sixty}\n", "m.py", {1}, [])[0].detail
    bare = "def f():\n    raise\n"
    assert "`raise re-raise`" in analyze_module(bare, "m.py", {1}, [])[0].detail


def test_external_call_gap_points_at_first_external_call():
    src = "import requests\n\ndef f(u):\n    x = len(u)\n    a = requests.post(u)\n    b = requests.get(u)\n    return a, b, x\n"
    (gap,) = analyze_module(src, "m.py", None, [])
    assert gap.kind == "external-call" and gap.line == 6  # names sorted: requests.get first
    assert "`requests.get`, `requests.post`" in gap.detail


# ------------------------------------------------------------------ suggest / report / package
def test_markdown_numbering_limits_and_gap_only():
    from testteeth.operators import generate_mutants as gen

    src = "def f(a):\n    return a + 1\n"
    ms = gen(src, "f.py")
    for m in ms:
        m.status = "survived"
    report = GradeReport(root="/", base_ref=None, test_command=[], mutants=ms)
    briefs = build_briefs(report, {"f.py": src})
    md = render_markdown(briefs, [], limit=len(briefs))
    assert "## 1. `f`" in md and "more surviving" not in md
    assert md.rstrip().endswith("`")
    gap = FailurePathGap("f.py", "f", 1, "raise", 2, "boom")
    only_gaps = render_markdown([], [gap])
    assert "## Failure paths no test exercises" in only_gaps and "## 1." not in only_gaps
    assert render_markdown(briefs, [], limit=None).count("\n## ") == len(briefs)


def test_report_json_rounds_times():
    r = GradeReport(root="/", base_ref=None, test_command=[], baseline_seconds=1.23456, elapsed_seconds=2.98765)
    data = r.to_dict()
    assert (data["baseline_seconds"], data["elapsed_seconds"]) == (1.235, 2.988)


def test_package_lazy_exports():
    from testteeth.config import load_settings as ls
    from testteeth.engine import grade as g

    assert testteeth.grade is g and testteeth.load_settings is ls
    with pytest.raises(AttributeError):
        testteeth.nope  # noqa: B018
