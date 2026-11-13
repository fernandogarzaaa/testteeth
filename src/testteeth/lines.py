"""Line splitting that agrees with what each language's tooling calls a line.

``str.splitlines()`` also breaks on form feeds, vertical tabs, ``\\x1c``-``\\x1e``, ``\\x85`` and the Unicode
line/paragraph separators. Parsers do not: a form feed in a Python file or a U+2028 in a Rust string does not
start a new line for ``ast`` or cargo-mutants, so splitting with ``splitlines()`` shifts every mutant span that
follows. These helpers split exactly where the language's own line numbers advance.
"""

from __future__ import annotations

import re

_BREAKS = {
    # Python's tokenizer: \n, \r\n and a lone \r.
    "python": re.compile(r"\r\n|\r|\n"),
    # ECMAScript line terminators: LF, CR, CRLF, LS (U+2028) and PS (U+2029).
    "typescript": re.compile("\r\n|\r|\n| | "),
    # rustc (and proc-macro2 spans, which cargo-mutants reports) count \n only.
    "rust": re.compile(r"\n"),
}


def line_break(lang: str) -> re.Pattern[str]:
    """The pattern matching one line break in ``lang`` source (Python rules for an unknown language)."""
    return _BREAKS.get(lang, _BREAKS["python"])


def split_lines(text: str, lang: str = "python", keepends: bool = False) -> list[str]:
    """Like ``str.splitlines`` but breaking only where ``lang`` tooling starts a new line."""
    out: list[str] = []
    pos = 0
    for match in line_break(lang).finditer(text):
        out.append(text[pos : match.end() if keepends else match.start()])
        pos = match.end()
    if pos < len(text):
        out.append(text[pos:])
    return out


def utf16_to_char_col(line_text: str, units: int) -> int:
    """Convert a 0-based column counted in UTF-16 code units (JavaScript tooling) to code points."""
    seen = 0
    for index, char in enumerate(line_text):
        if seen >= units:
            return index
        seen += 2 if ord(char) > 0xFFFF else 1
    return len(line_text)
