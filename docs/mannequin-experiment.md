# Mannequin experiment — do GEI mannequins survive without BB emails?

**Question:** Does GitHub still attribute PR / comment / participant activity to
*reclaimable* GEI mannequins when the archive's user-id strings carry no email?

**Verified answer (live, 2026-08-21):** **Yes.** Repo-level imports produce
`type: Mannequin` users for PR/comment/participant identities even though every
`userId` in the archive is `slug|displayName||NORMAL` with the email slot empty.
The git author/committer email is additionally preserved inside the git objects,
powering commit attribution. The mannequins were then **reclaimed end-to-end**
(more below) — invite the real account into the org by email, reattribute the
mannequin, and commit + PR-comment attribution both flip to the real user.

## Background (why this needs testing)

Bitbucket's official exporter hardcodes the email slot in its user-id strings to
empty — `UserEntityExportMapping.getExportId()` never reads
`getEmailAddress()` (see `FORMAT_SPEC.md` §3 + §6.6). The bb-rest-archiver
reproduces that exactly. So a migrated repo contains **no email for users**; the
only email fidelity is inside git author/committer objects.

A reasonable worry: "if emails are stripped, will GitHub know who the PR
commenters/reviewers were, and will the user be reclaimable?" This
experiment answers it.

## The experiment

Scripted end-to-end in `lab/mannequin-experiment.sh` (idempotent). It:

1. **Fixture** on `bb-lab-b` (port 7991):
   - user `artjom.velosipedov` / `artjom.velosipedov@locals.tf` (active, email set)
   - project `MANI` / repo `manitest`
   - commits authored & committed by that user (email in git objects)
   - PR `feature/vok -> main` created by admin; artjom **comments** on it and is
     added as a **REVIEWER** participant (9.4.18: `/reviewers` 404s; use
     `POST /pull-requests/{id}/participants` with `role:REVIEWER`)
2. **Scrape** via bb-archiver (`--project MANI --repo manitest`)
3. **Assemble** the migration tar
4. **Sanity:** prints each archive `userId` and asserts the email slot is empty
5. **Import** to a GitHub org via `gh bbs2gh migrate-repo --archive-path ...`
   (`--queue-only`, then `wait-for-migration`)
6. **Verify** on github.com:
   - commits carry `Artjom Velosipedov <artjom.velosipedov@locals.tf>`
   - PR comment actor is a `type: Mannequin` user

### Run it

```bash
# lab-only (no GitHub required):
MANI_SKIP_IMPORT=1 ./lab/mannequin-experiment.sh

# full pipeline (needs GH_PAT + enterprise org; see FIXTURES/README):
GH_PAT=/path/to/token ./lab/mannequin-experiment.sh
```

Overridable: `MANI_BASE`, `MANI_USER`, `MANI_EMAIL`, `MANI_PROJECT`,
`MANI_REPO`, `MANI_GH_ORG`, `MANI_GH_PAT`, `MANI_SKIP_IMPORT`, `MANI_WORK`.

## What GitHub shows (observed)

- **Commits**: git author/email preserved (`Artjom Velosipedov
  <artjom.velosipedov@locals.tf>`); REST `.author.login` is `null` (no linked
  GH account).
- **PR comment** (`issues/{n}/timeline`, event `commented`): actor is an opaque
  username like `AHyUu476q81BwrsGr8xFy2EPfqcUdNQvHxX26XT`.
- **Review requests** (event `review_requested`): actor is another opaque
  username — one per distinct BB identity (the PR author's identity also becomes
  a mannequin if that BB user isn't a GH member).
- `GET /users/{opaque}` returns `"type": "Mannequin"` → these are real GitHub
  **mannequin entities**, not bogus.

### What GitHub does NOT show

Mannequins are **not org members**:
- `GET /orgs/{org}/members` lists only real users (no mannequins).
- `orgs/{org}/people` in the UI likewise shows only real members.
- They are not in GraphQL `membersWithRole`.

So "view/reclaim the mannequins" is an **org-owner UI flow** (migrations/mannequin
reclaim), not something exposed via the public REST members endpoints. The
experiment's contract is: the mannequins *exist and are attributed*; running the
reclaim is a manual org-owner action against the GitHub UI.

## Reclaim (mannequin -> real user) — completed end-to-end

Observed live on 2026-08-21. The mannequins are listed at
`https://github.com/organizations/{org}/settings/import-export` beside a
**Reattribute** button. The reclaim has three stages:

1. **Create the target GH account + get it into the org.** Reattributing only
   lets you pick an *existing org member*. Create the account, then invite by
   email (works even before the account exists; matches on the verified contact
   email), and have them accept the membership invite.

   ```bash
   # owner, admin:org PAT:
   GH_TOKEN="$(cat /path/to/token)" gh api --method POST orgs/{org}/invitations \
     -f email="artjom.velosipedov@locals.tf" -f role="direct_member"
   # -> pending until the account accepts
   ```

   Note: `gh api` uses the CLI's own auth by default; export `GH_TOKEN` (or pass
   `-H "Authorization: token $PAT"`) — the `gho_` CLI token here lacked
   `admin:org`.

2. **Reattribute the mannequin to the member** (UI: Import/Export -> Reattribute
   -> pick the member). This creates an **attribution invitation** shown at
   `https://github.com/orgs/{org}/attribution-invitations`.
   - Commit attribution flips **immediately** (the git re-writing happens
     server-side, email-matched).
   - PR/comment/review attribution stays on the mannequin until the target
     **accepts the attribution invitation** (sign in as the target account, open
     that URL, Accept). State was *pending* until then.

3. **After acceptance** everything points at the real user (verified):

   ```bash
   gh api orgs/{org}/members --jq '.[].login'                       # artjom joined
   gh api repos/{org}/manitest/commits --jq '.[] | {sha, author: .author.login, email: .commit.author.email}'
   gh api repos/{org}/manitest/issues/1/timeline --jq '.[] | {event, actor: .actor.login}'
   ```

   Result: PR comment actor `AHyUu476q81BwrsGr8xFy2EPfqcUdNQvHxX26XT` ->
   `atrjom-velosipedov`; commits `983d9b85` + `8db418d4` authored by
   `atrjom-velosipedov` with `commit.author.email = artjom.velosipedov@locals.tf`.

## Caveats

- The `unapplicable` / `admin` BB identities become mannequins too (they aren't
  GH org members). Use a BB user that maps to a real GH identity only if you want
  a *matched* (non-mannequin) result.
- The GH account login does **not** need to match the BB slug — the match is
  email-driven. Here the member is `atrjom-velosipedov` (GH login) while the BB
  user is `artjom.velosipedov`; attribution still resolved because the emails
  match.
- `gh bbs2gh migrate-repo` requires `BBS_USERNAME`/`BBS_PASSWORD` env even when
  using `--archive-path` (it validates them; they're not used for fetching).
- Migration logs (24 h retention) don't expose the mannequin-mapping details.
- Mannequin listing/reclaim is not reachable via REST or GraphQL with a normal
  PAT (introspection is restricted on github.com).