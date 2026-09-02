from pathlib import Path

import pytest
from pydantic import ValidationError

from patchcage.domain import (
    AGENT_ACTION_ADAPTER,
    CheckExpectation,
    CompletionAction,
    Finding,
    FindingSource,
    PatchAction,
    ProjectManifest,
    ToolAction,
    load_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_agent_action_adapter_discriminates_all_action_types() -> None:
    tool = AGENT_ACTION_ADAPTER.validate_python(
        {
            "type": "tool",
            "tool": "read_file",
            "arguments": {"path": "src/app.py"},
            "summary": "Inspect the vulnerable query.",
        }
    )
    patch = AGENT_ACTION_ADAPTER.validate_python(
        {
            "type": "patch",
            "diff": "diff --git a/src/app.py b/src/app.py\n",
            "summary": "Parameterize the query.",
        }
    )
    complete = AGENT_ACTION_ADAPTER.validate_python(
        {
            "type": "complete",
            "summary": "Request host verification.",
            "evidence_ids": ["check-unit"],
        }
    )

    assert isinstance(tool, ToolAction)
    assert isinstance(patch, PatchAction)
    assert isinstance(complete, CompletionAction)


def test_agent_action_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AGENT_ACTION_ADAPTER.validate_python(
            {
                "type": "tool",
                "tool": "read_file",
                "arguments": {"path": "src/app.py"},
                "summary": "Inspect source.",
                "command": "cat /etc/passwd",
            }
        )


def test_finding_rejects_inverted_line_range() -> None:
    with pytest.raises(ValidationError, match="end_line must not be before start_line"):
        Finding(
            id="finding-1",
            source=FindingSource.MANUAL,
            title="SQL injection",
            description="User input reaches SQL.",
            severity="high",
            file_path="src/app.py",
            start_line=20,
            end_line=10,
            verification_recipe="sql_injection_oracle",
        )


def test_manifest_parses_alias_and_detects_unpinned_image() -> None:
    manifest = ProjectManifest.model_validate(
        {
            "version": 1,
            "project": {"name": "demo", "language": "python"},
            "runtime": {"image": "patchcage/python-demo:dev"},
            "scope": {
                "readable": ["src/**", "tests/**"],
                "writable": ["src/**"],
                "blocked": [".git/**"],
            },
            "checks": {
                "compile": {
                    "argv": ["python", "-m", "compileall", "-q", "src"],
                    "timeout_seconds": 30,
                },
                "scanner": {
                    "argv": ["semgrep", "scan", "--metrics", "off"],
                    "timeout_seconds": 60,
                },
                "unit": {
                    "argv": ["pytest", "-q", "tests/unit"],
                    "timeout_seconds": 60,
                },
                "security": {
                    "argv": ["python", "/opt/patchcage/oracle.py"],
                    "timeout_seconds": 60,
                },
            },
        }
    )

    assert manifest.checks.compile_check.argv[0] == "python"
    assert manifest.limits.run_wall_clock_seconds == 2_400


def test_checked_in_demo_manifest_encodes_stage_expectations() -> None:
    manifest = load_manifest(PROJECT_ROOT / "manifests" / "flask_sql_injection.yml")

    assert manifest.runtime.image_is_unresolved is False
    assert manifest.checks.scanner.baseline_expectation is CheckExpectation.FINDING_PRESENT
    assert manifest.checks.scanner.patched_expectation is CheckExpectation.FINDING_ABSENT
    assert (
        manifest.checks.security.baseline_expectation is CheckExpectation.VULNERABILITY_REPRODUCED
    )
    assert manifest.checks.security.baseline_required_marker == "PATCHCAGE_VULNERABILITY_REPRODUCED"
