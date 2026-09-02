# PatchCage CLI — Implementation Plan

Spec: `docs/superpowers/specs/2026-09-02-patchcage-cli-two-mode-design.md`
Engine reference: `docs/superpowers/specs/2026-08-27-patchcage-aws-first-design.md`

Build order is **engine-first** (spec §8): prove the model-driven Python engine
before the TypeScript CLI integration.

---

## Phase 1 — AgentContext + Python ModelGateway

Goal: a real model can drive the sandbox investigation (replacing the spike's
scripted model).

Files: `src/patchcage/harness/context.py`, `src/patchcage/gateway/__init__.py`,
`src/patchcage/gateway/base.py`, `src/patchcage/gateway/openai_compat.py`,
`src/patchcage/gateway/scripted.py`, `tests/unit/test_context.py`,
`tests/unit/test_gateway.py`.

- **`AgentContext` first.** The bounded, typed view the model sees: the
  finding, current phase, last tool result, latest check results, candidate
  patch hash, budget snapshot, and a capped tail of recent actions — never
  the unbounded transcript. The check results are what make the REPAIRING
  loop useful: the model sees which check failed and why. `harness/context.py`
  is the single builder; the gateway never assembles context itself.
- `ModelGateway` Protocol (spec §6.2 of the AWS doc): `health()`,
  `next_action(context: AgentContext) -> AgentAction`.
- `OpenAICompatGateway`: httpx POST to `{base_url}/chat/completions`; strict
  JSON-envelope parse into `AGENT_ACTION_ADAPTER`; one formatting retry;
  timeout + ~1200-token cap; key from `PATCHCAGE_MODEL_API_KEY`, redacted from
  errors/logs. Works for Ollama / llama.cpp / vLLM / hosted.
- `ScriptedGateway`: replays a canned list of `AgentAction`s for tests.
- Tests: context builder (bounds, finding/phase/budget contents), schema
  parse, malformed-then-retry, timeout, key redaction, scripted replay. Mock
  httpx; no live endpoint in CI.

## Phase 2 — Harness runner

Goal: the host-owned loop that ties model ↔ policy ↔ MCP ↔ verification.

Files: `src/patchcage/harness/runner.py`, `tests/unit/test_runner.py`,
`tests/integration/test_engine_pipeline.py`.

- Drives the existing state machine exactly as defined in
  `harness/state_machine.py`: CREATED → PREFLIGHT_VALIDATED → SNAPSHOT_READY →
  BASELINE_VERIFIED → INVESTIGATING → FINDING_CONFIRMED → PATCH_ENABLED →
  PATCH_VALIDATING → WORKING_VERIFICATION → CLEAN_REPLAY_VERIFIED →
  AWAITING_APPROVAL — including the repair loop WORKING_VERIFICATION →
  REPAIRING → PATCH_VALIDATING, bounded by `repair_cycles`.
- Investigation loop: build bounded `AgentContext` → `gateway.next_action` →
  policy gate → MCP call in the sandbox → record result → repeat.
- Finding confirmation is host-side evidence, not a model claim: the baseline
  security check must reproduce the vulnerability (`vulnerability_reproduced`
  + required marker) before investigation begins. A `PatchAction` then walks
  INVESTIGATING → FINDING_CONFIRMED → PATCH_ENABLED → PATCH_VALIDATING; these
  waypoints are host-controlled and the model cannot skip or self-declare
  them.
- Plug in existing `BudgetTracker` and `LoopSupervisor`.
- Host re-validates every patch with `inspect_patch` before forwarding to MCP
  (defense in depth; the MCP server also validates).
- Verification ladder: compile → scanner → security (host-only) → unit, then
  clean replay in a fresh container. Derive the verdict from check outcomes,
  never from a model success field.
- Tests: scripted model sequences (success, malformed-then-retry, policy
  violation, patch-then-verify, repair loop after failed verification,
  oscillation, premature completion rejected).

## Phase 3 — `patchcage-engine` console script

Goal: the engine as a subprocess the TS CLI can drive.

Files: `src/patchcage/engine_cli.py`, `pyproject.toml` (`[project.scripts]`),
`tests/integration/test_engine_cli.py`.

- argparse: `run --repo --manifest --finding <file> --model-endpoint
  --model-id [--instructions <text>]`, plus `--scripted` for tests. The
  manifest is required — it names the pinned image, scope, and check ladder;
  there is no manifest-less "any repo" mode at MVP. `--finding` is a
  JSON/YAML file matching the `Finding` model (free text cannot populate
  `severity`, `file_path`, `verification_recipe`); the demo ships
  `manifests/flask_sql_injection.finding.yml`. SARIF ingestion is deferred
  (no parser exists).
- `export --run <dir> --out <path>`: a second invocation after user approval
  that writes `final.patch` + evidence bundle. `run` exits at
  `awaiting_approval` and never exports on its own. `export` refuses unless
  the persisted run state is `awaiting_approval` and the stored diff hash
  matches the artifact on disk — a tampered or half-written run directory
  fails closed.
- Emits JSON-lines events (phase, model action, check result) on stdout;
  final result object (status, run dir, diff ref, evidence path, per-check
  outcomes).
- SIGTERM/SIGINT → trap, tear down labeled containers/volumes, exit distinct
  code.
- Writes the evidence bundle to a run directory.
- Tests: JSON-lines protocol shape, cancellation cleanup, export gating (no
  export without an explicit `export` call), scripted end-to-end.

## Phase 4 — Fork pi → `patchcage` CLI

Goal: the conversational TypeScript shell, rebranded.

- Pin a pi version by commit SHA; vendor/fork into `cli/`; strip to agent
  loop + TUI + provider layer (`pi-agent-core`, `pi-tui`, `pi-ai`). Retain
  pi's MIT license notice in the fork.
- Rebrand to `patchcage`; confirm it builds and runs a local model.
- Keep upstream-tracking notes; upgrade deliberately.

## Phase 5 — Agent mode

Goal: the default, security-scoped agent experience.

- Model presets (Ollama / llama.cpp / vLLM / OpenAI-compatible) over `pi-ai`;
  env-var API key.
- Curated security prompt/task library (defensive) + suggested-prompt
  onboarding.
- First-run safety disclosure (agent mode is unsandboxed; `/sandbox` is the
  contained mode).

## Phase 6 — `/sandbox` mode in the TUI

Goal: the engine surfaced as a mode.

- Slash-command `/sandbox`; spawn `patchcage-engine`, render JSON-lines events
  as narration + verification checklist, gate export on approval.
- Docker detection + clear "daemon down" / "install Docker" guidance.

## Phase 7 — One-command installer

Goal: `curl … | bash` (or `npm i -g patchcage`) installs both halves.

- **Packaging spike first:** PyInstaller binary vs. `uv`-managed environment —
  build both against the real engine, pick one, delete the loser from this
  plan.
- CLI via npm/bun; engine per the spike outcome.
- Docker check/guide. Pull the pre-built sandbox image only when a daemon is
  present; otherwise defer the pull to the first `/sandbox` run. Install must
  succeed without Docker — agent mode has no Docker dependency.

---

## Sequencing note

Phases 1–3 are the engine and are fully in our control — do them first and
prove the thesis end-to-end on the SQL-injection demo. Phases 4–7 are the CLI
and integration. Do not start Phase 4 until Phase 3 passes a scripted
end-to-end run.
