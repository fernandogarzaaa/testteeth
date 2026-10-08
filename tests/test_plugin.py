from __future__ import annotations

import json

import pytest

from testteeth.plugin import _forwarded_args


def test_forwarded_args_strip_testteeth_options():
    args = ["tests", "--testteeth", "--testteeth-base", "main", "--testteeth-fail-under=80", "-q",
            "--testteeth-paths", "src", "--testteeth-json=out.json", "-k", "fast"]
    assert _forwarded_args(args) == ["tests", "-q", "-k", "fast"]
    assert _forwarded_args([]) == []


def test_plugin_is_inert_without_flag(pytester):
    pytester.makepyfile(test_a="def test_a():\n    assert True\n")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert "Mutation score" not in result.stdout.str()


CODE = "def is_adult(age):\n    if age < 0:\n        raise ValueError('age')\n    return age >= 18\n"
WEAK = "from calc import is_adult\n\ndef test_adult():\n    assert is_adult(30)\n"
STRONG = (
    "import pytest\nfrom calc import is_adult\n\n"
    "def test_boundary():\n    assert is_adult(18) is True\n    assert is_adult(17) is False\n\n"
    "def test_zero():\n    assert is_adult(0) is False\n\n"
    "def test_negative():\n    with pytest.raises(ValueError):\n        is_adult(-1)\n"
)


@pytest.mark.slow
def test_plugin_grades_after_green_session_and_enforces_gate(pytester):
    pytester.makepyfile(calc=CODE, test_calc=WEAK)
    result = pytester.runpytest_subprocess("--testteeth", "--testteeth-fail-under=90", "--testteeth-json=r.json")
    result.stdout.fnmatch_lines(["*testteeth*", "*is_adult*", "*Mutation score:*FAIL: --fail-under 90*"])
    assert result.ret == 1
    data = json.loads((pytester.path / "r.json").read_text())
    assert data["passed_gate"] is False and data["total"] > 0

    pytester.makepyfile(test_calc=STRONG)
    result = pytester.runpytest_subprocess("--testteeth", "--testteeth-fail-under", "90", "--testteeth-paths", "calc.py")
    result.stdout.fnmatch_lines(["*Mutation score: 100.0%*PASS*"])
    assert result.ret == 0


@pytest.mark.slow
def test_plugin_skips_red_session_and_reports_errors(pytester):
    pytester.makepyfile(calc=CODE, test_calc="def test_bad():\n    assert False\n")
    result = pytester.runpytest_subprocess("--testteeth")
    result.stdout.fnmatch_lines(["*testteeth: skipped, the test suite did not pass*"])
    assert result.ret == 1
    pytester.makepyfile(test_calc=WEAK)
    result = pytester.runpytest_subprocess("--testteeth", "--testteeth-base", "no-such-ref")
    result.stdout.fnmatch_lines(["*testteeth: error:*"])
    assert result.ret == 1
