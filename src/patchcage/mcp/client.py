from __future__ import annotations

import json
from typing import Any

from mcp import Client, StdioServerParameters

# Must exceed the largest legitimate result: a max-size patch (limits.patch_bytes,
# default 65_536) returns from get_current_diff with git-regenerated overhead, and
# read_file's 64_000-byte content cap grows under JSON escaping.
MAX_RESULT_BYTES = 256_000


class MCPToolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_tool_error(text: str) -> MCPToolError:
    for part in text.split(":"):
        token = part.strip().split()[0] if part.strip() else ""
        if (
            token.startswith("DENY_")
            or token.endswith("_FAILED")
            or token
            in {
                "SEARCH_TIMEOUT",
                "TOOL_ERROR",
                "RESULT_TOO_LARGE",
                "UNSTRUCTURED_RESULT",
            }
        ):
            return MCPToolError(token, text)
    return MCPToolError("TOOL_ERROR", text)


class WorkspaceMCPClient:
    def __init__(self, container_id: str, *, timeout_seconds: float = 90) -> None:
        self._params = StdioServerParameters(
            command="docker",
            args=[
                "exec",
                "-i",
                "-u",
                "1000:1000",
                "-w",
                "/workspace",
                "-e",
                "PYTHONUNBUFFERED=1",
                container_id,
                "python",
                "-m",
                "patchcage_workspace",
            ],
        )
        self._timeout_seconds = timeout_seconds
        self._client: Client | None = None

    async def __aenter__(self) -> WorkspaceMCPClient:
        self._client = Client(self._params, read_timeout_seconds=self._timeout_seconds)
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        assert self._client is not None
        await self._client.__aexit__(exc_type, exc, tb)
        self._client = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("MCP client is not connected")
        return self._client

    async def list_tools(self) -> list[str]:
        result = await self.client.list_tools()
        return [tool.name for tool in result.tools]

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self.client.call_tool(name, arguments or {})
        if result.is_error:
            text = ""
            if result.content:
                block = result.content[0]
                text = getattr(block, "text", str(block))
            raise _parse_tool_error(text)
        payload = result.structured_content
        if payload is None:
            raise MCPToolError("UNSTRUCTURED_RESULT", f"{name} returned no structured content")
        encoded = json.dumps(payload)
        if len(encoded.encode()) > MAX_RESULT_BYTES:
            raise MCPToolError(
                "RESULT_TOO_LARGE",
                f"{name} result exceeded {MAX_RESULT_BYTES} bytes",
            )
        if not isinstance(payload, dict):
            raise MCPToolError("UNSTRUCTURED_RESULT", f"{name} structured content is not an object")
        return payload
