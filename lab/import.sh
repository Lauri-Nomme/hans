#!/usr/bin/env bash
# Import a bb-archiver archive into a running lab container via Bitbucket's
# official migration import endpoint (the same path Gate 2 uses).
#
#   ./lab/import.sh <archive.tar>            # into bb-lab-b (default)
#   ./lab/import.sh <archive.tar> bb-lab-b
#
# The archive is copied into the container's shared-home migration/import dir
# (the REST endpoint reads only from there, not from export/), chowned to the
# bitbucket user, then the import job is triggered and polled to completion.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/config.env"

ARCHIVE="${1:?usage: ./lab/import.sh <archive.tar> [container]}"
TARGET="${2:-$B_NAME}"
port="$B_HTTP_PORT"
[[ "$TARGET" == "$A_NAME" ]] && port="$A_HTTP_PORT"

test -f "$ARCHIVE" || { echo "archive not found: $ARCHIVE" >&2; exit 1; }
case "$TARGET" in
  "$A_NAME"|"$B_NAME") ;;
  *) echo "unknown container '$TARGET' (expected $A_NAME or $B_NAME)" >&2; exit 1 ;;
esac

log() { printf '[import] %s\n' "$*"; }

BASE="http://localhost:$port"
IMPORT_DIR="/var/atlassian/application-data/bitbucket/shared/data/migration/import"
NAME="$(basename "$ARCHIVE")"

if ! $NERDCTL_CMD container inspect "$TARGET" >/dev/null 2>&1; then
  echo "container $TARGET not present - run ./lab/lab-up.sh first" >&2
  exit 1
fi

log "copying $ARCHIVE -> $TARGET:$IMPORT_DIR/$NAME"
$NERDCTL_CMD cp "$ARCHIVE" "$TARGET:$IMPORT_DIR/$NAME"
$NERDCTL_CMD exec "$TARGET" chown bitbucket:bitbucket "$IMPORT_DIR/$NAME"

log "triggering import of $NAME on $BASE"
JOB="$(curl -s -u "$SYSADMIN_USER:$SYSADMIN_PASSWORD" -X POST \
  "$BASE/rest/api/1.0/migration/imports" \
  -H 'Content-Type: application/json' \
  -d "{\"archivePath\": \"$NAME\"}")"
JOB_ID="$(printf '%s' "$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
log "import job $JOB_ID started"

state="INITIALISING"
deadline=$((SECONDS + 1800))
while (( SECONDS < deadline )); do
  state="$(curl -s -u "$SYSADMIN_USER:$SYSADMIN_PASSWORD" \
    "$BASE/rest/api/1.0/migration/imports/$JOB_ID" | \
    python3 -c 'import json,sys; o=json.load(sys.stdin); print(o["state"])')"
  log "  $state"
  case "$state" in
    COMPLETED) break ;;
    FAILED|CANCELLED) echo "import job $JOB_ID ended with $state" >&2; exit 1 ;;
  esac
  sleep 5
done

if [[ "$state" != "COMPLETED" ]]; then
  echo "import job $JOB_ID did not complete in time (last state $state)" >&2
  exit 1
fi

log "import COMPLETED"
log "  scrape the imported repo and run Gate 2:
      python3 corpus/scrape.py $BASE --user $SYSADMIN_USER --password \$SYSADMIN_PASSWORD -o <scrape-b-dir>
      python3 corpus/gate2.py <scrape-a-dir> <scrape-b-dir>"