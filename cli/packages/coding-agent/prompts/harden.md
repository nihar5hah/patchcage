---
description: Harden code in this repository against a named issue
argument-hint: "[issue or path]"
---
Harden this codebase against: ${@:-the current change set}

Authorized defensive work only. Prefer the smallest patch that closes the issue without adding speculative framework.

If no issue was named, review `git diff` / `git diff --cached` and harden what is actually changing.

Do:
- Fix the root cause in the shared function, not one caller
- Add or tighten validation at the trust boundary
- Leave one small check that fails if the guard is removed

Do not:
- Attack other systems
- Add unused abstraction or new dependencies
