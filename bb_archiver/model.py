"""Internal entity model + user harvester + stable synthetic ID allocation.

Consumes the scrape dir produced by `scrape.py` and exposes plain dicts that
`emit.py` serializes into archive files. All decisions recorded as warnings.
"""
import json
from collections import OrderedDict
from pathlib import Path

MISSING = object()


class Model:
    def __init__(self, scrape_dir):
        self.dir = Path(scrape_dir)
        self.rest = self.dir / "rest"
        self.index = json.loads((self.dir / "index.json").read_text())
        self.project = self.index["project"]
        self.repo = self.index["repo"]
        self.users = {}
        self.warnings = []
        self._cache = {}

    # -- load raw dumps -----------------------------------------------------
    def _load(self, name):
        """Read + parse a REST dump once, caching the result. These files are
        immutable for the lifetime of the model, and several are re-read many
        times per PR (activities in particular), so caching avoids redundant
        disk reads + JSON parses."""
        if name in self._cache:
            return self._cache[name]
        p = self.rest / f"{name}.json"
        data = json.loads(p.read_text()) if p.exists() else None
        self._cache[name] = data
        return data

    @property
    def project_id(self):
        return self.index["project_id"]

    @property
    def repo_id(self):
        return self.index["repo_id"]

    @property
    def hierarchy_id(self):
        return self.index.get("hierarchy_id")

    def project_meta(self):
        return self._load(f"project_{self.project}")

    def repo_meta(self):
        return self._load(f"repo_{self.project}_{self.repo}")

    def prs(self):
        return self._load(f"pull-requests_{self.project}_{self.repo}")

    def pr(self, pid):
        return self._load(f"pr_{pid}")

    def activities(self, pid):
        return self._load(f"pr_{pid}_activities")

    def pr_commits(self, pid):
        return self._load(f"pr_{pid}_commits")

    def pr_diff(self, pid):
        return self._load(f"pr_{pid}_diff")

    def branches(self):
        return self._load(f"branches_{self.project}_{self.repo}")

    def tags(self):
        return self._load(f"tags_{self.project}_{self.repo}")

    # --- user harvesting -----------------------------------------------------
    def harvest_users(self):
        """Collect distinct users from every author/commenter/reviewer field."""
        users = {}
        seen = set()

        def add(user):
            if not isinstance(user, dict) or not user.get("slug"):
                return
            key = user.get("name") or user.get("slug")
            if key in seen:
                return
            seen.add(key)
            users[user["slug"]] = {
                "slug": user["slug"],
                "displayName": user.get("displayName") or user.get("name"),
                "emailAddress": user.get("emailAddress"),
                "type": user.get("type", "NORMAL"),
            }

        for pr in (self.prs() or []):
            add(pr.get("author", {}).get("user"))
            for u in (pr.get("reviewers") or []) + (pr.get("participants") or []):
                add(u.get("user"))
            for act in (self.activities(pr["id"]) or []):
                add(act.get("user"))
                c = act.get("comment")
                if isinstance(c, dict):
                    add(c.get("author"))
        return OrderedDict(sorted(users.items(), key=lambda kv: kv[0]))

    # --- synthetic IDs (stable across runs, from natural keys) ---------------
    def ref_id_for(self, user_slug):
        """Derive 'slug|displayName||type' archive userId for a user slug."""
        u = self.users[self._slug_key(user_slug)]
        return f"{u['slug']}|{u['displayName']}||{u.get('type', 'NORMAL')}"

    def _slug_key(self, slug):
        return slug if slug in self.users else next(iter(self.users.keys()))


def load_model(scrape_dir):
    m = Model(scrape_dir)
    m.users = m.harvest_users()
    return m