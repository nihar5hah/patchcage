"""DockerRuntime cleanup without a daemon."""

from __future__ import annotations

import pytest
from docker.errors import APIError

from patchcage.sandbox.docker_runtime import DockerRuntime


class _BoomList:
    def list(self, **kwargs: object) -> list[object]:
        raise RuntimeError("docker down")


class _Gone:
    def remove(self, force: bool = False) -> None:
        raise APIError("already gone")


class _OkList:
    def list(self, **kwargs: object) -> list[_Gone]:
        return [_Gone()]


def test_cleanup_run_list_failure_propagates() -> None:
    client = type("Client", (), {"containers": _BoomList(), "volumes": _OkList()})()
    runtime = DockerRuntime(client=client)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="docker down"):
        runtime.cleanup_run("run-1")


def test_cleanup_run_remove_api_error_is_ignored() -> None:
    client = type("Client", (), {"containers": _OkList(), "volumes": _OkList()})()
    runtime = DockerRuntime(client=client)  # type: ignore[arg-type]
    runtime.cleanup_run("run-1")
