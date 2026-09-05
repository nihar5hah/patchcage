"""Enter a named check in its disposable, read-only verification container.

Scanner/compile/oracle code is image-owned. Unit tests import candidate code,
so run them as nobody. The oracle separately drops its app child's identity.
"""

import os
import sys


def main() -> None:
    name, *argv = sys.argv[1:]
    if name not in {"compile", "scanner", "security", "unit"} or not argv:
        raise SystemExit(2)
    os.environ["PATCHCAGE_VERIFY"] = "1"
    if name == "unit":
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
        os.environ["HOME"] = "/tmp"
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.execvpe(argv[0], argv, os.environ)


if __name__ == "__main__":
    main()
