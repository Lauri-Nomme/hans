#!/usr/bin/env bash
# Phase 1 fixture orchestrator: WIPES and rebuilds the golden repo from scratch
# so the corpus is deterministic (delete is async -> poll until gone).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source "$(pwd)/lib.sh"

log "wiping repo $PROJECT_KEY/$REPO_SLUG (async delete)"
curl -s -u "$ADMIN" -X DELETE "$BASE/rest/api/1.0/projects/$PROJECT_KEY/repos/$REPO_SLUG" \
  -o /dev/null -w "delete HTTP %{http_code}\n"
for _ in $(seq 1 30); do
  if ! api GET "/rest/api/1.0/projects/$PROJECT_KEY/repos/$REPO_SLUG" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
rm -rf "$WORKDIR"

./create.sh
./prs.sh

log "golden repo rebuilt. Corpus: project $PROJECT_KEY/$REPO_SLUG (see FIXTURES.md)"