"""End-to-end runs with real pytest subprocesses (marked slow)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import git, write
from testteeth.config import load_settings
from testteeth.engine import BaselineFailed, grade

pytestmark = pytest.mark.slow

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "payments"

COUNTER = """
def count_up(start, stop):
    steps = 0
    i = start
    while i != stop:
        i += 1
        steps += 1
    return steps
"""


def test_example_weak_vs_strong_scores():
    weak = grade(load_settings(EXAMPLE, test_args=["tests_weak"]))
    strong = grade(load_settings(EXAMPLE, test_args=["tests_strong"]))
    assert weak.total == strong.total > 40
    assert weak.score < 40 < 90 < strong.score
    weak_kinds = {g.kind for g in weak.gaps}
    assert {"except-branch", "raise", "retry", "timeout"} <= weak_kinds
    assert strong.gaps == []
    surviving_ops = {m.operator for m in weak.surviving()}
    assert {"remove-raise", "swallow-exception", "comparison"} <= surviving_ops


def test_hanging_mutant_is_a_timeout_and_counts_as_killed(tmp_path):
    write(tmp_path, "counter.py", COUNTER)
    write(tmp_path, "test_counter.py", "from counter import count_up\n\ndef test_count():\n    assert count_up(2, 5) == 3\n")
    report = grade(load_settings(tmp_path, min_timeout=2, timeout_factor=1, operators=["arithmetic"]))
    statuses = {m.original: m.status for m in report.mutants}
    assert statuses["i += 1"] == "timeout"
    assert statuses["steps += 1"] == "killed"
    assert report.score == 100.0


def test_broken_suite_is_refused(tmp_path):
    write(tmp_path, "counter.py", COUNTER)
    write(tmp_path, "test_counter.py", "def test_bad():\n    assert 1 == 2\n")
    with pytest.raises(BaselineFailed) as info:
        grade(load_settings(tmp_path))
    assert "assert 1 == 2" in info.value.output


def test_diff_mode_only_grades_changed_function_and_respects_src_layout(tmp_path):
    write(tmp_path, "src/lib/__init__.py", "")
    write(tmp_path, "src/lib/ops.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    write(tmp_path, "tests/test_ops.py",
          "from lib.ops import add, sub\n\ndef test_add():\n    assert add(2, 3) == 5\n\ndef test_sub():\n    assert sub(2, 3)\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    write(tmp_path, "src/lib/ops.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return (a - b)\n")
    report = grade(load_settings(tmp_path, base_ref="main"))
    assert {m.function for m in report.mutants} == {"sub"}
    statuses = {m.operator: m.status for m in report.mutants}
    # `assert sub(2, 3)` is truthy for a - b and a + b alike, but not for None
    assert statuses == {"arithmetic": "survived", "return-value": "killed"}
    assert [f.function for f in report.functions] == ["sub"]


def run_cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "testteeth", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=300)


def test_cli_gate_exit_codes_and_suggest_on_example():
    weak = run_cli("run", "-q", "--pytest-args", "tests_weak", "--fail-under", "60", "--json", "-", cwd=EXAMPLE)
    assert weak.returncode == 1
    assert json.loads(weak.stdout)["passed_gate"] is False
    strong = run_cli("run", "-q", "--pytest-args", "tests_strong", "--fail-under", "90", cwd=EXAMPLE)
    assert strong.returncode == 0, strong.stdout + strong.stderr
    suggest = run_cli("suggest", "-q", "--pytest-args", "tests_weak", "--limit", "3", cwd=EXAMPLE)
    assert suggest.returncode == 0
    assert suggest.stdout.count("\n## ") == 4 and "[failure path]" in suggest.stdout


def test_cli_reports_missing_base_ref(tmp_path):
    write(tmp_path, "m.py", "x = 1\n")
    git(tmp_path, "init", "-q", "-b", "main")
    result = run_cli("run", "--base", "main", cwd=tmp_path)
    assert result.returncode == 3 and "unknown base ref" in result.stderr


def test_mcp_server_speaks_stdio():
    """Start the real server and do the JSON-RPC initialize + tools/list handshake."""
    proc = subprocess.Popen([sys.executable, "-m", "testteeth", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        def send(msg):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        init = json.loads(proc.stdout.readline())
        assert init["result"]["serverInfo"]["name"] == "testteeth"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = json.loads(proc.stdout.readline())
        names = {t["name"] for t in tools["result"]["tools"]}
        assert names == {"grade_tests", "list_surviving_mutants", "suggest_missing_tests"}
    finally:
        proc.kill()
        proc.communicate()
