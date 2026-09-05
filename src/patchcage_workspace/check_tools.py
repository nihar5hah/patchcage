from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from patchcage.domain import Finding
from patchcage_workspace.context import WorkspaceContext


def register_check_tools(server: MCPServer, ctx: WorkspaceContext) -> None:
    @server.tool(structured_output=True)
    def get_finding() -> Finding:
        """Return the host-normalized finding for this run."""
        if ctx.finding is None:
            raise ToolError("DENY_MISSING_FINDING: no finding was provided for this sandbox")
        return ctx.finding
