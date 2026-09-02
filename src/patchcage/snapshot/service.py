from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from patchcage.policy.paths import (
    PolicyViolation,
    is_reserved_control_path,
    is_secret_path,
    normalize_relative_path,
)

MAX_ARCHIVE_FILE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_TREE_ENTRIES = 50_000
GIT_TIMEOUT_SECONDS = 30


class SnapshotError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SnapshotArtifact:
    commit_sha: str
    raw_sha256: str
    snapshot_sha256: str
    sanitized_archive_sha256: str
    entries: tuple[str, ...]
    archive: bytes


def _git(repository: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise SnapshotError("GIT_TIMEOUT", f"git {args[0]} exceeded time limit") from error
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", b"").decode(errors="replace").strip()
        raise SnapshotError("GIT_COMMAND_FAILED", detail or str(error)) from error
    return completed.stdout


def _reject_gitattributes_export(repository: Path, commit_sha: str, path: str) -> None:
    """export-ignore would silently drop audited files; export-subst rewrites them."""
    content = _git(repository, "show", f"{commit_sha}:{path}")
    for line in content.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if any(token in ("export-ignore", "export-subst") for token in tokens[1:]):
            raise SnapshotError(
                "GITATTRIBUTES_EXPORT_FORBIDDEN",
                f"{path} export-ignore/export-subst would corrupt snapshot fidelity",
            )


def _validate_tree(
    repository: Path,
    commit_sha: str,
    blocked_patterns: tuple[str, ...],
) -> None:
    blocked = GitIgnoreSpec.from_lines(blocked_patterns)
    tree = _git(repository, "ls-tree", "-r", "-l", "-z", commit_sha)

    entries = 0
    total_size = 0
    for raw_entry in tree.split(b"\0"):
        if not raw_entry:
            continue
        entries += 1
        if entries > MAX_TREE_ENTRIES:
            raise SnapshotError(
                "SNAPSHOT_TOO_MANY_FILES",
                f"tree exceeds {MAX_TREE_ENTRIES} entries",
            )
        try:
            metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
            # ls-tree -l right-pads the size column; split() collapses whitespace runs.
            parts = metadata.split()
            mode, object_type = parts[0], parts[1]
            size_field = parts[3] if len(parts) > 3 else b"-"
            path_text = raw_path.decode("utf-8")
            relative = normalize_relative_path(path_text)
        except (ValueError, UnicodeDecodeError, PolicyViolation) as error:
            raise SnapshotError("UNSAFE_TREE_ENTRY", "Git tree contains an unsafe path") from error

        if size_field != b"-":
            total_size += int(size_field)
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                raise SnapshotError(
                    "SNAPSHOT_TOO_LARGE",
                    f"tree exceeds {MAX_ARCHIVE_TOTAL_BYTES} bytes",
                )

        if mode == b"160000" or object_type == b"commit":
            raise SnapshotError("SUBMODULE_FORBIDDEN", f"submodule is forbidden: {relative}")
        if mode == b"120000":
            raise SnapshotError("SYMLINK_FORBIDDEN", f"symlink is forbidden: {relative}")
        posix = relative.as_posix()
        if (
            is_secret_path(posix)
            or is_reserved_control_path(posix)
            or blocked.match_file(posix)
        ):
            raise SnapshotError("TRACKED_BLOCKED_FILE", f"tracked blocked path: {relative}")
        if relative.name == ".gitattributes":
            _reject_gitattributes_export(repository, commit_sha, posix)


def _safe_member_name(member: tarfile.TarInfo) -> str:
    name = member.name.rstrip("/")
    try:
        relative = normalize_relative_path(name)
    except PolicyViolation as error:
        raise SnapshotError("UNSAFE_ARCHIVE_PATH", str(error)) from error
    if relative.as_posix() == ".":
        raise SnapshotError("UNSAFE_ARCHIVE_PATH", "archive root entry is not allowed")
    return relative.as_posix()


@contextmanager
def _validated_archive(raw_archive: bytes) -> Iterator[tarfile.TarFile]:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw_archive), mode="r:") as archive:
            yield archive
    except tarfile.TarError as error:
        raise SnapshotError("INVALID_ARCHIVE", "Git archive is not valid tar") from error


def sanitize_archive(
    raw_archive: bytes,
    *,
    blocked_patterns: tuple[str, ...] = (),
    max_file_bytes: int = MAX_ARCHIVE_FILE_BYTES,
    max_total_bytes: int = MAX_ARCHIVE_TOTAL_BYTES,
) -> tuple[bytes, str, tuple[str, ...]]:
    blocked = GitIgnoreSpec.from_lines(blocked_patterns)
    files: list[tuple[str, int, bytes]] = []
    directories: set[str] = set()
    seen: set[str] = set()
    total_bytes = 0

    with _validated_archive(raw_archive) as archive:
        for member in archive.getmembers():
            name = _safe_member_name(member)
            if name in seen:
                raise SnapshotError("DUPLICATE_ARCHIVE_ENTRY", f"duplicate archive path: {name}")
            seen.add(name)

            if is_secret_path(name) or is_reserved_control_path(name) or blocked.match_file(name):
                raise SnapshotError("TRACKED_BLOCKED_FILE", f"tracked blocked path: {name}")
            if member.issym() or member.islnk():
                raise SnapshotError("SYMLINK_FORBIDDEN", f"link is forbidden: {name}")
            if member.isdir():
                directories.add(name)
                continue
            if not member.isreg():
                raise SnapshotError("SPECIAL_FILE_FORBIDDEN", f"special file is forbidden: {name}")
            if member.size > max_file_bytes:
                raise SnapshotError("ARCHIVE_FILE_TOO_LARGE", f"archive file is too large: {name}")

            extracted = archive.extractfile(member)
            if extracted is None:
                raise SnapshotError("INVALID_ARCHIVE", f"cannot read archive file: {name}")
            content = extracted.read(max_file_bytes + 1)
            if len(content) != member.size or len(content) > max_file_bytes:
                raise SnapshotError(
                    "INVALID_ARCHIVE_SIZE",
                    f"invalid size for archive file: {name}",
                )

            total_bytes += len(content)
            if total_bytes > max_total_bytes:
                raise SnapshotError("ARCHIVE_TOO_LARGE", "archive exceeds total size limit")
            files.append((name, member.mode & 0o777, content))

    output = io.BytesIO()
    content_hash = hashlib.sha256()
    entries: list[str] = []
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as sanitized:
        for name in sorted(directories):
            info = tarfile.TarInfo(f"{name}/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            sanitized.addfile(info)
            entries.append(name)
            content_hash.update(f"D\0{name}\0".encode())

        for name, mode, content in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            info.mtime = 0
            sanitized.addfile(info, io.BytesIO(content))
            entries.append(name)
            content_hash.update(f"F\0{name}\0{mode:o}\0{len(content)}\0".encode())
            content_hash.update(hashlib.sha256(content).digest())

    return output.getvalue(), content_hash.hexdigest(), tuple(entries)


def create_snapshot(
    repository: Path,
    commit: str,
    *,
    blocked_patterns: tuple[str, ...],
) -> SnapshotArtifact:
    repository = repository.resolve(strict=True)
    inside = _git(repository, "rev-parse", "--is-inside-work-tree").decode().strip()
    if inside != "true":
        raise SnapshotError("NOT_A_GIT_REPOSITORY", f"not a Git worktree: {repository}")

    commit_sha = _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    _validate_tree(repository, commit_sha, blocked_patterns)
    raw_archive = _git(repository, "archive", "--format=tar", commit_sha)
    sanitized, snapshot_hash, entries = sanitize_archive(
        raw_archive,
        blocked_patterns=blocked_patterns,
    )
    return SnapshotArtifact(
        commit_sha=commit_sha,
        raw_sha256=hashlib.sha256(raw_archive).hexdigest(),
        snapshot_sha256=snapshot_hash,
        sanitized_archive_sha256=hashlib.sha256(sanitized).hexdigest(),
        entries=entries,
        archive=sanitized,
    )


def extract_snapshot(snapshot: SnapshotArtifact, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(snapshot.archive), mode="r:") as archive:
        for member in archive.getmembers():
            relative = Path(_safe_member_name(member))
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SnapshotError("INVALID_ARCHIVE", f"cannot read archive file: {relative}")
            target.write_bytes(extracted.read())
            target.chmod(member.mode & 0o777)
