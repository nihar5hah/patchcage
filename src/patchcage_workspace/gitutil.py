"""Git helpers for the disposable workspace (status noise and untracked diffs)."""

from __future__ import annotations

import subprocess
from pathlib import Path

NOISE_DIR_NAMES = frozenset({".patchcage", "__pycache__", ".pytest_cache"})
DIFF_EXCLUDES = (
    ":!.patchcage",
    ":!.patchcage/**",
    ":!**/__pycache__",
    ":!**/__pycache__/**",
    ":!.pytest_cache",
    ":!.pytest_cache/**",
)


class GitFailed(RuntimeError):
    pass


def porcelain_path(line: str) -> str:
    path = line[3:] if len(line) >= 3 else line
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def is_status_noise(path: str) -> bool:
    parts = path.strip("/").split("/")
    return any(part in NOISE_DIR_NAMES for part in parts) or path.endswith(".pyc")


def git_output(root: Path, *args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise GitFailed(detail)
    return completed.stdout


def working_tree_diff(root: Path) -> str:
    git_output(root, "add", "--intent-to-add", "--", ".", *DIFF_EXCLUDES)
    return git_output(root, "diff", "--", ".", *DIFF_EXCLUDES)
