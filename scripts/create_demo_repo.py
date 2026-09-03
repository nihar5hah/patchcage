from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = PROJECT_ROOT / "demo_projects" / "flask_sql_injection"


def run_git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def create_demo_repo(destination: Path, template: Path = DEFAULT_TEMPLATE) -> str:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")

    shutil.copytree(template, destination)
    finding = PROJECT_ROOT / "manifests" / f"{template.name}.finding.yml"
    manifest = PROJECT_ROOT / "manifests" / f"{template.name}.yml"
    if finding.is_file() and manifest.is_file():
        dest = destination / "manifests"
        dest.mkdir(exist_ok=True)
        shutil.copy2(finding, dest / finding.name)
        shutil.copy2(manifest, dest / manifest.name)
    run_git(destination, "init", "-q", "-b", "main")
    run_git(destination, "add", ".")
    run_git(
        destination,
        "-c",
        "user.name=PatchCage Demo",
        "-c",
        "user.email=demo@patchcage.invalid",
        "commit",
        "-q",
        "-m",
        "Create vulnerable SQL injection demo",
    )
    commit_sha = run_git(destination, "rev-parse", "HEAD")

    # This file intentionally stays untracked to demonstrate that git archive excludes it.
    (destination / ".env").write_text("DEMO_ONLY_TOKEN=not-a-real-secret\n")
    return commit_sha


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args()

    commit_sha = create_demo_repo(args.destination, args.template)
    print(json.dumps({"repository": str(args.destination.resolve()), "commit_sha": commit_sha}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
