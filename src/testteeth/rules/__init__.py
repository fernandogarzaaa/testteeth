"""Heuristic failure-path rule packs for languages graded through native engines.

Each pack scans the selected functions of a source file for constructs that imply a failure path and reports
the ones the test suite never drives, mirroring the Python checks in :mod:`testteeth.failure_paths`:

* **typescript** (also JavaScript): ``catch`` blocks, ``throw``, Promise ``.catch(...)``, ``fetch`` / axios /
  HTTP-client calls, ``setTimeout`` / ``AbortController`` timeouts and retry loops.
* **rust**: ``Err(...)`` returns and ``?`` propagation, ``match`` / ``if let`` ``Err`` arms,
  ``unwrap()`` / ``expect()`` on fallible calls, timeouts, and reqwest / tokio / std I/O calls.

Two signals decide whether a path is exercised: line coverage derived from the engine (StrykerJS reports
``NoCoverage`` per mutant; cargo-mutants has no coverage data) and the text of the tests (a test that mentions
the function *and* sets up or expects a failure). Everything here is pattern based and labelled heuristic.
"""

from __future__ import annotations

from ..languages.scan import Scan
from ..models import FailurePathGap


def analyze(
    scan: Scan,
    rel_path: str,
    test_bodies: list[str],
    selected: set[str] | None = None,
    coverage: dict[int, bool] | None = None,
) -> list[FailurePathGap]:
    """Failure-path gaps for one scanned TypeScript/JavaScript or Rust file."""
    if scan.lang == "rust":
        from .rust import analyze_rust

        return analyze_rust(scan, rel_path, test_bodies, selected)
    from .typescript import analyze_typescript

    return analyze_typescript(scan, rel_path, test_bodies, selected, coverage)


def test_bodies(source: str, lang: str) -> list[str]:
    """Split a test file into the bodies of its individual tests (whole file if none are found)."""
    if lang == "rust":
        from .rust import rust_test_bodies

        return rust_test_bodies(source)
    from .typescript import ts_test_bodies

    return ts_test_bodies(source)


test_bodies.__test__ = False  # type: ignore[attr-defined]  # not a pytest test when imported into test modules
