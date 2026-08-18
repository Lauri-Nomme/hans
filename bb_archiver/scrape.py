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
but we fetch them via REST too: they are a single paginated call each (negligible
vs per-PR activities), and the tags dump carries the annotated-vs-lightweight
distinction (`hash` for the tag object vs `latestCommit` for the peeled commit)
that the archive's refs/tags/* files require. The real scale cost — and the part
that is genuinely REST-only — is the per-PR activity stream.

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


class Api:
    """requests wrapper: pagination, rate-limit sleep, retry/backoff."""

    def __init__(self, base, auth, quiet=False, headroom=10):
        self.base = base
        self.auth = auth
        self.session = requests.Session()
        self.session.auth = auth
        self.headroom = headroom
        self.limit = self.remaining = self.reset = None

    def _throttle(self):
        if self.remaining is None:
            return
        if self.remaining > self.headroom:
            return
        wait = (self.reset or time.time()) - time.time() + 5
        if wait > 0:
            print(f"[scrape] rate limit low ({self.remaining}/{self.limit}) — "
                  f"sleeping {int(wait)}s", flush=True)
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
                    print(f"[scrape] 403 rate-limited — sleeping {int(wait)}s", flush=True)
                    time.sleep(wait)
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = min(2 ** attempt * 2, 60)
                    print(f"[scrape] HTTP {r.status_code} — retry in {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
            except (requests.RequestException, ConnectionError, TimeoutError):
                wait = min(2 ** attempt * 2, 60)
                print(f"[scrape] network error — retry in {wait}s", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"gave up after {retries} retries: {path}")

    def paginate(self, path, params=None, per_page=100):
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
    save(f"branches_{project}_{repo}", api.paginate(f"{rest_path}/branches"),
         f"{rest_path}/branches")
    save(f"tags_{project}_{repo}", api.paginate(f"{rest_path}/tags"),
         f"{rest_path}/tags")

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
            print(f"[scrape] PR {i}/{total} done", flush=True)

    # --- git mirror ----------------------------------------------------------
    if git_dir is not None:
        fetch_mirror(git_dir, base, user, password, project, repo, index)

    (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def git_mirror_url(base, user, password, project, repo):
    url = f"{base}/scm/{project}/{repo}.git"
    if "@" not in url:
        url = url.replace("://", f"://{user}:{password}@", 1)
    return url


def fetch_mirror(git_dir, base, user, password, project, repo, index, git="git"):
    git_dir = str(git_dir)
    if not (os.path.isdir(os.path.join(git_dir, "objects"))):
        subprocess.run([git, "init", "--bare", "--quiet", git_dir], check=True)
    url = git_mirror_url(base, user, password, project, repo)
    subprocess.run([git, "remote", "add", "origin", url], check=True, cwd=git_dir)
    # fetch everything incl. PR refs (commits/trees/blobs/tags come from here)
    subprocess.run([git, "-c", "remote.origin.fetch=", "fetch",
                    "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
                    "+refs/pull-requests/*:refs/pull-requests/*",
                    "+refs/stash-refs/*:refs/stash-refs/*"],
                   check=True, cwd=git_dir)
    print(f"    git mirror ready at {git_dir}")
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
    print(f"scraped {index['project']}/{index['repo']} -> {args.out}")


if __name__ == "__main__":
    sys.exit(main())