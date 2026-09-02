"""Sandbox process environment. Docker-free so the in-container MCP server can import it."""

from __future__ import annotations

WORKSPACE = "/workspace"
HOME_DIR = "/home/patchcage"
IMAGE_SEMGREP_SETTINGS = "/opt/patchcage/semgrep/offline-settings.yml"
SEMGREP_SETTINGS_FILE = f"{HOME_DIR}/.semgrep/settings.yml"

SANDBOX_ENV = {
    "PATH": "/opt/venvs/workspace/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": HOME_DIR,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "SEMGREP_ENABLE_VERSION_CHECK": "0",
    "SEMGREP_SETTINGS_FILE": SEMGREP_SETTINGS_FILE,
}
