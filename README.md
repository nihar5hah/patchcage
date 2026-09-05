# PatchCage

Least-privilege harness for **test-verified** vulnerability remediation.

PatchCage lets an untrusted model work on a finding inside a locked Docker
sandbox. The host — not the model — owns policy, verification, and export.
Patches only leave the cage after compile, scanner, security-oracle, and unit
checks pass on a **clean replay** of the snapshot, and a human approves.

This repository is the Python engine plus `patchcage-engine`, the JSON-lines
subprocess CLI the TypeScript agent under `cli/` drives. You can also call the
harness from Python (or pytest).

The default interactive agent is **unsandboxed** and can execute local commands.
The isolation guarantees below apply only to `/sandbox` / `patchcage-engine`,
not ordinary agent conversations. Its acknowledgement is stored globally;
repository settings cannot acknowledge that risk on your behalf.

## What it does

1. **Snapshot** the target git repo (`git archive` of `HEAD`; untracked files
   such as `.env` never enter the cage).
2. **Sandbox** the work: no network, uid 1000, read-only root filesystem,
   writable `/workspace` only, MCP tools for inspect/edit.
3. **Steer the model** through a host-owned loop. The model proposes one
   action per turn (tool call or patch), with tool argument schemas. `propose_patch` is **not**
   a model-visible tool — patches arrive as envelope actions so the host
   always walks confirmation → apply → host checks → clean replay.
4. **Verify independently.** Host check order is compile → scanner →
   security oracle → unit. Expectations come from the project manifest.
   Each check gets a fresh container with a read-only workspace. Candidate
   code runs unprivileged, separately from the MCP editor and trusted HTTP
   oracle. Output floods and timeouts terminate the check container.
5. **Export only after approval.** A passing `run` stops at `awaiting_approval`
   and never writes `final.patch`. Export is a second invocation:

   ```bash
   patchcage-engine run --repo <path> --manifest <path> --finding <file> \
     --model-endpoint <url> --model-id <id> [--run-dir <dir>]
   patchcage-engine export --run <dir> --out <dir>
   ```

   `--out` must not exist; it receives `final.patch` and `evidence.json` atomically.
   The TUI shows the candidate in `less` before asking for approval (default:
   discard), and pins export to the reviewed SHA-256. Direct callers can use
   `export --expected-sha256 <digest>`. Evidence records the model, finding,
   manifest, source/archive and runtime image digests. Export rejects changed
   evidence or patches; older bundles without these hashes must be rerun.
   `--finding` is Finding YAML/JSON, not free text. `--scripted` replays a
   list of actions (tests/demos) and makes the model flags optional.
   Live model calls send `Authorization: Bearer` from `PATCHCAGE_MODEL_API_KEY`
   when that env var is set (llama.cpp-style endpoints can omit it).
   Events are JSON lines on stdout; diagnostics go to stderr.

   `python -m patchcage.engine_cli` is the same entry as `patchcage-engine`.

## Requirements

- **One-liner / `scripts/install.sh`:** `curl`, `git`. `uv` is installed if
  missing and fetches Python 3.12. Node 22.19+ is optional (agent TUI).
  Docker is **not** required to install or run agent mode.
- **Dev / tests / live `/sandbox`:** Python 3.12+, Docker daemon, Git; `less` for TUI approval.

## Install

One-liner (engine always; agent when Node ≥22.19 is on PATH — **no Docker**):

```bash
curl -fsSL https://raw.githubusercontent.com/nihar5hah/patchcage/main/scripts/install.sh | bash
```

From a checkout of this tree (installs that revision):

```bash
bash scripts/install.sh
```

Puts `patchcage-engine` in `~/.local/bin` via `uv tool install`. If Node is
new enough, builds `cli/` and symlinks `patchcage` there too (`patchcage`
points at that source tree — keep it). Overrides: `PATCHCAGE_REF`,
`PATCHCAGE_SRC`, `PATCHCAGE_BIN`, `PATCHCAGE_SKIP_AGENT=1`.
Self-check (stubbed, offline): `bash scripts/test_install.sh`.

Dev editable install (engine + tests):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -c requirements.lock -e ".[dev,demo]"
```

Build the demo runtime image (semgrep, oracle, workspace MCP, Flask) — only
needed for live `/sandbox` / Docker tests:

```bash
python scripts/build_runtime_image.py
```

That tags `patchcage/python-flask-demo:local` from
`runtime/python-demo/Dockerfile`. The sandbox **never pulls**; it resolves
the local tag to a digest and fails if the image is missing.
Engine runtime dependencies use `requirements.lock`; agent installation uses
`npm ci`. The image base, OS packages and development tooling are not fully
locked, so this is not a bit-for-bit reproducible build.

## Verification limits

The supported security demonstration is the packaged Flask SQL-injection
finding. Its oracle checks randomized injection cases and legitimate searches;
the scanner follows query data through intermediate variables. This is stronger
than matching two known payloads, but is not a general proof of security.
Unit tests that import candidate code are regression signals, not an independent
adversarial judge. Manifests and runtime images remain trusted inputs. Docker
is the isolation boundary, not a defense against kernel/container escapes.

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
`demo_projects/flask_sql_injection` as a throwaway git repo. The helper also
copies the packaged finding + manifest into that repo's `manifests/`.

## Demo finding

The packaged demo is a Flask search endpoint that interpolates user input
into SQL (`manifests/flask_sql_injection.yml`). `scripts/create_demo_repo.py
<dir>` copies that finding + manifest into `<dir>/manifests/` so `/sandbox`
auto-picks them after `cd <dir>`. Run `/sandbox` from that materialized git
root, not this checkout.

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
scripts/                  install.sh, image build, demo git repo helper
cli/                      vendored pi fork (TypeScript agent, bin name patchcage)
docs/roadmap.md           phase status
tests/
```

Python conventions: frozen pydantic `StrictModel`, `from __future__ import
annotations`, 100-char ruff, pytest-asyncio auto.

### `.patchcage/` namespaces

Same name, three different owners. Do not merge or rename them for Phase 5
(the agent still uses pi's `configDir`; see `cli/UPSTREAM.md`).

| Path | Owner |
| --- | --- |
| `~/.patchcage/agent/` (or `PATCHCAGE_CODING_AGENT_DIR`) | Agent secrets and sessions (`auth.json`) |
| `<cwd>/.patchcage/runs/` | Engine evidence (`patchcage-engine` default `--run-dir`) |
| `<cwd>/.patchcage/settings.json` | Agent **project** config if you run the TUI in that repo |
| `/workspace/.patchcage/` **inside the sandbox** | Harness control dir; snapshot-blocked, not on the host |

The repo `.gitignore` ignores host `.patchcage/` wholesale. Engine cleanup
removes labeled Docker resources; it does not `rm -rf` the host directory.

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

**0.1.0** — Phases 1–7. Engine library and `patchcage-engine` CLI: a run that
passes verification exits at `awaiting_approval`; `export --run <dir> --out
<dir>` writes the evidence bundle and `final.patch`. The TypeScript agent CLI
is a vendored pi fork under `cli/`. Install both with `scripts/install.sh`
(see Install above), or after `cd cli && npm install --ignore-scripts && npm
run build`, invoke `node packages/coding-agent/dist/bundle/cli.js` (npm bin
name `patchcage`). The standalone `dist/patchcage` binary is only from
`build:binary`, not the default build. Agent mode is unsandboxed; first-run
disclosure, model presets, the defensive prompt library, and `/sandbox` (from
the **target** git root, spawns `patchcage-engine`, export gated on approval)
are in the CLI ([docs/agent-mode.md](docs/agent-mode.md)). Roadmap:
[docs/roadmap.md](docs/roadmap.md).

A localhost OpenAI-compatible URL is not enough to prove weights stay on the
machine. Ollama tags ending in `:cloud` proxy to ollama.com. To smoke a
true-local model: `ollama list` (or `curl -s http://127.0.0.1:11434/v1/models`)
and pick a tag **without** `:cloud`, or use llama.cpp on `:8080`. Register it
in `~/.patchcage/agent/models.json` (same shape as
`cli/packages/coding-agent/docs/models.md`; that vendor doc still says
`~/.pi`):

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://localhost:11434/v1",
      "api": "openai-completions",
      "apiKey": "ollama",
      "models": [{ "id": "<local-tag>" }]
    }
  }
}
```

The CLI has no `--base-url`. Then:

```bash
cd cli
node packages/coding-agent/dist/bundle/cli.js --print --no-tools --no-session \
  --offline --provider ollama --model '<local-tag>' --api-key dummy \
  'Reply with the single word pong.'
```

Do not `ollama pull` a large model just to tick this box. This checkout's
Ollama has only `:cloud` tags, so that smoke is not re-run here.
