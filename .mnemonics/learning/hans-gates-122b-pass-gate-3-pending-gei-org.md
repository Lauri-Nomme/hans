---
id: cba19794-b28d-458b-9c75-94570ef067b2
created: '2026-08-18T20:21:46.873Z'
modified: '2026-08-18T20:21:46.873Z'
memory_type: learning
tags:
  - hans
  - phase5
  - gate2
  - gate2b
  - bb-rest-archiver
---
hans (bb-rest-archiver) Phase 5 Gates 1, 2, 2B all PASS and committed.

- Gate 1 (corpus/gate1.py): tool archive vs real golden export — no genuine diffs. Benign: inner-tar mtimes/mode-type-bits, PR1 rescopedTimestamp (internal target-advance lazy-refresh, not REST-derived), real-only ACTIVITY/UPDATED for title/desc edits (REST /activities omits them — verified live: after PUT title edit REST still shows only OPENED), allParticipants order (DB position). PR1/4 metadata, activities byte-faithful except those.
- Gate 2 (corpus/gate2.py): round-trip import synth archive into bb-lab-b (POST /rest/api/1.0/migration/imports {"archivePath": "Bitbucket_export_<n>.tar"}), scrape-b diff vs scrape-a. Scripted checks: PR state/title/refs/reviewers/participants (slug-keyed), activities (commentId excluded — target reallocates), branches, tags, AND git objects object-wise (git cat-file, 54 objects identical). Users import as STUB accounts (displayName=slug, active=false, no email) — expected; email fidelity only in git author/committer objects. Commit-level comments absent after import (correct, mirrors real export).
- Gate 2B (corpus/gate2b.py): NEW — official re-export of bb-lab-b via POST /rest/api/1.0/migration/exports, pull tar via `sudo -n nerdctl cp`, compare vs golden export. Normalizations: repo/project ids (<ID>/<PRID>), nodeId, stub-user identities (userId slug|displayName||type → slug), reallocated comment ids (strip comment.id + commentId in activities), allParticipants as sorted set, rescopedTimestamp skipped, timestamps rounded to seconds. Result: PASS — only diff = title-edit ACTIVITY/UPDATED lost in round trip (REST gap). Note: bb-lab-b repo id = 1 (fresh), so map_key remaps paths; beware repo-id vs PR-id collision (both 1).
- run.sh rebuild + re-scrape + reassemble + gate1 gives a clean reproducible loop. bb-lab-b import archive needs chown bitbucket:bitbucket.
- Gate 3 (GEI end-to-end) still pending — needs throwaway Enterprise trial org + GH_PAT with migrate scope/migrator role. gh bbs2gh (v1.32.0) installed; migrate-repo supports --archive-path (no BBS needed if using --archive-path).
