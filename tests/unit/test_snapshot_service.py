from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from patchcage.snapshot import (
    SnapshotError,
    create_snapshot,
    extract_snapshot,
    sanitize_archive,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CREATE_DEMO = PROJECT_ROOT / "scripts" / "create_demo_repo.py"


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_all(repository: Path) -> str:
    git(repository, "init", "-q", "-b", "main")
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=PatchCage Test",
        "-c",
        "user.email=test@patchcage.invalid",
        "commit",
        "-q",
        "-m",
        "Create fixture",
    )
    return git(repository, "rev-parse", "HEAD")


def make_tar(name: str, content: bytes = b"x", *, entry_type: bytes | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.type = entry_type or tarfile.REGTYPE
        info.size = len(content) if info.isreg() else 0
        archive.addfile(info, io.BytesIO(content) if info.isreg() else None)
    return output.getvalue()


def test_snapshot_uses_commit_and_excludes_untracked_env(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    created = subprocess.run(
        [sys.executable, str(CREATE_DEMO), str(repository)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    commit_sha = json.loads(created.stdout)["commit_sha"]
    status_before = git(repository, "status", "--short")

    snapshot = create_snapshot(repository, commit_sha, blocked_patterns=(".env*", ".git/**"))
    extracted = tmp_path / "extracted"
    extract_snapshot(snapshot, extracted)

    assert snapshot.commit_sha == commit_sha
    assert ".env" not in snapshot.entries
    assert (extracted / "src" / "demo_app" / "search.py").is_file()
    assert not (extracted / ".git").exists()
    assert not (extracted / ".env").exists()
    assert git(repository, "status", "--short") == status_before


def test_tracked_blocked_file_rejects_snapshot(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("print('ok')\n")
    (repository / ".env").write_text("SECRET=tracked\n")
    commit_sha = commit_all(repository)

    with pytest.raises(SnapshotError) as raised:
        create_snapshot(repository, commit_sha, blocked_patterns=(".env*",))

    assert raised.value.code == "TRACKED_BLOCKED_FILE"


def test_tracked_symlink_rejects_snapshot(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("print('ok')\n")
    (repository / "link.py").symlink_to("app.py")
    commit_sha = commit_all(repository)

    with pytest.raises(SnapshotError) as raised:
        create_snapshot(repository, commit_sha, blocked_patterns=())

    assert raised.value.code == "SYMLINK_FORBIDDEN"


def test_tracked_secret_is_rejected_case_insensitively(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("print('ok')\n")
    (repository / "tls.PEM").write_text("not-a-real-key\n")
    commit_sha = commit_all(repository)

    with pytest.raises(SnapshotError) as raised:
        create_snapshot(repository, commit_sha, blocked_patterns=())

    assert raised.value.code == "TRACKED_BLOCKED_FILE"


def test_gitattributes_export_directives_reject_snapshot(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("print('ok')\n")
    (repository / ".gitattributes").write_text("app.py export-ignore\n")
    commit_sha = commit_all(repository)

    with pytest.raises(SnapshotError) as raised:
        create_snapshot(repository, commit_sha, blocked_patterns=())

    assert raised.value.code == "GITATTRIBUTES_EXPORT_FORBIDDEN"


def test_nested_gitattributes_export_directives_reject_snapshot(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "src").mkdir()
    (repository / "src" / "app.py").write_text("print('ok')\n")
    (repository / "src" / ".gitattributes").write_text("app.py export-ignore\n")
    commit_sha = commit_all(repository)

    with pytest.raises(SnapshotError) as raised:
        create_snapshot(repository, commit_sha, blocked_patterns=())

    assert raised.value.code == "GITATTRIBUTES_EXPORT_FORBIDDEN"


def test_tracked_patchcage_control_dir_rejects_snapshot(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("print('ok')\n")
    (repository / ".patchcage").mkdir()
    (repository / ".patchcage" / "state.json").write_text("{}\n")
    commit_sha = commit_all(repository)

    with pytest.raises(SnapshotError) as raised:
        create_snapshot(repository, commit_sha, blocked_patterns=())

    assert raised.value.code == "TRACKED_BLOCKED_FILE"


def test_gitattributes_without_export_directives_is_allowed(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("print('ok')\n")
    (repository / ".gitattributes").write_text("*.py text eol=lf\n# export-ignore in a comment\n")
    commit_sha = commit_all(repository)

    snapshot = create_snapshot(repository, commit_sha, blocked_patterns=())

    assert "app.py" in snapshot.entries


@pytest.mark.parametrize(
    ("archive", "code"),
    [
        (make_tar("../escape.py"), "UNSAFE_ARCHIVE_PATH"),
        (make_tar("/absolute.py"), "UNSAFE_ARCHIVE_PATH"),
        (make_tar("src/link", entry_type=tarfile.SYMTYPE), "SYMLINK_FORBIDDEN"),
        (make_tar("src/fifo", entry_type=tarfile.FIFOTYPE), "SPECIAL_FILE_FORBIDDEN"),
    ],
)
def test_unsafe_tar_members_are_rejected(archive: bytes, code: str) -> None:
    with pytest.raises(SnapshotError) as raised:
        sanitize_archive(archive)

    assert raised.value.code == code


def test_archive_file_size_limit_is_enforced() -> None:
    with pytest.raises(SnapshotError) as raised:
        sanitize_archive(make_tar("large.txt", b"1234"), max_file_bytes=3)

    assert raised.value.code == "ARCHIVE_FILE_TOO_LARGE"


def test_sanitized_archive_hash_is_deterministic() -> None:
    archive = make_tar("src/app.py", b"print('ok')\n")

    first = sanitize_archive(archive)
    second = sanitize_archive(archive)

    assert first == second
