from contextlib import suppress

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patchcage.domain import RunLimits
from patchcage.harness.budgets import BudgetExceeded, BudgetName, BudgetTracker


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_counter_budget_allows_limit_and_rejects_next_use() -> None:
    tracker = BudgetTracker(RunLimits(model_turns=2))

    assert tracker.consume(BudgetName.MODEL_TURNS) == 1
    assert tracker.consume(BudgetName.MODEL_TURNS) == 0

    with pytest.raises(BudgetExceeded):
        tracker.consume(BudgetName.MODEL_TURNS)

    assert tracker.used(BudgetName.MODEL_TURNS) == 2


def test_failed_consumption_does_not_mutate_usage() -> None:
    tracker = BudgetTracker(RunLimits(tool_calls=1))
    tracker.consume(BudgetName.TOOL_CALLS)

    with pytest.raises(BudgetExceeded):
        tracker.consume(BudgetName.TOOL_CALLS, amount=2)

    assert tracker.used(BudgetName.TOOL_CALLS) == 1


def test_wall_clock_budget_uses_injected_monotonic_clock() -> None:
    clock = FakeClock()
    tracker = BudgetTracker(RunLimits(run_wall_clock_seconds=10), clock=clock)
    clock.now = 9.5

    assert tracker.remaining_wall_clock_seconds == 0.5

    clock.now = 10.0
    with pytest.raises(BudgetExceeded, match="run_wall_clock_seconds"):
        tracker.assert_within_wall_clock()


def test_snapshot_contains_all_counter_and_wall_clock_remaining_values() -> None:
    clock = FakeClock()
    tracker = BudgetTracker(RunLimits(model_turns=3, run_wall_clock_seconds=20), clock=clock)
    tracker.consume(BudgetName.MODEL_TURNS)
    clock.now = 5.0

    snapshot = tracker.snapshot()

    assert snapshot.used[BudgetName.MODEL_TURNS] == 1
    assert snapshot.remaining[BudgetName.MODEL_TURNS] == 2
    assert snapshot.elapsed_seconds == 5.0
    assert snapshot.remaining_wall_clock_seconds == 15.0


@given(
    limit=st.integers(min_value=1, max_value=100),
    attempts=st.integers(min_value=0, max_value=200),
)
def test_budget_usage_never_exceeds_limit(limit: int, attempts: int) -> None:
    tracker = BudgetTracker(RunLimits(tool_calls=limit))

    for _ in range(attempts):
        with suppress(BudgetExceeded):
            tracker.consume(BudgetName.TOOL_CALLS)

    assert tracker.used(BudgetName.TOOL_CALLS) == min(limit, attempts)
