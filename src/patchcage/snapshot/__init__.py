"""Immutable Git snapshot creation and validation."""

from patchcage.snapshot.service import (
    SnapshotArtifact,
    SnapshotError,
    create_snapshot,
    extract_snapshot,
    sanitize_archive,
)

__all__ = [
    "SnapshotArtifact",
    "SnapshotError",
    "create_snapshot",
    "extract_snapshot",
    "sanitize_archive",
]
