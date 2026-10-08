"""TypeScript / JavaScript failure-path rules (heuristic)."""

from __future__ import annotations

import re

from ..languages.scan import Scan, block_lines, mask, match_paren
from ..models import FailurePathGap
from ._common import RETRY_WORDS, GapCollector, failure_tested, first_names

LANG = "typescript"

FAILURE_TEST_WORDS = re.compile(
    r"\.rejects\b|toThrow|mockRejectedValue|mockImplementation\w*\(\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{?\s*throw"
    r"|\bthrow\b|new\s+\w*(?:Error|Exception|Timeout|Abort)\w*\s*\(|Promise\.reject|\breject\s*\("
    r"|useFakeTimers|advanceTimers|runAllTimers|\.catch\s*\("
)

CATCH = re.compile(r"\bcatch\b\s*(?:\([^)]*\))?\s*\{")
THROW = re.compile(r"\bthrow\b\s*(?:new\s+)?([A-Za-z_$][\w$.]*)?")
PROMISE_CATCH = re.compile(r"\.catch\s*\(")
LOOP = re.compile(r"\b(?:for|while)\s*\(|\bdo\s*\{")
TIMEOUT = re.compile(
    r"\bAbortController\b|\bAbortSignal\.timeout\b|\bPromise\.race\b|\b\w*[tT]imeout\w*\b(?<!setTimeout)(?<!clearTimeout)"
)
SET_TIMEOUT_AS_TIMEOUT = re.compile(r"\bsetTimeout\s*\([^;]*\b(?:reject|abort|controller)\b", re.S)
EXTERNAL = re.compile(
    r"\b(fetch)\s*\("
    r"|\b(axios(?:\.(?:get|post|put|patch|delete|request|head))?)\s*\("
    r"|\b((?:got|ky|superagent|needle)(?:\.\w+)?)\s*\("
    r"|\b(https?\.(?:get|request))\s*\("
    r"|\b(new\s+(?:WebSocket|EventSource|XMLHttpRequest))\b"
    r"|\b((?:fs|fsPromises|fs\.promises)\.\w+)\s*\("
    r"|\b((?:exec|execSync|execFile|spawn|spawnSync))\s*\("
    r"|\b([\w$]*(?:[cC]lient|[aA]pi|[gG]ateway|[hH]ttp|[dD]b|[sS]ession|[cC]onn|[rR]epo|[sS]ervice|[sS]dk|[qQ]ueue|"
    r"[sS]tore|[pP]rovider|[tT]ransport)[\w$]*\.(?:get|post|put|patch|delete|request|send|fetch|query|execute|charge|"
    r"publish|call|invoke|save|insert|update))\s*\("
)


def ts_test_bodies(source: str) -> list[str]:
    masked = mask(source, LANG)
    bodies: list[str] = []
    for match in re.finditer(r"\b(?:it|test)(?:\.(?:only|concurrent|skip|each\([^)]*\)))?\s*\(", masked):
        open_paren = match.end() - 1
        bodies.append(source[match.start() : match_paren(masked, open_paren) + 1])
    return bodies or [source]


def analyze_typescript(
    scan: Scan,
    rel_path: str,
    test_bodies: list[str],
    selected: set[str] | None = None,
    coverage: dict[int, bool] | None = None,
) -> list[FailurePathGap]:
    masked = scan.masked
    col = GapCollector(scan, rel_path, selected, LANG)
    coverage = coverage or {}

    handler_ran: dict[str, bool] = {}
    catch_lines: set[int] = set()
    catches = col.per_function(CATCH, masked)
    for qualname, hits in catches.items():
        for func, line, match in hits:
            _, _, inner = block_lines(masked, scan.index, match.end() - 1)
            catch_lines |= inner
            known = [coverage[n] for n in inner if n in coverage]
            if any(known):
                handler_ran[qualname] = True
            elif known:
                col.add(func, "catch-branch", min(inner), "`catch` block is never executed by any test", once=False)

    exercised_cache: dict[str, bool] = {}

    def exercised(qualname: str) -> bool:
        if qualname not in exercised_cache:
            exercised_cache[qualname] = handler_ran.get(qualname, False) or failure_tested(
                qualname, test_bodies, FAILURE_TEST_WORDS
            )
        return exercised_cache[qualname]

    for qualname, hits in catches.items():
        for func, line, match in hits:
            _, _, inner = block_lines(masked, scan.index, match.end() - 1)
            if not any(n in coverage for n in inner) and not exercised(qualname):
                col.add(func, "catch-branch", min(inner),
                        "`catch` block: no test makes the `try` body throw, so the handler is unverified", once=False)

    for qualname, hits in col.per_function(THROW, masked).items():
        for func, line, match in hits:
            if line in catch_lines or coverage.get(line, True):
                continue
            what = match.group(1) or "error"
            col.add(func, "throw", line, f"`throw {what}` is never triggered by any test", once=False)

    for qualname, hits in col.per_function(PROMISE_CATCH, masked).items():
        if not exercised(qualname):
            func, line, _ = hits[0]
            col.add(func, "promise-catch", line, "`.catch(...)` handler: no test makes the promise reject")

    for qualname, hits in col.per_function(LOOP, masked).items():
        func = hits[0][0]
        retry_name = bool(RETRY_WORDS.search(func.name))
        for _, line, match in hits:
            if match.group(0).startswith("do"):
                header, body_open = "", match.end() - 1
            else:
                close = match_paren(masked, match.end() - 1)
                header = masked[match.end() : close]
                brace = masked.find("{", close)
                body_open = brace if brace != -1 else close
            first, last, _ = block_lines(masked, scan.index, body_open)
            body = "\n".join(masked.splitlines()[first - 1 : last])
            has_try = bool(re.search(r"\btry\b|\.catch\s*\(", body))
            if (RETRY_WORDS.search(header) or (retry_name and has_try)) and not exercised(qualname):
                col.add(func, "retry", line,
                        "retry loop: no test makes an attempt fail and checks the retry / give-up behaviour")
                break

    timeouts = col.per_function(TIMEOUT, masked)
    for qualname, hits in col.per_function(SET_TIMEOUT_AS_TIMEOUT, masked).items():
        timeouts.setdefault(qualname, []).extend(hits)
    for qualname, hits in timeouts.items():
        if not exercised(qualname):
            func, line, _ = min(hits, key=lambda h: h[1])
            col.add(func, "timeout", line, "uses a timeout but no test simulates the timeout expiring")

    for qualname, hits in col.per_function(EXTERNAL, masked).items():
        if not exercised(qualname):
            func, line, _ = hits[0]
            names = first_names(hits, lambda m: next(g for g in m.groups() if g))
            col.add(func, "external-call", line, f"calls `{names}` but no test simulates that call failing")
    return col.result()
