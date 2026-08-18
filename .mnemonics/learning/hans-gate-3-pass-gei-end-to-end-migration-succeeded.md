---
id: 01dc09af-2fe2-4bb4-86d3-53813e963e1d
created: '2026-08-18T22:31:47.871Z'
modified: '2026-08-18T22:31:47.871Z'
memory_type: learning
tags:
  - hans
  - gate3
  - gei
  - bbs2gh
  - migration
  - enterprise-trial
---
hans (bb-rest-archiver): GATE 3 PASS — end-to-end GEI migration succeeded.

Ran `gh bbs2gh migrate-repo --archive-path /tmp/opencode/synth-a4.tar --use-github-storage --queue-only --github-org unapplicable --github-repo hans-golden ...` into the unapplicable org (now Enterprise trial; token at /tmp/hans.gei.txt with scopes admin:org, repo, workflow). Migration RM_kgDaACQwNzRhN2RkYS0zMzMyLTQwNDctODc2Mi1lMzM1NjQ2ZjdmNjQ SUCCEEDED (~7 min), 1 benign warning ("Allow Forking" not enabled at org level).

Key GEI access facts (I had it WRONG earlier — there is NO `migrate` PAT scope checkbox): required scopes for running a repo migration are `repo`, `read:org`, `workflow` (migrator role) or `repo`, `admin:org`, `workflow` (org owner). The real prerequisite is an Enterprise-plan org + owner/migrator role. Free orgs reject CreateMigrationSource.

Verified via corpus/gate3.py (committed): PR states+merged flags (1/2/3 merged, 4 open, 5 declined), all 6 branches, tags v1.0 (lightweight->commit 66d85f4) + v1.1 (annotated->tag obj a668497), main tip = PR1 merge commit a668497c — GIT SHAs BYTE-IDENTICAL to golden corpus. Review states: PR1 alan+grace APPROVED, PR4 alan CHANGES_REQUESTED + grace APPROVED (final). Comment bodies w/ unicode/emoji preserved; inline anchors (path+line) preserved; git author identities (Ada Lovelace/Grace Hopper/Alan Turing + emails) preserved. Note: GEI collapses multiple inline comments per position — PR4's 4 inline comments surfaced as 1 visible review comment; task comment migrated as an inline comment.

The whole project is DONE through Gate 3. Commits: cf74294 (gate3 script), 4ca7dab (plan). Pushed to github.com/Lauri-Nomme/hans (public).
