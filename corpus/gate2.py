#!/usr/bin/env python3
"""Gate 2 — round-trip: compare a REST scrape of bb-lab-a (source) against a
scrape of bb-lab-b after importing the tool's archive.

Target industry note: import creates STUB users on the target (displayName=slug,
active=false, no email) — expected Bitbucket behavior for unknown users; the only
email fidelity in the whole system is inside git author/committer objects. So this
comparison keys users by slug, not their identity fields.
"""
import json
import sys
from pathlib import Path

PR_IDS = (1, 2, 3, 4, 5, 6, 7)


def load(d, name):
    p = Path(d) / "rest" / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def norm_pr(pr):
    pr = pr or {}
    fr = pr.get("fromRef") or {}
    tr = pr.get("toRef") or {}
    return {
        "id": pr.get("id"),
        "version": pr.get("version"),
        "state": pr.get("state"),
        "title": pr.get("title"),
        "description": pr.get("description"),
        "draft": pr.get("draft"),
        "closedDate": pr.get("closedDate"),
        "fromRefTip": fr.get("latestCommit"),
        "toRefTip": tr.get("latestCommit"),
        "author": (pr.get("author") or {}).get("user") or {},
        "authorSlug": ((pr.get("author") or {}).get("user") or {}).get("slug"),
        "reviewers": sorted({(p.get("user") or {}).get("slug")
                             for p in (pr.get("reviewers") or [])}),
        "participants": sorted({((p.get("user") or {}).get("slug"), p.get("role"),
                                 bool(p.get("approved")), p.get("status"))
                                for p in (pr.get("participants") or [])}),
    }


def norm_act(a):
    a = a or {}
    c = a.get("comment") or {}
    return json.dumps({
        "action": a.get("action"),
        "createdDate": a.get("createdDate"),
        "commentAction": a.get("commentAction"),
        "slug": (a.get("user") or {}).get("slug"),
        # commentId excluded: target reallocates comment ids on import
        "commentId": None,
    }, sort_keys=True)


def check_prs(A, B, verbosity=1):
    fails = 0
    for pid in PR_IDS:
        pa, pb = load(A, f"pr_{pid}"), load(B, f"pr_{pid}")
        if pa is None or pb is None:
            print(f"PR{pid}: missing on one side (A={pa is not None} B={pb is not None})")
            fails += 1
            continue
        na, nb = norm_pr(pa), norm_pr(pb)
        ns_a = dict(na); ns_b = dict(nb)
        # user identity fields differ by design on import (stub users); compare slug only
        ns_a.pop("author", None); ns_b.pop("author", None)
        ns_a.pop("authorSlug", None); ns_b.pop("authorSlug", None)
        if ns_a != ns_b:
            diffs = {k for k in set(ns_a) | set(ns_b) if ns_a.get(k) != ns_b.get(k)}
            fails += 1
            print(f"PR{pid}: DIFF {sorted(diffs)}")
            for k in sorted(diffs):
                print(f"   {k}: A={str(ns_a.get(k))[:140]} B={str(ns_b.get(k))[:140]}")
        elif na.get("authorSlug") != nb.get("authorSlug"):
            fails += 1
            print(f"PR{pid}: author slug {na.get('authorSlug')} vs {nb.get('authorSlug')}")
        else:
            print(f"PR{pid}: match (author={na.get('authorSlug')})")
    return fails


def load_pr(d, pid):
    return load(d, f"pr_{pid}")


def check_activities(A, B):
    fails = 0
    for pid in PR_IDS:
        aa, ab = load(A, f"pr_{pid}_activities"), load(B, f"pr_{pid}_activities")
        if aa is None or ab is None:
            print(f"PR{pid} activities: missing on one side")
            fails += 1
            continue
        sa = sorted(norm_act(x) for x in aa)
        sb = sorted(norm_act(x) for x in ab)
        # normalize author identity email/display differences out
        sa = [x for x in sa]
        sb = [x for x in sb]
        if sa != sb:
            ao = [x for x in sa if x not in sb]
            bo = [x for x in sb if x not in sa]
            print(f"PR{pid} activities: {len(aa)} vs {len(ab)} "
                  f"(A-only={len(ao)} B-only={len(bo)})")
            for x in ao[:6]:
                print("   A-only:", x)
            for x in bo[:6]:
                print("   B-only:", x)
            fails += 1
        else:
            print(f"PR{pid} activities: match ({len(aa)})")
    return fails


def check_refs(A, B):
    """Branches + tags (id, latestCommit, hash for annotated tags) from the scrapes."""
    fails = 0
    for kind in ("branches", "tags"):
        ra = load(A, f"{kind}_{'FIX'}_golden")
        rb = load(B, f"{kind}_{'FIX'}_golden")
        if ra is None or rb is None:
            print(f"{kind}: missing on one side"); fails += 1; continue
        def sig(items):
            return sorted({(x.get("id"), x.get("latestCommit"), x.get("hash"))
                           for x in (items or [])})
        sa, sb = sig(ra), sig(rb)
        if sa != sb:
            fails += 1
            print(f"{kind}: DIFF")
            for x in sa:
                if x not in sb: print("   A-only:", x)
            for x in sb:
                if x not in sa: print("   B-only:", x)
        else:
            print(f"{kind}: match ({len(sa)})")
    return fails


def _git_objects(gitdir):
    """sha -> (type, raw bytes) for every object in a bare repo."""
    import subprocess
    git = lambda *a: subprocess.run(["git", "-C", gitdir, *a],
                                    capture_output=True, check=True)
    out = git("cat-file", "--batch-all-objects",
              "--batch-check=%(objectname) %(objecttype)").stdout.decode()
    objs = {}
    for line in out.splitlines():
        sha, typ = line.split()
        raw = git("cat-file", typ, sha).stdout
        objs[sha] = (typ, raw)
    return objs


def check_git_objects(A, B):
    """Object-wise git repo comparison via the scraped mirrors
    (`git cat-file`, per the plan — not byte-level tar compare)."""
    import subprocess
    ga, gb = f"{A}/git", f"{B}/git"
    for g in (ga, gb):
        if not (subprocess.run(["git", "-C", g, "rev-parse", "--is-bare-repository"],
                               capture_output=True).returncode == 0):
            print(f"git mirror missing at {g}")
            return 1
    ma, mb = _git_objects(ga), _git_objects(gb)
    ka, kb = set(ma), set(mb)
    if ka != kb:
        print(f"git objects: set differ (A={len(ka)} B={len(kb)})")
        print("   A-only:", sorted(ka - kb)[:5])
        print("   B-only:", sorted(kb - ka)[:5])
        return 1
    diffs = [sha for sha in ka if ma[sha] != mb[sha]]
    if diffs:
        print(f"git objects: {len(diffs)} content diffs: {sorted(diffs)[:5]}")
        return 1
    print(f"git objects: {len(ka)} objects identical (set + content)")
    return 0


def check_tasks(A, B):
    """Tasks (severity BLOCKER comments) must survive the round trip with
    fidelity. Comment ids differ (target reallocates), so compare by task text
    with severity/state and (for resolved) resolvedDate/resolver slug."""
    fails = 0
    for pid in PR_IDS:
        aa, ab = load(A, f"pr_{pid}_activities"), load(B, f"pr_{pid}_activities")
        if aa is None or ab is None:
            continue
        def tasks(acts):
            out = {}
            for a in (acts or []):
                c = a.get("comment") or {}
                if c.get("severity") == "BLOCKER":
                    r = (c.get("resolver") or {}).get("slug")
                    out[c.get("text")] = (c.get("severity"), c.get("state"),
                                          c.get("resolvedDate"), r)
            return out
        ta, tb = tasks(aa), tasks(ab)
        if ta != tb:
            fails += 1
            print(f"PR{pid}: tasks differ A={ta} B={tb}")
        elif ta:
            print(f"PR{pid}: {len(ta)} task(s) preserved ({list(ta)})")
    return fails


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("scrape_a")
    ap.add_argument("scrape_b")
    args = ap.parse_args()
    f = 0
    f += check_prs(args.scrape_a, args.scrape_b)
    f += check_activities(args.scrape_a, args.scrape_b)
    f += check_refs(args.scrape_a, args.scrape_b)
    f += check_git_objects(args.scrape_a, args.scrape_b)
    f += check_tasks(args.scrape_a, args.scrape_b)
    print("GATE 2:", "PASS" if f == 0 else f"FAIL ({f})")
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())