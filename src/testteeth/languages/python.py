"""The Python adapter: testteeth's own AST-based mutation engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..models import GradeReport
    from . import ProgressCallback


class PythonAdapter:
    lang = "python"
    engine = "testteeth"

    def grade(self, settings: Settings, progress: ProgressCallback | None = None) -> GradeReport:
        from ..engine import grade

        report = grade(settings, progress)
        report.lang, report.engine = self.lang, self.engine
        return report
