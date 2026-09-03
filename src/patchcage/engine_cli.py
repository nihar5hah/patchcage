"""Subprocess CLI for the PatchCage engine.

JSON-lines on stdout; diagnostics on stderr. `run` never exports — a second
`export` invocation writes `final.patch` after the persisted run is approved.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import yaml
from docker.errors import DockerException
from pydantic import ValidationError

from patchcage.domain import (
    RunEvent,
    RunPhase,
    load_finding,
    load_manifest,
    load_scripted_actions,
)
from patchcage.gateway import OpenAICompatGateway, ScriptedGateway
from patchcage.harness.docker_session import docker_session_factory
from patchcage.harness.runner import HarnessRunner, RunRequest, RunResult
from patchcage.sandbox.docker_runtime import DockerRuntime, SandboxError
from patchcage.snapshot import SnapshotError

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SIGINT = 130
EXIT_SIGTERM = 143

_RUN_STATE = "run_state.json"
_CANDIDATE = "candidate.patch"
_EVIDENCE = "evidence.json"
_FINAL_PATCH = "final.patch"


class ExportError(ValueError):
    """Export refused: the run directory is not an approvable artifact."""


def _out_mutates_run(run_dir: Path, out_dir: Path) -> bool:
    run = run_dir.resolve()
    out = out_dir.resolve()
    return out == run or run in out.parents


def export_run(run_dir: Path, out_dir: Path) -> str:
    """Copy `candidate.patch` → `final.patch` plus evidence if the run is gated.

    Does not mutate `run_dir`. Fails closed on a missing or unreadable
    artifact, or when `candidate.patch` bytes do not match `candidate_sha256`.
    Evidence is copied as stored; it is not independently hashed.
    Returns the verified SHA-256 hex digest of the patch.
    """
    if _out_mutates_run(run_dir, out_dir):
        raise ExportError(
            "export refused: --out must not be the run directory or inside it"
        )
    state_path = run_dir / _RUN_STATE
    patch_path = run_dir / _CANDIDATE
    evidence_path = run_dir / _EVIDENCE
    if not state_path.is_file():
        raise ExportError(f"export refused: {_RUN_STATE} is missing")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise ExportError("export refused: run_state.json is unreadable") from error
    except json.JSONDecodeError as error:
        raise ExportError("export refused: run_state.json is not valid JSON") from error
    if not isinstance(state, dict):
        raise ExportError("export refused: run_state.json root must be an object")
    phase = state.get("phase")
    if phase != RunPhase.AWAITING_APPROVAL.value:
        raise ExportError(f"export refused: phase is {phase!r}, not awaiting_approval")
    stored = state.get("candidate_sha256")
    if not isinstance(stored, str) or not stored:
        raise ExportError("export refused: candidate_sha256 is missing")
    if not patch_path.is_file():
        raise ExportError(f"export refused: {_CANDIDATE} is missing")
    if not evidence_path.is_file():
        raise ExportError(f"export refused: {_EVIDENCE} is missing")
    try:
        patch_bytes = patch_path.read_bytes()
        evidence_bytes = evidence_path.read_bytes()
    except OSError as error:
        raise ExportError("export refused: run artifact is unreadable") from error
    digest = hashlib.sha256(patch_bytes).hexdigest()
    if digest != stored:
        raise ExportError("export refused: candidate.patch hash does not match run state")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / _FINAL_PATCH).write_bytes(patch_bytes)
        (out_dir / _EVIDENCE).write_bytes(evidence_bytes)
    except OSError as error:
        raise ExportError(f"export refused: could not write bundle: {error}") from error
    return digest


def _emit(event: RunEvent) -> None:
    print(event.model_dump_json(), flush=True)


def _emit_result(result: RunResult) -> None:
    payload = {
        "event_type": "result",
        "status": result.phase.value,
        "run_dir": result.run_dir,
        "diff_ref": result.candidate_sha256,
        "evidence_path": str(Path(result.run_dir) / _EVIDENCE),
        "detail": result.detail,
        "checks": [check.model_dump(mode="json") for check in result.check_results],
    }
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def _repo_head(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        raise ValueError(f"could not read HEAD from {repo}: {detail}")
    sha = proc.stdout.strip()
    if not sha:
        raise ValueError(f"could not read HEAD from {repo}: empty rev-parse output")
    return sha


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patchcage-engine",
        description=(
            "Drive a PatchCage sandbox run as a subprocess. Events are JSON lines on "
            "stdout. run stops at awaiting_approval; export is a second invocation."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="snapshot, sandbox, and verify a finding")
    run.add_argument("--repo", type=Path, required=True, help="git repository to snapshot")
    run.add_argument("--manifest", type=Path, required=True, help="project manifest YAML/JSON")
    run.add_argument(
        "--finding",
        type=Path,
        required=True,
        help="Finding YAML/JSON matching the Finding model (not free text)",
    )
    run.add_argument("--instructions", help="optional steering text for the model")
    run.add_argument("--commit", help="commit SHA to snapshot (default: repo HEAD)")
    run.add_argument(
        "--run-dir",
        type=Path,
        help="directory for run_state.json / candidate.patch / evidence.json "
        "(default: .patchcage/runs/<id>)",
    )
    run.add_argument(
        "--model-endpoint",
        help=(
            "OpenAI-compatible base URL (required unless --scripted). "
            "Authorization uses PATCHCAGE_MODEL_API_KEY when that env var is set; "
            "extra headers from PATCHCAGE_MODEL_HTTP_HEADERS (JSON object)"
        ),
    )
    run.add_argument(
        "--model-id",
        help="model id at that endpoint (required unless --scripted)",
    )
    run.add_argument(
        "--scripted",
        type=Path,
        metavar="ACTIONS",
        help="replay a JSON/YAML list of AgentAction objects (tests/demos); skips the live model",
    )
    run.set_defaults(_handler=_cmd_run)

    export = sub.add_parser(
        "export",
        help="write final.patch + evidence.json from an awaiting_approval run directory",
    )
    export.add_argument("--run", type=Path, required=True, dest="run_dir", help="run directory")
    export.add_argument(
        "--out",
        type=Path,
        required=True,
        help="directory that receives final.patch and evidence.json",
    )
    export.set_defaults(_handler=_cmd_export)
    return parser


def _cmd_export(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    try:
        digest = export_run(args.run_dir, args.out)
    except ExportError as error:
        print(str(error), file=sys.stderr)
        return EXIT_FAIL
    payload = {
        "event_type": "result",
        "status": "exported",
        "run_dir": str(args.run_dir),
        "out_dir": str(args.out),
        "diff_ref": digest,
        "detail": "exported final.patch and evidence.json",
    }
    print(json.dumps(payload, separators=(",", ":")), flush=True)
    return EXIT_OK


def _cmd_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.scripted is None and (not args.model_endpoint or not args.model_id):
        parser.error("run without --scripted requires --model-endpoint and --model-id")
    try:
        manifest = load_manifest(args.manifest)
        finding = load_finding(args.finding)
        commit = args.commit or _repo_head(args.repo)
        if args.scripted is not None:
            gateway: OpenAICompatGateway | ScriptedGateway = ScriptedGateway(
                load_scripted_actions(args.scripted)
            )
        else:
            assert args.model_endpoint is not None and args.model_id is not None
            gateway = OpenAICompatGateway(args.model_endpoint, args.model_id)
        request = RunRequest(
            repo=args.repo,
            commit=commit,
            manifest=manifest,
            finding=finding,
            instructions=args.instructions,
        )
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as error:
        print(f"invalid run arguments: {error}", file=sys.stderr)
        return EXIT_FAIL

    run_dir = args.run_dir or (Path.cwd() / ".patchcage" / "runs" / uuid4().hex)
    if (run_dir / _RUN_STATE).is_file():
        print(
            f"invalid run arguments: {run_dir} already contains {_RUN_STATE}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    try:
        return asyncio.run(_run_engine(request, gateway, run_dir))
    except KeyboardInterrupt:
        return EXIT_SIGINT


async def _run_engine(
    request: RunRequest,
    gateway: OpenAICompatGateway | ScriptedGateway,
    run_dir: Path,
) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    received: list[int] = []

    def _on_signal(sig: int) -> None:
        received.append(sig)
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig)

    try:
        try:
            runtime = DockerRuntime()
        except DockerException as error:
            print(f"docker daemon unavailable: {error}", file=sys.stderr)
            return EXIT_FAIL
        runner = HarnessRunner(
            gateway=gateway,
            session_factory=docker_session_factory(
                runtime=runtime,
                image=request.manifest.runtime.image,
                manifest=request.manifest,
                finding=request.finding,
            ),
            run_dir=run_dir,
            on_event=_emit,
        )
        try:
            result = await runner.run(request)
        except asyncio.CancelledError:
            # Distinct 130/143 only for in-flight cancel. A SIGTERM that
            # arrives after runner.run has already returned is ignored: the
            # verified result is emitted and the process exits 0.
            _emit_cancelled(run_dir)
            if signal.SIGTERM in received:
                return EXIT_SIGTERM
            return EXIT_SIGINT
        _emit_result(result)
        return EXIT_OK if result.phase is RunPhase.AWAITING_APPROVAL else EXIT_FAIL
    except DockerException as error:
        print(f"docker error: {error}", file=sys.stderr)
        return EXIT_FAIL
    except (SandboxError, SnapshotError) as error:
        print(str(error), file=sys.stderr)
        return EXIT_FAIL
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        closer = getattr(gateway, "aclose", None)
        if closer is not None:
            with suppress(Exception, asyncio.CancelledError):
                await closer()


def _emit_cancelled(run_dir: Path) -> None:
    print(
        json.dumps(
            {
                "event_type": "result",
                "status": "cancelled",
                "run_dir": str(run_dir),
                "diff_ref": None,
                "detail": "cancelled",
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(None if argv is None else list(argv))
    return int(args._handler(args, parser))


if __name__ == "__main__":
    raise SystemExit(main())
