from __future__ import annotations

import sys

from patchcage.domain import CheckStatus
from patchcage.sandbox.process import run_local_command


def test_output_flood_is_stopped_without_buffering_it_all() -> None:
    result = run_local_command(
        [sys.executable, "-c", "import os\nwhile True: os.write(1, b'x' * 8192)"],
        name="unit",
        timeout_seconds=5,
    )
    assert result.status is CheckStatus.ERROR
    assert "output bytes" in result.summary
    assert result.duration_ms < 5000


def test_descendant_holding_stdout_does_not_escape_deadline() -> None:
    result = run_local_command(
        [
            sys.executable,
            "-c",
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])",
        ],
        name="unit",
        timeout_seconds=1,
    )
    assert result.status is CheckStatus.ERROR
    assert result.duration_ms < 3000


def test_normal_output_preserves_exit_and_summary() -> None:
    result = run_local_command(
        [sys.executable, "-c", "print('passed'); raise SystemExit(1)"],
        name="unit",
        timeout_seconds=5,
    )
    assert result.status is CheckStatus.FAILED
    assert result.exit_code == 1
    assert result.summary == "passed"
