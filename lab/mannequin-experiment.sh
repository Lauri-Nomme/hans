#!/usr/bin/env bash
# ============================================================================
# Mannequin experiment — end-to-end, reproducible.
#
# Question it answers:
#   Do GitHub-side PR/comment/participant users still become *reclaimable GEI
#   mannequins* when Bitbucket user emails are absent from the archive?
#
# Verified background (see FORMAT_SPEC §3 + §6.6):
#   - The official BB export's userId strings are `slug|displayName||NORMAL`
#     with the EMAIL SLOT HARDCODED EMPTY (UserEntityExportMapping never reads
#     getEmailAddress()). It does not depend on user active status.
#   - So the tool's REST-derived archive also carries no email for users.
#   - The ONLY email fidelity is inside git author/committer objects.
#
# This script builds a repo whose commits are authored by a user whose DB email
# is NOT in the archive (only in git objects + a PR comment authored by them),
# scrapes + assembles it, and imports it into a GitHub org via gh bbs2gh. It
# then verifies what GitHub attributed to mannequins.
#
# Pipeline:
#   1. (bb-lab-b) create user <USER>, project <PROJECT>, repo <REPO>
#   2. push commits: artjom-authored commits carrying <EMAIL> in git objects
#   3. create a PR (author=admin), artjom comments on it + is a REVIEWER
#   4. bb-archiver scrape -> scrape/
#   5. bb-archiver assemble -> Bitbucket_export_<REPO>.tar
#   6. (optional, needs GH_PAT) gh bbs2gh migrate-repo -> org <GH_ORG>
#   7. verify: git emails preserved, PR comment author is a Mannequin user
#
# Usage:
#   ./lab/mannequin-experiment.sh             # everything (needs GH_PAT)
#   GH_ORG=unapplicable GH_PAT=/tmp/hans.gei.txt \
#       MANI_SKIP_IMPORT=1 ./lab/mannequin-experiment.sh   # lab-only, no GH
#
# Overridable env:
#   BASE          Bitbucket base (default http://localhost:7991 = bb-lab-b)
#   USER          BB user slug (default artjom.velosipedov)
#   EMAIL         user email, git author email (default artjom.velosipedov@locals.tf)
#   PROJECT       BB project key (default MANI)
#   REPO          BB repo slug + GH repo name (default manitest)
#   GH_ORG        GitHub org to import into (default unapplicable)
#   GH_PAT        path to PAT with repo+admin:org+workflow (default /tmp/hans.gei.txt)
#   MANI_SKIP_IMPORT=1   stop after assemble (no GitHub import)
#   WORK          scratch dir (default /tmp/opencode/mannequin)
# ============================================================================
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/config.env"

# --- overridable configuration ------------------------------------------------
BASE="${MANI_BASE:-http://localhost:${B_HTTP_PORT:-7991}}"
ADMIN="$SYSADMIN_USER:$SYSADMIN_PASSWORD"
USER="${MANI_USER:-artjom.velosipedov}"
EMAIL="${MANI_EMAIL:-artjom.velosipedov@locals.tf}"
PASS="${MANI_PASS:-mani-pw-123}"
DISPLAY="${MANI_DISPLAY:-Artjom Velosipedov}"
PROJECT="${MANI_PROJECT:-MANI}"
REPO="${MANI_REPO:-manitest}"
GH_ORG="${MANI_GH_ORG:-unapplicable}"
GH_PAT="${MANI_GH_PAT:-/tmp/hans.gei.txt}"
WORK="${MANI_WORK:-/tmp/opencode/mannequin}"
SKIP_IMPORT="${MANI_SKIP_IMPORT:-0}"

SCRAPE="$WORK/scrape"
TAR="$WORK/Bitbucket_export_${REPO}.tar"
REPO_R="/rest/api/1.0/projects/$PROJECT/repos/$REPO"

mkdir -p "$WORK"
cd "$WORK"

log() { printf '[mannequin] %s\n' "$*"; }

api() { curl -s -u "$ADMIN" -H 'Content-Type: application/json' -H 'X-Atlassian-Token: no-check' -X "$1" "$BASE$2" ${3:+--data-binary "@$3"}; }
bjson() { python3 -c "import json,sys; print(json.dumps($1))" > "$WORK/.b.json"; echo "$WORK/.b.json"; }
jq_get() { jq -r "$1"; }

need() { command -v "$1" >/dev/null || { echo "[mannequin] missing: $1" >&2; exit 1; }; }
need curl; need git; need python3; need jq

# --- 1. user, project, repo (idempotent) --------------------------------------
if curl -s -u "$ADMIN" "$BASE/rest/api/1.0/users/$USER" | jq -e '.name == "'"$USER"'"' >/dev/null 2>&1; then
  log "user $USER exists"
else
  log "creating user $USER ($EMAIL)"
  curl -s -u "$ADMIN" -X POST -G "$BASE/rest/api/1.0/admin/users" \
    --data-urlencode "name=$USER" --data-urlencode "password=$PASS" \
    --data-urlencode "displayName=$DISPLAY" --data-urlencode "emailAddress=$EMAIL" \
    --data-urlencode "addToDefaultGroup=true" --data-urlencode "notify=false"
fi

if curl -s -u "$ADMIN" "$BASE/rest/api/1.0/projects/$PROJECT" | jq -e '.key == "'"$PROJECT"'"' >/dev/null 2>&1; then
  log "project $PROJECT exists"
else
  log "creating project $PROJECT"
  api POST "/rest/api/1.0/projects" "$(bjson "{'key':'$PROJECT','name':'Mannequin Test','public':False}")" >/dev/null
fi
if curl -s -u "$ADMIN" "$BASE$REPO_R" | jq -e '.slug == "'"$REPO"'"' >/dev/null 2>&1; then
  log "repo $REPO exists"
else
  log "creating repo $REPO"
  api POST "/rest/api/1.0/projects/$PROJECT/repos" "$(bjson "{'name':'$REPO','scmId':'git','forkable':True}")" >/dev/null
fi
# fixture user must be able to comment on the PR
api PUT "/rest/api/1.0/projects/$PROJECT/permissions/users?name=$USER&permission=PROJECT_ADMIN" >/dev/null

# --- 2. git layer: commits authored by $USER ($EMAIL) --------------------------
GITDIR="$WORK/gitd"
rm -rf "$GITDIR"
git init -q -b main "$GITDIR"
cd "$GITDIR"
git config user.name "$DISPLAY"
git config user.email "$EMAIL"

mkcommit() { GIT_AUTHOR_NAME="$1" GIT_AUTHOR_EMAIL="$2" \
  GIT_COMMITTER_NAME="$3" GIT_COMMITTER_EMAIL="$4" \
  GIT_AUTHOR_DATE="$5" GIT_COMMITTER_DATE="$5" git commit -q --allow-empty -m "$6"; }

# main: a scaffold commit (admin, plain identity) then user-authored commits
mkcommit "Lauri Nomme" "lauri.nomme@company.example" "Lauri Nomme" "lauri.nomme@company.example" "2026-01-01T09:00:00+00:00" "chore: scaffold"
mkcommit "$DISPLAY" "$EMAIL" "$DISPLAY" "$EMAIL" "2026-01-02T10:00:00+00:00" "feat: skywheel release"
printf 'sky\n' > wheel.txt
git add wheel.txt
mkcommit "$DISPLAY" "$EMAIL" "$DISPLAY" "$EMAIL" "2026-01-03T11:00:00+00:00" "feat: add wheel file"

# feature/vok off main, a user-authored commit
git checkout -q -b feature/vok main
printf 'vok\n' > vok.txt
git add vok.txt
mkcommit "$DISPLAY" "$EMAIL" "$DISPLAY" "$EMAIL" "2026-01-04T12:00:00+00:00" "feat: vok integration"

git checkout -q main
git push -q "http://$ADMIN@$(echo "$BASE" | awk -F/ '{print $3}')/scm/$PROJECT/$REPO.git" "+refs/heads/main" "+refs/heads/feature/vok"
curl -s -u "$ADMIN" -X PUT "$BASE$REPO_R/default-branch" -H 'Content-Type: application/json' -d '{"id":"refs/heads/main"}' >/dev/null
log "git pushed (main + feature/vok)"

# --- 3. PR by admin, comment + reviewer by $USER --------------------------------
cd "$WORK"
PRB="$(bjson "{'title':'vok feature','description':'integrate vok','state':'OPEN','fromRef':{'id':'refs/heads/feature/vok','repository':{'slug':'$REPO','project':{'key':'$PROJECT'}}},'toRef':{'id':'refs/heads/main','repository':{'slug':'$REPO','project':{'key':'$PROJECT'}}}}")"
RAW="$(api POST "$REPO_R/pull-requests" "$PRB")"
PR_ID="$(printf '%s' "$RAW" | jq -r 'if .id != null then .id else empty end')"
if [[ -z "$PR_ID" ]]; then
  # rerun on an already-populated repo: reuse an existing open PR from this branch
  PR_ID="$(api GET "$REPO_R/pull-requests?state=OPEN&limit=100" | jq -r --arg b 'feature/vok' '.values[] | select(.fromRef.displayId == $b) | .id' | head -1)"
  log "PR create skipped (already exists); reusing PR id=$PR_ID"
else
  log "PR created id=$PR_ID"
fi
[[ -n "$PR_ID" ]] || { echo "[mannequin] no PR available (needs feature/vok -> main)" >&2; exit 1; }

V="$(api GET "$REPO_R/pull-requests/$PR_ID" | jq_get '.version')"
curl -s -u "$USER:$PASS" -X POST "$BASE$REPO_R/pull-requests/$PR_ID/comments" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json;print(json.dumps({'text':'looks good to me, ching','version':$V}))")" >/dev/null
PARB="$(bjson "{'user':{'name':'$USER'},'role':'REVIEWER'}")"
api POST "$REPO_R/pull-requests/$PR_ID/participants" "$PARB" >/dev/null
log "PR $PR_ID: comment + reviewer by $USER"

echo "$PR_ID" > "$WORK/pr_id"
echo "PR_ID=$PR_ID" > "$WORK/env"

# --- 4. bb-archiver scrape -------------------------------------------------------
rm -rf "$SCRAPE"
export PYTHONPATH="$LAB_DIR/..${PYTHONPATH:+:$PYTHONPATH}"
python3 -c "import bb_archiver.cli as c; c.main(['scrape','--base','$BASE','--user','$SYSADMIN_USER','--password','$SYSADMIN_PASSWORD','--project','$PROJECT','--repo','$REPO','--out','$SCRAPE'])" 2>&1 | sed 's/^/  scrape: /'
log "scraped -> $SCRAPE"

# --- 5. bb-archiver assemble -----------------------------------------------------
python3 -c "import bb_archiver.cli as c; c.main(['assemble','--scrape','$SCRAPE','--out','$TAR'])" 2>&1 | sed 's/^/  assemble: /'
log "assembled -> $TAR"

# sanity: the archive carries the email-only-in-git situation
ACT=$(tar -tf "$TAR" | grep 'activities.json.atl.gz' | head -1)
tar -xf "$TAR" -O "$ACT" | python3 -c "
import gzip,json,sys
d=json.loads(gzip.decompress(sys.stdin.buffer.read()))
uid={a.get('userId') for a in d if a.get('userId')}
print('archive userIds:', uid)
for u in sorted(uid):
    parts=u.split('|')
    assert len(parts)==4, u
    print(f'  email slot of {u!r} is EMPTY: {parts[2]==\"\"}')
"

# --- 6. GitHub import --------------------------------------------------------------
if [[ "$SKIP_IMPORT" == "1" ]]; then
  log "MANI_SKIP_IMPORT=1: stopping after assemble (no GitHub import)"
  log "import manually with:"
  log "  MY_GH_PAT=\$(cat $GH_PAT) BBS_USERNAME=$SYSADMIN_USER BBS_PASSWORD=$SYSADMIN_PASSWORD \\"
  log "      gh bbs2gh migrate-repo --archive-path $TAR --use-github-storage --queue-only \\"
  log "          --github-org $GH_ORG --github-repo $REPO --bbs-server-url $BASE \\"
  log "          --bbs-project $PROJECT --bbs-repo $REPO"
  exit 0
fi

[[ -s "$GH_PAT" ]] || { echo "[mannequin] GH_PAT file missing: $GH_PAT" >&2; exit 1; }
GH_PAT="$(cat "$GH_PAT")"
export GH_PAT BBS_USERNAME="$SYSADMIN_USER" BBS_PASSWORD="$SYSADMIN_PASSWORD"

log "importing $TAR -> $GH_ORG/$REPO"
OUT="$(gh bbs2gh migrate-repo --archive-path "$TAR" --use-github-storage --queue-only \
  --github-org "$GH_ORG" --github-repo "$REPO" \
  --bbs-server-url "$BASE" --bbs-project "$PROJECT" --bbs-repo "$REPO" 2>&1)"
echo "$OUT" | sed 's/^/  import: /'
MID="$(printf '%s\n' "$OUT" | grep -oE 'RM_[A-Za-z0-9_]+' | head -1)"
[[ -n "$MID" ]] || { echo "[mannequin] could not get migration id" >&2; exit 1; }

log "waiting for migration $MID ..."
gh bbs2gh wait-for-migration --migration-id "$MID" 2>&1 | sed 's/^/  wait: /'

# --- 7. verify GitHub-side attribution ---------------------------------------------
log "GH verification ($GH_ORG/$REPO):"
log "  commits (git author/email must be preserved):"
gh api "repos/$GH_ORG/$REPO/commits" --paginate --jq '.[] | "    \(.sha[0:8]) \(.commit.author.name) <\(.commit.author.email)> login=\(.author.login // "null")"' | sort -u

log "  PR + comment attribution (mannequin usernames expected):"
PR_N="$(gh api "repos/$GH_ORG/$REPO/pulls" --jq 'map(.number)[]' | head -1)"
gh api "repos/$GH_ORG/$REPO/issues/$PR_N/timeline" --jq '.[] | "    event=\(.event) actor=\(.actor.login // "null") body=\(.body[0:24] // "")"' | grep -v '^    event=committed' | head

log "  mannequin users involved (type must be Mannequin):"
for a in $(gh api "repos/$GH_ORG/$REPO/issues/$PR_N/timeline" --jq '.[].actor.login // empty'); do
  t="$(gh api "users/$a" --jq '.type')"
  log "    $a -> $t"
done

log "DONE. Artifacts in $WORK"
log "NOTE: mannequins are NOT org members (do not appear on the org People page);"
log "  reclaim is an org-owner flow in the GitHub UI / migration admin."