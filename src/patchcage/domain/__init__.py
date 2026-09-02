"""Framework-independent PatchCage domain models."""

from patchcage.domain.manifest import load_manifest
from patchcage.domain.models import (
    AGENT_ACTION_ADAPTER,
    AgentAction,
    CheckExpectation,
    CheckResult,
    CheckStatus,
    CommandSpec,
    CompletionAction,
    Finding,
    FindingSource,
    PatchAction,
    ProjectManifest,
    RunEvent,
    RunLimits,
    RunPhase,
    RuntimeSpec,
    ScopeSpec,
    ToolAction,
)

__all__ = [
    "AGENT_ACTION_ADAPTER",
    "AgentAction",
    "CheckExpectation",
    "CheckResult",
    "CheckStatus",
    "CommandSpec",
    "CompletionAction",
    "Finding",
    "FindingSource",
    "PatchAction",
    "ProjectManifest",
    "RuntimeSpec",
    "RunEvent",
    "RunLimits",
    "RunPhase",
    "ScopeSpec",
    "ToolAction",
    "load_manifest",
]
