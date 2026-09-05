from __future__ import annotations

import contextlib
import io
import json
import tarfile
import uuid
from dataclasses import dataclass

import docker
from docker.errors import APIError, ImageNotFound
from docker.models.containers import Container

from patchcage.domain import Finding, ProjectManifest
from patchcage.sandbox_env import (
    HOME_DIR,
    IMAGE_SEMGREP_SETTINGS,
    SANDBOX_ENV,
    SEMGREP_SETTINGS_FILE,
    WORKSPACE,
)
from patchcage.snapshot import SnapshotArtifact

LABEL_MANAGED = "patchcage.managed"
LABEL_RUN = "patchcage.run_id"
LABEL_ROLE = "patchcage.role"
RUNTIME_USER = "1000:1000"
CONTROL_DIR = f"{WORKSPACE}/.patchcage"
MEMORY_LIMIT = "512m"
NANO_CPUS = 1_000_000_000
PIDS_LIMIT = 128


class SandboxError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Sandbox:
    run_id: str
    image_id: str
    volume_name: str
    container_id: str
    baseline_sha: str
    workdir: str = WORKSPACE


def _labels(run_id: str, role: str) -> dict[str, str]:
    return {LABEL_MANAGED: "true", LABEL_RUN: run_id, LABEL_ROLE: role}


def _tar_regular_file(name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = 0o644
        info.uid = 1000
        info.gid = 1000
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class DockerRuntime:
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self.client = client or docker.from_env()

    def resolve_image(self, image: str) -> str:
        if image.endswith(":unresolved"):
            raise SandboxError(
                "IMAGE_UNRESOLVED",
                "sandbox image is unresolved; build the runtime image first",
            )
        try:
            resolved = self.client.images.get(image)
        except ImageNotFound as error:
            raise SandboxError(
                "IMAGE_NOT_LOCAL",
                f"image is not present locally and runs do not pull: {image}",
            ) from error
        image_id = str(resolved.id)
        if not image_id.startswith("sha256:"):
            raise SandboxError("IMAGE_UNPINNED", f"local image has no digest: {image}")
        return image_id

    def create_work_sandbox(
        self,
        *,
        image: str,
        snapshot: SnapshotArtifact,
        manifest: ProjectManifest,
        finding: Finding | None = None,
        run_id: str | None = None,
    ) -> Sandbox:
        if manifest.runtime.image_is_unresolved:
            raise SandboxError("IMAGE_UNRESOLVED", "refusing to start an unresolved image")
        image_id = self.resolve_image(image)
        run_id = run_id or uuid.uuid4().hex
        volume_name = f"patchcage-{run_id}-workspace"
        self.client.volumes.create(name=volume_name, labels=_labels(run_id, "workspace"))
        seed: Container | None = None
        work: Container | None = None
        try:
            seed = self._run_container(
                image_id=image_id,
                run_id=run_id,
                role="seed",
                volume_name=volume_name,
                user="0:0",
                read_only=False,
                extra_tmpfs={},
                drop_capabilities=False,
            )
            self._seed_volume(seed, snapshot, manifest, finding)
            baseline_sha = self._require_exec(
                seed, ["git", "rev-parse", "HEAD"], user=RUNTIME_USER
            ).strip()
            work = self._run_container(
                image_id=image_id,
                run_id=run_id,
                role="work",
                volume_name=volume_name,
                user=RUNTIME_USER,
                read_only=True,
                extra_tmpfs={HOME_DIR: "rw,nosuid,nodev,size=32m,uid=1000,gid=1000,mode=755"},
                drop_capabilities=True,
            )
            self._stage_home_settings(work)
            return Sandbox(
                run_id=run_id,
                image_id=image_id,
                volume_name=volume_name,
                container_id=str(work.id),
                baseline_sha=baseline_sha,
            )
        except Exception:
            if work is not None:
                with contextlib.suppress(APIError):
                    work.remove(force=True)
            with contextlib.suppress(Exception):
                self.cleanup_run(run_id)
            raise
        finally:
            if seed is not None:
                with contextlib.suppress(APIError):
                    seed.remove(force=True)

    def cleanup(self, sandbox: Sandbox) -> None:
        self.cleanup_run(sandbox.run_id)

    def create_check_container(self, sandbox: Sandbox) -> Container:
        """A fresh PID namespace and read-only candidate volume for each check."""
        container = self._run_container(
            image_id=sandbox.image_id,
            run_id=sandbox.run_id,
            role="check",
            volume_name=sandbox.volume_name,
            user="0:0",
            read_only=True,
            extra_tmpfs={HOME_DIR: "rw,nosuid,nodev,size=32m,mode=755"},
            drop_capabilities=True,
            verification=True,
        )
        try:
            self._stage_home_settings(container, user="0:0")
        except BaseException:
            container.remove(force=True)
            raise
        return container

    def cleanup_run(self, run_id: str) -> None:
        filters = {"label": f"{LABEL_RUN}={run_id}"}
        for container in self.client.containers.list(all=True, filters=filters):
            with contextlib.suppress(APIError):
                container.remove(force=True)
        for volume in self.client.volumes.list(filters=filters):
            with contextlib.suppress(APIError):
                volume.remove(force=True)

    def _run_container(
        self,
        *,
        image_id: str,
        run_id: str,
        role: str,
        volume_name: str,
        user: str,
        read_only: bool,
        extra_tmpfs: dict[str, str],
        drop_capabilities: bool,
        verification: bool = False,
    ) -> Container:
        try:
            return self.client.containers.run(
                image_id,
                command=["sleep", "infinity"],
                detach=True,
                user=user,
                network_mode="none",
                read_only=read_only,
                cap_drop=["ALL"] if drop_capabilities else None,
                cap_add=["SETUID", "SETGID", "KILL"] if verification else None,
                security_opt=["no-new-privileges:true"] if drop_capabilities else None,
                mem_limit=MEMORY_LIMIT,
                nano_cpus=NANO_CPUS,
                pids_limit=PIDS_LIMIT,
                tmpfs={"/tmp": "rw,nosuid,nodev,size=64m", **extra_tmpfs},
                volumes={volume_name: {"bind": WORKSPACE, "mode": "ro" if verification else "rw"}},
                working_dir=WORKSPACE,
                environment=SANDBOX_ENV,
                labels=_labels(run_id, role),
            )
        except APIError as error:
            raise SandboxError("CONTAINER_CREATE_FAILED", str(error)) from error

    def _seed_volume(
        self,
        seed: Container,
        snapshot: SnapshotArtifact,
        manifest: ProjectManifest,
        finding: Finding | None,
    ) -> None:
        if not seed.put_archive(WORKSPACE, snapshot.archive):
            raise SandboxError("ARCHIVE_UPLOAD_FAILED", "docker put_archive returned false")
        self._require_exec(seed, ["chown", "-R", RUNTIME_USER, WORKSPACE], user="0:0")
        self._require_exec(seed, ["git", "init", "-q", "-b", "main"], user=RUNTIME_USER)
        self._require_exec(seed, ["git", "add", "-A"], user=RUNTIME_USER)
        self._require_exec(
            seed,
            [
                "git",
                "-c",
                "user.name=PatchCage",
                "-c",
                "user.email=sandbox@patchcage.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "baseline",
            ],
            user=RUNTIME_USER,
        )
        baseline_sha = self._require_exec(
            seed, ["git", "rev-parse", "HEAD"], user=RUNTIME_USER
        ).strip()
        state = {
            "manifest": json.loads(manifest.model_dump_json(by_alias=True)),
            "finding": None if finding is None else json.loads(finding.model_dump_json()),
            "baseline_sha": baseline_sha,
        }
        self._require_exec(seed, ["mkdir", "-p", CONTROL_DIR], user=RUNTIME_USER)
        seed.put_archive(CONTROL_DIR, _tar_regular_file("state.json", json.dumps(state).encode()))
        self._require_exec(seed, ["chown", "-R", RUNTIME_USER, CONTROL_DIR], user="0:0")

    def _stage_home_settings(self, work: Container, *, user: str = RUNTIME_USER) -> None:
        # Semgrep mkstemps next to SEMGREP_SETTINGS_FILE; /opt is read-only.
        self._require_exec(work, ["mkdir", "-p", f"{HOME_DIR}/.semgrep"], user=user)
        self._require_exec(
            work,
            ["cp", IMAGE_SEMGREP_SETTINGS, SEMGREP_SETTINGS_FILE],
            user=user,
        )

    def _require_exec(self, container: Container, argv: list[str], *, user: str) -> str:
        result = container.exec_run(
            list(argv),
            user=user,
            workdir=WORKSPACE,
            environment=SANDBOX_ENV,
        )
        output = result.output if isinstance(result.output, bytes) else b""
        if result.exit_code != 0:
            detail = output.decode("utf-8", errors="replace")
            raise SandboxError("SANDBOX_EXEC_FAILED", f"{argv[0]} failed: {detail[:2_000]}")
        return output.decode("utf-8", errors="replace")
