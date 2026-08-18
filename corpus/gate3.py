#!/usr/bin/env python3
"""Gate 3 — verify a GitHub Enterprise Importer migration against the BB scrape.

Validates the migrated GitHub repo against the local `bb-archiver scrape` output
of the SAME Bitbucket repo, at production scale (10k+ PRs, 100k+ commits):

  git layer  — compares GH refs (branches/tags + SHAs) against the scrape's
               git mirror (`<scrape>/git`) via `git for-each-ref`; optionally
               object-wise compare via `git cat-file --batch-all-objects`.
  PR layer   — paginated list compare (state via `merged_at`, title, head/base).
  deep layer — optional per-PR reviews/comments, rate-limit aware + resumable.

Design for scale:
  - RateLimitClient: honors X-RateLimit-Remaining/Reset (sleeps), retries 429/5xx
    with backoff, reads `Link:` pagination, optional ETag 304 caching.
  - State checkpoint (`--state file`): completed PR ids persisted so a rerun
    resumes instead of restarting; safe to Ctrl-C / run across rate-limit windows.
  - Progress: line every `--progress-every` PRs with counts + rate-limit budget.
  - Dependencies: stdlib only (urllib + subprocess git).

Usage:
  python3 corpus/gate3.py --scrape ./scrape/FIX-golden --org ORG --repo REPO \
      --pat ghp_... [--no-git-objects] [--deep] [--state gate3-state.json]
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


class RateLimitClient:
    """urllib wrapper: rate-limit aware, retry with backoff, ETag cache, paginate."""

    def __init__(self, token, cache=None, quiet=False, budget_headroom=10):
        self.token = token
        self.cache = cache or {}          # path -> (etag, body)
        self.quiet = quiet
        self.budget_headroom = budget_headroom
        self.requests = 0
        self.limit = None
        self.remaining = None
        self.reset = None

    def _headers(self, etag=None):
        h = {"Authorization": f"Bearer {self.token}",
             "User-Agent": "hans-gate3",
             "Accept": "application/vnd.github+json"}
        if etag:
            h["If-None-Match"] = etag
        return h

    def _wait_for_budget(self):
        """If core rate limit is nearly exhausted, sleep until reset."""
        if self.remaining is None:
            return
        if self.remaining > self.budget_headroom:
            return
        now = int(time.time())
        wait = (self.reset or now) - now + 5
        if wait > 0:
            print(f"[gate3] rate limit low ({self.remaining}/{self.limit}) — "
                  f"sleeping {wait}s until reset", flush=True)
            time.sleep(wait)

    def _update_limits(self, headers):
        rl = headers.get("X-RateLimit-Remaining")
        self.limit = int(headers.get("X-RateLimit-Limit", 0) or 0)
        if rl is not None:
            self.remaining = int(rl)
        rs = headers.get("X-RateLimit-Reset")
        if rs:
            self.reset = int(rs)

    def get(self, path, params=None, use_cache=True, retries=4):
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        etag = None
        if use_cache and path in self.cache:
            etag = self.cache[path][0]
        for attempt in range(retries + 1):
            self._wait_for_budget()
            try:
                req = urllib.request.Request(url, headers=self._headers(etag))
                with urllib.request.urlopen(req, timeout=120) as r:
                    self._update_limits(r.headers)
                    self.requests += 1
                    body = r.read().decode()
                    if use_cache:
                        self.cache[path] = (r.headers.get("ETag"), body)
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as e:
                self._update_limits(e.headers or {})
                self.requests += 1
                if e.code == 304 and path in self.cache:   # not modified
                    return json.loads(self.cache[path][1])
                if e.code == 403:                          # rate limited / blocked
                    body = e.read().decode()[:200]
                    if "rate limit" in body.lower():
                        reset = int(e.headers.get("X-RateLimit-Reset", 0) or 0)
                        wait = max(reset - int(time.time()) + 2, 30)
                        print(f"[gate3] 403 rate-limited — sleeping {wait}s", flush=True)
                        time.sleep(wait)
                        continue
                    raise
                if e.code in (429, 500, 502, 503, 504):
                    wait = min(2 ** attempt * 2, 60)
                    print(f"[gate3] HTTP {e.code} — retry in {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                wait = min(2 ** attempt * 2, 60)
                print(f"[gate3] network error — retry in {wait}s", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"gave up after {retries} retries: {url}")

    def paginate(self, path, params=None, per_page=100):
        """Yield items from a paginated collection endpoint."""
        params = dict(params or {})
        params["per_page"] = per_page
        page = 1
        while True:
            items = self.get(path, {**params, "page": page})
            if not items:
                break
            for it in items:
                yield it
            if len(items) < per_page:
                break
            page += 1
        return


# --------------------------------------------------------------------------
def git(args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def refs_of(gitdir):
    """{refname: sha} for a bare repo via for-each-ref."""
    r = git(["-C", gitdir, "for-each-ref",
             "--format=%(refname) %(objectname)"])
    if r.returncode != 0:
        raise RuntimeError(f"git for-each-ref failed: {r.stderr[:200]}")
    return dict(line.split(" ", 1) for line in r.stdout.splitlines())


def _norm_refs(mapping):
    """Drop non-content refs: remote-tracking, BB PR refs, peeled tag refs."""
    out = {}
    for ref, sha in mapping.items():
        if ref.startswith("refs/remotes/"):
            continue
        if ref.startswith("refs/pull-requests/"):
            continue
        if ref.endswith("^{}"):
            continue
        out[ref] = sha
    return out


def verify_git_refs(scrape_git, org_repo, client, report):
    """Compare scrape-mirror refs vs GH repo refs (cheap, no clone)."""
    local = _norm_refs(refs_of(scrape_git))
    # GH: refs/heads/* + refs/tags/* via the git protocol ls-remote (auth by token)
    remote = {}
    out = subprocess.run(
        ["git", "ls-remote", f"https://x-access-token:{client.token}@github.com/{org_repo}.git",
         "refs/heads/*", "refs/tags/*"],
        capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        report["genuine"].append(f"git ls-remote failed: {out.stderr[:200]}")
        return False
    for line in out.stdout.splitlines():
        sha, ref = line.split("\t", 1)
        if ref.endswith("^{}"):          # peeled annotated-tag ref
            continue
        remote[ref] = sha

    only_local = {r: local[r] for r in set(local) - set(remote)}
    only_remote = {r: remote[r] for r in set(remote) - set(local)}
    sha_diff = {r: (local[r], remote[r]) for r in set(local) & set(remote)
                if local[r] != remote[r]}
    if only_local:
        report["genuine"].append(f"refs only in BB: {sorted(only_local)}")
    if only_remote:
        report["genuine"].append(f"refs only in GH: {sorted(only_remote)}")
    if sha_diff:
        report["genuine"].append(f"refs differ: { {k: v for k, v in list(sha_diff.items())[:5]} }")
    report["notes"].append(f"git refs: {len(local)} local, {len(remote)} remote, "
                           f"{len(sha_diff)} sha-diffs")
    return not (only_local or only_remote or sha_diff)


def verify_git_objects(scrape_git, org_repo, client, report):
    """Object-wise compare: fetch GH objects into a temp bare repo, then compare
    `git cat-file --batch-all-objects` output (sha -> type + content hash)."""
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="gate3-obj-")
    try:
        r = git(["init", "-q", "--bare", tmp])
        url = f"https://x-access-token:{client.token}@github.com/{org_repo}.git"
        # shallow-ish: fetch all refs once (objects will be compared by content)
        r = subprocess.run(["git", "-C", tmp, "fetch", "--prune", "--no-tags",
                            url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            report["genuine"].append(f"git fetch for objects failed: {r.stderr[:200]}")
            return False

        def objmap(gd):
            r = subprocess.run(["git", "-C", gd, "cat-file", "--batch-all-objects",
                                "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                               capture_output=True, text=True, timeout=3600)
            m = {}
            for line in r.stdout.splitlines():
                sha, typ, size = line.split(" ")
                m[sha] = typ
            return m

        la = objmap(scrape_git)
        lb = objmap(tmp)
        only_a = set(la) - set(lb)
        only_b = set(lb) - set(la)
        same = set(la) & set(lb)
        report["notes"].append(f"git objects: BB={len(la)} GH={len(lb)} "
                               f"shared={len(same)} only-BB={len(only_a)} only-GH={len(only_b)}")
        if only_a or only_b:
            report["genuine"].append(
                f"git object sets differ: only-BB {sorted(only_a)[:5]} "
                f"only-GH {sorted(only_b)[:5]}")
            return False
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
def load_scrape(scrape_dir):
    index = json.loads((Path(scrape_dir) / "index.json").read_text())
    proj, repo = index["project"], index["repo"]
    rest = Path(scrape_dir) / "rest"
    prs = json.loads((rest / f"pull-requests_{proj}_{repo}.json").read_text())
    branches = json.loads((rest / f"branches_{proj}_{repo}.json").read_text())
    tags = json.loads((rest / f"tags_{proj}_{repo}.json").read_text())
    return index, prs, branches, tags, rest


def bb_state(pr):
    return pr["state"]  # OPEN / MERGED / DECLINED


def gh_state(pr):
    if pr["state"] == "open":
        return "OPEN"
    return "MERGED" if pr.get("merged_at") else "DECLINED"


def verify_pr_list(prs, client, org_repo, report):
    gh_prs = list(client.paginate(f"/repos/{org_repo}/pulls", {"state": "all"}))
    report["notes"].append(f"PR counts: BB={len(prs)} GH={len(gh_prs)}")
    if len(prs) != len(gh_prs):
        report["genuine"].append(f"PR count mismatch: BB={len(prs)} GH={len(gh_prs)}")

    bb = {p["id"]: p for p in prs}
    gh = {p["number"]: p for p in gh_prs}
    for n in sorted(set(bb) | set(gh)):
        if n not in bb:
            report["genuine"].append(f"PR {n}: only on GH")
            continue
        if n not in gh:
            report["genuine"].append(f"PR {n}: missing on GH")
            continue
        b, g = bb[n], gh[n]
        if bb_state(b) != gh_state(g):
            report["genuine"].append(f"PR {n}: state BB={bb_state(b)} GH={gh_state(g)}")
        if (b.get("title") or "") != (g.get("title") or ""):
            report["genuine"].append(
                f"PR {n}: title BB={b.get('title')!r} GH={g.get('title')!r}")
        bh = (b.get("fromRef") or {}).get("displayId")
        gh_head = (g.get("head") or {}).get("ref")
        if bh != gh_head:
            report["genuine"].append(f"PR {n}: head BB={bh} GH={gh_head}")
    report["notes"].append("PR list compare done")


def verify_pr_deep(prs, client, org_repo, report, state_path, limit_prs, progress_every):
    """Per-PR reviews + comment count. Resumable via state file."""
    done = set()
    if state_path and os.path.exists(state_path):
        try:
            done = set(json.loads(open(state_path).read()).get("deep_done", []))
        except Exception:
            pass
    todo = sorted(p["id"] for p in prs)[:limit_prs] if limit_prs else sorted(p["id"] for p in prs)
    todo = [n for n in todo if n not in done]
    total = len(todo)
    for i, n in enumerate(todo, 1):
        reviews = client.get(f"/repos/{org_repo}/pulls/{n}/reviews", {"per_page": 100}) or []
        comments = client.get(f"/repos/{org_repo}/issues/{n}/comments", {"per_page": 100}) or []
        # reviewers expected from BB scrape
        bb = next((p for p in prs if p["id"] == n), {})
        bb_rev = sorted(r.get("user", {}).get("slug") or r.get("user", {}).get("name")
                        for r in (bb.get("reviewers") or []))
        gh_rev = sorted({r.get("user", {}).get("login") for r in reviews
                         if r.get("state") in ("APPROVED", "CHANGES_REQUESTED")})
        # Reviewers on GH are mapped by email; mannequins adopt hashed logins,
        # so mismatches are advisory, not hard failures. Compare as sets.
        bb_set = set(bb_rev)
        gh_set = {x.split("__")[0].split("_")[0] for x in gh_rev}  # crude normalize
        if bb_set and gh_set and bb_set != gh_set:
            report["notes"].append(
                f"PR {n}: reviewer set differs BB={sorted(bb_set)} GH={sorted(gh_set)} "
                f"(mannequin mapping is expected to differ)")
        report["deep"].append({
            "pr": n,
            "bb_reviewers": bb_rev,
            "gh_reviewers": gh_rev,
            "gh_comment_count": len(comments),
            "gh_review_count": len(reviews),
        })
        done.add(n)
        if state_path:
            json.dump({"deep_done": sorted(done)},
                      open(state_path, "w"))
        if i % progress_every == 0 or i == total:
            now = time.strftime("%H:%M:%S")
            pct = 100.0 * i / total if total else 100.0
            print(f"[gate3] deep {i}/{total} ({pct:.0f}%) [{now}] "
                  f"remaining={client.remaining}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scrape", required=True)
    ap.add_argument("--org", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pat", default=os.environ.get("GH_PAT", ""))
    ap.add_argument("--state", default="gate3-state.json")
    ap.add_argument("--deep", action="store_true",
                    help="per-PR reviews/comments (rate-limited, resumable)")
    ap.add_argument("--no-git-objects", action="store_true",
                    help="skip object-wise git compare (refs only)")
    ap.add_argument("--limit-prs", type=int, default=0, help="cap PRs checked (test)")
    ap.add_argument("--progress-every", type=int, default=100)
    args = ap.parse_args()
    if not args.pat:
        print("--pat or GH_PAT required"); return 2

    index, prs, branches, tags, rest = load_scrape(args.scrape)
    report = {"notes": [], "genuine": [], "deep": []}
    client = RateLimitClient(args.pat)
    org_repo = f"{args.org}/{args.repo}"
    print(f"[gate3] BB: {index['project']}/{index['repo']} ({len(prs)} PRs, "
          f"{len(branches)} branches, {len(tags)} tags)")
    print(f"[gate3] GH: {org_repo}")

    # --- git layer -------------------------------------------------------
    scrape_git = Path(args.scrape) / "git"
    verify_git_refs(scrape_git, org_repo, client, report)
    if not args.no_git_objects:
        verify_git_objects(scrape_git, org_repo, client, report)

    # --- PR layer --------------------------------------------------------
    verify_pr_list(prs, client, org_repo, report)
    if args.deep:
        verify_pr_deep(prs, client, org_repo, report, args.state,
                       args.limit_prs, args.progress_every)

    # --- summary ----------------------------------------------------------
    print("\n=== GATE 3 ===")
    for n in report["notes"]:
        print("  [note]", n)
    for d in report["deep"][:50]:
        print("  [deep]", json.dumps(d))
    if len(report["deep"]) > 50:
        print(f"  ... {len(report['deep'])} deep records (first 50 shown; "
              f"state saved for resume)")
    if report["genuine"]:
        print(f"  GENUINE ({len(report['genuine'])}):")
        for g in report["genuine"][:50]:
            print("   ", g)
        if len(report["genuine"]) > 50:
            print(f"   ... and {len(report['genuine'])-50} more")
        print("GATE 3: FAIL")
        return 1
    print("GATE 3: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())