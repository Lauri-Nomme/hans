#!/usr/bin/env bash
# Shared helpers for Phase 1 fixture scripts (source me, don't execute).
# Ground rules from the plan: never guess semantics - every endpoint below was
# verified live against Bitbucket 9.4.18 (or is exercised and logged by the
# scripts). All text fields exercise unicode/emoji/markdown/@mentions.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/lab/config.env"

BASE="${A_BASE:-http://localhost:${A_HTTP_PORT:-7990}}"
ADMIN="$SYSADMIN_USER:$SYSADMIN_PASSWORD"
PROJECT_KEY="${FIXTURE_PROJECT_KEY:-FIX}"
REPO_SLUG="${FIXTURE_REPO_SLUG:-golden}"
TMP="${FIXTURE_TMP:-/tmp/opencode/fixture-tmp}"
WORKDIR="${FIXTURE_WORKDIR:-$TMP/work}"
GIT_REMOTE="$BASE/scm/$PROJECT_KEY/$REPO_SLUG.git"
mkdir -p "$TMP" "$WORKDIR"

log() { printf '[fixtures] %s\n' "$*"; }

# --- REST ---------------------------------------------------------------------
# api METHOD path [body_file] [user:pass]  -> prints response body
api() {
  local method="$1" path="$2" body="${3:-}" cred="${4:-$ADMIN}"
  local -a args=(-s -u "$cred" -H 'Content-Type: application/json'
    -H 'X-Atlassian-Token: no-check' -X "$method" "$BASE$path")
  [[ -n "$body" ]] && args+=(--data-binary "@$body")
  curl "${args[@]}"
}

# py_json '<python expression>' -> path to a temp JSON file with the value
py_json() {
  local f="$TMP/body-$(basename "$(mktemp)").json"
  python3 -c "import json,sys; print(json.dumps($1))" > "$f"
  echo "$f"
}

# --- git ---------------------------------------------------------------------
mkcommit() { # author_name author_email committer_name committer_email date msg
  GIT_AUTHOR_NAME="$1" GIT_AUTHOR_EMAIL="$2" \
  GIT_COMMITTER_NAME="$3" GIT_COMMITTER_EMAIL="$4" \
  GIT_AUTHOR_DATE="$5" GIT_COMMITTER_DATE="$5" \
    git commit -q --allow-empty -m "$6"
}

# push_branch <user:pass> <refspec...>
push_branches() {
  local cred="$1"; shift
  local url="${GIT_REMOTE/http:\/\//http://$cred@}"
  git push -q "$url" "$@" 2>&1 | sed 's/^/  push: /'
}

# diff_line <pr_id> <path> <ADDED|REMOVED|CONTEXT> -> one anchor line number
diff_line() {
  local pr_id="$1" path="$2" type="$3"
  api GET "/rest/api/1.0/projects/$PROJECT_KEY/repos/$REPO_SLUG/pull-requests/$pr_id/diff" > "$TMP/diff.json"
  jq -r --arg p "$path" --arg t "$type" '
    .diffs[] | select((.destination.path // "") == $p) |
    .hunks[].segments[] | select(.type == $t) |
    .lines[] | select(.line != null) |
    if $t == "REMOVED" then .source else .destination end
  ' "$TMP/diff.json" | head -1
}

# pr_id <number> -> the numeric id of pull request <number>
pr_id() {
  api GET "/rest/api/1.0/projects/$PROJECT_KEY/repos/$REPO_SLUG/pull-requests?limit=100&state=ALL" \
    | jq -r --arg n "$1" '.values[] | select(.id|tostring == $n or .fromRef.displayId == $n) | .id' | head -1
}

# pr_version <pr_id> -> current version for optimistic-locking writes
pr_version() {
  api GET "/rest/api/1.0/projects/$PROJECT_KEY/repos/$REPO_SLUG/pull-requests/$1" | jq -r '.version'
}