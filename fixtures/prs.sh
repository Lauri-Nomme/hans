#!/usr/bin/env bash
# Phase 1 fixture: the PR layer. Exercises every PR-related REST representation
# the REST->Archive tool must reconstruct. Idempotent per-PR via branch name.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

cd "$WORKDIR/golden"

R="/rest/api/1.0/projects/$PROJECT_KEY/repos/$REPO_SLUG"

# pr_by_branch <branch> -> numeric id of the open/all PR from that branch ("" if none)
pr_by_branch() {
  api GET "$R/pull-requests?limit=100&state=ALL" \
    | jq -r --arg b "$1" '.values[] | select(.fromRef.displayId == $b) | .id' | head -1
}

pr_create() { # user:pass title description fromBranch reviewers_json...
  local cred="$1" title="$2" desc="$3" from="$4"; shift 4
  local reviewers="${1:-[]}"
  api POST "$R/pull-requests" "$(py_json "{
    'title': $title,
    'description': $desc,
    'state': 'OPEN',
    'fromRef': {'id': 'refs/heads/$from', 'repository': {'slug': '$REPO_SLUG', 'project': {'key': '$PROJECT_KEY'}}},
    'toRef': {'id': 'refs/heads/main', 'repository': {'slug': '$REPO_SLUG', 'project': {'key': '$PROJECT_KEY'}}},
    'reviewers': $reviewers
  }")" "$cred"
}

# ------------------------------- PR 1: MERGED --------------------------------
PR1_BRANCH=feature/login
PR1_ID="$(pr_by_branch "$PR1_BRANCH")"
if [[ -z "$PR1_ID" ]]; then
  log "creating PR1 ($PR1_BRANCH -> main, will MERGE with merge_commit)"
  RESP="$(pr_create "ada:ada-pw-123" \
    '"Feature: login screen"' \
    '"Implements the **login screen** 🚀 with email login.\n\nMentions @grace for review.\n\n- [ ] add tests\n\nUnicode: ünïcödé 漢字 emoji 🎉"' \
    "$PR1_BRANCH" \
    '[{"user":{"name":"grace"}}]')"
  PR1_ID="$(printf '%s' "$RESP" | jq -r '.id')"
  log "PR1 id=$PR1_ID"
else
  log "PR1 exists (id=$PR1_ID)"
fi

# reviewers added/removed mid-review (9.4.18: /reviewers 404s; use /participants)
api POST "$R/pull-requests/$PR1_ID/participants" "$(py_json "{'user': {'name': 'alan'}, 'role': 'REVIEWER'}")" >/dev/null
log "PR1: added reviewer alan"
api DELETE "$R/pull-requests/$PR1_ID/participants/alan" >/dev/null
log "PR1: removed reviewer alan"

# approvals happen right before the merge (PR2/PR3 advancing main resets an open
# PR's approvals; the author ada cannot approve her own PR).

# top-level comment + reply + edit
TOP_ID="$(api POST "$R/pull-requests/$PR1_ID/comments" \
  "$(py_json "{'text': 'Looks good to me! 🎉 cc @grace', 'severity': 'NORMAL'}")" | jq -r '.id')"
REPLY_ID="$(api POST "$R/pull-requests/$PR1_ID/comments" \
  "$(py_json "{'text': 'Agreed, let us merge it. 🚀', 'parent': {'id': $TOP_ID}}")" | jq -r '.id')"
api PUT "$R/pull-requests/$PR1_ID/comments/$TOP_ID" \
  "$(py_json "{'version': 0, 'text': 'Looks good to me! (edited) 🎉 cc @grace'}")" >/dev/null
log "PR1: top-level comment $TOP_ID + reply $REPLY_ID + edit"

# inline comments: added line + context line + file-level
ALINE="$(diff_line "$PR1_ID" "login.py" "ADDED" | head -1)"
CLINE="$(diff_line "$PR1_ID" "login.py" "CONTEXT" | head -1)"
api POST "$R/pull-requests/$PR1_ID/comments" \
  "$(py_json "{'text': 'inline: added line', 'anchor': {'line': $ALINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'login.py'}}")" >/dev/null
if [[ -n "$CLINE" ]]; then
  api POST "$R/pull-requests/$PR1_ID/comments" \
    "$(py_json "{'text': 'inline: context line', 'anchor': {'line': $CLINE, 'lineType': 'CONTEXT', 'fileType': 'TO', 'path': 'login.py'}}")" >/dev/null
fi
api POST "$R/pull-requests/$PR1_ID/comments" \
  "$(py_json "{'text': 'file-level note on login.py', 'anchor': {'path': 'login.py'}}")" >/dev/null
log "PR1: inline comments (added@$ALINE, context@$CLINE, file-level)"

# title/description edit (UPDATED activity)
V="$(pr_version "$PR1_ID")"
api PUT "$R/pull-requests/$PR1_ID" "$(py_json "{
  'version': $V,
  'title': 'Feature: login screen (updated title)',
  'description': 'Implements the **login screen** 🚀 with email login.\\n\\nMentions @grace.\\n\\nUnicode: ünïcödé 漢字 🎉' }")" >/dev/null
log "PR1: title+description edited"

# tasks = comments with severity BLOCKER (verified 9.4.18). Create one then
# resolve it -> REST exposes state, resolvedDate, resolver; export's comment
# carries resolvedTimestamp/resolverId too.
TASK_ID="$(api POST "$R/pull-requests/$PR1_ID/comments" \
  "$(py_json "{'text': 'add unit tests for validate()', 'severity': 'BLOCKER'}")" | jq -r '.id')"
log "PR1: task comment $TASK_ID created (severity BLOCKER, state OPEN)"
api PUT "$R/pull-requests/$PR1_ID/comments/$TASK_ID" \
  "$(py_json "{'version': 0, 'text': 'add unit tests for validate()', 'severity': 'BLOCKER', 'state': 'RESOLVED'}")" >/dev/null
log "PR1: task $TASK_ID resolved"

# ------------------------------- PR 2: MERGED (ff) ---------------------------
PR2_BRANCH=hotfix/critical
PR2_ID="$(pr_by_branch "$PR2_BRANCH")"
if [[ -z "$PR2_ID" ]]; then
  log "creating PR2 ($PR2_BRANCH -> main, will MERGE with ff)"
  PR2_ID="$(pr_create "alan:alan-pw-123" \
    '"fix: critical sanitization"' \
    '"Hotfix for the `sanitize` gap in util.py. 🛡️"' \
    "$PR2_BRANCH" '[{"user":{"name":"grace"}}]' | jq -r '.id')"
  log "PR2 id=$PR2_ID"
fi
if merge_pr "$PR2_ID" "Merged in $PR2_BRANCH (pull request #$PR2_ID)" "ff"; then
  log "PR2: MERGED (fast-forward, no merge commit)"
else
  log "PR2: strategy 'ff' rejected - falling back to merge_commit"
  if merge_pr "$PR2_ID" "Merged in $PR2_BRANCH (pull request #$PR2_ID)" "merge_commit"; then
    log "PR2: MERGED (merge_commit fallback)"
  else
    log "PR2: merge failed after retries"
  fi
fi

# ------------------------------- PR 3: MERGED (squash) -----------------------
PR3_BRANCH=experiment/squash
PR3_ID="$(pr_by_branch "$PR3_BRANCH")"
if [[ -z "$PR3_ID" ]]; then
  log "creating PR3 ($PR3_BRANCH -> main, will MERGE with squash)"
  PR3_ID="$(pr_create "ada:ada-pw-123" \
    '"refactor: experimental util cleanups"' \
    '"Squashes two WIP commits into one. 🧪"' \
    "$PR3_BRANCH" '[]' | jq -r '.id')"
  log "PR3 id=$PR3_ID"
fi
if merge_pr "$PR3_ID" "Merged in $PR3_BRANCH (pull request #$PR3_ID)" "squash"; then
  log "PR3: MERGED (squash)"
else
  log "PR3: merge failed after retries"
fi

# ------------------------------- PR 1: MERGE (merge_commit) ------------------
# Merge last so PR2's ff and PR3's squash land on main first.
# approvals must be set AFTER PR2/PR3 merges (target advance resets approvals)
api POST "$R/pull-requests/$PR1_ID/participants" "$(py_json "{'user': {'name': 'alan'}, 'role': 'REVIEWER'}")" >/dev/null
api POST "$R/pull-requests/$PR1_ID/approve" "" grace:grace-pw-123 >/dev/null
api POST "$R/pull-requests/$PR1_ID/approve" "" alan:alan-pw-123 >/dev/null
log "PR1: grace + alan approved (after PR2/PR3 merges; author ada cannot approve)"
if merge_pr "$PR1_ID" "Merged in $PR1_BRANCH (pull request #$PR1_ID)" "merge_commit"; then
  log "PR1: MERGED (merge_commit)"
else
  log "PR1: merge failed after retries"
fi

# KNOWN-BAD: merge does NOT auto-delete the source branch on 9.4.18.
if git ls-remote --heads "$(cred_url "$GIT_REMOTE" "$ADMIN")" "refs/heads/$PR1_BRANCH" | grep -q .; then
  log "PR1: KNOWN-BAD - $PR1_BRANCH still exists after merge (branch NOT auto-deleted)"
else
  log "PR1: $PR1_BRANCH was auto-deleted after merge"
fi

# annotated tag v1.1 on PR1's actual merge commit (tagger = alan, distinct from
# committer). mergeCommit only appears in the merge RESPONSE, not on GET; remote
# main tip IS the PR1 merge commit (merged last).
git fetch -q "$(cred_url "$GIT_REMOTE" "alan:alan-pw-123")" main
MERGE_SHA="$(git rev-parse FETCH_HEAD)"
git -c user.name="Alan Turing" -c user.email="alan@example.com" \
  tag -a "v1.1" -m "release 1.1 (PR1 merge)" "$MERGE_SHA"
push_branches "alan:alan-pw-123" "+refs/tags/v1.1"
log "PR1: annotated tag v1.1 pushed (tagger alan, on merge commit $MERGE_SHA)"

# -------------------- PR 6 + PR 7: stacked pair (MERGED without commit) ----
# PR6 (feature/stacked/base -> main) stays open; PR7 (feature/stacked/dependent
# -> main, descended from base) is merged. Merging PR7 carries PR6's commits
# into main, so Bitbucket flips PR6 to a commit-less MERGED ("remotely merged").
# Placed BEFORE PR4 so the main advance does not reset PR4's reviewer states.
PR6_BRANCH=feature/stacked/base
PR6_ID="$(pr_by_branch "$PR6_BRANCH")"
if [[ -z "$PR6_ID" ]]; then
  log "creating PR6 ($PR6_BRANCH -> main, will show commit-less MERGED after PR7)"
  PR6_ID="$(pr_create "ada:ada-pw-123" \
    '"feat: stacked base"' \
    '"Base PR of a stacked pair; merging the dependent PR7 flips this to a commit-less MERGED. 📚"' \
    "$PR6_BRANCH" '[]' | jq -r '.id')"
  log "PR6 id=$PR6_ID"
fi
api POST "$R/pull-requests/$PR6_ID/comments" \
  "$(py_json "{'text': 'base PR of the stacked pair 📚'}")" >/dev/null
log "PR6: top-level comment"

PR7_BRANCH=feature/stacked/dependent
PR7_ID="$(pr_by_branch "$PR7_BRANCH")"
if [[ -z "$PR7_ID" ]]; then
  log "creating PR7 ($PR7_BRANCH -> main, will MERGE)"
  PR7_ID="$(pr_create "grace:grace-pw-123" \
    '"feat: stacked dependent"' \
    '"Dependent PR on top of PR6; merging it causes the commit-less MERGED on PR6. 🔀"' \
    "$PR7_BRANCH" '[{"user":{"name":"alan"}}]' | jq -r '.id')"
  log "PR7 id=$PR7_ID"
fi
if merge_pr "$PR7_ID" "Merged in $PR7_BRANCH (pull request #$PR7_ID)" "merge_commit"; then
  log "PR7: MERGED (merge_commit) -> PR6 should now show commit-less MERGED"
else
  log "PR7: merge failed after retries"
fi

# ------------------------------- PR 4: OPEN ----------------------------------
PR4_BRANCH=feature/explore
PR4_ID="$(pr_by_branch "$PR4_BRANCH")"
if [[ -z "$PR4_ID" ]]; then
  log "creating PR4 ($PR4_BRANCH -> main, stays OPEN + force-push RESCOPED)"
  PR4_ID="$(pr_create "ada:ada-pw-123" \
    '"feat: explore page"' \
    '"Adds an explore page. Will be force-pushed to orphan an inline anchor. 🔀"' \
    "$PR4_BRANCH" '[{"user":{"name":"grace"}}]' | jq -r '.id')"
  log "PR4 id=$PR4_ID"
fi

# inline comments: added, removed, context, file-level (before force push)
A_LINE="$(diff_line "$PR4_ID" "explore.md" "ADDED" | head -1)"
R_LINE="$(diff_line "$PR4_ID" "explore.md" "REMOVED" | head -1)"
C_LINE="$(diff_line "$PR4_ID" "explore.md" "CONTEXT" | head -1)"
ORPHAN_ID="$(api POST "$R/pull-requests/$PR4_ID/comments" \
  "$(py_json "{'text': 'inline on added line that will be orphaned', 'anchor': {'line': $A_LINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'explore.md'}}")" | jq -r '.id')"
log "PR4: inline comment on ADDED line (will orphan): id=$ORPHAN_ID line=$A_LINE"
api POST "$R/pull-requests/$PR4_ID/comments" \
  "$(py_json "{'text': 'inline on removed line', 'anchor': {'line': $R_LINE, 'lineType': 'REMOVED', 'fileType': 'FROM', 'path': 'explore.md'}}")" >/dev/null
log "PR4: inline comment on REMOVED line=$R_LINE"
api POST "$R/pull-requests/$PR4_ID/comments" \
  "$(py_json "{'text': 'inline on context line', 'anchor': {'line': $C_LINE, 'lineType': 'CONTEXT', 'fileType': 'TO', 'path': 'explore.md'}}")" >/dev/null
log "PR4: inline comment on CONTEXT line=$C_LINE"
api POST "$R/pull-requests/$PR4_ID/comments" \
  "$(py_json "{'text': 'file-level comment on explore.md', 'anchor': {'path': 'explore.md'}}")" >/dev/null
log "PR4: file-level comment"

# force push -> RESCOPED (reviewer states set BEFORE the push get reset by it)
# rebuild: keep C11, drop C12, add C12b
git checkout -q feature/explore
cat > explore.md <<'EOF'
# Explore

- nav link
- categories
- paginated results (v2)
EOF
git add explore.md
mkcommit "Grace Hopper" "grace@example.com" "Grace Hopper" "grace@example.com" "2024-03-17T12:00:00+00:00" "feat: explore pagination v2"
push_branches "ada:ada-pw-123" "+refs/heads/feature/explore"
NEW_TIP="$(git rev-parse feature/explore)"
log "PR4: force-pushed feature/explore (RESCOPED, new tip $NEW_TIP)"

# The drift processor re-anchors inline comments async and resets reviewer
# states while running; ANY PR update (title edit, push) also resets them.
# Wait until the added-line comment's anchor points at the new tip before any
# state changes, and do reviewer states LAST.
AH=""
for _ in $(seq 1 20); do
  AH="$(api GET "$R/pull-requests/$PR4_ID/comments?path=explore.md&limit=100" 2>/dev/null \
    | jq -r --argjson id "$ORPHAN_ID" '.values[] | select(.id==$id) | .anchor.toHash // ""' | head -1)"
  [[ -n "$AH" && "$AH" == "$NEW_TIP" ]] && break
  sleep 1
done
log "PR4: drift settled (added-line comment anchor -> $AH)"

# title edit (UPDATED) - BEFORE reviewer states (PUT also resets them)
V="$(pr_version "$PR4_ID")"
api PUT "$R/pull-requests/$PR4_ID" "$(py_json "{
  'version': $V,
  'title': 'feat: explore page (rebased for pagination v2)',
  'description': 'Rebased the explore branch; inline anchor on the old added line is now orphaned.' }")" >/dev/null
log "PR4: title+description edited after force push"

# approve -> unapprove -> reapprove (grace) on the rescoped diff (LAST ops)
api POST "$R/pull-requests/$PR4_ID/approve" "" grace:grace-pw-123 >/dev/null || true
api DELETE "$R/pull-requests/$PR4_ID/approve" "" grace:grace-pw-123 >/dev/null || true
api POST "$R/pull-requests/$PR4_ID/approve" "" grace:grace-pw-123 >/dev/null || true
log "PR4: grace approve -> withdraw -> approve"

# NEEDS_WORK: admin adds alan as reviewer; alan sets NEEDS_WORK himself (self-service;
# admin PUT /participants/{user} is 403)
api POST "$R/pull-requests/$PR4_ID/participants" "$(py_json "{'user': {'name': 'alan'}, 'role': 'REVIEWER'}")" >/dev/null
api PUT "$R/pull-requests/$PR4_ID/participants/alan" \
  "$(py_json "{'user': {'name': 'alan'}, 'approved': False, 'status': 'NEEDS_WORK'}")" "alan:alan-pw-123" >/dev/null
log "PR4: alan set NEEDS_WORK"

# PR4: open task (severity BLOCKER, left OPEN) — anchored variant on explore.md
api POST "$R/pull-requests/$PR4_ID/comments" \
  "$(py_json "{'text': 'verify orphaned-anchor handling', 'severity': 'BLOCKER', 'anchor': {'path': 'explore.md'}}")" >/dev/null
log "PR4: open task created (severity BLOCKER, state OPEN)"

# ------------------------------- PR 5: DECLINED ------------------------------
PR5_BRANCH=feature/declined
PR5_ID="$(pr_by_branch "$PR5_BRANCH")"
if [[ -z "$PR5_ID" ]]; then
  log "creating PR5 ($PR5_BRANCH -> main, will be DECLINED)"
  PR5_ID="$(pr_create "alan:alan-pw-123" \
    '"experiment: declined feature"' \
    '"A feature branch that will end up declined. ❌"' \
    "$PR5_BRANCH" '[{"user":{"name":"ada"}}]' | jq -r '.id')"
  log "PR5 id=$PR5_ID"
fi
api POST "$R/pull-requests/$PR5_ID/approve" "" ada:ada-pw-123 >/dev/null || true
api POST "$R/pull-requests/$PR5_ID/comments" \
  "$(py_json "{'text': 'This will not work in production. 🚫'}")" "ada:ada-pw-123" >/dev/null
api DELETE "$R/pull-requests/$PR5_ID/approve" "" ada:ada-pw-123 >/dev/null || true
log "PR5: ada approved, commented, withdrew approval"

# deleted comment fixture: REST delete is a HARD delete on 9.4.18 (no activity
# trace; GET /comments/{id} -> 404). Requires the comment's current version.
DEL_ID="$(api POST "$R/pull-requests/$PR5_ID/comments" \
  "$(py_json "{'text': 'This comment gets deleted.'}")" | jq -r '.id')"
api DELETE "$R/pull-requests/$PR5_ID/comments/$DEL_ID?version=0" >/dev/null
log "PR5: comment $DEL_ID HARD-deleted (no activity trace on 9.4.18)"

V="$(pr_version "$PR5_ID")"
api POST "$R/pull-requests/$PR5_ID/decline" \
  "$(py_json "{'version': $V, 'message': 'Not going to ship this. ❌'}")" >/dev/null
log "PR5: DECLINED"

# -------------------------- commit-level comments ----------------------------
C2_SHA="$(git rev-parse main~2 2>/dev/null || true)"
if [[ -n "$C2_SHA" ]]; then
  # anchored to src/util.py so it is retrievable: GET /commits/{sha}/comments
  # REQUIRES a path param and only returns comments anchored to that path
  api POST "$R/commits/$C2_SHA/comments" \
    "$(py_json "{'text': 'commit-level comment on util library commit 📝', 'anchor': {'path': 'src/util.py', 'srcPath': 'src/util.py', 'line': 1, 'lineType': 'CONTEXT', 'fileType': 'TO'}}")" >/dev/null
  log "commit-level comment on $C2_SHA"
else
  log "commit-level comment skipped (sha lookup)"
fi

# ----------------- PR 8: reference-form corpus (OPEN, created LAST) ----------
# An OPEN PR whose description AND top-level comment carry a matrix of PR-number
# and commit references (all rewritten to real, resolvable lab targets) so we can
# verify byte-fidelity of each reference form through REST scrape -> export
# archive -> GH import. Created LAST so the earlier PR ids (1-7) keep their
# numbers. Open (never merged) so its body is migrated as-is.
PR8_BRANCH=feature/references
PR8_ID="$(pr_by_branch "$PR8_BRANCH")"
if [[ -z "$PR8_ID" ]]; then
  log "creating PR8 ($PR8_BRANCH -> main, stays OPEN; reference-form corpus)"
  git checkout -q -B "$PR8_BRANCH" main
  cat > references.md <<'EOF'
# Reference forms

Fixture content for reference-fidelity testing: the PR-number/commit reference
matrix lives in this PR's description and top-level comment.
EOF
  git add references.md
  mkcommit "Alan Turing" "alan@example.com" "Alan Turing" "alan@example.com" \
    "2024-05-01T09:00:00+00:00" "test: add reference-forms fixture file"
  push_branches "alan:alan-pw-123" "+refs/heads/$PR8_BRANCH"
  log "PR8: pushed branch $PR8_BRANCH"

  # real, resolvable reference targets (all exist in this repo's git history)
  C2="$(git rev-parse main~2)"               # 'feat: add core utility library' on main
  C1="$(git rev-parse v1.0)"                 # lightweight tag v1.0 -> C1 on main
  MAIN_TIP="$(git rev-parse main)"           # C3b, the pre-merge main tip
  LOGIN_TIP="$(git rev-parse feature/login)"     # PR1 branch tip (C5)
  LOGIN_PARENT="$(git rev-parse feature/login~1)" # PR1 earlier commit (C4)
  BASEURL="$BASE/projects/$PROJECT_KEY/repos/$REPO_SLUG"

  REF_TXT="$TMP/pr8-refs.txt"
  cat > "$REF_TXT" <<EOF
Check out #3 — bare PR-number reference to an existing merged PR.

Reference-form test matrix (labels test-1..test5 are stable search anchors):

- test-1 #5
- test0 $BASEURL/pull-requests/6/overview
- test1 $BASEURL/commits/$C2
- test2 $C2
- test3 ${C2:0:11}
- test4 $BASEURL/pull-requests/1/commits/$LOGIN_TIP?since=$LOGIN_PARENT
- test5 $C1...$MAIN_TIP
EOF

  PR8_ID="$(api POST "$R/pull-requests" "$(py_json "{
    'title': 'test: reference-form corpus',
    'description': open('$REF_TXT').read().rstrip(),
    'state': 'OPEN',
    'fromRef': {'id': 'refs/heads/$PR8_BRANCH', 'repository': {'slug': '$REPO_SLUG', 'project': {'key': '$PROJECT_KEY'}}},
    'toRef': {'id': 'refs/heads/main', 'repository': {'slug': '$REPO_SLUG', 'project': {'key': '$PROJECT_KEY'}}},
    'reviewers': []
  }")" "ada:ada-pw-123" | jq -r '.id')"
  log "PR8 id=$PR8_ID"

  # mirror the same reference matrix as a top-level comment (comment-path fidelity)
  api POST "$R/pull-requests/$PR8_ID/comments" \
    "$(py_json "{'text': open('$REF_TXT').read().rstrip(), 'severity': 'NORMAL'}")" "ada:ada-pw-123" >/dev/null
  log "PR8: top-level comment with the reference matrix"
else
  log "PR8 exists (id=$PR8_ID)"
fi

# ------------------------------- PR 9: INTERLEAVED ----------------------------
# Comment/reply/edit on v1 -> force-push v2 (orphans v1 anchors) -> comment/reply
# on v2 -> force-push v3 (orphans v2 anchors) -> comment on v3. Mirrors the
# production REVIEW_THREAD_MISSING pattern: inline anchors made on an earlier
# head, then the branch was force-pushed away under them.
PR9_BRANCH=feature/interleaved
PR9_ID="$(pr_by_branch "$PR9_BRANCH")"
if [[ -z "$PR9_ID" ]]; then
  log "creating PR9 ($PR9_BRANCH -> main, OPEN with interleaved comments/pushes)"
  PR9_ID="$(pr_create "ada:ada-pw-123" \
    '"feat: interleaved comment/push interleave"' \
    '"Comments and replies interleaved with force-pushes; inline anchors made on earlier heads are orphaned by later RESCOPEDs."' \
    "$PR9_BRANCH" '[{"user":{"name":"grace"}}]' | jq -r '.id')"
  log "PR9 id=$PR9_ID"
fi

# start-of-PR reference second (for reflog timestamp-rule analysis)
PR9_CREATED="$(api GET "$R/pull-requests/$PR9_ID" | jq -r '.createdDate')"
log "PR9 createdDate=$PR9_CREATED"
sleep 15

# --- v1 diff: comment + reply + edit (before any push) ------------------------
V1_LINE="$(diff_line "$PR9_ID" "interleaved.md" "ADDED" | head -1)"
V1_CID="$(api POST "$R/pull-requests/$PR9_ID/comments" \
  "$(py_json "{'text': 'v1: inline on added line', 'anchor': {'line': $V1_LINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'interleaved.md'}}")" | jq -r '.id')"
log "PR9: v1 inline comment $V1_CID line=$V1_LINE"
V1_REPLY="$(api POST "$R/pull-requests/$PR9_ID/comments" \
  "$(py_json "{'text': 'v1: reply', 'parent': {'id': $V1_CID}}")" | jq -r '.id')"
api PUT "$R/pull-requests/$PR9_ID/comments/$V1_CID" \
  "$(py_json "{'version': 0, 'text': 'v1: inline on added line (edited)'}")" >/dev/null
log "PR9: v1 reply $V1_REPLY + edit of $V1_CID"

# orphan-target comment: anchored on line 3 of v1 (the "- line v1 alpha" ADDED
# line). v2 rewrites the file so the line content shifts and the anchor cannot
# be re-anchored to the final head -> Bitbucket marks it orphaned=true
# (verified live: it is hidden from /comments but present orphaned:true in the
# activities and in the export, the exact production REVIEW_THREAD_MISSING
# input). Captured golden uses this exact comment id/anchor.
ORPHAN_LINE=3
ORPHAN_CID="$(api POST "$R/pull-requests/$PR9_ID/comments" \
  "$(py_json "{'text': 'v1: comment that will be orphaned by v2 removing the line', 'anchor': {'line': $ORPHAN_LINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'interleaved.md'}}")" | jq -r '.id')"
log "PR9: orphan-target comment $ORPHAN_CID line=$ORPHAN_LINE (v1-only line)"

# orphan-target comment: anchored on a v1-only line (line 3 = "- line v1 alpha"? no —
# use the ORPHAN-ME line 5; but the captured golden used line 3, keep parity).
# The v2 push below DROPS the ORPHAN-ME line but KEEPS alpha/beta, so the drift
# processor re-anchors line-3/line-4 comments but orphaning requires the line to
# vanish. Use line 5 (ORPHAN-ME) which v2 removes -> real orphaned anchor.
ORPHAN_LINE=5
ORPHAN_CID="$(api POST "$R/pull-requests/$PR9_ID/comments" \
  "$(py_json "{'text': 'v1: comment that will be orphaned by v2 removing the line', 'anchor': {'line': $ORPHAN_LINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'interleaved.md'}}")" | jq -r '.id')"
log "PR9: orphan-target comment $ORPHAN_CID line=$ORPHAN_LINE (v1-only line)"

# --- force-push v1 -> v2 (RESCOPED, orphans v1 anchor) -------------------------
git checkout -q "$PR9_BRANCH"
cat > interleaved.md <<'EOF'
# Interleaved

- line v2 alpha
- line v2 beta
- line v2 gamma
EOF
git add interleaved.md
mkcommit "Grace Hopper" "grace@example.com" "Grace Hopper" "grace@example.com" "2024-05-11T10:00:00+00:00" "feat: interleaved v2"
# 15s settle so force-push events land in distinct wall-clock seconds (the
# reflog/RESCOPED timestamp rule can then be told apart: PR-creation time for
# all lines vs per-RESCOPED ceil'd time).
sleep 15
push_branches "ada:ada-pw-123" "+refs/heads/$PR9_BRANCH"
V2_TIP="$(git rev-parse "$PR9_BRANCH")"
log "PR9: force-pushed to v2 ($V2_TIP) -> v1 anchor orphaned"

# wait for the drift processor to re-anchor/reset before new diff comments.
# The v1 comment's anchor should advance to V2_TIP (or flush); poll briefly.
for _ in $(seq 1 20); do
  AH="$(api GET "$R/pull-requests/$PR9_ID/comments?path=interleaved.md&limit=100" 2>/dev/null \
    | jq -r --argjson id "$V1_CID" '.values[] | select(.id==$id) | .anchor.toHash // ""' | head -1)"
  [[ -n "$AH" ]] && break
  sleep 1
done
log "PR9: after v1->v2 push, v1 anchor -> $AH"

# --- v2 diff: comment + reply (now anchored on v2) ----------------------------
V2_LINE="$(diff_line "$PR9_ID" "interleaved.md" "ADDED" | head -1)"
V2_CID="$(api POST "$R/pull-requests/$PR9_ID/comments" \
  "$(py_json "{'text': 'v2: inline on added line', 'anchor': {'line': $V2_LINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'interleaved.md'}}")" | jq -r '.id')"
log "PR9: v2 inline comment $V2_CID line=$V2_LINE"
V2_REPLY="$(api POST "$R/pull-requests/$PR9_ID/comments" \
  "$(py_json "{'text': 'v2: reply', 'parent': {'id': $V2_CID}}")" | jq -r '.id')"
log "PR9: v2 reply $V2_REPLY"

# --- force-push v2 -> v3 (RESCOPED, orphans v2 anchor) -------------------------
cat > interleaved.md <<'EOF'
# Interleaved

- line v3 alpha
- line v3 beta
- line v3 gamma
- line v3 delta
EOF
git add interleaved.md
mkcommit "Alan Turing" "alan@example.com" "Alan Turing" "alan@example.com" "2024-05-12T11:00:00+00:00" "feat: interleaved v3"
sleep 15
push_branches "ada:ada-pw-123" "+refs/heads/$PR9_BRANCH"
V3_TIP="$(git rev-parse "$PR9_BRANCH")"
log "PR9: force-pushed to v3 ($V3_TIP) -> v2 anchor orphaned"

for _ in $(seq 1 20); do
  AH="$(api GET "$R/pull-requests/$PR9_ID/comments?path=interleaved.md&limit=100" 2>/dev/null \
    | jq -r --argjson id "$V2_CID" '.values[] | select(.id==$id) | .anchor.toHash // ""' | head -1)"
  [[ -n "$AH" ]] && break
  sleep 1
done
log "PR9: after v2->v3 push, v2 anchor -> $AH"

# --- v3 diff: comment (last op; leave OPEN) -----------------------------------
V3_LINE="$(diff_line "$PR9_ID" "interleaved.md" "ADDED" | head -1)"
api POST "$R/pull-requests/$PR9_ID/comments" \
  "$(py_json "{'text': 'v3: inline on added line', 'anchor': {'line': $V3_LINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'interleaved.md'}}")" >/dev/null
log "PR9: v3 inline comment line=$V3_LINE"

# reviewer state AFTER all pushes (any PR mutation resets them)
api POST "$R/pull-requests/$PR9_ID/approve" "" grace:grace-pw-123 >/dev/null || true
log "PR9: grace approves final state (after all interleaved ops)"

# ------------------------- PR 10: TRUE FORCE-PUSH-AWAY -----------------------
# Unlike PR9 (whose pushes append commits, leaving v1 an ancestor), here v1 is
# genuinely force-pushed away: the branch is reset to v1's parent and a new
# commit is pushed, so v1 is NOT an ancestor of the head and is reachable only
# via the reflog / refs-keep. This is the production
# REVIEW_THREAD_MISSING_START_COMMIT_OID input.
PR10_BRANCH=feature/rewritten
PR10_ID="$(pr_by_branch "$PR10_BRANCH")"
if [[ -z "$PR10_ID" ]]; then
  log "creating PR10 ($PR10_BRANCH -> main, OPEN, true force-push-away)"
  PR10_ID="$(pr_create "ada:ada-pw-123" \
    '"feat: rewritten (true force-push-away)"' \
    '"v1 is force-pushed away by resetting the branch and pushing a new commit; inline anchors on v1 mirror the production REVIEW_THREAD_MISSING case."' \
    "$PR10_BRANCH" '[{"user":{"name":"grace"}}]' | jq -r '.id')"
  log "PR10 id=$PR10_ID"
fi
sleep 15

# comment + reply anchored on v1's ORPHAN-ME added line (the last added line),
# whose content v2 removes -> BB cannot re-anchor it to the new head, so it
# stays pinned to the vanished v1 commit with orphaned=true.
R10_LINE="$(diff_line "$PR10_ID" "rewritten.md" "ADDED" | tail -1)"
R10_V1_CID="$(api POST "$R/pull-requests/$PR10_ID/comments" \
  "$(py_json "{'text': 'v1: anchored on a commit that will be force-pushed away', 'anchor': {'line': $R10_LINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'rewritten.md'}}")" | jq -r '.id')"
R10_V1_REPLY="$(api POST "$R/pull-requests/$PR10_ID/comments" \
  "$(py_json "{'text': 'v1: reply on the vanishing commit', 'parent': {'id': $R10_V1_CID}}")" | jq -r '.id')"
log "PR10: v1 comment $R10_V1_CID + reply $R10_V1_REPLY (anchor on soon-unreachable commit)"

# TRUE rewrite: reset to v1's parent, DELETE the anchored file (so BB cannot
# re-anchor the comment to the new head), add a control file, force-push.
git checkout -q "$PR10_BRANCH"
R10_V1_COMMIT="$(git rev-parse "$PR10_BRANCH")"
R10_BASE="$(git rev-parse "$PR10_BRANCH^")"
git reset --hard -q "$R10_BASE"
# (rewritten.md is gone after the reset to base — no git rm needed)
cat > stable.md <<'EOF'
# Stable

- stable v2 line
EOF
git add stable.md
mkcommit "Grace Hopper" "grace@example.com" "Grace Hopper" "grace@example.com" "2024-05-14T10:00:00+00:00" "feat: rewritten v2 (replaces v1, drops the anchored file)"
sleep 15
push_branches "ada:ada-pw-123" "+refs/heads/$PR10_BRANCH"
R10_V2_TIP="$(git rev-parse "$PR10_BRANCH")"
if git merge-base --is-ancestor "$R10_V1_COMMIT" "$R10_V2_TIP"; then
  log "PR10: WARNING v1 ($R10_V1_COMMIT) is still an ancestor of $R10_V2_TIP"
else
  log "PR10: confirmed v1 ($R10_V1_COMMIT) force-pushed away; head $R10_V2_TIP"
fi

# wait for the drift processor, then comment on the control file (reachable)
for _ in $(seq 1 20); do
  AH="$(api GET "$R/pull-requests/$PR10_ID/comments?path=rewritten.md&limit=100" 2>/dev/null \
    | jq -r --argjson id "$R10_V1_CID" '.values[] | select(.id==$id) | .anchor.toHash // ""' | head -1)"
  [[ -n "$AH" ]] && break
  sleep 1
done
log "PR10: after force-push, v1 comment anchor.toHash -> $AH"

R10_V2_LINE="$(diff_line "$PR10_ID" "stable.md" "ADDED" | head -1)"
api POST "$R/pull-requests/$PR10_ID/comments" \
  "$(py_json "{'text': 'v2: anchored on the surviving head', 'anchor': {'line': $R10_V2_LINE, 'lineType': 'ADDED', 'fileType': 'TO', 'path': 'stable.md'}}")" >/dev/null
log "PR10: v2 control comment on stable.md line=$R10_V2_LINE"

# reviewer state last (any PR mutation resets it)
api POST "$R/pull-requests/$PR10_ID/approve" "" grace:grace-pw-123 >/dev/null || true
log "PR10: grace approves final state"

git checkout -q main

log "PR layer complete. PR ids: PR1=$PR1_ID PR2=$PR2_ID PR3=$PR3_ID PR4=$PR4_ID PR5=$PR5_ID PR6=$PR6_ID PR7=$PR7_ID PR8=$PR8_ID PR9=$PR9_ID PR10=$PR10_ID"
