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

## Phase 4 — The archiver tool

CLI sketch:
```
bb-archiver scrape   --base-url https://bb.prod --token $READONLY_PAT \
                     --project PRJ --repo my-repo --out ./scrape/my-repo/
bb-archiver assemble --scrape ./scrape/my-repo/ --mirror ./mirrors/my-repo.git \
                     --app-version 9.4.18 --out Bitbucket_export_synth_<n>.tar
bb-archiver validate --archive Bitbucket_export_synth_<n>.tar   # schema self-check vs FORMAT_SPEC
```

Modules:
- **crawler**: paginated pulls of all Phase-2 endpoints; rate-limit/backoff; resumable
  (checkpoint per repo); records `warnings.jsonl` for any unrecoverable data.
- **git-fetcher**: `git clone --mirror` per repo. NOTE: Bitbucket Server exposes PR refs —
  fetch `+refs/pull-requests/*:refs/pull-requests/*` too; they may be required for merged-PR
  fidelity `[VERIFY against ground-truth archive]`.
- **model**: internal entity graph (users, repos, PRs, activities, comments) with stable
  synthetic ID allocation consistent across runs (hash of natural keys, not random).
- **emitter**: serializes the model per FORMAT_SPEC; assembles bare repo(s) into the tar at the
  exact path pattern; sets version markers; produces any manifest/checksums the format demands.
- **user harvester**: collects distinct users from all author/commenter/reviewer fields;
  preserves slug, displayName, emailAddress (email fidelity is what makes GitHub-side
  attribution/mannequin reclaim work).

Edge cases to handle explicitly:
- RESCOPED activities referencing GC'd commits → keep activity if format tolerates dangling
  SHAs `[VERIFY in lab]`, else drop + warn.
- Merged PR + deleted source branch → replicate whatever the real exporter does (Phase 1
  fixture) + warn; do not attempt repair.
- Deleted comments/activities invisible to REST → document as fidelity gap.

## Phase 5 — Validation harness (three gates)

Gate 1 — **Golden master** (no external deps):
Scrape bb-lab-a via REST → `assemble` → tool archive `A_tool`. Real admin export → `A_real`.
Semantic-diff them: normalized tar listings; canonicalized JSON entity-by-entity; git repos
compared object-wise (`git cat-file --batch-all-objects`, not bytes). Iterate until the diff
contains only benign entries (job ids, export timestamps). This is the primary dev loop.

Gate 2 — **Round-trip import**:
Import `A_tool` into bb-lab-b via the migration import endpoint. Then REST-scrape bb-lab-b and
diff against the bb-lab-a scrape. Any loss here = format violation the lab caught for free.

Gate 3 — **End-to-end GEI** (throwaway Enterprise Cloud trial org):
`gh bbs2gh migrate-repo --archive-path A_tool ...` per repo. Verify on GitHub: PR states,
merge SHAs, comment bodies AND timestamps, inline anchors, force-push events, mannequin
identities per lab user, and the known-bad deleted-branch PR behavior. Reclaim mannequins in
the trial org to confirm attribution mapping works end-to-end.

## Phase 6 — Production runbook

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
8. PR tasks API is absent on 9.4.18 (404) — if production exposes it, re-open the fixture;
   otherwise tasks are out of scope.
9. Deleted comments: REST delete is hard (no activity trace) — the exporter can only observe
   what remains; confirm the real exporter's behavior in Phase 3.

## Non-goals

- LFS contents, CI config, branch permissions, repo settings (GEI doesn't migrate these from
  real exports either — parity, not a gap).
- Bitbucket Cloud sources.
- Forging GitHub-format migration archives (considered and rejected: the Atlassian format has
  locally-producible ground truth; the GitHub format does not).
