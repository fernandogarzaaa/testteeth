"""AST-driven mutation operators.

Each mutant is described as a text replacement of one exact source span, so the rest of the
file (formatting, comments, line numbers above the span) is untouched. Every generated mutant is
compiled before it is returned; invalid ones are dropped.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Iterator
from dataclasses import dataclass

from .models import Mutant

COMPARISON_SWAPS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

ARITHMETIC_SWAPS: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult,
    ast.Mod: ast.FloorDiv,
    ast.Pow: ast.Mult,
}

#: Call roots whose removal is (almost always) unobservable: logging and printing.
QUIET_CALL_ROOTS = frozenset({"print", "logging", "logger", "log", "_log", "_logger", "LOGGER", "LOG"})

ALL_OPERATORS = (
    "comparison",
    "boolean",
    "arithmetic",
    "constant",
    "return-value",
    "remove-raise",
    "swallow-exception",
    "remove-call",
)


@dataclass
class _Span:
    line: int
    col: int  # UTF-8 byte offset, as reported by ast
    end_line: int
    end_col: int


def _span(node: ast.AST) -> _Span:
    return _Span(node.lineno, node.col_offset, node.end_lineno or node.lineno, node.end_col_offset or 0)  # type: ignore[attr-defined]


def _char_index(lines: list[str], line: int, byte_col: int) -> int:
    """Convert a 1-based line and UTF-8 byte column into an index into the joined source."""
    offset = sum(len(text) for text in lines[: line - 1])
    return offset + len(lines[line - 1].encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


def apply_mutant(source: str, mutant: Mutant) -> str:
    """Return ``source`` with the mutant's span replaced by its replacement text."""
    lines = source.splitlines(keepends=True)
    start = _char_index(lines, mutant.line, mutant.col)
    end = _char_index(lines, mutant.end_line, mutant.end_col)
    return source[:start] + mutant.replacement + source[end:]


def matches_source(source: str, mutant: Mutant) -> bool:
    """True if ``source`` still contains the mutant's original text at its recorded span."""
    lines = source.splitlines(keepends=True)
    if not 1 <= mutant.line <= mutant.end_line <= len(lines):
        return False
    start = _char_index(lines, mutant.line, mutant.col)
    end = _char_index(lines, mutant.end_line, mutant.end_col)
    return source[start:end] == mutant.original


def _dotted(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - defensive, unparse handles all expression nodes
        return "<call>"
    return text if len(text) <= 60 else text[:57] + "..."


def _call_root(func: ast.AST) -> str:
    while isinstance(func, ast.Attribute):
        func = func.value
    if isinstance(func, ast.Call):
        return _call_root(func.func)
    return func.id if isinstance(func, ast.Name) else ""


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in test.comparators)
    )


def _exception_name(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Call):
        node = node.func
    return _dotted(node)


class MutantGenerator:
    """Walks a module and yields every mutant it can make."""

    def __init__(self, source: str, rel_path: str, operators: frozenset[str] | None = None) -> None:
        self.source = source
        self.rel_path = rel_path
        self.operators = operators or frozenset(ALL_OPERATORS)
        self._lines = source.splitlines(keepends=True)
        self._out: list[Mutant] = []
        self._scope: list[str] = []
        self._func: tuple[str, int] = ("<module>", 0)
        self._context: list[str] = []

    # ------------------------------------------------------------------ public API
    def generate(self) -> list[Mutant]:
        tree = ast.parse(self.source)
        self._body(tree.body)
        valid: list[Mutant] = []
        for mutant in self._out:
            try:
                compile(apply_mutant(self.source, mutant), self.rel_path, "exec")
            except (SyntaxError, ValueError):
                continue
            valid.append(mutant)
        for index, mutant in enumerate(valid, start=1):
            mutant.id = f"{self.rel_path}#{index}"
        return valid

    # ------------------------------------------------------------------ helpers
    def _segment(self, span: _Span) -> str:
        start = _char_index(self._lines, span.line, span.col)
        end = _char_index(self._lines, span.end_line, span.end_col)
        return self.source[start:end]

    def _add(self, operator: str, span: _Span, replacement: str, description: str, **extra: str) -> None:
        if operator not in self.operators:
            return
        original = self._segment(span)
        if original == replacement:
            return
        self._out.append(
            Mutant(
                id="",
                file=self.rel_path,
                function=self._func[0],
                function_line=self._func[1],
                line=span.line,
                col=span.col,
                end_line=span.end_line,
                end_col=span.end_col,
                operator=operator,
                description=description,
                original=original,
                replacement=replacement,
                context=self._context[-1] if self._context else "",
                **extra,
            )
        )

    def _replace_expr(self, operator: str, node: ast.expr, new: ast.expr, description: str) -> None:
        self._add(operator, _span(node), f"({ast.unparse(new)})", description)

    @staticmethod
    def _header(text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= 80 else text[:77] + "..."

    # ------------------------------------------------------------------ statements
    def _body(self, body: list[ast.stmt]) -> None:
        for index, stmt in enumerate(body):
            if index == 0 and _is_docstring(stmt):
                continue
            self._stmt(stmt)

    def _stmt(self, node: ast.stmt) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._function(node)
        elif isinstance(node, ast.ClassDef):
            self._scope.append(node.name)
            self._body(node.body)
            self._scope.pop()
        elif isinstance(node, ast.If):
            if _is_main_guard(node):
                return
            self._expr(node.test)
            self._guarded(f"if {ast.unparse(node.test)}:", node.body)
            if node.orelse:
                self._guarded(f"else branch of `if {ast.unparse(node.test)}:`", node.orelse)
        elif isinstance(node, ast.While):
            self._expr(node.test)
            self._guarded(f"while {ast.unparse(node.test)}:", node.body)
            self._body_plain(node.orelse)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self._expr(node.iter)
            header = f"for {ast.unparse(node.target)} in {ast.unparse(node.iter)}:"
            self._guarded(header, node.body)
            self._body_plain(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self._expr(item.context_expr)
            self._body_plain(node.body)
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            self._try(node)  # type: ignore[arg-type]
        elif isinstance(node, ast.Raise):
            exc = _exception_name(node.exc)
            what = f"raise {exc}" if exc else "bare re-raise"
            self._add("remove-raise", _span(node), "pass", f"removed `{what}`", exception=exc)
        elif isinstance(node, ast.Return):
            if node.value is not None:
                if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                    self._add("return-value", _span(node), "return None", "return value replaced with None")
                self._expr(node.value)
        elif isinstance(node, ast.Expr):
            self._expr_stmt(node)
        elif isinstance(node, ast.AugAssign):
            swap = ARITHMETIC_SWAPS.get(type(node.op))
            if swap is not None:
                new = copy.deepcopy(node)
                new.op = swap()
                self._add(
                    "arithmetic",
                    _span(node),
                    ast.unparse(new),
                    f"augmented `{type(node.op).__name__}` swapped for `{swap.__name__}`",
                )
            self._expr(node.value)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self._expr(node.value)
        elif isinstance(node, ast.Assign):
            self._expr(node.value)
        elif isinstance(node, (ast.Assert, ast.Delete, ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue)):
            return
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            return
        else:  # match statements and anything newer: walk expressions generically
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt):
                    self._stmt(child)
                elif isinstance(child, ast.expr):
                    self._expr(child)
                elif type(child).__name__ == "match_case":
                    case: ast.match_case = child  # type: ignore[assignment]
                    if case.guard is not None:
                        self._expr(case.guard)
                    self._guarded(f"case {ast.unparse(case.pattern)}:", case.body)

    def _body_plain(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            self._stmt(stmt)

    def _guarded(self, header: str, body: list[ast.stmt]) -> None:
        self._context.append(self._header(header))
        self._body_plain(body)
        self._context.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer_func, outer_context = self._func, self._context
        self._scope.append(node.name)
        self._func = (".".join(self._scope), node.lineno)
        self._context = []
        for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]:
            self._expr(default)
        self._body(node.body)
        self._scope.pop()
        self._func, self._context = outer_func, outer_context

    def _try(self, node: ast.Try) -> None:
        self._body_plain(node.body)
        for handler in node.handlers:
            exc = ast.unparse(handler.type) if handler.type is not None else "Exception"
            header = f"except {exc}:" if handler.type is not None else "except:"
            body = handler.body
            trivial = len(body) == 1 and (
                isinstance(body[0], ast.Pass)
                or (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant))
            )
            if not trivial:
                first, last = _span(body[0]), _span(body[-1])
                span = _Span(first.line, first.col, last.end_line, last.end_col)
                self._context.append(self._header(header))
                self._add(
                    "swallow-exception",
                    span,
                    "pass",
                    f"`{header}` handler body replaced with `pass` (error silently swallowed)",
                    exception=exc,
                )
                self._context.pop()
            self._guarded(header, body)
        self._body_plain(node.orelse)
        self._guarded("finally:", node.finalbody)

    def _expr_stmt(self, node: ast.Expr) -> None:
        value = node.value
        call = value.value if isinstance(value, ast.Await) else value
        if isinstance(call, ast.Call) and _call_root(call.func) not in QUIET_CALL_ROOTS:
            name = _dotted(call.func)
            self._add("remove-call", _span(node), "pass", f"removed call statement `{name}(...)`", call=name)
        self._expr(value)

    # ------------------------------------------------------------------ expressions
    def _expr(self, node: ast.expr) -> None:
        if isinstance(node, ast.JoinedStr):
            return  # f-string internals: positions are unreliable before 3.12 and mutations are noise
        if isinstance(node, (ast.Lambda,)):
            self._expr(node.body)
            return
        if isinstance(node, ast.Compare):
            for index, op in enumerate(node.ops):
                swap = COMPARISON_SWAPS.get(type(op))
                if swap is None:
                    continue
                new = copy.deepcopy(node)
                new.ops[index] = swap()
                self._replace_expr(
                    "comparison", node, new, f"comparison `{type(op).__name__}` flipped to `{swap.__name__}`"
                )
        elif isinstance(node, ast.BoolOp):
            new = copy.deepcopy(node)
            new.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            label = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
            self._replace_expr("boolean", node, new, f"`{label[0]}` replaced with `{label[1]}`")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self._add("boolean", _span(node), f"({ast.unparse(node.operand)})", "`not` removed (condition inverted)")
        elif isinstance(node, ast.BinOp):
            swap = ARITHMETIC_SWAPS.get(type(node.op))
            if swap is not None:
                new = copy.deepcopy(node)
                new.op = swap()
                self._replace_expr(
                    "arithmetic", node, new, f"arithmetic `{type(node.op).__name__}` swapped for `{swap.__name__}`"
                )
        elif isinstance(node, ast.Constant):
            self._constant(node)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._expr(child)
            elif isinstance(child, ast.comprehension):
                self._expr(child.iter)
                for cond in child.ifs:
                    self._expr(cond)
            elif isinstance(child, ast.keyword):
                self._expr(child.value)

    def _constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, bool):
            self._add("constant", _span(node), repr(not value), f"`{value}` flipped to `{not value}`")
        elif isinstance(value, int):
            self._add("constant", _span(node), repr(value + 1), f"off-by-one: `{value}` -> `{value + 1}`")
        elif isinstance(value, float):
            self._add("constant", _span(node), repr(value + 1.0), f"constant `{value}` -> `{value + 1.0}`")


def generate_mutants(source: str, rel_path: str, operators: frozenset[str] | None = None) -> list[Mutant]:
    """Return every valid mutant of ``source`` (a module whose path relative to the root is ``rel_path``)."""
    return MutantGenerator(source, rel_path, operators).generate()


def iter_functions(tree: ast.Module) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Yield ``(qualified_name, node)`` for every function and method in a module."""

    def walk(body: list[ast.stmt], scope: list[str]) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = [*scope, node.name]
                yield ".".join(name), node
                yield from walk(node.body, name)
            elif isinstance(node, ast.ClassDef):
                yield from walk(node.body, [*scope, node.name])
            else:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        yield from walk([child], scope)

    yield from walk(tree.body, [])
