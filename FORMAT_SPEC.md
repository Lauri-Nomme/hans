# Bitbucket Data Center Migration Export — Format Specification

**Source of truth for Phase 4.** Reverse-engineered from a real export of `FIX/golden`
(job 1) on **Bitbucket Data Center 9.4.18** (`buildVersion 9004018`), produced via
`POST /rest/api/1.0/migration/exports`. Ground truth lives in `ground-truth/export-a/`;
REST dumps in `corpus/rest/`; pairing check in `corpus/compare.py`.

Scope: single project, single git repo. Multi-repo archives repeat the per-repo subtree.

---

## 1. Archive envelope

- **Plain `tar`** (no gzip on the tar itself). Files inside that end `.gz` are
  individually gzipped.
- Tar entries: uid/gid `0/0`, regular files, no dir entries.
- Modes: `0644` for JSON/git-metadata files, **`0400` for loose git objects**.
- mtime of every entry = export start time (all identical).
- Entry order (stable, reproduce it):
  1. `..._instanceDetails/instance-details.json.atl.gz`
  2. `..._metadata/project_<pid>/project.json.atl.gz`
  3. `..._permissions/project/<pid>/all-permissions.json.atl.gz`
  4. `..._permissions/project/<pid>/permissions.json.atl.gz`
  5. `_/repository/hierarchy_begin/<hierarchyId>` (empty file)
  6. `..._metadata/project_<pid>/repository_<rid>.json.atl.gz`
  7. `..._permissions/repository/<rid>/permissions.json.atl.gz`
  8. `..._git_git/repositories/<rid>/metadata/metadata.atl.tar.atl.gz`
  9. `..._git_git/repositories/<rid>/hooks/hooks.atl.tar.atl.gz`
  10. `..._git_git/repositories/<rid>/contents/objects.atl.tar` (uncompressed tar)
  11. `..._git-lfs_gitLfsSettings/<rid>/git-lfs-settings.json.atl.gz`
  12. Per PR (ascending id):
      `..._pullRequests/repository/<rid>/pullrequest/<prid>/metadata.json.atl.gz`,
      `.../activities.json.atl.gz`,
      `..._git_gitPullRequests/repositories/<rid>/pullrequests/<prid>/caches.atl.tar.atl.gz`
  13. `_/repository/hierarchy_end/<hierarchyId>` (empty file)

- **No checksums/manifest anywhere.** The only content integrity is git's own
  SHA-1 addressing in `objects.atl.tar`. No field pins JSON serialization
  (risk #1 resolved: no byte-pinning by GEI; but we still match bytes for Gate 1).

## 2. Version / identity markers

`instance-details.json.atl.gz` (pretty JSON, hand-ordered keys):

```json
{
  "product" : "Bitbucket Data Center",
  "version" : "9.4.18",
  "buildVersion" : "9004018",
  "archiveVersion" : 2,
  "dataCenter" : true,
  "instanceName" : "Bitbucket",
  "nodeId" : "80b56f04-61aa-41f7-98be-22c104c8616c"
}
```

- `archiveVersion` = **2**. `nodeId` = the export job's node (matches the
  `/migration/exports` job response `nodeId`).
- `instanceName` = Bitbucket server display name.
- Must claim a `version` GEI supports. 9.4.18 is the lab truth; validate the
  accepted range in Gate 3.

## 3. IDs and namespaces

- **Project id** (numeric, here `1`) and **repo id** (numeric, here `15`) are
  DB keys; they appear in every path. For synthetic archives, allocate stable
  ids (project id from project listing `id` field; repo id from repo listing `id`).
  Cross-references inside JSON: `project_1/project.json` carries `id:"1"`;
  `repository_15.json` carries `projectId:"1"`, `id:"15"`.
- **hierarchyId**: repo UUID (here `a1f92f6e4287f433820f`) from the repo's
  `hierarchyId` field. Used in hierarchy markers + repo metadata.
- **PR ids**: numeric, in paths + metadata.
- **User ids**: string form **`slug|displayName||type`** (pipe-separated;
  third field always empty in observed data; `type` = `NORMAL`).
  Examples: `ada|Ada Lovelace||NORMAL`, `admin|Lab Admin||NORMAL`.
  Used in: PR `allParticipants`, activity `userId`/`authorId`, permission `userIds`,
  `REVIEWERS:UPDATED` `addedIds`/`removedIds`.
  **Email is NOT carried in the archive's user id strings.** The only email
  fidelity in the whole archive is inside git author/committer objects — which is
  what GitHub attribution/mannequin reclaim keys off. Preserve git identities exactly.

## 4. Timestamps

- Migration JSON (PR metadata, activities, comments, threads): **epoch millis**.
- Git objects: **epoch seconds + tz offset** (standard git).
- Reflogs (`logs/stash-refs/...`): epoch seconds + offset.

## 5. Byte-level JSON hygiene (two serializers!)

Verified at the byte level — the export does NOT use one JSON writer:

| Files | Style |
|---|---|
| `instance-details.json`, PR `metadata.json`, PR `activities.json` | **pretty**, 2-space indent, `"key" : value` (space around colon), arrays as `[ { ... }, { ... } ]`, each field on its own line, **no trailing newline** after final `}` |
| `project.json`, `repository_*.json`, `permissions.json`, `all-permissions.json`, `git-lfs-settings.json` | **compact**, single line, **no trailing newline** |

- **Key order: alphabetical** (Jackson `ORDER_MAP_ENTRIES_BY_KEYS` or alphabetical DTO
  field order) in every pretty/compact JSON **except `instance-details.json`**, whose
  key order is exactly as shown above (hand-authored).
  Examples of alphabetical order: `description,id,key,name,public,type`;
  `authorId,comments,createdTimestamp,id,severity,state,text,thread,updatedTimestamp`.
- gzip on `.atl.gz` files: standard (magic `1f8b`, CM 8, FLG 0), **MTIME zeroed**,
  OS byte 255, default level. Decompressed `.gz` files retain the `.gz` suffix.

## 6. Per-entity field correspondence

### 6.1 Project metadata — `metadata/project_<pid>/project.json.atl.gz`

Compact, alphabetical. Matches `GET /rest/api/1.0/projects/{key}`:

```json
{"description":"Golden fixture corpus","id":"1","key":"FIX","name":"Fixture Project","public":false,"type":0}
```

| archive | REST source |
|---|---|
| `id` | project `id` (numeric as **string**) |
| `key` | `key` |
| `name` | `name` |
| `description` | `description` |
| `public` | `public` |
| `type` | `type` (0 = NORMAL) |

### 6.2 Repo metadata — `metadata/project_<pid>/repository_<rid>.json.atl.gz`

Compact, alphabetical. Matches `GET .../repos/{slug}`:

```json
{"forkable":true,"hierarchyId":"a1f92f6e4287f433820f","id":"15","name":"golden","projectId":"1","public":false,"scmId":"git","slug":"golden"}
```

| archive | REST source |
|---|---|
| `forkable` | `forkable` |
| `hierarchyId` | `hierarchyId` |
| `id` | `id` (numeric as string) |
| `name` | `name` |
| `projectId` | `project.id` (as string) |
| `public` | `public` |
| `scmId` | `scmId` (`git`) |
| `slug` | `slug` |

### 6.3 Permissions

- `permissions/project/<pid>/all-permissions.json.atl.gz` — flags for the calling
  user (export initiator): `{"projectAdmin":false,"projectRead":false,"projectWrite":false}`.
  All false observed even for sysadmin (the migration context user has none).
- `permissions/project/<pid>/permissions.json.atl.gz` — array:
  ```json
  [{"groups":[],"permission":"PROJECT_ADMIN","userIds":["ada|Ada Lovelace||NORMAL","admin|Lab Admin||NORMAL","alan|Alan Turing||NORMAL","grace|Grace Hopper||NORMAL"]}]
  ```
  One entry per permission level (`PROJECT_ADMIN` etc.); `userIds` use the
  `slug|displayName||type` form; `groups` empty (no LDAP groups in lab).
  Alphabetical keys: `groups,permission,userIds`; userIds order = archive order.
- `permissions/repository/<rid>/permissions.json.atl.gz` — array; empty `[]` in lab.

### 6.4 git-lfs settings — `git-lfs_gitLfsSettings/<rid>/git-lfs-settings.json.atl.gz`

Compact: `{"enabled":false}`. (LFS contents are a non-goal; only this flag ships.)

### 6.5 PR metadata — `pullRequests/repository/<rid>/pullrequest/<prid>/metadata.json.atl.gz`

Pretty JSON, alphabetical keys. Example (PR4):

```json
{
  "allParticipants" : [ {
    "role" : "AUTHOR", "status" : "UNAPPROVED", "userId" : "ada|Ada Lovelace||NORMAL"
  }, {
    "lastReviewedCommit" : "51e7999...", "role" : "REVIEWER",
    "status" : "APPROVED", "userId" : "grace|Grace Hopper||NORMAL"
  } ],
  "createdTimestamp" : 1787075613573,
  "description" : "...",
  "draft" : false,
  "fromRef" : { "displayId" : "feature/explore", "id" : "refs/heads/feature/explore",
                "latestCommit" : "51e7999..." },
  "id" : 4,
  "rescopedTimestamp" : 1787075613956,
  "state" : "OPEN",
  "title" : "feat: explore page (rebased for pagination v2)",
  "toRef" : { "displayId" : "main", "id" : "refs/heads/main",
              "latestCommit" : "06cfd630..." },
  "updatedTimestamp" : 1787075614103,
  "version" : 2
}
```

Field map:

| archive | REST source (`pull-requests/{id}?withAttributes=true`) | notes |
|---|---|---|
| `allParticipants` | merge of `author` + `reviewers[]` + `participants[]` | see below |
| `closedTimestamp` | `closedDate` | present only for MERGED/DECLINED |
| `createdTimestamp` | `createdDate` | |
| `description` | `description` | |
| `draft` | `draft` | always false in lab |
| `fromRef` | `fromRef` | `displayId`,`id` (`refs/heads/...`),`latestCommit` |
| `id` | `id` | numeric |
| `rescopedTimestamp` | `updatedDate` at last ref change | present ALWAYS (== createdTimestamp when never re-scoped, PR2/3/5); PR1 shows a target-only advance bumps it with NO RESCOPED activity |
| `state` | `state` | `OPEN`/`MERGED`/`DECLINED` |
| `title` | `title` | |
| `toRef` | `toRef` | snapshot of target at last update |
| `updatedTimestamp` | `updatedDate` | |
| `version` | `version` | PR version counter |

`allParticipants` entry forms (alphabetical keys):
- `{"role":"AUTHOR","status":"UNAPPROVED","userId":...}` (author; status stays UNAPPROVED — author cannot approve)
- `{"lastReviewedCommit":..., "role":"REVIEWER","status":"APPROVED"|"NEEDS_WORK"|"UNAPPROVED","userId":...}`
- `{"role":"PARTICIPANT","status":"UNAPPROVED","userId":...}` (anyone else who touched it — e.g. the merger admin auto-joins)

Roles present = AUTHOR always, REVIEWER for each reviewer, PARTICIPANT for others
(admin auto-added as PARTICIPANT when merging). `status` maps REST `approved`+`status`:
APPROVED→APPROVED, NEEDS_WORK→NEEDS_WORK, else UNAPPROVED. `lastReviewedCommit`
present when the participant has an approval/needs-work state (REST participant
`lastReviewedCommit`). userId order in the array is not semantically meaningful.

### 6.6 PR activities — `pullRequests/.../pullrequest/<prid>/activities.json.atl.gz`

Pretty JSON array. **Ordering rule (verified): all comment records first
(chronological), then all non-comment records (chronological).** Reproduce it.

Record schemas (all alphabetical keys, all carry `createdTimestamp` + `userId`):

**`ACTIVITY`** — generic event:
```json
{ "kind" : "ACTIVITY", "action" : "OPENED", "createdTimestamp" : ..., "userId" : "..." }
```
`action` ∈ `OPENED`, `UPDATED` (title/desc edit), `APPROVED`, `UNAPPROVED`,
`REVIEWED` (NEEDS_WORK set), `DECLINED`. Maps from REST activity `action`
(which uses the same names — see §7 mapping).

**`COMMENT:ADDED`** — full comment snapshot (the current state of the comment):
```json
{
  "kind" : "COMMENT:ADDED", "createdTimestamp" : ..., "userId" : "...",
  "comment" : {
    "authorId" : "...",
    "comments" : [ /* nested replies, recursively same comment schema */ ],
    "createdTimestamp" : ..., "id" : "117",
    "resolvedTimestamp" : ...,   /* tasks only (severity BLOCKER, state RESOLVED) */
    "resolverId" : "...",        /* tasks only */
    "severity" : "NORMAL",       /* NORMAL | BLOCKER (task) */
    "state" : "OPEN",            /* OPEN | RESOLVED */
    "text" : "...",
    "thread" : {
      "anchor" : { ... },               /* inline/file-level only */
      "createdTimestamp" : ..., "resolved" : false, "updatedTimestamp" : ...
    },
    "updatedTimestamp" : ...
  }
}
```
- Comment `id` is a **string**. `state` = `OPEN`/`RESOLVED`.
- **Tasks are comments with `severity: "BLOCKER"`** (verified on 9.4.18; there is NO
  `/tasks` REST endpoint on this build). REST comment carries `state`, `resolvedDate`,
  `resolver` for resolved tasks; the archive carries `resolvedTimestamp` +
  `resolverId` (userId string) instead. Map: `resolvedDate`→`resolvedTimestamp`,
  `resolver.slug`→`resolverId`, `state` passthrough. PR `properties.openTaskCount` /
  `resolvedTaskCount` (REST) are NOT stored in the archive — they are derived
  server-side; only the comments themselves encode tasks.
- Replies are **nested** under `comments[]`, not flattened.
- Top-level comments: `thread` has **no `anchor`**.
- Thread `resolved` reflects thread resolution (NOT task resolution — a resolved
  task can still have `thread.resolved:false`).

**Inline/file-level anchor** (`thread.anchor`, alphabetical keys):
```json
{
  "diffType" : "EFFECTIVE",      /* EFFECTIVE | COMMIT */
  "fileType" : "TO",             /* TO | FROM — present for inline, ABSENT for file-level */
  "fromHash" : "4135001...",
  "line" : 4,                    /* 0 for file-level */
  "lineType" : "ADDED",          /* ADDED | REMOVED | CONTEXT — absent for file-level */
  "orphaned" : false,
  "path" : "explore.md",
  "toHash" : "51e7999..."
}
```
Correspondence to REST comment `anchor`: `diffType`, `fileType`, `path`,
`line`, `lineType`, `orphaned`, `fromHash`, `toHash` — all direct.
**Drift-re-anchored comments** serialize `toHash` = new tip and `orphaned:false`
(the "orphan" comment in the corpus was re-anchored by the async drift processor;
a genuinely orphaned comment would show `orphaned:true` + original hashes).

**`COMMENT:OTHER`** — a comment mutation not carrying a full snapshot:
```json
{ "kind" : "COMMENT:OTHER", "commentAction" : "EDITED", "commentId" : "117",
  "createdTimestamp" : ..., "userId" : "..." }
```
`commentAction` ∈ `REPLIED`, `EDITED`, ... (REPLIED references the reply id;
EDITED references the edited comment id). A comment edit yields BOTH a
`COMMENT:ADDED` (new snapshot with edited text) and a `COMMENT:OTHER/EDITED`.

**`REVIEWERS:UPDATED`** — reviewer list change:
```json
{ "kind" : "REVIEWERS:UPDATED", "createdTimestamp" : ..., "userId" : "...",
  "addedIds" : ["grace|Grace Hopper||NORMAL"], "removedIds" : [] }
```
Note: an APPROVED activity is typically followed by a `REVIEWERS:UPDATED`
adding that reviewer (approval also adds them as reviewer). Title/desc edits do
NOT produce REVIEWERS:UPDATED (only `ACTIVITY/UPDATED`).

**`RESCOPED`** — branch force-push / new commits:
```json
{
  "kind" : "RESCOPED", "createdTimestamp" : ..., "userId" : "...",
  "commits" : [ { "action" : "ADDED", "commitId" : "51e7999..." } ],
  "fromHash" : "51e7999...", "previousFromHash" : "1bd19c0...",
  "previousToHash" : "06cfd630...", "toHash" : "06cfd630...",
  "totalAdded" : 1, "totalRemoved" : 0
}
```
Correspondence to REST `RESCOPED` activity: `previousFromHash`/`fromHash` =
before/after source tip, `previousToHash`/`toHash` = before/after target tip,
`commits[].commitId` = the added/removed commits, `totalAdded`/`totalRemoved` = counts.
(Verify commit-list completeness vs REST in Gate 1.)

**Activity-emission rule (verified)**: a `RESCOPED` **activity** is emitted only when the
**source** ref changes (force-push / new commits on `fromRef`). When only the **target**
advances (e.g. another PR merged, PR1 case), `rescopedTimestamp` is updated but **no
`RESCOPED` activity record is emitted**.

**`MERGED`** — merge result:
```json
{ "kind" : "MERGED", "autoMerge" : false, "createdTimestamp" : ..., "hash" : "06cfd630...", "userId" : "..." }
```
`hash` = the merge commit (main tip after merge) — always present, even for `ff`.

### 6.7 Git layout

Three nested tars:

**`metadata.atl.tar.atl.gz`** = bare-repo skeleton (tar, then gzip). Contains:
- `HEAD` = `ref: refs/heads/main` (21 bytes incl. newline)
- `config` = git config:
  ```
  [include]
  	path = ../../../config/git/system-config
  	path = repository-config
  [core]
  	repositoryformatversion = 0
  	filemode = true
  	bare = true
  ```
  (references config outside the archive — the importer must tolerate missing includes;
  reproduce verbatim)
- `app-info/gc.pid` — gc state, content not semantically relevant (reproduce or omit;
  observe in Gate 1)
- `refs/heads/*` — 41 bytes each (40-hex sha + `\n`) — one file per branch tip
- `refs/tags/*` — same format; **lightweight tag** → commit sha directly; **annotated
  tag** → tag-object sha
- `refs/pull-requests/<prid>/from` — **only for OPEN PRs** (merged/declined removed
  from this namespace)
- `stash-refs/pull-requests/<prid>/from` — **all PRs**, historical source tip at PR
  creation (retained even after merge/decline) ← CRITICAL for GEI merged-PR handling
- `logs/stash-refs/pull-requests/<prid>/from` — reflogs, format:
  `<old> <new> Bitbucket Mesh <bitbucket.mesh@atlassian.com> <epoch> +0000`
  one line per update (create = `0000...` → first tip; then per rescope).
  The `Bitbucket Mesh` identity is what Bitbucket writes for PR-ref updates.

**No `refs/pull-requests/*/merge` in the archive** (verified). Merge results live
only in PR metadata/activities + git merge commits. GEI must not need `/merge` refs
(validate in Gate 3).

**`hooks.atl.tar.atl.gz`** = empty tar (no entries).

**`contents/objects.atl.tar`** (uncompressed tar of the git object store):
- Loose objects laid out as `<2-hexdir>/<38-hex>`, mode `0400`.
- Each file = zlib-compressed git object (blob/tree/commit/tag) named by its SHA-1;
  all 54 objects in the corpus verified sha1-consistent.
- Reachable-object set = everything reachable from `refs/heads/*`,
  `refs/pull-requests/*/from`, `stash-refs/pull-requests/*/from`, `refs/tags/*`
  (tag objects + peeled). No packfiles. Order inside tar: not strictly sorted;
  reproduce deterministically (e.g. sorted by path) — content-addressed so order
  is not load-bearing.
- Note: the PR3 squash merge's 2nd parent `6a304a6` (squashed tip) and PR1's merge
  commit `06cfd630` are present and reachable; branch-tip-only commits that were
  never part of a merged PR are still present because they're referenced by
  `stash-refs` (e.g. `d40c0f4` for declined PR5, `1bd19c0`/`51e7999` for PR4).

### 6.8 PR cache — `git_gitPullRequests/.../pullrequests/<prid>/caches.atl.tar.atl.gz`

Tar containing one file `cached-ancestor.txt`:
`<fromTip>,<toTip>,<mergeBase>` — three 40-hex shas comma-separated, **no trailing
newline** (122 bytes). From the PR's `changes.mergeBase` / diff merge-base.

### 6.9 Hierarchy markers

`_/repository/hierarchy_begin/<hierarchyId>` and `.../hierarchy_end/<hierarchyId>` —
empty files wrapping the repo's entries.

## 7. REST → archive activity mapping (supervision signal)

Archive `kind` is NOT a 1:1 rename of REST `action`. Observed derivations:

| REST activity (`action`) | archive record(s) |
|---|---|
| `OPENED` | `ACTIVITY/OPENED` |
| `UPDATED` (title/desc) | `ACTIVITY/UPDATED` |
| `APPROVED` | `ACTIVITY/APPROVED` + `REVIEWERS:UPDATED` (addedIds += user) |
| `UNAPPROVED` | `ACTIVITY/UNAPPROVED` (no REVIEWERS:UPDATED observed on withdraw) |
| `REVIEWED` (NEEDS_WORK) | `ACTIVITY/REVIEWED` |
| `DECLINED` | `ACTIVITY/DECLINED` |
| `MERGED` | `MERGED` (with `hash` + `autoMerge`) |
| `RESCOPED` | `RESCOPED` |
| `REVIEWERS_UPDATED` | `REVIEWERS:UPDATED` (addedIds/removedIds) |
| `COMMENTED` (ADDED) | `COMMENT:ADDED` (full snapshot) |
| `COMMENTED` (EDITED) | `COMMENT:ADDED` (new snapshot) + `COMMENT:OTHER/EDITED` |
| `COMMENTED` (REPLIED) | `COMMENT:ADDED` of reply + `COMMENT:OTHER/REPLIED` |

Because the archive emits a separate `REVIEWERS:UPDATED` for approvals (and for
any reviewer-list mutation) that REST folds differently, **activity counts differ**
(PR1 archive=15 rest=12, PR4 archive=14 rest=13; PR2/3/5 equal). Phase 4 must
derive archive records from the REST streams using this table, then re-derive
`allParticipants` final state from the last participant snapshot.

## 8. Non-REST-visible / dropped items (fidelity gaps)

- **Commit-level comments (repo commit comments) are NOT exported** (verified absent
  from the archive; REST has them). Dropped, not migrated. Document, do not synthesize.
- **Hard-deleted comments** leave no archive trace (verified: PR5's deleted comment
  absent) — matches REST behavior (hard delete).
- **Tasks ARE migrated** as `severity:"BLOCKER"` comments (there is no `/tasks` REST
  endpoint on 9.4.18; the earlier "tasks 404" finding was correct about the endpoint,
  but tasks exist as BLOCKER-severity comments). Open tasks have `state:OPEN`; resolved
  tasks add `resolvedTimestamp`/`resolverId`.
- **Title/description-edit `UPDATED` activities are recorded by the real exporter but
  NOT returned by REST `/activities`** (verified live on 9.4.18: after a PUT title edit,
  REST still shows only `OPENED`; the archive contains `ACTIVITY/UPDATED` with
  `createdTimestamp` = edit time, `userId` = editor). A REST-only tool CANNOT recover
  the exact timestamps; the ar­chive produced by the tool will therefore contain FEWER
  `UPDATED` records than the real one (a known, benign divergence — GEI reads final
  title/desc from PR metadata, not activities).
- PR tasks: there is NO `/tasks` REST endpoint on 9.4.18 (the earlier "tasks 404"
  finding was about the endpoint) — but tasks DO migrate as `severity:"BLOCKER"`
  comments, which the tool reproduces (see §6.6).
- Branch permissions / CI config / LFS blobs — not exported (non-goals; parity).
- `instance-details.nodeId` (job node) and all mtimes are export-time artifacts —
  benign Gate-1 diff noise.
- Inner tar entry mtimes (git metadata, caches, objects) = export mtime; mode 0644
  (objects 0400). Gate-1 normalizes these.
- `allParticipants` array order is an internal DB position order, NOT derivable from
  REST (REST returns separate `author`/`reviewers`/`participants` lists). Compare as a
  SET in Gate-1.

## 9. Phase 4 implementation notes (from this spec)

- Allocate stable synthetic ids (project/repo/PR) hashed from natural keys.
- Harvest users from REST author/commenter/reviewer fields → `slug|displayName||type`.
- Build bare-repo skeleton: HEAD, config (verbatim), refs/heads, refs/tags,
  refs/pull-requests/*/from (OPEN), stash-refs/*/from (all), reflogs.
- Emit loose objects (zlib, 0400) for every reachable object — `git clone --mirror`
  gives the pack; unpack it.
- Merge commits: author = PR author identity, committer = merger identity, message =
  the documented `Merge pull request #N in K/repo from b to t\n\nMerged in b (pull request #N)\n\n* commit '<sha>':\n  <indented first-lines>`. Fetch `+refs/pull-requests/*:refs/pull-requests/*` from a live server to recover exact merge-commit messages/identities.
- Serialize JSON with the two writers per §5 (pretty for instance-details/PR
  metadata/activities; compact-alphabetical for the rest).
- Gate 1 diff normalization: ignore entry mtimes, tar order, gzip bytes.