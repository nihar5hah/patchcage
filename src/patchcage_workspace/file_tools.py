from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from patchcage.policy import AccessMode, PolicyViolation
from patchcage_workspace.context import WorkspaceContext
from patchcage_workspace.gitutil import is_status_noise, porcelain_path

MAX_FILE_BYTES = 64_000
MAX_LINE_CHARS = 8_192
MAX_SEARCH_MATCHES = 50
SEARCH_TIMEOUT_SECONDS = 2.0


class FileEntry(BaseModel):
    name: str
    is_dir: bool


class ListFilesResult(BaseModel):
    path: str
    entries: list[FileEntry]


class ReadFileResult(BaseModel):
    path: str
    content: str
    truncated: bool


class SearchMatch(BaseModel):
    path: str
    line: int
    text: str


class SearchCodeResult(BaseModel):
    matches: list[SearchMatch]
    truncated: bool


class RepositoryStatus(BaseModel):
    baseline_sha: str
    dirty: bool
    summary: str


def _raise_policy(error: PolicyViolation) -> None:
    raise ToolError(f"{error.code}: {error}") from error


def _git_output(argv: list[str], cwd: Path) -> str:
    completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise ToolError(f"GIT_COMMAND_FAILED: {detail}")
    return completed.stdout


def register_file_tools(server: MCPServer, ctx: WorkspaceContext) -> None:
    @server.tool(structured_output=True)
    def list_files(path: str = ".") -> ListFilesResult:
        """List a readable directory in the disposable workspace."""
        try:
            directory = ctx.policy.resolve_existing(path, AccessMode.READ)
        except PolicyViolation as error:
            _raise_policy(error)
        if not directory.is_dir():
            raise ToolError("DENY_NON_DIRECTORY: list_files requires a directory")
        entries: list[FileEntry] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = child.relative_to(ctx.root).as_posix()
            try:
                ctx.policy.resolve_existing(relative, AccessMode.READ)
            except PolicyViolation:
                continue
            entries.append(FileEntry(name=child.name, is_dir=child.is_dir()))
        return ListFilesResult(path=path, entries=entries)

    @server.tool(structured_output=True)
    def read_file(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ReadFileResult:
        """Read a readable regular file, optionally sliced by 1-based line range."""
        try:
            target = ctx.policy.resolve_file(path, AccessMode.READ)
        except PolicyViolation as error:
            _raise_policy(error)
        data = target.read_bytes()
        truncated = len(data) > MAX_FILE_BYTES
        text = data[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        if start_line is not None or end_line is not None:
            lines = text.splitlines(keepends=True)
            start = (start_line or 1) - 1
            stop = end_line if end_line is not None else len(lines)
            if start < 0 or (end_line is not None and end_line < (start_line or 1)):
                raise ToolError("DENY_LINE_RANGE: invalid line range")
            text = "".join(lines[start:stop])
        return ReadFileResult(path=path, content=text, truncated=truncated)

    @server.tool(structured_output=True)
    def search_code(pattern: str, path: str = "src") -> SearchCodeResult:
        """Search readable files with a bounded regular expression."""
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ToolError(f"DENY_INVALID_REGEX: {error}") from error
        try:
            root = ctx.policy.resolve_existing(path, AccessMode.READ)
        except PolicyViolation as error:
            _raise_policy(error)

        files = (
            [root]
            if root.is_file()
            else [candidate for candidate in root.rglob("*") if candidate.is_file()]
        )
        matches: list[SearchMatch] = []
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        for file in files:
            if time.monotonic() > deadline:
                raise ToolError("SEARCH_TIMEOUT: search_code exceeded wall-clock limit")
            relative = file.relative_to(ctx.root).as_posix()
            try:
                target = ctx.policy.resolve_file(relative, AccessMode.READ)
            except PolicyViolation:
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(text.splitlines(), start=1):
                if time.monotonic() > deadline:
                    raise ToolError("SEARCH_TIMEOUT: search_code exceeded wall-clock limit")
                # ponytail: compiled.search can still backtrack past this deadline
                # on one pathological line; bound with a worker that can be killed.
                if len(line) > MAX_LINE_CHARS or compiled.search(line) is None:
                    continue
                matches.append(SearchMatch(path=relative, line=index, text=line[:500]))
                if len(matches) >= MAX_SEARCH_MATCHES:
                    return SearchCodeResult(matches=matches, truncated=True)
        return SearchCodeResult(matches=matches, truncated=False)

    @server.tool(structured_output=True)
    def get_repository_status() -> RepositoryStatus:
        """Show whether the disposable workspace differs from its baseline commit."""
        text = _git_output(["git", "status", "--porcelain"], ctx.root)
        tracked = [
            line for line in text.splitlines() if not is_status_noise(porcelain_path(line))
        ]
        return RepositoryStatus(
            baseline_sha=ctx.baseline_sha,
            dirty=bool(tracked),
            summary="\n".join(tracked) or "clean",
        )
