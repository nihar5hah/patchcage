from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from patchcage.domain import CheckStatus, Finding, FindingSource, load_manifest
from patchcage.mcp import MCPToolError, WorkspaceMCPClient
from patchcage.sandbox.check_runner import run_named_check
from patchcage.sandbox.docker_runtime import DockerRuntime, Sandbox, SandboxError
from patchcage.sandbox.image import IMAGE_TAG, build_runtime_image
from patchcage.sandbox_env import SANDBOX_ENV
from patchcage.snapshot import create_snapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CREATE_DEMO = PROJECT_ROOT / "scripts" / "create_demo_repo.py"
FIX_PATCH = PROJECT_ROOT / "tests" / "fixtures" / "sql_injection_fix.patch"
RULE_ID = "patchcage.python.sql-injection.formatted-query"
MANIFEST_PATH = PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"

EXPECTED_TOOLS = {
    "get_finding",
    "list_files",
    "read_file",
    "search_code",
    "get_repository_status",
    "run_finding_check",
    "propose_patch",
    "get_current_diff",
    "run_named_check",
    "discard_patch",
}


def _docker_ready() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_ready(), reason="Docker daemon is not running"),
]


def demo_finding() -> Finding:
    return Finding(
        id="sql-1",
        source=FindingSource.SEMGREP_SARIF,
        rule_id=RULE_ID,
        title="SQL injection via formatted query",
        description="User input is interpolated into a SQL execute call.",
        severity="ERROR",
        file_path="src/demo_app/search.py",
        start_line=20,
        verification_recipe="sql_injection_oracle",
    )


@pytest.fixture(scope="session")
def runtime_image_id() -> str:
    return build_runtime_image()


@pytest.fixture
def sql_harness(runtime_image_id: str, tmp_path: Path):
    repository = tmp_path / "sql-demo"
    created = subprocess.run(
        [sys.executable, str(CREATE_DEMO), str(repository)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    commit_sha = json.loads(created.stdout)["commit_sha"]
    manifest = load_manifest(MANIFEST_PATH)
    snapshot = create_snapshot(repository, commit_sha, blocked_patterns=manifest.scope.blocked)
    runtime = DockerRuntime()
    sandbox = runtime.create_work_sandbox(
        image=IMAGE_TAG,
        snapshot=snapshot,
        manifest=manifest,
        finding=demo_finding(),
    )
    try:
        yield {
            "runtime": runtime,
            "sandbox": sandbox,
            "repository": repository,
            "manifest": manifest,
            "commit_sha": commit_sha,
            "env_text": (repository / ".env").read_text(),
        }
    finally:
        runtime.cleanup(sandbox)


def _semgrep_results(sandbox: Sandbox) -> list[dict[str, object]]:
    env_flags = [
        item
        for key, value in SANDBOX_ENV.items()
        for item in ("-e", f"{key}={value}")
    ]
    completed = subprocess.run(
        [
            "docker",
            "exec",
            "-u",
            "1000:1000",
            "-w",
            "/workspace",
            *env_flags,
            sandbox.container_id,
            "semgrep",
            "scan",
            "--config",
            "/opt/patchcage/rules/sql-injection.yml",
            "--metrics",
            "off",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(completed.stdout)["results"]
    except json.JSONDecodeError as error:
        raise AssertionError(completed.stderr or completed.stdout) from error


def _exec(sandbox: Sandbox, argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", "-u", "1000:1000", "-w", "/workspace", sandbox.container_id, *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def test_unresolved_image_is_rejected() -> None:
    runtime = DockerRuntime()
    with pytest.raises(SandboxError) as error:
        runtime.resolve_image("patchcage/python-flask-demo:unresolved")
    assert error.value.code == "IMAGE_UNRESOLVED"


def test_sandbox_has_no_network_and_runs_as_non_root(sql_harness: dict) -> None:
    sandbox = sql_harness["sandbox"]
    identity = _exec(sandbox, ["id", "-u"])
    assert identity.returncode == 0
    assert identity.stdout.strip() == "1000"

    probe = _exec(
        sandbox,
        [
            "python",
            "-c",
            "import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=2)",
        ],
    )
    assert probe.returncode != 0

    socket_probe = _exec(
        sandbox,
        ["python", "-c", "import os; print(os.path.exists('/var/run/docker.sock'))"],
    )
    assert socket_probe.stdout.strip() == "False"


def test_snapshot_does_not_modify_original_repository(sql_harness: dict) -> None:
    repository = sql_harness["repository"]
    assert (repository / ".env").read_text() == sql_harness["env_text"]
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "?? .env" in status
    search = (repository / "src" / "demo_app" / "search.py").read_text()
    assert "LIKE '%{query}%'" in search


def test_baseline_and_reference_patch_inside_image(sql_harness: dict) -> None:
    sandbox = sql_harness["sandbox"]
    manifest = sql_harness["manifest"]

    compile_result = run_named_check(sandbox, "compile", manifest)
    unit_result = run_named_check(sandbox, "unit", manifest)
    oracle_result = run_named_check(sandbox, "security", manifest, allow_security=True)
    baseline_findings = _semgrep_results(sandbox)

    assert compile_result.status is CheckStatus.PASSED
    assert unit_result.status is CheckStatus.PASSED
    assert any(str(item["check_id"]).endswith(RULE_ID) for item in baseline_findings)
    assert "PATCHCAGE_VULNERABILITY_REPRODUCED" in oracle_result.summary

    applied = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-u",
            "1000:1000",
            "-w",
            "/workspace",
            sandbox.container_id,
            "git",
            "apply",
        ],
        input=FIX_PATCH.read_text(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr

    compile_after = run_named_check(sandbox, "compile", manifest)
    unit_after = run_named_check(sandbox, "unit", manifest)
    oracle_after = run_named_check(sandbox, "security", manifest, allow_security=True)
    patched_findings = _semgrep_results(sandbox)

    assert compile_after.status is CheckStatus.PASSED
    assert unit_after.status is CheckStatus.PASSED
    assert "PATCHCAGE_SECURITY_ORACLE_PASSED" in oracle_after.summary
    assert all(not str(item["check_id"]).endswith(RULE_ID) for item in patched_findings)


@pytest.mark.usefixtures("runtime_image_id")
def test_cleanup_removes_labeled_resources(tmp_path: Path) -> None:
    import docker

    repository = tmp_path / "sql-demo"
    created = subprocess.run(
        [sys.executable, str(CREATE_DEMO), str(repository)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    commit_sha = json.loads(created.stdout)["commit_sha"]
    manifest = load_manifest(MANIFEST_PATH)
    snapshot = create_snapshot(repository, commit_sha, blocked_patterns=manifest.scope.blocked)
    runtime = DockerRuntime()
    sandbox = runtime.create_work_sandbox(
        image=IMAGE_TAG,
        snapshot=snapshot,
        manifest=manifest,
        finding=demo_finding(),
    )
    container_id = sandbox.container_id
    volume_name = sandbox.volume_name
    runtime.cleanup(sandbox)
    from docker.errors import NotFound

    client = docker.from_env()
    with pytest.raises(NotFound):
        client.containers.get(container_id)
    names = {volume.name for volume in client.volumes.list()}
    assert volume_name not in names


@pytest.mark.asyncio
async def test_mcp_stdio_tools_and_policy_denials(sql_harness: dict) -> None:
    sandbox = sql_harness["sandbox"]
    async with WorkspaceMCPClient(sandbox.container_id, timeout_seconds=90) as client:
        tools = set(await client.list_tools())
        assert tools >= EXPECTED_TOOLS

        finding = await client.call("get_finding")
        assert finding["rule_id"] == RULE_ID

        listing = await client.call("list_files", {"path": "src"})
        names = {entry["name"] for entry in listing["entries"]}
        assert "demo_app" in names
        assert ".git" not in names
        assert ".patchcage" not in names

        source = await client.call("read_file", {"path": "src/demo_app/search.py"})
        assert "LIKE" in source["content"]

        hits = await client.call("search_code", {"pattern": "execute", "path": "src"})
        assert hits["matches"]

        with pytest.raises(MCPToolError) as security_error:
            await client.call("run_named_check", {"name": "security"})
        assert security_error.value.code == "DENY_UNKNOWN_CHECK"

        with pytest.raises(MCPToolError) as traversal_error:
            await client.call("read_file", {"path": "../etc/passwd"})
        assert traversal_error.value.code == "DENY_PATH_TRAVERSAL"

        with pytest.raises(MCPToolError) as git_error:
            await client.call("read_file", {"path": ".git/HEAD"})
        assert git_error.value.code == "DENY_BLOCKED_FILE"

        with pytest.raises(MCPToolError) as oracle_error:
            await client.call(
                "read_file",
                {"path": "/opt/patchcage/oracles/sql_injection_oracle.py"},
            )
        assert oracle_error.value.code == "DENY_ABSOLUTE_PATH"

        patch = await client.call("propose_patch", {"diff": FIX_PATCH.read_text()})
        assert patch["applied"] is True
        diff = await client.call("get_current_diff")
        assert "search_products" in diff["diff"]
        discarded = await client.call("discard_patch")
        assert discarded["discarded"] is True
        clean_diff = await client.call("get_current_diff")
        assert clean_diff["dirty"] is False
        status = await client.call("get_repository_status")
        assert status["baseline_sha"] == sandbox.baseline_sha
