"""DockerWorkspaceSession leak path — no Docker daemon required."""

from __future__ import annotations

import pytest

from patchcage.domain import Finding, FindingSource, ProjectManifest
from patchcage.harness.docker_session import DockerWorkspaceSession
from patchcage.sandbox.docker_runtime import Sandbox
from patchcage.snapshot import SnapshotArtifact


def _manifest() -> ProjectManifest:
    return ProjectManifest.model_validate(
        {
            "version": 1,
            "project": {"name": "demo", "language": "python"},
            "runtime": {"image": "patchcage/python-demo:dev"},
            "scope": {
                "readable": ["src/**"],
                "writable": ["src/**"],
                "blocked": [".git/**"],
            },
            "checks": {
                "compile": {"argv": ["true"], "timeout_seconds": 1},
                "scanner": {"argv": ["true"], "timeout_seconds": 1},
                "unit": {"argv": ["true"], "timeout_seconds": 1},
                "security": {"argv": ["true"], "timeout_seconds": 1},
            },
        }
    )


def _finding() -> Finding:
    return Finding(
        id="sql-1",
        source=FindingSource.MANUAL,
        title="SQL injection",
        description="User input reaches SQL.",
        severity="high",
        file_path="src/app.py",
        verification_recipe="sql_injection_oracle",
    )


def _snapshot() -> SnapshotArtifact:
    return SnapshotArtifact(
        commit_sha="0" * 40,
        raw_sha256="0" * 64,
        snapshot_sha256="0" * 64,
        sanitized_archive_sha256="0" * 64,
        entries=(),
        archive=b"",
    )


class FakeRuntime:
    def __init__(self) -> None:
        self.cleaned: list[Sandbox] = []
        self.sandbox = Sandbox(
            run_id="run-1",
            image_id="img",
            volume_name="vol",
            container_id="ctr",
            baseline_sha="0" * 40,
        )

    def create_work_sandbox(self, **kwargs: object) -> Sandbox:
        return self.sandbox

    def cleanup(self, sandbox: Sandbox) -> None:
        self.cleaned.append(sandbox)


class BoomMCP:
    def __init__(self, container_id: str) -> None:
        self.container_id = container_id

    async def __aenter__(self) -> BoomMCP:
        raise RuntimeError("mcp failed")

    async def __aexit__(self, *args: object) -> None:
        return None


class BoomMCPExit:
    def __init__(self, container_id: str) -> None:
        self.container_id = container_id

    async def __aenter__(self) -> BoomMCPExit:
        return self

    async def __aexit__(self, *args: object) -> None:
        raise RuntimeError("mcp exit failed")


async def test_mcp_connect_failure_cleans_up_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(
        "patchcage.harness.docker_session.WorkspaceMCPClient", BoomMCP
    )
    session = DockerWorkspaceSession(
        runtime=runtime,  # type: ignore[arg-type]
        image="patchcage/python-demo:dev",
        snapshot=_snapshot(),
        manifest=_manifest(),
        finding=_finding(),
    )

    with pytest.raises(RuntimeError, match="mcp failed"):
        await session.__aenter__()

    assert runtime.cleaned == [runtime.sandbox]
    assert session._sandbox is None
    assert session._mcp is None


async def test_mcp_exit_failure_still_cleans_up_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(
        "patchcage.harness.docker_session.WorkspaceMCPClient", BoomMCPExit
    )
    session = DockerWorkspaceSession(
        runtime=runtime,  # type: ignore[arg-type]
        image="patchcage/python-demo:dev",
        snapshot=_snapshot(),
        manifest=_manifest(),
        finding=_finding(),
    )

    await session.__aenter__()
    with pytest.raises(RuntimeError, match="mcp exit failed"):
        await session.__aexit__(None, None, None)

    assert runtime.cleaned == [runtime.sandbox]
    assert session._sandbox is None
    assert session._mcp is None
