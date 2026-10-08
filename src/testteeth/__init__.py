"""testteeth: grade AI-written tests with mutation testing (Python, TypeScript/JavaScript, Rust) and close the gaps."""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__", "grade", "load_settings"]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    # Lazy re-exports keep `import testteeth` (and the pytest plugin entry point) cheap.
    if name == "grade":
        from .engine import grade

        return grade
    if name == "load_settings":
        from .config import load_settings

        return load_settings
    raise AttributeError(name)
