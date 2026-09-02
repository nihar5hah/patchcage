from pathlib import Path

import pytest

from patchcage.domain import RunLimits, ScopeSpec
from patchcage.policy.patches import PatchPolicyError, inspect_patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_PATCH = PROJECT_ROOT / "tests" / "fixtures" / "sql_injection_fix.patch"

SOURCE_SCOPE = ScopeSpec(
    readable=("src/**", "tests/**", "pyproject.toml"),
    writable=("src/**",),
    blocked=(".git/**", ".env*"),
)


def new_file_patch(path: str, lines: list[str], *, mode: str = "100644") -> str:
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode {mode}\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


def test_valid_source_patch_returns_bounded_metadata() -> None:
    diff = REFERENCE_PATCH.read_text()

    metadata = inspect_patch(
        diff,
        scope=SOURCE_SCOPE,
        limits=RunLimits(),
        cwd=PROJECT_ROOT,
    )

    assert [file.path for file in metadata.files] == ["src/demo_app/search.py"]
    assert metadata.added_lines == 2
    assert metadata.deleted_lines == 1
    assert metadata.byte_count == len(diff.encode())


def test_test_file_change_is_rejected_by_writable_scope() -> None:
    diff = new_file_patch("tests/test_backdoor.py", ["def test_pass():", "    assert True"])

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "UNWRITABLE_PATCH_PATH"


def test_dependency_change_is_rejected_even_with_broad_writable_scope() -> None:
    diff = new_file_patch("pyproject.toml", ["[project]", 'name = "changed"'])
    broad_scope = ScopeSpec(readable=("**",), writable=("**",), blocked=(".git/**",))

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=broad_scope, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "DEPENDENCY_CHANGE_FORBIDDEN"


def test_interpreter_startup_hooks_are_rejected() -> None:
    for path in ("src/sitecustomize.py", "src/usercustomize.py"):
        diff = new_file_patch(path, ["print('hook')"])
        with pytest.raises(PatchPolicyError) as raised:
            inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)
        assert raised.value.code == "INTERPRETER_HOOK_FORBIDDEN"


def test_requirements_variant_and_setup_py_are_rejected() -> None:
    for path in ("src/setup.py", "src/requirements-ci.txt"):
        diff = new_file_patch(path, ["x = 1"])
        with pytest.raises(PatchPolicyError) as raised:
            inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)
        assert raised.value.code == "DEPENDENCY_CHANGE_FORBIDDEN"


def test_scanner_suppression_marker_is_rejected() -> None:
    diff = new_file_patch("src/bypass.py", ["dangerous_call()  # nosemgrep"])

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "SCANNER_SUPPRESSION_FORBIDDEN"


def test_symlink_creation_is_rejected() -> None:
    diff = new_file_patch("src/link.py", ["../outside.py"], mode="120000")

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "SYMLINK_PATCH_FORBIDDEN"


def test_executable_file_creation_is_rejected() -> None:
    diff = new_file_patch("src/run.sh", ["echo hi"], mode="100755")

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "PATCH_OPERATION_FORBIDDEN"


def test_rename_is_rejected() -> None:
    diff = (
        "diff --git a/src/demo_app/search.py b/src/demo_app/renamed.py\n"
        "similarity index 100%\n"
        "rename from src/demo_app/search.py\n"
        "rename to src/demo_app/renamed.py\n"
    )

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "PATCH_OPERATION_FORBIDDEN"


def test_copy_is_rejected() -> None:
    diff = (
        "diff --git a/src/demo_app/search.py b/src/demo_app/copy.py\n"
        "similarity index 100%\n"
        "copy from src/demo_app/search.py\n"
        "copy to src/demo_app/copy.py\n"
    )

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "PATCH_OPERATION_FORBIDDEN"


def test_secret_path_in_patch_is_rejected_case_insensitively() -> None:
    diff = new_file_patch("src/tls.PEM", ["data"])

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "BLOCKED_PATCH_PATH"


def test_binary_patch_marker_is_rejected_before_git_parsing() -> None:
    diff = "diff --git a/src/image.bin b/src/image.bin\nGIT binary patch\n"

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(diff, scope=SOURCE_SCOPE, limits=RunLimits(), cwd=PROJECT_ROOT)

    assert raised.value.code == "BINARY_PATCH_FORBIDDEN"


def test_patch_size_and_line_limits_are_enforced() -> None:
    diff = new_file_patch("src/large.py", ["x = 1", "y = 2"])

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(
            diff,
            scope=SOURCE_SCOPE,
            limits=RunLimits(added_lines=1),
            cwd=PROJECT_ROOT,
        )
    assert raised.value.code == "TOO_MANY_ADDED_LINES"

    with pytest.raises(PatchPolicyError) as raised:
        inspect_patch(
            diff,
            scope=SOURCE_SCOPE,
            limits=RunLimits(patch_bytes=10),
            cwd=PROJECT_ROOT,
        )
    assert raised.value.code == "PATCH_TOO_LARGE"
