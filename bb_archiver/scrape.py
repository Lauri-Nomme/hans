"""Crawler: pull every REST representation of one repo that the assembler needs.

PRINCIPLE: only fetch what the archive actually needs and what is NOT already in
the git mirror. Everything git provides (commits, trees, blobs, tags, branches,
diffs, merge-bases) is fetched via the mirror, NOT via REST. So the REST surface
is limited to:
    project, repo, users, branches, tags,
    the PR list, and per-PR detail + activities.

Per-PR `diff`, `commits`, per-path `comments`, and all per-commit dumps are
deliberately NOT fetched here — the mirror + the activity stream (which embeds
full comment objects) cover them. (The corpus tool `corpus/capture.py` fetches
the extra dumps for supervision; the production scraper does not.)

Branches and tags ARE also present in the git mirror (refs/heads/*, refs/tags/*),
but they fetch them via REST too: they are called one paginated call each (negligible
vs per-PR activities), and the tags dump carries the annotated-vs-lightweight
distinction (`hash` for the tag object vs `latestCommit` for the peeled commit)
that the archive's refs/tags/* files require. The real scale cost — and the part
that is genuinely REST-only — is the per-PR activity stream.

**Git mirror completeness:** Bitbucket does NOT advertise every object over the
git protocol. In particular `refs/stash-refs/*` (which carries the historical
source-tip of every PR, incl. merged/declined PRs whose branch was later
deleted) is never advertised, so a plain `git fetch` never transports those
commits — yet the REST activity/fromRef references them. `git upload-pack` CAN
still serve an arbitrary object given its SHA, so the mirror post-processes the
main fetch by SHA-fetching any commit the archive will reference that the main
fetch did not already obtain. See `fetch_mirror`.

Scalable: paginated everywhere, rate-limit aware (honors X-RateLimit-*),
retry/backoff, and resumable via a checkpoint file (completed PR ids), so a run
can be interrupted and continued across rate-limit windows.

Output layout (consumed by `assemble`):
    <out>/rest/*.json        raw REST dumps (project/repo/users/branches/tags,
                             PR list, per-PR detail+activities)
    <out>/users.json         harvested distinct users
    <out>/index.json         scrape metadata (base, project, repo, ids, prs)
    <out>/checkpoint.json    resumable progress (PR ids completed)
    <out>/git/               bare mirror clone incl. PR refs
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests


def _log(msg):
    """Print with an ISO-8601 UTC timestamp prefix for correlation."""
    print(f"[scrape] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}",
          flush=True)


class Api:
    """requests wrapper: pagination, rate-limit sleep, retry/backoff."""

    def __init__(self, base, auth, quiet=False, headroom=10):
        self.base = base
        self.auth = auth
        self.session = requests.Session()
        self.session.auth = auth
        self.headroom = headroom
        self.limit = self.remaining = self.reset = None

    def _log(self, msg):
        """Print with an ISO-8601 UTC timestamp prefix for correlation."""
        _log(msg)

    def _throttle(self):
        if self.remaining is None:
            return
        if self.remaining > self.headroom:
            return
        wait = (self.reset or time.time()) - time.time() + 5
        if wait > 0:
            self._log(f"rate limit low ({self.remaining}/{self.limit}) — "
                      f"sleeping {int(wait)}s")
            time.sleep(wait)

    def _limits(self, resp):
        self.limit = int(resp.headers.get("X-RateLimit-Limit", 0) or 0)
        rl = resp.headers.get("X-RateLimit-Remaining")
        if rl is not None:
            self.remaining = int(rl)
        rs = resp.headers.get("X-RateLimit-Reset")
        if rs:
            self.reset = int(rs)

    def get(self, path, params=None, retries=4):
        url = path if path.startswith("http") else f"{self.base}{path}"
        for attempt in range(retries + 1):
            self._throttle()
            try:
                r = self.session.get(url, params=params, timeout=180)
                self._limits(r)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 403 and "rate limit" in r.text.lower():
                    wait = max((self.reset or 0) - time.time() + 2, 30)
                    self._log(f"403 rate-limited — sleeping {int(wait)}s")
                    time.sleep(wait)
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = min(2 ** attempt * 2, 60)
                    self._log(f"HTTP {r.status_code} — retry in {wait}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
            except (requests.RequestException, ConnectionError, TimeoutError):
                wait = min(2 ** attempt * 2, 60)
                self._log(f"network error — retry in {wait}s")
                time.sleep(wait)
        raise RuntimeError(f"gave up after {retries} retries: {path}")

    def paginate(self, path, params=None, per_page=1000):
        """Page through a collection. Bitbucket caps `limit` at 1000, so request
        the largest page to minimize REST round-trips (and rate-limit overhead)."""
        params = dict(params or {})
        values, start = [], 0
        while True:
            p = self.get(path, {**params, "start": start, "limit": per_page})
            if p is None:
                break
            values.extend(p.get("values", []))
            if p.get("isLastPage", True):
                break
            start = p.get("nextPageStart", start + len(p.get("values", [])))
        return values


def crawl(base, user, password, project, repo, out, git_dir=None, limit_prs=0,
          checkpoint=True, http=None):
    out = Path(out)
    (out / "rest").mkdir(parents=True, exist_ok=True)
    api = Api(base, (user, password))
    rest_path = f"/rest/api/1.0/projects/{project}/repos/{repo}"

    index = {"scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "base": base, "project": project, "repo": repo,
             "project_id": None, "repo_id": None, "hierarchy_id": None,
             "users_endpoint": True, "prs": [], "git": {}}

    def save(name, payload, endpoint):
        fp = out / "rest" / f"{name}.json"
        fp.write_text(json.dumps(payload), encoding="utf-8")
        index.setdefault("entities", []).append(
            {"file": f"rest/{name}.json", "endpoint": endpoint})

    # --- project + repo ----------------------------------------------------
    proj = api.get(f"/rest/api/1.0/projects/{project}")
    rep = api.get(rest_path)
    index["project_id"] = proj["id"]
    index["repo_id"] = rep["id"]
    index["hierarchy_id"] = rep.get("hierarchyId")
    save("project_" + project, proj, f"/rest/api/1.0/projects/{project}")
    save(f"repo_{project}_{repo}", rep, rest_path)

    # --- users (paginated) --------------------------------------------------
    save("users", api.paginate("/rest/api/1.0/users"), "/rest/api/1.0/users")

    # --- branches + tags (paginated; refs also present in the git mirror) ----
    branches = api.paginate(f"{rest_path}/branches")
    tags = api.paginate(f"{rest_path}/tags")
    save(f"branches_{project}_{repo}", branches, f"{rest_path}/branches")
    save(f"tags_{project}_{repo}", tags, f"{rest_path}/tags")

    # --- pull requests (paginated) ------------------------------------------
    prs = api.paginate(f"{rest_path}/pull-requests", {"state": "ALL", "withAttributes": True})
    save(f"pull-requests_{project}_{repo}", prs,
         f"{rest_path}/pull-requests?state=ALL&withAttributes=true")

    # --- checkpoint / resume ------------------------------------------------
    ckpt_path = out / "checkpoint.json"
    done = set()
    if checkpoint and ckpt_path.exists():
        try:
            done = set(json.loads(ckpt_path.read_text()).get("prs_done", []))
        except Exception:
            done = set()

    todo = [p["id"] for p in prs]
    if limit_prs:
        todo = todo[:limit_prs]
    todo = [pid for pid in todo if pid not in done]
    total = len(todo)
    for i, pid in enumerate(todo, 1):
        pre = f"{rest_path}/pull-requests/{pid}"
        save(f"pr_{pid}", api.get(pre, {"withAttributes": True}), pre)
        save_paged_list = api.paginate(f"{pre}/activities")
        save(f"pr_{pid}_activities", save_paged_list, f"{pre}/activities")
        index["prs"].append(pid)
        if checkpoint:
            done.add(pid)
            ckpt_path.write_text(json.dumps({"prs_done": sorted(done)}))
        if total and (i % 25 == 0 or i == total):
            api._log(f"PR {i}/{total} done")

    # --- git mirror ----------------------------------------------------------
    if git_dir is not None:
        # Derive the exact ref set the archive will emit, so we can guarantee the
        # mirror contains every object behind a REST-referenced SHA. The main
        # advertised fetch misses historical PR source-tips (hidden stash-refs);
        # fetch_mirror SHA-fetches only what is still missing and writes the refs.
        needed_refs = []
        for b in branches:
            display = b.get("displayId") or b["id"].replace("refs/heads/", "")
            needed_refs.append((f"refs/heads/{display}", b["latestCommit"]))
        for t in tags:
            display = t.get("displayId") or t["id"].replace("refs/tags/", "")
            target = t.get("hash") or t["latestCommit"]   # annotated -> tag object
            needed_refs.append((f"refs/tags/{display}", target))
        for p in prs:
            sha = p["fromRef"]["latestCommit"]
            if p.get("state") == "OPEN":
                needed_refs.append((f"refs/pull-requests/{p['id']}/from", sha))
            needed_refs.append((f"stash-refs/pull-requests/{p['id']}/from", sha))
        fetch_mirror(git_dir, base, user, password, project, repo, index,
                     needed_refs=needed_refs)

    (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def git_mirror_url(base, user, password, project, repo):
    # Auth is delivered via an HTTP Basic header (_git_auth_args), NOT by
    # embedding credentials in the URL (tokens with '+', '=', '/' get mangled
    # by URL decoding and the fetch redirects to a login page).
    return f"{base.rstrip('/')}/scm/{project}/{repo}.git"


def _git_auth_args(user, password):
    """git -c args to send credentials as a Basic header.

    Embedding user:password in the fetch URL corrupts tokens containing URL
    special characters (e.g. '+', '=', '/'), which makes the server redirect to
    its login page and the fetch die. The Base header avoids re-encoding the
    password entirely. Returns [] when no user is given."""
    import base64
    if not user:
        return []
    tok = base64.b64encode(f"{user}:{password}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {tok}"]


def _available_objects(git_dir, git="git"):
    """Set of every object sha present in the mirror (loose + packed).

    Uses a single `git cat-file --batch-all-objects` instead of one
    `cat-file -e` per SHA — O(repo) once, not O(n candidate SHAs)."""
    p = subprocess.run([git, "cat-file", "--batch-all-objects",
                        "--batch-check=%(objectname)"],
                       capture_output=True, text=True, cwd=git_dir)
    return {line.strip() for line in p.stdout.splitlines() if line.strip()}


def _check_refname(git_dir, name, git="git"):
    """Authoritative refname check via `git check-ref-format` (returns bool)."""
    r = subprocess.run([git, "check-ref-format", name], cwd=git_dir,
                       capture_output=True)
    return r.returncode == 0


def _current_refs(git_dir, git="git"):
    """{refname: sha} of every ref present in the mirror, in one pass.

    Used to decide which refs still need writing on a resume, independent of
    whether the object is already present (an object can be in the odb yet not
    reachable from any ref — e.g. a prior run fetched it by SHA but crashed
    before update-ref)."""
    p = subprocess.run([git, "for-each-ref", "--format=%(refname) %(objectname)"],
                       capture_output=True, text=True, cwd=git_dir)
    out = {}
    for line in p.stdout.splitlines():
        if line.strip():
            name, _, obj = line.partition(" ")
            out[name] = obj
    return out


def _apply_refs_batch(git_dir, refs, batch_limit=10, git="git"):
    """Write `refs` ([(name, sha), ...]) so they are reachable, using the
    batched `git update-ref --stdin` fast path.

    update-ref --stdin is batch-atomic: one invalid refname aborts the whole
    group. Rather than fall back to one subprocess per ref (slow for ~3k with a
    handful bad), on failure we recursively split the set and retry both halves,
    stopping recursion at `batch_limit` items, below which each ref is validated
    (check-ref-format) and written individually.
    Returns count of refs successfully written.
    """
    if not refs:
        return 0
    lines = "".join(f"update {n} {s}\n" for n, s in refs)
    r = subprocess.run([git, "update-ref", "--stdin"], input=lines,
                       text=True, cwd=git_dir, capture_output=True)
    if r.returncode == 0:
        return len(refs)
    if len(refs) <= batch_limit:
        # bottom out: isolate the bad one(s) individually, authoritative check
        wrote = 0
        for n, s in refs:
            if not _check_refname(git_dir, n, git):
                # The mirror ref only exists to keep the object reachable for
                # repack; it does not need to match the archive's name. Replace
                # the invalid name with a valid, SHA-keyed one so the object is
                # still retained, and keep a warning.
                synth = f"refs/keep/{s}"
                rr = subprocess.run([git, "update-ref", synth, s], cwd=git_dir,
                                    capture_output=True)
                if rr.returncode == 0:
                    _log(f"git mirror: warning: ref name {n!r} is not a valid "
                         f"git refname — wrote {synth!r} instead to retain {s}")
                    wrote += 1
                else:
                    _log(f"git mirror: warning: could not write {synth!r}: "
                         f"{rr.stderr.strip()[:160]}")
                continue
            rr = subprocess.run([git, "update-ref", n, s], cwd=git_dir,
                                capture_output=True)
            if rr.returncode == 0:
                wrote += 1
            else:
                _log(f"git mirror: warning: could not write {n!r}: "
                     f"{rr.stderr.strip()[:160]}")
        return wrote
    # recursive bisection: the batch failed; retry on each half
    mid = len(refs) // 2
    _log(f"git mirror: update-ref batch failed on {len(refs)} refs — splitting")
    return (_apply_refs_batch(git_dir, refs[:mid], batch_limit, git)
            + _apply_refs_batch(git_dir, refs[mid:], batch_limit, git))


def fetch_mirror(git_dir, base, user, password, project, repo, index,
                 needed_refs=(), fetch_batch=50, git="git"):
    """Build the bare mirror + guarantee object completeness.

    Sub-phases (in order):
      1. init + `git fetch origin` of every advertised ref namespace.
      2. SHA-fetch any ref the archive will emit whose object the main fetch
         did NOT already obtain (Bitbucket hides refs/stash-refs/* but still
         serves arbitrary objects by SHA).
      3. `git update-ref` the fetched SHAs so they are *reachable* — otherwise
         `git repack -adf` in the emitter would treat them as dangling and drop
         them from the object store.
    """
    git_dir = str(git_dir)
    if not (os.path.isdir(os.path.join(git_dir, "objects"))):
        subprocess.run([git, "init", "--bare", "--quiet", git_dir], check=True)
    url = git_mirror_url(base, user, password, project, repo)
    auth = _git_auth_args(user, password)
    r = subprocess.run([git, "remote", "get-url", "origin"], cwd=git_dir,
                       capture_output=True)
    if r.returncode == 0:
        subprocess.run([git, "remote", "set-url", "origin", url], check=True,
                       cwd=git_dir)
    else:
        subprocess.run([git, "remote", "add", "origin", url], check=True,
                       cwd=git_dir)
    # sub-phase 1: everything currently advertised (commits/trees/blobs/tags).
    # refs/stash-refs/* advertises nothing; kept in the spec for symmetry.
    subprocess.run([git, *auth, "-c", "remote.origin.fetch=", "fetch",
                    "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
                    "+refs/pull-requests/*:refs/pull-requests/*",
                    "+refs/stash-refs/*:refs/stash-refs/*"],
                   check=True, cwd=git_dir)

    # sub-phases 2+3: recover the hidden but REST-referenced commits.
    # Compute the fetch delta in one pass against the entire mirror object set
    # (rather than probing each candidate SHA individually)...
    avail = _available_objects(git_dir, git)
    missing = [(name, sha) for name, sha in needed_refs if sha not in avail]
    if missing:
        shas = sorted(set(sha for _, sha in missing))
        _log(f"git mirror: {len(missing)} ref(s) reference objects the main "
             f"fetch did not carry; SHA-fetching {len(shas)} unique object(s) "
             f"(Bitbucket hides refs/stash-refs/*)")
        for i in range(0, len(shas), fetch_batch):
            batch = shas[i:i + fetch_batch]
            _log(f"git mirror: sha-fetch {i + 1}-{min(i + fetch_batch, len(shas))}"
                 f"/{len(shas)}")
            subprocess.run([git, *auth, "fetch", "origin", *batch], check=True,
                           cwd=git_dir, capture_output=True)

    # ...then ALWAYS make the refs correct, keyed on REF state not object
    # presence. A resumed run can have the objects present (a prior run fetched
    # them by SHA) yet the refs missing (it crashed before update-ref); in that
    # case the objects are dangling and repack would drop them from the archive.
    # Re-snapshot objects AFTER the fetch loop so a fresh SHA-fetch is included.
    avail = _available_objects(git_dir, git)
    current = _current_refs(git_dir, git)
    to_ref = []
    unfetchable = []
    for n, s in needed_refs:
        if current.get(n) == s:
            continue                    # already correct
        if s in avail:
            to_ref.append((n, s))       # object present: writable ref
        else:
            unfetchable.append((n, s))  # object absent even after fetch
    for n, s in unfetchable:
        _log(f"git mirror: warning: object {s} for {n} is not present and could "
             f"not be fetched — ref skipped (object will not be in the archive)")
    if unfetchable and missing:
        _log(f"git mirror: {len(unfetchable)} ref(s) refer to objects that are "
             f"absent even after the SHA-fetch and were dropped \u2014 review the "
             f"warn lines above")
    elif unfetchable:
        _log(f"git mirror: {len(unfetchable)} ref(s) refer to objects absent "
             f"from the mirror on this run; they were skipped (see warnings)")
    if to_ref:
        _log(f"git mirror: writing {len(to_ref)} ref(s)")
        # See _apply_refs_batch: batched fast path, recursive bisection on
        # failure, per-ref bottom-out only for small sets. With to_ref filtered
        # to object-present refs, the only remaining failure class is an invalid
        # refname — which is rare, so bisection stays fast.
        wrote = _apply_refs_batch(git_dir, to_ref, git=git)
        _log(f"git mirror: wrote {wrote} ref(s)" +
             (f", {len(to_ref) - wrote} skipped"
              if wrote != len(to_ref) else ""))
    else:
        _log("git mirror: refs already correct; nothing to write")

    _log(f"git mirror ready at {git_dir}")
    return git_dir

    _log(f"git mirror ready at {git_dir}")
    return git_dir


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BB_BASE", "http://localhost:7990"))
    ap.add_argument("--user", default=os.environ.get("BB_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("BB_PASSWORD", "admin-lab-pw"))
    ap.add_argument("--project", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--limit-prs", type=int, default=0)
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()
    index = crawl(args.base, args.user, args.password, args.project, args.repo, args.out,
                  git_dir=None if args.no_git else os.path.join(args.out, "git"),
                  limit_prs=args.limit_prs, checkpoint=not args.no_resume)
    _log(f"scraped {index['project']}/{index['repo']} -> {args.out}")


if __name__ == "__main__":
    sys.exit(main())