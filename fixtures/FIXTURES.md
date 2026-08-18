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
- Merging does **NOT** auto-delete the source branch (`KNOWN-BAD` case to study in
  the real export).
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
- `v1.0` lightweight tag on C1.

## PR layer (prs.sh)

| PR | branch → main | state | REST representation to reconstruct |
|----|--------------|-------|-------------------------------------|
| 1  | feature/login | MERGED (merge_commit) | reviewers added+removed (alan), approvals grace+alan (author ada cannot), top-level comment + reply + edit, inline ADDED + file-level comments, title/desc edit (UPDATED), merge commit |
| 2  | hotfix/critical | MERGED (ff) | fast-forward, no merge commit; reviewer grace |
| 3  | experiment/squash | MERGED (squash) | single squash commit |
| 4  | feature/explore | OPEN (RESCOPED) | inline ADDED/REMOVED/CONTEXT/file-level comments, force-push RESCOPED, drift-re-anchored added-line comment, title edit after push, grace approve→withdraw→re-approve, alan NEEDS_WORK |
| 5  | feature/declined | DECLINED | ada approve + comment + withdraw, hard-deleted comment, decline activity |

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
- Merge: strategy + resulting commit; ff leaves no merge commit.
- Tags: lightweight (`v1.0`) vs annotated (`v1.1`) — annotated exposes tagger
  + message (object + peeled `^{}`).
- Branches: which exist after merges (source branch NOT auto-deleted = KNOWN-BAD).