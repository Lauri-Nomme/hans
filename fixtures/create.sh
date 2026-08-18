#!/usr/bin/env bash
# Phase 1 fixture: users, project, repo, and the git layer (crafted dates,
# distinct author/committer, branches, lightweight + annotated tags).
# Idempotent: existing users/project/repo are skipped.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

# --- users (admin-only endpoint; verified live) ------------------------------
user_create() { # name password displayName email
  local name="$1" pw="$2" dn="$3" em="$4"
  if api GET "/rest/api/1.0/users/$name" | jq -e '.name == "'$name'"' >/dev/null 2>&1; then
    log "user $name exists"
  else
    log "creating user $name"
    curl -s -u "$ADMIN" -X POST -G "$BASE/rest/api/1.0/admin/users" \
      --data-urlencode "name=$name" --data-urlencode "password=$pw" \
      --data-urlencode "displayName=$dn" --data-urlencode "emailAddress=$em" \
      --data-urlencode "addToDefaultGroup=true" --data-urlencode "notify=false"
  fi
}

user_create ada   "ada-pw-123"   "Ada Lovelace"   "ada@example.com"
user_create grace "grace-pw-123" "Grace Hopper"   "grace@example.com"
user_create alan  "alan-pw-123"  "Alan Turing"    "alan@example.com"

# --- project + repo -----------------------------------------------------------
if api GET "/rest/api/1.0/projects/$PROJECT_KEY" | jq -e '.key == "'$PROJECT_KEY'"' >/dev/null 2>&1; then
  log "project $PROJECT_KEY exists"
else
  log "creating project $PROJECT_KEY"
  api POST "/rest/api/1.0/projects" "$(py_json "{'key':'$PROJECT_KEY','name':'Fixture Project','description':'Golden fixture corpus for REST->Archive','public':False}")"
fi

if api GET "/rest/api/1.0/projects/$PROJECT_KEY/repos/$REPO_SLUG" | jq -e '.slug == "'$REPO_SLUG'"' >/dev/null 2>&1; then
  log "repo $REPO_SLUG exists"
else
  log "creating repo $REPO_SLUG"
  api POST "/rest/api/1.0/projects/$PROJECT_KEY/repos" "$(py_json "{'name':'$REPO_SLUG','scmId':'git','forkable':True}")"
fi

# fixture users need write access to push/merge
for u in ada grace alan; do
  api PUT "/rest/api/1.0/projects/$PROJECT_KEY/permissions/users?name=$u&permission=PROJECT_ADMIN"
done
log "granted PROJECT_ADMIN on $PROJECT_KEY to ada, grace, alan"

# --- git layer ----------------------------------------------------------------
if [[ ! -d "$WORKDIR/golden" ]]; then
  git init -q -b main "$WORKDIR/golden"
  cd "$WORKDIR/golden"
  git config user.name "Ada Lovelace"
  git config user.email "ada@example.com"

  # main: C0..C3
  cat > README.md <<'EOF'
# Golden Fixture Repository

A synthetic repository used to exercise every REST representation the
REST->Archive tool must reconstruct.
EOF
  git add README.md
  mkcommit "Ada Lovelace" "ada@example.com" "Ada Lovelace" "ada@example.com" "2024-01-15T10:00:00+00:00" "chore: initial project scaffold"

  cat > README.md <<'EOF'
# Golden Fixture Repository

A synthetic repository used to exercise every REST representation the
REST->Archive tool must reconstruct.

## Overview
This is the source of ground truth for the export-format reconstruction.
EOF
  git add README.md
  mkcommit "Ada Lovelace" "ada@example.com" "Ada Lovelace" "ada@example.com" "2024-01-16T11:00:00+00:00" "docs: expand README with overview"
  C1="$(git rev-parse HEAD)"

  mkdir -p src
  cat > src/util.py <<'EOF'
"""Core utilities."""

def add(a, b):
    return a + b
EOF
  git add src/util.py
  mkcommit "Grace Hopper" "grace@example.com" "Ada Lovelace" "ada@example.com" "2024-02-01T09:30:00+00:00" "feat: add core utility library"

  cat > CONTRIBUTING.md <<'EOF'
# Contributing

Please follow the project conventions. Thank you!
EOF
  git add CONTRIBUTING.md
  mkcommit "Alan Turing" "alan@example.com" "Alan Turing" "alan@example.com" "2024-02-10T14:00:00+00:00" "docs: add contributing guide"

  # C3b on main: explore placeholder (gives feature/explore a REMOVED-line anchor)
  cat > explore.md <<'EOF'
# Explore

- placeholder entry
- nav link
EOF
  git add explore.md
  mkcommit "Alan Turing" "alan@example.com" "Alan Turing" "alan@example.com" "2024-02-15T09:00:00+00:00" "feat: add explore placeholder page"

  # feature/login: C4, C5 (from C1)
  git branch feature/login "$C1"
  git checkout -q feature/login
  cat > login.py <<'EOF'
import re

def normalize(username):
    return username.strip().lower()
EOF
  git add login.py
  mkcommit "Ada Lovelace" "ada@example.com" "Ada Lovelace" "ada@example.com" "2024-02-20T10:00:00+00:00" "feat: add login screen"

  cat > login.py <<'EOF'
import re

def normalize(username):
    return username.strip().lower()

def validate(username, password):
    if len(password) < 8:
        raise ValueError("password too short")
    return True
EOF
  git add login.py
  mkcommit "Grace Hopper" "grace@example.com" "Grace Hopper" "grace@example.com" "2024-02-21T15:00:00+00:00" "feat: add login validation"

  # hotfix/critical: C7 (from main tip so the PR merge can be a true fast-forward)
  git checkout -q main
  git branch hotfix/critical
  git checkout -q hotfix/critical
  mkdir -p src
  cat > src/util.py <<'EOF'
"""Core utilities."""

def add(a, b):
    return a + b

def sanitize(s):
    return s.replace("<", "&lt;")
EOF
  git add src/util.py
  mkcommit "Alan Turing" "alan@example.com" "Alan Turing" "alan@example.com" "2024-03-01T08:00:00+00:00" "fix: sanitize user input in util library"

  # experiment/squash: C8, C9 (from C3b == main tip). Touches a NEW file so it
  # does not conflict with hotfix/critical's src/util.py change (PR2 + PR3 both merge).
  git checkout -q main
  git branch experiment/squash
  git checkout -q experiment/squash
  mkdir -p src
  cat > src/models.py <<'EOF'
"""Domain models."""

class User:
    def __init__(self, name):
        self.name = name
EOF
  git add src/models.py
  mkcommit "Ada Lovelace" "ada@example.com" "Ada Lovelace" "ada@example.com" "2024-03-10T09:00:00+00:00" "feat: experimental refactor (wip 1)"

  cat > src/models.py <<'EOF'
"""Domain models."""

class User:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"User({self.name})"
EOF
  git add src/models.py
  mkcommit "Grace Hopper" "grace@example.com" "Grace Hopper" "grace@example.com" "2024-03-11T10:00:00+00:00" "feat: experimental refactor (wip 2)"

  # feature/explore: C11, C12 (from main tip = C3b)
  git checkout -q main
  git branch feature/explore
  git checkout -q feature/explore
  cat > explore.md <<'EOF'
# Explore

- placeholder entry
- nav link
- categories
EOF
  cat >> README.md <<'EOF'

## Explore
See the explore page for content discovery.
EOF
  git add explore.md README.md
  mkcommit "Ada Lovelace" "ada@example.com" "Ada Lovelace" "ada@example.com" "2024-03-15T10:00:00+00:00" "feat: add explore page"

  cat > explore.md <<'EOF'
# Explore

- nav link
- categories
- paginated results
EOF
  git add explore.md
  mkcommit "Grace Hopper" "grace@example.com" "Grace Hopper" "grace@example.com" "2024-03-16T12:00:00+00:00" "feat: explore pagination"

  # feature/declined: C13 (from C3)
  git checkout -q main
  git branch feature/declined
  git checkout -q feature/declined
  cat > declined.md <<'EOF'
# Declined experiment

This branch should end up declined.
EOF
  git add declined.md
  mkcommit "Alan Turing" "alan@example.com" "Alan Turing" "alan@example.com" "2024-04-01T09:00:00+00:00" "feat: declined experiment"

  # lightweight tag v1.0 -> C1
  git tag v1.0 "$C1"

  # back to main
  git checkout -q main

  # push everything (branches from C0..C3b + feature branches + v1.0).
  # refs may exist remotely from earlier runs -> force all.
  push_branches "ada:ada-pw-123" \
    "+refs/heads/main" "+refs/heads/feature/login" "+refs/heads/hotfix/critical" \
    "+refs/heads/experiment/squash" "+refs/heads/feature/explore" "+refs/heads/feature/declined" \
    "+refs/tags/v1.0"

  log "git layer pushed (main + 5 feature branches + lightweight tag v1.0)"

  # default branch is NOT auto-set when a repo is created empty via REST;
  # without it merge-listener errors ("No default branch is defined") occur.
  api PUT "/rest/api/1.0/projects/$PROJECT_KEY/repos/$REPO_SLUG/default-branch" \
    "$(py_json "{'id': 'refs/heads/main'}")" >/dev/null
  log "default branch set to main"
else
  log "git workdir already present, skipping git layer"
fi