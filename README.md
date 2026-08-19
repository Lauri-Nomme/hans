# hans

> **HA-not-supported**: the production claim that motivates this project.

`hans` reconstructs a valid **Bitbucket Data Center migration export archive**
(`Bitbucket_export_<jobId>.tar`) using **only read-only REST API access and git
mirror clones** — no admin privileges required — so the archive is accepted by
GitHub Enterprise Importer (`gh bbs2gh migrate-repo --archive-path ...`).

Every piece of export *content* is derivable
from endpoints readable by normal users. `hans` removes the admin dependency.

## How it solves the goal

The admin-only migration export endpoint produces a plain tar of bare git repos +
JSON metadata, with **no checksums, no signatures, no byte-pinning**. The export
format is fully reverse-engineerable, so `hans` re-derives the exact same archive
from data a normal user *can* read:

1. **Scrape** — crawl the REST API (project/repo/branches/tags, every PR with its
   activities, diff, comments, commits) and git-clone a mirror of the repo
   including PR refs (`refs/pull-requests/*`, `refs/stash-refs/*`).
2. **Assemble** — serialize the scraped model into a byte-faithful migration
   archive: instance/project/repo/permission metadata, bare-repo skeleton with
   loose git objects, PR metadata + activities + caches, all in the exact format
   the real exporter emits.
3. **Validate** — schema self-check of the assembled tar.

Correctness is not assumed: every format claim was reverse-engineered from a
**real admin export** (the "golden" artifact) and validated with three scripted
gates.

## Internal architecture

### Pipeline

```
bb-archiver scrape    -> scrape dir (REST dumps + index.json + git mirror)
bb-archiver assemble  -> Bitbucket_export_synth_<n>.tar
bb-archiver validate  -> schema self-check of the tar
```

`bb_archiver/` (Python):

- `scrape.py` — crawler. Pulls raw REST JSON for project, repo, branches, tags,
  every PR (metadata, activities, commits, diff, per-path comments), every
  commit (detail, changes, per-path commit comments). Harvests distinct users
  from author/commenter/reviewer fields. Then `git init --bare` + fetch of
  `refs/heads/*`, `refs/tags/*`, `refs/pull-requests/*`, `refs/stash-refs/*`.
  Writes `index.json` (ids, entities) and `warnings.jsonl` for anything
  unrecoverable.
- `model.py` — entity model over the scrape dir: loads raw dumps, harvests users
  into `slug|displayName||type` archive id strings, stable id allocation.
- `emitter.py` — the assembler. Implements `FORMAT_SPEC.md` exactly:
  - two Jackson-compatible JSON writers (pretty for instance-details / PR
    metadata / activities; compact-alphabetical for project/repo/permissions/LFS),
    emoji as uppercase surrogate `\uXXXX` escapes, no trailing newlines
  - bare-repo skeleton (HEAD, config, refs/heads, refs/tags, PR refs +
    stash-refs + reflogs)
  - loose git objects via `git repack -adf` + `unpack-objects`
  - PR metadata (from REST fields) and activities (derived from REST activity
    stream via the mapping table in `FORMAT_SPEC.md §7`)
  - PR caches (`cached-ancestor.txt` = fromTip,toTip,mergeBase)
- `jsonwriter.py` — byte-level Jackson-compatible serializer.
- `cli.py` — the `bb-archiver` CLI.

### Ground truth, fixtures, and gates

- `fixtures/` — scripted golden repo ("FIX/golden") on lab instance `bb-lab-a`:
  branches, tags, force pushes, all PR terminal states, approvals, inline
  comments with orphaned anchors, tasks (BLOCKER comments), unicode/emoji,
  merged-PR-with-deleted-branch (known-bad case), and a **stacked-PR pair**
  (base PR flips to a commit-less MERGED when its dependent PR merges).
  Fully reproducible.
- `corpus/` — paired capture of **both views** of every entity: raw REST dumps
  next to the extracted real admin export, indexed by `manifest.json`.
- `ground-truth/export-a/` — the extracted real admin export of FIX/golden, the
  supervision signal every format claim is checked against.
- `FORMAT_SPEC.md` — the reverse-engineered format spec: the single source of
  truth for the assembler. Archive envelope, version markers, ID namespaces,
  timestamps, byte-level JSON hygiene, per-entity field correspondence, REST→
  archive activity mapping, and fidelity gaps.
- `bb-rest-archiver-plan.md` — the full working plan (phases 0–6, risks).

### Validation gates (Phase 5)

- **Gate 1 — golden master**: `corpus/gate1.py` assembles the archive from REST
  + mirror and semantic-diffs it against the real admin export. **PASS**, no
  genuine differences.
- **Gate 2 — round-trip import**: `corpus/gate2.py` imports the synthetic
  archive into lab instance `bb-lab-b`, rescrapes both, and diffs PRs,
  activities, branches, tags, and git objects object-wise. **PASS** — all 7 PRs
  incl. the stacked-pair PR4 phantom-merge (commit-less MERGED survives import),
  65 git objects identical.
- **Gate 2B — official re-export**: `corpus/gate2b.py` re-exports `bb-lab-b`
  with the *official* admin endpoint and compares against the golden export,
  surfacing everything the official path preserves that REST cannot see.
  **PASS** — only genuine diff is title/description-edit `ACTIVITY/UPDATED`
  (round-trip loss) and per-instance hierarchy markers are excluded; PR4's
  commit-less MERGED reproduces exactly.
- **Gate 3 — end-to-end GEI**: `corpus/gate3.py` — the lab's synthetic archive
  was accepted by `gh bbs2gh migrate-repo --archive-path` into the `unapplicable`
  Enterprise-trial org and migrated with **byte-identical git SHAs**, correct PR
  states/merge flags, branches, tags, review states, comment bodies, and author
  identities. **PASS**. (No special "migrate" PAT scope exists; the requirement
  is an Enterprise org + `repo`/`read:org` or `admin:org`/`workflow` token.)

## Usage — the whole procedure

Requirements: Python 3.10+ with `requests`, and a `git` binary. `bb-archiver`
is a thin wrapper that loads `bb_archiver/` from the repo root.

Sources and destinations:

| Thing | What you need |
|---|---|
| Bitbucket source | a normal (non-admin) user's basic-auth or PAT with **repo read** |
| GitHub destination | an **Enterprise-plan org** where you are owner or hold the **migrator role** |
| GitHub PAT (`GH_PAT`) | classic token, scopes `repo`, `read:org` (or `admin:org`), `workflow` |

### Step 1 — scrape the Bitbucket repo (read-only)

```bash
bb-archiver scrape --base https://bb.example.com \
    --user readonly --password "$PASSWORD" \
    --project PRJ --repo my-repo \
    --out ./scrape/my-repo
```

Produces `./scrape/my-repo/`:
- `rest/*.json` — raw dumps of project/repo/branches/tags, every PR
  (metadata, activities, diff, commits, per-path comments), every commit
  (detail, changes, comments)
- `git/` — a bare mirror including `refs/heads/*`, `refs/tags/*`,
  `refs/pull-requests/*`, `refs/stash-refs/*` (the mirror is what carries the
  git author/committer emails that make GitHub attribution work)
- `index.json` — ids + entity manifest

Add `--no-git` if you want the REST part only.

### Step 2 — assemble the migration archive

```bash
bb-archiver assemble --scrape ./scrape/my-repo \
    --out Bitbucket_export_synth_1.tar
```

Full options: `--app-version 9.4.18 --build-version 9004018`,
`--instance-name` (target display name), `--node-id`, `--mtime` (defaults to
export-determined values; only override for byte-reproducibility).

### Step 3 — self-check the archive

```bash
bb-archiver validate --archive Bitbucket_export_synth_1.tar
```

Checks tar structure + gzip headers against `FORMAT_SPEC.md`.

### Step 3b — verify a real bake with no admin export (work box)

When the source Bitbucket admins won't run the official export (so Gate 1/2B
against a golden are moot), import the archive into a local lab container and
compare the *imported* repo against the original scrape with Gate 2. This runs
entirely on the work box — the archive never leaves it.

On the work box (Rancher Desktop/WSL2 provides `nerdctl`):

```bash
# 1. bring up only the import target (bb-lab-b) and drive its wizard
SKIP_A=1 LAB_LICENSE_FILE=/path/to/trial3h.txt ./lab/lab-up.sh
./lab/wizard.sh bb-lab-b

# 2. import the archive (copies into shared migration/import dir, chowns,
#    POSTs the import and polls to completion)
./lab/import.sh ./scrape/Bitbucket_export_SX202608191427_1.tar bb-lab-b

# 3. scrape the imported repo (A = original scrape dir already on this box)
python3 corpus/scrape.py http://localhost:7991 --user admin --password '...' \
    -o ./scrape/sx-lab-b

# 4. round-trip gate: original scrape vs imported (PR-count-agnostic, batch
#    git-object compare — built for 10k PRs / 863k objects)
python3 corpus/gate2.py ./scrape/SX ./scrape/sx-lab-b
```

Expected benign diffs: comment ids reallocated on import, activity ordering
churn, and stub users (displayName=slug) on the target — gate2 keys by slug.
Any other diff is an emitter bug at scale.

### Step 4 — migrate with GitHub Enterprise Importer

Set the GitHub PAT, then:

```bash
export GH_PAT='ghp_...'
gh bbs2gh migrate-repo \
    --github-org ORG --github-repo my-repo \
    --archive-path Bitbucket_export_synth_1.tar \
    --use-github-storage \
    --target-repo-visibility private \
    --bbs-server-url https://bb.example.com --bbs-project PRJ --bbs-repo my-repo
```

Notes on the flags:
- `--archive-path` uploads our locally-built archive — the `--bbs-*` flags are
  still *required* by the CLI but are not used for fetching (no
  `--bbs-username/password` with `--archive-path`).
- There is no "migrate" PAT scope; the required scopes are `repo`,
  `read:org`/`admin:org`, `workflow`.
- Add `--queue-only` to queue and continue, then
  `gh bbs2gh wait-for-migration --migration-id RM_...` to block on it.

### Step 5 — verify the migration (Gate 3)

`corpus/gate3.py` validates the migrated GitHub repo against the scrape output
of the same Bitbucket repo — designed for 10k PRs / 100k commits:

```bash
python3 corpus/gate3.py --scrape ./scrape/my-repo \
    --org ORG --repo my-repo --pat "$GH_PAT"
```

- **git layer** — refs (branches/tags SHAs via `git ls-remote` vs the scrape
  mirror) and optionally full object-wise compare (`git cat-file
  --batch-all-objects`; add `--no-git-objects` to skip the clone-with-fetch).
- **PR layer** — paginated list compare (state via `merged_at`, title, head/base).
- **deep layer** (`--deep`) — per-PR review states and comment-body comparison
  (BB top-level comments must appear in GH issue comments, thread-flattening
  aware). Rate-limit-aware, resumable via `--state` file, progress every
  `--progress-every` PRs.
- Every output line is ISO-timestamped; the client sleeps on rate limits,
  retries 429/5xx, caches ETags, and follows `Link:` pagination.

## Known fidelity gaps

These are inherent to being REST-only — verified against real exports, not
theorized:

- **Title/description-edit `ACTIVITY/UPDATED` events are not recoverable.**
  The real exporter records them, but REST `/activities` does not return them
  (verified live on 9.4.18). The tool's archive therefore contains *fewer*
  `UPDATED` records than a real one. Benign — GEI reads final title/desc from
  PR metadata, not activities.
- **Repo commit-level comments are not exported** by the real exporter either
  (absent from the real archive). Dropped, never synthesized — parity.
- **Hard-deleted comments** leave no trace in REST or in the real archive. Only
  surviving comments migrate.
- **`allParticipants` array order** is an internal DB position, not derivable
  from REST — compared as a set in Gate 1.
- **`rescopedTimestamp` internal target-advance**: target-only advances bump it
  without emitting a RESCOPED activity (verified); the tool approximates it from
  the last REST RESCOPED activity. Benign divergence.
- **Users import as stub accounts** (displayName=slug, no email) — expected
  Bitbucket import behavior. The only email fidelity in the whole archive is in
  git author/committer objects, which is what GitHub attribution / mannequin
  reclaim keys off, so git identities are preserved exactly.
- **`nodeId`** (export job node) and all tar mtimes are export-time artifacts —
  benign Gate-1 noise.
- **PR tasks**: there is no `/tasks` REST endpoint on 9.4.18, but tasks exist as
  `severity:"BLOCKER"` comments and are migrated correctly
  (`resolvedDate`/`resolver` → `resolvedTimestamp`/`resolverId`).
- **Merged PRs whose head branch was deleted** lose their git refs and can fail
  on the GitHub side (applies to real exports too). Flagged, not fixed.
- **Not migrated by design** (parity with real exports): LFS contents, CI
  config, branch permissions, repo settings.

## Repo layout

```
bb_archiver/            the tool (scrape / model / emitter / jsonwriter / cli)
bb-archiver             CLI wrapper script
FORMAT_SPEC.md          reverse-engineered export format spec
bb-rest-archiver-plan.md  the working plan (phases 0–6)
fixtures/               scripted golden-repo builder + FIXTURES.md
corpus/                 REST dumps + gate scripts + manifest
ground-truth/export-a/  extracted real admin export (golden)
lab/                    scripted two-instance lab bring-up (SEED.md)
```

## Status

- Phases 0–4 complete: lab, fixture corpus, paired capture, format reverse
  engineering, working tool.
- Phase 5: **Gates 1, 2, 2B, and 3 all PASS** — see the gates section.
- Phase 6 (production runbook) planned in `bb-rest-archiver-plan.md`.

## Non-goals

- LFS contents, CI config, branch permissions, repo settings.
- Bitbucket Cloud sources.
- Forging GitHub-format migration archives — deliberately rejected: the Atlassian
  format has locally-producible ground truth; the GitHub format does not.
