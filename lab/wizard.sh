#!/usr/bin/env bash
# Drive the 9.4.18 setup wizard to completion for a running container.
# The setup.* properties in bitbucket.properties do NOT reliably self-complete
# this build's wizard, so a manual walk of the HTML form is required (license
# posted from LAB_LICENSE_FILE when present, else unlicensed). Idempotent:
# containers that already have the sysadmin user are skipped.
#
#   ./lab/wizard.sh            # walk every running container
#   ./lab/wizard.sh bb-lab-b   # walk only bb-lab-b
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/config.env"

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }

log() { printf '[wizard] %s\n' "$*"; }

json_get() {
  local key="$1"
  python3 -c "import json,sys
try: o=json.load(sys.stdin)
except Exception: sys.exit(1)
v=o.get('$key','')
print(v if v else '')"
}

is_setup_done() {
  local name="$1" port="$2"
  local base="http://localhost:$port"
  if v="$(curl -s -u "$SYSADMIN_USER:$SYSADMIN_PASSWORD" \
           "$base/rest/api/1.0/users/$SYSADMIN_USER" | json_get name 2>/dev/null)"; then
    [[ "$v" == "$SYSADMIN_USER" ]]
  else
    return 1
  fi
}

walk_wizard() {
  local name="$1" port="$2"
  local base="http://localhost:$port"
  if is_setup_done "$name" "$port"; then
    log "$name: setup already complete, skipping"
    return 0
  fi
  local jar; jar="$(mktemp)"
  log "$name: waiting for app to come up"
  local deadline=$((SECONDS + READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    curl -sf -o /dev/null "$base/status" && break
    sleep 5
  done
  curl -sf -o /dev/null "$base/status" || { echo "$name: app did not come up" >&2; return 1; }

  local license="false"
  local license_str=""
  if [[ -s "$LAB_LICENSE_FILE" ]]; then
    license="true"
    license_str="$(tr -d '\n\r ' < "$LAB_LICENSE_FILE")"
  fi

  log "$name: walking wizard steps (license=$license)"
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
        form=(step=settings applicationTitle="BB Lab" baseUrl="$base" license-type="$license" license="$license_str" locale=en_US atl_token="$token" submit=Next)
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

wait_sysadmin() {
  local name="$1" port="$2"
  local base="http://localhost:$port"
  log "$name: waiting for sysadmin '$SYSADMIN_USER' to resolve"
  local deadline=$((SECONDS + READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    if v="$(curl -s -u "$SYSADMIN_USER:$SYSADMIN_PASSWORD" \
            "$base/rest/api/1.0/users/$SYSADMIN_USER" | json_get name 2>/dev/null)"; then
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

targets=("$@")
if [[ ${#targets[@]} -eq 0 ]]; then
  targets=("$A_NAME" "$B_NAME")
fi

for name in "${targets[@]}"; do
  case "$name" in
    "$A_NAME") port="$A_HTTP_PORT" ;;
    "$B_NAME") port="$B_HTTP_PORT" ;;
    *) echo "unknown container '$name' (expected $A_NAME or $B_NAME)" >&2; exit 1 ;;
  esac
  if ! $NERDCTL_CMD container inspect "$name" >/dev/null 2>&1; then
    echo "container $name not present - run ./lab/lab-up.sh first" >&2
    exit 1
  fi
  walk_wizard "$name" "$port"
  wait_sysadmin "$name" "$port"
done

log "Done."