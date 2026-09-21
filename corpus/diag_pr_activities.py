#!/usr/bin/env python3
"""Dump a PR's activity timeline from an assembled archive, in document order,
to check for ordering anomalies (e.g. a COMMENT placed after a RESCOPED that
moved the anchor) and to inspect specific comment anchors.

Usage:
  python diag_pr_activities.py ARCHIVE PR_ID [BBS_COMMENT_ID...]

For each activity it prints: index, kind/action, timestamp (ISO), and, where
present, the commit hashes it carries (anchor fromHash/toHash, RESCOPED
hashes/commits, MERGED hash). If one or more BBS_COMMENT_IDs are given, the
comment records matching those ids are also expanded with their full anchor.
"""
import gzip
import json
import sys
import tarfile
from datetime import datetime, timezone


def load_pr_activities(archive, pr_id):
    t = tarfile.open(archive)
    for name in t.getnames():
        if f"/pullrequest/{pr_id}/activities.json" in name:
            data = t.extractfile(name).read()
            return json.loads(gzip.decompress(data))
    raise SystemExit(f"PR {pr_id} not found in {archive}")


def iso(ts):
    try:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts)


def sha_of(o, *keys):
    for k in keys:
        if isinstance(o.get(k), str) and len(o[k]) == 40 and all(c in "0123456789abcdef" for c in o[k]):
            return o[k][:12]
    return None


def fmt_anchor(a):
    if not a:
        return ""
    parts = []
    for k in ("fromHash", "toHash"):
        if a.get(k):
            parts.append(f"{k}={a[k][:12]}")
    if a.get("path"):
        parts.append(f"path={a['path']}")
    if a.get("line"):
        parts.append(f"line={a.get('line')}")
    if a.get("lineType"):
        parts.append(f"lineType={a.get('lineType')}")
    if a.get("orphaned"):
        parts.append("orphaned")
    return " ".join(parts)


def walk_comments(c, want, found):
    if c.get("id") in want:
        found.append(c)
    for r in c.get("comments") or []:
        walk_comments(r, want, found)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    archive, pr_id = sys.argv[1], int(sys.argv[2])
    want = {str(x) for x in sys.argv[3:]}
    acts = load_pr_activities(archive, pr_id)

    print(f"=== PR {pr_id} activity timeline ({len(acts)} records) ===")
    for i, a in enumerate(acts):
        kind = a.get("kind")
        extra = []
        if kind == "ACTIVITY":
            extra.append(f"action={a.get('action')}")
        elif kind == "COMMENT:ADDED":
            c = a.get("comment") or {}
            extra.append(f"commentId={c.get('id')}")
            extra.append(fmt_anchor((c.get("thread") or {}).get("anchor")))
        elif kind == "COMMENT:OTHER":
            extra.append(f"commentAction={a.get('commentAction')} "
                         f"commentId={a.get('commentId')}")
        elif kind == "RESCOPED":
            extra.append(f"prevFrom={sha_of(a, 'previousFromHash')} "
                         f"from={sha_of(a, 'fromHash')} "
                         f"prevTo={sha_of(a, 'previousToHash')} "
                         f"to={sha_of(a, 'toHash')}")
            commits = (a.get("commits") or [])
            extra.append(f"commits={len(commits)}")
        elif kind == "MERGED":
            extra.append(f"hash={sha_of(a, 'hash')}")
        ts = a.get("createdTimestamp")
        print(f"{i:3d} {kind:<18} @{iso(ts)}  {' '.join(extra)}")

    if want:
        print(f"\n=== matched comment ids present in PR {pr_id} ===")
        for a in acts:
            c = a.get("comment")
            if isinstance(c, dict):
                found = []
                walk_comments(c, want, found)
                for f in found:
                    print(f"\ncomment id={f.get('id')} created={iso(f.get('createdDate'))}")
                    print("  text:", (f.get("text") or "")[:200])
                    anchor = (f.get("thread") or {}).get("anchor")
                    if anchor:
                        print("  anchor:", json.dumps(anchor))
                    else:
                        print("  anchor: NONE (top-level, non-inline)")
    else:
        print(f"\n(no comment ids given)")


if __name__ == "__main__":
    main()