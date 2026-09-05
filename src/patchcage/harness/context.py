"""Bounded model-facing view of a run.

The harness builds one AgentContext per model turn. The model sees the
finding, the current phase, the latest tool/check outcomes, budget pressure,
and a short tail of what it already tried — never the raw transcript or the
full event log.
"""

from __future__ import annotations

import hashlib
import json
import typing
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import Field, field_validator

from patchcage.domain import (
    AgentAction,
    CheckResult,
    CompletionAction,
    Finding,
    PatchAction,
    RunPhase,
    ToolAction,
)
from patchcage.domain.models import StrictModel
from patchcage.harness.budgets import BudgetSnapshot

RECENT_ACTIONS_LIMIT = 8
CHECK_RESULTS_LIMIT = 8
AVAILABLE_TOOLS_LIMIT = 32
OUTCOME_LIMIT = 1_000
INSTRUCTIONS_LIMIT = 4_000
LAST_TOOL_RESULT_LIMIT = 8_000
PATCH_HASH_LENGTH = 64


class TurnRecord(StrictModel):
    """One model turn, compacted. Patch diffs are hashed, never embedded."""

    kind: Literal["tool", "patch", "complete"]
    summary: str = Field(max_length=500)
    outcome: str = Field(min_length=1, max_length=OUTCOME_LIMIT)
    tool: str | None = Field(default=None, max_length=64)
    patch_hash: str | None = Field(default=None, min_length=PATCH_HASH_LENGTH)


def record_turn(action: AgentAction, outcome: str) -> TurnRecord:
    """Compact a proposed action plus its host outcome into a TurnRecord."""
    if isinstance(action, ToolAction):
        return TurnRecord(kind="tool", summary=action.summary, outcome=outcome, tool=action.tool)
    if isinstance(action, PatchAction):
        digest = hashlib.sha256(action.diff.encode()).hexdigest()
        return TurnRecord(kind="patch", summary=action.summary, outcome=outcome, patch_hash=digest)
    if isinstance(action, CompletionAction):
        return TurnRecord(kind="complete", summary=action.summary, outcome=outcome)
    typing.assert_never(action)


class AgentContext(StrictModel):
    """Everything the model may know on one turn. Bounded by construction."""

    finding: Finding
    phase: RunPhase
    budgets: BudgetSnapshot
    instructions: str | None = Field(default=None, max_length=INSTRUCTIONS_LIMIT)
    last_tool_result: str | None = Field(default=None, max_length=LAST_TOOL_RESULT_LIMIT)
    check_results: tuple[CheckResult, ...] = Field(default=(), max_length=CHECK_RESULTS_LIMIT)
    candidate_patch_hash: str | None = Field(default=None, min_length=PATCH_HASH_LENGTH)
    available_tools: tuple[str, ...] = Field(default=(), max_length=AVAILABLE_TOOLS_LIMIT)
    tool_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=32)
    recent_actions: tuple[TurnRecord, ...] = Field(default=(), max_length=RECENT_ACTIONS_LIMIT)

    @field_validator("tool_schemas")
    @classmethod
    def bounded_schemas(cls, schemas: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if len(json.dumps(schemas).encode()) > 24_000:
            raise ValueError("tool schemas exceed 24000 bytes")
        return schemas


def build_context(
    *,
    finding: Finding,
    phase: RunPhase,
    budgets: BudgetSnapshot,
    instructions: str | None = None,
    last_tool_result: str | None = None,
    check_results: Iterable[CheckResult] = (),
    candidate_patch_hash: str | None = None,
    available_tools: Iterable[str] = (),
    tool_schemas: dict[str, dict[str, Any]] | None = None,
    recent_actions: Iterable[TurnRecord] = (),
) -> AgentContext:
    """Assemble the context, trimming history to the most recent entries."""
    return AgentContext(
        finding=finding,
        phase=phase,
        budgets=budgets,
        instructions=instructions,
        last_tool_result=last_tool_result,
        check_results=tuple(check_results)[-CHECK_RESULTS_LIMIT:],
        candidate_patch_hash=candidate_patch_hash,
        available_tools=tuple(available_tools),
        tool_schemas=tool_schemas or {},
        recent_actions=tuple(recent_actions)[-RECENT_ACTIONS_LIMIT:],
    )
