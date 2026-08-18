#!/usr/bin/env bash
# Phase 0 lab tear-down. Removes containers but keeps instance data under
# lab/home/ (set PURGE=1 to delete data too).
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/config.env"

for name in "$A_NAME" "$B_NAME"; do
  if $NERDCTL_CMD container inspect "$name" >/dev/null 2>&1; then
    echo "[lab-down] removing container $name"
    $NERDCTL_CMD rm -f "$name" >/dev/null
  else
    echo "[lab-down] $name not present"
  fi
done

if [[ "${PURGE:-0}" == "1" ]]; then
  echo "[lab-down] purging data under $LAB_DIR/home"
  rm -rf "$LAB_DIR/home"
fi

echo "[lab-down] done. instance data (if kept) is under $LAB_DIR/home/"