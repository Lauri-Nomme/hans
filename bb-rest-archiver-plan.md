# REST→Archive: A Bitbucket Export Format Reconstruction Tool

## Objective

Build a tool that reconstructs a valid Bitbucket Data Center migration export archive
(`Bitbucket_export_<jobId>.tar`) using **only read-only REST API access + git mirror clones**
against a target Bitbucket instance, such that the archive is accepted by GitHub Enterprise
Importer (`gh bbs2gh migrate-repo --archive-path ...`).

## Ground rules for the implementing agent

1. **Never guess semantics.** Every format claim must be backed by a ground-truth artifact from
   Phase 2, or explicitly marked `[HYPOTHESIS]` in the spec doc.
2. **Pin versions.** The lab instance MUST match production's version (9.4.18) so REST payloads
   and export format match exactly. If the exact Docker tag is unavailable, use the closest
   9.4.x and record the delta as a risk.
3. **Preserve everything, synthesize nothing silently.** When production data is unrecoverable
   (e.g. GC'd commits), the tool must emit a warning entry in a manifest, not fabricate data.
4. All artifacts (fixtures, dumps, archives, diffs) go into a versioned workspace dir; this is
   the audit trail.

## Background: verified facts

- Export endpoint (admin-only): `POST /rest/api/latest/migration/exports` with body
  `{"repositoriesRequest":{"includes":[{"projectKey":"PRJ","slug":"repo"}]}}`; wildcards `*`
  allowed; optional `"exportLocation"`. Poll `GET /rest/api/latest/migration/exports/{jobId}`
  until state `EXPORTED`. Archive lands at
  `<shared-home>/data/migration/export/Bitbucket_export_<jobId>.tar`.
- A matching **import endpoint exists** (`/rest/api/latest/migration/imports`) for DC-to-DC
  migration — usable locally for round-trip validation. Resolve exact method/params from
  Atlassian's "Importing" docs during Phase 0.
- `gh bbs2gh migrate-repo --archive-path <tar>` consumes the Atlassian-format tar directly;
  upload/splitting is handled by the CLI (source: github/gh-gei, open source, C#).
- GEI requires source version 5.14+; the archive presumably carries an app-version marker —
  must be set to `9.4.18`.
- Known GEI gap: merged PRs whose head branch was deleted on the source lose their git refs and
  fail to migrate (can 500 on GitHub side). This applies equally to synthetic archives — flag
  affected PRs, don't try to fix them.
- GEI destination must be a GitHub Enterprise Cloud org; trial migrations are free/unlimited.
- Migration archives are plain tars of bare git repos + JSON metadata; no signing/provenance
  checks are publicly known. `[VERIFY in Phase 3]`

## Phase 0 — Lab environment

- Docker: `atlassian/bitbucket` image, tag pinned to 9.4.x. Run TWO containers (`bb-lab-a`,
  `bb-lab-b`) for export/import round-trip validation. ~4 GB RAM each; startup takes minutes.
  ```bash
  docker run -v bb_a:/var/atlassian/application-data/bitbucket \
    --name bb-lab-a -d -p 7990:7990 -p 7999:7999 atlassian/bitbucket:<9.4.x>
  ```
- License: Bitbucket **Data Center** evaluation license from my.atlassian.com (manual step;
  export/import requires a DC license — a Server license will not do).
  Automation option: Bitbucket supports unattended setup via `bitbucket.properties`
  (`setup.license`, `setup.baseUrl`, `setup.displayName`, ...) — use it so lab bring-up is
  fully scripted and repeatable.
- Shared home per container is the mounted volume; confirm the export dir path inside it.
- Deliverable: `lab-up.sh` / `lab-down.sh`, `SEED.md` describing the scripted setup.

## Phase 1 — Fixture corpus ("golden repo")

Script ALL of the following via REST + git pushes (fully reproducible, idempotent re-runnable).
Use ≥3 distinct users with distinct display names/emails (they become GEI mannequins later).

Git layer:
- [x] commits with crafted `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` and distinct author/committer
- [x] branches, tags (lightweight + annotated)
- [x] a merge commit, a squash merge, a fast-forward merge
- [ ] an LFS object IF production uses LFS (otherwise skip — GEI doesn't migrate LFS anyway)

PR layer (the core):
- [x] PR in each terminal state: MERGED, DECLINED, OPEN
- [x] approvals, un-approvals (approval withdrawn), NEEDS_WORK status
- [x] reviewers added/removed mid-review
- [x] top-level comments; comment replies (threads); comment edits; a deleted comment `[VERIFY:
      visible in activities? in export?]` — REST delete is HARD on 9.4.18 (no trace; see FIXTURES.md)
- [x] inline comments anchored on: added line, removed line, context line, file-level; on a file
      that was later force-pushed away (orphaned anchor) — drift processor re-anchors instead of
      orphaning on 9.4.18; the anchor's fromHash/toHash shift is captured (see FIXTURES.md)
- [x] force push on PR branch (RESCOPED activity), including one that removes previously
      commented-on commits
- [x] PR title and description edits (UPDATED activity)
- [ ] PR tasks, if production uses them — tasks API 404s on 9.4.18 (not in corpus; re-check)
- [x] unicode/emoji/markdown in all text fields; @mentions
- [x] a merged PR followed by source-branch deletion (KNOWN-BAD case — study what real export
      does with it)
- [x] commit-level comments on the repo (not expected to migrate, but observe their export form)

Deliverable: `fixtures/` scripts + `FIXTURES.md` enumerating each fixture with its expected
REST representation.

## Phase 2 — Paired capture (the supervision signal)

For the golden repo, capture BOTH views of every entity:

1. **REST dumps**: full JSON responses, saved raw, for:
   - `GET /rest/api/1.0/projects/{key}/repos/{slug}/pull-requests?state=ALL&withAttributes=true`
   - `GET .../pull-requests/{id}` and `/activities` (paginated fully), `/comments`, `/commits`,
     `/diff`, `/merge`
   - `GET .../commits/{sha}/comments` (commit comments)
   - `GET .../branches`, `/tags`
   - author user objects as embedded in the above (note: `/rest/api/1.0/users` may be
     admin-only — check whether full user enumeration needs admin; if so, the tool must harvest
     users from author/commenter fields. `[VERIFY]`)
2. **Real export**: run the admin export on bb-lab-a, extract the tar into
   `ground-truth/export-a/`.

Deliverable: `corpus/` directory with REST dumps beside the extracted ground-truth archive,
indexed by a manifest mapping fixture → rest-dump-file → archive-region (filled in Phase 3).

Phase 2 complete:

- [x] `corpus/capture.py` — raw REST dumps for project/repo/users/branches/tags, per-PR detail
      + activities + per-path comments + commits + diff, per-commit detail + changes + per-path
      commit comments; writes `corpus/manifest.json` (file → endpoint).
- [x] Real admin export triggered via `POST /rest/api/1.0/migration/exports` (job 1), extracted
      into `ground-truth/export-a/` (archive layout documented in `fixtures/FIXTURES.md`).
- [x] `corpus/compare.py` — pairing check: archive PR metadata matches REST dumps; activity
      counts differ by design (archive `kind` schema ≠ REST `action` schema) — see FIXTURES.md.
- [x] Fixed fixture bug: C2 (`feat: add core utility library`) was an empty commit — missing
      `git add src/util.py`; hotfix now MODIFIES util.py instead of ADDing it (new corpus SHAs).

## Phase 3 — Format reverse engineering

Produce `FORMAT_SPEC.md`. Method:

1. Inventory the tar: file tree, per-file type (JSON? binary? bare repo?), tar metadata
   (mtimes, ordering — likely irrelevant, but confirm).
2. Find version/manifest markers: app version string, export format version, checksums or
   per-file hashes anywhere. `[CRITICAL: if a manifest hashes contents, document the algorithm
   and which fields it covers]`
3. Build the field-correspondence table: for EVERY fixture entity, locate it in the archive and
   map each archive field to its REST source field. Pay special attention to:
   - **ID namespaces**: REST numeric IDs vs archive keys; how cross-references are expressed
   - **timestamps**: epoch millis? ISO strings? which activity subtypes carry which fields
   - **users**: which user attributes the archive carries (id, slug, displayName, email —
     email matters most: GitHub attribution/mannequins key off it)
   - **activity enum**: COMMENT / RESCOPED / APPROVED / MERGED / DECLINED / UPDATED mapping,
     including payload sub-fields per type (e.g. RESCOPED from/to hashes, added/removed commit
     lists)
   - **inline comment anchors**: how diff position is serialized (path, line, lineType,
     fileType, source/destination), and how orphaned anchors are represented
   - **merge commit / state fields**: how MERGED records the merge SHA; how the archive links
     PRs to refs
   - **git layout**: exact in-tar path pattern for bare repos; are refs beyond branches/tags
     included (PR refs like `refs/pull-requests/*/from` and `/merge` — Bitbucket Server keeps
     these! Check whether real exports include them, because they directly affect GEI's
     merged-PR handling)
4. Byte-level hygiene: compression? (plain tar vs tar.gz), JSON canonicalization, key ordering,
   null-vs-absent fields.
5. For anything NOT visible via REST that appears in the archive: document it, find its
   derivation, or decide a safe default + warning.

Deliverable: `FORMAT_SPEC.md` — the single source of truth for Phase 4.

Phase 3 complete:

- [x] Archive inventory + byte-level hygiene: plain tar, per-file gzip with zeroed MTIME,
      two serializers (pretty vs compact-alphabetical JSON), no trailing newlines.
- [x] Version/marker: `archiveVersion: 2`, `version: 9.4.18`, `buildVersion: 9004018`,
      `nodeId` = export job node. **No checksums/manifest anywhere** — nothing pins JSON bytes.
- [x] Full field-correspondence tables in `FORMAT_SPEC.md` (project/repo/PR metadata,
      activities with all `kind` schemas, anchors, permissions, git layout, PR caches).
- [x] **Merge-strategy reality check (9.4.18, verified live on scratch repo ZZ/scratch):**
      `ff`/`squash`/`merge_commit`/unknown ALL produce a two-parent merge commit;
      `ff` is a content-fast-forward recorded as a merge commit; unknown strategies are
      silently accepted; merge-commit author = PR author, committer = merger. The
      fixture's "PR2 = ff, no merge commit" claim was wrong — corrected in FIXTURES.md.
- [x] PR refs: `refs/pull-requests/*/from` only for OPEN PRs; `stash-refs/pull-requests/*/from`
      for ALL PRs; **no `/merge` refs** in the archive (risk #2 partially resolved — GEI
      must not need them; still validate in Gate 3).
- [x] Dropped items confirmed: repo commit-level comments and hard-deleted comments are
      NOT in the archive; target-only advances bump `rescopedTimestamp` without a RESCOPED
      activity.

## Phase 4 — The archiver tool

**COMPLETE** — `bb_archiver/` (Python, per user preference): `scrape`, `assemble`,
`validate` subcommands, worked end-to-end.

- [x] `scrape` — crawler (project/repo/branches/tags/PRs+activities+diff+comments,
      per-commit detail+changes+comments) + git mirror (refs/heads, refs/tags,
      refs/pull-requests, refs/stash-refs). Writes `index.json` + raw dumps.
- [x] `assemble` — Jackson-compatible JSON writer (pretty for instance-details/PR
      metadata/activities, compact-alphabetical for metadata/permissions/lfs; non-BMP
      emoji escaped as UPPERCASE `\uXXXX` surrogate pairs, BMP non-ASCII raw UTF-8,
      no trailing newlines); bare-repo skeleton (HEAD/config/app-info.gc.pid/refs/
      stash-refs/reflogs); loose-object tar via `git repack -adf` + `unpack-objects`;
      PR metadata/activities (derived from REST, ordering = comments-first then
      chronological); caches (`cached-ancestor.txt` = fromTip,toTip,mergeBase).
- [x] `validate` — schema self-check (paths + gzip headers).
- [x] CLI `bb-archiver scrape/assemble/validate` works on bb-lab-a; archive assembled
      to `Bitbucket_export_synth_*.tar`.

## Phase 5 — Validation harness

- [x] **Gate 1 — golden master (PASS)**: `corpus/gate1.py` assembles FIX/golden from
      REST + mirror and semantic-diffs against the real admin export (job 1).
      No genuine differences. Benign, documented divergences only:
      inner-tar mtimes/mode-type-bits, `rescopedTimestamp` internal target-advance
      refresh, real-only `ACTIVITY/UPDATED` for title/desc edits (REST cannot expose),
      `allParticipants` order (DB position), `nodeId`/`instanceName` (instance
      specific). **Explicit task-fidelity check**: severity-BLOCKER comments
      (id/severity/state/resolvedTimestamp/resolverId) must match; corpus now has a
      resolved task (PR1) + open task (PR4).
- [x] **Gate 2 — round-trip import (PASS)**: `corpus/gate2.py` — imports the synth
      archive into bb-lab-b, scapes both lab instances, and scripted-diffs:
      PR state/title/refs/reviewers/participants (slug-keyed), activities, BRANCHES,
      TAGS, and git OBJECT-WISE (54 objects identical set+content via
      `git cat-file`). **Task check**: BLOCKER comments survive with severity/state/
      resolvedDate/resolver. Users import as stub accounts (displayName=slug,
      active=false, no email) — expected Bitbucket behavior; git author emails are the
      only email fidelity. Commit-level comments correctly absent (mirrors real export).
      Reproduced with `corpus/gate2.py scrape_a scrape_b`.
- [x] **Gate 2B — round-trip through the OFFICIAL exporter (PASS)**: `corpus/gate2b.py`
      re-exports bb-lab-b with `POST /rest/api/1.0/migration/exports` and compares the
      resulting tar against the golden bb-lab-a export — surfacing everything the
      official path preserves that the REST scrape cannot see. All git objects
      identical; metadata/permissions/lfs equal after normalizing instance ids, nodeId,
      stub-user identities (`slug|displayName||type`→slug) and reallocated comment ids;
      the only genuine diff is the title/desc-edit `ACTIVITY/UPDATED` events (lost
      through the archive→import→export path because REST hides them). **Task check**:
      BLOCKER comments reproduce exactly through the official re-export. Run with
      default args against bb-lab-b (needs `sudo nerdctl`). NOTE: repo ids are
      instance-specific (A=2, B=1 on the current lab) — pass `--repo-id-real/--repo-id-syn`.
- [x] **Gate 3 — end-to-end GEI (PASS)**: the tool's archive (`/tmp/opencode/synth-a4.tar`)
      was accepted by `gh bbs2gh migrate-repo --archive-path ... --use-github-storage`
      into the `unapplicable` org (now on Enterprise trial) and migrated successfully
      (1 benign warning: "Allow Forking" not enabled at org level). Verified with
      `corpus/gate3.py`: PR states + merged flags (1/2/3 merged, 4 open, 5 declined),
      all 6 branches, tags v1.0 (lightweight) + v1.1 (annotated), main tip =
      PR1 merge commit `a668497c…` — **git SHAs byte-identical** to the golden corpus;
      review states (alan/grace APPROVED, alan CHANGES_REQUESTED on PR4),
      comment bodies (unicode/emoji/@mentions) and inline anchors preserved;
      git author identities intact (Ada Lovelace/Grace Hopper/Alan Turing + emails).
      GEI note: no `migrate` PAT scope exists (my earlier claim was wrong) — the required
      scopes are `repo`, `read:org` (migrator role) or `admin:org` (owner), `workflow`;
      the real prerequisite is an Enterprise-plan org + owner/migrator role.
      `corpus/gate3.py` is **scale-ready** (10k PRs / 100k commits): rate-limit-aware
      client (backoff on 429/5xx, ETag 304, `Link:` pagination, X-RateLimit-Reset
      sleeping), resumable `--deep` per-PR review/comment checks persisted to a state
      file (Ctrl-C safe, runs across rate windows), git `ls-remote` ref compare + full
      object-wise compare against the scrape mirror, progress reporting, and
      mannequin-tolerant reviewer-set comparison.

## Phase 6 — Production runbook

1. Read-only PAT on prod; crawl project/repo inventory.
2. Scrape + mirror per repo (checkpointed; parallelize across repos).
3. Assemble one archive per repo.
4. Gate 3 on a sample of repos (including the messiest: most PRs, most force pushes).
5. Freeze window → final scrape delta (re-run crawler; it's incremental) → re-assemble →
   production migration waves.
6. Post-migration: mannequin reclaim mapping (CSV of prod slug → GitHub user), verification
   report from `warnings.jsonl`.

1. Read-only PAT on prod; crawl project/repo inventory.
2. Scrape + mirror per repo (checkpointed; parallelize across repos).
3. Assemble one archive per repo.
4. Gate 3 on a sample of repos (including the messiest: most PRs, most force pushes).
5. Freeze window → final scrape delta (re-run crawler; it's incremental) → re-assemble →
   production migration waves.
6. Post-migration: mannequin reclaim mapping (CSV of prod slug → GitHub user), verification
   report from `warnings.jsonl`.

## Risks / open questions (resolve empirically, in order)

1. Does the archive contain content checksums that pin exact JSON serialization? (Gate 1 will
   reveal.)
2. Are PR refs (`refs/pull-requests/*`) in exported repos, and does GEI rely on them for
   merged-PR import? (May be the difference between success and the known 500s.)
3. `/rest/api/1.0/users` IS enumerable as a normal user on 9.4.18 (resolved). Use it, but also
   harvest per-entity author/commenter objects.
4. Does GEI validate the archive's app-version marker against a supported range, and is 9.4.18
   the right value to claim?
5. Import-endpoint round trip: exact API shape (Atlassian "Importing" docs).
6. Scale: archive size limits (GEI documents ~10 GB/repo archive limits) — chunk strategy for
   giant repos.
7. Reviewer-state model: Bitbucket resets approvals/NEEDS_WORK on ANY PR mutation (push,
   target advance, title edit) and a drift processor re-anchors inline comments async — the
   exporter must read final participant state + full activity log, not assume consistency.
8. PR tasks: there is NO `/tasks` REST endpoint on 9.4.18 (the earlier "tasks 404"
   finding), BUT tasks exist as `severity:"BLOCKER"` comments (verified live). The
   exporter round-trips them (REST `resolvedDate`/`resolver` -> archive
   `resolvedTimestamp`/`resolverId`). Fixture PR1 now creates + resolves a task.
9. Deleted comments: REST delete is hard (no activity trace) — the exporter can only observe
   what remains; confirmed the real exporter also drops them.

## Non-goals

- LFS contents, CI config, branch permissions, repo settings (GEI doesn't migrate these from
  real exports either — parity, not a gap).
- Bitbucket Cloud sources.
- Forging GitHub-format migration archives (considered and rejected: the Atlassian format has
  locally-producible ground truth; the GitHub format does not).
