from __future__ import annotations

import subprocess

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from patchcage.policy import PatchPolicyError, inspect_patch
from patchcage_workspace.context import WorkspaceContext
from patchcage_workspace.gitutil import GitFailed, working_tree_diff


class PatchApplyResult(BaseModel):
    applied: bool
    sha256: str
    files: list[str]


class CurrentDiffResult(BaseModel):
    diff: str
    dirty: bool


class DiscardResult(BaseModel):
    discarded: bool


def _git(ctx: WorkspaceContext, *args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ctx.root,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise ToolError(f"GIT_COMMAND_FAILED: {detail}")
    return completed.stdout


def _reset_to_baseline(ctx: WorkspaceContext) -> None:
    _git(ctx, "reset", "--hard", ctx.baseline_sha)
    _git(ctx, "clean", "-fd", "--exclude=.patchcage")


def register_patch_tools(server: MCPServer, ctx: WorkspaceContext) -> None:
    @server.tool(structured_output=True)
    def propose_patch(diff: str) -> PatchApplyResult:
        """Reset to baseline and apply a complete candidate patch."""
        try:
            metadata = inspect_patch(diff, scope=ctx.manifest.scope, limits=ctx.manifest.limits)
        except PatchPolicyError as error:
            raise ToolError(f"{error.code}: {error}") from error
        _reset_to_baseline(ctx)
        _git(ctx, "apply", "--check", input_text=diff)
        _git(ctx, "apply", input_text=diff)
        return PatchApplyResult(
            applied=True,
            sha256=metadata.sha256,
            files=[file.path for file in metadata.files],
        )

    @server.tool(structured_output=True)
    def get_current_diff() -> CurrentDiffResult:
        """Return the working-tree diff against HEAD, including new files."""
        try:
            diff = working_tree_diff(ctx.root)
        except GitFailed as error:
            raise ToolError(f"GIT_COMMAND_FAILED: {error}") from error
        return CurrentDiffResult(diff=diff, dirty=bool(diff.strip()))

    @server.tool(structured_output=True)
    def discard_patch() -> DiscardResult:
        """Drop working-tree changes and restore the baseline snapshot."""
        _reset_to_baseline(ctx)
        return DiscardResult(discarded=True)
