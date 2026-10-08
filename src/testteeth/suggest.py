"""Turn surviving mutants and failure-path gaps into precise, agent-ready test briefs.

No LLM is involved: each brief is assembled from the mutant's operator, the exact source lines,
the guarding condition and the exception / call involved. The brief tells a coding agent which
behaviour a *new* test must pin down so that the mutant would be killed.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import asdict, dataclass
from typing import Any

from . import idioms
from .models import NO_COVERAGE, FailurePathGap, GradeReport, Mutant
from .operators import apply_mutant, matches_source

FAILURE_OPERATORS = frozenset({"remove-raise", "swallow-exception"})

_CALL_KINDS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"retry|backoff|sleep|wait", re.I),
        "retry/backoff",
        "make the first attempt fail transiently and assert the operation is retried (and that it gives up after the "
        "maximum number of attempts)",
    ),
    (
        re.compile(r"seen|dedup|idempot|add$|mark|register|remember", re.I),
        "idempotency/deduplication",
        "call the operation twice with the same key/input and assert the second call is a no-op (no duplicate "
        "side effect, same result)",
    ),
    (
        re.compile(r"save|commit|write|flush|persist|store|insert|update|append|put", re.I),
        "persistence",
        "assert the persisted state after the call (the record/row/file/list actually contains the new value)",
    ),
    (
        re.compile(r"close|release|unlock|cleanup|shutdown|dispose|rollback", re.I),
        "cleanup",
        "assert the resource is released/rolled back, including when the operation fails part-way",
    ),
    (
        re.compile(r"validate|check|verify|ensure|assert", re.I),
        "validation",
        "pass invalid input and assert it is rejected (the validation call is load-bearing)",
    ),
    (
        re.compile(r"send|notify|emit|publish|post|charge|call|request", re.I),
        "outbound side effect",
        "use a fake/mock collaborator and assert it was called with the exact arguments (and exactly once)",
    ),
]


@dataclass
class TestBrief:
    mutant_id: str
    file: str
    function: str
    line: int
    operator: str
    status: str
    original_code: str
    mutated_code: str
    change: str
    why_missed: str
    must_assert: str
    failure_path: bool
    suggested_test_name: str
    context: str
    lang: str = "python"
    test_skeleton: str = ""  # a starting point in the project's test framework (TypeScript / Rust)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lines(source: str, start: int, end: int) -> str:
    return textwrap.dedent("\n".join(source.splitlines()[start - 1 : end])).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


_REL_OP = re.compile(r"<=|>=|(?<![=<>-])<(?![=<])|(?<![=<>-])>(?![=>])")


def _is_boundary(m: Mutant) -> bool:
    if any(s in m.description for s in ("`Lt`", "`LtE`", "`Gt`", "`GtE`")):
        return True
    a, b = _REL_OP.findall(m.original), _REL_OP.findall(m.replacement)
    return len(a) == 1 and len(b) == 1 and a[0].rstrip("=") == b[0].rstrip("=") and a[0] != b[0]


def _why_and_what(m: Mutant, lang: str = "python", line_text: str = "") -> tuple[str, str]:
    ctx = f" (guarded by `{m.context}`)" if m.context else ""
    orig, repl = m.original.strip(), m.replacement.strip()
    if line_text and (len(orig) <= 3 and not re.search(r"\w", orig)):
        # a bare operator token (cargo-mutants): show it in its line
        orig, repl = f"{orig}` in `{line_text.strip()}", repl
    op = m.operator
    if lang != "python":
        special = _native_why_and_what(m, lang, orig, repl, ctx)
        if special is not None:
            return special
    if op == "comparison":
        if _is_boundary(m):
            return (
                f"Changing `{orig}` to `{repl}` only matters when both sides are equal, and no test uses that "
                "boundary value.",
                f"call `{m.function}` with inputs that sit exactly on the boundary of `{orig}` and assert the exact "
                "result (also one value just below and just above it)",
            )
        return (
            f"No test distinguishes the case where `{orig}` is true from the case where it is false.",
            f"add one case where `{orig}` holds and one where it does not, asserting the different outcomes",
        )
    if op == "boolean":
        if "`not`" in m.description:
            return (
                f"Inverting `{orig}` is not detected, so the tests never check the outcome on both sides of that "
                "condition.",
                f"assert the behaviour of `{m.function}` both when `{orig}` is true and when it is false",
            )
        return (
            f"`{orig}` behaves the same as `{repl}` for every input the tests use: no test makes exactly one "
            "operand true.",
            "add cases where only one operand of the boolean expression is true and assert the outcome",
        )
    if op == "arithmetic":
        return (
            f"The result of `{orig}` is not pinned: computing `{repl}` instead still passes.",
            "assert the exact computed value with non-trivial inputs (non-zero, not 1) taken from the spec, not "
            "re-derived from the implementation",
        )
    if op == "constant":
        return (
            f"The tests do not depend on the exact value `{orig}`; `{repl}` passes too (off-by-one is invisible).",
            f"assert behaviour at the exact limit set by `{orig}` (e.g. N allowed, N+1 rejected; or the exact "
            "count/amount)",
        )
    if op == "return-value":
        return (
            f"`{m.function}` can return `None` instead of its real value and every test still passes: the "
            "return value is never asserted on this path" + ctx + ".",
            f"assert the exact value returned by `{m.function}` on this path{ctx}",
        )
    if op == "remove-raise":
        exc = m.exception or "the exception"
        return (
            f"Deleting `{orig}` goes unnoticed: no test triggers this error path{ctx}.",
            f"drive `{m.function}` into this failure{ctx} and assert it raises `{exc}` "
            + idioms.RAISE_ASSERT.get(lang, idioms.RAISE_ASSERT["python"]).format(
                exc=exc.split("(")[0] or "Exception"
            )
            + ", including the message if it matters",
        )
    if op == "swallow-exception":
        exc = m.exception or "the exception"
        return (
            f"The `except {exc}` handler can be replaced by `pass` and nothing fails: no test makes the guarded "
            "code raise and checks what the handler does.",
            f"make the code inside the `try` raise `{exc}` (e.g. "
            + idioms.FAKE_FAILURE.get(lang, idioms.FAKE_FAILURE["python"]).format(
                exc=exc.split(",")[0].strip("() ") if lang == "python" else "Error"
            )
            + ") and assert the handler's observable effect (re-raised error, fallback value, retry, logged/recorded "
            "state)",
        )
    if op == "remove-call":
        for pattern, kind, what in _CALL_KINDS:
            if pattern.search(m.call.rsplit(".", 1)[-1]) or pattern.search(m.call):
                return (
                    f"Removing the call `{m.call}(...)` does not fail any test: its {kind} side effect is never "
                    "verified.",
                    what,
                )
        return (
            f"Removing the call `{m.call}(...)` does not fail any test: its side effect is never verified.",
            "assert the side effect of that call (state change, collaborator called with the right arguments)",
        )
    return (f"Mutation `{m.description}` was not detected.", "assert the behaviour this line implements")


def _native_why_and_what(m: Mutant, lang: str, orig: str, repl: str, ctx: str) -> tuple[str, str] | None:
    """Operators that only native engines (StrykerJS, cargo-mutants) produce, or that read differently there."""
    op = m.operator
    short_orig = " ".join(orig.split())
    if len(short_orig) > 60:
        short_orig = short_orig[:57] + "..."
    if op == "return-value":
        why = f"`{m.function}` can return `{repl}` instead of its real result and every test still passes"
        what = f"assert the exact value returned by `{m.function}` for inputs from the spec{ctx}"
        if m.exception == "Err" and repl.startswith("Ok("):
            why += ": the `Err` paths and the exact `Ok` value are never asserted"
            what += ", and add a case that makes it return `Err` (`assert_eq!(.., Err(..))`)"
        return why + ".", what
    if op == "condition":
        return (
            f"`{short_orig}` can be replaced by `{repl}` and every test still passes: no test takes both branches"
            f"{ctx}.",
            f"add one case where `{short_orig}` is true and one where it is false, asserting the different outcomes",
        )
    if op == "remove-block":
        return (
            f"The block `{short_orig}` can be emptied and nothing fails: its effect is never asserted{ctx}.",
            f"drive `{m.function}` through this block{ctx} and assert its observable effect (returned value, thrown "
            "error, state change, collaborator call)",
        )
    if op == "string":
        return (
            f"The string `{short_orig}` can be replaced by `{repl}` without failing a test.",
            "assert the exact text the code produces here (error message, key, URL or format)",
        )
    if op == "literal":
        return (
            f"The value `{short_orig}` can become `{repl}` unnoticed.",
            "assert the exact contents of this value (elements, fields), not just its type or length",
        )
    if op == "method":
        return (
            f"Replacing `{short_orig}` with `{repl}` is not detected.",
            "assert the result of this call with inputs where the two methods behave differently",
        )
    if op == "remove-branch":
        return (
            f"Deleting the match arm `{short_orig}` goes unnoticed: no test reaches it or checks its result.",
            "add a test whose input takes this arm and assert its outcome",
        )
    return None


def _function_is_async(source: str, mutant: Mutant, lang: str) -> bool:
    if lang == "python":
        return False
    from .languages.scan import scan

    try:
        found = [f for f in scan(source, lang).functions if f.qualname == mutant.function]
    except Exception:  # pragma: no cover - the scanner is best-effort
        return False
    return bool(found and found[0].is_async)


def brief_for(mutant: Mutant, source: str, lang: str = "python") -> TestBrief:
    """Build the brief for one surviving mutant, given the original source of its file."""
    mutated_source = apply_mutant(source, mutant)
    delta = len(mutated_source.splitlines()) - len(source.splitlines())
    original_code = _lines(source, mutant.line, mutant.end_line)
    mutated_code = _lines(mutated_source, mutant.line, mutant.end_line + delta)
    line_text = source.splitlines()[mutant.line - 1] if 0 < mutant.line <= len(source.splitlines()) else ""
    why, what = _why_and_what(mutant, lang, line_text)
    if mutant.status == NO_COVERAGE:
        where = f" inside `{mutant.context}`" if mutant.context else ""
        why = f"No test executes line {mutant.line}{where} at all, so any bug here is invisible. " + why
        what = f"first reach this code: call `{mutant.function}` with inputs that take the path{where}; then {what}"
    short = mutant.function.rsplit(".", 1)[-1].lstrip("_") or "module"
    failure = (
        mutant.operator in FAILURE_OPERATORS
        or mutant.context.startswith(("except", "catch"))
        or (lang == "rust" and ("Err" in mutant.context or "Err(" in mutant.original or mutant.exception == "Err"))
    )
    if lang == "python":
        name = f"test_{_slug(short)}_{_slug(mutant.operator)}_line_{mutant.line}"
        skeleton = ""
    else:
        name = idioms.test_name(lang, mutant.function, mutant.operator, mutant.line)
        skeleton = idioms.skeleton(
            lang, name=name, function=mutant.function, file=mutant.file, line=mutant.line, failure=failure,
            is_async=_function_is_async(source, mutant, lang),
        )
    return TestBrief(
        mutant_id=mutant.id,
        file=mutant.file,
        function=mutant.function,
        line=mutant.line,
        operator=mutant.operator,
        status=mutant.status,
        original_code=original_code,
        mutated_code=mutated_code,
        change=mutant.description,
        why_missed=why,
        must_assert=what,
        failure_path=failure,
        suggested_test_name=name,
        context=mutant.context,
        lang=lang,
        test_skeleton=skeleton,
    )


def gap_brief(gap: FailurePathGap) -> dict[str, Any]:
    data = gap.to_dict()
    data["must_assert"] = idioms.gap_guidance(gap.lang, gap.kind)
    data["heuristic"] = True
    return data


def build_briefs(report: GradeReport, sources: dict[str, str]) -> list[TestBrief]:
    """Briefs for every surviving mutant in ``report``; ``sources`` maps file -> original source.

    Mutants whose file is missing or was edited since grading (stale span) are skipped.
    """
    briefs = [
        brief_for(m, sources[m.file], report.lang)
        for m in report.surviving()
        if m.file in sources and matches_source(sources[m.file], m)
    ]
    briefs.sort(key=lambda b: (not b.failure_path, b.file, b.line))
    return briefs


_FENCE = {"python": "python", "typescript": "ts", "rust": "rust"}


def render_markdown(briefs: list[TestBrief], gaps: list[FailurePathGap], limit: int | None = None) -> str:
    """Render briefs as Markdown a coding agent can act on directly."""
    if not briefs and not gaps:
        return "No surviving mutants and no failure-path gaps: the tests already pin this code down.\n"
    out = [
        "# Missing tests (testteeth)",
        "",
        "Write tests from the *specification*, not by re-reading the implementation. Each item below is a "
        "concrete bug that the current suite lets through; a new test must fail on the mutated code and pass on "
        "the original.",
        "",
    ]
    shown = briefs[:limit] if limit else briefs
    for i, brief in enumerate(shown, start=1):
        tag = " [failure path]" if brief.failure_path else ""
        fence = _FENCE.get(brief.lang, "")
        out += [
            f"## {i}. `{brief.function}` - {brief.file}:{brief.line}{tag}",
            f"- Mutant `{brief.mutant_id}` ({brief.operator}, {brief.status}): {brief.change}",
            "- Original:",
            f"  ```{fence}",
            *[f"  {line}" for line in brief.original_code.splitlines()],
            "  ```",
            "- Mutated (tests still pass):",
            f"  ```{fence}",
            *[f"  {line}" for line in brief.mutated_code.splitlines()],
            "  ```",
            f"- Why the tests miss it: {brief.why_missed}",
            f"- A new test must: {brief.must_assert}.",
            f"- Suggested name: `{brief.suggested_test_name}`",
        ]
        if brief.test_skeleton:
            out += ["- Skeleton:", f"  ```{fence}", *[f"  {line}".rstrip() for line in brief.test_skeleton.splitlines()],
                    "  ```"]
        out.append("")
    if limit and len(briefs) > limit:
        out += [f"_...and {len(briefs) - limit} more surviving mutants (raise the limit to see them)._", ""]
    if gaps:
        out += ["## Failure paths no test exercises (heuristic)", ""]
        for gap in gaps:
            out.append(f"- `{gap.function}` {gap.file}:{gap.line} [{gap.kind}] {gap.detail}. "
                       f"Test: {gap_brief(gap)['must_assert']}.")
        out.append("")
    return "\n".join(out)
