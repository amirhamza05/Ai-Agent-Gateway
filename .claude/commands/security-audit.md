---
description: Run the security-reviewer agent against current code and config
---

Dispatch the `security-reviewer` agent to audit the Gateway against the §14 threat model.

The agent reads-only — it produces a markdown report with Critical / Important / Minor findings, each grounded in a file:line reference. Do not implement fixes inside this command — relay the report back to the user and let them decide what to address.

Use this:
- Before merging any auth, logging, or upstream-call change
- After landing a new endpoint
- Before deploying to the trial VPS for the first time
