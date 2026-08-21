---
id: 5bc7ac25-0320-4483-9318-80ebbf29f038
created: '2026-08-19T17:48:51.622Z'
modified: '2026-08-19T17:48:51.622Z'
memory_type: learning
tags:
  - lab
  - bitbucket
  - rca
  - merge-base
  - emitter
  - stacked-pr
---
RCA of the real-repo crash (KeyError 'commit' at emitter pr_activities MERGED) — REPRODUCED in lab (FIX/stacked) and fixed:

Scenario: base=main, branch1 → PR A (branch1→main), branch2 built ON branch1 → PR B (branch2→main). Merge PR B. Bitbucket then adds a MERGED activity to PR A with NO `commit` object (autoMerge=false), the "remotely merged" effect. 6 such PRs in real SX: 10231, 3041, 4082, 4587, 4605, 4859.

Admin export ground truth: such activities are exported with kind=MERGED, createdTimestamp, userId, autoMerge — and NO `hash` key at all (not empty string). Normal merges have the hash.

Fix (emitter.py pr_activities MERGED branch): build items list, append ("hash", commit) only when commit present. Verified EXACT MATCH on repro PR activities vs lab admin export, gate1 on the repro archive = NO GENUINE DIFFERENCES, golden gate1 still passes. Commit 3c99c08.

Lab gotchas relearned this session:
- trial3h.txt license (repo root) is applied to an EMPTY env — lab re-create renews the trial window. Expired license blocks git push ("License limit exceeded") AND migration export.
- Recreate: `PURGE=1 ./lab/lab-down.sh` + `sudo rm -rf lab/home/bb-lab-*` (home owned by container uid) + `LAB_LICENSE_FILE=.../trial3h.txt ./lab/lab-up.sh`.
- lab-up wait_ready TIMES OUT on first boot (setup.* not honored); drive wizard manually: steps database → settings (license-type=true, license via --data-urlencode NOT -d) → user → jira(skip). Then /status = RUNNING.
- Admin export: POST /rest/api/1.0/migration/exports (body {"repositoriesRequest":{"includes":[{"projectKey":"*","slug":"*"}]}}), poll GET .../exports/{job}; tar lands in CONTAINER at /var/atlassian/application-data/bitbucket/shared/data/migration/export/ (NOTE: shared/data, not data/).
- After this lab recreate: project FIX id 1, repo stacked id 1. (Previous gate2b repo-id mapping is stale.)
