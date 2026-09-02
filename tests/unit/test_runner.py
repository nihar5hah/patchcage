"""Runner tests: scripted gateways + in-memory sessions, no Docker."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from patchcage.domain import (
    AgentAction,
    CheckResult,
    CheckStatus,
    CompletionAction,
    Finding,
    FindingSource,
    PatchAction,
    ProjectManifest,
    RunPhase,
    ToolAction,
)
from patchcage.gateway import (
    InvalidModelOutput,
    ModelHealth,
    ModelUnavailable,
    ScriptedGateway,
)
from patchcage.harness.runner import HarnessRunner, RunRequest
from patchcage.snapshot import SnapshotArtifact, SnapshotError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIX_PATCH = PROJECT_ROOT / "tests" / "fixtures" / "sql_injection_fix.patch"

ALL_TOOLS = [
    "get_finding",
    "list_files",
    "read_file",
    "search_code",
    "get_repository_status",
    "run_finding_check",
    "run_named_check",
    "propose_patch",
    "get_current_diff",
    "discard_patch",
]


def _manifest(extra_limits: dict[str, int] | None = None) -> ProjectManifest:
    spec: dict[str, Any] = {
        "version": 1,
        "project": {"name": "demo", "language": "python"},
        "runtime": {"image": "patchcage/python-demo:dev"},
        "scope": {
            "readable": ["src/**", "tests/**"],
            "writable": ["src/**"],
            "blocked": [".git/**"],
        },
        "checks": {
            "compile": {
                "argv": ["python", "-m", "compileall", "-q", "src"],
                "timeout_seconds": 30,
            },
            "scanner": {
                "argv": ["semgrep", "scan", "--error", "--json"],
                "timeout_seconds": 60,
                "baseline_expectation": "finding_present",
                "patched_expectation": "finding_absent",
            },
            "unit": {"argv": ["pytest", "-q"], "timeout_seconds": 60},
            "security": {
                "argv": ["python", "oracle.py"],
                "timeout_seconds": 60,
                "baseline_expectation": "vulnerability_reproduced",
                "baseline_required_marker": "PATCHCAGE_VULNERABILITY_REPRODUCED",
            },
        },
    }
    if extra_limits:
        spec["limits"] = extra_limits
    return ProjectManifest.model_validate(spec)


def _finding() -> Finding:
    return Finding(
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


def _ok(name: str) -> CheckResult:
    return CheckResult(
        name=name, status=CheckStatus.PASSED, exit_code=0, duration_ms=1, summary="ok"
    )


def _failed(name: str, summary: str = "boom") -> CheckResult:
    return CheckResult(
        name=name, status=CheckStatus.FAILED, exit_code=1, duration_ms=1, summary=summary
    )


def _baseline_script() -> list[CheckResult]:
    return [
        _ok("compile"),
        _failed("scanner", "semgrep: 1 finding"),  # exit 1 with --error = finding present
        _failed("security", "PATCHCAGE_VULNERABILITY_REPRODUCED"),
        _ok("unit"),
    ]


def _verified_script() -> list[CheckResult]:
    return [_ok("compile"), _ok("scanner"), _ok("security"), _ok("unit")]


def _read_action() -> ToolAction:
    return ToolAction(
        type="tool",
        tool="read_file",
        arguments={"path": "src/demo_app/search.py"},
        summary="Inspect the vulnerable query.",
    )


def _patch_action(diff: str | None = None) -> PatchAction:
    return PatchAction(
        type="patch",
        diff=diff if diff is not None else FIX_PATCH.read_text(),
        summary="Parameterize the query.",
    )


def _complete_action() -> CompletionAction:
    return CompletionAction(type="complete", summary="Done.", evidence_ids=())


def _snapshot(
    repository: Path, commit: str, *, blocked_patterns: tuple[str, ...]
) -> SnapshotArtifact:
    return SnapshotArtifact(
        commit_sha=commit,
        raw_sha256="0" * 64,
        snapshot_sha256="0" * 64,
        sanitized_archive_sha256="0" * 64,
        entries=(),
        archive=b"",
    )


def _regenerate_diff(diff: str) -> str:
    """Mimic git's regenerated diff text: an index line the model never sent.

    Keeps the candidate-diff hash distinct from the submitted-diff hash, like
    the real workspace — the loop supervisor must track one hash domain.
    """
    if not diff:
        return diff
    first, _, rest = diff.partition("\n")
    return f"{first}\nindex 0000000..1111111 100644\n{rest}"


class FakeSession:
    """In-memory WorkspaceSession. Host checks are consumed from a script."""

    def __init__(self, check_script: list[CheckResult]) -> None:
        self._check_script = list(check_script)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._diff = ""
        self.baseline_sha = "0" * 40
        self.closed = False

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def list_tools(self) -> list[str]:
        return list(ALL_TOOLS)

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        if tool == "propose_patch":
            self._diff = str(arguments["diff"])
            return {
                "applied": True,
                "sha256": hashlib.sha256(self._diff.encode()).hexdigest(),
                "files": ["src/demo_app/search.py"],
            }
        if tool == "get_current_diff":
            regenerated = _regenerate_diff(self._diff)
            return {"diff": regenerated, "dirty": bool(self._diff)}
        return {}

    def run_host_check(self, name: str) -> CheckResult:
        assert self._check_script, f"no scripted check result left for {name}"
        result = self._check_script.pop(0)
        assert result.name == name, f"runner asked for {name}, script has {result.name}"
        return result


class Rig:
    """Builds a runner over scripted sessions and records events."""

    def __init__(
        self,
        gateway: object,
        session_scripts: list[list[CheckResult]],
        tmp_path: Path,
        extra_limits: dict[str, int] | None = None,
    ) -> None:
        self.sessions = [FakeSession(script) for script in session_scripts]
        self._session_index = 0
        self._extra_limits = extra_limits
        self.events: list = []
        self.run_dir = tmp_path / "run"
        self.runner = self._build(gateway)

    def _build(self, gateway: object) -> HarnessRunner:
        return HarnessRunner(
            gateway=gateway,  # type: ignore[arg-type]
            session_factory=self._next_session,
            run_dir=self.run_dir,
            snapshotter=_snapshot,
            on_event=self.events.append,
        )

    def _next_session(self, snapshot: SnapshotArtifact) -> FakeSession:
        session = self.sessions[self._session_index]
        self._session_index += 1
        return session

    async def run(self) -> object:
        request = RunRequest(
            repo=Path("/tmp/demo"),
            commit="0" * 40,
            manifest=_manifest(self._extra_limits),
            finding=_finding(),
        )
        return await self.runner.run(request)

    def phases(self) -> list[str]:
        return [e.payload["phase"] for e in self.events if e.event_type == "phase"]


async def test_happy_path_reaches_awaiting_approval(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_read_action(), _patch_action()])
    rig = Rig(gateway, [_baseline_script() + _verified_script(), _verified_script()], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.AWAITING_APPROVAL
    assert rig.phases() == [
        "preflight_validated",
        "snapshot_ready",
        "baseline_verified",
        "investigating",
        "finding_confirmed",
        "patch_enabled",
        "patch_validating",
        "working_verification",
        "clean_replay_verified",
        "awaiting_approval",
    ]
    assert result.candidate_patch is not None
    state = json.loads((rig.run_dir / "run_state.json").read_text())
    assert state["phase"] == "awaiting_approval"
    on_disk = (rig.run_dir / "candidate.patch").read_text()
    assert hashlib.sha256(on_disk.encode()).hexdigest() == state["candidate_sha256"]
    assert all(session.closed for session in rig.sessions)


async def test_model_unavailable_at_preflight(tmp_path: Path) -> None:
    gateway = ScriptedGateway([], healthy=False)
    rig = Rig(gateway, [[]], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.MODEL_UNAVAILABLE
    assert rig.sessions[0].closed is False  # sandbox never opened


async def test_snapshot_rejection_maps_to_source_rejected(tmp_path: Path) -> None:
    def rejecting(
        repository: Path, commit: str, *, blocked_patterns: tuple[str, ...]
    ) -> SnapshotArtifact:
        raise SnapshotError("NOT_A_GIT_REPOSITORY", "not a Git worktree")

    rig = Rig(ScriptedGateway([]), [[]], tmp_path)
    rig.runner = HarnessRunner(
        gateway=ScriptedGateway([]),
        session_factory=lambda snapshot: rig.sessions.pop(0),
        run_dir=rig.run_dir,
        snapshotter=rejecting,
        on_event=rig.events.append,
    )

    result = await rig.run()

    assert result.phase is RunPhase.SOURCE_REJECTED


async def test_invalid_output_budget_exhaustion(tmp_path: Path) -> None:
    class AlwaysInvalid:
        async def health(self) -> ModelHealth:
            return ModelHealth(ok=True)

        async def next_action(self, context: object) -> AgentAction:
            raise InvalidModelOutput("garbage")

    rig = Rig(AlwaysInvalid(), [_baseline_script()], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.INVALID_MODEL_OUTPUT


async def test_completion_before_patch_is_rejected(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_complete_action(), _patch_action()])
    rig = Rig(gateway, [_baseline_script() + _verified_script(), _verified_script()], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.AWAITING_APPROVAL
    seen = gateway.seen_contexts[-1]
    assert seen.last_tool_result is not None
    assert "rejected" in seen.last_tool_result


async def test_host_policy_rejects_blocked_patch_then_recovers(tmp_path: Path) -> None:
    evil = (
        "diff --git a/.git/config b/.git/config\n"
        "--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-x\n+pwned\n"
    )
    gateway = ScriptedGateway([_patch_action(evil), _patch_action()])
    rig = Rig(gateway, [_baseline_script() + _verified_script(), _verified_script()], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.AWAITING_APPROVAL
    seen = gateway.seen_contexts[-1]
    assert seen.last_tool_result is not None
    assert "rejected by host policy" in seen.last_tool_result


async def test_unknown_tool_is_a_policy_violation(tmp_path: Path) -> None:
    rogue = ToolAction(
        type="tool", tool="delete_everything", arguments={}, summary="Chaos."
    )
    gateway = ScriptedGateway([rogue, _patch_action()])
    rig = Rig(gateway, [_baseline_script() + _verified_script(), _verified_script()], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.AWAITING_APPROVAL
    seen = gateway.seen_contexts[-1]
    assert seen.last_tool_result is not None
    assert "unknown or host-only tool" in seen.last_tool_result


async def test_propose_patch_tool_is_hidden_from_the_model(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_patch_action()])
    rig = Rig(gateway, [_baseline_script() + _verified_script(), _verified_script()], tmp_path)

    await rig.run()

    assert "propose_patch" not in gateway.seen_contexts[0].available_tools
    assert "read_file" in gateway.seen_contexts[0].available_tools


async def test_repair_loop_after_failed_verification(tmp_path: Path) -> None:
    first_ladder = [_ok("compile"), _ok("scanner"), _ok("security"), _failed("unit", "1 failed")]
    gateway = ScriptedGateway([_patch_action(), _patch_action()])
    rig = Rig(
        gateway,
        [_baseline_script() + first_ladder + _verified_script(), _verified_script()],
        tmp_path,
    )

    result = await rig.run()

    assert result.phase is RunPhase.AWAITING_APPROVAL
    assert "repairing" in rig.phases()
    seen = gateway.seen_contexts[-1]
    assert seen.last_tool_result is not None
    assert "verification failed" in seen.last_tool_result
    assert seen.check_results[-1].name == "unit"


async def test_verification_failure_after_repair_budget(tmp_path: Path) -> None:
    failing = [_ok("compile"), _ok("scanner"), _ok("security"), _failed("unit")]
    # Two repairs allowed by default limits; all three ladders fail.
    gateway = ScriptedGateway([_patch_action(), _patch_action(), _patch_action()])
    rig = Rig(
        gateway,
        [_baseline_script() + failing + failing + failing],
        tmp_path,
    )

    result = await rig.run()

    assert result.phase is RunPhase.VERIFICATION_FAILED


async def test_patch_oscillation_is_blocked(tmp_path: Path) -> None:
    patch_a = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    patch_b = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+c\n"
    )
    failing = [_ok("compile"), _ok("scanner"), _ok("security"), _failed("unit")]
    gateway = ScriptedGateway(
        [_patch_action(patch_a), _patch_action(patch_b), _patch_action(patch_a), _patch_action()]
    )
    rig = Rig(
        gateway,
        [_baseline_script() + failing + failing + _verified_script(), _verified_script()],
        tmp_path,
        extra_limits={"patch_attempts": 4},
    )

    result = await rig.run()

    assert result.phase is RunPhase.AWAITING_APPROVAL
    outcomes = [
        turn.outcome
        for context in gateway.seen_contexts
        for turn in context.recent_actions
    ]
    assert any("oscillat" in outcome for outcome in outcomes)


async def test_stagnation_ends_the_run(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_complete_action()] * 10)
    rig = Rig(gateway, [_baseline_script()], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.BUDGET_EXHAUSTED


async def test_baseline_without_reproduction_is_rejected(tmp_path: Path) -> None:
    not_reproduced = [
        _ok("compile"),
        _failed("scanner", "semgrep: 1 finding"),
        _ok("security"),  # oracle did not reproduce
    ]
    rig = Rig(ScriptedGateway([_patch_action()]), [not_reproduced], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.FINDING_NOT_REPRODUCIBLE


async def test_model_unavailable_mid_run(tmp_path: Path) -> None:
    class DyingGateway:
        async def health(self) -> ModelHealth:
            return ModelHealth(ok=True)

        async def next_action(self, context: object) -> AgentAction:
            raise ModelUnavailable("connection reset")

    rig = Rig(DyingGateway(), [_baseline_script()], tmp_path)

    result = await rig.run()

    assert result.phase is RunPhase.MODEL_UNAVAILABLE
    state = json.loads((rig.run_dir / "run_state.json").read_text())
    assert state["phase"] == "model_unavailable"
    assert rig.sessions[0].closed


async def test_clean_replay_failure_ends_the_run(tmp_path: Path) -> None:
    gateway = ScriptedGateway([_patch_action()])
    replay_fail = [_ok("compile"), _failed("scanner"), _ok("security"), _ok("unit")]
    rig = Rig(
        gateway, [_baseline_script() + _verified_script(), replay_fail], tmp_path
    )

    result = await rig.run()

    assert result.phase is RunPhase.CLEAN_REPLAY_FAILED
    state = json.loads((rig.run_dir / "run_state.json").read_text())
    assert state["phase"] == "clean_replay_failed"


async def test_unexpected_session_error_persists_sandbox_error(tmp_path: Path) -> None:
    class BoomDiffSession(FakeSession):
        async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if tool == "get_current_diff":
                raise RuntimeError("mcp pipe closed")
            return await super().call(tool, arguments)

    gateway = ScriptedGateway([_patch_action()])
    work = BoomDiffSession(_baseline_script() + _verified_script())
    runner = HarnessRunner(
        gateway=gateway,
        session_factory=lambda snapshot: work,
        run_dir=tmp_path / "run",
        snapshotter=_snapshot,
    )
    request = RunRequest(
        repo=Path("/tmp/demo"),
        commit="0" * 40,
        manifest=_manifest(),
        finding=_finding(),
    )

    result = await runner.run(request)

    assert result.phase is RunPhase.SANDBOX_ERROR
    state = json.loads((tmp_path / "run" / "run_state.json").read_text())
    assert state["phase"] == "sandbox_error"
    assert work.closed


async def test_teardown_cancel_does_not_overwrite_concluded_run(tmp_path: Path) -> None:
    class CancelOnExit(FakeSession):
        async def __aexit__(self, *args: object) -> None:
            self.closed = True
            raise asyncio.CancelledError

    gateway = ScriptedGateway([_patch_action()])
    work = CancelOnExit(_baseline_script() + _verified_script())
    replay = FakeSession(_verified_script())
    sessions = [work, replay]
    index = {"n": 0}

    def factory(snapshot: SnapshotArtifact) -> FakeSession:
        session = sessions[index["n"]]
        index["n"] += 1
        return session

    runner = HarnessRunner(
        gateway=gateway,
        session_factory=factory,
        run_dir=tmp_path / "run",
        snapshotter=_snapshot,
    )
    request = RunRequest(
        repo=Path("/tmp/demo"),
        commit="0" * 40,
        manifest=_manifest(),
        finding=_finding(),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run(request)

    state = json.loads((tmp_path / "run" / "run_state.json").read_text())
    assert state["phase"] == "awaiting_approval"


async def test_cancellation_marks_state_and_propagates(tmp_path: Path) -> None:
    class CancellingGateway:
        async def health(self) -> ModelHealth:
            return ModelHealth(ok=True)

        async def next_action(self, context: object) -> AgentAction:
            raise asyncio.CancelledError

    rig = Rig(CancellingGateway(), [_baseline_script()], tmp_path)

    with pytest.raises(asyncio.CancelledError):
        await rig.run()

    state = json.loads((rig.run_dir / "run_state.json").read_text())
    assert state["phase"] == "cancelled"
    assert rig.sessions[0].closed
