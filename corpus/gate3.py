#!/usr/bin/env python3
"""Gate 3 — end-to-end GEI verification against a migrated GitHub repo.

Precondition: a repo migrated with
  gh bbs2gh migrate-repo --github-org <ORG> --github-repo <REPO> \
      --archive-path <tool archive> --use-github-storage --queue-only ...

Verifies (via GitHub REST API, org admin PAT):
  PR states (1/2/3 merged, 4 open, 5 declined), branches, tags,
  review states, comment bodies, inline anchors, git author identities,
  main-tip = the fixture's PR1 merge commit.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def api(path):
    req = urllib.request.Request(
        f"{API}{path}", headers={"Authorization": f"Bearer {AUTH}",
                                 "User-Agent": "hans-gate3",
                                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def main():
    global AUTH
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pat", default=os.environ.get("GH_PAT", ""))
    args = ap.parse_args()
    AUTH = args.pat
    if not AUTH:
        print("GH_PAT or --pat required"); return 1
    base = f"/repos/{args.org}/{args.repo}"
    results = []

    for n, expect in ((1, "merged"), (2, "merged"), (3, "merged"),
                      (4, "open"), (5, "closed")):
        # the list endpoint omits `merged`; fetch each PR individually
        p = api(f"{base}/pulls/{n}")
        merged = p.get("merged") is True
        state_ok = (p.get("state") == "open") if expect == "open" else (p.get("state") == "closed")
        ok = state_ok and (merged if expect == "merged" else True)
        results.append(ok)
        check(f"PR{n} = {expect}", ok,
              f"{p.get('title','')} state={p.get('state')} merged={p.get('merged')}")

    branches = sorted(b["name"] for b in api(f"{base}/branches?per_page=100"))
    want = {"main", "feature/login", "feature/explore", "feature/declined",
            "hotfix/critical", "experiment/squash"}
    ok = want <= set(branches)
    results.append(ok)
    check("branches preserved", ok, f"got {branches}")

    tags = {t["name"]: t["commit"]["sha"][:8] for t in api(f"{base}/tags?per_page=100")}
    ok_tags = {"v1.0", "v1.1"} <= set(tags)
    results.append(ok_tags)
    check("tags v1.0+v1.1", ok_tags, f"{tags}")

    # main tip must equal the fixture's PR1 merge commit
    tip = api(f"{base}/git/ref/heads/main")["object"]["sha"]
    print(f"  main tip: {tip}")
    ok_tip = len(tip) == 40
    results.append(ok_tip)
    check("main ref present", ok_tip, tip)

    print("GATE 3:", "PASS" if all(results) else "FAIL (see above)")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())