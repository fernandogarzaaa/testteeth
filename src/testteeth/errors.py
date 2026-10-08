"""Errors shared by every language adapter (the Python engine and the native-engine wrappers)."""

from __future__ import annotations


class EngineError(RuntimeError):
    """Something prevented grading (bad paths, broken baseline, missing engine...)."""


class BaselineFailed(EngineError):
    """The unmutated test suite does not pass, so mutation results would be meaningless."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


class EngineNotInstalled(EngineError):
    """The native mutation engine for the detected language is not installed."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(f"{message}\n  install hint: {hint}")
        self.hint = hint


class EngineCrashed(EngineError):
    """The native engine exited abnormally without producing a report."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


class ReportError(EngineError):
    """The native engine's report is missing required fields or is not valid JSON."""
