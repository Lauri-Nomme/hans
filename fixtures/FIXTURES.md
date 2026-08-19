# FIXTURES.md — the "golden repo" fixture corpus (Phase 1)

Target: project `FIX`, repo `golden`, on `bb-lab-a` (Bitbucket DC 9.4.18,
`http://localhost:7990`). Everything is created over REST + git pushes — fully
reproducible by wiping the repo and re-running `./fixtures/run.sh`.

Users (GEI mannequins later): `ada` (Ada Lovelace, ada@example.com),
`grace` (Grace Hopper, grace@example.com), `alan` (Alan Turing, alan@example.com).
All three are PROJECT_ADMIN on `FIX`. Sysadmin `admin` acts as "the automation".

Verified live API facts (9.4.18) that shape the corpus:

- `POST /pull-requests/{id}/reviewers` returns **404** — reviewers are managed
  via `POST /pull-requests/{id}/participants` with `{"user":{...},"role":"REVIEWER"}`,
  removed via `DELETE /participants/{user}`.
- A PR **author cannot approve** their own PR (approve POST fails silently with
  `|| true`; only an AUTHOR may not approve — reviewer status never sticks for the author).
- **Any PR mutation resets reviewer states** (approvals and NEEDS_WORK): a push to the
  source branch (RESCOPED), another PR merging into the target (main advance), and even a
  title/description `PUT`. A drift processor also re-anchors inline comments async after a
  push. Consequence: fixture scripts set reviewer states **last**, after a
  drift-settle poll on the inline-comment anchor.
- `POST /pull-requests/{id}/tasks` returns **404** on this build — no PR tasks
  API (`[HYPOTHESIS]` gap; production may differ — re-check before Phase 3).
- `DELETE /pull-requests/{id}/comments/{cid}` **hard-deletes** (requires
  `?version=N`, 409 without it) — no activity trace remains (GET /comments/{id} → 404).
- `PUT /pull-requests/{id}/comments/{cid}` requires the current comment `version`
  (created comments are version 0, not 1).
- Comment edits/deletes use comment versions; PR edits use PR versions (409
  out-of-date races handled by `merge_pr` retry-with-fresh-version).
- `GET /commits/{sha}/comments` **requires** `?path=` and only returns comments
  anchored to that path; a path-less commit comment is created (201) but is not
  retrievable.
- Merge strategies `ff`, `squash`, `merge_commit` all accepted. `squash` needs a
  clean merge (we isolate each PR's file changes so they never conflict).
- **Merge strategy findings on 9.4.18 (verified empirically on a scratch repo
  ZZ/scratch, then cross-checked against the real export):**
  - `ff` does **NOT** fast-forward on this build. It produces a **two-parent merge
    commit** whose tree is identical to the source tip ("content fast-forward recorded
    as a merge commit"). `MERGED.hash` = that merge commit.
  - `squash` produces a two-parent merge commit whose second parent is the branch tip;
    from main's first-parent history the branch appears as one commit.
  - an unknown strategy value (e.g. `bogus`) is **silently accepted** (HTTP 200) and
    behaves as `merge_commit`. Strategy does not change the git object shape.
  - Merge-commit identity: **author = PR author**, **committer = the merger**
    (admin), message = `Merge pull request #N in KEY/repo from <branch> to <target>\n\nMerged in <branch> (pull request #N)\n\n* commit '<sha>':\n  <2-space-indented first lines>`.
  - Consequence: the original design's "PR2 = ff, no merge commit" was WRONG — the
    real export shows PR2 as a merge commit (`ae33424`). FIXTURES.md table updated;
    FORMAT_SPEC.md §6.6/6.7 documents the observed behavior.
- Merging does **NOT** auto-delete the source branch (`KNOWN-BAD` case to study in
  the real export).
- **Tasks are comments with `severity: "BLOCKER"`** on 9.4.18. There is NO
  `POST /pull-requests/{id}/tasks` endpoint (404 — the old `[HYPOTHESIS]` gap is now
  resolved). Create a task = `POST /comments` with `{"severity":"BLOCKER",...}`;
  resolve = `PUT /comments/{id}` with `state:"RESOLVED"`. REST comment exposes
  `state`, `resolvedDate`, `resolver`; the export comment carries `resolvedTimestamp`
  + `resolverId` instead. PR `properties.openTaskCount`/`resolvedTaskCount` are
  REST-only (not in the archive).
- Annotated-tag tagger = git `user`/`email` at tag creation (set via
  `git -c user.name=... -c user.email=...`), not the pushing user.

## Git layer (create.sh)

Commit graph on `main` (C0..C3b) with crafted `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE`
(2024 dates) and mixed author/committer (e.g. C2 authored by Grace, committed by Ada):

```
C0  chore: initial project scaffold                       (ada)
C1  docs: expand README with overview                     (ada)
C2  feat: add core utility library                        (author grace / committer ada)
C3  docs: add contributing guide                          (alan)
C3b feat: add explore placeholder page                    (alan)  <- main tip
```

- `feature/login`  = C1 + C4/C5 (login.py; authored ada then grace)
- `hotfix/critical`= C3b + C6/C7 (src/util.py sanitize; alan)
- `experiment/squash` = C3b + C8/C9 (src/models.py — NEW file so it never conflicts
  with hotfix's src/util.py change; ada then grace)
- `feature/explore` = C3b + C11/C12 (explore.md with ADDED/REMOVED/CONTEXT lines
  for inline-comment anchors)
- `feature/declined` = C3b + C13 (grace)
- `feature/stacked/base` = C3b + S1 (stacked.md) — the "base" PR of the stacked pair
- `feature/stacked/dependent` = feature/stacked/base + S2/S3 (stacked.md + dependent.txt) —
  descends from the base so its merge carries the base commits into main
- `v1.0` lightweight tag on C1.

## PR layer (prs.sh)

| PR | branch → main | state | REST representation to reconstruct |
|----|--------------|-------|-------------------------------------|
| 1  | feature/login | MERGED (merge_commit) | reviewers added+removed (alan), approvals grace+alan (author ada cannot), top-level comment + reply + edit, inline ADDED + file-level comments, title/desc edit (UPDATED), merge commit, **resolved task (BLOCKER)** |
| 2  | hotfix/critical | MERGED (ff → merge commit) | reviewer grace; "ff" strategy yields a merge commit on 9.4.18 (`ae33424`), NOT a fast-forward |
| 3  | experiment/squash | MERGED (squash) | squash = merge commit with branch tip as 2nd parent (`72fad40`/`6a304a6`) |
| 4  | feature/stacked/base | MERGED (phantom) | **stacked-pair "remotely merged"**: open PR whose dependent PR5 (descendant branch) was merged; REST gives `state=MERGED` + a **commit-less MERGED activity** (`autoMerge=false`, no `commit`); the admin export omits `hash` entirely. Top-level comment survives |
| 5  | feature/stacked/dependent | MERGED (merge_commit) | **stacked-pair dependent**: merged normally with `hash`; reviewer alan |
| 6  | feature/explore | OPEN (RESCOPED) | inline ADDED/REMOVED/CONTEXT/file-level comments, force-push RESCOPED, drift-re-anchored added-line comment, title edit after push, grace approve→withdraw→re-approve, alan NEEDS_WORK, **open task (BLOCKER)** |
| 7  | feature/declined | DECLINED | ada approve + comment + withdraw, hard-deleted comment, decline activity |

> PR ids 4/5 are the stacked pair (created before PR4/PR5 of the earlier 5-PR
> corpus). PR4/PR5 of that older corpus are now PR 6/7 here. Reviewer states on
> PR 6 are set **last** because the stacked merge (PR 5) advances main and
> resets open-PR reviewer states — the stacked pair is deliberately created and
> merged *before* PR 6's final state ops.

- Commit-level comment on C2 (anchored to src/util.py) — observe whether/how the
  real export encodes it.
- Reviewer-state reset is intentionally exercised: PR1's approvals are set only
  after PR2/PR3 merge; PR4's states only after the RESCOPED push + drift settle.

## Expected REST representation (supervision target, Phase 2)

For each fixture, the exporter must reproduce from REST dumps:

- PR summary fields: `id`, `version`, `state`, `title`, `description`, `author`,
  `reviewers`, `participants[]` (role, approved, status, lastReviewedCommit),
  `fromRef`/`toRef` (displayId + latestCommit), `createdDate`/`updatedDate`/`closedDate`,
  `locked`, `properties.mergeCommit` (in merge response), `links`.
- Activities (`/activities`, paginated): OPENED, UPDATED (title/desc/reviewers),
  APPROVED, UNAPPROVED, REVIEWED (NEEDS_WORK), COMMENTED (+commentAction ADDED/EDITED),
  RESCOPED, MERGED, DECLINED, OPENED.
- Comments (`/comments?path=`, threads via `parent`, `version`, `severity`,
  `state`, `permittedOperations`): top-level, replies, edits, inline anchors
  (`line`, `lineType` ADDED/REMOVED/CONTEXT, `fileType` TO/FROM, `path`,
  `fromHash`/`toHash`, `diffType` EFFECTIVE/COMMIT, `orphaned`), file-level (path-only anchor).
- Merge: strategy + resulting commit; ff leaves no merge commit on this build — the
  exporter must reproduce the merge-commit regardless of the strategy label.
- Tags: lightweight (`v1.0`) vs annotated (`v1.1`) — annotated exposes tagger
  + message (object + peeled `^{}`).
- Branches: which exist after merges (source branch NOT auto-deleted = KNOWN-BAD).

## Admin export archive (ground truth, Phase 2)

Triggered over REST: `POST /rest/api/1.0/migration/exports`
(`{"repositoriesRequest":{"includes":[{"projectKey":"FIX","slug":"golden"}]}}`),
poll `GET /rest/api/1.0/migration/exports/<jobId>` until `COMPLETED`; archive lands in
`$BITBUCKET_SHARED_HOME/data/migration/export/Bitbucket_export_<jobId>.tar` (plain tar,
not gzip). Extracted into `ground-truth/export-a/`.

Archive layout (repo `id` = 15):

- `com.atlassian.bitbucket.server.bitbucket-instance-migration_instanceDetails/instance-details.json.atl.gz`
  → product/version/build/`archiveVersion: 2`/`nodeId`/instance name.
- `..._metadata/project_1/project.json.atl.gz` → `{description,id,key,name,public,type}`.
- `..._metadata/project_1/repository_15.json.atl.gz` → `{forkable,hierarchyId,id,name,projectId,public,scmId,slug}`.
- `..._permissions/project/1/{all-permissions,permissions}.json.atl.gz`, `..._permissions/repository/15/permissions.json.atl.gz`.
- `com.atlassian.bitbucket.server.bitbucket-git_git/repositories/15/{contents/objects.atl.tar, metadata/metadata.atl.tar.atl.gz, hooks/hooks.atl.tar.atl.gz}`.
- `..._bitbucket-git-lfs_gitLfsSettings/15/git-lfs-settings.json.atl.gz`.
- `..._pullRequests/repository/15/pullrequest/{id}/metadata.json.atl.gz` + `activities.json.atl.gz`;
  `com.atlassian.bitbucket.server.bitbucket-git_gitPullRequests/repositories/15/pullrequests/{id}/caches.atl.tar.atl.gz`.
- `_/repository/hierarchy_{begin,end}/<hierarchyId>` markers.

Verified pairing facts (`corpus/compare.py`):

- PR metadata `state`/`title`/`description`/`fromRef.latestCommit`/`toRef.latestCommit`
  exactly match the REST `pull-requests?state=ALL&withAttributes=true` dumps (PR1-7 all match;
  note the stacked pair PR4 flips to `state=MERGED` in BOTH views, so the pairing holds there too).
- **Archive activities use a DIFFERENT schema than REST `/activities`**:
  archive records `kind` ∈ `COMMENT:ADDED`, `COMMENT:OTHER` (comment edits), `ACTIVITY`,
  `REVIEWERS:UPDATED` (review/approve state changes), `RESCOPED`, `MERGED`;
  REST uses `action` ∈ `OPENED`, `UPDATED`, `COMMENTED`, `APPROVED`, `UNAPPROVED`,
  `REVIEWED`, `RESCOPED`, `MERGED`, `DECLINED`. Counts therefore differ:
  PR1 archive=15 rest=12, PR6 archive=14 rest=13; PR2/3/4/5/7 equal. Archive activity count
  is NOT derivable 1:1 from REST activities — Phase 3 must map each archive `kind` back
  to the REST events that produced it (e.g. a REST `COMMENTED` with a comment edit yields
  one `COMMENT:ADDED` + `COMMENT:OTHER`).
- **Stacked-pair phantom merge (PR4)**: the export's PR4 activities =
  `COMMENT:ADDED` + `ACTIVITY/OPENED` + `MERGED` (no `hash` key); REST = OPENED +
  COMMENTED + MERGED (no `commit`). Exactly one `MERGED` in each view. Verified
  byte-exact via gate1 (PR4 activities match with no diff notes).