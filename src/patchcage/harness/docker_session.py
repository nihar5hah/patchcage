"""Docker-backed WorkspaceSession: the production seam for the runner.

ponytail: container creation and host checks block the event loop. The engine
CLI runs one run per process, so pushing these onto threads is not worth it
yet; revisit if the CLI ever runs concurrent runs in-process.
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any

from patchcage.domain import CheckResult, Finding, ProjectManifest
from patchcage.mcp import WorkspaceMCPClient
from patchcage.sandbox.check_runner import run_named_check
from patchcage.sandbox.docker_runtime import DockerRuntime, Sandbox
from patchcage.snapshot import SnapshotArtifact


class DockerWorkspaceSession:
    """One locked work sandbox plus its MCP client."""

    def __init__(
        self,
        *,
        runtime: DockerRuntime,
        image: str,
        snapshot: SnapshotArtifact,
        manifest: ProjectManifest,
        finding: Finding,
    ) -> None:
        self._runtime = runtime
        self._image = image
        self._snapshot = snapshot
        self._manifest = manifest
        self._finding = finding
        self._sandbox: Sandbox | None = None
        self._mcp: WorkspaceMCPClient | None = None

    async def __aenter__(self) -> DockerWorkspaceSession:
        self._sandbox = self._runtime.create_work_sandbox(
            image=self._image,
            snapshot=self._snapshot,
            manifest=self._manifest,
            finding=self._finding,
        )
        try:
            self._mcp = WorkspaceMCPClient(self._sandbox.container_id)
            await self._mcp.__aenter__()
        except BaseException:
            self._runtime.cleanup(self._sandbox)
            self._sandbox = None
            self._mcp = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._mcp is not None:
                await self._mcp.__aexit__(exc_type, exc, tb)
        finally:
            self._mcp = None
            if self._sandbox is not None:
                self._runtime.cleanup(self._sandbox)
                self._sandbox = None

    @property
    def baseline_sha(self) -> str:
        if self._sandbox is None:
            raise RuntimeError("session is not open")
        return self._sandbox.baseline_sha

    async def list_tools(self) -> list[str]:
        if self._mcp is None:
            raise RuntimeError("session is not open")
        return await self._mcp.list_tools()

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._mcp is None:
            raise RuntimeError("session is not open")
        return await self._mcp.call(tool, arguments)

    def run_host_check(self, name: str) -> CheckResult:
        if self._sandbox is None:
            raise RuntimeError("session is not open")
        return run_named_check(self._sandbox, name, self._manifest, allow_security=True)


def docker_session_factory(
    *,
    runtime: DockerRuntime,
    image: str,
    manifest: ProjectManifest,
    finding: Finding,
) -> Callable[[SnapshotArtifact], DockerWorkspaceSession]:
    """Build a session factory binding everything except the snapshot.

    `finding` must be the same Finding object passed to RunRequest — the
    sandbox seeds it into state.json for MCP get_finding, while the runner
    shows request.finding to the model.
    """

    def factory(snapshot: SnapshotArtifact) -> DockerWorkspaceSession:
        return DockerWorkspaceSession(
            runtime=runtime,
            image=image,
            snapshot=snapshot,
            manifest=manifest,
            finding=finding,
        )

    return factory
