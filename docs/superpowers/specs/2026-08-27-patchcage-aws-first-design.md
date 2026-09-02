# PatchCage AWS-First Design

Status: Draft for user review  
Date: 2026-08-27

## 1. Purpose

PatchCage is a least-privilege agent harness that converts an existing software
security finding into a candidate patch. A self-hosted open-weight model
investigates an immutable source snapshot through narrow MCP tools. PatchCage,
not the model, controls execution phases, permissions, resource budgets,
verification, and export.

The model is useful but untrusted. Its statements never count as proof that a
vulnerability is fixed.

### Formal title

**Design and Implementation of a Least-Privilege MCP Agent Harness for
Self-Hosted Open-Weight Model-Based Software Vulnerability Remediation**

### Product description

PatchCage runs a user-controlled AWS-hosted model as an untrusted patch worker.
The local control plane sends selected task context and source excerpts to that
model, executes approved repository actions in network-isolated disposable
containers, independently verifies each candidate diff, and exports artifacts
only after human approval.

This is no longer described as strictly local-first. Source excerpts cross the
local-machine boundary and reach an authenticated endpoint in the user's AWS
account.

## 2. MVP decisions

The MVP uses:

- Python repositories only.
- One user-facing model backend: the existing OpenAI-compatible Qwen endpoint
  hosted in the user's AWS account.
- A scripted fake model for deterministic automated tests.
- Semgrep SARIF and manual finding ingestion.
- Docker Engine with prebuilt, pinned runtime images.
- The official MCP Python SDK v2 over stdio.
- FastAPI, Pydantic v2, SQLite, React, TypeScript, Vite, and Monaco.
- SQL injection as the first complete vertical slice.
- Command injection and path traversal only after the SQL-injection slice
  passes clean replay.

Deferred features include Ollama, additional languages, native model tool
calling, AWS instance start/stop automation, arbitrary dependency changes,
remote workers, pull-request creation, automatic repository application,
digital signatures, and stronger isolation such as gVisor or microVMs.

## 3. Non-goals

PatchCage is not:

- A vulnerability discovery engine.
- A general-purpose coding agent.
- A penetration-testing or exploit-automation platform.
- A public-target scanner.
- A replacement for Semgrep.
- A model benchmark or leaderboard.
- A multi-agent system.
- A service that pushes, merges, deploys, or edits the original repository.
- A guarantee that ordinary Docker containers contain every possible hostile
  kernel exploit.

## 4. Trusted and untrusted components

### Trusted

- PatchCage control plane.
- Host-managed project manifest.
- Policy engine.
- Snapshot validator.
- Docker lifecycle manager.
- Pinned runtime image and verifier scripts.
- Finding-specific security oracle.
- Verification engine.
- Human approving export.

### Untrusted

- AWS-hosted language model.
- Model output and tool arguments.
- Candidate patches.
- Imported repository and all of its text.
- Imported finding descriptions and SARIF.
- Repository tests and build scripts.
- Output produced by repository code.
- Working and verifier containers.

The control plane has Docker access and is therefore security-sensitive. The
model, MCP server, repository, and containers never receive the Docker socket.

## 5. System architecture

```text
React UI
    |
    | REST + SSE
    v
FastAPI control plane
    |
    +-- Agent harness
    |     +-- state machine
    |     +-- context builder
    |     +-- budget tracker
    |     `-- loop supervisor
    |
    +-- Policy and verification core
    |
    +-- AWS model gateway -------- HTTPS --------> Qwen llama.cpp server
    |
    +-- MCP client ----- stdio through docker exec -----+
    |                                                   |
    +-- Docker manager                                  v
    |                                         Working container
    |                                         + workspace MCP server
    |                                         + scratch Git repository
    |                                         + no network
    |
    `-- Clean replay runner ------------------> Fresh verifier container
                                              + original snapshot
                                              + final patch
                                              + hidden oracle
                                              + no network
```

Only the control plane has outbound access to the model endpoint. Working and
verifier containers use Docker's no-network mode.

## 6. Core component boundaries

### 6.1 Domain models

Framework-independent Pydantic models define findings, manifests, agent
actions, policy decisions, run phases, tool results, patches, checks, events,
and verification reports.

The domain layer does not import FastAPI, SQLAlchemy, Docker, MCP, or an
OpenAI-compatible client.

### 6.2 Model gateway

The production gateway implements one narrow interface:

```python
class ModelGateway(Protocol):
    async def health(self) -> ModelHealth: ...
    async def next_action(self, context: AgentContext) -> AgentAction: ...
```

Its first implementation calls the AWS-hosted OpenAI-compatible Chat
Completions endpoint. The base URL and model ID may be stored, but the Bearer
key is resolved from a process environment variable and is never persisted.

The gateway:

- Applies connection and response timeouts.
- Limits action output to approximately 1,200 tokens.
- Requests JSON-constrained output when the live endpoint supports it.
- Falls back to a strict JSON envelope parser when constrained output is not
  supported.
- Allows one compact formatting correction.
- Does not expose MCP or native OpenAI tools directly to the model.
- Redacts authorization data from all errors and logs.

The endpoint may be offline because the EC2 GPU can be stopped. PatchCage
detects this during preflight and fails without attempting to manage AWS.

### 6.3 Agent harness

The harness owns:

- Model invocation.
- Current phase and visible actions.
- Context construction.
- Schema parsing.
- Policy evaluation.
- MCP invocation.
- Budget accounting.
- Loop detection.
- Verification transitions.
- Completion rejection.
- Cancellation and cleanup.

The model selects one proposed action at a time. It does not control phase
transitions or decide that checks are sufficient.

### 6.4 Policy engine

The policy engine consists of pure decision functions:

```python
authorize_action(action, state, manifest) -> PolicyDecision
authorize_read(path, manifest) -> PolicyDecision
authorize_patch(metadata, manifest) -> PolicyDecision
authorize_check(name, phase, manifest) -> PolicyDecision
```

Every decision includes an allow/deny result, stable code, reason, risk level,
approval requirement, and structured evidence.

### 6.5 Snapshot service

The snapshot service:

1. Verifies the repository and selected commit.
2. Resolves the commit to a full SHA.
3. Rejects submodules for the MVP.
4. Preflights the tree with `git ls-tree -r -l`, rejecting trees that exceed a
   file-count or total-byte ceiling before any archive is produced, so a hostile
   oversized repository cannot exhaust the trusted control plane.
5. Rejects any committed `.gitattributes` (at any depth) that uses
   `export-ignore` or `export-subst`, which would otherwise silently drop
   audited files or rewrite their contents in the archive.
6. Runs `git archive --format=tar <sha>` without a shell and with a timeout.
7. Hashes the raw archive.
8. Validates every archive member before extraction.
9. Rejects absolute paths, traversal, NUL bytes, symlinks, hard links, devices,
   FIFOs, sockets, and unsupported entry types.
10. Fails import if a tracked path matches a blocked-secret pattern (checked
    case-insensitively).
11. Extracts regular files and directories into a new Docker volume.
12. Hashes the extracted snapshot deterministically.

Untracked files, including an untracked `.env`, are absent because `git archive`
contains only the selected commit. A tracked blocked secret causes an explicit
failure rather than silent omission, preserving reproducibility. The blocked
pattern set is a mandatory argument to snapshot creation, so secret exclusion
fails closed rather than depending on a caller remembering to pass it.

The original repository is never bind-mounted into a container.

### 6.6 Sandbox manager

The Docker manager creates containers with:

- No network.
- Non-root user.
- Read-only root filesystem.
- Exactly three writable locations: the `/workspace` volume, a `tmpfs` at
  `/tmp`, and a `tmpfs` mounted as the non-root user's `HOME` (pytest writes
  `.pytest_cache`, Semgrep writes under `$HOME`/`$XDG` cache; both must land on
  tmpfs rather than failing against the read-only root).
- Semgrep's offline settings are copied onto the HOME tmpfs at sandbox start
  (`SEMGREP_SETTINGS_FILE=$HOME/.semgrep/settings.yml`) because Semgrep
  mkstemps next to that file and `/opt` is read-only. The sandbox-wide
  environment, not the manifest, carries this variable.
- Dropped Linux capabilities.
- No privileged mode.
- No host PID or network namespace.
- No Docker socket.
- No host credentials.
- CPU, memory, PID, and wall-clock limits.
- Labels that permit cleanup after a control-plane crash.
- A prebuilt image pinned by digest.

The runtime image is a build-time dependency of the harness: it is built with
the application dependencies, pytest, Semgrep, Git, and the
`patchcage_workspace` MCP server package baked in, so the container can serve
MCP without any install step at run time.

Semgrep and `patchcage_workspace` use separate virtual environments inside the
image. The current Semgrep distribution pins MCP 1.x as one of its own
dependencies, while PatchCage targets the official MCP Python SDK v2. Isolating
the scanner CLI prevents its transitive dependency from downgrading the
workspace server.

Images and dependencies are prepared before a run. The run does not pull
images, install packages, or contact package registries.

### 6.7 Workspace MCP server

The server runs inside the working container. The host launches it through a
stdio command equivalent to:

```text
docker exec -i <container> python -m patchcage_workspace
```

The server can access the extracted workspace and an immutable manifest
projection. It has no model credential, host path, or Docker access. The
package providing it is installed into the runtime image at image-build time;
it is not copied in per-run.

The MCP SDK's structured Pydantic outputs are used for tool results. The client
and server both enforce timeouts and result-size limits.

### 6.8 Verification engine

The verifier owns baseline validation, working-sandbox checks, and clean replay.
It derives the final verdict from structured check outcomes. It never accepts a
model-authored success field as evidence.

The hidden security oracle is a black-box judge. It starts the application as a
separate process and decides pass/fail only from external behavior (HTTP
responses), never by importing workspace code into the judging process. The
application seed data is randomized per run through an environment variable the
patch cannot predict, so a patch cannot hardcode the expected result. A patch
that writes a side-effecting module (for example a `sitecustomize.py` or an
import-time `os._exit`) cannot forge a verdict: the oracle's own process never
has the workspace on its `sys.path`, and a child that exits before serving fails
closed as an oracle error, not a pass.

For the same reason, the unit check runs through a trusted runner baked into the
image that inserts the workspace onto `sys.path` only after interpreter startup,
rather than via a `PYTHONPATH` environment variable (which would let a patched
`sitecustomize.py` execute before pytest starts).

### 6.9 Persistence and artifacts

SQLite stores structured metadata. Large or sensitive outputs are stored as
content-addressed local artifacts referenced by SHA-256.

The event stream is append-only at the application level. Reports are generated
from stored events and check results rather than from model prose.

## 7. Host-managed project manifest

Repository content cannot define trusted commands. A `patchcage.yml` imported
from the target repository is treated as untrusted data and is never executed
without explicit conversion into a reviewed host-side manifest.

For the shipped demos, PatchCage owns the manifests.

```yaml
version: 1

project:
  name: flask-sql-injection-demo
  language: python

runtime:
  image: patchcage/python-flask-demo@sha256:TRUSTED_IMAGE_DIGEST
  workdir: /workspace

scope:
  readable:
    - src/**
    - tests/**
    - pyproject.toml
  writable:
    - src/**
  blocked:
    - .git/**
    - .env*
    - "**/*.pem"
    - "**/*.key"

checks:
  compile:
    argv: [python, -m, compileall, -q, src]
    timeout_seconds: 30
  scanner:
    argv:
      - semgrep
      - scan
      - --config
      - /opt/patchcage/rules/sql-injection.yml
      - --metrics
      - "off"
      - --json
    timeout_seconds: 60
  unit:
    argv:
      - python
      - /opt/patchcage/runners/run_unit.py
      - /workspace
      - tests/unit
    timeout_seconds: 60
  security:
    argv:
      - python
      - /opt/patchcage/oracles/sql_injection_oracle.py
      - --workspace
      - /workspace
    timeout_seconds: 60

limits:
  model_turns: 12
  tool_calls: 25
  invalid_outputs: 3
  policy_violations: 5
  patch_attempts: 3
  repair_cycles: 2
  identical_failed_actions: 2
  run_wall_clock_seconds: 2400
  check_timeout_seconds: 300
  patch_bytes: 65536
  patch_files: 5
  added_lines: 200
  deleted_lines: 100
```

Commands are argument arrays. The model supplies only a check name. No command
interpolation or shell execution is permitted.

There are two distinct time budgets. `check_timeout_seconds` bounds each
individual sandbox operation (compile, scanner, unit tests) and is the
per-check ceiling used in Section 15. `run_wall_clock_seconds` bounds the whole
run and is sized for the measured endpoint: at roughly 14 tokens/second with a
single inference slot, a 12-turn investigation with ~1,200-token actions takes
15–20 minutes. A 300-second whole-run limit would abort real runs
mid-investigation, so it must not be reused as the run budget.

## 8. Path rules

All repository paths use POSIX relative syntax.

The host and MCP server independently:

1. Reject empty, absolute, NUL-containing, and backslash-containing paths.
2. Normalize `.` segments.
3. Reject any `..` segment.
4. Resolve the existing target strictly.
5. Confirm the canonical target remains under the workspace root.
6. Reject symlinks and non-regular file types.
7. Apply blocked patterns before readable or writable allowlists.
8. Enforce file-size, line-range, and response-size limits.

Two refinements hold regardless of the manifest's literal patterns:

- Secret files are blocked case-insensitively by name (`.env*` and key/material
  suffixes such as `.pem`, `.key`, `.p12`, `.pfx`, `.crt`). General scope
  patterns remain case-sensitive, which is correct on Linux, but secret material
  must not slip through on a case change (for example `secret.PEM`).
- A directory is readable when it is an ancestor of, equal to, or beneath the
  static prefix of a readable pattern, so `list_files("src")` works even though
  the `src/**` glob does not textually match the bare `src` path.

`search_code` additionally enforces a hard wall-clock timeout and a result cap
per query, so a model-supplied regex cannot backtrack pathologically or exhaust
the sandbox CPU budget.

For a new file in a patch, the policy validates the normalized path and its
nearest existing parent. Snapshot-level symlink rejection prevents a new-file
parent from redirecting outside the workspace.

## 9. Model action protocol

The model returns one JSON object:

```python
class ToolAction(BaseModel):
    type: Literal["tool"]
    tool: str
    arguments: dict[str, Any]
    summary: str


class PatchAction(BaseModel):
    type: Literal["patch"]
    diff: str
    summary: str


class CompletionAction(BaseModel):
    type: Literal["complete"]
    summary: str
    evidence_ids: list[str]
```

`PatchAction` is the model-facing representation. The harness validates it and
routes it to the MCP patch operation. This avoids giving the model a direct
write primitive while keeping patch execution behind the MCP boundary.

The summary is a short action explanation, not hidden chain-of-thought.
PatchCage does not request or store private reasoning. Because the summary is
model-controlled prose, it is excluded from the loop supervisor's action
signature; otherwise the model could evade repetition detection by rewording the
explanation of an identical call.

There is no separate planning action in the MVP. Action summaries and the
structured state are sufficient, reducing one model turn and one output format.

## 10. State machine

```text
CREATED
  -> PREFLIGHT_VALIDATED
  -> SNAPSHOT_READY
  -> BASELINE_VERIFIED
  -> INVESTIGATING
  -> FINDING_CONFIRMED
  -> PATCH_ENABLED
  -> PATCH_VALIDATING
  -> WORKING_VERIFICATION
  -> CLEAN_REPLAY_VERIFIED
  -> AWAITING_APPROVAL
  -> EXPORTED
```

`WORKING_VERIFICATION` may transition to `REPAIRING`, then back to
`PATCH_VALIDATING`, within the repair budget. `WORKING_VERIFICATION` may also
transition to `CLEAN_REPLAY_FAILED` when the fresh-container replay fails.

Individual policy denials are non-terminal events: the run keeps its current
phase, the denial is recorded, and the `policy_violations` budget is consumed.
Only exhausting that budget terminates the run (as `BUDGET_EXHAUSTED`). There is
no terminal `POLICY_REJECTED` state, because a single denial must not kill a run
that still has budget.

Terminal states include:

- `MODEL_UNAVAILABLE`
- `SOURCE_REJECTED`
- `BASELINE_FAILED`
- `FINDING_NOT_REPRODUCIBLE`
- `INVALID_MODEL_OUTPUT`
- `PATCH_APPLICATION_FAILED`
- `VERIFICATION_FAILED`
- `CLEAN_REPLAY_FAILED`
- `BUDGET_EXHAUSTED`
- `SANDBOX_ERROR`
- `CANCELLED`
- `USER_REJECTED`

Transitions are implemented as an explicit table. API handlers and model output
cannot set a phase directly.

## 11. Phase-based MCP tools

### Investigation

- `get_finding`
- `list_files`
- `read_file`
- `search_code`
- `get_repository_status`
- `run_finding_check`

### Patch and repair

The investigation tools remain available, with:

- `propose_patch`
- `get_current_diff`
- `run_named_check`
- `discard_patch`

`run_named_check` accepts only `compile`, `scanner`, and `unit`. The `security`
oracle is never model-invocable: it runs only inside the host-driven
verification ladder (Sections 12.7 and 12.9), so the model cannot observe its
verdict and tune a patch against the hidden test. A model request for
`security` is a policy denial (`DENY_UNKNOWN_CHECK`).

After a valid candidate patch is applied, PatchCage automatically runs the full
verification ladder. `run_named_check` remains useful for a bounded targeted
rerun, but identical checks against an unchanged repository return cached
evidence rather than executing again.

## 12. Run lifecycle

### 12.1 Preflight

- Validate the model endpoint, TLS, authentication, model ID, and a minimal
  structured response.
- Validate the host-managed manifest.
- Verify Docker availability and the pinned image.
- Verify the repository and commit.
- Show the user that selected source excerpts will be sent to AWS.

No run starts without explicit initial authorization.

### 12.2 Snapshot

Create and hash the archive, validate its entries, extract it into a fresh
volume, initialize new scratch Git metadata inside the container, and commit the
baseline there. Scratch Git metadata never comes from the original repository
and is blocked from MCP access.

### 12.3 Baseline

The baseline is valid only when:

- Compilation passes.
- Existing unit tests pass.
- The imported finding is present.
- The finding-specific oracle reproduces the vulnerable behavior.

Expected-vulnerable oracle output is normalized as a successful baseline
reproduction, not confused with an infrastructure test failure.

### 12.4 Investigation

Each turn receives:

1. Authorized task and normalized finding.
2. Host-controlled phase and visible actions.
3. Verified facts.
4. Relevant recent result.
5. Failed approaches.
6. Remaining budgets.

The raw conversation is not appended indefinitely. Large tool and check output
remains in artifacts; the model receives bounded excerpts and deterministic
summaries.

### 12.5 Finding confirmation

Patch capability remains unavailable until `run_finding_check` produces the
manifest-defined confirmation result. Model confidence alone cannot enable
patching.

### 12.6 Patch application

Every patch proposal is a complete candidate against the baseline:

1. Enforce response and patch byte limits.
2. Ask Git to parse metadata using NUL-safe output.
3. Reject binary changes, renames, copies, mode changes, symlinks, and special
   files.
4. Enforce file and line budgets.
5. Enforce writable, blocked, test, and dependency policies.
6. Reset the disposable workspace to its baseline.
7. Run `git apply --check` against that clean baseline.
8. Apply the complete candidate.
9. Record the patch and resulting repository hashes.

Git is authoritative for diff syntax and clean application. PatchCage avoids
implementing a complete unified-diff parser from scratch.

### 12.7 Working verification

The host automatically runs:

1. Patch policy replay.
2. Compile.
3. Relevant Semgrep rule.
4. Hidden security oracle.
5. Existing unit tests.
6. Repository-status and diff inspection.

A scanner result alone is insufficient. A candidate that merely removes code,
adds suppression, or breaks functionality does not pass.

### 12.8 Repair

On a repairable failure, the model receives the failed check, a bounded output
excerpt, the current patch hash, and remaining budget. The next patch replaces
the whole candidate from a clean baseline.

Patch oscillation or repeated equivalent failures terminate the repair loop.

### 12.9 Clean replay

A new container and volume are created from the immutable snapshot. The final
patch is applied and the entire ladder runs again. The verifier receives no
model conversation, working-container filesystem, or cached success result.

Hidden oracle files are outside MCP-readable paths, are not invocable through
any model-facing tool, and are introduced only by the verification environment.
This reduces straightforward test gaming but is not described as cryptographic
secrecy from arbitrary code executing in the verifier container.

### 12.10 Approval and export

The user reviews:

- Finding and source commit.
- Source and patch hashes.
- Complete diff.
- Changed-file and line counts.
- Baseline, working, and clean-replay checks.
- Policy decisions and warnings.
- Blocked actions.
- Model identity and endpoint type.

Approval exports `final.patch`, structured evidence, logs, and an HTML report to
a user-selected output directory. Rejection destroys disposable resources and
retains the audit record.

## 13. Patch policy

Deterministic rejections include:

- Malformed or non-applicable diff.
- Absolute, traversing, blocked, or non-writable path.
- Binary content.
- Rename, copy, symlink, or mode change.
- Test or dependency-file modification.
- Excessive bytes, files, additions, or deletions.
- Scanner configuration modification.
- Known scanner-ignore markers configured for the project.

Heuristic checks may warn about suspicious secret-like values or unusually broad
security-control removal. Heuristic warnings are clearly distinguished from
deterministic violations and require human review rather than being presented
as proof.

## 14. Loop supervision

An action signature contains the phase, tool or action type, normalized
arguments, repository hash, and current patch hash.

The supervisor detects:

- Identical failed action repetition.
- Same check against unchanged code.
- Repeated invalid JSON.
- Repeated policy violations.
- Patch `A -> B -> A` oscillation.
- Turns without new files, evidence, patch state, or improved checks.
- Completion requested before host requirements pass.

The same failed action may execute twice. A third equivalent attempt is blocked
before execution. Different arguments or a changed repository state create a
new signature.

PatchCage supports one active model run in the MVP because the deployed
llama.cpp server has one inference slot. This global lock is a deliberate
prototype ceiling and can later be replaced by a queued worker model.

## 15. Error handling and recovery

### Recoverable

- First malformed response: send one schema correction.
- Policy denial: return structured denial and consume violation budget.
- Working verification failure: enter repair when budget remains.
- MCP tool error: return bounded structured error if the session remains valid.
- Repeated check: return cached evidence or block without re-execution.

### Terminal

- Model offline, invalid TLS, or failed authentication during preflight.
- Unsafe or invalid source archive.
- Missing or mismatched pinned image.
- Invalid baseline.
- MCP process loss.
- Sandbox operation exceeding its per-check timeout, or resource failure.
- Whole-run wall-clock, turn, tool-call, or repair budget exhaustion.
- Failed clean replay.
- User cancellation or rejection.

Every terminal path records a stable failure code and attempts idempotent
container and volume cleanup. Crash-resume is deferred; on restart, PatchCage
marks abandoned active runs failed and removes resources carrying their labels.

## 16. Persistence model

Initial tables:

```text
model_profiles
projects
snapshots
findings
runs
run_events
patches
check_results
policy_decisions
approvals
artifacts
```

`run_events` records ordered lifecycle facts. Patches, checks, decisions, and
approvals have normalized tables because the UI and report query them directly.

No secret values are stored. A model profile contains an environment-variable
name such as `PATCHCAGE_MODEL_API_KEY`, not its resolved value.

## 17. API and UI

### API

```text
POST /api/models
POST /api/models/{id}/test
GET  /api/models

POST /api/projects/import
GET  /api/projects
GET  /api/projects/{id}

POST /api/findings/import-sarif
POST /api/findings/manual
GET  /api/projects/{id}/findings

POST /api/runs
GET  /api/runs/{id}
GET  /api/runs/{id}/events
GET  /api/runs/{id}/stream
POST /api/runs/{id}/cancel
POST /api/runs/{id}/approve-export
POST /api/runs/{id}/reject

GET  /api/runs/{id}/patch
GET  /api/runs/{id}/evidence
GET  /api/runs/{id}/report
```

SSE is sufficient for one-way progress updates. Commands use normal REST
requests; WebSockets are unnecessary for the MVP.

### UI flow

1. Configure endpoint, model ID, and credential environment-variable name.
2. Test model health and structured output.
3. Select repository and commit.
4. Select a reviewed host manifest.
5. Import SARIF or enter a finding.
6. Review source-sharing disclosure, scope, checks, and budgets.
7. Start the run and observe the event timeline.
8. Review candidate diff, verification evidence, and blocked actions.
9. Approve export or reject.

The UI never displays or submits the resolved API key.

## 18. Evidence and observability

Each run produces:

```text
evidence/
  manifest.json
  original-finding.json
  source-snapshot.json
  model-config.json
  policy.yaml
  action-summaries.jsonl
  tool-trace.jsonl
  policy-decisions.jsonl
  blocked-actions.json
  final.patch
  workspace-verification.json
  clean-replay-verification.json
  logs/
  report.html
```

`model-config.json` contains only the endpoint URL, model ID, sampling
parameters, and the credential environment-variable name. The resolved API key
is never written into the evidence bundle.

Default telemetry records model identity, durations, token counts when returned,
action types, tool names, argument hashes, result status, policy codes,
repository hashes, patch hashes, and timestamps.

Prompt text, source snippets, tool payloads, raw model output, credentials, and
full logs are excluded from telemetry by default. Structured run events are the
evidence source of truth. OpenTelemetry export is optional and can be added
after the local event pipeline works.

## 19. Testing strategy

### Deterministic unit and property tests

- Archive path traversal and unsupported member types.
- Absolute, relative, mixed-separator, and symlink path attacks.
- Readable, writable, and blocked pattern precedence.
- Patch metadata, scope, mode, binary, and size rules.
- Action schemas and formatting retry.
- State-transition table.
- Budget accounting.
- Repetition, stagnation, and oscillation detection.
- Verification verdict derivation.

### Docker integration tests

- Snapshot cannot modify original repository.
- MCP discovery and calls over `docker exec` stdio.
- No network in working and verifier containers.
- Non-root execution and resource limits.
- Patch reset, apply, discard, and diff.
- Baseline and clean replay.
- Cleanup after timeout and cancellation.

### End-to-end deterministic tests

A scripted fake model emits exact sequences for:

- Successful SQL-injection repair.
- Invalid path request.
- Prompt-injection-following request.
- Malformed output then correction.
- Broken patch then repair.
- Repeated failed check.
- Patch oscillation.
- Premature completion.

### Live AWS acceptance test

An opt-in test uses the hosted Qwen endpoint to investigate and repair the demo.
It proves endpoint compatibility and practical agent behavior. It is not a
required deterministic CI check because the GPU can be stopped and model output
is probabilistic.

## 20. Implementation sequence

1. **Deterministic security kernel**
   - Domain models, manifest schema, state machine, path policy, patch metadata
     policy, budgets, loop supervisor, and unit/property tests.
   - SQL-injection demo, Semgrep rule, unit tests, and hidden oracle.

2. **Immutable execution**
   - Git snapshot service, safe archive extraction, Docker runtime, scratch Git,
     named checks, resource limits, and clean cleanup.
   - Build the pinned demo image with app dependencies, pytest, Semgrep, Git,
     and the `patchcage_workspace` MCP server package baked in.

3. **MCP workspace boundary**
   - Container MCP server, stdio client, structured tools, duplicate path
     enforcement, and integration tests.

4. **Patch and verifier pipeline**
   - Complete-candidate patch lifecycle, automatic verification, bounded repair,
     and fresh-container replay.

5. **Model and harness**
   - Scripted model, AWS gateway, bounded context, action parser, harness loop,
     model preflight, and live opt-in acceptance.

6. **Persistence and API**
   - SQLite, artifacts, run events, FastAPI routes, SSE, cancellation, approval,
     export, and evidence report.

7. **Web interface**
   - Model, project, finding, run, timeline, diff, verification, and export
     screens.

8. **Additional demonstrations and hardening**
   - Command-injection and path-traversal fixtures, adversarial tests,
     documentation, final demo, and presentation evidence.

## 21. Acceptance criteria

PatchCage is complete when:

- The AWS-hosted model can pass endpoint and action-format preflight.
- A selected Git commit becomes a reproducible, safely extracted snapshot.
- The source repository remains byte-for-byte unchanged.
- A Semgrep or manual finding can be normalized.
- Baseline checks distinguish vulnerability reproduction from infrastructure
  failure.
- The model can inspect only permitted files through MCP.
- No generic shell, network, package-installation, or direct-write tool exists.
- Unsafe paths and patches are rejected twice, at host and MCP boundaries.
- Candidate patches modify only permitted source files.
- Compile, scanner, hidden oracle, and unit checks execute automatically.
- Failed checks prevent completion and permit only bounded repair.
- The final patch passes a fresh-container replay.
- Repetitive actions terminate within budget.
- Events, policy decisions, blocked actions, hashes, and checks appear in the
  evidence report.
- Export requires explicit human approval.
- Export creates patch and evidence artifacts without applying them to the
  original repository.

## 22. Explicit limitations

- Source excerpts are transmitted to the user's authenticated AWS endpoint.
- The endpoint currently depends on a manually started EC2 instance.
- Docker isolation is suitable for a controlled student prototype, not a claim
  of VM-grade containment.
- A hidden oracle is hidden from model tools and is not model-invocable, but it
  is not mathematically secret from arbitrary code executed in the verifier
  container.
- Scanner suppression and broad test tampering can be blocked deterministically;
  all semantic weakening cannot be detected perfectly.
- One active model run is supported.
- Active runs do not resume after a control-plane crash.
- Only reviewed, prebuilt Python images and known findings are supported.
