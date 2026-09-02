from __future__ import annotations

from pathlib import Path

import docker

PROJECT_ROOT = Path(__file__).resolve().parents[3]
IMAGE_TAG = "patchcage/python-flask-demo:local"
DOCKERFILE = "runtime/python-demo/Dockerfile"


def build_runtime_image(client: docker.DockerClient | None = None) -> str:
    docker_client = client or docker.from_env()
    image, _logs = docker_client.images.build(
        path=str(PROJECT_ROOT),
        dockerfile=DOCKERFILE,
        tag=IMAGE_TAG,
        rm=True,
        pull=False,
    )
    image_id = str(image.id)
    if not image_id.startswith("sha256:"):
        raise RuntimeError(f"built image has no digest: {image_id}")
    return image_id
