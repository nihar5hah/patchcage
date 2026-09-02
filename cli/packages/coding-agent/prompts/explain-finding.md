---
description: Explain a security finding in plain language
argument-hint: "<finding>"
---
Explain this finding: $ARGUMENTS

Authorized defensive work only. Explain impact, preconditions, and how a maintainer would confirm and fix it in this codebase.

Do not provide a weaponized exploit, payload, or steps to attack a system the user does not own or is not authorized to test.

Cover:
- What is going wrong, in the code
- Who can trigger it and from where
- Realistic impact
- The smallest correct fix
