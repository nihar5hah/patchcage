# Agent mode (Phase 5)

PatchCage's interactive coding agent is **unsandboxed**. It uses your user
permissions for read, write, and bash. `/sandbox` is Phase 6 and is not
available yet.

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
