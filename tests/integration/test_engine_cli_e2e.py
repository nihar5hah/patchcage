"""Scripted engine CLI against the Docker sandbox. Same skip as the pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from patchcage.engine_cli import EXIT_OK, EXIT_SIGTERM, main
from patchcage.sandbox.docker_runtime import LABEL_MANAGED
from patchcage.sandbox.image import build_runtime_image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CREATE_DEMO = PROJECT_ROOT / "scripts" / "create_demo_repo.py"
FIX_PATCH = PROJECT_ROOT / "tests" / "fixtures" / "sql_injection_fix.patch"
MANIFEST_PATH = PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"
FINDING_PATH = PROJECT_ROOT / "manifests" / "flask_sql_injection.finding.yml"


def _docker_ready() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_ready(), reason="Docker daemon is not running"),
]


@pytest.fixture(scope="session")
def runtime_image_id() -> str:
    return build_runtime_image()


def _managed_container_ids() -> set[str]:
    import docker

    client = docker.from_env()
    return {
        str(container.id)
        for container in client.containers.list(
            all=True, filters={"label": f"{LABEL_MANAGED}=true"}
        )
    }


def _write_scripted(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "type": "tool",
                    "tool": "read_file",
                    "arguments": {"path": "src/demo_app/search.py"},
                    "summary": "Inspect the vulnerable query.",
                },
                {
                    "type": "patch",
                    "diff": FIX_PATCH.read_text(),
                    "summary": "Parameterize the query.",
                },
            ]
        )
    )


def _make_demo(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "sql-demo"
    created = subprocess.run(
        [sys.executable, str(CREATE_DEMO), str(repository)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return repository, json.loads(created.stdout)["commit_sha"]


@pytest.mark.usefixtures("runtime_image_id")
def test_scripted_cli_run_then_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repository, commit = _make_demo(tmp_path)
    scripted = tmp_path / "actions.json"
    _write_scripted(scripted)
    run_dir = tmp_path / "run"
    code = main(
        [
            "run",
            "--repo",
            str(repository),
            "--commit",
            commit,
            "--manifest",
            str(MANIFEST_PATH),
            "--finding",
            str(FINDING_PATH),
            "--scripted",
            str(scripted),
            "--run-dir",
            str(run_dir),
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    phases = [
        event.get("payload", {}).get("phase") or event.get("phase")
        for event in events
        if event.get("event_type") == "phase"
    ]
    assert "investigating" in phases
    assert phases[-1] == "awaiting_approval"
    result = events[-1]
    assert result["event_type"] == "result"
    assert result["status"] == "awaiting_approval"
    assert (run_dir / "candidate.patch").is_file()
    assert (run_dir / "evidence.json").is_file()
    assert not (run_dir / "final.patch").exists()

    out = tmp_path / "bundle"
    assert main(["export", "--run", str(run_dir), "--out", str(out)]) == EXIT_OK
    capsys.readouterr()
    exported = (out / "final.patch").read_bytes()
    assert exported == (run_dir / "candidate.patch").read_bytes()
    assert hashlib.sha256(exported).hexdigest() == json.loads(
        (run_dir / "run_state.json").read_text()
    )["candidate_sha256"]
    assert (out / "evidence.json").is_file()


@pytest.mark.usefixtures("runtime_image_id")
def test_sigterm_after_investigating_exits_143(tmp_path: Path) -> None:
    repository, commit = _make_demo(tmp_path)
    scripted = tmp_path / "actions.json"
    _write_scripted(scripted)
    run_dir = tmp_path / "run"
    before = _managed_container_ids()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    stderr_log = tmp_path / "stderr.log"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "patchcage.engine_cli",
            "run",
            "--repo",
            str(repository),
            "--commit",
            commit,
            "--manifest",
            str(MANIFEST_PATH),
            "--finding",
            str(FINDING_PATH),
            "--scripted",
            str(scripted),
            "--run-dir",
            str(run_dir),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=stderr_log.open("w"),
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + 180
    saw_investigating = False
    stdout_lines: list[str] = []
    try:
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line:
                stdout_lines.append(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                phase = event.get("phase") or event.get("payload", {}).get("phase")
                if event.get("event_type") == "phase" and phase == "investigating":
                    saw_investigating = True
                    os.kill(proc.pid, signal.SIGTERM)
                    rest, _ = proc.communicate(timeout=60)
                    if rest:
                        stdout_lines.append(rest)
                    break
            elif proc.poll() is not None:
                break
        err = stderr_log.read_text()
        assert saw_investigating, (
            f"never reached investigating; exit={proc.poll()} "
            f"stdout={''.join(stdout_lines)!r} stderr={err!r}"
        )
        if proc.poll() is None:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                pytest.fail("engine CLI did not exit after SIGTERM")
        err = stderr_log.read_text()
        assert proc.returncode == EXIT_SIGTERM, (
            f"returncode={proc.returncode} stdout={''.join(stdout_lines)!r} stderr={err!r}"
        )
        cancelled = any(
            '"status":"cancelled"' in line or '"status": "cancelled"' in line
            for line in stdout_lines
        )
        assert cancelled, (
            f"missing cancelled result on stdout: {''.join(stdout_lines)!r}"
        )
        if (run_dir / "run_state.json").is_file():
            state = json.loads((run_dir / "run_state.json").read_text())
            assert state.get("phase") == "cancelled"
        after = _managed_container_ids()
        assert after <= before, f"leaked managed containers: {after - before}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
