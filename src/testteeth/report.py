"""Human-readable and JSON renderings of a :class:`GradeReport`."""

from __future__ import annotations

import json

from .models import NO_COVERAGE, GradeReport


def _pct(score: float | None) -> str:
    return "  n/a" if score is None else f"{score:5.1f}%"


def _short(text: str, width: int = 48) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def render_text(report: GradeReport, max_listed: int = 8) -> str:
    scope = f"code changed vs {report.base_ref}" if report.base_ref else "all selected code"
    out = [f"testteeth: mutation grade for {scope}", ""]
    if not report.mutants and not report.gaps:
        out += [*(f"note: {n}" for n in report.notes), "Nothing to grade.", ""]
        return "\n".join(out)
    current_file = None
    for grade in report.functions:
        if grade.file != current_file:
            current_file = grade.file
            out.append(grade.file)
        verdict = "ok" if grade.score is not None and grade.survived == 0 else ("WEAK" if grade.total else "")
        out.append(
            f"  {grade.function:<34} {_pct(grade.score)}  {grade.killed:>3}/{grade.total:<3} killed  {verdict}".rstrip()
        )
        surviving = [m for m in grade.mutants if m.undetected]
        for mutant in surviving[:max_listed]:
            change = f"`{_short(mutant.original, 30)}` -> `{_short(mutant.replacement, 30)}`"
            tag = "not covered" if mutant.status == NO_COVERAGE else "survived"
            out.append(f"      L{mutant.line:<4} {mutant.operator:<17} {change}  [{tag}]")
        if len(surviving) > max_listed:
            out.append(f"      ... {len(surviving) - max_listed} more surviving")
        for gap in grade.gaps:
            out.append(f"      ! L{gap.line:<4} failure path ({gap.kind}): {gap.detail}")
    out.append("")
    out.append(
        f"Mutants: {report.total}  killed: {report.killed}  survived: {report.count('survived')}  "
        f"not covered: {report.count(NO_COVERAGE)}  timeouts: {report.count('timeout')}"
    )
    out.append(f"Failure-path gaps: {len(report.gaps)}")
    gate = ""
    if report.fail_under is not None:
        gate = f"  ({'PASS' if report.passed_gate else 'FAIL'}: --fail-under {report.fail_under:g})"
    out.append(f"Mutation score: {_pct(report.score).strip()}{gate}")
    out.append(f"Time: baseline {report.baseline_seconds:.1f}s, total {report.elapsed_seconds:.1f}s")
    for note in report.notes:
        out.append(f"note: {note}")
    if report.surviving():
        out.append("Next: `testteeth suggest` prints an agent-ready brief for each surviving mutant.")
    return "\n".join(out) + "\n"


def render_json(report: GradeReport, indent: int | None = 2) -> str:
    return json.dumps(report.to_dict(), indent=indent)
