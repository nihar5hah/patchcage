from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from patchcage.domain import AgentAction, PatchAction, RunPhase


class SupervisorCode(StrEnum):
    ALLOW = "allow"
    REPEATED_FAILED_ACTION = "repeated_failed_action"
    PATCH_OSCILLATION = "patch_oscillation"
    STATE_STAGNATION = "state_stagnation"


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    allowed: bool
    code: SupervisorCode
    message: str
    signature: str | None = None


def action_signature(
    action: AgentAction,
    *,
    phase: RunPhase,
    repository_hash: str,
    patch_hash: str | None,
) -> str:
    action_data = action.model_dump(mode="json")
    # The summary is model-controlled prose; excluding it prevents the model from
    # evading repetition detection by rewording the explanation of an identical action.
    action_data.pop("summary", None)
    if isinstance(action, PatchAction):
        action_data["diff"] = hashlib.sha256(action.diff.encode()).hexdigest()

    normalized = {
        "action": action_data,
        "phase": phase.value,
        "repository_hash": repository_hash,
        "patch_hash": patch_hash,
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True)
class LoopSupervisor:
    max_identical_failures: int = 2
    max_stagnant_turns: int = 3
    _failed_actions: Counter[str] = field(default_factory=Counter, init=False, repr=False)
    _patch_history: list[str] = field(default_factory=list, init=False, repr=False)
    _stagnant_turns: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_identical_failures < 1:
            raise ValueError("max_identical_failures must be positive")
        if self.max_stagnant_turns < 1:
            raise ValueError("max_stagnant_turns must be positive")

    def evaluate_action(
        self,
        action: AgentAction,
        *,
        phase: RunPhase,
        repository_hash: str,
        patch_hash: str | None,
    ) -> SupervisorDecision:
        signature = action_signature(
            action,
            phase=phase,
            repository_hash=repository_hash,
            patch_hash=patch_hash,
        )
        if self._failed_actions[signature] >= self.max_identical_failures:
            return SupervisorDecision(
                allowed=False,
                code=SupervisorCode.REPEATED_FAILED_ACTION,
                message=(
                    f"Equivalent action already failed {self.max_identical_failures} "
                    "times against unchanged state."
                ),
                signature=signature,
            )
        return SupervisorDecision(
            allowed=True,
            code=SupervisorCode.ALLOW,
            message="Action is not blocked by loop supervision.",
            signature=signature,
        )

    def record_action_result(
        self,
        signature: str,
        *,
        succeeded: bool,
        made_progress: bool,
    ) -> SupervisorDecision:
        if not succeeded:
            self._failed_actions[signature] += 1

        self._stagnant_turns = 0 if made_progress else self._stagnant_turns + 1
        if self._stagnant_turns >= self.max_stagnant_turns:
            return SupervisorDecision(
                allowed=False,
                code=SupervisorCode.STATE_STAGNATION,
                message="Run produced no new evidence or state change for too many turns.",
                signature=signature,
            )
        return SupervisorDecision(
            allowed=True,
            code=SupervisorCode.ALLOW,
            message="Run may continue.",
            signature=signature,
        )

    def evaluate_patch(self, patch_hash: str) -> SupervisorDecision:
        if (
            len(self._patch_history) >= 2
            and patch_hash == self._patch_history[-2]
            and patch_hash != self._patch_history[-1]
        ):
            return SupervisorDecision(
                allowed=False,
                code=SupervisorCode.PATCH_OSCILLATION,
                message="Candidate patch would repeat an A-B-A oscillation.",
            )
        return SupervisorDecision(
            allowed=True,
            code=SupervisorCode.ALLOW,
            message="Candidate patch does not oscillate.",
        )

    def record_patch(self, patch_hash: str) -> None:
        self._patch_history.append(patch_hash)
