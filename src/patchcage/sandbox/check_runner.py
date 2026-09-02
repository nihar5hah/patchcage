from __future__ import annotations

from patchcage.domain import CheckResult, CommandSpec, ProjectManifest
from patchcage.sandbox.docker_runtime import Sandbox
from patchcage.sandbox.process import run_local_command
from patchcage.sandbox_env import SANDBOX_ENV

MODEL_CHECK_NAMES = frozenset({"compile", "scanner", "unit"})
HOST_CHECK_NAMES = MODEL_CHECK_NAMES | {"security"}


def _spec_for(manifest: ProjectManifest, name: str) -> CommandSpec:
    checks = {
        "compile": manifest.checks.compile_check,
        "scanner": manifest.checks.scanner,
        "unit": manifest.checks.unit,
        "security": manifest.checks.security,
    }
    if name not in checks:
        raise KeyError(name)
    return checks[name]


def run_named_check(
    sandbox: Sandbox,
    name: str,
    manifest: ProjectManifest,
    *,
    allow_security: bool = False,
) -> CheckResult:
    if name == "security" and not allow_security:
        raise PermissionError("DENY_UNKNOWN_CHECK")
    if name not in HOST_CHECK_NAMES:
        raise KeyError(name)
    spec = _spec_for(manifest, name)
    environment = dict(SANDBOX_ENV)
    environment.update(spec.env)
    argv = [
        "docker",
        "exec",
        "-u",
        "1000:1000",
        "-w",
        sandbox.workdir,
        *[item for key, value in environment.items() for item in ("-e", f"{key}={value}")],
        sandbox.container_id,
        *spec.argv,
    ]
    return run_local_command(
        argv,
        name=name,
        timeout_seconds=spec.timeout_seconds,
        env=None,
    )
