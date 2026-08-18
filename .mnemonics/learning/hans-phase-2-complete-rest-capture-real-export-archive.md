---
id: baf1110e-a09d-460e-b748-1a3bd88a476e
created: '2026-08-18T17:55:57.370Z'
modified: '2026-08-18T17:55:57.370Z'
memory_type: learning
tags:
  - hans
  - phase2
  - bb-rest-archiver
  - ground-truth
  - export-archive
---
hans (bb-rest-archiver) Phase 2 complete, committed 5ff1ebd.

- corpus/capture.py: raw REST dumps (project/repo/users/branches/tags, per-PR detail+activities+per-path comments+commits+diff, per-commit detail+changes+per-path commit comments) → corpus/rest/ + corpus/manifest.json (file→endpoint). Commit comments captured via /commits/{sha}/changes (commit detail has files:null).
- Real export triggered over REST: POST /rest/api/1.0/migration/exports body {"repositoriesRequest":{"includes":[{"projectKey":"FIX","slug":"golden"}]}} → job 1 → COMPLETED. Archive at $BITBUCKET_SHARED_HOME/data/migration/export/Bitbucket_export_1.tar (plain tar, NOT gzip), extracted to ground-truth/export-a/ (27 files).
- Archive layout: instance-details.json (archiveVersion:2, nodeId 80b56f04...), metadata/project_1/project.json, metadata/project_1/repository_15.json (repo id=15, hierarchyId a1f92f6e...), permissions project+repo, bitbucket-git_git/repositories/15/{contents/objects.atl.tar, metadata/metadata.atl.tar.atl.gz, hooks/hooks.atl.tar.atl.gz}, bitbucket-git-lfs git-lfs-settings, pullRequests/{id}/{metadata,activities}.json.atl.gz, gitPullRequests/{id}/caches.atl.tar.atl.gz, _/repository/hierarchy_{begin,end}/<hierarchyId>.
- corpus/compare.py pairing check: PR metadata state/title/description/fromRef.latestCommit/toRef.latestCommit all match REST dumps. CRITICAL: archive activities use DIFFERENT schema than REST /activities — archive kind ∈ {COMMENT:ADDED, COMMENT:OTHER(edits), ACTIVITY, REVIEWERS:UPDATED, RESCOPED, MERGED} vs REST action ∈ {OPENED,UPDATED,COMMENTED,APPROVED,UNAPPROVED,REVIEWED,RESCOPED,MERGED,DECLINED}. Counts differ (PR1 archive=15 rest=12, PR4 archive=14 rest=13; others equal). Phase 3 must map archive kinds back to REST events.
- Fixture bug fixed (dfcd7f6): C2 commit was empty — missing `git add src/util.py`; hotfix now MODIFIES util.py instead of ADDing. Corpus SHAs regenerated (main tip 06cfd630, hotfix 7b74ee8, PR4 tip 51e7999).

Current phase: Phase 3 — format reverse engineering → FORMAT_SPEC.md.
