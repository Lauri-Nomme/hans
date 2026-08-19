#!/usr/bin/env python3
"""Phase 2 pairing check: verify the REST dumps and the extracted admin-export
archive describe the same repo state.  Prints per-PR field matches and the
activity-count delta (archive records activities in a different schema than
REST /activities, so counts are NOT expected to be equal).
"""
import gzip
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCH = HERE.parent / "ground-truth" / "export-a"


def _archive_repo_id():
    """Repo id inside the export (changes per lab re-create)."""
    root = ARCH / "com.atlassian.bitbucket.server.bitbucket-instance-migration_pullRequests" / "repository"
    if not root.exists():
        return None
    return next((p.name for p in root.iterdir() if p.is_dir()), None)


def main():
    rid = _archive_repo_id()
    if not rid:
        raise SystemExit("export archive not present (ground-truth/export-a)")
    rest_prs = {p["id"]: p
                for p in json.loads((HERE / "rest" / "pull-requests_FIX_golden.json").read_text())}
    failures = 0
    for pid in sorted(rest_prs):
        meta_p = (ARCH / "com.atlassian.bitbucket.server.bitbucket-instance-migration_pullRequests"
                  / "repository" / rid / "pullrequest" / str(pid) / "metadata.json.atl.gz")
        acts_p = (ARCH / "com.atlassian.bitbucket.server.bitbucket-instance-migration_pullRequests"
                  / "repository" / rid / "pullrequest" / str(pid) / "activities.json.atl.gz")
        if not meta_p.exists():
            print(f"PR{pid}: no archive metadata — archive predates this PR")
            continue
        meta = json.load(gzip.open(meta_p))
        acts = json.load(gzip.open(acts_p))
        r = rest_prs[pid]
        for field in ("state", "title", "description"):
            if meta.get(field) != r.get(field):
                print(f"PR{pid} {field}: archive={meta.get(field)!r} rest={r.get(field)!r} [MISMATCH]")
                failures += 1
        for ref in ("fromRef", "toRef"):
            a, b = meta[ref]["latestCommit"], r[ref]["latestCommit"]
            if a != b:
                print(f"PR{pid} {ref}.latestCommit: archive={a} rest={b} [MISMATCH]")
                failures += 1
        racts = json.loads((HERE / "rest" / f"pr_{pid}_activities.json").read_text())
        print(f"PR{pid} activities archive={len(acts)} rest={len(racts)} "
              f"(delta {len(acts)-len(racts)} — expected, different schemas)")
    print("ALL MATCH" if failures == 0 else f"{failures} mismatches")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())