"""Plain data structures shared by the engine, reporters, CLI, pytest plugin and MCP server."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Mutant statuses
KILLED = "killed"  # at least one test failed -> the tests noticed the bug
SURVIVED = "survived"  # every test still passed -> a gap in the tests
NO_COVERAGE = "no_coverage"  # no test even executes the mutated line (counts as survived)
TIMEOUT = "timeout"  # the mutant hung the suite (counts as killed)

DETECTED = frozenset({KILLED, TIMEOUT})
UNDETECTED = frozenset({SURVIVED, NO_COVERAGE})


@dataclass
class Mutant:
    """A single, small, syntactically valid change to one source file."""

    id: str
    file: str  # path relative to the project root, POSIX separators
    function: str  # qualified name of the enclosing function, or "<module>"
    function_line: int
    line: int
    col: int
    end_line: int
    end_col: int
    operator: str
    description: str
    original: str  # source text that is replaced
    replacement: str  # text it is replaced with
    context: str = ""  # nearest guarding construct, e.g. "if qty <= 0:"
    exception: str = ""  # exception type involved (raise / except mutants)
    call: str = ""  # dotted call name (remove-call mutants)
    status: str = "pending"
    duration: float = 0.0
    detail: str = ""

    @property
    def detected(self) -> bool:
        return self.status in DETECTED

    @property
    def undetected(self) -> bool:
        return self.status in UNDETECTED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FailurePathGap:
    """A failure path (error branch, retry, timeout, external call) no test exercises."""

    file: str
    function: str
    function_line: int
    kind: str  # except-branch | raise | retry | timeout | external-call
    line: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FunctionGrade:
    file: str
    function: str
    line: int
    mutants: list[Mutant] = field(default_factory=list)
    gaps: list[FailurePathGap] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.mutants)

    @property
    def killed(self) -> int:
        return sum(1 for m in self.mutants if m.detected)

    @property
    def survived(self) -> int:
        return sum(1 for m in self.mutants if m.undetected)

    @property
    def score(self) -> float | None:
        return round(100.0 * self.killed / self.total, 1) if self.total else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "function": self.function,
            "line": self.line,
            "score": self.score,
            "total": self.total,
            "killed": self.killed,
            "survived": self.survived,
            "surviving_mutants": [m.id for m in self.mutants if m.undetected],
            "failure_path_gaps": [g.to_dict() for g in self.gaps],
        }


@dataclass
class GradeReport:
    root: str
    base_ref: str | None
    test_command: list[str]
    mutants: list[Mutant] = field(default_factory=list)
    gaps: list[FailurePathGap] = field(default_factory=list)
    functions: list[FunctionGrade] = field(default_factory=list)
    baseline_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    fail_under: float | None = None
    notes: list[str] = field(default_factory=list)

    def count(self, *statuses: str) -> int:
        return sum(1 for m in self.mutants if m.status in statuses)

    @property
    def total(self) -> int:
        return len(self.mutants)

    @property
    def killed(self) -> int:
        return sum(1 for m in self.mutants if m.detected)

    @property
    def score(self) -> float | None:
        return round(100.0 * self.killed / self.total, 1) if self.total else None

    @property
    def passed_gate(self) -> bool:
        if self.fail_under is None or self.score is None:
            return True
        return self.score >= self.fail_under

    def surviving(self) -> list[Mutant]:
        return [m for m in self.mutants if m.undetected]

    def mutant(self, mutant_id: str) -> Mutant | None:
        return next((m for m in self.mutants if m.id == mutant_id), None)

    def to_dict(self) -> dict[str, Any]:
        from . import __version__

        return {
            "tool": "testteeth",
            "version": __version__,
            "root": self.root,
            "base_ref": self.base_ref,
            "test_command": self.test_command,
            "score": self.score,
            "total": self.total,
            "killed": self.count(KILLED),
            "timeouts": self.count(TIMEOUT),
            "survived": self.count(SURVIVED),
            "no_coverage": self.count(NO_COVERAGE),
            "fail_under": self.fail_under,
            "passed_gate": self.passed_gate,
            "baseline_seconds": round(self.baseline_seconds, 3),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "notes": self.notes,
            "functions": [f.to_dict() for f in self.functions],
            "mutants": [m.to_dict() for m in self.mutants],
            "failure_path_gaps": [g.to_dict() for g in self.gaps],
        }
