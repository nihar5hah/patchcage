"""Host-owned run loop: model ↔ policy ↔ MCP ↔ verification.

The runner drives the state machine from harness/state_machine.py. The model
proposes one action per turn through the gateway; the host owns every phase
transition, re-validates every patch with inspect_patch before forwarding it,
and derives the verdict from check outcomes — never from a model claim.

The model never sees the propose_patch tool: patches arrive only as envelope
PatchActions, so the host walks the FINDING_CONFIRMED → PATCH_ENABLED →
PATCH_VALIDATING waypoints itself and no tool call can skip verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field

from patchcage.domain import (
    CheckExpectation,
    CheckResult,
    CheckStatus,
    CommandSpec,
    CompletionAction,
    Finding,
    PatchAction,
    ProjectManifest,
    RunEvent,
    RunPhase,
    ToolAction,
)
from patchcage.domain.models import StrictModel
from patchcage.gateway.base import InvalidModelOutput, ModelGateway, ModelUnavailable
from patchcage.harness.budgets import BudgetExceeded, BudgetName, BudgetTracker
from patchcage.harness.context import (
    LAST_TOOL_RESULT_LIMIT,
    OUTCOME_LIMIT,
    TurnRecord,
    build_context,
    record_turn,
)
from patchcage.harness.loop_supervisor import LoopSupervisor
from patchcage.harness.state_machine import is_terminal, transition
from patchcage.mcp import MCPToolError
from patchcage.policy import PatchPolicyError, inspect_patch
from patchcage.sandbox.docker_runtime import SandboxError
from patchcage.snapshot import SnapshotArtifact, SnapshotError, create_snapshot

CHECK_ORDER = ("compile", "scanner", "security", "unit")
MODEL_HIDDEN_TOOLS = frozenset({"propose_patch"})


class WorkspaceSession(Protocol):
    """One live sandboxed workspace (the work session or a clean replay)."""

    @property
    def baseline_sha(self) -> str: ...

    async def __aenter__(self) -> WorkspaceSession: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...

    async def list_tools(self) -> list[str]: ...

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def run_host_check(self, name: str) -> CheckResult: ...


class Snapshotter(Protocol):
    def __call__(
        self,
        repository: Path,
        commit: str,
        *,
        blocked_patterns: tuple[str, ...],
    ) -> SnapshotArtifact: ...


SessionFactory = Callable[[SnapshotArtifact], WorkspaceSession]
EventSink = Callable[[RunEvent], None]


class RunRequest(StrictModel):
    repo: Path
    commit: str = Field(min_length=1, max_length=100)
    manifest: ProjectManifest
    finding: Finding
    instructions: str | None = Field(default=None, max_length=4_000)


class RunResult(StrictModel):
    run_id: str
    phase: RunPhase
    run_dir: str
    detail: str = Field(max_length=2_000)
    candidate_patch: str | None = None
    candidate_sha256: str | None = None
    check_results: tuple[CheckResult, ...] = ()


def _check_specs(manifest: ProjectManifest) -> dict[str, CommandSpec]:
    return {
        "compile": manifest.checks.compile_check,
        "scanner": manifest.checks.scanner,
        "security": manifest.checks.security,
        "unit": manifest.checks.unit,
    }


def _expectation_met(spec: CommandSpec, result: CheckResult, *, baseline: bool) -> bool:
    expectation = spec.baseline_expectation if baseline else spec.patched_expectation
    if expectation is CheckExpectation.PASSED:
        return result.status is CheckStatus.PASSED
    if expectation is CheckExpectation.FINDING_PRESENT:
        # The scanner runs with --error: exit 1 means findings, exit 0 means
        # clean, anything else is a scanner malfunction and satisfies neither.
        return result.status is CheckStatus.FAILED and result.exit_code == 1
    if expectation is CheckExpectation.FINDING_ABSENT:
        return result.status is CheckStatus.PASSED
    if expectation is CheckExpectation.VULNERABILITY_REPRODUCED:
        marker = spec.baseline_required_marker
        return marker is not None and marker in result.summary
    raise AssertionError(f"unhandled expectation: {expectation}")


def _summarize_tool_result(tool: str, payload: dict[str, Any]) -> str:
    rendered = json.dumps(payload, separators=(",", ":"))
    if len(rendered) > LAST_TOOL_RESULT_LIMIT:
        rendered = rendered[:LAST_TOOL_RESULT_LIMIT] + "…[truncated]"
    return f"{tool} -> {rendered}"


def _cap_outcome(text: str) -> str:
    if len(text) <= OUTCOME_LIMIT:
        return text
    return text[:OUTCOME_LIMIT] + "…[truncated]"


def _summarize_failures(results: list[CheckResult]) -> str:
    failed = [r for r in results if r.status is not CheckStatus.PASSED]
    lines = [f"{r.name}: {r.status.value} — {r.summary}" for r in failed]
    text = "verification failed; fix and re-patch:\n" + "\n".join(lines)
    if len(text) > LAST_TOOL_RESULT_LIMIT:
        text = text[:LAST_TOOL_RESULT_LIMIT] + "…[truncated]"
    return text


class HarnessRunner:
    """Single-use driver for one remediation run."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        session_factory: SessionFactory,
        run_dir: Path,
        snapshotter: Snapshotter = create_snapshot,
        on_event: EventSink | None = None,
    ) -> None:
        self._gateway = gateway
        self._session_factory = session_factory
        self._run_dir = run_dir
        self._snapshotter = snapshotter
        self._on_event = on_event
        self._phase = RunPhase.CREATED
        self._sequence = 0
        self._started = False
        self._concluded = False

    async def run(self, request: RunRequest) -> RunResult:
        if self._started:
            raise RuntimeError("HarnessRunner is single-use")
        self._started = True

        run_id = uuid4().hex
        limits = request.manifest.limits
        budgets = BudgetTracker(limits=limits)
        supervisor = LoopSupervisor(max_identical_failures=limits.identical_failed_actions)
        turns: list[TurnRecord] = []
        check_results: list[CheckResult] = []
        last_tool_result: str | None = None
        candidate_patch: str | None = None
        candidate_sha256: str | None = None

        try:
            health = await self._gateway.health()
            if not health.ok:
                return self._conclude(
                    run_id,
                    RunPhase.MODEL_UNAVAILABLE,
                    f"model unavailable: {health.detail}",
                    check_results,
                )
            self._advance(RunPhase.PREFLIGHT_VALIDATED)

            try:
                snapshot = self._snapshotter(
                    request.repo,
                    request.commit,
                    blocked_patterns=request.manifest.scope.blocked,
                )
            except SnapshotError as error:
                return self._conclude(
                    run_id,
                    RunPhase.SOURCE_REJECTED,
                    f"snapshot rejected: {error.code}: {error}",
                    check_results,
                )
            self._advance(RunPhase.SNAPSHOT_READY)

            async with self._session_factory(snapshot) as session:
                baseline_ok, baseline_results = self._run_ladder(
                    session, request.manifest, baseline=True, fail_fast=True
                )
                check_results.extend(baseline_results)
                if not baseline_ok:
                    last = baseline_results[-1]
                    if last.name in ("scanner", "security"):
                        return self._conclude(
                            run_id,
                            RunPhase.FINDING_NOT_REPRODUCIBLE,
                            f"baseline {last.name} check did not confirm the finding",
                            check_results,
                        )
                    return self._conclude(
                        run_id,
                        RunPhase.BASELINE_FAILED,
                        f"baseline {last.name} check failed: {last.summary}",
                        check_results,
                    )
                self._advance(RunPhase.BASELINE_VERIFIED)

                tools = [
                    name for name in await session.list_tools() if name not in MODEL_HIDDEN_TOOLS
                ]
                self._advance(RunPhase.INVESTIGATING)

                while True:
                    budgets.assert_within_wall_clock()
                    context = build_context(
                        finding=request.finding,
                        phase=self._phase,
                        budgets=budgets.snapshot(),
                        instructions=request.instructions,
                        last_tool_result=last_tool_result,
                        check_results=check_results,
                        candidate_patch_hash=candidate_sha256,
                        available_tools=tools,
                        recent_actions=turns,
                    )
                    try:
                        action = await self._gateway.next_action(context)
                    except ModelUnavailable as error:
                        return self._conclude(
                            run_id,
                            RunPhase.MODEL_UNAVAILABLE,
                            f"model lost mid-run: {error}",
                            check_results,
                        )
                    except InvalidModelOutput as error:
                        try:
                            budgets.consume(BudgetName.INVALID_OUTPUTS)
                        except BudgetExceeded:
                            return self._conclude(
                                run_id,
                                RunPhase.INVALID_MODEL_OUTPUT,
                                "model repeatedly produced invalid output",
                                check_results,
                            )
                        last_tool_result = f"previous output rejected by the host: {error}"
                        continue
                    budgets.consume(BudgetName.MODEL_TURNS)
                    self._emit(
                        "model_action",
                        {"type": action.type, "summary": action.summary},
                    )

                    repository_hash = f"{session.baseline_sha}:{candidate_sha256 or 'clean'}"
                    decision = supervisor.evaluate_action(
                        action,
                        phase=self._phase,
                        repository_hash=repository_hash,
                        patch_hash=candidate_sha256,
                    )
                    signature = decision.signature
                    assert signature is not None
                    if not decision.allowed:
                        last_tool_result = f"action blocked by loop supervisor: {decision.message}"
                        turns.append(record_turn(action, f"blocked: {decision.message}"))
                        stall = supervisor.record_action_result(
                            signature, succeeded=False, made_progress=False
                        )
                        if not stall.allowed:
                            return self._conclude(
                                run_id, RunPhase.BUDGET_EXHAUSTED, stall.message, check_results
                            )
                        continue

                    if isinstance(action, ToolAction):
                        succeeded, outcome, last_tool_result = await self._run_tool_action(
                            session, action, tools, budgets
                        )
                        turns.append(record_turn(action, outcome))
                        stall = supervisor.record_action_result(
                            signature, succeeded=succeeded, made_progress=succeeded
                        )
                        if not stall.allowed:
                            return self._conclude(
                                run_id, RunPhase.BUDGET_EXHAUSTED, stall.message, check_results
                            )
                        continue

                    if isinstance(action, PatchAction):
                        budgets.consume(BudgetName.PATCH_ATTEMPTS)
                        patch_outcome = await self._run_patch_action(
                            session, action, request, budgets, supervisor
                        )
                        if patch_outcome is not None:
                            # Rejected before landing: feed back and keep going.
                            outcome, last_tool_result = patch_outcome, patch_outcome
                            turns.append(record_turn(action, outcome))
                            supervisor.record_action_result(
                                signature, succeeded=False, made_progress=False
                            )
                            continue
                        candidate_patch = (await session.call("get_current_diff", {}))["diff"]
                        candidate_sha256 = hashlib.sha256(candidate_patch.encode()).hexdigest()
                        turns.append(record_turn(action, "patch applied by the host"))
                        supervisor.record_action_result(
                            signature, succeeded=True, made_progress=True
                        )

                        if self._phase is RunPhase.REPAIRING:
                            self._advance(RunPhase.PATCH_VALIDATING)
                        else:
                            self._advance(RunPhase.FINDING_CONFIRMED)
                            self._advance(RunPhase.PATCH_ENABLED)
                            self._advance(RunPhase.PATCH_VALIDATING)
                        self._advance(RunPhase.WORKING_VERIFICATION)

                        verified, ladder_results = self._run_ladder(
                            session, request.manifest, baseline=False, fail_fast=False
                        )
                        check_results.extend(ladder_results)
                        if verified:
                            replay_ok, replay_results = await self._clean_replay(
                                snapshot, candidate_patch, request.manifest
                            )
                            check_results.extend(replay_results)
                            if not replay_ok:
                                return self._conclude(
                                    run_id,
                                    RunPhase.CLEAN_REPLAY_FAILED,
                                    "clean replay failed in a fresh container",
                                    check_results,
                                )
                            self._advance(RunPhase.CLEAN_REPLAY_VERIFIED)
                            return self._conclude(
                                run_id,
                                RunPhase.AWAITING_APPROVAL,
                                "patch verified; awaiting approval",
                                check_results,
                                candidate_patch=candidate_patch,
                                candidate_sha256=candidate_sha256,
                            )
                        try:
                            budgets.consume(BudgetName.REPAIR_CYCLES)
                        except BudgetExceeded:
                            return self._conclude(
                                run_id,
                                RunPhase.VERIFICATION_FAILED,
                                "verification failed and the repair budget is exhausted",
                                check_results,
                            )
                        self._advance(RunPhase.REPAIRING)
                        last_tool_result = _summarize_failures(ladder_results)
                        continue

                    assert isinstance(action, CompletionAction)
                    outcome = "rejected: the host verifies and finishes runs; propose a patch"
                    turns.append(record_turn(action, outcome))
                    last_tool_result = outcome
                    stall = supervisor.record_action_result(
                        signature, succeeded=False, made_progress=False
                    )
                    if not stall.allowed:
                        return self._conclude(
                            run_id, RunPhase.BUDGET_EXHAUSTED, stall.message, check_results
                        )

        except asyncio.CancelledError:
            if not self._concluded and not is_terminal(self._phase):
                self._advance(RunPhase.CANCELLED)
                self._persist(
                    self._result(run_id, "cancelled", check_results, None, None)
                )
            raise
        except SandboxError as error:
            if self._concluded:
                raise
            return self._conclude(
                run_id,
                RunPhase.SANDBOX_ERROR,
                f"sandbox error: {error.code}: {error}",
                check_results,
            )
        except MCPToolError as error:
            if self._concluded:
                raise
            return self._conclude(
                run_id,
                RunPhase.SANDBOX_ERROR,
                f"workspace tool error: {error.code}: {error}",
                check_results,
            )
        except BudgetExceeded as error:
            if self._concluded:
                raise
            return self._conclude(
                run_id, RunPhase.BUDGET_EXHAUSTED, str(error), check_results
            )
        except Exception as error:
            if self._concluded:
                raise
            return self._conclude(
                run_id,
                RunPhase.SANDBOX_ERROR,
                f"sandbox error: {type(error).__name__}: {error}",
                check_results,
            )

    async def _run_tool_action(
        self,
        session: WorkspaceSession,
        action: ToolAction,
        tools: list[str],
        budgets: BudgetTracker,
    ) -> tuple[bool, str, str]:
        """Returns (succeeded, compact turn outcome, full result for the next turn)."""
        if action.tool not in tools:
            budgets.consume(BudgetName.POLICY_VIOLATIONS)
            message = f"denied: unknown or host-only tool {action.tool!r}"
            return False, message, message
        budgets.consume(BudgetName.TOOL_CALLS)
        try:
            payload = await session.call(action.tool, action.arguments)
        except MCPToolError as error:
            if error.code.startswith("DENY_"):
                budgets.consume(BudgetName.POLICY_VIOLATIONS)
            message = f"{error.code}: {error}"
            return False, _cap_outcome(message), message[:LAST_TOOL_RESULT_LIMIT]
        rendered = _summarize_tool_result(action.tool, payload)
        brief = f"{action.tool} -> ok ({len(rendered)} chars)"
        return True, brief, rendered

    async def _run_patch_action(
        self,
        session: WorkspaceSession,
        action: PatchAction,
        request: RunRequest,
        budgets: BudgetTracker,
        supervisor: LoopSupervisor,
    ) -> str | None:
        """Validate and apply a candidate patch. Returns None on success, else feedback."""
        try:
            metadata = inspect_patch(
                action.diff, scope=request.manifest.scope, limits=request.manifest.limits
            )
        except PatchPolicyError as error:
            budgets.consume(BudgetName.POLICY_VIOLATIONS)
            return _cap_outcome(f"patch rejected by host policy: {error.code}: {error}")
        oscillation = supervisor.evaluate_patch(metadata.sha256)
        if not oscillation.allowed:
            return _cap_outcome(f"patch blocked by loop supervisor: {oscillation.message}")
        try:
            await session.call("propose_patch", {"diff": action.diff})
        except MCPToolError as error:
            return _cap_outcome(f"patch application failed: {error.code}: {error}")
        # Track the submitted-diff hash: evaluate_patch compares against these, and
        # the git-regenerated get_current_diff text differs byte-wise from both.
        supervisor.record_patch(metadata.sha256)
        return None

    async def _clean_replay(
        self,
        snapshot: SnapshotArtifact,
        candidate_patch: str,
        manifest: ProjectManifest,
    ) -> tuple[bool, list[CheckResult]]:
        async with self._session_factory(snapshot) as replay:
            await replay.call("propose_patch", {"diff": candidate_patch})
            return self._run_ladder(replay, manifest, baseline=False, fail_fast=False)

    def _run_ladder(
        self,
        session: WorkspaceSession,
        manifest: ProjectManifest,
        *,
        baseline: bool,
        fail_fast: bool,
    ) -> tuple[bool, list[CheckResult]]:
        specs = _check_specs(manifest)
        results: list[CheckResult] = []
        ok = True
        for name in CHECK_ORDER:
            result = session.run_host_check(name)
            results.append(result)
            self._emit(
                "check_result",
                {"name": name, "status": result.status.value, "summary": result.summary},
            )
            if not _expectation_met(specs[name], result, baseline=baseline):
                ok = False
                if fail_fast:
                    break
        return ok, results

    def _advance(self, target: RunPhase) -> None:
        self._phase = transition(self._phase, target)
        self._emit("phase", {"phase": target.value})

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        event = RunEvent(
            sequence=self._sequence,
            event_type=event_type,
            phase=self._phase,
            payload=payload,
        )
        self._sequence += 1
        self._on_event(event)

    def _conclude(
        self,
        run_id: str,
        phase: RunPhase,
        detail: str,
        check_results: list[CheckResult],
        *,
        candidate_patch: str | None = None,
        candidate_sha256: str | None = None,
    ) -> RunResult:
        self._concluded = True
        self._advance(phase)
        self._emit("run_finished", {"detail": detail})
        result = self._result(
            run_id, detail, check_results, candidate_patch, candidate_sha256
        )
        self._persist(result)
        return result

    def _result(
        self,
        run_id: str,
        detail: str,
        check_results: list[CheckResult],
        candidate_patch: str | None,
        candidate_sha256: str | None,
    ) -> RunResult:
        return RunResult(
            run_id=run_id,
            phase=self._phase,
            run_dir=str(self._run_dir),
            detail=detail[:2_000],
            candidate_patch=candidate_patch,
            candidate_sha256=candidate_sha256,
            check_results=tuple(check_results),
        )

    def _persist(self, result: RunResult) -> None:
        self._run_dir.mkdir(parents=True, exist_ok=True)
        if result.candidate_patch is not None:
            (self._run_dir / "candidate.patch").write_text(result.candidate_patch)
        state = {
            "run_id": result.run_id,
            "phase": result.phase.value,
            "candidate_sha256": result.candidate_sha256,
            "detail": result.detail,
        }
        (self._run_dir / "run_state.json").write_text(json.dumps(state, indent=2) + "\n")
        evidence = {
            "run_id": result.run_id,
            "phase": result.phase.value,
            "checks": [check.model_dump(mode="json") for check in result.check_results],
        }
        (self._run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
