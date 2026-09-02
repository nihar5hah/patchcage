---
description: Review code for vulnerabilities in an authorized codebase
argument-hint: "[path or finding]"
---
Review $ARGUMENTS for security issues in this repository.

Authorized defensive work only. Stay inside this project. Do not attack, scan, or exploit any third-party system.

Focus on:
- Injection, authz/authn gaps, unsafe deserialization, path traversal, SSRF, secrets in source
- Whether a finding is reachable and what would actually break
- Concrete, minimal patches

If the user did not name a path, inspect the current change set (`git diff` / `git diff --cached`) first.
