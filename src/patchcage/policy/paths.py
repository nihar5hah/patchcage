from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath

from pathspec import GitIgnoreSpec

from patchcage.domain import ScopeSpec

SECRET_NAME_PREFIXES = (".env",)
SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".der", ".jks", ".keystore")


class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


class PolicyViolation(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_relative_path(requested: str) -> PurePosixPath:
    if not requested:
        raise PolicyViolation("DENY_EMPTY_PATH", "path must not be empty")
    if "\x00" in requested:
        raise PolicyViolation("DENY_NUL_PATH", "path must not contain NUL bytes")
    if "\\" in requested:
        raise PolicyViolation("DENY_BACKSLASH_PATH", "paths must use POSIX separators")

    path = PurePosixPath(requested)
    if path.is_absolute():
        raise PolicyViolation("DENY_ABSOLUTE_PATH", "absolute paths are forbidden")
    if ".." in requested.split("/"):
        raise PolicyViolation("DENY_PATH_TRAVERSAL", "parent traversal is forbidden")
    return path


def is_secret_path(path: str) -> bool:
    """Case-insensitive secret-file invariant, independent of manifest patterns.

    General scope patterns stay case-sensitive (correct on Linux), but secret
    material must be blocked regardless of casing.
    """
    name = PurePosixPath(path).name.lower()
    return name.startswith(SECRET_NAME_PREFIXES) or name.endswith(SECRET_FILE_SUFFIXES)


def is_reserved_control_path(path: str) -> bool:
    posix = path.strip("/")
    return posix == ".patchcage" or posix.startswith(".patchcage/")


def _static_prefix(pattern: str) -> PurePosixPath | None:
    segments: list[str] = []
    for segment in PurePosixPath(pattern).parts:
        if any(marker in segment for marker in "*?["):
            break
        segments.append(segment)
    if not segments:
        return None
    return PurePosixPath(*segments)


def _is_ancestor(ancestor: PurePosixPath, descendant: PurePosixPath) -> bool:
    return bool(ancestor.parts) and descendant.parts[: len(ancestor.parts)] == ancestor.parts


class PathPolicy:
    def __init__(self, workspace: Path, scope: ScopeSpec) -> None:
        self.root = workspace.resolve(strict=True)
        self._readable_patterns = scope.readable
        self._readable = GitIgnoreSpec.from_lines(scope.readable)
        self._writable = GitIgnoreSpec.from_lines(scope.writable)
        self._blocked = GitIgnoreSpec.from_lines(scope.blocked)

    def _directory_is_readable(self, path: str) -> bool:
        if path == ".":
            return True
        directory = PurePosixPath(path)
        for pattern in self._readable_patterns:
            static = _static_prefix(pattern)
            if static is None:
                continue
            if (
                directory == static
                or _is_ancestor(directory, static)
                or _is_ancestor(static, directory)
            ):
                return True
        return False

    def _reject_blocked(self, relative: PurePosixPath) -> None:
        path = relative.as_posix()
        if is_secret_path(path):
            raise PolicyViolation("DENY_BLOCKED_FILE", f"secret file is forbidden: {path}")
        if is_reserved_control_path(path) or self._blocked.match_file(path):
            raise PolicyViolation("DENY_BLOCKED_FILE", f"path is blocked: {path}")

    def _check_allowed(self, relative: PurePosixPath, access: AccessMode, *, is_dir: bool) -> None:
        path = relative.as_posix()
        if access is AccessMode.READ and is_dir and self._directory_is_readable(path):
            return

        allowed = self._readable if access is AccessMode.READ else self._writable
        if not allowed.match_file(path):
            code = "DENY_UNREADABLE_PATH" if access is AccessMode.READ else "DENY_UNWRITABLE_PATH"
            raise PolicyViolation(code, f"path is outside {access.value} scope: {path}")

    def _reject_symlink_components(self, relative: PurePosixPath) -> None:
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise PolicyViolation("DENY_SYMLINK", f"symlink component is forbidden: {relative}")

    def resolve_existing(self, requested: str, access: AccessMode) -> Path:
        relative = normalize_relative_path(requested)
        self._reject_blocked(relative)
        self._reject_symlink_components(relative)

        try:
            candidate = (self.root / relative).resolve(strict=True)
        except FileNotFoundError as error:
            raise PolicyViolation(
                "DENY_MISSING_PATH",
                f"path does not exist: {relative}",
            ) from error

        if not candidate.is_relative_to(self.root):
            raise PolicyViolation("DENY_PATH_ESCAPE", "resolved path escapes workspace")
        is_dir = candidate.is_dir()
        if not (is_dir or candidate.is_file()):
            raise PolicyViolation("DENY_SPECIAL_FILE", f"special file is forbidden: {relative}")
        self._check_allowed(relative, access, is_dir=is_dir)
        return candidate

    def resolve_file(self, requested: str, access: AccessMode) -> Path:
        candidate = self.resolve_existing(requested, access)
        if not candidate.is_file():
            raise PolicyViolation("DENY_NON_FILE", f"regular file required: {requested}")
        return candidate
