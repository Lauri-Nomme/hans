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
  --repo O/R     migrated GitHub repo (e.g. MY-ORG/my-repo)
  --pat / GH_PAT GitHub token (repo read)

Candidate SHAs per PR (from rest/pr_<id>_activities.json + the PR list):
  * RESCOPED.previousFromHash      -> a replaced source tip  (f-b-a candidate)
  * RESCOPED.fromHash/toHash       -> post-push tips
  * comment anchor fromHash/toHash -> every inline comment anchor
  * reviewer lastReviewedCommit    -> what a reviewer last approved
  * fromRef/toRef.latestCommit     -> current PR refs

For each unique SHA it reports:
  * bb_reachable : ancestor of the PR's CURRENT head in the local BB mirror?
                   (a previousFromHash that is NOT an ancestor = truly f-b-a)
  * gh_reachable : is the commit reachable from any GH ref? Checked FIRST
                   against a local mirror (--gh-git, cloned once if absent):
                   if it's in the mirror it is present, with NO API call.
  * gh_present   : only for SHAs the mirror lacks (absent, or pushed but
                   unreferenced): GET /repos/{repo}/git/commits/{sha}
                   (200 = exists on GH but unreferenced, 404 = never pushed).

Output: a summary table + JSONL of every (pr, sha, kind, flags) to --out.
Re-running with the same --out resumes: SHAs already present are skipped and
the final summary is computed from the whole file. Progress shows rate + ETA.

Usage (Windows workbox):
  set GH_PAT=ghp_...
  python corpus\\diag_fba_commits.py ^
      --scrape D:\\path\\to\\scrape ^
      --repo MY-ORG/my-repo ^
      --gh-git gh-mirror ^        :: cloned automatically if missing
      --out fba.jsonl
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


def ensure_gh_mirror(repo, pat, path):
    """Make sure `path` is a mirror clone of the GH repo (clone once if not).

    A mirror holds every object reachable from a ref — enough to answer the
    reachability question locally, so the GH API is only needed for SHAs the
    mirror is missing."""
    if (Path(path) / "objects").exists():
        return path
    url = f"https://x-access-token:{pat}@github.com/{repo}.git"
    log(f"gh-git: cloning mirror into {path} (one time, this can take a while)")
    r = subprocess.run(["git", "clone", "--quiet", "--mirror", url, str(path)],
                       capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        raise SystemExit(f"gh-git: clone failed: {r.stderr[:300]}")
    # don't persist the token in the mirror's config
    subprocess.run(["git", "-C", str(path), "remote", "set-url", "origin",
                    f"https://github.com/{repo}.git"], capture_output=True)
    return path


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

    def validate(self, repo):
        """Fail fast with a clear message on bad credentials / no access."""
        req = urllib.request.Request(f"{API}/repos/{repo}", headers={
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "hans-fba-check",
            "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._limits(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SystemExit("GitHub 401 Unauthorized — bad/expired token. "
                                 "Pass --pat or set GH_PAT.")
            if e.code == 404:
                raise SystemExit(f"GitHub 404 for {repo} — repo not found or "
                                 f"this token has no access.")
            raise

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
                    help="local mirror of the GH repo; checked first so only "
                         "missing SHAs hit the API. Cloned automatically if the "
                         "path does not exist.")
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

    gh_reach = None
    if args.gh_git:
        ensure_gh_mirror(args.repo, args.pat, args.gh_git)
        gh_reach = gh_reachable_set(args.gh_git)

    # Only SHAs that were a PR *source* tip / comment anchor can be "force-
    # pushed away" relative to the PR head. Base-branch tips (toRef) and merge
    # commits live on main, so they are legitimately not ancestors of the PR
    # head and must NOT be classified as f-b-a.
    SOURCE_KINDS = {"rescoped_previousFromHash", "rescoped_fromHash",
                    "rescoped_toHash", "rescoped_previousToHash",
                    "comment_anchor"}

    # Resume: a rate-limited run takes hours (1 API call per SHA), so skip any
    # SHA already written to --out and append rather than restart.
    done_shas = set()
    if args.out and os.path.exists(args.out):
        try:
            for line in open(args.out, encoding="utf-8"):
                line = line.strip()
                if line:
                    done_shas.add(json.loads(line)["sha"])
        except Exception:
            pass
    todo = {s: e for s, e in cand.items() if s not in done_shas}
    log(f"to check: {len(todo)}" +
        (f" (resuming; {len(done_shas)} already in {args.out})" if done_shas else ""))

    gh = GH(args.pat)
    gh.validate(args.repo)
    out = (open(args.out, "a", encoding="utf-8") if done_shas
           else open(args.out, "w", encoding="utf-8")) if args.out else None

    total = len(todo)
    last_t = time.time()
    last_i = 0
    for i, (sha, e) in enumerate(sorted(todo.items()), 1):
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
        # Local GH mirror first: a commit reachable from a ref is present by
        # definition, so NO API call is needed. Only SHAs the mirror lacks
        # (absent, or pushed-but-unreferenced) go to the GH API.
        if gh_reach is not None and sha in gh_reach:
            present, ghr = True, True
        else:
            present = gh.present(args.repo, sha)
            ghr = False if gh_reach is not None else None
        row = {"prs": sorted(e["prs"]), "sha": sha,
               "kinds": sorted(e["kinds"]), "orphaned_anchor": e["orphaned"],
               "text": e["text"], "bb_reachable": bb_reach,
               "force_pushed_away": is_fba,
               "gh_present": present, "gh_reachable": ghr}
        if out:
            out.write(json.dumps(row) + "\n")
            out.flush()
        if i % 100 == 0 or i == total:
            now = time.time()
            rate = (i - last_i) / max(now - last_t, 0.001)   # windowed
            eta = (total - i) / max(rate, 1e-9)
            log(f"checked {i}/{total} ({100.0*i/total:4.1f}%, {rate:,.1f}/s, "
                f"ETA {int(eta)//3600}:{int(eta)%3600//60:02d}:{int(eta)%60:02d}) "
                f"gh-calls={gh.calls} gh-budget-remaining={gh.remaining}")
            last_t, last_i = now, i

    if out:
        out.close()

    # Summary from the full --out file (so a resumed run reports complete
    # numbers, not just this session's rows).
    all_rows = []
    if args.out and os.path.exists(args.out):
        for line in open(args.out, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    all_rows.append(json.loads(line))
                except Exception:
                    pass

    tally = {"total": 0, "gh_present": 0, "gh_absent": 0,
             "bb_fba": 0, "gh_present_and_bb_fba": 0,
             "gh_present_and_gh_unreachable": 0}
    for r in all_rows:
        tally["total"] += 1
        tally["gh_present" if r["gh_present"] else "gh_absent"] += 1
        if r.get("force_pushed_away"):
            tally["bb_fba"] += 1
            if r["gh_present"]:
                tally["gh_present_and_bb_fba"] += 1
        if r["gh_present"] and r.get("gh_reachable") is False:
            tally["gh_present_and_gh_unreachable"] += 1

    log("=== summary ===")
    log(json.dumps(tally))
    log(f"GH API calls: {gh.calls} — everything else resolved from the local "
        f"mirror (reachable-and-present, no API needed)")
    fba = [r for r in all_rows if r.get("force_pushed_away")]
    log(f"force-pushed-away source tips: {len(fba)}")
    for r in fba[:40]:
        log(f"  PR{r['prs']} {r['sha'][:12]} present_on_gh={r['gh_present']} "
            f"gh_reachable={r['gh_reachable']} orphaned={r['orphaned_anchor']} "
            f"{r['text']!r}")
    orphan = [r for r in all_rows if r.get("orphaned_anchor")]
    log(f"orphaned-anchor SHAs: {len(orphan)}; "
        f"of those present on GH: {sum(1 for r in orphan if r['gh_present'])}")
    src = [r for r in all_rows if r.get("bb_reachable") is False and r["kinds"]
           and "comment_anchor" in r["kinds"]]
    log(f"comment-anchor SHAs not reachable from their PR head: {len(src)}; "
        f"present on GH: {sum(1 for r in src if r['gh_present'])}")
    if args.out:
        log(f"wrote {len(all_rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
