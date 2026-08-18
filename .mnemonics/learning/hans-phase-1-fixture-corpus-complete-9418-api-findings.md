---
id: 8f18a611-0b72-42dc-b3e7-fe24a51a8789
created: '2026-08-18T17:39:48.571Z'
modified: '2026-08-18T17:39:48.571Z'
memory_type: learning
tags:
  - hans
  - phase1
  - fixtures
  - bitbucket
  - api-findings
---
hans project Phase 1 (fixture corpus) COMPLETE and verified on bb-lab-a (Bitbucket DC 9.4.18). fixtures/run.sh rebuilds project FIX/repo golden from scratch: users ada/grace/alan (PROJECT_ADMIN), crafted-date git layer (main C0..C3b + 5 branches + v1.0 lightweight tag), then PR layer (PR1 feature/login MERGED merge_commit w/ reviewer churn + grace&alan approvals + comments; PR2 hotfix/critical MERGED ff; PR3 experiment/squash MERGED squash; PR4 feature/explore OPEN RESCOPED w/ inline anchors + grace approve/withdraw/reapprove + alan NEEDS_WORK; PR5 feature/declined DECLINED w/ approve/withdraw + hard-deleted comment; annotated tag v1.1 tagger Alan Turing on PR1 merge commit; commit-level comment on C2 anchored to src/util.py). Key 9.4.18 API facts: /reviewers endpoint 404s (use /participants role REVIEWER); PR author cannot approve own PR; ANY PR mutation (push/RESCOPED, target advance, title/description PUT) RESETS reviewer states (approvals + NEEDS_WORK) and a drift processor re-anchors inline comments async — so fixture scripts set reviewer states LAST after a drift-settle poll; tasks API 404s; comment DELETE is hard + needs ?version=N; comment edits need version 0; GET /commits/{sha}/comments requires ?path= and only returns path-anchored comments; merge needs fresh version (409 out-of-date races handled by merge_pr retry helper in fixtures/lib.sh); merge does NOT auto-delete source branch (KNOWN-BAD). User preference: Phase 4 archiver tool code must be Python, not bash. Next: Phase 2 paired capture (REST dumps + real admin export into ground-truth/). Uncommitted as of end of session: fixtures/prs.sh, run.sh, FIXTURES.md, plan updates, .gitignore.
