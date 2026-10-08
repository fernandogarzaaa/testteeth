from __future__ import annotations

import pytest

from testteeth.models import FailurePathGap, GradeReport
from testteeth.operators import generate_mutants
from testteeth.suggest import brief_for, build_briefs, gap_brief, render_markdown

SRC = '''\
import time


def place(ledger, key, qty, limit=3):
    if qty <= 0 or qty > limit:
        raise ValueError("bad qty")
    if key in ledger.seen:
        return False
    ledger.seen.add(key)
    ledger.save()
    total = qty * 2
    try:
        ledger.client.send(key)
    except TimeoutError:
        time.sleep(1)
        ledger.retry(key)
    return not total
'''


@pytest.fixture
def mutants():
    ms = generate_mutants(SRC, "shop.py")
    for m in ms:
        m.status = "survived"
    return ms


def find(mutants, operator, contains):
    return next(m for m in mutants if m.operator == operator and contains in (m.original + m.call))


def test_boundary_comparison_brief(mutants):
    b = brief_for(find(mutants, "comparison", "qty <= 0"), SRC)
    assert b.original_code == "if qty <= 0 or qty > limit:"
    assert b.mutated_code == "if (qty < 0) or qty > limit:"
    assert "boundary" in b.why_missed and "exactly on the boundary" in b.must_assert
    assert b.suggested_test_name == "test_place_comparison_line_5"
    assert not b.failure_path


def test_equality_comparison_brief(mutants):
    b = brief_for(find(mutants, "comparison", "key in"), SRC)
    assert "distinguishes" in b.why_missed and "one case where" in b.must_assert


def test_boolean_briefs(mutants):
    b = brief_for(find(mutants, "boolean", "qty <= 0 or"), SRC)
    assert "exactly one operand" in b.why_missed
    nb = brief_for(find(mutants, "boolean", "not total"), SRC)
    assert "Inverting" in nb.why_missed and "both when" in nb.must_assert


def test_arithmetic_constant_and_return_briefs(mutants):
    assert "not pinned" in brief_for(find(mutants, "arithmetic", "qty * 2"), SRC).why_missed
    assert "exact limit" in brief_for(find(mutants, "constant", "3"), SRC).must_assert
    rb = brief_for(find(mutants, "return-value", "return False"), SRC)
    assert "return `None`" in rb.why_missed and "guarded by `if key in ledger.seen:`" in rb.why_missed


def test_raise_brief_is_failure_path(mutants):
    b = brief_for(find(mutants, "remove-raise", "raise"), SRC)
    assert b.failure_path
    assert "pytest.raises(ValueError)" in b.must_assert
    assert b.mutated_code == "pass"


def test_swallow_brief_multiline_code_is_dedented(mutants):
    b = brief_for(find(mutants, "swallow-exception", "time.sleep"), SRC)
    assert b.failure_path
    assert b.original_code == "time.sleep(1)\nledger.retry(key)"
    assert b.mutated_code == "pass"
    assert "side_effect=TimeoutError(...)" in b.must_assert


@pytest.mark.parametrize(
    ("call", "kind"),
    [
        ("ledger.seen.add", "idempotency"),
        ("ledger.save", "persistence"),
        ("time.sleep", "retry/backoff"),
        ("ledger.retry", "retry/backoff"),
        ("ledger.client.send", "outbound side effect"),
    ],
)
def test_remove_call_classification(mutants, call, kind):
    m = next(m for m in mutants if m.operator == "remove-call" and m.call == call)
    assert kind in brief_for(m, SRC).why_missed


def test_remove_call_unknown_kind_and_cleanup_validation(make_mutant):
    src = "def f(x):\n    x.frobnicate()\n    x.close()\n    x.validate()\n"
    ms = generate_mutants(src, "f.py")
    texts = {m.call: brief_for(m, src).why_missed for m in ms}
    assert "side effect is never verified" in texts["x.frobnicate"]
    assert "cleanup" in texts["x.close"]
    assert "validation" in texts["x.validate"]


def test_no_coverage_prefix(mutants):
    m = find(mutants, "constant", "2")
    m.status = "no_coverage"
    b = brief_for(m, SRC)
    assert b.why_missed.startswith("No test executes line 11")
    assert b.must_assert.startswith("first reach this code")


def test_unknown_operator_fallback(make_mutant):
    m = make_mutant(operator="weird", description="something odd")
    b = brief_for(m, "def f(a, b):\n    return a < b\n")
    assert "something odd" in b.why_missed


def test_build_briefs_orders_failure_paths_first_and_skips_killed(mutants):
    mutants[0].status = "killed"
    report = GradeReport(root="/", base_ref=None, test_command=[], mutants=mutants)
    briefs = build_briefs(report, {"shop.py": SRC})
    assert len(briefs) == len(mutants) - 1
    flags = [b.failure_path for b in briefs]
    assert flags == sorted(flags, reverse=True)
    assert build_briefs(report, {}) == []


def test_render_markdown_limits_and_lists_gaps(mutants):
    report = GradeReport(root="/", base_ref=None, test_command=[], mutants=mutants)
    briefs = build_briefs(report, {"shop.py": SRC})
    gap = FailurePathGap("shop.py", "place", 4, "retry", 16, "retry loop")
    md = render_markdown(briefs, [gap], limit=2)
    assert md.count("\n## ") == 2 + 1  # two briefs + the gaps section
    assert f"and {len(briefs) - 2} more surviving mutants" in md
    assert "[retry] retry loop" in md
    assert "```python" in md


def test_render_markdown_when_nothing_to_do():
    assert "already pin" in render_markdown([], [])


@pytest.mark.parametrize("kind", ["except-branch", "raise", "retry", "timeout", "external-call", "other"])
def test_gap_brief_guidance(kind):
    data = gap_brief(FailurePathGap("a.py", "f", 1, kind, 2, "d"))
    assert data["kind"] == kind and data["must_assert"]


def test_build_briefs_skips_stale_mutants(mutants):
    report = GradeReport(root="/", base_ref=None, test_command=[], mutants=mutants)
    edited = SRC.replace("qty <= 0", "qty < 1")
    briefs = build_briefs(report, {"shop.py": edited})
    assert 0 < len(briefs) < len(mutants)
    assert all("qty <= 0" not in b.original_code for b in briefs)
    assert build_briefs(report, {"shop.py": "x = 1\n"}) == []
