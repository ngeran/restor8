---
description: Bounded single-file edit (local-model safe — anchor, apply, verify, stop)
---
Make this edit: $ARGUMENTS

Rules (follow exactly):
- Edit ONLY the file(s) named above. Read the file first if you need the exact text.
- Then apply the edit IMMEDIATELY with the edit tool. Do NOT ask for confirmation, do NOT propose the change in text.
- If the edit fails because oldString doesn't match: re-read the file, retry with the EXACT content from the file. Never guess anchors. Retry up to 3 times.
- When applied, run `nix develop -c ruff check <the-file>` once and fix anything it reports the same way.
- Then STOP and show the resulting diff. Nothing else.
