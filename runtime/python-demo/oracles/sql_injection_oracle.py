"""Black-box SQL-injection oracle.

The application under test is started as a separate process and judged only by
its HTTP responses. The oracle never imports workspace code into its own
process, and the product seed is randomized per run so a patch cannot hardcode
expected results.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VULNERABLE_MARKER = "PATCHCAGE_VULNERABILITY_REPRODUCED"
SECURE_MARKER = "PATCHCAGE_SECURITY_ORACLE_PASSED"
ERROR_MARKER = "PATCHCAGE_ORACLE_ERROR"

STARTUP_TIMEOUT_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 5.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _random_products(rng: secrets.SystemRandom) -> list[str]:
    return [
        "product-" + "".join(rng.choice(string.ascii_lowercase) for _ in range(10))
        for _ in range(3)
    ]


def _injection_payloads(rng: secrets.SystemRandom) -> list[str]:
    number = rng.randrange(2, 1_000_000)
    token = secrets.token_hex(8)
    return [
        "' OR 1=1 --", "' OR '1'='1", "' OR 2=2 --",
        f"' OR {number}={number} --",
        f"' oR '{token}'='{token}' --",
        "'/**/OR/**/1=1--",
        "' UNION SELECT id, name FROM products --",
    ]


def _get_json(port: int, query: str) -> object:
    url = f"http://127.0.0.1:{port}/search?{urllib.parse.urlencode({'query': query})}"
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        data = response.read(64_001)
        if len(data) > 64_000:
            raise ValueError("application response exceeded 64000 bytes")
        return json.loads(data.decode())


def _wait_for_application(port: int, target: str) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            rows = _get_json(port, target)
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.2)
            continue
        return (
            isinstance(rows, list)
            and len(rows) == 1
            and isinstance(rows[0], dict)
            and rows[0].get("name") == target
        )
    return False


def run(workspace: Path) -> int:
    rng = secrets.SystemRandom()
    products = _random_products(rng)
    products.extend(["O'Reilly-" + secrets.token_hex(4), "literal-%-" + secrets.token_hex(4)])
    target = products[1]
    port = _free_port()

    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/tmp",
        "DEMO_SEED_PRODUCTS": ",".join(products),
        "FLASK_SKIP_DOTENV": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    runner = Path(__file__).resolve().parent.parent / "runners" / "run_flask.py"
    command = [
        sys.executable,
        "-I",
        str(runner),
        str(workspace),
        "127.0.0.1",
        str(port),
    ]
    identity = {}
    if os.environ.get("PATCHCAGE_VERIFY") == "1":
        if os.geteuid() != 0:
            raise RuntimeError("verification oracle requires its isolated verifier identity")
        identity = {"user": 65534, "group": 65534, "extra_groups": []}
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **identity,
    )
    try:
        if not _wait_for_application(port, target):
            print(f"{ERROR_MARKER}: application did not start or failed sanity search")
            return 2

        for payload in _injection_payloads(rng):
            rows = _get_json(port, payload)
            if not isinstance(rows, list):
                print(f"{ERROR_MARKER}: unexpected response type {type(rows).__name__}")
                return 2
            if rows:
                print(VULNERABLE_MARKER)
                return 1

        # Prevent "fixes" that disable search, reject every quote, or return one
        # arbitrary product for every request. LIKE wildcard behavior is retained.
        for query, expected in [
            (target, [target]),
            ("not-present-" + secrets.token_hex(8), []),
            (products[3], [products[3]]),
            ("%", products),
        ]:
            rows = _get_json(port, query)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                print(f"{ERROR_MARKER}: invalid search response")
                return 2
            names = [row.get("name") for row in rows]
            if sorted(names, key=str) != sorted(expected):
                print(f"{ERROR_MARKER}: legitimate search behavior changed")
                return 2
        print(SECURE_MARKER)
        return 0
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"{ERROR_MARKER}: {type(error).__name__}: {error}")
        return 2
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    args = parser.parse_args()
    return run(args.workspace.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
