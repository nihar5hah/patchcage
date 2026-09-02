from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from patchcage.domain import CheckResult, CommandSpec, Finding
from patchcage.sandbox.process import run_local_command
from patchcage.sandbox_env import SANDBOX_ENV
from patchcage_workspace.context import WorkspaceContext

MODEL_CHECK_NAMES = frozenset({"compile", "scanner", "unit"})


def _spec(ctx: WorkspaceContext, name: str) -> CommandSpec:
    mapping = {
        "compile": ctx.manifest.checks.compile_check,
        "scanner": ctx.manifest.checks.scanner,
        "unit": ctx.manifest.checks.unit,
        "security": ctx.manifest.checks.security,
    }
    return mapping[name]


def _run_check(ctx: WorkspaceContext, name: str) -> CheckResult:
    spec = _spec(ctx, name)
    env = dict(SANDBOX_ENV)
    env.update(spec.env)
    return run_local_command(
        spec.argv,
        name=name,
        timeout_seconds=spec.timeout_seconds,
        cwd=ctx.root,
        env=env,
    )


def register_check_tools(server: MCPServer, ctx: WorkspaceContext) -> None:
    @server.tool(structured_output=True)
    def get_finding() -> Finding:
        """Return the host-normalized finding for this run."""
        if ctx.finding is None:
            raise ToolError("DENY_MISSING_FINDING: no finding was provided for this sandbox")
        return ctx.finding

    @server.tool(structured_output=True)
    def run_finding_check() -> CheckResult:
        """Run the scanner check that confirms the imported finding is present."""
        return _run_check(ctx, "scanner")

    @server.tool(structured_output=True)
    def run_named_check(name: str) -> CheckResult:
        """Run compile, scanner, or unit. The hidden security oracle is not available."""
        if name == "security" or name not in MODEL_CHECK_NAMES:
            raise ToolError("DENY_UNKNOWN_CHECK: security oracle is not model-invocable")
        return _run_check(ctx, name)
