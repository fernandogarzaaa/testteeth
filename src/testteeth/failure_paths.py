"""Heuristic failure-path coverage.

AI-written tests overwhelmingly exercise the happy path. This module looks at each selected
function for constructs that imply a failure path -- ``try``/``except`` handlers, ``raise``
statements, retry loops, timeouts, calls to the network / filesystem / subprocesses -- and
reports the ones the test suite never drives:

* an ``except`` body or ``raise`` line that the baseline test run never executed, or
* a function with retries / timeouts / external calls where no test that mentions the
  function also simulates a failure (``pytest.raises``, ``side_effect``, ``Timeout``...).

It is a heuristic, not a proof: it reports likely gaps cheaply and without running mutants.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

from .models import FailurePathGap

EXTERNAL_ROOTS = frozenset(
    {
        "requests",
        "httpx",
        "urllib",
        "urllib3",
        "aiohttp",
        "socket",
        "subprocess",
        "smtplib",
        "ftplib",
        "boto3",
        "botocore",
        "sqlite3",
        "psycopg",
        "psycopg2",
        "pymongo",
        "redis",
        "grpc",
        "shutil",
    }
)
EXTERNAL_BUILTINS = frozenset({"open", "urlopen"})
EXTERNAL_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "request", "send", "fetch", "execute", "commit", "connect", "charge"}
)
CLIENT_HINTS = ("client", "session", "conn", "gateway", "api", "http", "db", "cursor", "socket", "transport")
RETRY_WORDS = re.compile(r"retr(y|ies)|attempt|backoff|tries", re.IGNORECASE)
TIMEOUT_WORDS = re.compile(r"timeout|timed_out|deadline", re.IGNORECASE)
FAILURE_TEST_WORDS = re.compile(
    r"pytest\.raises|assertRaises|side_effect|\braise\b|\b[A-Z]\w*(?:Error|Exception|Timeout)\b|\bTimeout\b"
)


def _root_name(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else ""


def external_call_name(call: ast.Call) -> str | None:
    """Return a display name if ``call`` looks like I/O against something that can fail."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in EXTERNAL_BUILTINS:
        return func.id
    if isinstance(func, ast.Attribute):
        root = _root_name(func)
        dotted = ast.unparse(func)
        if root in EXTERNAL_ROOTS:
            return dotted
        receiver = ast.unparse(func.value).lower()
        if func.attr in EXTERNAL_METHODS and any(hint in receiver for hint in CLIENT_HINTS):
            return dotted
    return None


def _has_timeout(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.keyword) and node.arg and TIMEOUT_WORDS.search(node.arg):
            return True
        if isinstance(node, ast.Name) and TIMEOUT_WORDS.search(node.id):
            return True
        if isinstance(node, ast.arg) and TIMEOUT_WORDS.search(node.arg):
            return True
    return False


def _retry_loops(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    loops: list[ast.stmt] = []
    func_hint = bool(RETRY_WORDS.search(func.name))
    for node in ast.walk(func):
        if isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            text = ast.unparse(node.target if isinstance(node, (ast.For, ast.AsyncFor)) else node.test)
            if isinstance(node, (ast.For, ast.AsyncFor)):
                text += " " + ast.unparse(node.iter)
            has_try = any(isinstance(n, ast.Try) for n in ast.walk(node))
            if RETRY_WORDS.search(text) or (func_hint and has_try):
                loops.append(node)
    return loops


def _test_functions(test_sources: Iterable[str]) -> list[str]:
    bodies: list[str] = []
    for source in test_sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                bodies.append("\n".join(lines[node.lineno - 1 : node.end_lineno]))
    return bodies


def failure_tested(function_name: str, test_bodies: list[str]) -> bool:
    """True if some test mentions the function and also sets up or expects a failure."""
    short = function_name.rsplit(".", 1)[-1]
    pattern = re.compile(rf"\b{re.escape(short)}\b")
    return any(pattern.search(body) and FAILURE_TEST_WORDS.search(body) for body in test_bodies)


def is_stub(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A body of only a docstring, ``pass``, ``...`` or ``raise NotImplementedError`` (protocols, ABCs)."""
    body = [s for s in func.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if not body:
        return True
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return True
    return len(body) == 1 and isinstance(body[0], ast.Raise) and "NotImplementedError" in ast.unparse(body[0])


def analyze_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    qualname: str,
    rel_path: str,
    covered: set[int] | None,
    test_bodies: list[str],
) -> list[FailurePathGap]:
    """Return failure-path gaps for one function.

    ``covered`` is the set of executed line numbers in the file from the baseline run, or
    ``None`` when no coverage data exists (then only the static/test-text checks apply).
    """
    gaps: list[FailurePathGap] = []
    if is_stub(func):
        return gaps

    def gap(kind: str, line: int, detail: str) -> None:
        gaps.append(FailurePathGap(rel_path, qualname, func.lineno, kind, line, detail))

    handler_lines: set[int] = set()
    nested = {
        id(n)
        for child in ast.walk(func)
        if child is not func and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for n in ast.walk(child)
    }
    own = [n for n in ast.walk(func) if id(n) not in nested]

    for node in own:
        if isinstance(node, ast.ExceptHandler):
            exc = ast.unparse(node.type) if node.type is not None else "BaseException"
            body_lines = {n.lineno for b in node.body for n in ast.walk(b) if hasattr(n, "lineno")}
            handler_lines |= body_lines
            if covered is not None and not (body_lines & covered):
                gap("except-branch", node.body[0].lineno, f"`except {exc}` branch is never executed by any test")
    for node in own:
        if isinstance(node, ast.Raise) and node.lineno not in handler_lines:
            if covered is not None and node.lineno not in covered:
                what = ast.unparse(node.exc) if node.exc is not None else "re-raise"
                if len(what) > 60:
                    what = what[:57] + "..."
                gap("raise", node.lineno, f"`raise {what}` is never triggered by any test")

    handler_ran = covered is not None and bool(handler_lines & covered)
    exercised = handler_ran or failure_tested(qualname, test_bodies)
    retry_loops = [loop for loop in _retry_loops(func) if id(loop) not in nested]
    if retry_loops and not exercised:
        gap(
            "retry",
            retry_loops[0].lineno,
            "retry loop: no test makes an attempt fail and checks the retry / give-up behaviour",
        )
    if _has_timeout(func) and not exercised:
        gap("timeout", func.lineno, "uses a timeout but no test simulates the timeout expiring")
    if not exercised:
        calls = [external_call_name(n) for n in own if isinstance(n, ast.Call)]
        names = sorted({c for c in calls if c})
        if names:
            first = next(n for n in own if isinstance(n, ast.Call) and external_call_name(n) == names[0])
            gap(
                "external-call",
                first.lineno,
                f"calls `{'`, `'.join(names[:3])}` but no test simulates that call failing",
            )
    gaps.sort(key=lambda g: g.line)
    return gaps


def analyze_module(
    source: str,
    rel_path: str,
    covered: set[int] | None,
    test_sources: Iterable[str],
    functions: set[str] | None = None,
) -> list[FailurePathGap]:
    """Analyse every function (or only those in ``functions``) of one module."""
    from .operators import iter_functions

    tree = ast.parse(source)
    bodies = _test_functions(test_sources)
    gaps: list[FailurePathGap] = []
    for qualname, node in iter_functions(tree):
        if functions is not None and qualname not in functions:
            continue
        gaps.extend(analyze_function(node, qualname, rel_path, covered, bodies))
    return gaps
