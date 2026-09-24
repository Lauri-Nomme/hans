#!/usr/bin/env python3
"""Check whether force-pushed-away (f-b-a) commits — and the inline-comment
anchors tied to them — are present (and reachable) in a migrated GitHub repo.

Theory being tested: a PR source tip that was force-pushed away is retained by
Bitbucket/hans only in reflogs (logs/stash-refs/.../from) and in
objects.atl.tar; GEI imports refs (branches/tags + refs/github-services/
pull-requests/*/from) but does NOT turn those reflog-only commits into
reachable GH refs. If so, an inline comment anchored at such a commit can't be
placed, and the commit may be absent from GH entirely.

This harvests candidate SHAs from the local Bitbucket scrape (no BB access
needed) and checks GH directly:

  --scrape DIR   bb-archiver scrape dir (has rest/ and usually git/)
  --repo O/R     migrated GitHub repo (e.g. dtrnd-data-ingest/sx-spectx-test3)
  --pat / GH_PAT GitHub token (repo read)

Candidate SHAs per PR (from rest/pr_<id>_activities.json + the PR list):
  * RESCOPED.previousFromHash      -> a replaced source tip  (f-b-a candidate)
  * RESCOPED.fromHash/toHash       -> post-push tips
  * comment anchor fromHash/toHash -> every inline comment anchor
  * reviewer lastReviewedCommit    -> what a reviewer last approved
  * fromRef/toRef.latestCommit     -> current PR refs

For each unique SHA it reports:
  * bb_reachable : ancestor of the PR's CURRENT head in the local mirror?
                   (a previousFromHash that is NOT an ancestor = truly f-b-a)
  * gh_present   : GET /repos/{repo}/git/commits/{sha} -> object exists on GH?
                   (200 = exists, even if unreachable; 404 = GEI never pushed it)
  * gh_reachable : ancestor of any GH ref? (only with --gh-git, a local clone)

Output: a summary table + JSONL of every (pr, sha, kind, flags) to --out.

Usage (Windows workbox):
  set GH_PAT=ghp_...
  python corpus\\diag_fba_commits.py ^
      --scrape D:\\wrk\\hans\\scrape\\SXspectx20260921_1 ^
      --repo dtrnd-data-ingest/sx-spectx-test3 ^
      --out fba.jsonl
  :: optional, for GH reachability (clone once):
  git clone --mirror https://x-access-token:%GH_PAT%@github.com/dtrnd-data-ingest/sx-spectx-test3.git gh-mirror
  python corpus\\diag_fba_commits.py --scrape ... --repo ... --gh-git gh-mirror --out fba.jsonl
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


# ---------------------------------------------------------------- scrape scan
def load_prs(rest):
    hits = sorted(rest.glob("pull-requests_*.json"))
    if not hits:
        raise SystemExit(f"no pull-requests_*.json under {rest}")
    return json.loads(hits[0].read_text())


def scan_pr(rest, pid):
    """Return list of (sha, kind, extra) for one PR."""
    out = []
    f = rest / f"pr_{pid}_activities.json"
    if not f.exists():
        return out
    try:
        acts = json.loads(f.read_text())
    except Exception:
        return out

    def add(sha, kind, **extra):
        if isinstance(sha, str) and HEX40.match(sha):
            out.append((sha, kind, extra))

    def walk_comment(c, depth=0):
        if not isinstance(c, dict):
            return
        a = c.get("anchor") or {}
        for k in ("fromHash", "toHash"):
            add(a.get(k), "comment_anchor",
                orphaned=bool(a.get("orphaned")), path=a.get("path"),
                line=a.get("line"), depth=depth,
                text=(c.get("text") or "")[:80])
        for r in c.get("comments") or []:
            walk_comment(r, depth + 1)

    for a in acts:
        action = a.get("action")
        if action == "COMMENTED" and isinstance(a.get("comment"), dict):
            walk_comment(a["comment"])
        elif action == "RESCOPED":
            for k in ("previousFromHash", "fromHash", "toHash",
                      "previousToHash"):
                add(a.get(k), "rescoped_" + k)
            for side in ("added", "removed"):
                for c in (a.get(side) or {}).get("commits") or []:
                    add((c or {}).get("id"), "rescoped_commit")
        elif action == "MERGED" and isinstance(a.get("commit"), dict):
            add(a["commit"].get("id"), "merged")
    return out


# ------------------------------------------------------------------ local git
def bb_reachable(bbgit, sha, head):
    """True/False/None: is sha an ancestor of head in the local BB mirror?"""
    if not bbgit or not (Path(bbgit) / "objects").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(bbgit), "merge-base",
                            "--is-ancestor", sha, head],
                           capture_output=True, timeout=30)
        if r.returncode == 0:
            return True
        if r.returncode == 1:
            return False
        return None            # object missing / not a commit
    except Exception:
        return None


def gh_reachable_set(ghgit):
    """Set of all commit SHAs reachable from any ref in a GH clone."""
    log(f"gh-git: rev-list --all in {ghgit}")
    r = subprocess.run(["git", "-C", str(ghgit), "rev-list", "--all"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        log(f"gh-git rev-list failed: {r.stderr[:200]}")
        return set()
    return set(r.stdout.split())


# --------------------------------------------------------------------- GH API
class GH:
    def __init__(self, token):
        self.token = token
        self.remaining = None
        self.reset = None
        self.calls = 0

    def _wait(self):
        if self.remaining is not None and self.remaining <= 5:
            wait = max((self.reset or int(time.time())) - int(time.time()) + 5, 30)
            log(f"rate limit low ({self.remaining}) — sleeping {wait}s")
            time.sleep(wait)

    def present(self, repo, sha):
        """True if the object exists in the repo (200), False if 404."""
        url = f"{API}/repos/{repo}/git/commits/{sha}"
        for attempt in range(4):
            self._wait()
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "hans-fba-check",
                "Accept": "application/vnd.github+json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    self.calls += 1
                    self._limits(r.headers)
                    return True
            except urllib.error.HTTPError as e:
                self.calls += 1
                self._limits(e.headers or {})
                if e.code == 404:
                    return False
                if e.code == 403 and "rate limit" in e.read().decode()[:200].lower():
                    time.sleep(max((self.reset or 0) - int(time.time()) + 5, 30))
                    continue
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(min(2 ** attempt * 2, 60))
                    continue
                raise
            except Exception:
                time.sleep(min(2 ** attempt * 2, 60))
        raise RuntimeError(f"gave up on {url}")

    def _limits(self, h):
        rl = h.get("X-RateLimit-Remaining")
        if rl is not None:
            self.remaining = int(rl)
        rs = h.get("X-RateLimit-Reset")
        if rs:
            self.reset = int(rs)


# ----------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scrape", required=True)
    ap.add_argument("--repo", required=True, help="ORG/REPO on GitHub")
    ap.add_argument("--pat", default=os.environ.get("GH_PAT", ""))
    ap.add_argument("--bb-git", default=None,
                    help="BB mirror for reachability (default <scrape>/git)")
    ap.add_argument("--gh-git", default=None,
                    help="local clone/mirror of the GH repo for reachability")
    ap.add_argument("--out", default=None, help="JSONL output path")
    ap.add_argument("--limit-prs", type=int, default=0)
    ap.add_argument("--fba-only", action="store_true",
                    help="only SHAs that are previousFromHash of a RESCOPED")
    args = ap.parse_args()
    if not args.pat:
        log("--pat or GH_PAT required")
        return 2

    scrape = Path(args.scrape)
    rest = scrape / "rest"
    bbgit = args.bb_git or (scrape / "git")
    prs = load_prs(rest)
    heads = {p["id"]: (p.get("fromRef") or {}).get("latestCommit")
             for p in prs}
    log(f"scrape {scrape}: {len(prs)} PRs; bb mirror {bbgit}")

    # collect candidates: sha -> {kinds, prs, orphaned, text}
    cand = {}
    for p in prs:
        pid = p["id"]
        if args.limit_prs and pid > args.limit_prs:
            continue
        for sha, kind, extra in scan_pr(rest, pid):
            if args.fba_only and kind != "rescoped_previousFromHash":
                continue
            e = cand.setdefault(sha, {"kinds": set(), "prs": set(),
                                      "orphaned": False, "text": ""})
            e["kinds"].add(kind)
            e["prs"].add(pid)
            if extra.get("orphaned"):
                e["orphaned"] = True
            if extra.get("text"):
                e["text"] = extra["text"]
    log(f"candidate SHAs: {len(cand)}")

    gh_reach = gh_reachable_set(args.gh_git) if args.gh_git else None
    gh = GH(args.pat)
    out = open(args.out, "w", encoding="utf-8") if args.out else None

    # Only SHAs that were a PR *source* tip / comment anchor can be "force-
    # pushed away" relative to the PR head. Base-branch tips (toRef) and merge
    # commits live on main, so they are legitimately not ancestors of the PR
    # head and must NOT be classified as f-b-a.
    SOURCE_KINDS = {"rescoped_previousFromHash", "rescoped_fromHash",
                    "rescoped_toHash", "rescoped_previousToHash",
                    "comment_anchor"}

    tally = {"total": 0, "gh_present": 0, "gh_absent": 0,
             "bb_fba": 0, "gh_present_and_bb_fba": 0, "gh_present_and_gh_unreachable": 0}
    rows = []
    for i, (sha, e) in enumerate(sorted(cand.items()), 1):
        is_source = bool(e["kinds"] & SOURCE_KINDS)
        reaches = []
        if is_source:
            for pid in e["prs"]:
                h = heads.get(pid)
                if h:
                    r = bb_reachable(bbgit, sha, h)
                    if r is not None:
                        reaches.append(r)
        bb_reach = None if not reaches else any(reaches)
        is_fba = (bb_reach is False
                  and "rescoped_previousFromHash" in e["kinds"])
        present = gh.present(args.repo, sha)
        ghr = (sha in gh_reach) if gh_reach is not None else None
        tally["total"] += 1
        tally["gh_present" if present else "gh_absent"] += 1
        if is_fba:
            tally["bb_fba"] += 1
            if present:
                tally["gh_present_and_bb_fba"] += 1
        if present and ghr is False:
            tally["gh_present_and_gh_unreachable"] += 1
        row = {"prs": sorted(e["prs"]), "sha": sha,
               "kinds": sorted(e["kinds"]), "orphaned_anchor": e["orphaned"],
               "text": e["text"], "bb_reachable": bb_reach,
               "force_pushed_away": is_fba,
               "gh_present": present, "gh_reachable": ghr}
        rows.append(row)
        if out:
            out.write(json.dumps(row) + "\n")
        if i % 100 == 0:
            log(f"checked {i}/{len(cand)} (gh calls={gh.calls}, "
                f"remaining={gh.remaining})")

    if out:
        out.close()

    log("=== summary ===")
    log(json.dumps(tally))
    # the decisive view: SHAs that were a replaced PR source tip locally
    fba = [r for r in rows if r["force_pushed_away"]]
    log(f"force-pushed-away source tips: {len(fba)}")
    for r in fba[:40]:
        log(f"  PR{r['prs']} {r['sha'][:12]} present_on_gh={r['gh_present']} "
            f"gh_reachable={r['gh_reachable']} orphaned={r['orphaned_anchor']} "
            f"{r['text']!r}")
    orphan = [r for r in rows if r["orphaned_anchor"]]
    log(f"orphaned-anchor SHAs: {len(orphan)}; "
        f"of those present on GH: {sum(1 for r in orphan if r['gh_present'])}")
    src = [r for r in rows if r["bb_reachable"] is False and r["kinds"]
           and "comment_anchor" in r["kinds"]]
    log(f"comment-anchor SHAs not reachable from their PR head: {len(src)}; "
        f"present on GH: {sum(1 for r in src if r['gh_present'])}")
    if args.out:
        log(f"wrote {len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
