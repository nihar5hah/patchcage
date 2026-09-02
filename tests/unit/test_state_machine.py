import pytest

from patchcage.domain import RunPhase
from patchcage.harness.state_machine import (
    TERMINAL_PHASES,
    InvalidTransition,
    allowed_transitions,
    is_terminal,
    transition,
)


def test_happy_path_is_explicitly_connected() -> None:
    path = [
        RunPhase.CREATED,
        RunPhase.PREFLIGHT_VALIDATED,
        RunPhase.SNAPSHOT_READY,
        RunPhase.BASELINE_VERIFIED,
        RunPhase.INVESTIGATING,
        RunPhase.FINDING_CONFIRMED,
        RunPhase.PATCH_ENABLED,
        RunPhase.PATCH_VALIDATING,
        RunPhase.WORKING_VERIFICATION,
        RunPhase.CLEAN_REPLAY_VERIFIED,
        RunPhase.AWAITING_APPROVAL,
        RunPhase.EXPORTED,
    ]

    current = path[0]
    for target in path[1:]:
        current = transition(current, target)

    assert current is RunPhase.EXPORTED
    assert is_terminal(current)


def test_verification_can_enter_bounded_repair_path() -> None:
    assert transition(RunPhase.WORKING_VERIFICATION, RunPhase.REPAIRING) is RunPhase.REPAIRING
    assert transition(RunPhase.REPAIRING, RunPhase.PATCH_VALIDATING) is RunPhase.PATCH_VALIDATING


def test_sandbox_error_is_reachable_from_live_phases() -> None:
    for phase in (
        RunPhase.BASELINE_VERIFIED,
        RunPhase.FINDING_CONFIRMED,
        RunPhase.PATCH_ENABLED,
        RunPhase.REPAIRING,
    ):
        assert transition(phase, RunPhase.SANDBOX_ERROR) is RunPhase.SANDBOX_ERROR


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(InvalidTransition):
        transition(RunPhase.CREATED, RunPhase.EXPORTED)


def test_model_can_be_lost_mid_run() -> None:
    assert (
        transition(RunPhase.INVESTIGATING, RunPhase.MODEL_UNAVAILABLE)
        is RunPhase.MODEL_UNAVAILABLE
    )
    assert (
        transition(RunPhase.REPAIRING, RunPhase.MODEL_UNAVAILABLE)
        is RunPhase.MODEL_UNAVAILABLE
    )


def test_baseline_can_reject_irreproducible_finding() -> None:
    assert (
        transition(RunPhase.SNAPSHOT_READY, RunPhase.FINDING_NOT_REPRODUCIBLE)
        is RunPhase.FINDING_NOT_REPRODUCIBLE
    )


def test_clean_replay_failure_originates_from_working_verification() -> None:
    assert (
        transition(RunPhase.WORKING_VERIFICATION, RunPhase.CLEAN_REPLAY_FAILED)
        is RunPhase.CLEAN_REPLAY_FAILED
    )
    with pytest.raises(InvalidTransition):
        transition(RunPhase.CLEAN_REPLAY_VERIFIED, RunPhase.CLEAN_REPLAY_FAILED)


@pytest.mark.parametrize("phase", TERMINAL_PHASES)
def test_terminal_phases_have_no_outgoing_transitions(phase: RunPhase) -> None:
    assert is_terminal(phase)
    assert allowed_transitions(phase) == frozenset()
