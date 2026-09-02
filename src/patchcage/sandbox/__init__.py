"""Disposable Docker sandboxes and bounded check execution.

Imports are lazy so that `patchcage.sandbox_env` (a sibling module) and the
in-container MCP server never load docker-py.
"""

from __future__ import annotations

__all__ = [
    "IMAGE_TAG",
    "DockerRuntime",
    "Sandbox",
    "SandboxError",
    "build_runtime_image",
    "run_local_command",
    "run_named_check",
]


def __getattr__(name: str) -> object:
    if name in ("DockerRuntime", "Sandbox", "SandboxError"):
        from patchcage.sandbox.docker_runtime import DockerRuntime, Sandbox, SandboxError

        return {"DockerRuntime": DockerRuntime, "Sandbox": Sandbox, "SandboxError": SandboxError}[
            name
        ]
    if name == "run_named_check":
        from patchcage.sandbox.check_runner import run_named_check

        return run_named_check
    if name in ("IMAGE_TAG", "build_runtime_image"):
        from patchcage.sandbox.image import IMAGE_TAG, build_runtime_image

        return {"IMAGE_TAG": IMAGE_TAG, "build_runtime_image": build_runtime_image}[name]
    if name == "run_local_command":
        from patchcage.sandbox.process import run_local_command

        return run_local_command
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
