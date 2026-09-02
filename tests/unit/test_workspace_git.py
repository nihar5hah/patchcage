from __future__ import annotations

import subprocess
from pathlib import Path

from patchcage_workspace.gitutil import (
    is_status_noise,
    porcelain_path,
    working_tree_diff,
)


def _commit(repository: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=PatchCage Test",
            "-c",
            "user.email=test@patchcage.invalid",
            "commit",
            "-q",
            "-m",
            "baseline",
        ],
        cwd=repository,
        check=True,
    )


def test_status_noise_covers_control_and_bytecode() -> None:
    assert is_status_noise(porcelain_path("?? .patchcage/"))
    assert is_status_noise(porcelain_path("?? src/demo_app/__pycache__/x.pyc"))
    assert is_status_noise(porcelain_path("?? .pytest_cache/v/cache/nodeids"))
    assert not is_status_noise(porcelain_path(" M src/demo_app/search.py"))
    assert not is_status_noise(porcelain_path("?? src/extra.py"))


def test_working_tree_diff_includes_new_files_and_hides_control_dir(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "app.py").write_text("x = 1\n")
    _commit(repository)
    (repository / ".patchcage").mkdir()
    (repository / ".patchcage" / "state.json").write_text("{}\n")
    (repository / "src" / "extra.py").write_text("y = 2\n")

    diff = working_tree_diff(repository)

    assert "extra.py" in diff
    assert ".patchcage" not in diff
