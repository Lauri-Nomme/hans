#!/usr/bin/env bash
# Phase 0 lab bring-up: two Bitbucket Data Center 9.4.18 containers.
#   bb-lab-a = admin-export source + fixture target (Phase 1-3)
#   bb-lab-b = migration-import target (Phase 5 Gate 2 round trip)
# Unattended setup via bitbucket.properties (no install wizard).
# Idempotent: safe to re-run; instance data persists under lab/home/<name>.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/config.env"

HOMES_DIR="$LAB_DIR/home"
mkdir -p "$HOMES_DIR/$A_NAME" "$HOMES_DIR/$B_NAME"

log() { printf '[lab-up] %s\n' "$*"; }

# --- preflight ---------------------------------------------------------------
if ! $NERDCTL_CMD version >/dev/null 2>&1; then
  echo "nerdctl is not usable via: $NERDCTL_CMD" >&2
  echo "set NERDCTL_CMD to a working runtime command" >&2
  exit 1
fi

if ! $NERDCTL_CMD image inspect "$BB_IMAGE" >/dev/null 2>&1; then
  log "pulling $BB_IMAGE ..."
  $NERDCTL_CMD pull "$BB_IMAGE"
fi

# --- license -----------------------------------------------------------------
# Verified (9.4.18): the setup wizard gate `hasLicenseAndBaseUrl()` requires a
# license to be present, so unattended setup CANNOT complete without one.
# Set ALLOW_UNLICENSED=1 to start anyway and drive the wizard with
# ./lab/hack-unlicensed.sh (lab-only; see SEED.md).
LICENSE=
if [[ -s "$LAB_LICENSE_FILE" ]]; then
  LICENSE="$(tr -d '\n\r ' < "$LAB_LICENSE_FILE")"
  log "license loaded from $LAB_LICENSE_FILE"
elif [[ "${ALLOW_UNLICENSED:-0}" == "1" ]]; then
  log "ALLOW_UNLICENSED=1: starting WITHOUT a license."
  log "  the wizard will be completed by ./lab/hack-unlicensed.sh."
else
  echo "FATAL: no license at $LAB_LICENSE_FILE" >&2
  echo "  Bitbucket 9.4.18 cannot complete setup without a Data Center license" >&2
  echo "  (verified against the setup wizard gate; the wizard loops on the" >&2
  echo "   settings step until licenseService.isPresent())." >&2
  echo "  1. Get a Data Center evaluation license from my.atlassian.com" >&2
  echo "  2. Save it to $LAB_LICENSE_FILE" >&2
  echo "  3. Re-run: ./lab/lab-down.sh && PURGE=1 ./lab/lab-down.sh && ./lab/lab-up.sh" >&2
  echo "  or for the lab-only no-license path: ALLOW_UNLICENSED=1 ./lab/lab-up.sh" >&2
  exit 2
fi

# --- render bitbucket.properties (before first boot) -------------------------
# Fresh home dirs are owned by the invoking user; after the first boot the
# container's uid (2003) owns them, so fall back to sudo -n for re-runs.
render_props() {
  local name="$1" base_url="$2"
  local f="$HOMES_DIR/$name/bitbucket.properties"
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Managed by lab-up.sh - edit lab/bitbucket.properties.template instead.
setup.displayName=BB Lab ($name)
setup.baseUrl=$base_url
setup.sysadmin.username=$SYSADMIN_USER
setup.sysadmin.password=$SYSADMIN_PASSWORD
setup.sysadmin.displayName=$SYSADMIN_DISPLAYNAME
setup.sysadmin.emailAddress=$SYSADMIN_EMAIL
feature.migration.export.enabled=true
feature.migration.import.enabled=true
server.port=7990
EOF
  if [[ -n "$LICENSE" ]]; then
    printf 'setup.license=%s\n' "$LICENSE" >> "$tmp"
  fi
  if install -m 644 "$tmp" "$f" 2>/dev/null || sudo -n install -m 644 "$tmp" "$f" 2>/dev/null; then
    rm -f "$tmp"
    log "wrote $f"
  else
    echo "failed to write $f (needs write access or passwordless sudo)" >&2
    rm -f "$tmp"
    exit 1
  fi
}

# JSON value extraction without jq (not guaranteed on Rancher Desktop/WSL).
json_get() {
  local key="$1"
  python3 -c "import json,sys
try: o=json.load(sys.stdin)
except Exception: sys.exit(1)
v=o.get('$key','')
print(v if v else '')"
}

# --- container lifecycle -----------------------------------------------------
start_container() {
  local name="$1" http_port="$2" ssh_port="$3"
  if $NERDCTL_CMD container inspect "$name" >/dev/null 2>&1; then
    log "removing existing container $name"
    $NERDCTL_CMD rm -f "$name" >/dev/null
  fi
  log "starting $name (http :$http_port, ssh :$ssh_port, heap $JVM_MAX_MEM)"
  $NERDCTL_CMD run -d --name "$name" \
    --restart unless-stopped \
    -p "$http_port:7990" \
    -p "$ssh_port:7999" \
    -e JVM_MINIMUM_MEMORY="$JVM_MIN_MEM" \
    -e JVM_MAXIMUM_MEMORY="$JVM_MAX_MEM" \
    -v "$HOMES_DIR/$name:/var/atlassian/application-data/bitbucket" \
    "$BB_IMAGE"
}

# --- readiness ---------------------------------------------------------------
wait_ready() {
  local name="$1" port="$2"
  local base="http://localhost:$port"
  log "waiting for $name at $base (timeout ${READY_TIMEOUT}s)"
  local deadline=$((SECONDS + READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    if curl -sf -o /dev/null "$base/status"; then
      if [[ "${ALLOW_UNLICENSED:-0}" == "1" ]]; then
        # Unlicensed path: setup wizard not yet complete; wait for the app only.
        if v="$(curl -sf "$base/rest/api/1.0/application-properties" | json_get version 2>/dev/null)"; then
          log "$name APP UP (Bitbucket $v; wizard pending — run hack-unlicensed.sh)"
          return 0
        fi
      else
        # Setup is complete once the sysadmin account resolves to the configured name.
        if v="$(curl -s -u "$SYSADMIN_USER:$SYSADMIN_PASSWORD" \
                "$base/rest/api/1.0/users/$SYSADMIN_USER" | json_get name 2>/dev/null)"; then
          if [[ "$v" == "$SYSADMIN_USER" ]]; then
            log "$name READY (sysadmin user '$v' exists)"
            return 0
          fi
        fi
      fi
    fi
    sleep 5
  done
  echo "ERROR: $name did not become ready within ${READY_TIMEOUT}s" >&2
  echo "  inspect: $NERDCTL_CMD logs $name" >&2
  return 1
}

if [[ "${SKIP_A:-0}" != "1" ]]; then
  render_props "$A_NAME" "http://localhost:$A_HTTP_PORT"
fi
render_props "$B_NAME" "http://localhost:$B_HTTP_PORT"

if [[ "${SKIP_A:-0}" != "1" ]]; then
  start_container "$A_NAME" "$A_HTTP_PORT" "$A_SSH_PORT"
  wait_ready "$A_NAME" "$A_HTTP_PORT"
fi

start_container "$B_NAME" "$B_HTTP_PORT" "$B_SSH_PORT"
wait_ready "$B_NAME" "$B_HTTP_PORT"

log "Done. container(s) up."
if [[ "${SKIP_A:-0}" != "1" ]]; then
  log "  A (export source): http://localhost:$A_HTTP_PORT  (ssh :$A_SSH_PORT)"
fi
log "  B (import target): http://localhost:$B_HTTP_PORT  (ssh :$B_SSH_PORT)"
log "  sysadmin: $SYSADMIN_USER / \$SYSADMIN_PASSWORD (config.env)"
log "  shared home (A): $HOMES_DIR/$A_NAME/shared/data/migration/"
log "  shared home (B): $HOMES_DIR/$B_NAME/shared/data/migration/"