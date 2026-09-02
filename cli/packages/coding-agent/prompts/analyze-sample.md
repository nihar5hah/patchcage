---
description: Analyze a local sample, log, or artifact for defensive review
argument-hint: "<path>"
---
Analyze this local sample: $1

Authorized defensive work only. Read the file in this workspace. Identify what it is, what it does, and what a defender should watch for.

Do not execute unknown binaries. Do not reach out to third-party hosts. Do not turn the sample into an attack against anything outside this authorized scope.

Report:
- File type and likely origin
- Observable behavior (APIs, paths, network indicators)
- Whether it is malicious, suspicious, or benign, and why
- Recommended next steps for the owner of this system
