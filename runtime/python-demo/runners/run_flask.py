"""Trusted Flask entry point.

The interpreter starts without the workspace on sys.path, so a tree-local
sitecustomize.py cannot execute during startup. The workspace is inserted
only after startup, immediately before the app is imported.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    workspace = Path(sys.argv[1]).resolve()
    host = sys.argv[2]
    port = int(sys.argv[3])
    sys.path.insert(0, str(workspace / "src"))
    from demo_app import create_app

    create_app().run(host=host, port=port, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
