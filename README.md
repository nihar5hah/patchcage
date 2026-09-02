# PatchCage

Least-privilege harness for **test-verified** vulnerability remediation.

PatchCage lets an untrusted model work on a finding inside a locked Docker
sandbox. The host — not the model — owns policy, verification, and export.
Patches only leave the cage after compile, scanner, security-oracle, and unit
checks pass on a **clean replay** of the snapshot, and a human approves.

This repository is the Python engine (Phases 1–2). There is no `patchcage`
CLI yet. You drive the harness from Python (or pytest). A subprocess CLI and
a TypeScript agent wrapper are planned separately.

## What it does

1. **Snapshot** the target git repo (`git archive` of `HEAD`; untracked files
   such as `.env` never enter the cage).
2. **Sandbox** the work: no network, uid 1000, read-only root filesystem,
   writable `/workspace` only, MCP tools for inspect/edit.
3. **Steer the model** through a host-owned loop. The model proposes one
   action per turn (tool call, patch, or complete). `propose_patch` is **not**
   a model-visible tool — patches arrive as envelope actions so the host
   always walks confirmation → apply → host checks → clean replay.
4. **Verify independently.** Host check order is compile → scanner →
   security oracle → unit. Expectations come from the project manifest.
5. **Export only after approval.** A passing run stops at `awaiting_approval`.
   Persisting `final.patch` is a separate step.

Steering comments are advisory. Verification cannot be skipped.

## Requirements

- Python 3.12+
- Docker daemon (integration tests and live sandbox runs)
- Git (snapshots and the Flask demo fixture)

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Build the demo runtime image (semgrep, oracle, workspace MCP, Flask):

```bash
python scripts/build_runtime_image.py
```

That tags `patchcage/python-flask-demo:local` from
`runtime/python-demo/Dockerfile`. The sandbox **never pulls**; it resolves
the local tag to a digest and fails if the image is missing.

## Tests

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy
```

Unit tests do not need Docker. Tests marked `docker` skip when the daemon is
down. They build (or reuse) the local runtime image and use
`scripts/create_demo_repo.py` to materialize
`demo_projects/flask_sql_injection` as a throwaway git repo.

## Demo finding

The packaged demo is a Flask search endpoint that interpolates user input
into SQL (`manifests/flask_sql_injection.yml`).

| Piece | Path |
| --- | --- |
| Vulnerable app | `demo_projects/flask_sql_injection/` |
| Manifest (scope, checks, budgets) | `manifests/flask_sql_injection.yml` |
| Semgrep rule | `runtime/python-demo/rules/` |
| Hidden oracle | `runtime/python-demo/oracles/` |
| Reference fix (tests only) | `tests/fixtures/sql_injection_fix.patch` |

The demo README contains an intentional prompt-injection fixture. Treat
repository content as untrusted; it must never override PatchCage policy.

## Layout

```
src/patchcage/            host engine (snapshot, sandbox, policy, harness, gateway)
src/patchcage_workspace/  in-container MCP server
runtime/python-demo/      Docker image, rules, oracles, check runners
manifests/                project manifests
demo_projects/            authorized vulnerable fixtures
scripts/                  image build + demo git repo helper
tests/
```

Python conventions: frozen pydantic `StrictModel`, `from __future__ import
annotations`, 100-char ruff, pytest-asyncio auto.

## Security model (short)

- Model traffic stays on the host gateway (`httpx` to an OpenAI-compatible
  endpoint, or `ScriptedGateway` in tests). The container has no network.
- Path and patch policy run on the host before any write is forwarded.
- Check binaries and the security oracle live in the image under
  `/opt/patchcage`; the model cannot rewrite them.
- Identical failed actions and size/file budgets are enforced by the
  loop supervisor, not by the model.

Do not use this against systems you do not own or are not authorized to test.

## Status

**0.1.0** — engine library. Honest MVP: a run that passes verification
exits at `awaiting_approval`; writing the evidence bundle / `final.patch`
is a second host action. No `patchcage-engine` console script yet.
