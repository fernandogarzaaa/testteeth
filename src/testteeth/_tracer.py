"""Tiny line tracer loaded into the baseline pytest run with ``-p testteeth._tracer``.

It records which lines of the target files execute while the test suite runs and writes them as
JSON to ``$TESTTEETH_TRACE_OUT``. Only code that lives in ``$TESTTEETH_TRACE_FILES``
(``os.pathsep``-separated absolute paths) is recorded, so the overhead is small.

On Python 3.12+ the tracer uses :mod:`sys.monitoring` under its own tool id, so it keeps working next to
coverage.py / pytest-cov and debuggers. Older interpreters fall back to :func:`sys.settrace`, which another
tracer can replace (the engine therefore runs the suite with ``--no-cov`` when pytest-cov is installed).

pytest-xdist workers are separate processes: each one writes its own ``$TESTTEETH_TRACE_OUT.<worker>``
fragment and the engine merges every fragment it finds.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from types import CodeType, FrameType
from typing import Any

_targets: frozenset[str] = frozenset()
_hits: dict[str, set[int]] = {}
_tool_id: int | None = None  # sys.monitoring tool id in use, if any
_settrace = False


# ------------------------------------------------------------------ sys.settrace (Python < 3.12)
def _local(frame: FrameType, event: str, arg: Any) -> Any:
    if event == "line":
        _hits.setdefault(frame.f_code.co_filename, set()).add(frame.f_lineno)
    return _local


def _global(frame: FrameType, event: str, arg: Any) -> Any:
    filename = frame.f_code.co_filename
    if filename in _targets:
        _hits.setdefault(filename, set()).add(frame.f_lineno)
        return _local
    return None


# ------------------------------------------------------------------ sys.monitoring (Python 3.12+)
def _on_line(code: CodeType, line: int) -> Any:
    filename = code.co_filename
    if filename in _targets:
        _hits.setdefault(filename, set()).add(line)
    # One hit per location is all that is needed: disabling it keeps the overhead near zero.
    return sys.monitoring.DISABLE


def _on_start(code: CodeType, offset: int) -> Any:
    # Mirrors the settrace "call" event: entering a function marks its ``def`` line as executed.
    filename = code.co_filename
    if filename in _targets:
        _hits.setdefault(filename, set()).add(code.co_firstlineno)
    return sys.monitoring.DISABLE


def _start_monitoring() -> bool:
    global _tool_id
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        return False
    for tool_id in (4, 3):  # the ids no well-known tool (debugger, coverage, profiler, optimizer) claims
        try:
            monitoring.use_tool_id(tool_id, "testteeth")
        except ValueError:
            continue
        events = monitoring.events
        monitoring.register_callback(tool_id, events.LINE, _on_line)
        monitoring.register_callback(tool_id, events.PY_START, _on_start)
        monitoring.set_events(tool_id, events.LINE | events.PY_START)
        _tool_id = tool_id
        return True
    return False


def _start() -> None:
    global _targets, _settrace
    raw = os.environ.get("TESTTEETH_TRACE_FILES", "")
    _targets = frozenset(os.path.realpath(p) for p in raw.split(os.pathsep) if p) | frozenset(
        p for p in raw.split(os.pathsep) if p
    )
    if _targets and not _start_monitoring():
        _settrace = True
        sys.settrace(_global)
        threading.settrace(_global)


def _stop() -> None:
    global _tool_id, _settrace
    if _tool_id is not None:
        monitoring = sys.monitoring
        monitoring.set_events(_tool_id, 0)
        monitoring.register_callback(_tool_id, monitoring.events.LINE, None)
        monitoring.register_callback(_tool_id, monitoring.events.PY_START, None)
        monitoring.free_tool_id(_tool_id)
        _tool_id = None
    if _settrace:
        sys.settrace(None)
        threading.settrace(None)  # type: ignore[arg-type]
        _settrace = False


# Start at import time: ``-p`` plugins are imported before conftest files, so module-level code
# of target modules imported by a conftest is still traced.
_start()


def pytest_unconfigure(config: Any) -> None:
    _stop()
    out = os.environ.get("TESTTEETH_TRACE_OUT")
    if out:
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        if worker:  # one fragment per xdist worker process; the engine merges them
            out = f"{out}.{worker}"
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({name: sorted(lines) for name, lines in _hits.items()}, handle)
