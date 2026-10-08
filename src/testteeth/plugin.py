"""pytest plugin: ``pytest --testteeth`` grades the suite right after it passes.

Options::

    --testteeth                    run mutation testing after a green test session
    --testteeth-base=REF           only mutate functions changed vs REF (git diff aware)
    --testteeth-fail-under=SCORE   fail the session if the mutation score is below SCORE
    --testteeth-paths=PATHS        comma-separated source paths to mutate
    --testteeth-json=FILE          write the JSON report to FILE

The mutant runs reuse the same pytest arguments you passed (minus the ``--testteeth*`` options).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("testteeth", "mutation grading of the test suite")
    group.addoption("--testteeth", action="store_true", default=False, help="grade the tests by mutation testing")
    group.addoption("--testteeth-base", default=None, help="only mutate code changed vs this git ref")
    group.addoption("--testteeth-fail-under", type=float, default=None, help="minimum mutation score (0-100)")
    group.addoption("--testteeth-paths", default=None, help="comma-separated source paths to mutate")
    group.addoption("--testteeth-json", default=None, help="write the JSON report to this file")


def _forwarded_args(args: list[str]) -> list[str]:
    """Drop testteeth's own options (both ``--opt value`` and ``--opt=value`` forms)."""
    with_value = {"--testteeth-base", "--testteeth-fail-under", "--testteeth-paths", "--testteeth-json"}
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        name = arg.split("=", 1)[0]
        if name == "--testteeth":
            continue
        if name in with_value:
            skip = "=" not in arg
            continue
        out.append(arg)
    return out


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if not config.getoption("testteeth") or os.environ.get("TESTTEETH_ACTIVE"):
        return
    if exitstatus != 0:
        config._testteeth_message = "testteeth: skipped, the test suite did not pass"  # type: ignore[attr-defined]
        return
    from .config import ConfigError, load_settings
    from .diff import GitError
    from .engine import EngineError, grade
    from .report import render_json, render_text

    paths = config.getoption("testteeth_paths")
    try:
        settings = load_settings(
            config.rootpath,
            base_ref=config.getoption("testteeth_base"),
            fail_under=config.getoption("testteeth_fail_under"),
            paths=[p.strip() for p in paths.split(",") if p.strip()] if paths else None,
            test_args=_forwarded_args(list(config.invocation_params.args)),
        )
        report = grade(settings)
    except (ConfigError, EngineError, GitError) as exc:
        config._testteeth_message = f"testteeth: error: {exc}"  # type: ignore[attr-defined]
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    json_path = config.getoption("testteeth_json")
    if json_path:
        with open(json_path, "w", encoding="utf-8") as handle:
            handle.write(render_json(report))
    config._testteeth_message = render_text(report)  # type: ignore[attr-defined]
    if not report.passed_gate:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    message = getattr(config, "_testteeth_message", None)
    if message:
        terminalreporter.section("testteeth")
        terminalreporter.write(message if message.endswith("\n") else message + "\n")
