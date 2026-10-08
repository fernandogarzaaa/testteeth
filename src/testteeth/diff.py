"""Git integration: which lines changed relative to a base ref (any language, by pathspec)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Marker meaning "every line of this file is new" (untracked or newly added files).
ALL_LINES: frozenset[int] = frozenset({-1})

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    """Raised when git is missing, the directory is not a repository, or the ref is unknown."""


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=60, check=False
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out") from exc
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed with exit code {proc.returncode}")
    return proc.stdout


def repo_toplevel(path: Path) -> Path:
    return Path(_git(path, "rev-parse", "--show-toplevel").strip()).resolve()


def parse_unified_diff(text: str) -> dict[str, set[int]]:
    """Parse ``git diff -U0`` output into ``{posix_path: {changed new-side line numbers}}``.

    Pure deletions are recorded as the line on either side of the hole so the enclosing
    function is still selected. Files added in the diff map to :data:`ALL_LINES`.
    """
    changed: dict[str, set[int]] = {}
    current: str | None = None
    new_file = False
    for line in text.splitlines():
        if line.startswith("diff --git"):
            current, new_file = None, False
        elif line.startswith("new file mode"):
            new_file = True
        elif line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
                continue
            current = target[2:] if target.startswith("b/") else target
            if new_file:
                changed[current] = set(ALL_LINES)
            else:
                changed.setdefault(current, set())
        elif current is not None and line.startswith("@@"):
            match = _HUNK.match(line)
            if not match or changed.get(current) == set(ALL_LINES):
                continue
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count == 0:
                changed[current].update({start, start + 1})
            else:
                changed[current].update(range(start, start + count))
    return changed


def verify_ref(root: Path, base_ref: str) -> Path:
    """Return the repository top level, raising :class:`GitError` if ``base_ref`` is unknown."""
    top = repo_toplevel(root)
    try:
        _git(top, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
    except GitError as exc:
        raise GitError(f"unknown base ref {base_ref!r} (is it fetched? try `git fetch origin {base_ref}`)") from exc
    return top


def changed_lines(
    root: Path, base_ref: str, patterns: tuple[str, ...] = ("*.py",)
) -> dict[Path, frozenset[int]]:
    """Return absolute paths of changed files (vs ``base_ref``) mapped to changed lines.

    Compares the working tree (staged and unstaged edits) against ``base_ref`` and treats
    untracked files as entirely changed. ``patterns`` are git pathspecs (default: Python files).
    """
    top = verify_ref(root, base_ref)
    diff = _git(top, "diff", "--no-color", "--no-ext-diff", "-U0", base_ref, "--", *patterns)
    result: dict[Path, frozenset[int]] = {
        (top / rel).resolve(): frozenset(lines) for rel, lines in parse_unified_diff(diff).items()
    }
    untracked = _git(top, "ls-files", "--others", "--exclude-standard", "--", *patterns)
    for rel in untracked.splitlines():
        if rel.strip():
            result[(top / rel.strip()).resolve()] = ALL_LINES
    return result


def diff_with_untracked(root: Path, base_ref: str, patterns: tuple[str, ...]) -> str:
    """A unified diff of ``root`` vs ``base_ref`` with paths relative to ``root``, untracked files included.

    Used to feed engines that read a diff file (``cargo mutants --in-diff``).
    """
    verify_ref(root, base_ref)
    parts = [_git(root, "diff", "--no-color", "--no-ext-diff", "--relative", base_ref, "--", *patterns)]
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", *patterns)
    for rel in untracked.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        proc = subprocess.run(
            ["git", "diff", "--no-color", "--no-index", "--", "/dev/null", rel],
            cwd=root, capture_output=True, text=True, timeout=60, check=False,
        )
        # `git diff --no-index` exits 1 when the files differ, which is the expected case here.
        parts.append(proc.stdout.replace("+++ b//dev/null", "+++ /dev/null"))
    return "".join(parts)
