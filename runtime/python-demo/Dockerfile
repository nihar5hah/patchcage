FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --user-group --shell /usr/sbin/nologin patchcage \
    && mkdir -p /workspace /opt/patchcage /opt/venvs /opt/src/patchcage \
    && chown patchcage:patchcage /workspace

# Semgrep pins MCP 1.x; keep it out of the workspace server's environment.
RUN python -m venv /opt/venvs/semgrep \
    && /opt/venvs/semgrep/bin/pip install --no-cache-dir 'semgrep==1.175.0' \
    && ln -s /opt/venvs/semgrep/bin/semgrep /usr/local/bin/semgrep

COPY pyproject.toml /opt/src/patchcage/pyproject.toml
COPY src /opt/src/patchcage/src
COPY runtime/python-demo/rules /opt/patchcage/rules
COPY runtime/python-demo/oracles /opt/patchcage/oracles
COPY runtime/python-demo/runners /opt/patchcage/runners
COPY runtime/python-demo/semgrep /opt/patchcage/semgrep

RUN python -m venv /opt/venvs/workspace \
    && /opt/venvs/workspace/bin/pip install --no-cache-dir \
        'flask==3.1.2' \
        'pytest==8.4.1' \
        '/opt/src/patchcage' \
    && chmod -R a+rX /opt/patchcage /opt/src/patchcage

ENV PATH="/opt/venvs/workspace/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME=/home/patchcage \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    SEMGREP_ENABLE_VERSION_CHECK=0 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

USER patchcage
WORKDIR /workspace
