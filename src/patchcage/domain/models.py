from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FindingSource(StrEnum):
    SEMGREP_SARIF = "semgrep_sarif"
    MANUAL = "manual"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class CheckExpectation(StrEnum):
    PASSED = "passed"
    FINDING_PRESENT = "finding_present"
    FINDING_ABSENT = "finding_absent"
    VULNERABILITY_REPRODUCED = "vulnerability_reproduced"


class RunPhase(StrEnum):
    CREATED = "created"
    PREFLIGHT_VALIDATED = "preflight_validated"
    SNAPSHOT_READY = "snapshot_ready"
    BASELINE_VERIFIED = "baseline_verified"
    INVESTIGATING = "investigating"
    FINDING_CONFIRMED = "finding_confirmed"
    PATCH_ENABLED = "patch_enabled"
    PATCH_VALIDATING = "patch_validating"
    WORKING_VERIFICATION = "working_verification"
    REPAIRING = "repairing"
    CLEAN_REPLAY_VERIFIED = "clean_replay_verified"
    AWAITING_APPROVAL = "awaiting_approval"
    EXPORTED = "exported"

    MODEL_UNAVAILABLE = "model_unavailable"
    SOURCE_REJECTED = "source_rejected"
    BASELINE_FAILED = "baseline_failed"
    FINDING_NOT_REPRODUCIBLE = "finding_not_reproducible"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    PATCH_APPLICATION_FAILED = "patch_application_failed"
    VERIFICATION_FAILED = "verification_failed"
    CLEAN_REPLAY_FAILED = "clean_replay_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SANDBOX_ERROR = "sandbox_error"
    CANCELLED = "cancelled"
    USER_REJECTED = "user_rejected"


class Finding(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    source: FindingSource
    rule_id: str | None = Field(default=None, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=10_000)
    severity: str = Field(min_length=1, max_length=50)
    cwe_ids: tuple[str, ...] = ()
    file_path: str = Field(min_length=1, max_length=1_000)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    fingerprint: str | None = Field(default=None, max_length=1_000)
    verification_recipe: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_line_range(self) -> Finding:
        if self.end_line is not None and self.start_line is None:
            raise ValueError("end_line requires start_line")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must not be before start_line")
        return self


class ToolAction(StrictModel):
    type: Literal["tool"]
    tool: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: dict[str, Any]
    summary: str = Field(min_length=1, max_length=500)


class PatchAction(StrictModel):
    type: Literal["patch"]
    diff: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)


class CompletionAction(StrictModel):
    type: Literal["complete"]
    summary: str = Field(min_length=1, max_length=1_000)
    evidence_ids: tuple[str, ...] = ()


AgentAction = Annotated[
    ToolAction | PatchAction | CompletionAction,
    Field(discriminator="type"),
]
AGENT_ACTION_ADAPTER: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)


class CheckResult(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    status: CheckStatus
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=2_000)
    artifact_id: str | None = None


class CommandSpec(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(gt=0, le=3_600)
    env: dict[str, str] = Field(default_factory=dict)
    baseline_expectation: CheckExpectation = CheckExpectation.PASSED
    patched_expectation: CheckExpectation = CheckExpectation.PASSED
    baseline_required_marker: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, argv: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or "\x00" in value for value in argv):
            raise ValueError("argv entries must be non-empty and NUL-free")
        return argv

    @model_validator(mode="after")
    def validate_baseline_marker(self) -> CommandSpec:
        if (
            self.baseline_expectation is CheckExpectation.VULNERABILITY_REPRODUCED
            and self.baseline_required_marker is None
        ):
            raise ValueError("vulnerability_reproduced requires baseline_required_marker")
        return self


class ProjectSpec(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    language: Literal["python"]


class RuntimeSpec(StrictModel):
    image: str = Field(min_length=1, max_length=500)
    workdir: Literal["/workspace"] = "/workspace"

    @property
    def image_is_unresolved(self) -> bool:
        return self.image.endswith(":unresolved")


class ScopeSpec(StrictModel):
    readable: tuple[str, ...] = Field(min_length=1)
    writable: tuple[str, ...] = Field(min_length=1)
    blocked: tuple[str, ...] = ()


class CheckSet(StrictModel):
    compile_check: CommandSpec = Field(alias="compile")
    scanner: CommandSpec
    unit: CommandSpec
    security: CommandSpec


class RunLimits(StrictModel):
    model_turns: int = Field(default=12, gt=0)
    tool_calls: int = Field(default=25, gt=0)
    invalid_outputs: int = Field(default=3, gt=0)
    policy_violations: int = Field(default=5, gt=0)
    patch_attempts: int = Field(default=3, gt=0)
    repair_cycles: int = Field(default=2, gt=0)
    identical_failed_actions: int = Field(default=2, gt=0)
    run_wall_clock_seconds: int = Field(default=2_400, gt=0)
    patch_bytes: int = Field(default=65_536, gt=0)
    patch_files: int = Field(default=5, gt=0)
    added_lines: int = Field(default=200, ge=0)
    deleted_lines: int = Field(default=100, ge=0)


class ProjectManifest(StrictModel):
    version: Literal[1]
    project: ProjectSpec
    runtime: RuntimeSpec
    scope: ScopeSpec
    checks: CheckSet
    limits: RunLimits = Field(default_factory=RunLimits)


class RunEvent(StrictModel):
    sequence: int = Field(ge=0)
    event_type: str = Field(min_length=1, max_length=100)
    phase: RunPhase
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
