"""Rust failure-path rules (heuristic). cargo-mutants reports no line coverage, so every check here relies on
the text of the tests: a test that names the function and sets up or asserts a failure."""

from __future__ import annotations

import re

from ..languages.scan import Scan, block_lines, match_paren, scan
from ..models import FailurePathGap
from ._common import RETRY_WORDS, GapCollector, failure_tested, first_names

LANG = "rust"

FAILURE_TEST_WORDS = re.compile(
    r"is_err\s*\(|unwrap_err\s*\(|expect_err\s*\(|\bErr\s*\(|should_panic|catch_unwind|\bpanic!"
    r"|[tT]imeout|returning\s*\([^;]*Err|\.times\(\s*\d+\s*\)\s*\.returning"
)

ERR_CALL = re.compile(r"\bErr\s*\(")
ERR_MACRO = re.compile(r"\b(?:bail|anyhow|ensure)!\s*[(\[{]")
IF_LET_ERR = re.compile(r"\b(?:if|while)\s+let\s+Err\s*\(|\blet\s+Err\s*\(")
QUESTION = re.compile(r"[\w)\]]\?(?![\w?])")
UNWRAP = re.compile(r"\.(unwrap|expect)\s*\(")
TIMEOUT = re.compile(r"\btokio::time::timeout\b|\.timeout\s*\(|\b\w*[tT]imeout\w*\b|\bTIMEOUT\b|\bdeadline\b")
EXTERNAL = re.compile(
    r"\b(reqwest::[\w:]+)"
    r"|\b(Client::(?:new|builder))\s*\("
    r"|(\.send\(\)\s*\.await)"
    r"|\b((?:std::)?(?:net::)?(?:TcpStream|UdpSocket|TcpListener)::\w+)"
    r"|\b(tokio::(?:net|fs|process)::[\w:]+)"
    r"|\b((?:std::)?fs::\w+)\s*\("
    r"|\b(File::(?:open|create))\s*\("
    r"|\b((?:std::process::)?Command::new)\s*\("
    r"|\b((?:sqlx|diesel|redis|hyper|tonic|rusqlite|postgres|mongodb)::[\w:]+)"
    r"|\b(\w*(?:client|api|gateway|http|db|conn|session|pool|repo|service|store|transport)\w*\."
    r"(?:get|post|put|patch|delete|request|send|execute|query|fetch|charge|call|publish|insert|update))\s*\("
)
LOOP = re.compile(r"\bfor\s+([^{]*?)\s+in\s+([^{]*)\{|\bwhile\b([^{]*)\{|\bloop\s*\{")


def rust_test_bodies(source: str) -> list[str]:
    scanned = scan(source, LANG)
    bodies = [
        source[f.open_index : f.close_index + 1]
        for f in scanned.functions
        if f.is_test and re.search(r"#\[(?:\w+::)*(?:test|rstest|test_case)\b", f.header)
    ]
    if not bodies:
        bodies = [source[f.open_index : f.close_index + 1] for f in scanned.functions if f.is_test]
    return bodies or [source]


def _err_arms(masked: str) -> list[int]:
    """Offsets of ``Err(..) =>`` match arms."""
    arms: list[int] = []
    for match in ERR_CALL.finditer(masked):
        close = match_paren(masked, match.end() - 1)
        tail = masked[close + 1 : close + 200]
        if re.match(r"\s*(?:if\b[^=]*)?=>", tail):
            arms.append(match.start())
    return arms


def analyze_rust(
    scan_: Scan, rel_path: str, test_bodies: list[str], selected: set[str] | None = None
) -> list[FailurePathGap]:
    masked = scan_.masked
    col = GapCollector(scan_, rel_path, selected, LANG)
    cache: dict[str, bool] = {}

    def exercised(qualname: str) -> bool:
        if qualname not in cache:
            cache[qualname] = failure_tested(qualname, test_bodies, FAILURE_TEST_WORDS)
        return cache[qualname]

    arm_offsets = set(_err_arms(masked))
    for offset in sorted(arm_offsets) + [m.start() for m in IF_LET_ERR.finditer(masked)]:
        func = col.owner(offset)
        if func is not None and not exercised(func.qualname):
            col.add(func, "match-err", scan_.index.line(offset),
                    "`Err(..)` arm: no test makes the call return an error and checks how it is handled")

    returns: dict[str, list[int]] = {}
    for match in list(ERR_CALL.finditer(masked)) + list(ERR_MACRO.finditer(masked)):
        if match.start() in arm_offsets:
            continue
        before = masked[max(0, match.start() - 12) : match.start()]
        if re.search(r"\blet\s+$|\blet\s+$", before):
            continue
        func = col.owner(match.start())
        if func is not None:
            returns.setdefault(func.qualname, []).append(scan_.index.line(match.start()))
    for qualname, lines in returns.items():
        func = next(f for f in scan_.functions if f.qualname == qualname)
        if not exercised(qualname):
            count = len(lines)
            col.add(func, "error-return", min(lines),
                    f"returns `Err(..)` on {count} path{'s' if count > 1 else ''} but no test asserts the error "
                    "(`is_err()`, `assert_eq!(.., Err(..))`, `matches!`)")

    for qualname, hits in col.per_function(QUESTION, masked).items():
        if not exercised(qualname):
            func, line, _ = hits[0]
            col.add(func, "error-propagation", line,
                    "`?` propagates errors but no test makes the inner call fail")

    for qualname, hits in col.per_function(UNWRAP, masked).items():
        if not exercised(qualname):
            func, line, _ = hits[0]
            names = first_names(hits, lambda m: f".{m.group(1)}()")
            col.add(func, "unwrap", line,
                    f"`{names}` turns a failure into a panic and no test drives that failure")

    for qualname, hits in col.per_function(LOOP, masked).items():
        func = hits[0][0]
        retry_name = bool(RETRY_WORDS.search(func.name))
        for _, line, match in hits:
            header = " ".join(g for g in match.groups() if g)
            first, last, _ = block_lines(masked, scan_.index, match.end() - 1)
            body = "\n".join(masked.splitlines()[first - 1 : last])
            fallible = bool(re.search(r"\bErr\b|\?|\bmatch\b", body))
            if (RETRY_WORDS.search(header) or (retry_name and fallible)) and not exercised(qualname):
                col.add(func, "retry", line,
                        "retry loop: no test makes an attempt fail and checks the retry / give-up behaviour")
                break

    for qualname, hits in col.per_function(TIMEOUT, masked).items():
        if not exercised(qualname):
            func, line, _ = hits[0]
            col.add(func, "timeout", line, "handles a timeout but no test simulates the timeout expiring")

    for qualname, hits in col.per_function(EXTERNAL, masked).items():
        if not exercised(qualname):
            func, line, _ = hits[0]
            names = first_names(hits, lambda m: next(g for g in m.groups() if g).strip())
            col.add(func, "external-call", line, f"calls `{names}` but no test simulates that call failing")
    return col.result()
