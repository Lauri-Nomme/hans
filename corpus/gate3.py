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
  - Optional persistent cache (`--cache file`): every GH API response is saved
    to a JSON file and served from disk on re-runs (no network for cached URLs),
    cutting a 3-hour validation cycle to seconds. `--refresh` revalidates via
    ETag instead. Only the REST calls (PR list + deep) are cached — the git
    layer uses `git`/ls-remote directly and always re-checks.
  - Progress: line every `--progress-every` PRs with counts + rate-limit budget.
  - Dependencies: stdlib only (urllib + subprocess git).

Usage:
  python3 corpus/gate3.py --scrape ./scrape/FIX-golden --org ORG --repo REPO \
      --pat ghp_... [--no-git-objects] [--deep] [--state gate3-state.json] \
      [--cache gh-cache.json] [--refresh]
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


def log(msg):
    """Print with an ISO-8601 UTC timestamp prefix for correlation."""
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def norm_comment(text):
    """Normalize a comment body so BB and GH markdown renderings compare equal:
    strip markdown quote markers (`> ` lines), leading/trailing space, collapse
    internal whitespace."""
    if text is None:
        return ""
    lines = []
    for ln in str(text).splitlines():
        ln = ln.lstrip()
        while ln.startswith(">"):
            ln = ln[1:].lstrip()
        lines.append(ln)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


class RateLimitClient:
    """urllib wrapper: rate-limit aware, retry with backoff, persistent cache, paginate.

    The cache persists across runs (one JSON file mapping request-URL -> body+etag),
    so a repeat gate3 run iterates on data it already fetched instead of re-querying
    GitHub. By default a cached URL is served from disk without a network call
    (post-migration validation is against a static GH state); pass refresh=True to
    request revalidation via ETag."""

    def __init__(self, token, cache_path=None, quiet=False, budget_headroom=10,
                 refresh=False):
        self.token = token
        self.quiet = quiet
        self.budget_headroom = budget_headroom
        self.refresh = refresh
        self.requests = 0
        self.limit = None
        self.remaining = None
        self.reset = None
        self._cache_path = cache_path
        self._disk = {}
        if cache_path and os.path.exists(cache_path):
            try:
                self._disk = json.loads(open(cache_path, encoding="utf-8").read())
            except Exception:
                self._disk = {}

    def _save(self):
        if self._cache_path:
            try:
                tmp = self._cache_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._disk, f)
                os.replace(tmp, self._cache_path)
            except Exception as e:
                log(f"cache write failed: {e}")

    def cached_body(self, url):
        """Body from an earlier run for this exact URL, or None."""
        ent = self._disk.get(url)
        return ent.get("body") if ent else None

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
            log(f"rate limit low ({self.remaining}/{self.limit}) — "
                f"sleeping {wait}s until reset")
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
        # 1) unconditional local replay: serve cached body, no network
        if use_cache and not self.refresh:
            cached = self.cached_body(url)
            if cached is not None:
                return json.loads(cached) if cached else None
        etag = None
        if use_cache:
            ent = self._disk.get(url)
            etag = ent.get("etag") if ent else None
        for attempt in range(retries + 1):
            self._wait_for_budget()
            try:
                req = urllib.request.Request(url, headers=self._headers(etag))
                with urllib.request.urlopen(req, timeout=120) as r:
                    self._update_limits(r.headers)
                    self.requests += 1
                    body = r.read().decode()
                    if use_cache:
                        self._disk[url] = {"body": body,
                                           "etag": r.headers.get("ETag")}
                        self._save()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as e:
                self._update_limits(e.headers or {})
                self.requests += 1
                if e.code == 304 and url in self._disk:   # not modified
                    return json.loads(self._disk[url]["body"])
                if e.code == 403:                          # rate limited / blocked
                    body = e.read().decode()[:200]
                    if "rate limit" in body.lower():
                        reset = int(e.headers.get("X-RateLimit-Reset", 0) or 0)
                        wait = max(reset - int(time.time()) + 2, 30)
                        log(f"403 rate-limited — sleeping {wait}s")
                        time.sleep(wait)
                        continue
                    raise
                if e.code in (429, 500, 502, 503, 504):
                    wait = min(2 ** attempt * 2, 60)
                    log(f"HTTP {e.code} — retry in {wait}s")
                    time.sleep(wait)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                wait = min(2 ** attempt * 2, 60)
                log(f"network error — retry in {wait}s")
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
    """Drop non-content refs: remote-tracking, BB PR refs, peeled tag refs,
    and the scraper's mirror-internal retention refs (refs/stash-refs/* keep
    hidden PR source-tips reachable for repack; refs/keep/* retain objects
    behind unwritable refnames). None of these are content refs and GH will
    never carry them."""
    out = {}
    for ref, sha in mapping.items():
        if ref.startswith("refs/remotes/"):
            continue
        if ref.startswith("refs/pull-requests/"):
            continue
        if ref.startswith("refs/stash-refs/"):
            continue
        if ref.startswith("refs/keep/"):
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

    def case_collided(rname):
        """A ref missing on the other side is a benign case-collision if the
        SAME name in a different casing exists there pointing at the same SHA
        (e.g. mara/WIP/.. vs mara/wip/.. — GitHub collapses case-insensitive
        duplicate names; nothing is lost)."""
        other = remote if rname in only_local else local
        here = local if rname in only_local else remote
        for o in other:
            if o.lower() == rname.lower() and o != rname and other[o] == here[rname]:
                return True
        return False

    col_local = {r for r in only_local if case_collided(r)}
    col_remote = {r for r in only_remote if case_collided(r)}
    real_local = {r: only_local[r] for r in only_local if r not in col_local}
    real_remote = {r: only_remote[r] for r in only_remote if r not in col_remote}

    if real_local:
        report["genuine"].append(f"refs only in BB: {sorted(real_local)}")
    if real_remote:
        report["genuine"].append(f"refs only in GH: {sorted(real_remote)}")
    if col_local:
        report["notes"].append(
            f"git refs: {sorted(col_local)} only in BB — case-variant duplicate "
            f"of a GH ref pointing at the same SHA (GitHub collapses "
            f"case-insensitive names); benign, no content loss")
    if col_remote:
        report["notes"].append(
            f"git refs: {sorted(col_remote)} only in GH — case-variant duplicate "
            f"of a BB ref (benign)")
    if sha_diff:
        report["genuine"].append(f"refs differ: { {k: v for k, v in list(sha_diff.items())[:5]} }")
    report["notes"].append(f"git refs: {len(local)} local, {len(remote)} remote, "
                           f"{len(sha_diff)} sha-diffs")
    return not (real_local or real_remote or sha_diff)


def verify_git_objects(scrape_git, org_repo, client, report):
    """Object-wise compare: fetch GH objects into a temp bare repo, then compare
    `git cat-file --batch-all-objects` output (sha -> type + content hash).

    only-BB objects are classified by reachability in the BB mirror:
      - reachable from a branch/tag ref  -> SHOULD be on GH; genuine if absent
      - reachable only via retention refs (refs/keep/*, refs/stash-refs/*,
        refs/pull-requests/*) -> GEI deliberately prunes these (intermediate PR
        commits, orphaned anchors); advisory, expected
      - not reachable from any ref       -> dangling in the odb; genuine
    Classification only runs when there are only-BB objects, so the common
    pass path pays only the one cat-file per side."""
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="gate3-obj-")
    try:
        log("git objects: fetching GH refs into temp bare repo (this is the "
            "long-est step; large repos take minutes to tens of minutes)")
        r = git(["init", "-q", "--bare", tmp])
        url = f"https://x-access-token:{client.token}@github.com/{org_repo}.git"
        # shallow-ish: fetch all refs once (objects will be compared by content)
        log("git objects: git fetch (no tags) — no progress sub-reporting; "
            "watch w/ `git -C %s count-objects -v` or top on your side" % tmp)
        r = subprocess.run(["git", "-C", tmp, "fetch", "--prune", "--no-tags",
                            url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            report["genuine"].append(f"git fetch for objects failed: {r.stderr[:200]}")
            return False
        log("git objects: fetch done; building object maps")

        def objmap(gd, label):
            log(f"git objects: cat-file --batch-all-objects ({label})")
            r = subprocess.run(["git", "-C", gd, "cat-file", "--batch-all-objects",
                                "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                               capture_output=True, text=True, timeout=3600)
            return {line.split()[0] for line in r.stdout.splitlines()}

        la = objmap(scrape_git, "BB")
        lb = objmap(tmp, "GH")
        log("git objects: comparing")
        only_a = la - lb
        only_b = lb - la
        same = la & lb
        log(f"git objects: BB={len(la)} GH={len(lb)} shared={len(same)} "
            f"only-BB={len(only_a)} only-GH={len(only_b)}")
        report["notes"].append(f"git objects: BB={len(la)} GH={len(lb)} "
                               f"shared={len(same)} only-BB={len(only_a)} only-GH={len(only_b)}")

        if only_b:
            report["genuine"].append(
                f"git object sets differ: only-GH {sorted(only_b)[:5]} (on GH, "
                f"not in archive — true loss)")
        should_be_gh = set()
        dangling = set()
        if only_a:
            # classify only-BB by reachability (only when needed)
            reach_migrated = _reachable_from(scrape_git, "refs/heads", "refs/tags")
            reach_all = _reachable_all(scrape_git)
            should_be_gh = only_a & reach_migrated
            dangling = only_a - reach_all
            expected_pruned = only_a - should_be_gh - dangling
            log(f"git objects: only-BB breakdown migrated-missing={len(should_be_gh)} "
                f"pruned-retained={len(expected_pruned)} dangling={len(dangling)}")
            report["notes"].append(
                f"git objects: only-BB = {len(only_a)} "
                f"(reachable-from-refs-should-be-on-GH {len(should_be_gh)}, "
                f"GEI-pruned-intermediate {len(expected_pruned)}, dangling {len(dangling)})")
            if should_be_gh:
                report["genuine"].append(
                    f"git object sets differ: {len(should_be_gh)} only-BB object(s) "
                    f"reachable from a branch/tag ref but missing on GH "
                    f"(sample {sorted(should_be_gh)[:5]})")
            if dangling:
                report["genuine"].append(
                    f"git object sets differ: {len(dangling)} only-BB object(s) "
                    f"not reachable from any ref (sample {sorted(dangling)[:5]})")
        good = not only_b and not should_be_gh and not dangling
        return good
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _reachable_tips(gitdir, *refspecs):
    """Objectnames of every object reachable from the given ref namespaces."""
    out = subprocess.run(
        ["git", "-C", gitdir, "for-each-ref", "--format=%(objectname)",
         *refspecs],
        capture_output=True, text=True)
    tips = out.stdout.split()
    if not tips:
        return set()
    r = subprocess.run(["git", "-C", gitdir, "rev-list", "--objects", "--stdin"],
                       input="\n".join(tips), capture_output=True, text=True)
    return {line.split()[0] for line in r.stdout.splitlines()}


def _reachable_from(gitdir, *refspecs):
    """Objectnames reachable from branch/tag refs (shallow-first optimization:
    rev-list streams in one pass, no per-ref subprocess)."""
    # Equivalent to `git rev-list --objects <tips>` via one rev-list --stdin.
    return _reachable_tips(gitdir, *refspecs)


def _reachable_all(gitdir):
    """Objectnames reachable from ALL refs (incl. refs/keep, stash-refs,
    pull-requests)."""
    return _reachable_tips(gitdir, "refs") if _has_refs(gitdir) else set()


def _has_refs(gitdir):
    return subprocess.run(["git", "-C", gitdir, "for-each-ref", "--count"],
                          capture_output=True, text=True).stdout.strip() != "0"


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
    log("PR list: fetching GH pulls (state=all)")
    gh_prs = list(client.paginate(f"/repos/{org_repo}/pulls", {"state": "all"}))
    log(f"PR list: fetched {len(gh_prs)} GH PRs (BB has {len(prs)})")
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


def bb_top_comments(rest, pid):
    """Top-level PR comments from the scrape activities (COMMENT:ADDED with a
    comment that has no anchor/path → pure PR comment). Returns list of
    normalized bodies in creation order."""
    p = rest / f"pr_{pid}_activities.json"
    if not p.exists():
        return []
    acts = json.loads(p.read_text())
    bodies = []
    for a in acts:
        if a.get("action") != "COMMENTED":
            continue
        c = a.get("comment") or {}
        if not c.get("text"):
            continue
        if (c.get("anchor") or {}).get("path"):
            continue            # inline/file-level, not a PR body comment
        bodies.append(norm_comment(c.get("text")))
    return bodies


def verify_pr_deep(prs, client, org_repo, report, state_path, limit_prs,
                   progress_every, rest):
    """Per-PR reviews + comment body compare. Resumable via state file.

    Findings are appended to `<state>.deep.jsonl` per PR (one JSON object per
    line), so a partial/crashed run keeps the mismatches it already found even
    though the final summary only prints at completion. On resume, prior
    records are loaded so the in-memory report stays complete."""
    done = set()
    if state_path and os.path.exists(state_path):
        try:
            done = set(json.loads(open(state_path).read()).get("deep_done", []))
        except Exception:
            pass
    deep_path = (state_path + ".deep.jsonl") if state_path else None
    deepf = None
    if deep_path and os.path.exists(deep_path):
        # seed in-memory report from prior partial runs so the summary is complete
        try:
            for line in open(deep_path, encoding="utf-8"):
                line = line.strip()
                if line:
                    report["deep"].append(json.loads(line))
        except Exception:
            pass
    todo = sorted(p["id"] for p in prs)[:limit_prs] if limit_prs else sorted(p["id"] for p in prs)
    todo = [n for n in todo if n not in done]
    total = len(todo)
    pr_by_id = {p["id"]: p for p in prs}

    def _norm_login(login):
        """Normalize EMU logins: strip __mannequin-style suffix, or a short
        underscore suffix (org/machine tag, e.g. '<user>_dtrnd'). The user is
        identified by the part before the tag; this matches how GEI maps EMU
        (Enterprise Managed User) logins back to their source identity."""
        if "__" in login:
            return login.split("__")[0]
        parts = login.rsplit("_", 1)
        if len(parts) == 2 and len(parts[1]) <= 6:
            return parts[0]
        return login

    _STATE_RANK = {"APPROVED": 3, "CHANGES_REQUESTED": 2,
                   "COMMENTED": 1, "DISMISSED": 0, "PENDING": 0}
    _BB_TO_GH = {"APPROVED": "APPROVED", "NEEDS_WORK": "CHANGES_REQUESTED"}

    if deepf is None and deep_path:
        deepf = open(deep_path, "a", encoding="utf-8")
    try:
        for i, n in enumerate(todo, 1):
            reviews = client.get(f"/repos/{org_repo}/pulls/{n}/reviews", {"per_page": 100}) or []
            comments = client.get(f"/repos/{org_repo}/issues/{n}/comments", {"per_page": 100}) or []
            # -- reviewer comparison --
            # BB: all assigned reviewers (with their review status)
            # GH: all users who submitted a review (any state)
            bb = pr_by_id.get(n, {})
            bb_reviewers_raw = bb.get("reviewers") or []
            def _u(rv):
                u = (rv or {}).get("user")
                return ((u or {}).get("slug") or (u or {}).get("name"))
            bb_rev = sorted(x for x in (_u(r) for r in bb_reviewers_raw) if x)
            bb_status = {_u(r): r.get("status", "UNKNOWN")
                         for r in bb_reviewers_raw if _u(r)}

            gh_rev_all = sorted({(r.get("user") or {}).get("login")
                                 for r in reviews
                                 if (r.get("user") or {}).get("login")} - {None})
            gh_rev_norm = sorted({_norm_login(x) for x in gh_rev_all})

            gh_best = {}
            for r in reviews:
                login = (r.get("user") or {}).get("login")
                if not login:
                    continue
                norm = _norm_login(login)
                st = r.get("state", "")
                if _STATE_RANK.get(st, 0) > _STATE_RANK.get(gh_best.get(norm, ""), 0):
                    gh_best[norm] = st

            bb_set = set(bb_rev)
            gh_set = set(gh_rev_norm)
            only_bb = sorted(bb_set - gh_set)
            only_gh = sorted(gh_set - bb_set)
            if only_bb or only_gh:
                parts = []
                if only_bb:
                    parts.append("only-BB: " + ", ".join(
                        f"{u}({bb_status.get(u, '?')})" for u in only_bb))
                if only_gh:
                    parts.append(f"only-GH: {only_gh}")
                report["notes"].append(f"PR {n}: reviewer set differs — {'; '.join(parts)}")

            for u in sorted(bb_set & gh_set):
                expected_gh = _BB_TO_GH.get(bb_status.get(u))
                if expected_gh and gh_best.get(u) != expected_gh:
                    report["notes"].append(
                        f"PR {n}: reviewer {u} status mismatch "
                        f"BB={bb_status[u]} GH={gh_best.get(u, 'NONE')}")

            # comment bodies: every BB top-level PR comment should appear (normalized)
            # in some GH issue comment (GH flattens threads + applies markdown).
            bb_bodies = bb_top_comments(rest, n)
            gh_bodies = [norm_comment(c.get("body")) for c in comments]
            missing = []
            for b in bb_bodies:
                if not any(norm_comment(b) in g or g in norm_comment(b) for g in gh_bodies):
                    missing.append(b[:120])
            if missing:
                report["notes"].append(f"PR {n}: {len(missing)} BB comment(s) not found on GH")
                for m in missing[:5]:
                    report["notes"].append(f"   missing: {m!r}")

            rec = {
                "pr": n,
                "bb_reviewers": bb_rev,
                "bb_reviewer_statuses": bb_status,
                "gh_reviewers": gh_rev_all,
                "gh_reviewer_best_state": gh_best,
                "bb_comment_count": len(bb_bodies),
                "gh_comment_count": len(comments),
                "gh_comment_missing": len(missing),
                "gh_review_count": len(reviews),
            }
            report["deep"].append(rec)
            if deepf:
                deepf.write(json.dumps(rec) + "\n")
                deepf.flush()
            done.add(n)
            if state_path:
                json.dump({"deep_done": sorted(done)},
                          open(state_path, "w"))
            if i % progress_every == 0 or i == total:
                pct = 100.0 * i / total if total else 100.0
                log(f"deep {i}/{total} ({pct:.0f}%) remaining={client.remaining}")
    finally:
        if deepf:
            deepf.close()


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
    ap.add_argument("--cache", default=None,
                    help="persist GH API responses to a JSON file and reuse them "
                         "on re-runs (fast iteration; no network for cached URLs). "
                         "Default: no persistent cache, live queries every run.")
    ap.add_argument("--refresh", action="store_true",
                    help="with --cache: revalidate cached URLs via ETag instead "
                         "of serving them from disk (slower, fresh data)")
    args = ap.parse_args()
    if not args.pat:
        log("--pat or GH_PAT required"); return 2

    index, prs, branches, tags, rest = load_scrape(args.scrape)
    report = {"notes": [], "genuine": [], "deep": []}
    client = RateLimitClient(args.pat, cache_path=args.cache, refresh=args.refresh)
    if args.cache:
        log(f"HTTP cache: {args.cache} (reuse={not args.refresh})")
    org_repo = f"{args.org}/{args.repo}"
    log(f"BB: {index['project']}/{index['repo']} ({len(prs)} PRs, "
        f"{len(branches)} branches, {len(tags)} tags)")
    log(f"GH: {org_repo}")

    # --- git layer -------------------------------------------------------
    scrape_git = Path(args.scrape) / "git"
    log("phase: git refs")
    verify_git_refs(scrape_git, org_repo, client, report)
    if not args.no_git_objects:
        log("phase: git objects")
        verify_git_objects(scrape_git, org_repo, client, report)
    else:
        log("skipping git-objects compare (--no-git-objects)")

    # --- PR layer --------------------------------------------------------
    log("phase: PR list")
    verify_pr_list(prs, client, org_repo, report)
    if args.deep:
        log("phase: deep per-PR (reviews/comments)")
        verify_pr_deep(prs, client, org_repo, report, args.state,
                       args.limit_prs, args.progress_every, rest)
    else:
        log("skipping deep per-PR (no --deep)")

    # --- summary ----------------------------------------------------------
    log("=== GATE 3 ===")
    for n in report["notes"]:
        log("  [note] " + n)
    for d in report["deep"][:50]:
        log("  [deep] " + json.dumps(d))
    if len(report["deep"]) > 50:
        log(f"  ... {len(report['deep'])} deep records (first 50 shown; "
            f"state saved for resume)")
    if report["genuine"]:
        log(f"  GENUINE ({len(report['genuine'])}):")
        for g in report["genuine"][:50]:
            log("   " + g)
        if len(report["genuine"]) > 50:
            log(f"   ... and {len(report['genuine'])-50} more")
        log("GATE 3: FAIL")
        return 1
    log("GATE 3: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())