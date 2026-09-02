# PatchCage roadmap

Phases 1–6 are done (engine context + gateway, host loop, `patchcage-engine`
JSON-lines CLI, vendored pi under `cli/`, unsandboxed agent-mode disclosure /
presets / prompt library, `/sandbox`). Next work, in this order:

| Phase | What |
| --- | --- |
| **5** | Done. See [docs/agent-mode.md](agent-mode.md). Interactive agent stays unsandboxed. |
| **6** | Done. `/sandbox` spawns `patchcage-engine`, narrates JSON-lines events, gates export on approval. See [docs/agent-mode.md](agent-mode.md#sandbox-findingyml-phase-6). |
| **7** | Installer (`curl … \| bash` or `npm i -g`). Spike PyInstaller vs uv first; pick one. Agent mode must install without Docker. |

Do not restore the dropped long specs. Pin, rebrand, and config-dir notes for
the vendor tree are in `cli/UPSTREAM.md`. Host vs in-container `.patchcage`
paths are in the root README.
