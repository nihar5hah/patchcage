# PatchCage CLI — Two-Mode Harness for Uncensored Models

Status: Draft for user review
Date: 2026-09-02
Supersedes the product framing (not the engine) of
`2026-08-27-patchcage-aws-first-design.md`.

## 1. What PatchCage is now

PatchCage is a **security-focused CLI for getting the most out of uncensored /
open-weight models**. The user brings their own model — via a hosted
OpenAI-compatible API or a local server (Ollama, llama.cpp, vLLM) — and
PatchCage is the harness that makes that model useful for security work.

It has two modes in one CLI:

- **Agent mode (default, home).** A conversational agent tuned for security /
  defensive work, preconfigured for uncensored models. Familiar pi/opencode-style
  experience.
- **Sandbox mode (`/sandbox`).** The least-privilege engine already built: the
  model works inside a locked Docker container and the host independently
  verifies any patch before export. The mode for *trusting* an untrusted
  model's output.

One CLI, one model configuration, shared across both modes.

### Why uncensored — the reason the product exists

Hosted (censored) models refuse legitimate defensive security work. Even "find
the vulnerabilities in my own codebase" or "help me learn how this attack works"
gets refused or watered down. PatchCage is for people doing **authorized but
over-refused** security work: testing their *own* systems, learning security as
a beginner, analyzing malware/phishing they received, building a PoC against a
target they own. The uncensored model removes the refusal wall; **sandbox mode
removes the risk** of running a model you don't fully trust.

That pairing — *no refusals from the model; sandbox mode removes the risk of
trusting its output* — is the differentiator no other harness leads with.

### Scope boundary (held deliberately)

Both modes stay on the **defensive / authorized** side: code vulnerability
review, vuln analysis, testing your own systems, learning, malware/phishing
analysis, PoC against your own target. PatchCage is not an offensive platform
and does not ship curated content for attacking systems the user doesn't own.

## 2. Architecture: two languages, one seam

The CLI and the engine are different languages and stay that way.

```
┌─────────────────────────────────────────────┐
│  TypeScript CLI (forked from pi, rebranded) │
│  - conversational TUI (pi-tui)              │
│  - provider abstraction (pi-ai)             │
│  - agent loop + tools (pi-agent-core)       │
│  - curated security prompts/tasks           │
│  - onboarding (suggested prompts)           │
│  - /sandbox mode switch                     │
└──────────────────┬──────────────────────────┘
                   │ subprocess + JSON-lines events
                   ▼
┌─────────────────────────────────────────────┐
│  Python engine (existing, unchanged core)   │
│  patchcage-engine run …                     │
│  - git snapshot service                     │
│  - Docker sandbox (no net, uid 1000, RO fs) │
│  - path/patch policy                        │
│  - MCP workspace server                     │
│  - hidden security oracle                   │
│  - verification ladder + clean replay       │
│  - minimal ModelGateway (httpx)             │
│  - evidence bundle                          │
└─────────────────────────────────────────────┘
```

- **The CLI is TypeScript, forked from pi and rebranded to `patchcage`.** The
  user types `patchcage`, gets PatchCage's UI and curated security defaults —
  a distinct product, not a pi plugin. pi is chosen over opencode because it is
  built to be embedded (minimal, model-agnostic, split into `pi-ai` /
  `pi-agent-core` / `pi-tui` / `pi-coding-agent`), MIT-licensed, and TypeScript.
  We pin a specific pi version and track upstream deliberately.
- **The engine stays Python.** It is done and tested (91 tests). Porting to
  TypeScript is weeks of rework for zero new capability. The CLI invokes it as
  a subprocess and consumes structured JSON-lines events plus a final result.

**Honest caveat:** pi provides no sandbox, verification, or oracle. The fork
supplies the conversational shell and provider plumbing only. Sandbox mode is
entirely our Python engine surfaced through the CLI — the differentiated part
does not come from the fork.

## 3. Two model call sites — pi-ai vs. our gateway

There are two separate model call sites and they share **configuration, not
code**:

- **Agent mode → pi's `pi-ai`.** Use it as-is. Model presets for Ollama /
  llama.cpp / vLLM / hosted OpenAI-compatible endpoints are configuration on
  top of `pi-ai`. Do not rebuild a provider framework.
- **Sandbox mode → a minimal Python `ModelGateway`.** The model investigating
  inside the container is driven by the Python harness, not the TS CLI, so it
  cannot use `pi-ai`. Per the original spec §6.2 it is one narrow function —
  `next_action(context: AgentContext) -> AgentAction` over httpx, strict
  JSON-envelope parse, timeout, ~1200-token cap, one formatting retry, API key
  from an env var and never persisted or logged. A `ScriptedGateway` provides
  deterministic tests. The context is a bounded `AgentContext` — finding,
  current phase, last tool result, latest check results, candidate patch hash,
  budget snapshot, capped action tail — built by the harness, never the raw
  transcript.

The user sets the model once (endpoint + key env var). The CLI stores it and
passes it to the Python engine over the seam when `/sandbox` runs.

## 4. Agent mode (default)

The home experience. A conversational security agent optimized for uncensored
models.

- **Model presets / plug-and-play setup.** One-command configuration for
  Ollama, llama.cpp, vLLM, or any hosted OpenAI-compatible endpoint. Detect a
  local server or pick from a list. API key from an env var, never persisted or
  shown.
- **Curated security prompts/tasks.** A built-in library of defensive security
  tasks: code vuln review, vuln/exploit explanation, CTF-style tasks, malware
  and phishing analysis, payload crafting for the user's own targets. Surfaced
  as suggested prompts/tasks in the UI.
- **Onboarding via suggested prompts/tasks.** First-run and empty states show
  concrete things to try so a first-time uncensored-model user immediately sees
  what the model is good at.
- **Security-scoped identity.** The agent's system prompt and tools are tuned
  for security/defensive work, not general coding.

### Default-mode disclosure (required)

Agent mode runs the model with the user's full system permissions (pi has no
built-in permission system). This is a deliberate product choice — agent mode is
the fast, familiar experience — but it must be honest. On first run, agent mode
displays a clear notice: **this mode runs the model with your system's
permissions and is not sandboxed; use `/sandbox` for contained, verified runs.**
The default stays agent mode; the risk is disclosed, not hidden.

## 5. Sandbox mode (`/sandbox`)

The existing least-privilege engine, exposed as a mode.

- The user points at a **manifest-packaged project** — a repo plus a PatchCage
  manifest naming the pinned runtime image, the path scope, and the check
  ladder — and a finding file (JSON/YAML matching the `Finding` model). The
  model works inside the locked Docker container through MCP tools — no shell,
  no network, no write outside the disposable workspace.
- The SQL-injection demo (`manifests/flask_sql_injection.yml`) is the first
  packaged project. Pointing at an arbitrary repo with no manifest is **not**
  supported at MVP: there is no image, oracle, or scanner config to run
  against it. SARIF ingestion is likewise deferred — no parser exists yet.
- The host runs the verification ladder (compile, scanner, hidden oracle, unit)
  and a clean replay in a fresh container. The model never sees or invokes the
  oracle.
- The user reviews the verified diff and approves export of `final.patch` plus
  an evidence bundle. The original repo is never modified.
- Steering is advisory and, at MVP, front-loaded: the finding file plus
  optional `--instructions` at launch shape the run; live mid-run steering
  over the seam is post-MVP. Neither user nor model can bypass verification.

### Docker requirement

Sandbox mode requires Docker (OrbStack on macOS). This is the load-bearing
isolation guarantee. The CLI detects Docker, guides install when missing, and
fails with a clear message when the daemon is down. Agent mode does not require
Docker.

## 6. The seam between CLI and engine

The TS CLI and Python engine communicate over a process boundary:

- CLI spawns `patchcage-engine run --repo <path> --manifest <path>
  --finding <file> [--instructions <text>] --model-endpoint <url>
  --model-id <id>` (key via env var).
- The engine streams **JSON-lines progress events** (phase changes, model
  actions, check results) and finishes with a result object: status, run
  directory, verified diff reference, evidence bundle path, per-check outcomes.
- The CLI renders those events as conversational narration + verification
  checklist + approval gate.
- **Approval is a second invocation, not stdin on a live process.** The engine
  exits at `awaiting_approval` with the verified diff and evidence in the run
  directory. On approve, the CLI runs `patchcage-engine export --run <dir>
  --out <path>` to write `final.patch`; on reject it deletes the run
  directory. The engine never exports without an explicit export call, and
  `export` re-checks the persisted phase and diff hash before writing — a
  tampered run directory fails closed, so the model cannot bypass the
  approval gate.
- **Cancellation:** Ctrl-C in the TUI sends SIGTERM to the engine subprocess;
  the engine traps it, tears down labeled Docker containers/volumes, and exits
  with a distinct code. This is part of the seam, not an afterthought.

The engine is a real Python console script (`[project.scripts]
patchcage-engine`), not the spike.

## 7. Install / distribution — one command

Goal: one terminal command installs both halves.

```
curl -fsSL https://patchcage.sh/install.sh | bash     # or: npm i -g patchcage
```

The installer:

1. Installs the **`patchcage` CLI** (the pi fork, via npm/bun).
2. Installs the **Python engine** so the user does **not** install Python.
   PyInstaller binary vs. `uv`-managed environment is decided by a packaging
   spike at the start of Phase 7 — not left open.
3. **Checks for Docker** and, if missing, guides the install (or offers to
   install OrbStack on macOS).
4. **Pulls the pre-built sandbox image only when a Docker daemon is present.**
   Without Docker the install still succeeds for agent mode; the image pull is
   deferred to the first `/sandbox` run, which re-checks the daemon and pulls
   then.

Honest limit: agent mode works immediately after the one command; sandbox mode
works once Docker is present and the image has been pulled. Docker is the one
thing a one-command install cannot fully hide on macOS.

## 8. Build order — engine first

The differentiated, risky, fully-ours core is the model-driven Python engine.
Prove it before the TS integration.

1. **`AgentContext` + Python `ModelGateway`** (httpx, OpenAI-compatible) +
   `ScriptedGateway`.
2. **Harness runner** (`harness/runner.py`) driving the state machine:
   preflight → snapshot → baseline → investigate (model-driven) → patch →
   verification ladder → clean replay → await approval. Budgets + loop
   supervisor plug in here.
3. **`patchcage-engine` console script** wrapping the pipeline as a JSON-lines
   subprocess with cancellation, plus a separate `export` subcommand for
   post-approval patch export. Proven end-to-end on the SQL-injection demo
   with a scripted model, then a live model (opt-in).
4. **Fork pi → rebrand to `patchcage`.** Pin a version; strip to agent loop +
   TUI + provider layer; confirm it builds and runs a local model.
5. **Agent mode:** model presets, curated security prompt/task library,
   suggested-prompt onboarding, first-run safety disclosure.
6. **`/sandbox` mode** in the TUI: spawn the engine, render events, gate export
   on approval.
7. **Installer** (one-command, §7).

## 9. Testing

- Engine: existing 91 Python tests stay green; new tests for the gateway
  (scripted + a mocked httpx endpoint), the runner (scripted model → verified
  patch → evidence bundle), and the engine CLI (JSON-lines protocol +
  cancellation).
- CLI: pi's test setup plus tests for preset config, the curated prompt
  library, the `/sandbox` subprocess integration (mock engine), and the
  first-run disclosure.
- Live-model runs (Ollama / hosted) remain opt-in, not CI.

## 10. Explicitly not in this phase

- No web UI, REST API, SQLite persistence, or SSE (deferred; the CLI is the
  product for now).
- No port of the Python engine to TypeScript.
- No offensive/exploitation task library.
- No model-abliteration or interpretability tooling.
- No multi-run history / team features.
- No rebuilding a provider abstraction in Python beyond the narrow gateway.

## 11. Open risks

- **Fork maintenance.** Tracking upstream pi means periodic merges; we pin a
  version and upgrade deliberately rather than tracking HEAD.
- **Two-language repo + three runtimes to install.** TS CLI + Python engine +
  Docker. The one-command installer (§7) is the mitigation and is itself real
  work.
- **Differentiation depends on curation quality.** Agent mode only earns its
  place if the presets + security prompt library are genuinely better than
  stock pi pointed at an uncensored model. Content/UX problem, not code.
- **Default mode is unsandboxed.** Mitigated only by disclosure (§4), not by
  isolation. Accepted as a product decision.
