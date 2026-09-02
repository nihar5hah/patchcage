from __future__ import annotations

from mcp.server import MCPServer

from patchcage_workspace.check_tools import register_check_tools
from patchcage_workspace.context import WorkspaceContext
from patchcage_workspace.file_tools import register_file_tools
from patchcage_workspace.patch_tools import register_patch_tools


def create_server(ctx: WorkspaceContext | None = None) -> MCPServer:
    context = ctx or WorkspaceContext.from_control_file()
    server = MCPServer(
        "patchcage-workspace",
        instructions="Inspect and patch the disposable workspace through the approved tools only.",
    )
    register_file_tools(server, context)
    register_check_tools(server, context)
    register_patch_tools(server, context)
    return server
