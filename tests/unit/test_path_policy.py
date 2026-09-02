import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patchcage.domain import ScopeSpec
from patchcage.policy import AccessMode, PathPolicy, PolicyViolation


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text("print('ok')\n")
    (root / "tests" / "test_app.py").write_text("def test_ok(): pass\n")
    (root / ".env").write_text("SECRET=demo\n")
    return root


@pytest.fixture
def policy(workspace: Path) -> PathPolicy:
    return PathPolicy(
        workspace,
        ScopeSpec(
            readable=("src/**", "tests/**", ".env*"),
            writable=("src/**",),
            blocked=(".env*", ".git/**"),
        ),
    )


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("", "DENY_EMPTY_PATH"),
        ("/etc/passwd", "DENY_ABSOLUTE_PATH"),
        ("src/../tests/test_app.py", "DENY_PATH_TRAVERSAL"),
        ("src\\app.py", "DENY_BACKSLASH_PATH"),
        ("src/\x00app.py", "DENY_NUL_PATH"),
    ],
)
def test_malformed_paths_are_rejected(policy: PathPolicy, path: str, code: str) -> None:
    with pytest.raises(PolicyViolation) as raised:
        policy.resolve_existing(path, AccessMode.READ)

    assert raised.value.code == code


def test_read_and_write_scopes_are_independent(policy: PathPolicy) -> None:
    assert policy.resolve_file("src/app.py", AccessMode.WRITE).name == "app.py"
    assert policy.resolve_file("tests/test_app.py", AccessMode.READ).name == "test_app.py"

    with pytest.raises(PolicyViolation) as raised:
        policy.resolve_file("tests/test_app.py", AccessMode.WRITE)
    assert raised.value.code == "DENY_UNWRITABLE_PATH"


def test_blocked_rule_wins_over_readable_rule(policy: PathPolicy) -> None:
    with pytest.raises(PolicyViolation) as raised:
        policy.resolve_file(".env", AccessMode.READ)

    assert raised.value.code == "DENY_BLOCKED_FILE"


def test_symlink_component_is_rejected(policy: PathPolicy, workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret = True\n")
    (workspace / "src" / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PolicyViolation) as raised:
        policy.resolve_file("src/linked/secret.py", AccessMode.READ)

    assert raised.value.code == "DENY_SYMLINK"


def test_special_file_is_rejected(policy: PathPolicy, workspace: Path) -> None:
    fifo = workspace / "src" / "events"
    os.mkfifo(fifo)

    with pytest.raises(PolicyViolation) as raised:
        policy.resolve_existing("src/events", AccessMode.READ)

    assert raised.value.code == "DENY_SPECIAL_FILE"


@pytest.mark.parametrize("path", [".env", ".ENV.local", "src/tls.PEM", "src/server.KEY"])
def test_secret_files_are_blocked_case_insensitively(policy: PathPolicy, path: str) -> None:
    with pytest.raises(PolicyViolation) as raised:
        policy.resolve_existing(path, AccessMode.READ)

    assert raised.value.code == "DENY_BLOCKED_FILE"


def test_control_dir_is_blocked_even_when_manifest_omits_it(workspace: Path) -> None:
    (workspace / ".patchcage").mkdir()
    (workspace / ".patchcage" / "state.json").write_text("{}\n")
    open_policy = PathPolicy(
        workspace,
        ScopeSpec(readable=("**",), writable=("src/**",), blocked=()),
    )

    with pytest.raises(PolicyViolation) as raised:
        open_policy.resolve_existing(".patchcage/state.json", AccessMode.READ)
    assert raised.value.code == "DENY_BLOCKED_FILE"


def test_directory_ancestors_of_readable_content_are_listable(policy: PathPolicy) -> None:
    assert policy.resolve_existing("src", AccessMode.READ).is_dir()
    assert policy.resolve_existing(".", AccessMode.READ).is_dir()

    with pytest.raises(PolicyViolation) as raised:
        policy.resolve_existing(".", AccessMode.WRITE)
    assert raised.value.code == "DENY_UNWRITABLE_PATH"


def test_unrelated_directory_is_not_listable(policy: PathPolicy, workspace: Path) -> None:
    (workspace / "docs").mkdir()

    with pytest.raises(PolicyViolation) as raised:
        policy.resolve_existing("docs", AccessMode.READ)

    assert raised.value.code == "DENY_UNREADABLE_PATH"


@given(segment=st.text(alphabet=st.characters(whitelist_categories=("L", "N")), min_size=1))
def test_parent_traversal_is_always_rejected_before_filesystem_access(segment: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "src").mkdir()
        path_policy = PathPolicy(
            root,
            ScopeSpec(readable=("src/**",), writable=("src/**",)),
        )
        with pytest.raises(PolicyViolation) as raised:
            path_policy.resolve_existing(f"src/{segment}/../app.py", AccessMode.READ)

    assert raised.value.code == "DENY_PATH_TRAVERSAL"
