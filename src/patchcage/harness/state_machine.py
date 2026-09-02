from __future__ import annotations

from patchcage.domain import RunPhase


class InvalidTransition(ValueError):
    def __init__(self, current: RunPhase, target: RunPhase) -> None:
        super().__init__(f"cannot transition from {current.value!r} to {target.value!r}")
        self.current = current
        self.target = target


TERMINAL_PHASES = frozenset(
    {
        RunPhase.EXPORTED,
        RunPhase.MODEL_UNAVAILABLE,
        RunPhase.SOURCE_REJECTED,
        RunPhase.BASELINE_FAILED,
        RunPhase.FINDING_NOT_REPRODUCIBLE,
        RunPhase.INVALID_MODEL_OUTPUT,
        RunPhase.PATCH_APPLICATION_FAILED,
        RunPhase.VERIFICATION_FAILED,
        RunPhase.CLEAN_REPLAY_FAILED,
        RunPhase.BUDGET_EXHAUSTED,
        RunPhase.SANDBOX_ERROR,
        RunPhase.CANCELLED,
        RunPhase.USER_REJECTED,
    }
)

_ALLOWED_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.CREATED: frozenset(
        {
            RunPhase.PREFLIGHT_VALIDATED,
            RunPhase.MODEL_UNAVAILABLE,
            RunPhase.SOURCE_REJECTED,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.PREFLIGHT_VALIDATED: frozenset(
        {
            RunPhase.SNAPSHOT_READY,
            RunPhase.SOURCE_REJECTED,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.SNAPSHOT_READY: frozenset(
        {
            RunPhase.BASELINE_VERIFIED,
            RunPhase.BASELINE_FAILED,
            RunPhase.FINDING_NOT_REPRODUCIBLE,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.BASELINE_VERIFIED: frozenset(
        {
            RunPhase.INVESTIGATING,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.INVESTIGATING: frozenset(
        {
            RunPhase.FINDING_CONFIRMED,
            RunPhase.FINDING_NOT_REPRODUCIBLE,
            RunPhase.INVALID_MODEL_OUTPUT,
            RunPhase.MODEL_UNAVAILABLE,
            RunPhase.BUDGET_EXHAUSTED,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.FINDING_CONFIRMED: frozenset(
        {
            RunPhase.PATCH_ENABLED,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.PATCH_ENABLED: frozenset(
        {
            RunPhase.PATCH_VALIDATING,
            RunPhase.INVALID_MODEL_OUTPUT,
            RunPhase.BUDGET_EXHAUSTED,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.PATCH_VALIDATING: frozenset(
        {
            RunPhase.WORKING_VERIFICATION,
            RunPhase.PATCH_APPLICATION_FAILED,
            RunPhase.BUDGET_EXHAUSTED,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.WORKING_VERIFICATION: frozenset(
        {
            RunPhase.CLEAN_REPLAY_VERIFIED,
            RunPhase.REPAIRING,
            RunPhase.VERIFICATION_FAILED,
            RunPhase.CLEAN_REPLAY_FAILED,
            RunPhase.BUDGET_EXHAUSTED,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.REPAIRING: frozenset(
        {
            RunPhase.PATCH_VALIDATING,
            RunPhase.INVALID_MODEL_OUTPUT,
            RunPhase.MODEL_UNAVAILABLE,
            RunPhase.VERIFICATION_FAILED,
            RunPhase.BUDGET_EXHAUSTED,
            RunPhase.SANDBOX_ERROR,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.CLEAN_REPLAY_VERIFIED: frozenset(
        {
            RunPhase.AWAITING_APPROVAL,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.AWAITING_APPROVAL: frozenset(
        {
            RunPhase.EXPORTED,
            RunPhase.USER_REJECTED,
            RunPhase.CANCELLED,
        }
    ),
}


def allowed_transitions(phase: RunPhase) -> frozenset[RunPhase]:
    return _ALLOWED_TRANSITIONS.get(phase, frozenset())


def can_transition(current: RunPhase, target: RunPhase) -> bool:
    return target in allowed_transitions(current)


def transition(current: RunPhase, target: RunPhase) -> RunPhase:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
    return target


def is_terminal(phase: RunPhase) -> bool:
    return phase in TERMINAL_PHASES
