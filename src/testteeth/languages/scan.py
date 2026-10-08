"""Lightweight scanning for brace languages (TypeScript/JavaScript and Rust).

No parser dependency: comments and string literals are masked out (replaced by spaces, newlines kept, so
offsets and line numbers stay valid), then braces are matched and each block's *header* (the text between the
previous ``;``/``{``/``}`` and the ``{``) is classified with regular expressions. This is deliberately
heuristic: it finds named functions, methods, closures assigned to names, classes / impl blocks and Rust test
modules well enough to attribute mutants to functions and run the failure-path rule packs. Exotic syntax
(regex literals containing quotes, object types in arrow-function parameters) can confuse it.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

TS = "typescript"
RUST = "rust"


def mask(source: str, lang: str) -> str:
    """Return ``source`` with comments and string/char literal contents replaced by spaces.

    Delimiters (quotes) are kept so patterns like ``.expect(`` still match; newlines are kept.
    """
    out = list(source)
    n = len(source)
    i = 0

    def blank(start: int, end: int) -> None:
        for k in range(max(start, 0), min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            end = source.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
        elif c == "/" and nxt == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if lang == RUST and source.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif source.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif lang == RUST and c == "r" and re.match(r'r(#*)"', source[i : i + 260]) and (
            i == 0 or not (source[i - 1].isalnum() or source[i - 1] == "_")
        ):
            m = re.match(r'r(#*)"', source[i:])
            assert m is not None
            closing = '"' + m.group(1)
            start = i + m.end()
            end = source.find(closing, start)
            end = n if end == -1 else end
            blank(start, end)
            i = end + len(closing)
        elif c in "\"`" or (c == "'" and lang == TS):
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        elif c == "'" and lang == RUST:
            # char literal ('x', '\n', '\u{1F600}') vs lifetime ('a)
            if nxt == "\\":
                j = source.find("'", i + 2)
                j = n if j == -1 else j
                blank(i + 1, j)
                i = j + 1
            elif i + 2 < n and source[i + 2] == "'":
                blank(i + 1, i + 2)
                i += 3
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


class LineIndex:
    """Convert between string offsets and 1-based line numbers."""

    def __init__(self, text: str) -> None:
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def offset(self, line: int) -> int:
        return self.starts[min(max(line, 1), len(self.starts)) - 1]


def match_brace(masked: str, open_index: int) -> int:
    """Index of the ``}`` matching the ``{`` at ``open_index`` (or ``len - 1`` if unbalanced)."""
    depth = 0
    for k in range(open_index, len(masked)):
        ch = masked[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return k
    return len(masked) - 1


def match_paren(masked: str, open_index: int) -> int:
    depth = 0
    for k in range(open_index, len(masked)):
        ch = masked[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return k
    return len(masked) - 1


@dataclass
class Function:
    name: str
    qualname: str
    line: int  # line of the name / signature
    end_line: int  # line of the closing brace
    open_index: int
    close_index: int
    is_test: bool = False  # inside a Rust #[cfg(test)] module, or a #[test] function
    is_async: bool = False
    header: str = ""

    def contains(self, line: int) -> bool:
        return self.line <= line <= self.end_line


@dataclass
class Scan:
    lang: str
    source: str
    masked: str
    index: LineIndex
    functions: list[Function] = field(default_factory=list)
    scopes: list[tuple[str, int, int]] = field(default_factory=list)  # (class/impl/trait name, first, last line)

    def scope_at(self, line: int) -> tuple[str, int] | None:
        """Innermost class / impl / trait containing ``line``: (name, first line)."""
        best: tuple[str, int, int] | None = None
        for name, first, last in self.scopes:
            if first <= line <= last and (best is None or first >= best[1]):
                best = (name, first, last)
        return (best[0], best[1]) if best else None

    def function_at(self, line: int, include_tests: bool = False) -> Function | None:
        """The innermost function containing ``line``."""
        best: Function | None = None
        for func in self.functions:
            if func.contains(line) and (include_tests or not func.is_test):
                if best is None or func.line >= best.line:
                    best = func
        return best

    def outermost_at(self, line: int) -> Function | None:
        best: Function | None = None
        for func in self.functions:
            if func.contains(line) and not func.is_test and (best is None or func.line < best.line):
                best = func
        return best


_TS_KEYWORDS = frozenset(
    "if for while switch catch function return with do else try finally new typeof await yield super import "
    "export default case throw delete void in of instanceof".split()
)
_TS_FUNCTION = re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*(?:<[^()]*>)?\s*\(", re.S)
_TS_ANON_FUNCTION = re.compile(r"(?:^|[\s(,=:])([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s+)?function\b[^{]*$", re.S)
_TS_ARROW = re.compile(
    r"(?:^|[\s,;{(])(?:(?:export\s+)?(?:const|let|var)\s+)?(?:(?:public|private|protected|static|readonly)\s+)*"
    r"([A-Za-z_$][\w$]*)\s*(?::[^=]+?)?\s*[:=]\s*(?:async\s+)?(?:\(.*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+?)?\s*=>\s*$",
    re.S,
)
_TS_METHOD = re.compile(
    r"^(?:@[\w.]+(?:\(.*?\))?\s*)*(?:(?:public|private|protected|static|async|override|readonly|abstract|get|set)\s+)*"
    r"\*?\s*(#?[A-Za-z_$][\w$]*)\s*(?:<[^()]*>)?\s*\((.*)\)\s*(?::\s*[^{}]+)?$",
    re.S,
)
_TS_CLASS = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")

_RS_FN = re.compile(r"(?:^|[\s;}>\]])(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_]\w*)")
_RS_IMPL_FOR = re.compile(r"\bimpl\b(?:\s*<.*?>)?\s+.*?\bfor\s+(?:&(?:'\w+\s+)?)?(?:mut\s+)?([A-Za-z_][\w:]*)", re.S)
_RS_IMPL = re.compile(r"\bimpl\b(?:\s*<.*?>)?\s+(?:&(?:'\w+\s+)?)?([A-Za-z_][\w:]*)", re.S)
_RS_TRAIT = re.compile(r"\btrait\s+([A-Za-z_]\w*)")
_RS_MOD = re.compile(r"\bmod\s+([A-Za-z_]\w*)\s*$")
_RS_TEST_ATTR = re.compile(r"#\[(?:\w+::)*(?:test|rstest|tokio::test|async_std::test|test_case)\b")


def _header(masked: str, brace: int) -> tuple[str, int]:
    k = brace - 1
    while k >= 0 and masked[k] not in ";{}":
        k -= 1
    start = k + 1
    # Rust attributes like #[cfg(test)] never contain ; { } so they stay part of the header.
    return masked[start:brace], start


def _classify(lang: str, header: str) -> tuple[str, str, bool]:
    """Return (kind, name, is_async) for a block header; kind is function|scope|test-scope|block."""
    text = header.strip()
    if not text:
        return "block", "", False
    if lang == RUST:
        fn = None
        for fn in _RS_FN.finditer(" " + text):
            pass
        if fn is not None and "=" not in text[text.find("fn ") :].split("->")[0].split("(")[0]:
            return "function", fn.group(2), bool(fn.group(1))
        mod = _RS_MOD.search(text)
        if mod:
            return ("test-scope" if "cfg(test)" in text.replace(" ", "") else "scope"), mod.group(1), False
        if re.search(r"\bimpl\b", text):
            m = _RS_IMPL_FOR.search(text) or _RS_IMPL.search(text)
            if m:
                return "scope", re.sub(r"<.*", "", m.group(1)).split("::")[-1], False
        trait = _RS_TRAIT.search(text)
        if trait:
            return "scope", trait.group(1), False
        return "block", "", False
    cls = _TS_CLASS.search(text)
    if cls and not text.rstrip().endswith(")"):
        return "scope", cls.group(1), False
    fn = _TS_FUNCTION.search(text)
    is_async = bool(re.search(r"\basync\b", text))
    if fn:
        return "function", fn.group(1), is_async
    if text.endswith("=>"):
        arrow = _TS_ARROW.search(" " + text)
        if arrow:
            return "function", arrow.group(1), is_async
        return "lambda", "", is_async
    anon = _TS_ANON_FUNCTION.search(" " + text)
    if anon:
        return "function", anon.group(1), is_async
    method = _TS_METHOD.match(text)
    if method and method.group(1) not in _TS_KEYWORDS and not re.search(r"[=;]", text.split("(")[0]):
        return "function", method.group(1).lstrip("#"), is_async
    return "block", "", False


def scan(source: str, lang: str) -> Scan:
    """Find the functions of one TypeScript/JavaScript or Rust source file."""
    masked = mask(source, lang)
    result = Scan(lang, source, masked, LineIndex(source))
    sep = "::" if lang == RUST else "."
    # stack of (close_index, kind, name, is_test)
    stack: list[tuple[int, str, str, bool]] = []
    for k, ch in enumerate(masked):
        if ch != "{":
            continue
        while stack and stack[-1][0] < k:
            stack.pop()
        header, hstart = _header(masked, k)
        kind, name, is_async = _classify(lang, header)
        close = match_brace(masked, k)
        in_test = any(entry[3] for entry in stack)
        if kind == "function":
            scopes = [entry[2] for entry in stack if entry[1] in ("scope", "function") and entry[2]]
            name_pos = header.rfind(name) if name else 0
            line = result.index.line(hstart + max(name_pos, 0))
            is_test = in_test or (lang == RUST and bool(_RS_TEST_ATTR.search(header)))
            result.functions.append(
                Function(
                    name=name,
                    qualname=sep.join([*scopes, name]),
                    line=line,
                    end_line=result.index.line(close),
                    open_index=k,
                    close_index=close,
                    is_test=is_test,
                    is_async=is_async,
                    header=" ".join(header.split()),
                )
            )
            stack.append((close, "function", name, is_test))
        elif kind in ("scope", "test-scope"):
            stack.append((close, "scope", name, in_test or kind == "test-scope"))
            if name and kind == "scope" and not in_test:
                result.scopes.append((name, result.index.line(hstart + max(header.rfind(name), 0)),
                                      result.index.line(close)))
        else:
            stack.append((close, "block", "", in_test))
    if lang == TS:
        _expression_arrows(result)
    result.functions.sort(key=lambda f: (f.line, f.open_index))
    return result


_TS_EXPR_ARROW = re.compile(
    r"(?:^|[\s;])(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+?)?=\s*(async\s+)?"
    r"(?:\([^()]*(?:\([^()]*\)[^()]*)*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+?)?=>\s*(?=[^\s{])"
)


def _expression_arrows(result: Scan) -> None:
    """``const f = (x) => expr;`` has no braces of its own: end it at the statement's end."""
    masked = result.masked
    for m in _TS_EXPR_ARROW.finditer(masked):
        depth, k = 0, m.end()
        while k < len(masked):
            ch = masked[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == ";" and depth == 0:
                break
            elif ch == "\n" and depth == 0 and not masked[m.end() : k].strip().endswith(("=>", "(", ",", "+", "?", ":")):
                break
            k += 1
        start_line = result.index.line(m.start(1))
        result.functions.append(
            Function(
                name=m.group(1), qualname=m.group(1), line=start_line, end_line=result.index.line(max(k - 1, m.end())),
                open_index=m.end(), close_index=k, is_async=bool(m.group(2)),
                header=" ".join(masked[m.start(1) : m.end()].split()),
            )
        )


def block_lines(masked: str, index: LineIndex, open_index: int) -> tuple[int, int, set[int]]:
    """(first line, last line, lines strictly inside) of the block opened at ``open_index``."""
    close = match_brace(masked, open_index)
    first, last = index.line(open_index), index.line(close)
    inner = set(range(first + 1, last)) if last > first + 1 else {first}
    return first, last, inner
