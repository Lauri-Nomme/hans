#!/usr/bin/env python3
"""Show what the admin-export archive recorded for a PR's activities, so we can
match our emitter output byte-for-byte (RCA ground truth for activities like
MERGED-without-commit).

Usage:
    python3 corpus/peek_archive_activities.py <export.tar> <pid> [pid...]
"""
import gzip
import json
import sys
import tarfile
from pathlib import Path


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: peek_archive_activities.py <export.tar> <pid...>")
    tar_path, pids = sys.argv[1], sys.argv[2:]
    targets = {f"pullrequest/{pid}/activities.json.atl.gz" for pid in pids}
    found = {}
    with tarfile.open(tar_path) as tar:
        for m in tar:
            if m.isfile() and "/pullrequest/" in m.name:
                pid = m.name.rsplit("/pullrequest/", 1)[1].split("/")[0]
                if f"pullrequest/{pid}/activities.json.atl.gz" in targets:
                    fh = tar.extractfile(m)
                    if fh is None:
                        continue
                    data = fh.read()
                    found[pid] = json.loads(gzip.decompress(data))
    for pid in pids:
        if pid not in found:
            print(f"[{pid}] NO activities.json.atl.gz in archive "
                  f"(PR predates export?)")
            continue
        acts = found[pid]
        for i, a in enumerate(acts or []):
            if a.get("action") == "MERGED":
                print(f"[{pid}] #{i} MERGED (raw archive activity):")
                print(json.dumps(a, indent=2))
                print()


if __name__ == "__main__":
    sys.exit(main())