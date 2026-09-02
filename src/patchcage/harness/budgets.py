from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic

from patchcage.domain import RunLimits


class BudgetName(StrEnum):
    MODEL_TURNS = "model_turns"
    TOOL_CALLS = "tool_calls"
    INVALID_OUTPUTS = "invalid_outputs"
    POLICY_VIOLATIONS = "policy_violations"
    PATCH_ATTEMPTS = "patch_attempts"
    REPAIR_CYCLES = "repair_cycles"


class BudgetExceeded(RuntimeError):
    def __init__(self, budget: BudgetName | str, limit: int | float) -> None:
        super().__init__(f"budget {str(budget)!r} exhausted at limit {limit}")
        self.budget = budget
        self.limit = limit


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    used: Mapping[BudgetName, int]
    remaining: Mapping[BudgetName, int]
    elapsed_seconds: float
    remaining_wall_clock_seconds: float


@dataclass(slots=True)
class BudgetTracker:
    limits: RunLimits
    clock: Callable[[], float] = field(default=monotonic, repr=False)
    _started_at: float = field(init=False, repr=False)
    _used: dict[BudgetName, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()
        self._used = {name: 0 for name in BudgetName}

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self._started_at)

    @property
    def remaining_wall_clock_seconds(self) -> float:
        return max(0.0, self.limits.run_wall_clock_seconds - self.elapsed_seconds)

    def assert_within_wall_clock(self) -> None:
        if self.elapsed_seconds >= self.limits.run_wall_clock_seconds:
            raise BudgetExceeded("run_wall_clock_seconds", self.limits.run_wall_clock_seconds)

    def limit_for(self, budget: BudgetName) -> int:
        return {
            BudgetName.MODEL_TURNS: self.limits.model_turns,
            BudgetName.TOOL_CALLS: self.limits.tool_calls,
            BudgetName.INVALID_OUTPUTS: self.limits.invalid_outputs,
            BudgetName.POLICY_VIOLATIONS: self.limits.policy_violations,
            BudgetName.PATCH_ATTEMPTS: self.limits.patch_attempts,
            BudgetName.REPAIR_CYCLES: self.limits.repair_cycles,
        }[budget]

    def used(self, budget: BudgetName) -> int:
        return self._used[budget]

    def remaining(self, budget: BudgetName) -> int:
        return self.limit_for(budget) - self.used(budget)

    def consume(self, budget: BudgetName, amount: int = 1) -> int:
        if amount <= 0:
            raise ValueError("budget consumption must be positive")
        self.assert_within_wall_clock()

        updated = self.used(budget) + amount
        limit = self.limit_for(budget)
        if updated > limit:
            raise BudgetExceeded(budget, limit)

        self._used[budget] = updated
        return limit - updated

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            used=dict(self._used),
            remaining={name: self.remaining(name) for name in BudgetName},
            elapsed_seconds=self.elapsed_seconds,
            remaining_wall_clock_seconds=self.remaining_wall_clock_seconds,
        )
