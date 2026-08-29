#!/usr/bin/env python3
"""Gate 2 — round-trip: compare a REST scrape of bb-lab-a (source) against a
scrape of bb-lab-b after importing the tool's archive.

Target industry note: import creates STUB users on the target (displayName=slug,
active=false, no email) — expected Bitbucket behavior for unknown users; the only
email fidelity in the whole system is inside git author/committer objects. So this
comparison keys users by slug, not their identity fields.
"""
import json
import subprocess
import sys
from pathlib import Path


def load(d, name):
    p = Path(d) / "rest" / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _proj_repo(d):
    """project/repo for a scrape dir (index.json), else infer from branches_*."""
    idx = Path(d) / "index.json"
    if idx.exists():
        i = json.loads(idx.read_text())
        return i["project"], i["repo"]
    hit = next(Path(d, "rest").glob("branches_*.json"), None)
    if hit:
        _, proj, repo = hit.stem.split("_", 2)
        return proj, repo
    return "FIX", "golden"


def pr_ids(d):
    """Every PR id present in a scrape (from the pull-requests list)."""
    proj, repo = _proj_repo(d)
    prs = load(d, f"pull-requests_{proj}_{repo}") or []
    return sorted({p["id"] for p in prs})


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
                             for p in (pr.get("reviewers") or [])}, key=str),
        "participants": sorted({((p.get("user") or {}).get("slug"), p.get("role"),
                                 bool(p.get("approved")), p.get("status"))
                                for p in (pr.get("participants") or [])}, key=str),
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


def check_prs(A, B, pids, verbosity=1):
    fails = 0
    for pid in pids:
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


def check_activities(A, B, pids):
    fails = 0
    for pid in pids:
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
    proj_a, repo_a = _proj_repo(A)
    proj_b, repo_b = _proj_repo(B)
    for kind in ("branches", "tags"):
        ra = load(A, f"{kind}_{proj_a}_{repo_a}")
        rb = load(B, f"{kind}_{proj_b}_{repo_b}")
        if ra is None or rb is None:
            print(f"{kind}: missing on one side"); fails += 1; continue
        def sig(items):
            return sorted({(x.get("id"), x.get("latestCommit"), x.get("hash"))
                           for x in (items or [])}, key=str)
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


def _object_set(gitdir):
    """{sha: type} for every object in a bare repo, one pass, no bodies.

    Git object SHAs are content hashes, so the set alone decides migration
    fidelity — equal sets imply equal contents."""
    out = subprocess.run(["git", "-C", gitdir, "cat-file", "--batch-all-objects",
                          "--batch-check=%(objectname) %(objecttype)"],
                         capture_output=True, check=True).stdout.decode()
    objs = {}
    for line in out.splitlines():
        sha, typ = line.split()
        objs[sha] = typ
    return objs


def _fsck(gitdir):
    """Re-hash every object in a mirror (streaming) and return problem lines.

    git verifies object hashes only at ingest (fetch -> index-pack /
    unpack-objects); plain reads do not re-hash, so post-ingest corruption
    (bit rot, a bad repack write) is servable by cat-file without complaint
    and invisible to sha-set comparison. `git fsck` catches exactly that
    class with O(1) memory."""
    r = subprocess.run(["git", "-C", gitdir, "fsck", "--no-dangling",
                        "--no-progress"],
                       capture_output=True, text=True)
    errs = [ln for ln in (r.stderr or "").splitlines()
            if ln.startswith(("error:", "fatal:"))]
    if r.returncode != 0 and not errs:
        errs.append(f"git fsck exited {r.returncode}: {(r.stderr or '')[:200]}")
    return errs


def check_git_objects(A, B):
    """Object-wise git repo comparison via the scraped mirrors.

    Migration fidelity: sha + type sets (`--batch-check`, no bodies held in
    RAM). Mirror integrity: `git fsck` per side, which re-hashes every
    object — the one class of difference (on-disk corruption) that equal
    SHAs cannot rule out."""
    ga, gb = f"{A}/git", f"{B}/git"
    for g in (ga, gb):
        if not (subprocess.run(["git", "-C", g, "rev-parse", "--is-bare-repository"],
                               capture_output=True).returncode == 0):
            print(f"git mirror missing at {g}")
            return 1
    fails = 0
    for g in (ga, gb):
        errs = _fsck(g)
        if errs:
            fails += 1
            print(f"git fsck {g}: {len(errs)} problem(s)")
            for e in errs[:5]:
                print("   ", e)
        else:
            print(f"git fsck {g}: clean")
    ma, mb = _object_set(ga), _object_set(gb)
    ka, kb = set(ma), set(mb)
    if ka != kb:
        print(f"git objects: set differ (A={len(ka)} B={len(kb)})")
        print("   A-only:", sorted(ka - kb)[:5])
        print("   B-only:", sorted(kb - ka)[:5])
        return fails + 1
    print(f"git objects: {len(ka)} objects identical (sha + type set)")
    return fails


def check_tasks(A, B, pids):
    """Tasks (severity BLOCKER comments) must survive the round trip with
    fidelity. Comment ids differ (target reallocates), so compare by task text
    with severity/state and (for resolved) resolvedDate/resolver slug."""
    fails = 0
    for pid in pids:
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
    pids = sorted(set(pr_ids(args.scrape_a)) | set(pr_ids(args.scrape_b)))
    print(f"[gate2] comparing {len(pids)} PRs "
          f"({_proj_repo(args.scrape_a)[0]}/{_proj_repo(args.scrape_a)[1]})")
    f = 0
    f += check_prs(args.scrape_a, args.scrape_b, pids)
    f += check_activities(args.scrape_a, args.scrape_b, pids)
    f += check_refs(args.scrape_a, args.scrape_b)
    f += check_git_objects(args.scrape_a, args.scrape_b)
    f += check_tasks(args.scrape_a, args.scrape_b, pids)
    print("GATE 2:", "PASS" if f == 0 else f"FAIL ({f})")
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())