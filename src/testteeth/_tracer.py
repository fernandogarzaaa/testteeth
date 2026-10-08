"""Tiny line tracer loaded into the baseline pytest run with ``-p testteeth._tracer``.

It records which lines of the target files execute while the test suite runs and writes them as
JSON to ``$TESTTEETH_TRACE_OUT``. Only frames whose code lives in ``$TESTTEETH_TRACE_FILES``
(``os.pathsep``-separated absolute paths) are traced line by line, so the overhead is small.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from types import FrameType
from typing import Any

_targets: frozenset[str] = frozenset()
_hits: dict[str, set[int]] = {}


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


def _start() -> None:
    global _targets
    raw = os.environ.get("TESTTEETH_TRACE_FILES", "")
    _targets = frozenset(os.path.realpath(p) for p in raw.split(os.pathsep) if p) | frozenset(
        p for p in raw.split(os.pathsep) if p
    )
    if _targets:
        sys.settrace(_global)
        threading.settrace(_global)


# Start at import time: ``-p`` plugins are imported before conftest files, so module-level code
# of target modules imported by a conftest is still traced.
_start()


def pytest_unconfigure(config: Any) -> None:
    sys.settrace(None)
    threading.settrace(None)  # type: ignore[arg-type]
    out = os.environ.get("TESTTEETH_TRACE_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({name: sorted(lines) for name, lines in _hits.items()}, handle)
