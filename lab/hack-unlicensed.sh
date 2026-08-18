#!/usr/bin/env bash
# LAB-ONLY hack (no license): drive the 9.4.18 setup wizard to completion in
# unlicensed mode by patching SetupController.hasLicenseAndBaseUrl() to always
# return true (the wizard's license gate). See SEED.md.
#
# Precondition: containers running via `ALLOW_UNLICENSED=1 ./lab/lab-up.sh`.
# This script is re-runnable; container re-creation (lab-up.sh) re-requires it.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/config.env"

PATCHER="$LAB_DIR/patch-setup.py"
PATCH_SRC_CLASS="com/atlassian/stash/internal/web/setup/SetupController.class"
PATCH_DST_CLASS="/opt/atlassian/bitbucket/app/WEB-INF/classes/com/atlassian/stash/internal/web/setup/SetupController.class"

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }

log() { printf '[hack-unlicensed] %s\n' "$*"; }

# --- patch a container's SetupController and restart it ----------------------
is_setup_done() {
  local name="$1" port="$2"
  local base="http://localhost:$port"
  if v="$(curl -s -u "$SYSADMIN_USER:$SYSADMIN_PASSWORD" \
           "$base/rest/api/1.0/users/$SYSADMIN_USER" | jq -r '.name // empty' 2>/dev/null)"; then
    [[ "$v" == "$SYSADMIN_USER" ]]
  else
    return 1
  fi
}

patch_container() {
  local name="$1"
  if ! $NERDCTL_CMD container inspect "$name" >/dev/null 2>&1; then
    echo "container $name not present - run: ALLOW_UNLICENSED=1 ./lab/lab-up.sh" >&2
    exit 1
  fi
  local tmp; tmp="$(mktemp -d)"
  $NERDCTL_CMD cp "$name:$PATCH_DST_CLASS" "$tmp/SetupController.class" 2>/dev/null || {
    echo "$name: class unreadable at $PATCH_DST_CLASS" >&2; exit 1; }
  sudo -n chmod 0444 "$tmp/SetupController.class" 2>/dev/null || true
  python3 "$PATCHER" "$tmp/SetupController.class" "$tmp/SetupController.patched.class"
  if cmp -s "$tmp/SetupController.class" "$tmp/SetupController.patched.class"; then
    log "$name: already patched, skipping restart"
    rm -rf "$tmp"
    return 0
  fi
  $NERDCTL_CMD cp "$tmp/SetupController.patched.class" "$name:$PATCH_DST_CLASS.patched"
  $NERDCTL_CMD exec "$name" sh -c "cp -f '$PATCH_DST_CLASS.patched' '$PATCH_DST_CLASS' && chmod 0444 '$PATCH_DST_CLASS' && rm -f '$PATCH_DST_CLASS.patched'"
  rm -rf "$tmp"
  log "$name: patched, restarting"
  $NERDCTL_CMD restart "$name" >/dev/null
}

# --- drive the wizard to completion via its HTML form flow -------------------
wizard_complete() {
  local name="$1" port="$2"
  local base="http://localhost:$port"
  if is_setup_done "$name" "$port"; then
    log "$name: setup already complete, skipping wizard walk"
    return 0
  fi
  local jar; jar="$(mktemp)"
  log "$name: waiting for app to come up after patch"
  local deadline=$((SECONDS + READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    curl -sf -o /dev/null "$base/status" && break
    sleep 5
  done
  curl -sf -o /dev/null "$base/status" || { echo "$name: app did not come up" >&2; return 1; }

  log "$name: walking wizard steps"
  for _ in $(seq 1 20); do
    local page step token
    page="$(curl -s -b "$jar" -c "$jar" "$base/setup")"
    step="$(printf '%s' "$page" | grep -oE "name=['\"]step['\"] value=['\"][a-zA-Z0-9-]+['\"]" | grep -oE "'[a-zA-Z0-9-]+'$" | tr -d "'")"
    if [[ -z "$step" ]]; then
      log "$name: no step marker (wizard done?)"
      break
    fi
    token="$(printf '%s' "$page" | grep -oE 'name="atl_token" value="[^"]*"' | grep -oE 'value="[^"]*"' | sed 's/value="//;s/"//')"
    log "$name: step=$step"
    local -a form
    case "$step" in
      database)
        form=(step=database internal=true type=postgres hostname= port=5432 database= username= password= locale=en_US atl_token="$token" submit=Next)
        ;;
      settings)
        form=(step=settings applicationTitle="BB Lab" baseUrl="$base" license-type=false license= locale=en_US atl_token="$token" submit=Next)
        ;;
      user)
        form=(step=user username="$SYSADMIN_USER" password="$SYSADMIN_PASSWORD" confirmPassword="$SYSADMIN_PASSWORD" fullname="$SYSADMIN_DISPLAYNAME" email="$SYSADMIN_EMAIL" skipJira="Go to Bitbucket" locale=en_US atl_token="$token")
        ;;
      jira)
        form=(step=jira setupJira=skip locale=en_US atl_token="$token" submit=Skip)
        ;;
      *)
        echo "$name: unexpected step '$step'" >&2
        rm -f "$jar"
        return 1
        ;;
    esac
    local -a curl_args=()
    local kv
    for kv in "${form[@]}"; do curl_args+=(-d "$kv"); done
    curl -s -L -b "$jar" -c "$jar" -X POST "$base/setup" "${curl_args[@]}" >/dev/null
    sleep 2
  done
  rm -f "$jar"
}

# --- verify admin user is live ----------------------------------------------
wait_sysadmin() {
  local name="$1" port="$2"
  local base="http://localhost:$port"
  log "$name: waiting for sysadmin '$SYSADMIN_USER' to resolve"
  local deadline=$((SECONDS + READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    if v="$(curl -s -u "$SYSADMIN_USER:$SYSADMIN_PASSWORD" \
            "$base/rest/api/1.0/users/$SYSADMIN_USER" | jq -r '.name // empty' 2>/dev/null)"; then
      if [[ "$v" == "$SYSADMIN_USER" ]]; then
        log "$name: READY (sysadmin '$v' live)"
        return 0
      fi
    fi
    sleep 5
  done
  echo "$name: sysadmin did not come up in ${READY_TIMEOUT}s" >&2
  return 1
}

patch_container "$A_NAME"
patch_container "$B_NAME"

wizard_complete "$A_NAME" "$A_HTTP_PORT"
wizard_complete "$B_NAME" "$B_HTTP_PORT"

wait_sysadmin "$A_NAME" "$A_HTTP_PORT"
wait_sysadmin "$B_NAME" "$B_HTTP_PORT"

log "Done (unlicensed mode)."
log "  A: http://localhost:$A_HTTP_PORT  B: http://localhost:$B_HTTP_PORT"
log "  NOTE: migration export/import still requires a real DC license (Phase 2/5)."