"""Test idioms per ecosystem, used to phrase briefs (pytest, vitest/jest, Rust #[test] / tokio / mockall)."""

from __future__ import annotations

import re

RAISE_ASSERT = {
    "python": "(`with pytest.raises({exc}):`)",
    "typescript": "(`expect(() => fn(...)).toThrow({exc})`, or `await expect(fn(...)).rejects.toThrow({exc})` for "
    "async code)",
    "rust": "(`assert_eq!(f(..), Err(..))` or `assert!(matches!(f(..), Err(..)))`; `#[should_panic]` if it panics)",
}

FAKE_FAILURE = {
    "python": "a fake/mock with `side_effect={exc}(...)`",
    "typescript": "a collaborator built with `vi.fn().mockRejectedValueOnce(new {exc}(...))` (jest: "
    "`jest.fn().mockRejectedValueOnce(...)`) or `mockImplementationOnce(() => {{ throw new {exc}(...) }})`",
    "rust": "a fake implementation of the trait (or a mockall mock with `.returning(|..| Err(..))`) that returns "
    "the error",
}

GAP_GUIDANCE: dict[str, dict[str, str]] = {
    "python": {
        "except-branch": "make the guarded operation raise that exception (fake, monkeypatch or side_effect) and "
        "assert what the handler does",
        "raise": "feed the invalid input / state that triggers this raise and assert it with pytest.raises",
        "retry": "simulate a transient failure on the first attempt(s) and assert the retry count, the backoff "
        "calls, and the final give-up error",
        "timeout": "simulate the timeout (raise TimeoutError / the client's timeout exception from a fake) and "
        "assert the caller's behaviour",
        "external-call": "replace the external dependency with a fake that fails (connection error, error status, "
        "partial response) and assert the function fails safely or reports the error",
    },
    "typescript": {
        "catch-branch": "make the code in the `try` throw (a `vi.fn()` / `jest.fn()` collaborator with "
        "`mockRejectedValueOnce(new Error(...))` or `mockImplementationOnce(() => { throw ... })`) and assert what "
        "the `catch` block does (rethrow, fallback value, retry, recorded state)",
        "throw": "feed the invalid input / state that triggers this `throw` and assert it with "
        "`expect(() => fn(...)).toThrow(...)` or `await expect(fn(...)).rejects.toThrow(...)`",
        "promise-catch": "make the promise reject (`mockRejectedValueOnce`) and assert the `.catch` handler's "
        "effect, including the value the caller finally sees",
        "retry": "make the first attempt(s) fail (`mockRejectedValueOnce(...)` twice, then `mockResolvedValueOnce`) "
        "and assert the call count, the backoff delays and the final give-up error",
        "timeout": "use `vi.useFakeTimers()` / `jest.useFakeTimers()` and `advanceTimersByTimeAsync(ms)` (or an "
        "aborted `AbortSignal`) to expire the timeout and assert the caller's behaviour",
        "external-call": "mock the client (`vi.mock(...)`, `vi.spyOn(globalThis, 'fetch')`, msw, or a fake "
        "object) so the call fails (network error, 500, malformed body) and assert the function fails safely",
    },
    "rust": {
        "match-err": "make the call inside the `match` return `Err` (a fake trait impl or a mockall "
        "`.returning(|..| Err(..))`) and assert this arm's outcome",
        "error-return": "drive the function into this error path and assert the exact error with "
        "`assert_eq!(f(..), Err(..))` or `assert!(matches!(f(..), Err(..)))`",
        "error-propagation": "make the inner fallible call fail (fake input, temp file that does not exist, mock "
        "returning `Err`) and assert the propagated error",
        "unwrap": "feed input that makes the fallible call fail: either assert the panic with `#[should_panic]` or, "
        "better, return a `Result` and assert the `Err`",
        "retry": "make the first attempt(s) return a transient `Err` (fake or mockall with `.times(n)`), then "
        "succeed, and assert the attempt count, backoff and final give-up error",
        "timeout": "simulate the timeout (fake returning the timeout error, or `#[tokio::test(start_paused = true)]` "
        "with `tokio::time::advance`) and assert the caller's behaviour",
        "external-call": "put the client behind a trait (or use mockall / wiremock / httpmock) so the call fails "
        "(connection error, 5xx, bad body) and assert the error is handled or returned",
    },
}


def gap_guidance(lang: str, kind: str) -> str:
    return GAP_GUIDANCE.get(lang, GAP_GUIDANCE["python"]).get(
        kind, "exercise this failure path and assert its outcome"
    )


def _camel_words(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).lower()


def test_name(lang: str, function: str, operator: str, line: int) -> str:
    short = re.split(r"::|\.", function)[-1].lstrip("_") or "module"
    slug_short = re.sub(r"[^a-z0-9]+", "_", _camel_words(short)).strip("_") or "module"
    slug_op = re.sub(r"[^a-z0-9]+", "_", operator.lower()).strip("_")
    if lang == "typescript":
        return f"{short} kills the {operator} mutant at line {line}"
    if lang == "rust":
        return f"{slug_short}_{slug_op}_line_{line}"
    return f"test_{slug_short}_{slug_op}_line_{line}"


def skeleton(lang: str, *, name: str, function: str, file: str, line: int, failure: bool, is_async: bool) -> str:
    """A minimal test skeleton in the project's ecosystem (empty for Python, whose briefs predate this)."""
    short = re.split(r"::|\.", function)[-1] or "subject"
    if lang == "typescript":
        call = f"await {short}(/* inputs */)" if is_async else f"{short}(/* inputs */)"
        if failure:
            body = (
                "  const dependency = { call: vi.fn().mockRejectedValueOnce(new Error(\"boom\")) }; // jest.fn() works too\n"
                f"  await expect({short}(/* inputs using dependency */)).rejects.toThrow(/* expected error */);\n"
                "  expect(dependency.call).toHaveBeenCalledTimes(1);"
                if is_async
                else f"  expect(() => {short}(/* inputs that take this path */)).toThrow(/* expected error */);"
            )
        else:
            body = f"  expect({call}).toBe(/* exact value from the spec */);"
        return (
            'import { expect, it, vi } from "vitest"; // jest: globals, use jest.fn()\n\n'
            f'it("{name}", async () => {{\n'
            f"  // arrange: inputs that reach {file}:{line}\n"
            f"{body}\n"
            "});\n"
        )
    if lang == "rust":
        attr = "#[tokio::test]" if is_async else "#[test]"
        sig = f"async fn {name}()" if is_async else f"fn {name}()"
        call = f"{short}(/* inputs */)" + (".await" if is_async else "")
        if failure:
            body = (
                "    // e.g. with mockall: let mut dep = MockDependency::new();\n"
                "    // dep.expect_call().times(1).returning(|..| Err(/* error */));\n"
                f"    let result = {call};\n"
                "    assert!(matches!(result, Err(/* expected error */ ..)));"
            )
        else:
            body = f"    assert_eq!({call}, /* exact value from the spec */);"
        return f"{attr}\n{sig} {{\n    // arrange: inputs that reach {file}:{line}\n{body}\n}}\n"
    return ""
