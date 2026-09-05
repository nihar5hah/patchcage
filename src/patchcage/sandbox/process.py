from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from patchcage.domain import CheckResult, CheckStatus

MAX_OUTPUT_BYTES = 64_000


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
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
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
    stdout_bytes = bytearray()
    stderr_bytes = bytearray()
    failure: str | None = None
    assert process.stdout is not None and process.stderr is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, stdout_bytes)
            selector.register(process.stderr, selectors.EVENT_READ, stderr_bytes)
            while selector.get_map():
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    failure = f"{name} exceeded {timeout_seconds}s"
                    break
                for key, _ in selector.select(timeout=remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(stdout_bytes) + len(stderr_bytes) + len(chunk) > MAX_OUTPUT_BYTES:
                        failure = f"{name} exceeded {MAX_OUTPUT_BYTES} output bytes"
                        break
                    key.data.extend(chunk)
                if failure:
                    break
        if failure is None:
            try:
                process.wait(timeout=max(0.01, timeout_seconds - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                failure = f"{name} exceeded {timeout_seconds}s"
    finally:
        # Kill descendants too, including children keeping output pipes open.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        process.stdout.close()
        process.stderr.close()
    duration_ms = int((time.monotonic() - started) * 1000)
    if failure is not None:
        return CheckResult(
            name=name,
            status=CheckStatus.ERROR,
            exit_code=None,
            duration_ms=duration_ms,
            summary=failure,
        )
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    combined = stdout.strip() or stderr.strip() or f"exit {process.returncode}"
    summary = combined.splitlines()[0][:2_000]
    status = CheckStatus.PASSED if process.returncode == 0 else CheckStatus.FAILED
    return CheckResult(
        name=name,
        status=status,
        exit_code=process.returncode,
        duration_ms=duration_ms,
        summary=summary,
    )
