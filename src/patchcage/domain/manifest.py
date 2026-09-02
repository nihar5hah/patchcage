from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from patchcage.domain.models import ProjectManifest


def load_manifest(path: Path) -> ProjectManifest:
    raw: Any = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be a mapping")
    return ProjectManifest.model_validate(raw)
