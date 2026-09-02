"""Engine CLI: export gate and argparse. No Docker."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from patchcage.domain import Finding, FindingSource, load_finding
from patchcage.engine_cli import ExportError, export_run, main

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FINDING_PATH = PROJECT_ROOT / "manifests" / "flask_sql_injection.finding.yml"
PATCH = b"diff --git a/x b/x\n"


def _approval_run(
    tmp_path: Path,
    *,
    phase: str = "awaiting_approval",
    digest: str | None = "auto",
    write_patch: bool = True,
    write_evidence: bool = True,
    patch: bytes = PATCH,
) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if write_patch:
        (run_dir / "candidate.patch").write_bytes(patch)
    if digest == "auto":
        stored: str | None = hashlib.sha256(patch).hexdigest()
    else:
        stored = digest
    (run_dir / "run_state.json").write_text(
        json.dumps(
            {
                "run_id": "abc",
                "phase": phase,
                "candidate_sha256": stored,
                "detail": "ok",
            }
        )
        + "\n"
    )
    if write_evidence:
        (run_dir / "evidence.json").write_text(
            json.dumps({"run_id": "abc", "phase": phase, "checks": []}) + "\n"
        )
    return run_dir


def test_export_refuses_wrong_phase(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path, phase="investigating")
    with pytest.raises(ExportError, match="not awaiting_approval"):
        export_run(run_dir, tmp_path / "out")
    assert not (tmp_path / "out" / "final.patch").exists()


def test_export_refuses_missing_hash(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path, digest=None)
    with pytest.raises(ExportError, match="candidate_sha256 is missing"):
        export_run(run_dir, tmp_path / "out")


def test_export_refuses_missing_patch(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path, write_patch=False)
    with pytest.raises(ExportError, match="candidate.patch is missing"):
        export_run(run_dir, tmp_path / "out")


def test_export_refuses_tampered_patch(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path)
    (run_dir / "candidate.patch").write_bytes(PATCH + b"tamper\n")
    with pytest.raises(ExportError, match="hash does not match"):
        export_run(run_dir, tmp_path / "out")


def test_export_refuses_missing_evidence(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path, write_evidence=False)
    with pytest.raises(ExportError, match="evidence.json is missing"):
        export_run(run_dir, tmp_path / "out")


def test_export_refuses_missing_run_state(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ExportError, match="run_state.json is missing"):
        export_run(run_dir, tmp_path / "out")


def test_export_refuses_invalid_json(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path)
    (run_dir / "run_state.json").write_text("{not json")
    with pytest.raises(ExportError, match="not valid JSON"):
        export_run(run_dir, tmp_path / "out")


def test_export_refuses_non_object_root(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path)
    (run_dir / "run_state.json").write_text("[]\n")
    with pytest.raises(ExportError, match="root must be an object"):
        export_run(run_dir, tmp_path / "out")


def test_export_refuses_unreadable_run_state(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path)
    (run_dir / "run_state.json").write_bytes(b"\xff\xfe")
    with pytest.raises(ExportError, match="unreadable"):
        export_run(run_dir, tmp_path / "out")


def test_export_refuses_out_equal_to_run_dir(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path)
    with pytest.raises(ExportError, match="must not be the run directory"):
        export_run(run_dir, run_dir)
    assert not (run_dir / "final.patch").exists()


def test_export_refuses_out_inside_run_dir(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path)
    with pytest.raises(ExportError, match="must not be the run directory"):
        export_run(run_dir, run_dir / "bundle")
    assert not (run_dir / "bundle").exists()


def test_export_success_does_not_mutate_run_dir(tmp_path: Path) -> None:
    run_dir = _approval_run(tmp_path)
    before_state = (run_dir / "run_state.json").read_text()
    before_patch = (run_dir / "candidate.patch").read_bytes()
    before_evidence = (run_dir / "evidence.json").read_bytes()
    out = tmp_path / "out"
    export_run(run_dir, out)
    assert (out / "final.patch").read_bytes() == PATCH
    assert (out / "evidence.json").read_bytes() == before_evidence
    assert (run_dir / "run_state.json").read_text() == before_state
    assert (run_dir / "candidate.patch").read_bytes() == before_patch
    assert (run_dir / "evidence.json").read_bytes() == before_evidence
    assert not (run_dir / "final.patch").exists()
    export_run(run_dir, tmp_path / "out2")
    assert (tmp_path / "out2" / "final.patch").read_bytes() == PATCH


def test_export_cli_writes_result_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = _approval_run(tmp_path)
    out = tmp_path / "exported"
    assert main(["export", "--run", str(run_dir), "--out", str(out)]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event_type"] == "result"
    assert payload["status"] == "exported"
    assert payload["diff_ref"] == hashlib.sha256(PATCH).hexdigest()


def test_export_cli_refuses_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _approval_run(tmp_path, phase="cancelled")
    assert main(["export", "--run", str(run_dir), "--out", str(tmp_path / "out")]) == 1
    captured = capsys.readouterr()
    assert "export refused" in captured.err
    assert captured.out == ""


def test_live_run_requires_model_flags() -> None:
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "run",
                "--repo",
                ".",
                "--manifest",
                "missing.yml",
                "--finding",
                "missing.yml",
            ]
        )
    assert exited.value.code == 2


def test_scripted_run_does_not_require_model_flags(tmp_path: Path) -> None:
    scripted = tmp_path / "actions.json"
    scripted.write_text("[]")
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--manifest",
            str(tmp_path / "nope.yml"),
            "--finding",
            str(tmp_path / "nope.yml"),
            "--scripted",
            str(scripted),
        ]
    )
    assert code == 1


def test_load_finding_matches_demo_yaml() -> None:
    finding = load_finding(FINDING_PATH)
    assert finding == Finding(
        id="sql-1",
        source=FindingSource.SEMGREP_SARIF,
        rule_id="patchcage.python.sql-injection.formatted-query",
        title="SQL injection via formatted query",
        description="User input is interpolated into a SQL execute call.",
        severity="ERROR",
        file_path="src/demo_app/search.py",
        start_line=20,
        verification_recipe="sql_injection_oracle",
    )


def test_console_script_entry_point() -> None:
    from importlib.metadata import entry_points

    eps = entry_points(group="console_scripts")
    match = [ep for ep in eps if ep.name == "patchcage-engine"]
    if not match:
        pytest.skip("editable install does not expose patchcage-engine")
    assert match[0].value == "patchcage.engine_cli:main"


def test_overlong_commit_exits_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted = tmp_path / "actions.json"
    scripted.write_text("[]")
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--commit",
            "a" * 101,
            "--manifest",
            str(PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"),
            "--finding",
            str(FINDING_PATH),
            "--scripted",
            str(scripted),
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert code == 1
    assert "invalid run arguments" in capsys.readouterr().err


def test_existing_run_dir_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _approval_run(tmp_path)
    scripted = tmp_path / "actions.json"
    scripted.write_text("[]")
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--commit",
            "0" * 40,
            "--manifest",
            str(PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"),
            "--finding",
            str(FINDING_PATH),
            "--scripted",
            str(scripted),
            "--run-dir",
            str(run_dir),
        ]
    )
    assert code == 1
    assert "already contains run_state.json" in capsys.readouterr().err


def test_docker_daemon_unavailable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from docker.errors import DockerException

    class BoomRuntime:
        def __init__(self) -> None:
            raise DockerException("down")

    monkeypatch.setattr("patchcage.engine_cli.DockerRuntime", BoomRuntime)
    scripted = tmp_path / "actions.json"
    scripted.write_text("[]")
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--commit",
            "0" * 40,
            "--manifest",
            str(PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"),
            "--finding",
            str(FINDING_PATH),
            "--scripted",
            str(scripted),
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert code == 1
    assert "docker daemon unavailable" in capsys.readouterr().err


def test_scripted_actions_must_be_a_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted = tmp_path / "actions.json"
    scripted.write_text("{}")
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--commit",
            "0" * 40,
            "--manifest",
            str(PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"),
            "--finding",
            str(FINDING_PATH),
            "--scripted",
            str(scripted),
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert code == 1
    assert "invalid run arguments" in capsys.readouterr().err


def test_overlong_instructions_exit_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted = tmp_path / "actions.json"
    scripted.write_text("[]")
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--commit",
            "0" * 40,
            "--manifest",
            str(PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"),
            "--finding",
            str(FINDING_PATH),
            "--instructions",
            "x" * 4001,
            "--scripted",
            str(scripted),
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert code == 1
    assert "invalid run arguments" in capsys.readouterr().err


async def test_run_engine_cancelled_without_sigterm_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from patchcage.domain import load_finding, load_manifest
    from patchcage.engine_cli import EXIT_SIGINT, _run_engine
    from patchcage.gateway import ScriptedGateway
    from patchcage.harness.runner import RunRequest

    async def cancelled_run(self: object, request: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr("patchcage.engine_cli.HarnessRunner.run", cancelled_run)
    monkeypatch.setattr("patchcage.engine_cli.DockerRuntime", object)
    monkeypatch.setattr(
        "patchcage.engine_cli.docker_session_factory",
        lambda **kwargs: object(),
    )
    request = RunRequest(
        repo=tmp_path,
        commit="0" * 40,
        manifest=load_manifest(PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"),
        finding=load_finding(FINDING_PATH),
    )
    code = await _run_engine(request, ScriptedGateway(()), tmp_path / "run")
    assert code == EXIT_SIGINT


async def test_run_engine_aclose_cancel_does_not_replace_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from patchcage.domain import RunPhase, load_finding, load_manifest
    from patchcage.engine_cli import EXIT_OK, _run_engine
    from patchcage.harness.runner import RunRequest, RunResult

    class BoomClose:
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    async def ok_run(self: object, request: object) -> RunResult:
        return RunResult(
            run_id="x",
            phase=RunPhase.AWAITING_APPROVAL,
            run_dir=str(tmp_path / "run"),
            detail="ok",
        )

    monkeypatch.setattr("patchcage.engine_cli.HarnessRunner.run", ok_run)
    monkeypatch.setattr("patchcage.engine_cli.DockerRuntime", object)
    monkeypatch.setattr(
        "patchcage.engine_cli.docker_session_factory",
        lambda **kwargs: object(),
    )
    request = RunRequest(
        repo=tmp_path,
        commit="0" * 40,
        manifest=load_manifest(PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"),
        finding=load_finding(FINDING_PATH),
    )
    code = await _run_engine(request, BoomClose(), tmp_path / "run")  # type: ignore[arg-type]
    assert code == EXIT_OK


def test_help_says_out_is_a_directory(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["export", "--help"])
    assert exited.value.code == 0
    assert "directory that receives final.patch" in capsys.readouterr().out
