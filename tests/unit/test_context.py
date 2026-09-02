import hashlib
import json

import pytest
from pydantic import ValidationError

from patchcage.domain import (
    CheckResult,
    CheckStatus,
    Finding,
    FindingSource,
    PatchAction,
    RunLimits,
    RunPhase,
    ToolAction,
)
from patchcage.harness.budgets import BudgetSnapshot, BudgetTracker
from patchcage.harness.context import (
    CHECK_RESULTS_LIMIT,
    RECENT_ACTIONS_LIMIT,
    AgentContext,
    build_context,
    record_turn,
)


def _finding() -> Finding:
    return Finding(
        id="finding-1",
        source=FindingSource.MANUAL,
        title="SQL injection",
        description="User input reaches SQL.",
        severity="high",
        file_path="src/app.py",
        verification_recipe="sql_injection_oracle",
    )


def _budgets() -> BudgetSnapshot:
    return BudgetTracker(limits=RunLimits()).snapshot()


def _tool_action(summary: str = "Inspect source.") -> ToolAction:
    return ToolAction(
        type="tool",
        tool="read_file",
        arguments={"path": "src/app.py"},
        summary=summary,
    )


def _check_result(name: str) -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.PASSED, duration_ms=1, summary="ok")


def test_build_context_trims_recent_actions_to_limit() -> None:
    records = [
        record_turn(_tool_action(f"turn {i}"), "ok") for i in range(RECENT_ACTIONS_LIMIT + 4)
    ]
    context = build_context(
        finding=_finding(),
        phase=RunPhase.INVESTIGATING,
        budgets=_budgets(),
        recent_actions=records,
    )

    assert len(context.recent_actions) == RECENT_ACTIONS_LIMIT
    assert context.recent_actions[0].summary == "turn 4"
    assert context.recent_actions[-1].summary == f"turn {RECENT_ACTIONS_LIMIT + 3}"


def test_build_context_trims_check_results_to_limit() -> None:
    results = [_check_result(f"check-{i}") for i in range(CHECK_RESULTS_LIMIT + 2)]
    context = build_context(
        finding=_finding(),
        phase=RunPhase.REPAIRING,
        budgets=_budgets(),
        check_results=results,
    )

    assert len(context.check_results) == CHECK_RESULTS_LIMIT
    assert context.check_results[0].name == "check-2"


def test_context_rejects_oversized_tail_on_direct_construction() -> None:
    records = [record_turn(_tool_action(), "ok") for _ in range(RECENT_ACTIONS_LIMIT + 1)]
    with pytest.raises(ValidationError):
        AgentContext(
            finding=_finding(),
            phase=RunPhase.INVESTIGATING,
            budgets=_budgets(),
            recent_actions=records,
        )


def test_record_turn_hashes_patch_instead_of_embedding_diff() -> None:
    patch = PatchAction(
        type="patch",
        diff="diff --git a/src/app.py b/src/app.py\n+safe_query()\n",
        summary="Parameterize the query.",
    )
    record = record_turn(patch, "allowed; forwarded to workspace")

    assert record.patch_hash == hashlib.sha256(patch.diff.encode()).hexdigest()
    assert "+safe_query()" not in json.dumps(record.model_dump(mode="json"))


def test_record_turn_rejects_empty_outcome() -> None:
    with pytest.raises(ValidationError):
        record_turn(_tool_action(), "")


def test_context_serializes_to_json_for_the_gateway() -> None:
    context = build_context(
        finding=_finding(),
        phase=RunPhase.REPAIRING,
        budgets=_budgets(),
        instructions="Fix only the login query.",
        last_tool_result="read_file(src/app.py) -> 412 bytes",
        check_results=[_check_result("security")],
        candidate_patch_hash=hashlib.sha256(b"diff").hexdigest(),
        available_tools=("read_file", "search_code"),
        recent_actions=[record_turn(_tool_action(), "ok")],
    )

    payload = json.dumps(context.model_dump(mode="json"))

    assert "finding-1" in payload
    assert "repairing" in payload
    assert "model_turns" in payload  # budget snapshot survives serialization
    assert "read_file" in payload
    assert context.available_tools == ("read_file", "search_code")
