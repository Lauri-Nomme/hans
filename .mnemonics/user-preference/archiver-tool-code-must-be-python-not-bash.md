---
id: aaab3af0-6cd1-4020-9923-f7cbfea7094b
created: '2026-08-18T17:15:56.041Z'
modified: '2026-08-18T17:15:56.041Z'
memory_type: user-preference
tags:
  - hans
  - phase4
  - python
  - tooling
---
For the hans project (Bitbucket REST->Archive export format reconstruction tool): all actual tool code that does the export over REST (Phase 4 archiver: scrape/assemble/validate; any crawler, git-fetcher, model, emitter, user-harvester) MUST be written in Python, not bash. Bash/.sh is acceptable only for lab bring-up (lab/*.sh) and Phase 1 fixture scaffolding (fixtures/*.sh).
