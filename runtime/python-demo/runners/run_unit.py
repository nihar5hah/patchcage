"""Trusted unit-test entry point.

The interpreter starts without the workspace on sys.path, so a patched
sitecustomize.py in the writable tree cannot execute during startup. The
workspace is inserted only after startup, immediately before pytest runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def main() -> int:
    workspace = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace").resolve()
    test_path = sys.argv[2] if len(sys.argv) > 2 else "tests/unit"
    sys.path.insert(0, str(workspace / "src"))
    return pytest.main(["-q", str(workspace / test_path)])


if __name__ == "__main__":
    raise SystemExit(main())
