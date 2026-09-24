#!/usr/bin/env python3
"""Gate 3 — verify a GitHub Enterprise Importer migration against the BB scrape.

Validates the migrated GitHub repo against the local `bb-archiver scrape` output
of the SAME Bitbucket repo, at production scale (10k+ PRs, 100k+ commits):

  git layer  — compares GH refs (branches/tags + SHAs) against the scrape's
               git mirror (`<scrape>/git`) via `git for-each-ref`; optionally
               object-wise compare via `git cat-file --batch-all-objects`.
  PR layer   — paginated list compare (state via `merged_at`, title, head/base).
  deep layer — optional per-PR reviews + comments (rate-limit aware + resumable):
               reviewer sets/statuses, top-level comment bodies, and inline
               (file-anchored) review threads. GH inline comments and issue
               comments are fetched ONCE repo-wide (/repos/{org}/{repo}/
               pulls/comments and /issues/comments, ~total/100 requests each)
               and bucketed by PR, instead of one request per PR; only
               /pulls/{n}/reviews stays per-PR. Missing threads are classified
               orphaned (expected GEI pruning) vs NON-orphaned (real loss,
               gate-failing under --strict-inline).

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
      [--cache gh-cache.json] [--refresh] [--strict-inline]
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


def _replay_findings(rec, strict_inline, report):
    """Re-derive a persisted deep record's summary notes (and strict-inline
    genuine findings) into `report`, so a RESUMED run's PASS/FAIL decision and
    note list include PRs that finished in an earlier session. Without this,
    only counts survive a cancel (deep.jsonl) while notes/genuine — computed
    in-memory per PR — are lost, and the gate could wrongly report PASS.

    Mirrors the live note generation in verify_pr_deep; keep the two in sync."""
    n = rec.get("pr")
    bb_set = set(rec.get("bb_reviewers") or [])
    gh_norm = {_norm_login(x) for x in (rec.get("gh_reviewers") or [])}
    bb_status = rec.get("bb_reviewer_statuses") or {}
    gh_best = rec.get("gh_reviewer_best_state") or {}
    only_bb = sorted(bb_set - gh_norm)
    only_gh = sorted(gh_norm - bb_set)
    if only_bb or only_gh:
        parts = []
        if only_bb:
            parts.append("only-BB: " + ", ".join(
                f"{u}({bb_status.get(u, '?')})" for u in only_bb))
        if only_gh:
            parts.append(f"only-GH: {only_gh}")
        report["notes"].append(f"PR {n}: reviewer set differs — {'; '.join(parts)}")
    for u in sorted(bb_set & gh_norm):
        expected_gh = _BB_TO_GH.get(bb_status.get(u))
        if expected_gh and gh_best.get(u) != expected_gh:
            report["notes"].append(
                f"PR {n}: reviewer {u} status mismatch "
                f"BB={bb_status[u]} GH={gh_best.get(u, 'NONE')}")
    if rec.get("gh_comment_missing"):
        report["notes"].append(
            f"PR {n}: {rec['gh_comment_missing']} BB comment(s) not found on GH")
    if rec.get("bb_inline_missing"):
        report["notes"].append(
            f"PR {n}: {rec['bb_inline_missing']}/{rec.get('bb_inline_comments', 0)} "
            f"inline comment(s) missing on GH "
            f"({rec.get('bb_inline_missing_orphaned', 0)} under orphaned roots — "
            f"expected; {rec.get('bb_inline_missing_nonorphaned', 0)} NON-orphaned)")
    if strict_inline and rec.get("bb_inline_missing_nonorphaned"):
        report["genuine"].append(
            f"PR {n}: {rec['bb_inline_missing_nonorphaned']} NON-orphaned inline "
            f"comment(s) missing on GH (strict-inline)")


class RateLimitClient:
    """urllib wrapper: rate-limit aware, retry with backoff, persistent cache, paginate.

    The cache persists across runs as an append-only JSONL file (one line per
    request: {"url","etag","body"}), so each fetched response costs an O(1)
    append instead of rewriting the whole cache. On load every line is read
    into memory (last wins); a legacy single-object JSON cache is migrated to
    JSONL in place automatically. By default a cached URL is served from disk
    without a network call (post-migration validation is against a static GH
    state); pass refresh=True to request revalidation via ETag."""

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
        self._disk = {}          # url -> {"etag":..., "body":...}
        self._load_cache()

    def _load_cache(self):
        if not self._cache_path or not os.path.exists(self._cache_path):
            return
        try:
            raw = open(self._cache_path, encoding="utf-8").read()
        except Exception as e:
            log(f"cache read failed: {e}")
            return
        # Legacy format: the whole file is one JSON object {url: {etag, body}}.
        legacy = None
        try:
            obj = json.loads(raw)
            if (isinstance(obj, dict) and obj
                    and all(isinstance(v, dict) and "body" in v
                            for v in obj.values())):
                legacy = obj
        except Exception:
            pass
        if legacy is not None:
            for url, ent in legacy.items():
                self._disk[url] = {"etag": ent.get("etag"), "body": ent.get("body")}
            log(f"cache: migrating legacy JSON -> JSONL ({len(self._disk)} entries)")
            self._rewrite_jsonl()
            return
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue            # skip a torn trailing line from a crash
            if isinstance(rec, dict) and "url" in rec and "body" in rec:
                self._disk[rec["url"]] = {"etag": rec.get("etag"),
                                          "body": rec["body"]}

    def _rewrite_jsonl(self):
        """One-time rewrite of the whole cache as JSONL (legacy migration)."""
        if not self._cache_path:
            return
        tmp = self._cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for url, ent in self._disk.items():
                f.write(json.dumps({"url": url, "etag": ent.get("etag"),
                                    "body": ent.get("body")}) + "\n")
        os.replace(tmp, self._cache_path)

    def _save(self, url, etag, body):
        """Append one cache entry (O(1)); no full-file rewrite."""
        if not self._cache_path:
            return
        try:
            with open(self._cache_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"url": url, "etag": etag, "body": body}) + "\n")
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
                        etag_new = r.headers.get("ETag")
                        self._disk[url] = {"body": body, "etag": etag_new}
                        self._save(url, etag_new, body)
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


def load_activities(rest, pid):
    """Parsed PR activities from the scrape, or [] if absent/unreadable."""
    p = rest / f"pr_{pid}_activities.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def bb_top_comments(acts):
    """Top-level PR comments from the scrape activities (COMMENTED with a
    comment that has no anchor/path → pure PR comment). Returns list of
    normalized bodies in creation order."""
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


def bb_inline_comments(acts):
    """All inline (file-anchored) review comments on a PR from the scrape
    activities, flattened root-first.

    A comment is inline when its record carries an `anchor` with a `path`
    (fromHash/toHash + path + line). Replies are nested under the thread root
    and inherit its anchor and `orphaned` flag (in the BB API only the root
    carries the anchor). Returns a list of dicts:
      {id, text(norm), path, line, orphaned, author, createdDate, depth, root}
    """
    out = []

    def walk(c, depth, orphaned):
        a = c.get("anchor") or {}
        root = depth == 0
        if root and not a.get("path"):
            return                    # top-level PR comment, not inline
        if root:
            orphaned = bool(a.get("orphaned"))
        out.append({
            "id": c.get("id"),
            "text": norm_comment(c.get("text")),
            "path": a.get("path"),
            "line": a.get("line"),
            "orphaned": orphaned,
            "author": (c.get("author") or {}).get("slug"),
            "createdDate": c.get("createdDate"),
            "depth": depth,
            "root": root,
        })
        for r in c.get("comments") or []:
            walk(r, depth + 1, orphaned)

    for a in acts:
        if a.get("action") != "COMMENTED":
            continue
        walk(a.get("comment") or {}, 0, False)
    return out


def load_gh_review_comments(client, org_repo):
    """All inline review comments in the repo, grouped by PR number.

    One paginated pass over /repos/{org}/{repo}/pulls/comments (~total/100
    requests) instead of one request per PR. Each review comment carries
    `pull_request_url` (.../pulls/<n>), which we parse to bucket it. Returns
    {pr_number: [comment, ...]} (PRs with no inline comments are simply
    absent)."""
    by_pr = {}
    n = 0
    for c in client.paginate(f"/repos/{org_repo}/pulls/comments"):
        n += 1
        url = c.get("pull_request_url") or ""
        try:
            pr = int(url.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, AttributeError):
            continue
        by_pr.setdefault(pr, []).append(c)
        if n % 5000 == 0:
            log(f"review comments: {n} fetched ({len(by_pr)} PRs so far)")
    log(f"review comments: {n} across {len(by_pr)} PRs (repo-wide)")
    return by_pr


def load_gh_issue_comments(client, org_repo):
    """All issue comments in the repo, grouped by issue/PR number.

    One paginated pass over /repos/{org}/{repo}/issues/comments (~total/100
    requests) instead of /issues/{n}/comments per PR. Each comment carries
    `issue_url` (.../issues/<n>). Returns {number: [comment, ...]} (this
    includes plain issues; only PR numbers are looked up)."""
    by_n = {}
    n = 0
    for c in client.paginate(f"/repos/{org_repo}/issues/comments"):
        n += 1
        url = c.get("issue_url") or ""
        try:
            num = int(url.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, AttributeError):
            continue
        by_n.setdefault(num, []).append(c)
        if n % 5000 == 0:
            log(f"issue comments: {n} fetched ({len(by_n)} issues so far)")
    log(f"issue comments: {n} across {len(by_n)} issues (repo-wide)")
    return by_n


def verify_pr_deep(prs, client, org_repo, report, state_path, limit_prs,
                   progress_every, rest, strict_inline=False, gh_inline_by_pr=None,
                   gh_issue_by_pr=None):
    """Per-PR reviews + comment body compare. Resumable via state file.

    Findings are appended to `<state>.deep.jsonl` per PR (one JSON object per
    line), so a partial/crashed run keeps the mismatches it already found even
    though the final summary only prints at completion. On resume, prior
    records are loaded AND their notes/genuine findings re-derived
    (_replay_findings), so a cancel/restart preserves the PASS/FAIL decision —
    not just the counts."""
    DEEP_SCHEMA = 5   # bump when the per-PR record shape changes
    done = set()
    if state_path and os.path.exists(state_path):
        try:
            st = json.loads(open(state_path).read())
            if st.get("schema") == DEEP_SCHEMA:
                done = set(st.get("deep_done", []))
        except Exception:
            pass
    deep_path = (state_path + ".deep.jsonl") if state_path else None
    deepf = None
    if deep_path and os.path.exists(deep_path):
        # seed in-memory report from prior partial runs so the summary is
        # complete — but only records matching the current schema (older deep
        # runs lack the inline fields and would skew the tally / double-count).
        try:
            for line in open(deep_path, encoding="utf-8"):
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    if rec.get("schema") == DEEP_SCHEMA:
                        report["deep"].append(rec)
                        # restore notes/genuine for PRs finished in an earlier
                        # session, so the resumed run's PASS/FAIL is complete
                        _replay_findings(rec, strict_inline, report)
        except Exception:
            pass
    todo = sorted(p["id"] for p in prs)[:limit_prs] if limit_prs else sorted(p["id"] for p in prs)
    todo = [n for n in todo if n not in done]
    total = len(todo)
    pr_by_id = {p["id"]: p for p in prs}

    if deepf is None and deep_path:
        deepf = open(deep_path, "a", encoding="utf-8")
    t0 = time.time()
    last_t, last_i = t0, 0
    try:
        for i, n in enumerate(todo, 1):
            reviews = client.get(f"/repos/{org_repo}/pulls/{n}/reviews", {"per_page": 100}) or []
            if gh_issue_by_pr is not None:
                comments = gh_issue_by_pr.get(n, [])
            else:
                comments = client.get(f"/repos/{org_repo}/issues/{n}/comments",
                                      {"per_page": 100}) or []
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
            acts = load_activities(rest, n)
            bb_bodies = bb_top_comments(acts)
            gh_bodies = [norm_comment(c.get("body")) for c in comments]
            missing = []
            for b in bb_bodies:
                if not any(norm_comment(b) in g or g in norm_comment(b) for g in gh_bodies):
                    missing.append(b[:120])
            if missing:
                report["notes"].append(f"PR {n}: {len(missing)} BB comment(s) not found on GH")
                for m in missing[:5]:
                    report["notes"].append(f"   missing: {m!r}")

            # inline (file-anchored) review threads: BB anchored comments
            # (/pr_<n>_activities anchor.path) vs GH review comments. GH side
            # comes from the repo-wide map (load_gh_review_comments) fetched
            # once; fall back to a per-PR request only if the map is absent
            # (and only when the scrape actually has inline comments).
            # GEI prunes threads whose BB anchor is orphaned (old diff revision,
            # no longer matches the final diff → REVIEW_THREAD_MISSING family),
            # so orphaned-root losses are the expected bucket; NON-orphaned
            # losses are the real signal.
            bb_inline = bb_inline_comments(acts)
            if gh_inline_by_pr is not None:
                gh_inline = gh_inline_by_pr.get(n, [])
            elif bb_inline:
                gh_inline = list(client.paginate(
                    f"/repos/{org_repo}/pulls/{n}/comments")) or []
            else:
                gh_inline = []
            gh_inline_bodies = [norm_comment(c.get("body")) for c in gh_inline]
            gh_roots = [c for c in gh_inline if c.get("in_reply_to_id") is None]
            # "outdated": GH keeps the comment but nulls `line` (original_line
            # keeps the old position) — i.e. it couldn't anchor to the final
            # diff. This is the bucket orphaned/force-pushed anchors land in.
            gh_outdated = sum(1 for c in gh_roots if c.get("line") is None)
            bb_roots = [b for b in bb_inline if b["root"]]
            bb_orphaned_roots = [b for b in bb_roots if b["orphaned"]]
            mis = [b for b in bb_inline
                   if not any(b["text"] in g or g in b["text"]
                              for g in gh_inline_bodies)]
            mis_orph = [b for b in mis if b["orphaned"]]
            mis_non = [b for b in mis if not b["orphaned"]]
            mis_roots = [b for b in mis if b["root"]]
            mis_replies = [b for b in mis if not b["root"]]
            if mis:
                line = (f"PR {n}: {len(mis)}/{len(bb_inline)} inline comment(s) "
                        f"missing on GH "
                        f"({len(mis_orph)} under orphaned roots — expected; "
                        f"{len(mis_non)} NON-orphaned)")
                if mis_non:
                    line += (f" e.g. {mis_non[0]['text'][:80]!r} "
                             f"path={mis_non[0]['path']}")
                report["notes"].append(line)
            if strict_inline and mis_non:
                report["genuine"].append(
                    f"PR {n}: {len(mis_non)} NON-orphaned inline comment(s) "
                    f"missing on GH (strict-inline) — first: "
                    f"{mis_non[0]['text'][:80]!r} path={mis_non[0]['path']}")

            rec = {
                "schema": DEEP_SCHEMA,
                "pr": n,
                "bb_reviewers": bb_rev,
                "bb_reviewer_statuses": bb_status,
                "gh_reviewers": gh_rev_all,
                "gh_reviewer_best_state": gh_best,
                "bb_comment_count": len(bb_bodies),
                "gh_comment_count": len(comments),
                "gh_comment_missing": len(missing),
                "gh_review_count": len(reviews),
                "bb_inline_threads": len(bb_roots),
                "bb_orphaned_threads": len(bb_orphaned_roots),
                "bb_inline_comments": len(bb_inline),
                "gh_inline_threads": len(gh_roots),
                "gh_outdated_threads": gh_outdated,
                "gh_inline_comments": len(gh_inline),
                "bb_inline_missing": len(mis),
                "bb_inline_missing_orphaned": len(mis_orph),
                "bb_inline_missing_nonorphaned": len(mis_non),
                "bb_inline_missing_roots": len(mis_roots),
                "bb_inline_missing_replies": len(mis_replies),
            }
            report["deep"].append(rec)
            if deepf:
                deepf.write(json.dumps(rec) + "\n")
                deepf.flush()
            done.add(n)
            if state_path:
                json.dump({"schema": DEEP_SCHEMA, "deep_done": sorted(done)},
                          open(state_path, "w"))
            if i % progress_every == 0 or i == total:
                now = time.time()
                rate = (i - last_i) / max(now - last_t, 0.001)   # recent window
                eta = (total - i) / max(rate, 1e-9)
                pct = 100.0 * i / total if total else 100.0
                log(f"deep {i}/{total} ({pct:4.1f}%, {rate:,.1f}/s, "
                    f"ETA {int(eta)//3600}:{int(eta)%3600//60:02d}:{int(eta)%60:02d}) "
                    f"gh-budget-remaining={client.remaining}")
                last_t, last_i = now, i
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
                    help="persist GH API responses to an append-only JSONL file "
                         "and reuse them on re-runs (fast iteration; no network "
                         "for cached URLs). A legacy single-object JSON cache is "
                         "migrated in place. Default: no persistent cache.")
    ap.add_argument("--refresh", action="store_true",
                    help="with --cache: revalidate cached URLs via ETag instead "
                         "of serving them from disk (slower, fresh data)")
    ap.add_argument("--strict-inline", action="store_true",
                    help="deep: treat NON-orphaned inline (file-anchored) review "
                         "comments missing on GH as a genuine (gate-failing) "
                         "finding instead of an advisory note. Orphaned-root "
                         "losses (GEI's known pruning bucket) stay advisory.")
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
        gh_inline_by_pr = gh_issue_by_pr = None
        log("fetching repo-wide inline review comments (one paginated pass)")
        try:
            gh_inline_by_pr = load_gh_review_comments(client, org_repo)
        except Exception as e:
            log(f"repo-wide review comments failed ({e}); "
                f"falling back to per-PR requests")
        log("fetching repo-wide issue comments (one paginated pass)")
        try:
            gh_issue_by_pr = load_gh_issue_comments(client, org_repo)
        except Exception as e:
            log(f"repo-wide issue comments failed ({e}); "
                f"falling back to per-PR requests")
        verify_pr_deep(prs, client, org_repo, report, args.state,
                       args.limit_prs, args.progress_every, rest,
                       strict_inline=args.strict_inline,
                       gh_inline_by_pr=gh_inline_by_pr,
                       gh_issue_by_pr=gh_issue_by_pr)
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
    deep = report["deep"]
    if deep:
        tally = {k: sum(d.get(k, 0) for d in deep) for k in (
            "bb_inline_threads", "bb_orphaned_threads", "bb_inline_comments",
            "gh_inline_threads", "gh_outdated_threads", "gh_inline_comments",
            "bb_inline_missing", "bb_inline_missing_orphaned",
            "bb_inline_missing_nonorphaned",
            "bb_inline_missing_roots", "bb_inline_missing_replies")}
        log("[inline-tally] " + json.dumps(tally))
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