#!/usr/bin/env python3
"""Phase 2 capture: dump every REST representation of the golden repo that the
Phase 4 archiver must reconstruct, saved raw into corpus/rest/, plus a manifest
mapping each entity -> dump file -> source endpoint.

Usage: corpus/capture.py [--base http://localhost:7990] [--user admin] [--pass ...]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REST_DIR = HERE / "rest"
MANIFEST = HERE / "manifest.json"

PROJECT = "FIX"
REPO = "golden"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BB_BASE", "http://localhost:7990"))
    ap.add_argument("--user", default=os.environ.get("BB_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("BB_PASSWORD", "admin-lab-pw"))
    ap.add_argument("--limit", type=int, default=0, help="max PRs to capture (0 = all)")
    args = ap.parse_args()

    c = Capture(args)
    c.run()


class Capture:
    def __init__(self, args):
        self.base = args.base.rstrip("/")
        self.auth = (args.user, args.password)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.limit = args.limit
        self.manifest = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "base": self.base, "project": PROJECT, "repo": REPO,
                         "entities": []}
        REST_DIR.mkdir(parents=True, exist_ok=True)

    # --- http ---------------------------------------------------------------
    def get(self, path, params=None):
        r = self.session.get(f"{self.base}{path}", params=params, timeout=120)
        r.raise_for_status()
        return r.json()

    def save(self, name, payload, endpoint):
        """Persist raw JSON (as text, unpretty) and record it in the manifest."""
        fp = REST_DIR / f"{name}.json"
        text = payload if isinstance(payload, str) else json.dumps(payload)
        fp.write_text(text, encoding="utf-8")
        self.manifest["entities"].append({
            "file": f"rest/{name}.json",
            "endpoint": endpoint,
        })
        return fp

    def save_paged(self, name, path, params=None):
        """Fully paginate a list endpoint and save the concatenated values."""
        params = dict(params or {})
        values, start = [], 0
        endpoint = path
        while True:
            params["start"] = start
            page = self.get(path, params)
            values.extend(page.get("values", []))
            if page.get("isLastPage", True):
                break
            start = page.get("nextPageStart", start + len(page.get("values", [])))
        return self.save(name, values, endpoint)

    # --- top-level ----------------------------------------------------------
    def run(self):
        r = self.get(f"/rest/api/1.0/users?limit=1000")
        self.save("users", r["values"], "/rest/api/1.0/users")
        r = self.get(f"/rest/api/1.0/projects/{PROJECT}")
        self.save("project_FIX", r, f"/rest/api/1.0/projects/{PROJECT}")
        r = self.get(f"/rest/api/1.0/projects/{PROJECT}/repos/{REPO}")
        self.save("repo_FIX_golden", r, f"/rest/api/1.0/projects/{PROJECT}/repos/{REPO}")
        self.save_paged("branches_FIX_golden",
                        f"/rest/api/1.0/projects/{PROJECT}/repos/{REPO}/branches")
        self.save_paged("tags_FIX_golden",
                        f"/rest/api/1.0/projects/{PROJECT}/repos/{REPO}/tags")
        r = self.get(f"/rest/api/1.0/projects/{PROJECT}/repos/{REPO}/pull-requests",
                     {"state": "ALL", "withAttributes": True, "limit": 100})
        prs = r["values"]
        self.save("pull-requests_FIX_golden", prs,
                  f"/rest/api/1.0/projects/{PROJECT}/repos/{REPO}/pull-requests?state=ALL&withAttributes=true")
        print(f"[capture] {len(prs)} PRs, users, project, repo, branches, tags")

        for pr in prs[: self.limit if self.limit else len(prs)]:
            self.capture_pr(pr["id"])

        self.capture_commits()
        MANIFEST.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        print(f"[capture] manifest written to {MANIFEST}")
        return 0

    # --- pull requests ------------------------------------------------------
    def capture_pr(self, pr_id):
        pre = f"/rest/api/1.0/projects/{PROJECT}/repos/{REPO}/pull-requests/{pr_id}"
        self.save(f"pr_{pr_id}", self.get(pre, {"withAttributes": True}), pre)
        self.save_paged(f"pr_{pr_id}_activities", f"{pre}/activities",
                        {"limit": 100})
        self.save_paged(f"pr_{pr_id}_commits", f"{pre}/commits", {"limit": 100})
        self.save(f"pr_{pr_id}_diff", self.get(f"{pre}/diff"), f"{pre}/diff")
        # comments are path-scoped: fetch per changed file from the diff
        diff = self.get(f"{pre}/diff")
        paths = []
        for d in diff.get("diffs", []):
            dst = d.get("destination") or {}
            p = dst.get("toString")
            if p:
                paths.append(p)
        for p in sorted(set(paths)):
            safe = p.replace("/", "_").replace(".", "_")
            comments = self.get(f"{pre}/comments", {"path": p, "limit": 100})
            vals = comments.get("values", [])
            if vals or True:
                self.save(f"pr_{pr_id}_comments_path_{safe}", vals,
                          f"{pre}/comments?path={p}")
        print(f"[capture] PR {pr_id}: detail, activities, commits, diff, "
              f"comments on {len(set(paths))} files")

    # --- commits ------------------------------------------------------------
    def capture_commits(self):
        """Commit detail + path-scoped commit comments for main history, all
        branch tips and both tags."""
        pre = f"/rest/api/1.0/projects/{PROJECT}/repos/{REPO}"
        shas = set()

        for br in self.get(f"{pre}/branches", {"limit": 100}).get("values", []):
            shas.add(br["latestCommit"])
        for tg in self.get(f"{pre}/tags", {"limit": 100}).get("values", []):
            shas.add(tg["latestCommit"])
        for commit in self.get(f"{pre}/commits", {"until": "refs/heads/main",
                                                  "limit": 100}).get("values", []):
            shas.add(commit["id"])

        for sha in sorted(shas):
            try:
                detail = self.get(f"{pre}/commits/{sha}")
                self.save(f"commit_{sha}", detail, f"{pre}/commits/{sha}")
            except requests.HTTPError as e:
                if e.response.status_code == 404:  # tag objects, deleted refs
                    continue
                raise
            # commit comments are path-scoped: one GET per changed file
            changed = []
            params, start = {"limit": 100}, 0
            while True:
                page = self.get(f"{pre}/commits/{sha}/changes", {**params, "start": start})
                changed.extend(page.get("values", []))
                if page.get("isLastPage", True):
                    break
                start = page.get("nextPageStart", start + len(page.get("values", [])))
            self.save(f"commit_{sha}_changes", changed, f"{pre}/commits/{sha}/changes")
            paths = [c.get("path", {}).get("toString") for c in changed]
            for p in sorted(set(paths)):
                safe = p.replace("/", "_").replace(".", "_")
                comments = self.get(f"{pre}/commits/{sha}/comments",
                                    {"path": p, "limit": 100}).get("values", [])
                if comments:
                    self.save(f"commit_{sha}_comments_path_{safe}", comments,
                              f"{pre}/commits/{sha}/comments?path={p}")
        print(f"[capture] commit detail for {len(shas)} commits")


if __name__ == "__main__":
    sys.exit(main())