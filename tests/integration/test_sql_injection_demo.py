from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CREATE_DEMO = PROJECT_ROOT / "scripts" / "create_demo_repo.py"
FIX_PATCH = PROJECT_ROOT / "tests" / "fixtures" / "sql_injection_fix.patch"
ORACLE = PROJECT_ROOT / "runtime" / "python-demo" / "oracles" / "sql_injection_oracle.py"
RULE = PROJECT_ROOT / "runtime" / "python-demo" / "rules" / "sql-injection.yml"
SETTINGS = PROJECT_ROOT / "runtime" / "python-demo" / "semgrep" / "offline-settings.yml"
RULE_ID = "patchcage.python.sql-injection.formatted-query"
UNIT_RUNNER = PROJECT_ROOT / "runtime" / "python-demo" / "runners" / "run_unit.py"


def run(
    argv: list[str],
    *,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def run_unit_checks(repository: Path) -> None:
    run([sys.executable, "-m", "compileall", "-q", "src"], cwd=repository)
    run(
        [sys.executable, str(UNIT_RUNNER), str(repository), "tests/unit"],
        cwd=repository,
    )


def semgrep_binary() -> str:
    isolated = PROJECT_ROOT / ".venvs" / "semgrep" / "bin" / "semgrep"
    if isolated.exists():
        return str(isolated)
    return str(Path(sys.executable).with_name("semgrep"))


def semgrep_results(repository: Path) -> list[dict[str, object]]:
    semgrep = semgrep_binary()
    environment = os.environ | {
        "SEMGREP_ENABLE_VERSION_CHECK": "0",
        "SEMGREP_SETTINGS_FILE": str(SETTINGS),
    }
    completed = run(
        [
            semgrep,
            "scan",
            "--config",
            str(RULE),
            "--metrics",
            "off",
            "--json",
            "src",
        ],
        cwd=repository,
        env=environment,
    )
    parsed = json.loads(completed.stdout)
    return parsed["results"]


def run_oracle(repository: Path) -> subprocess.CompletedProcess[str]:
    return run(
        [sys.executable, str(ORACLE), "--workspace", str(repository)],
        cwd=repository,
        check=False,
    )


def test_vulnerable_demo_and_reference_patch_have_opposite_results(tmp_path: Path) -> None:
    if not Path(semgrep_binary()).exists():
        pytest.skip("isolated Semgrep venv is missing")
    repository = tmp_path / "sql-demo"
    created = run([sys.executable, str(CREATE_DEMO), str(repository)], cwd=PROJECT_ROOT)
    commit_sha = json.loads(created.stdout)["commit_sha"]

    assert run(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip() == commit_sha
    assert "?? .env" in run(["git", "status", "--short"], cwd=repository).stdout
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit_sha],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        assert ".env" not in tar.getnames()

    run_unit_checks(repository)
    baseline_findings = semgrep_results(repository)
    assert any(str(result["check_id"]).endswith(RULE_ID) for result in baseline_findings)
    vulnerable = run_oracle(repository)
    assert vulnerable.returncode == 1
    assert "PATCHCAGE_VULNERABILITY_REPRODUCED" in vulnerable.stdout

    run(["git", "apply", str(FIX_PATCH)], cwd=repository)

    run_unit_checks(repository)
    patched_findings = semgrep_results(repository)
    assert all(not str(result["check_id"]).endswith(RULE_ID) for result in patched_findings)
    secure = run_oracle(repository)
    assert secure.returncode == 0
    assert "PATCHCAGE_SECURITY_ORACLE_PASSED" in secure.stdout


def test_oracle_does_not_execute_workspace_sitecustomize(tmp_path: Path) -> None:
    repository = tmp_path / "sql-demo"
    run([sys.executable, str(CREATE_DEMO), str(repository)], cwd=PROJECT_ROOT)
    (repository / "src" / "sitecustomize.py").write_text("import os\nos._exit(0)\n")

    vulnerable = run_oracle(repository)

    assert vulnerable.returncode == 1
    assert "PATCHCAGE_VULNERABILITY_REPRODUCED" in vulnerable.stdout


def test_filtering_only_known_payloads_is_not_a_fix(tmp_path: Path) -> None:
    repository = tmp_path / "sql-demo"
    run([sys.executable, str(CREATE_DEMO), str(repository)], cwd=PROJECT_ROOT)
    path = repository / "src" / "demo_app" / "search.py"
    source = path.read_text()
    source = source.replace(
        "    return list(connection.execute(f\"SELECT id, name FROM products "
        "WHERE name LIKE '%{query}%'\"))",
        "    if query in (\"' OR 1=1 --\", \"' OR '1'='1\"):\n"
        "        return []\n"
        "    statement = f\"SELECT id, name FROM products WHERE name LIKE '%{query}%'\"\n"
        "    return list(connection.execute(statement))",
    )
    path.write_text(source)
    assert "statement =" in source
    oracle = run_oracle(repository)
    assert oracle.returncode == 1, oracle.stdout + oracle.stderr
    if Path(semgrep_binary()).exists():
        assert semgrep_results(repository), "scanner missed taint through an intermediate variable"
