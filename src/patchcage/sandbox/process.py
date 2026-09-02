from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from patchcage.domain import CheckResult, CheckStatus

MAX_OUTPUT_BYTES = 64_000


def bounded_text(raw: bytes) -> tuple[str, bool]:
    truncated = len(raw) > MAX_OUTPUT_BYTES
    return raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), truncated


def run_local_command(
    argv: Sequence[str],
    *,
    name: str,
    timeout_seconds: int,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        duration_ms = int((time.monotonic() - started) * 1000)
        return CheckResult(
            name=name,
            status=CheckStatus.ERROR,
            exit_code=None,
            duration_ms=duration_ms,
            summary=f"command not found: {error}",
        )
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - started) * 1000)
        return CheckResult(
            name=name,
            status=CheckStatus.ERROR,
            exit_code=None,
            duration_ms=duration_ms,
            summary=f"{name} exceeded {timeout_seconds}s",
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout, stdout_truncated = bounded_text(completed.stdout)
    stderr, stderr_truncated = bounded_text(completed.stderr)
    truncated = stdout_truncated or stderr_truncated
    combined = stdout.strip() or stderr.strip() or f"exit {completed.returncode}"
    summary = combined.splitlines()[0][:2_000]
    if truncated:
        summary = f"truncated: {summary}"[:2_000]
    status = CheckStatus.PASSED if completed.returncode == 0 else CheckStatus.FAILED
    return CheckResult(
        name=name,
        status=status,
        exit_code=completed.returncode,
        duration_ms=duration_ms,
        summary=summary,
    )
