---
description: Write a proof of concept for an authorized finding in this repo
argument-hint: "<finding>"
---
Write a proof of concept for: $ARGUMENTS

Authorized defensive work only. The PoC must run against this repository or a local fixture the user owns. Its purpose is to show the bug exists and that a fix closes it.

Constraints:
- No targeting of third-party systems
- No credentials, payloads, or steps meant for unauthorized access
- Prefer a unit test or a small local script that fails before the fix and passes after
- If a full PoC would be weaponized, stop at a description of the trigger plus a failing test
