"""Docker-backed WorkspaceSession: the production seam for the runner.

ponytail: container creation and host checks block the event loop, so
SIGTERM/SIGINT is only delivered at the next await (a host check can run up
to its timeout). One run per process; move these onto threads if the CLI
needs prompt cancel during checks.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from patchcage.domain import CheckResult, Finding, ProjectManifest
from patchcage.harness.runner import SessionFactory, WorkspaceSession
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
        tb: object,
    ) -> None:
        incoming_cancel = exc_type is asyncio.CancelledError
        try:
            if self._mcp is not None:
                try:
                    await self._mcp.__aexit__(exc_type, exc, tb)
                except Exception as close_error:
                    if not incoming_cancel:
                        raise
                    print(
                        f"mcp close failed during cancel: {close_error}",
                        file=sys.stderr,
                    )
        finally:
            self._mcp = None
            if self._sandbox is not None:
                try:
                    self._runtime.cleanup(self._sandbox)
                except Exception as cleanup_error:
                    if not incoming_cancel:
                        raise
                    print(
                        f"sandbox cleanup failed during cancel: {cleanup_error}",
                        file=sys.stderr,
                    )
                finally:
                    self._sandbox = None

    @property
    def baseline_sha(self) -> str:
        if self._sandbox is None:
            raise RuntimeError("session is not open")
        return self._sandbox.baseline_sha

    @property
    def image_id(self) -> str:
        if self._sandbox is None:
            raise RuntimeError("session is not open")
        return self._sandbox.image_id

    async def list_tools(self) -> dict[str, dict[str, Any]]:
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
        return run_named_check(
            self._sandbox, name, self._manifest, allow_security=True, runtime=self._runtime
        )


def docker_session_factory(
    *,
    runtime: DockerRuntime,
    image: str,
    manifest: ProjectManifest,
    finding: Finding,
) -> SessionFactory:
    """Build a session factory binding everything except the snapshot.

    `finding` must be the same Finding object passed to RunRequest — the
    sandbox seeds it into state.json for MCP get_finding, while the runner
    shows request.finding to the model.
    """

    def factory(snapshot: SnapshotArtifact) -> WorkspaceSession:
        return DockerWorkspaceSession(
            runtime=runtime,
            image=image,
            snapshot=snapshot,
            manifest=manifest,
            finding=finding,
        )

    return factory
