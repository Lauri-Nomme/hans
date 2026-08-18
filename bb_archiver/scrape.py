"""Crawler: pull every REST representation of one repo into a scrape dir.

Output layout (consumed by `assemble`):
    <out>/rest/*.json            raw REST dumps (same naming as corpus/rest)
    <out>/users.json             harvested distinct users
    <out>/index.json             scrape metadata (base, project, repo, ids, git)
    <out>/warnings.jsonl         any unrecoverable data
    <out>/git/                   bare mirror clone incl. PR refs
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests


def crawl(base, user, password, project, repo, out, git_dir=None, limit_prs=0,
          http=None):
    out = Path(out)
    (out / "rest").mkdir(parents=True, exist_ok=True)
    http = http or requests.Session()
    http.auth = (user, password)
    api = f"{base}/rest/api/1.0/projects/{project}/repos/{repo}"

    index = {"scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "base": base, "project": project, "repo": repo,
             "project_id": None, "repo_id": None, "hierarchy_id": None,
             "users_endpoint": True, "prs": [], "git": {}}
    warnings = []

    def get(path, params=None):
        url = path if path.startswith("http") else f"{base}{path}"
        r = http.get(url, params=params, timeout=120)
        if r.status_code >= 400:
            warnings.append(f"GET {path} -> {r.status_code}")
            return None
        return r.json()

    def save(name, payload, endpoint):
        fp = out / "rest" / f"{name}.json"
        fp.write_text(json.dumps(payload), encoding="utf-8")
        index.setdefault("entities", []).append({"file": f"rest/{name}.json", "endpoint": endpoint})

    def save_paged(name, path, params=None):
        params = dict(params or {})
        values, start = [], 0
        while True:
            params["start"] = start
            p = get(path, params)
            if p is None:
                break
            values.extend(p.get("values", []))
            if p.get("isLastPage", True):
                break
            start = p.get("nextPageStart", start + len(p.get("values", [])))
        save(name, values, path)

    # --- users (harvest from all author/commenter/participant fields too) ---
    users = get("/rest/api/1.0/users?limit=1000")
    if users is not None:
        index["users_endpoint"] = True
    elif not http.auth or http.auth[0] != "admin":
        index["users_endpoint"] = False

    # --- project + repo ----------------------------------------------------
    proj = get(f"/rest/api/1.0/projects/{project}")
    rep = get(api)
    if proj is None or rep is None:
        raise RuntimeError("project or repo not found on scrape")
    index["project_id"] = proj["id"]
    index["repo_id"] = rep["id"]
    index["hierarchy_id"] = rep.get("hierarchyId")
    save("project_" + project, proj, f"/rest/api/1.0/projects/{project}")
    save(f"repo_{project}_{repo}", rep, api)

    save_paged(f"branches_{project}_{repo}", f"{api}/branches")
    save_paged(f"tags_{project}_{repo}", f"{api}/tags")

    # --- pull requests ------------------------------------------------------
    r = get(f"{api}/pull-requests", {"state": "ALL", "withAttributes": True, "limit": 100})
    prs = r["values"] if r else []
    save(f"pull-requests_{project}_{repo}", prs,
         f"{api}/pull-requests?state=ALL&withAttributes=true")
    for pr in prs[: limit_prs if limit_prs else len(prs)]:
        pid = pr["id"]
        index["prs"].append(pid)
        pre = f"{api}/pull-requests/{pid}"
        save(f"pr_{pid}", get(pre, {"withAttributes": True}), pre)
        save_paged(f"pr_{pid}_activities", f"{pre}/activities", {"limit": 100})
        save_paged(f"pr_{pid}_commits", f"{pre}/commits", {"limit": 100})
        diff = get(f"{pre}/diff")
        save(f"pr_{pid}_diff", diff, f"{pre}/diff")
        paths = sorted({d["destination"]["toString"] for d in (diff or {}).get("diffs", [])
                        if d.get("destination")})
        for p in paths:
            c = get(f"{pre}/comments", {"path": p, "limit": 100})
            if c is not None and c.get("values"):
                safe = p.replace("/", "_").replace(".", "_")
                save(f"pr_{pid}_comments_path_{safe}", c["values"], f"{pre}/comments?path={p}")
        print(f"[scrape] PR {pid}: activities/commits/diff/comments")

    # --- commits + commit comments ------------------------------------------
    shas = set()
    for br in (get(f"{api}/branches", {"limit": 100}) or {}).get("values", []):
        shas.add(br["latestCommit"])
    for tg in (get(f"{api}/tags", {"limit": 100}) or {}).get("values", []):
        shas.add(tg["latestCommit"])
    for c in (get(f"{api}/commits", {"until": "refs/heads/main", "limit": 100}) or {}).get("values", []):
        shas.add(c["id"])
    for sha in sorted(shas):
        d = get(f"{api}/commits/{sha}")
        if d is None:
            continue
        save(f"commit_{sha}", d, f"{api}/commits/{sha}")
        changed, start = [], 0
        while True:
            page = get(f"{api}/commits/{sha}/changes", {"limit": 100, "start": start})
            if page is None:
                break
            changed.extend(page.get("values", []))
            if page.get("isLastPage", True):
                break
            start = page.get("nextPageStart", start + len(page.get("values", [])))
        save(f"commit_{sha}_changes", changed, f"{api}/commits/{sha}/changes")
        for p in sorted({c["path"]["toString"] for c in changed if c.get("path")}):
            safe = p.replace("/", "_").replace(".", "_")
            cm = get(f"{api}/commits/{sha}/comments", {"path": p, "limit": 100})
            if cm and cm.get("values"):
                save(f"commit_{sha}_comments_path_{safe}", cm["values"],
                     f"{api}/commits/{sha}/comments?path={p}")

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
    # fetch everything incl. PR refs
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
    args = ap.parse_args()
    index = crawl(args.base, args.user, args.password, args.project, args.repo, args.out,
                  git_dir=None if args.no_git else os.path.join(args.out, "git"))
    print(f"scraped {index['project']}/{index['repo']} -> {args.out}")


if __name__ == "__main__":
    sys.exit(main())