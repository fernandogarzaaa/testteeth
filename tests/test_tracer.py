"""The tracer is exercised in a fresh interpreter: it installs a global trace function, and
reloading it inside this process would clobber any tracer already running (including testteeth's
own when it grades this suite)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import write

SCRIPT = """
import sys
sys.path.insert(0, {root!r})
import testteeth._tracer as tracer   # starts tracing at import time
import other_mod, target_mod
target_mod.f(True)
other_mod.g()
tracer.pytest_unconfigure(None)
print(sys.gettrace())
"""


def run(tmp_path, env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TESTTEETH_")}
    env.update(env_extra)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), *sys.path])
    return subprocess.run([sys.executable, "-c", SCRIPT.format(root=str(tmp_path))], env=env,
                          capture_output=True, text=True, timeout=60, check=True)


def test_tracer_records_only_target_files(tmp_path):
    target = write(tmp_path, "target_mod.py", "def f(x):\n    if x:\n        return 1\n    return 2\n")
    write(tmp_path, "other_mod.py", "def g():\n    return 3\n")
    out = tmp_path / "trace.json"
    proc = run(tmp_path, {"TESTTEETH_TRACE_FILES": str(target), "TESTTEETH_TRACE_OUT": str(out)})
    assert proc.stdout.strip() == "None"  # tracing stopped
    data = json.loads(out.read_text())
    assert list(data) == [str(target)]
    lines = set(data[str(target)])
    assert {1, 2, 3} <= lines and 4 not in lines  # `return 2` never ran


def test_tracer_is_inert_without_targets(tmp_path):
    write(tmp_path, "target_mod.py", "def f(x):\n    return x\n")
    write(tmp_path, "other_mod.py", "def g():\n    return 3\n")
    out = tmp_path / "trace.json"
    run(tmp_path, {"TESTTEETH_TRACE_OUT": str(out)})
    assert json.loads(out.read_text()) == {}
