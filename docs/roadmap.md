# PatchCage roadmap

Phases 1–7 are done (engine context + gateway, host loop, `patchcage-engine`
JSON-lines CLI, vendored pi under `cli/`, unsandboxed agent-mode disclosure /
presets / prompt library, `/sandbox`, uv-first installer).

| Phase | What |
| --- | --- |
| **5** | Done. See [docs/agent-mode.md](agent-mode.md). Interactive agent stays unsandboxed. |
| **6** | Done. `/sandbox` spawns `patchcage-engine`, narrates JSON-lines events, gates export on approval. See [docs/agent-mode.md](agent-mode.md#sandbox-findingyml-phase-6). |
| **7** | Done. `scripts/install.sh` — uv tool → `patchcage-engine`; Node ≥22.19 → build `cli/` and link `patchcage`. Agent installs without Docker. PyInstaller skipped (uv fetches Python). Self-check: `bash scripts/test_install.sh`. |

Do not restore the dropped long specs. Pin, rebrand, and config-dir notes for
the vendor tree are in `cli/UPSTREAM.md`. Host vs in-container `.patchcage`
paths are in the root README.
