# PatchCage

Least-privilege harness for **test-verified** vulnerability remediation.

PatchCage lets an untrusted model work on a finding inside a locked Docker
sandbox. The host — not the model — owns policy, verification, and export.
Patches only leave the cage after compile, scanner, security-oracle, and unit
checks pass on a **clean replay** of the snapshot, and a human approves.

This repository is the Python engine plus `patchcage-engine`, the JSON-lines
subprocess CLI the TypeScript agent under `cli/` drives. You can also call the
harness from Python (or pytest).

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
5. **Export only after approval.** A passing `run` stops at `awaiting_approval`
   and never writes `final.patch`. Export is a second invocation:

   ```bash
   patchcage-engine run --repo <path> --manifest <path> --finding <file> \
     --model-endpoint <url> --model-id <id> [--run-dir <dir>]
   patchcage-engine export --run <dir> --out <dir>
   ```

   `--out` is a directory that receives `final.patch` and `evidence.json`.
   `--finding` is Finding YAML/JSON, not free text. `--scripted` replays a
   list of actions (tests/demos) and makes the model flags optional.
   Live model calls send `Authorization: Bearer` from `PATCHCAGE_MODEL_API_KEY`
   when that env var is set (llama.cpp-style endpoints can omit it).
   Events are JSON lines on stdout; diagnostics go to stderr.

   `python -m patchcage.engine_cli` is the same entry as `patchcage-engine`.

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

Unit tests do not need Docker. Use `pytest -m "not docker"` to skip the
Docker-backed integration tests. Tests marked `docker` also skip when the
daemon is down. They build (or reuse) the local runtime image and use
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
cli/                      vendored pi fork (TypeScript agent, bin name patchcage)
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

**0.1.0** — engine library and `patchcage-engine` CLI. Honest MVP: a run that
passes verification exits at `awaiting_approval`; `export --run <dir> --out
<dir>` writes the evidence bundle and `final.patch`. The TypeScript agent CLI
is a vendored pi fork under `cli/`. After `cd cli && npm install
--ignore-scripts && npm run build`, invoke it as
`node packages/coding-agent/dist/bundle/cli.js` (npm bin name `patchcage`).
The standalone `dist/patchcage` binary is only from `build:binary`, not the
default build. First-run disclosure, `/sandbox`, and the installer are not in
this cut.
