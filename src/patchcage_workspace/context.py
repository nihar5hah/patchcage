from __future__ import annotations

import json
from pathlib import Path

from patchcage.domain import Finding, ProjectManifest
from patchcage.policy import PathPolicy

CONTROL_STATE = Path("/workspace/.patchcage/state.json")
WORKSPACE = Path("/workspace")


class WorkspaceContext:
    def __init__(
        self,
        *,
        root: Path,
        manifest: ProjectManifest,
        finding: Finding | None,
        baseline_sha: str,
    ) -> None:
        self.root = root
        self.manifest = manifest
        self.finding = finding
        self.baseline_sha = baseline_sha
        self.policy = PathPolicy(root, manifest.scope)

    @classmethod
    def from_control_file(cls, path: Path = CONTROL_STATE) -> WorkspaceContext:
        raw = json.loads(path.read_text())
        finding_raw = raw.get("finding")
        return cls(
            root=WORKSPACE,
            manifest=ProjectManifest.model_validate(raw["manifest"]),
            finding=None if finding_raw is None else Finding.model_validate(finding_raw),
            baseline_sha=str(raw["baseline_sha"]),
        )
