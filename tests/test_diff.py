from __future__ import annotations

import pytest

from conftest import git, write
from testteeth.diff import ALL_LINES, GitError, changed_lines, parse_unified_diff

DIFF = """\
diff --git a/pkg/a.py b/pkg/a.py
index 1..2 100644
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -3 +3 @@ def f():
-    return 1
+    return 2
@@ -10,0 +11,3 @@ def g():
+x
+y
+z
@@ -20,2 +23,0 @@
-gone
-gone
diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
diff --git a/pkg/old.py b/pkg/old.py
deleted file mode 100644
--- a/pkg/old.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""


def test_parse_unified_diff():
    result = parse_unified_diff(DIFF)
    assert result["pkg/a.py"] == {3, 11, 12, 13, 23, 24}
    assert result["pkg/new.py"] == set(ALL_LINES)
    assert "pkg/old.py" not in result


def test_parse_ignores_garbage_and_empty_input():
    assert parse_unified_diff("") == {}
    assert parse_unified_diff("+++ b/x.py\n@@ nonsense @@\n") == {"x.py": set()}


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "pkg/a.py", "def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    write(tmp_path, "README.md", "hi\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_changed_lines_against_ref_includes_unstaged_and_untracked(repo):
    write(repo, "pkg/a.py", "def f():\n    return 1\n\n\ndef g():\n    return 3\n")
    write(repo, "pkg/b.py", "x = 1\n")
    write(repo, "notes.txt", "ignored\n")
    result = changed_lines(repo, "main")
    assert result == {(repo / "pkg/a.py").resolve(): frozenset({6}), (repo / "pkg/b.py").resolve(): ALL_LINES}


def test_changed_lines_clean_tree_is_empty(repo):
    assert changed_lines(repo, "HEAD") == {}


def test_unknown_ref_has_helpful_error(repo):
    with pytest.raises(GitError, match="unknown base ref 'nope'"):
        changed_lines(repo, "nope")


def test_not_a_repository(tmp_path):
    with pytest.raises(GitError):
        changed_lines(tmp_path, "main")


def test_missing_git_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(GitError, match="git executable not found"):
        changed_lines(tmp_path, "main")


def test_git_timeout_is_reported(monkeypatch, tmp_path):
    import subprocess

    from testteeth import diff

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=60)

    monkeypatch.setattr(diff.subprocess, "run", boom)
    with pytest.raises(GitError, match="timed out"):
        diff.repo_toplevel(tmp_path)
