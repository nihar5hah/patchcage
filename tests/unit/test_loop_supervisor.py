from patchcage.domain import (
    PatchAction,
    RunPhase,
    ToolAction,
)
from patchcage.harness.loop_supervisor import (
    LoopSupervisor,
    SupervisorCode,
    action_signature,
)


def read_action(arguments: dict[str, object] | None = None) -> ToolAction:
    return ToolAction(
        type="tool",
        tool="read_file",
        arguments=arguments or {"path": "src/app.py"},
        summary="Inspect source.",
    )


def test_third_equivalent_failed_action_is_blocked_before_execution() -> None:
    supervisor = LoopSupervisor(max_identical_failures=2)
    action = read_action()

    for _ in range(2):
        decision = supervisor.evaluate_action(
            action,
            phase=RunPhase.INVESTIGATING,
            repository_hash="repo-a",
            patch_hash=None,
        )
        assert decision.allowed
        assert decision.signature is not None
        supervisor.record_action_result(
            decision.signature,
            succeeded=False,
            made_progress=False,
        )

    blocked = supervisor.evaluate_action(
        action,
        phase=RunPhase.INVESTIGATING,
        repository_hash="repo-a",
        patch_hash=None,
    )

    assert not blocked.allowed
    assert blocked.code is SupervisorCode.REPEATED_FAILED_ACTION


def test_summary_rewording_does_not_bypass_repetition_detection() -> None:
    supervisor = LoopSupervisor(max_identical_failures=2)
    first_action = read_action()
    reworded_action = ToolAction(
        type="tool",
        tool="read_file",
        arguments={"path": "src/app.py"},
        summary="A differently worded explanation of the identical call.",
    )

    for action in (first_action, reworded_action):
        decision = supervisor.evaluate_action(
            action,
            phase=RunPhase.INVESTIGATING,
            repository_hash="repo-a",
            patch_hash=None,
        )
        assert decision.allowed
        assert decision.signature is not None
        supervisor.record_action_result(
            decision.signature,
            succeeded=False,
            made_progress=False,
        )

    blocked = supervisor.evaluate_action(
        first_action,
        phase=RunPhase.INVESTIGATING,
        repository_hash="repo-a",
        patch_hash=None,
    )

    assert not blocked.allowed
    assert blocked.code is SupervisorCode.REPEATED_FAILED_ACTION


def test_same_action_is_allowed_after_repository_state_changes() -> None:
    supervisor = LoopSupervisor(max_identical_failures=1)
    action = read_action()
    first = supervisor.evaluate_action(
        action,
        phase=RunPhase.INVESTIGATING,
        repository_hash="repo-a",
        patch_hash=None,
    )
    assert first.signature is not None
    supervisor.record_action_result(first.signature, succeeded=False, made_progress=False)

    changed = supervisor.evaluate_action(
        action,
        phase=RunPhase.INVESTIGATING,
        repository_hash="repo-b",
        patch_hash=None,
    )

    assert changed.allowed


def test_action_signature_is_independent_of_argument_key_order() -> None:
    left = read_action({"path": "src/app.py", "start_line": 1})
    right = read_action({"start_line": 1, "path": "src/app.py"})

    assert action_signature(
        left,
        phase=RunPhase.INVESTIGATING,
        repository_hash="repo",
        patch_hash=None,
    ) == action_signature(
        right,
        phase=RunPhase.INVESTIGATING,
        repository_hash="repo",
        patch_hash=None,
    )


def test_patch_a_b_a_oscillation_is_blocked() -> None:
    supervisor = LoopSupervisor()
    supervisor.record_patch("patch-a")
    supervisor.record_patch("patch-b")

    decision = supervisor.evaluate_patch("patch-a")

    assert not decision.allowed
    assert decision.code is SupervisorCode.PATCH_OSCILLATION


def test_stagnation_limit_stops_run() -> None:
    supervisor = LoopSupervisor(max_stagnant_turns=2)
    action = PatchAction(type="patch", diff="diff --git a/a b/a\n", summary="Try patch.")
    decision = supervisor.evaluate_action(
        action,
        phase=RunPhase.PATCH_ENABLED,
        repository_hash="repo",
        patch_hash=None,
    )
    assert decision.signature is not None

    first = supervisor.record_action_result(
        decision.signature,
        succeeded=True,
        made_progress=False,
    )
    second = supervisor.record_action_result(
        decision.signature,
        succeeded=True,
        made_progress=False,
    )

    assert first.allowed
    assert not second.allowed
    assert second.code is SupervisorCode.STATE_STAGNATION
