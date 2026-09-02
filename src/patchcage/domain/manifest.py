from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from patchcage.domain.models import AGENT_ACTION_ADAPTER, AgentAction, Finding, ProjectManifest


def _load_mapping(path: Path, *, kind: str) -> dict[str, Any]:
    raw: Any = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{kind} root must be a mapping")
    return raw


def load_manifest(path: Path) -> ProjectManifest:
    return ProjectManifest.model_validate(_load_mapping(path, kind="manifest"))


def load_finding(path: Path) -> Finding:
    return Finding.model_validate(_load_mapping(path, kind="finding"))


def load_scripted_actions(path: Path) -> list[AgentAction]:
    raw: Any = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise ValueError("scripted actions must be a list")
    return [AGENT_ACTION_ADAPTER.validate_python(item) for item in raw]
