from __future__ import annotations

import ast
import textwrap

from testteeth.failure_paths import (
    analyze_function,
    analyze_module,
    external_call_name,
    failure_tested,
    is_stub,
)

FETCH = textwrap.dedent(
    """
    import requests

    def fetch(url, retries=3):
        for attempt in range(retries):
            try:
                return requests.get(url, timeout=2).json()
            except requests.Timeout:
                continue
        raise RuntimeError("unreachable")
    """
).lstrip()


def kinds(gaps):
    return sorted(g.kind for g in gaps)


def test_everything_flagged_when_only_happy_path_runs():
    covered = {1, 3, 4, 5, 6}
    gaps = analyze_module(FETCH, "f.py", covered, ["def test_fetch():\n    assert fetch('u')\n"])
    assert kinds(gaps) == ["except-branch", "external-call", "raise", "retry", "timeout"]
    assert [g.line for g in gaps] == sorted(g.line for g in gaps)
    assert all(g.function == "fetch" and g.file == "f.py" for g in gaps)


def test_covered_handler_counts_as_failure_exercised():
    covered = {1, 3, 4, 5, 6, 7, 8, 9}
    assert analyze_module(FETCH, "f.py", covered, []) == []


def test_test_text_with_failure_simulation_suppresses_heuristic_gaps():
    tests = ["def test_fetch_timeout(m):\n    m.side_effect = Timeout\n    with pytest.raises(RuntimeError):\n        fetch('u')\n"]
    gaps = analyze_module(FETCH, "f.py", {1, 3, 4, 5, 6}, tests)
    assert kinds(gaps) == ["except-branch", "raise"]


def test_without_coverage_only_static_checks_run():
    assert kinds(analyze_module(FETCH, "f.py", None, [])) == ["external-call", "retry", "timeout"]


def test_function_filter_and_unparsable_tests():
    src = "def a():\n    raise ValueError\n\ndef b():\n    raise KeyError\n"
    gaps = analyze_module(src, "m.py", set(), ["def test_(:"], functions={"b"})
    assert [(g.function, g.kind, g.line) for g in gaps] == [("b", "raise", 5)]


def test_raise_inside_handler_reported_once_as_handler():
    src = "def a(g):\n    try:\n        g()\n    except KeyError:\n        raise ValueError('x')\n"
    assert kinds(analyze_module(src, "m.py", {1, 2, 3}, [])) == ["except-branch"]


def test_nested_function_constructs_are_not_attributed_to_outer():
    src = "def outer():\n    def inner():\n        raise ValueError\n    return inner\n"
    gaps = analyze_module(src, "m.py", {1, 2, 4}, [])
    assert [(g.function, g.kind) for g in gaps] == [("outer.inner", "raise")]


def test_stub_detection():
    def fn(src):
        return ast.parse(textwrap.dedent(src)).body[0]

    assert is_stub(fn("def f():\n    ...\n"))
    assert is_stub(fn("def f():\n    '''doc'''\n"))
    assert is_stub(fn("def f():\n    pass\n"))
    assert is_stub(fn("def f():\n    raise NotImplementedError\n"))
    assert not is_stub(fn("def f():\n    raise ValueError\n"))
    assert not is_stub(fn("def f():\n    return 1\n"))
    func = fn("def charge(self, txn, timeout):\n    ...\n")
    assert analyze_function(func, "charge", "p.py", set(), []) == []


def call(expr):
    return ast.parse(expr).body[0].value


def test_external_call_detection():
    assert external_call_name(call("open('f')")) == "open"
    assert external_call_name(call("requests.post(u)")) == "requests.post"
    assert external_call_name(call("subprocess.run(['ls'])")) == "subprocess.run"
    assert external_call_name(call("self.client.get(u)")) == "self.client.get"
    assert external_call_name(call("db_session.execute(q)")) == "db_session.execute"
    assert external_call_name(call("items.get(k)")) is None
    assert external_call_name(call("len(x)")) is None
    assert external_call_name(call("make()()")) is None


def test_failure_tested_requires_name_and_failure_words():
    bodies = ["def test_x():\n    assert fetch_all() == 1\n", "def test_y():\n    with pytest.raises(KeyError):\n        other()\n"]
    assert not failure_tested("fetch_all", bodies)
    assert failure_tested("Cls.other", bodies)
    assert not failure_tested("fetch", bodies)  # word boundary: fetch_all is not fetch
    assert not failure_tested("f", ["def test_f():\n    f(timeout=1)\n"])  # lowercase 'timeout' is not a failure
    assert failure_tested("f", ["def test_f():\n    g.side_effect = OSError\n    f()\n"])


def test_retry_detected_from_function_name_with_try_in_loop():
    src = "def with_retries(g):\n    while True:\n        try:\n            return g()\n        except OSError:\n            pass\n"
    gaps = analyze_module(src, "m.py", {1, 2, 3, 4}, [])
    assert "retry" in kinds(gaps)
