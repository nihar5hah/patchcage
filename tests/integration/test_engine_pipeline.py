"""Scripted end-to-end engine pipeline: real sandbox, real checks, no live model.

This is the Phase 3 gate in the implementation plan: the model is a
ScriptedGateway, everything else — snapshot, Docker sandbox, MCP tools, host
verification ladder, clean replay, approval-gated persistence — is real.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from patchcage.domain import (
    Finding,
    FindingSource,
    PatchAction,
    RunPhase,
    ToolAction,
    load_manifest,
)
from patchcage.gateway import ScriptedGateway
from patchcage.harness.docker_session import docker_session_factory
from patchcage.harness.runner import HarnessRunner, RunRequest
from patchcage.sandbox.docker_runtime import DockerRuntime
from patchcage.sandbox.image import IMAGE_TAG, build_runtime_image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CREATE_DEMO = PROJECT_ROOT / "scripts" / "create_demo_repo.py"
FIX_PATCH = PROJECT_ROOT / "tests" / "fixtures" / "sql_injection_fix.patch"
RULE_ID = "patchcage.python.sql-injection.formatted-query"
MANIFEST_PATH = PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"


def _docker_ready() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_ready(), reason="Docker daemon is not running"),
]


def demo_finding() -> Finding:
    return Finding(
        id="sql-1",
        source=FindingSource.SEMGREP_SARIF,
        rule_id=RULE_ID,
        title="SQL injection via formatted query",
        description="User input is interpolated into a SQL execute call.",
        severity="ERROR",
        file_path="src/demo_app/search.py",
        start_line=20,
        verification_recipe="sql_injection_oracle",
    )


@pytest.fixture(scope="session")
def runtime_image_id() -> str:
    return build_runtime_image()


@pytest.mark.usefixtures("runtime_image_id")
async def test_scripted_run_reaches_awaiting_approval(tmp_path: Path) -> None:
    repository = tmp_path / "sql-demo"
    created = subprocess.run(
        [sys.executable, str(CREATE_DEMO), str(repository)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    commit_sha = json.loads(created.stdout)["commit_sha"]
    manifest = load_manifest(MANIFEST_PATH)
    finding = demo_finding()

    gateway = ScriptedGateway(
        [
            ToolAction(
                type="tool",
                tool="read_file",
                arguments={"path": "src/demo_app/search.py"},
                summary="Inspect the vulnerable query.",
            ),
            PatchAction(
                type="patch",
                diff=FIX_PATCH.read_text(),
                summary="Parameterize the query.",
            ),
        ]
    )
    events: list = []
    run_dir = tmp_path / "run"
    runner = HarnessRunner(
        gateway=gateway,
        session_factory=docker_session_factory(
            runtime=DockerRuntime(),
            image=IMAGE_TAG,
            manifest=manifest,
            finding=finding,
        ),
        run_dir=run_dir,
        on_event=events.append,
    )

    result = await runner.run(
        RunRequest(
            repo=repository,
            commit=commit_sha,
            manifest=manifest,
            finding=finding,
        )
    )

    assert result.phase is RunPhase.AWAITING_APPROVAL, result.detail
    assert result.candidate_patch is not None
    assert "search_products" in result.candidate_patch

    state = json.loads((run_dir / "run_state.json").read_text())
    assert state["phase"] == "awaiting_approval"
    on_disk = (run_dir / "candidate.patch").read_text()
    assert hashlib.sha256(on_disk.encode()).hexdigest() == state["candidate_sha256"]

    evidence = json.loads((run_dir / "evidence.json").read_text())
    checked = {(c["name"], c["status"]) for c in evidence["checks"]}
    assert ("security", "passed") in checked  # oracle passed post-patch, host-side only

    phases = [e.payload["phase"] for e in events if e.event_type == "phase"]
    assert phases[:4] == [
        "preflight_validated",
        "snapshot_ready",
        "baseline_verified",
        "investigating",
    ]
    assert phases[-2:] == ["clean_replay_verified", "awaiting_approval"]

    # The model saw a bounded context and never the patch tool.
    seen = gateway.seen_contexts
    assert seen[0].phase is RunPhase.INVESTIGATING
    assert "propose_patch" not in seen[0].available_tools
    # The original repository was never touched.
    assert "LIKE '%{query}%'" in (repository / "src" / "demo_app" / "search.py").read_text()
