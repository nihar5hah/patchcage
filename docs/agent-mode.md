# Agent mode (Phases 5–6)

PatchCage's interactive coding agent is **unsandboxed**. It uses your user
permissions for read, write, and bash. `/sandbox` (below) is the only path
that runs inside the Docker cage; the chat agent never becomes sandboxed.

## Disclosure

- Interactive TTY: a blocking notice. Continue persists
  `unsandboxedDisclosureAcknowledged` in `~/.patchcage/agent/settings.json`.
  Decline exits 0.
- `--print`, piped stdin, RPC, and `--mode json` with tools enabled require a
  prior interactive ack, or `--ack-unsandboxed` (this run only; not saved), or
  `--no-tools`. Otherwise the CLI exits 1.
- `--help`, `--list-models`, and `--version` skip the gate.
- `--no-builtin-tools` still enables extension tools, so disclosure still
  applies.

## Models

If no selectable model exists after the same resolution `/model` uses, the
interactive CLI offers presets: Ollama (`127.0.0.1:11434`), llama.cpp
(`:8080`), vLLM (`:8000`), or a hosted OpenAI-compatible endpoint.

Presets **merge** into `~/.patchcage/agent/models.json`. A malformed file is
not overwritten. Hosted keys are stored as `$ENV_NAME`, never as a pasted
secret. Skip for now sets `modelSetupSkipped`; `/setup-model` can run later
(Skip there is not persisted).

Localhost probes only hit those three loopback URLs, GET `/models`, ~1s
timeout. `localhost` hostnames are not probed.

## Prompts

Bundled slash templates: `/review-vuln`, `/explain-finding`, `/analyze-sample`,
`/harden`, `/poc`. User, project, and `--prompt-template` paths win over
bundled. `--no-prompt-templates` skips bundled templates. A short PatchCage
system-prompt appendix is always appended.

## `/sandbox [finding.yml]` (Phase 6)

Spawns `patchcage-engine run` as a subprocess and narrates its JSON-lines
events (`phase`, `check_result`, `result`) as status lines. The model cannot
invoke it; only the user can.

- Run from the git root. Finding is YAML/JSON, not chat text. The manifest is
  the sibling without `.finding` (`X.finding.yml` → `X.yml`). With no argument,
  exactly one `manifests/*.finding.yml` must exist; otherwise usage error.
- Engine binary: `PATCHCAGE_ENGINE` if set, else `patchcage-engine` on PATH.
  Missing binary is an error, never a `python -m` fallback.
- Model: the current session's OpenAI-compatible `baseUrl` / `id`. The API key
  reaches the child only as `PATCHCAGE_MODEL_API_KEY` and is never echoed.
- Run dir: `<repo>/.patchcage/runs/<id>`. Esc sends SIGINT; the engine reports
  `cancelled`.
- `awaiting_approval` → **Export** / **Discard**. Export runs
  `patchcage-engine export --run <dir> --out <repo>/.patchcage/exports/<id>`.
  Discard writes nothing and keeps the run dir. `final.patch` is never written
  without that approval.
- Docker down: the engine's `docker daemon unavailable` stderr is shown with a
  hint. The unsandboxed agent does **not** take over the task.
