from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from patchcage.domain import RunLimits, ScopeSpec
from patchcage.policy.paths import (
    PolicyViolation,
    is_reserved_control_path,
    is_secret_path,
    normalize_relative_path,
)

DEFAULT_SUPPRESSION_MARKERS = ("nosemgrep", "semgrep:ignore")
INTERPRETER_HOOK_NAMES = frozenset({"sitecustomize.py", "usercustomize.py"})
DEPENDENCY_FILE_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "constraints.txt",
        "package.json",
        "package-lock.json",
        "poetry.lock",
        "uv.lock",
        "pipfile",
        "pipfile.lock",
    }
)


class PatchPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PatchFile:
    path: str
    added_lines: int
    deleted_lines: int


@dataclass(frozen=True, slots=True)
class PatchMetadata:
    sha256: str
    byte_count: int
    files: tuple[PatchFile, ...]
    added_lines: int
    deleted_lines: int


def _git_patch_metadata(diff: str, *args: str, cwd: Path | None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "apply", *args],
            cwd=cwd,
            input=diff.encode(),
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", b"").decode(errors="replace").strip()
        raise PatchPolicyError("INVALID_PATCH", detail or str(error)) from error
    return completed.stdout


def _parse_numstat(output: bytes) -> tuple[PatchFile, ...]:
    files: list[PatchFile] = []
    seen: set[str] = set()
    for raw_record in output.split(b"\0"):
        if not raw_record:
            continue
        try:
            raw_added, raw_deleted, raw_path = raw_record.split(b"\t", maxsplit=2)
            if raw_added == b"-" or raw_deleted == b"-":
                raise PatchPolicyError("BINARY_PATCH_FORBIDDEN", "binary patches are forbidden")
            path = normalize_relative_path(raw_path.decode("utf-8")).as_posix()
            added = int(raw_added)
            deleted = int(raw_deleted)
        except PatchPolicyError:
            raise
        except (UnicodeDecodeError, ValueError, PolicyViolation) as error:
            raise PatchPolicyError("INVALID_PATCH_METADATA", "invalid patch metadata") from error

        if path in seen:
            raise PatchPolicyError("DUPLICATE_PATCH_PATH", f"duplicate patch path: {path}")
        seen.add(path)
        files.append(PatchFile(path=path, added_lines=added, deleted_lines=deleted))

    if not files:
        raise PatchPolicyError("EMPTY_PATCH", "patch contains no file changes")
    return tuple(files)


def _reject_unsupported_summary(summary: str) -> None:
    for line in summary.splitlines():
        normalized = line.strip().lower()
        if normalized.startswith(("rename ", "copy ", "mode change ")):
            raise PatchPolicyError("PATCH_OPERATION_FORBIDDEN", line.strip())
        if normalized.startswith(("create mode ", "delete mode ")):
            parts = normalized.split()
            mode = parts[2] if len(parts) > 2 else ""
            if mode == "120000":
                raise PatchPolicyError("SYMLINK_PATCH_FORBIDDEN", line.strip())
            if mode != "100644":
                raise PatchPolicyError("PATCH_OPERATION_FORBIDDEN", line.strip())


def _is_dependency_file(filename: str) -> bool:
    lowered = filename.lower()
    if lowered in DEPENDENCY_FILE_NAMES:
        return True
    return lowered.startswith("requirements") and lowered.endswith(".txt")


def _reject_suppression_markers(diff: str, markers: tuple[str, ...]) -> None:
    lowered_markers = tuple(marker.lower() for marker in markers)
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in lowered_markers):
            raise PatchPolicyError(
                "SCANNER_SUPPRESSION_FORBIDDEN",
                "patch adds a scanner suppression marker",
            )


def inspect_patch(
    diff: str,
    *,
    scope: ScopeSpec,
    limits: RunLimits,
    cwd: Path | None = None,
    suppression_markers: tuple[str, ...] = DEFAULT_SUPPRESSION_MARKERS,
) -> PatchMetadata:
    encoded = diff.encode()
    if len(encoded) > limits.patch_bytes:
        raise PatchPolicyError("PATCH_TOO_LARGE", "patch exceeds byte limit")
    if "GIT binary patch" in diff or "Binary files " in diff:
        raise PatchPolicyError("BINARY_PATCH_FORBIDDEN", "binary patches are forbidden")

    # Summary first: rename/copy/mode operations must get their precise rejection
    # code before numstat parsing, whose NUL record layout differs for renames.
    summary = _git_patch_metadata(diff, "--summary", cwd=cwd).decode(errors="replace")
    _reject_unsupported_summary(summary)
    _reject_suppression_markers(diff, suppression_markers)
    files = _parse_numstat(_git_patch_metadata(diff, "--numstat", "-z", cwd=cwd))

    if len(files) > limits.patch_files:
        raise PatchPolicyError("TOO_MANY_PATCH_FILES", "patch exceeds changed-file limit")

    blocked = GitIgnoreSpec.from_lines(scope.blocked)
    writable = GitIgnoreSpec.from_lines(scope.writable)
    for file in files:
        if (
            is_secret_path(file.path)
            or is_reserved_control_path(file.path)
            or blocked.match_file(file.path)
        ):
            raise PatchPolicyError("BLOCKED_PATCH_PATH", f"patch path is blocked: {file.path}")
        if not writable.match_file(file.path):
            raise PatchPolicyError(
                "UNWRITABLE_PATCH_PATH",
                f"patch path is not writable: {file.path}",
            )
        filename = Path(file.path).name.lower()
        if filename in INTERPRETER_HOOK_NAMES:
            raise PatchPolicyError(
                "INTERPRETER_HOOK_FORBIDDEN",
                f"interpreter startup hook is forbidden: {file.path}",
            )
        if _is_dependency_file(filename):
            raise PatchPolicyError(
                "DEPENDENCY_CHANGE_FORBIDDEN",
                f"dependency change is forbidden: {file.path}",
            )

    added_lines = sum(file.added_lines for file in files)
    deleted_lines = sum(file.deleted_lines for file in files)
    if added_lines > limits.added_lines:
        raise PatchPolicyError("TOO_MANY_ADDED_LINES", "patch exceeds added-line limit")
    if deleted_lines > limits.deleted_lines:
        raise PatchPolicyError("TOO_MANY_DELETED_LINES", "patch exceeds deleted-line limit")

    return PatchMetadata(
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
        files=files,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
    )
