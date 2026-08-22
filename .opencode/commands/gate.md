---
description: Run the just check gate and fix what it finds, one error at a time
---
Run `nix develop -c just check`.

If it passes: say "gate green" and stop.

Otherwise fix ONE reported error at a time:
1. Read the file the error names.
2. Apply the minimal fix with the edit tool immediately — no confirmation questions, no proposing in text.
3. Re-run the gate.

Repeat until green or after 5 fixes, whichever comes first. Then stop and summarize each fix in one line. Never edit files the errors don't name.
