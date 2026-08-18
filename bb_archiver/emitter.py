"""Emitter: serialize the model into a Bitbucket migration export tar.

Implements FORMAT_SPEC.md exactly: two JSON writers, bare-repo skeleton with
loose objects, PR metadata/activities, permissions, caches, hierarchy markers.
"""
import gzip
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import zlib
from collections import OrderedDict
from pathlib import Path

from . import jsonwriter as jw

META = "com.atlassian.bitbucket.server.bitbucket-instance-migration"
GIT = "com.atlassian.bitbucket.server.bitbucket-git_git"
GITPR = "com.atlassian.bitbucket.server.bitbucket-git_gitPullRequests"
LFS = "com.atlassian.bitbucket.server.bitbucket-git-lfs_gitLfsSettings"


def _gzbuf(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as g:
        g.write(data)
    return buf.getvalue()


class Emitter:
    def __init__(self, model, app_version="9.4.18", build_version="9004018",
                 instance_name="Bitbucket", node_id=None, export_mtime=None):
        self.m = model
        self.app_version = app_version
        self.build_version = build_version
        self.instance_name = instance_name
        self.node_id = node_id or "80b56f04-61aa-41f7-98be-22c104c8616c"
        self.mtime = int(export_mtime or time.time())
        self.pid = self.m.project_id
        self.rid = self.m.repo_id
        self.hid = self.m.hierarchy_id

    # ---------------- low-level JSON builders ----------------------------
    def instance_details(self):
        return _gzbuf(jw.pretty({
            "product": "Bitbucket Data Center",
            "version": self.app_version,
            "buildVersion": self.build_version,
            "archiveVersion": 2,
            "dataCenter": True,
            "instanceName": self.instance_name,
            "nodeId": self.node_id,
        }).encode())

    def project_file(self):
        pm = self.m.project_meta()
        return _gzbuf(jw.compact({
            "description": pm.get("description"),
            "id": str(pm["id"]),
            "key": pm["key"],
            "name": pm["name"],
            "public": bool(pm.get("public")),
            "type": 0,
        }).encode())

    def repository_file(self):
        rm = self.m.repo_meta()
        return _gzbuf(jw.compact(OrderedDict([
            ("forkable", bool(rm.get("forkable", True))),
            ("hierarchyId", self.hid),
            ("id", str(self.rid)),
            ("name", rm["name"]),
            ("projectId", str(self.pid)),
            ("public", bool(rm.get("public"))),
            ("scmId", rm.get("scmId", "git")),
            ("slug", rm["slug"]),
        ])).encode())

    def all_permissions(self):
        return _gzbuf(b'{"projectAdmin":false,"projectRead":false,"projectWrite":false}')

    def project_permissions(self):
        perm = {
            "groups": [],
            "permission": "PROJECT_ADMIN",
            "userIds": [self.m.ref_id_for(slug) for slug in sorted(self.m.users)],
        }
        return _gzbuf(jw.compact([perm]).encode())

    def repository_permissions(self):
        return _gzbuf(b"[]")

    def gitlfs_settings(self):
        return _gzbuf(b'{"enabled":false}')

    # ---------------- PR metadata ----------------------------------------
    def _status(self, p):
        if p.get("approved"):
            return "APPROVED"
        if p.get("status") == "NEEDS_WORK":
            return "NEEDS_WORK"
        return "UNAPPROVED"

    def _participant(self, user, role, approved=False, status="UNAPPROVED",
                     last_reviewed=None):
        d: "OrderedDict[str, object]" = OrderedDict()
        if last_reviewed:
            d["lastReviewedCommit"] = last_reviewed
        d["role"] = role
        d["status"] = self._status({"approved": approved, "status": status})
        d["userId"] = self.m.ref_id_for(user.get("slug"))
        return d

    def _rescope_ts(self, pr, activities):
        """rescopedTimestamp = last RESCOPED activity, else createdTimestamp."""
        last = pr.get("createdDate")
        for a in activities:
            if a.get("action") == "RESCOPED":
                last = a.get("createdDate", last)
        return last

    def pr_metadata(self, pr):
        ref = lambda r: OrderedDict([   # noqa: E731
            ("displayId", r.get("displayId")),
            ("id", r.get("id")),
            ("latestCommit", r.get("latestCommit")),
        ])
        parts = [self._participant(pr["author"].get("user"), "AUTHOR")]
        for r in (pr.get("reviewers") or []):
            parts.append(self._participant(
                r.get("user"), "REVIEWER",
                r.get("approved"), r.get("status"), r.get("lastReviewedCommit")))
        for p in (pr.get("participants") or []):
            if p.get("role") == "PARTICIPANT":
                parts.append(self._participant(
                    p.get("user"), "PARTICIPANT"))
        activities = self.m.activities(pr["id"]) or []
        meta: "OrderedDict[str, object]" = OrderedDict([("allParticipants", parts)])
        if pr.get("closedDate"):
            meta["closedTimestamp"] = pr["closedDate"]
        meta["createdTimestamp"] = pr["createdDate"]
        meta["description"] = pr.get("description") or ""
        meta["draft"] = bool(pr.get("draft"))
        meta["fromRef"] = ref(pr["fromRef"])
        meta["id"] = pr["id"]
        meta["rescopedTimestamp"] = self._rescope_ts(pr, activities)
        meta["state"] = pr["state"]
        meta["title"] = pr.get("title") or ""
        meta["toRef"] = ref(pr["toRef"])
        meta["updatedTimestamp"] = pr.get("updatedDate")
        meta["version"] = pr.get("version", 1)
        return _gzbuf(jw.pretty(meta).encode())

    # ---------------- PR activities --------------------------------------
    def _thread(self, comment):
        t: "OrderedDict[str, object]" = OrderedDict()
        anchor = comment.get("anchor")
        if anchor:
            a: "OrderedDict[str, object]" = OrderedDict()
            if "diffType" in anchor:
                a["diffType"] = anchor["diffType"]
            if "fileType" in anchor:
                a["fileType"] = anchor["fileType"]
            if anchor.get("fromHash"):
                a["fromHash"] = anchor["fromHash"]
            a["line"] = anchor.get("line", 0)
            if "lineType" in anchor:
                a["lineType"] = anchor["lineType"]
            a["orphaned"] = bool(anchor.get("orphaned"))
            a["path"] = anchor["path"]
            if anchor.get("toHash"):
                a["toHash"] = anchor["toHash"]
            t["anchor"] = a
        t["createdTimestamp"] = comment["createdDate"]
        t["resolved"] = bool(comment.get("threadResolved"))
        # thread.updatedTimestamp = last thread activity (top-level created or
        # latest reply created) — comment text edits do NOT bump it.
        latest = comment["createdDate"]
        for r in comment.get("comments") or []:
            latest = max(latest, r["createdDate"])
        t["updatedTimestamp"] = latest
        return t

    def _comment(self, comment, top=True):
        c: "OrderedDict[str, object]" = OrderedDict()
        c["authorId"] = self.m.ref_id_for(comment["author"]["slug"])
        c["comments"] = [self._comment(r, top=False) for r in comment.get("comments") or []]
        c["createdTimestamp"] = comment["createdDate"]
        c["id"] = str(comment["id"])
        if comment.get("resolvedDate") is not None:
            c["resolvedTimestamp"] = comment["resolvedDate"]
        resolver = comment.get("resolver")
        if isinstance(resolver, dict) and resolver.get("slug"):
            c["resolverId"] = self.m.ref_id_for(resolver["slug"])
        c["severity"] = comment.get("severity", "NORMAL")
        c["state"] = comment.get("state", "OPEN")
        c["text"] = comment.get("text") or ""
        if top:
            c["thread"] = self._thread(comment)
        c["updatedTimestamp"] = comment.get("updatedDate", comment["createdDate"])
        return c

    def _comment_events(self, comment):
        """Emit COMMENT:OTHER REPLIED/EDITED records for a comment subtree."""
        events = []
        for reply in comment.get("comments") or []:
            events.append(self._ev("COMMENT:OTHER", reply["createdDate"],
                                   reply["author"]["slug"],
                                   {"commentAction": "REPLIED",
                                    "commentId": str(reply["id"])}))
            events.extend(self._comment_events(reply))
        if (comment.get("updatedDate", comment["createdDate"])
                != comment["createdDate"]):
            events.append(self._ev("COMMENT:OTHER", comment["updatedDate"],
                                   comment["author"]["slug"],
                                   {"commentAction": "EDITED",
                                    "commentId": str(comment["id"])}))
        return events

    def _ev(self, kind, ts, slug, extra=None):
        """Activity record; field order matches the real exporter (<kind>,
        then ACTION for ACTIVITY, else createdTimestamp/userId then extra)."""
        d: "OrderedDict[str, object]" = OrderedDict([("kind", kind)])
        extra = dict(extra or {})
        has_extra = bool(extra)
        if kind == "ACTIVITY":
            d["action"] = extra.pop("action", None)
        if has_extra:
            d["createdTimestamp"] = ts
            d["userId"] = self.m.ref_id_for(slug)
            for k, v in extra.items():
                d[k] = v
        return d

    def pr_activities(self, pr):
        comments, others = [], []
        for a in self.m.activities(pr["id"]) or []:
            slug = a["user"]["slug"]
            ts = a["createdDate"]
            action = a["action"]
            if action == "COMMENTED":
                c = a.get("comment")
                if c is None:
                    continue
                comments.append(self._ev("COMMENT:ADDED", ts, slug,
                                         {"comment": self._comment(c)}))
                comments.extend(self._comment_events(c))
            elif action == "UPDATED" and (a.get("addedReviewers") or a.get("removedReviewers")):
                others.append(self._ev("REVIEWERS:UPDATED", ts, slug, OrderedDict([
                    ("addedIds", [self.m.ref_id_for(u["slug"])
                                  for u in a.get("addedReviewers") or []]),
                    ("removedIds", [self.m.ref_id_for(u["slug"])
                                    for u in a.get("removedReviewers") or []]),
                ])))
            elif action == "RESCOPED":
                commits = []
                for c in (a.get("added", {}) or {}).get("commits", []):
                    commits.append(OrderedDict([("action", "ADDED"),
                                                ("commitId", c["id"])]))
                for c in (a.get("removed", {}) or {}).get("commits", []):
                    commits.append(OrderedDict([("action", "REMOVED"),
                                                ("commitId", c["id"])]))
                others.append(self._ev("RESCOPED", ts, slug, OrderedDict([
                    ("commits", commits),
                    ("fromHash", a["fromHash"]),
                    ("previousFromHash", a["previousFromHash"]),
                    ("previousToHash", a["previousToHash"]),
                    ("toHash", a["toHash"]),
                    ("totalAdded", (a.get("added", {}) or {}).get("total", 0)),
                    ("totalRemoved", (a.get("removed", {}) or {}).get("total", 0)),
                ])))
            elif action == "MERGED":
                others.append(self._ev("MERGED", ts, slug, OrderedDict([
                    ("autoMerge", bool(a.get("autoMerge"))),
                    ("hash", a["commit"]["id"]),
                ])))
            else:
                others.append(self._ev("ACTIVITY", ts, slug, {"action": action}))
        comments.sort(key=lambda e: e["createdTimestamp"])
        others.sort(key=lambda e: e["createdTimestamp"])
        return _gzbuf(jw.pretty(comments + others).encode())

    # ---------------- PR caches ------------------------------------------
    def pr_cache(self, pr):
        """cached-ancestor.txt = fromTip,toTip,mergeBase (no newline)."""
        base = None
        diff = self.m.pr_diff(pr["id"]) or {}
        base = (diff.get("fromHash") if diff else None)
        if base is None:
            # fall back to merge-base of the two tips if we can compute it
            base = self._merge_base(pr)
        if base is None:
            base = self.m.pr(pr["id"])["fromRef"]["latestCommit"]
        line = f"{pr['fromRef']['latestCommit']},{pr['toRef']['latestCommit']},{base}"
        return _gzbuf(_tar_buf({"cached-ancestor.txt": line.encode()}))

    def _merge_base(self, pr):
        gitdir = self.m.dir / "git"
        if not (gitdir / "objects").exists():
            return None
        try:
            out = subprocess.run(
                ["git", "merge-base",
                 pr["fromRef"]["latestCommit"], pr["toRef"]["latestCommit"]],
                capture_output=True, text=True, check=True, cwd=gitdir)
            return out.stdout.strip() or None
        except Exception:
            return None

    # ---------------- git skeleton ---------------------------------------
    def _reflog(self, pr, activities):
        """Create line (0000 -> initial tip, floor sec) then one line per
        RESCOPED (old -> new, ceil sec), Bitbucket Mesh identity."""
        import math
        resc = [(math.ceil(a["createdDate"] / 1000),
                 a["previousFromHash"], a["fromHash"])
                for a in activities if a.get("action") == "RESCOPED"]
        created = pr.get("createdDate", 0) // 1000
        initial = resc[0][1] if resc else pr["fromRef"]["latestCommit"]
        lines = [f"{'0'*40} {initial} Bitbucket Mesh <bitbucket.mesh@atlassian.com> {created} +0000\n"]
        for ts, old, new in resc:
            lines.append(f"{old} {new} Bitbucket Mesh <bitbucket.mesh@atlassian.com> {ts} +0000\n")
        return "".join(lines).encode()

    def git_metadata(self):
        entries = {"HEAD": b"ref: refs/heads/main\n",
                   "config": CONFIG_BYTES,
                   "app-info/gc.pid": b"499@bd157217c6d7"}
        for b in self.m.branches() or []:
            display = b.get("displayId") or b["id"].replace("refs/heads/", "")
            entries[f"refs/heads/{display}"] = b["latestCommit"].encode() + b"\n"
        for t in self.m.tags() or []:
            display = t.get("displayId") or t["id"].replace("refs/tags/", "")
            target = t.get("hash") or t["latestCommit"]   # annotated -> tag object
            entries[f"refs/tags/{display}"] = target.encode() + b"\n"
        for pr in self.m.prs() or []:
            if pr.get("state") == "OPEN":
                yield_path = f"refs/pull-requests/{pr['id']}/from"
                entries[yield_path] = pr["fromRef"]["latestCommit"].encode() + b"\n"
            entries[f"stash-refs/pull-requests/{pr['id']}/from"] = \
                pr["fromRef"]["latestCommit"].encode() + b"\n"
            entries[f"logs/stash-refs/pull-requests/{pr['id']}/from"] = \
                self._reflog(pr, self.m.activities(pr["id"]) or [])
        return _gzbuf(_tar_buf(entries))

    def git_hooks(self):
        # empty tar = 1024 zero bytes (no entries); matches real exporter
        return _gzbuf(b"\x00" * 1024)

    def git_objects(self):
        """Repack the mirror into loose objects, return tar bytes."""
        gitdir = self.m.dir / "git"
        tmp = tempfile.mkdtemp(prefix="objpack-")
        subprocess.run(["git", "init", "-q", "-b", "unused", "--bare", tmp],
                       check=True, capture_output=True)
        try:
            # write a fresh pack (nothing to keep -> repack all), then unpack
            if os.path.isdir(os.path.join(gitdir, "objects", "pack")):
                subprocess.run(["git", "repack", "-adf"], check=True, cwd=gitdir)
            packdir = os.path.join(gitdir, "objects", "pack")
            packs = [p for p in os.listdir(packdir) if p.endswith(".pack")]
            if not packs:
                raise RuntimeError("no pack produced by repack")
            subprocess.run(["git", "unpack-objects", "-q", "-r"],
                           check=True, cwd=tmp,
                           input=Path(packdir, packs[0]).read_bytes())
            # now loose objects live in tmp/objects/<ab>/<rest>
            entries = {}
            objs = os.path.join(tmp, "objects")
            for ab in os.listdir(objs):
                for name in os.listdir(os.path.join(objs, ab)):
                    data = Path(objs, ab, name).read_bytes()
                    entries[f"{ab}/{name}"] = data
            return _tar_buf(entries, mode=0o400)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- assemble --------------------------------------------
    def assemble(self, out_path):
        with tarfile.open(out_path, "w", format=tarfile.PAX_FORMAT) as tar:
            def add(name, data):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                ti.mode = 0o644
                ti.mtime = self.mtime
                ti.uid = ti.gid = 0
                tar.addfile(ti, io.BytesIO(data))

            add(f"{META}_instanceDetails/instance-details.json.atl.gz",
                self.instance_details())
            add(f"{META}_metadata/project_{self.pid}/project.json.atl.gz",
                self.project_file())
            add(f"{META}_permissions/project/{self.pid}/all-permissions.json.atl.gz",
                self.all_permissions())
            add(f"{META}_permissions/project/{self.pid}/permissions.json.atl.gz",
                self.project_permissions())
            add(f"_/repository/hierarchy_begin/{self.hid}", b"")
            add(f"{META}_metadata/project_{self.pid}/repository_{self.rid}.json.atl.gz",
                self.repository_file())
            add(f"{META}_permissions/repository/{self.rid}/permissions.json.atl.gz",
                self.repository_permissions())
            add(f"{GIT}/repositories/{self.rid}/metadata/metadata.atl.tar.atl.gz",
                self.git_metadata())
            add(f"{GIT}/repositories/{self.rid}/hooks/hooks.atl.tar.atl.gz",
                self.git_hooks())
            add(f"{GIT}/repositories/{self.rid}/contents/objects.atl.tar",
                self.git_objects())
            add(f"{LFS}/{self.rid}/git-lfs-settings.json.atl.gz",
                self.gitlfs_settings())
            for pr in sorted(self.m.prs() or [], key=lambda p: p["id"]):
                pid = pr["id"]
                add(f"{META}_pullRequests/repository/{self.rid}/pullrequest/{pid}/metadata.json.atl.gz",
                    self.pr_metadata(pr))
                add(f"{META}_pullRequests/repository/{self.rid}/pullrequest/{pid}/activities.json.atl.gz",
                    self.pr_activities(pr))
                add(f"{GITPR}/repositories/{self.rid}/pullrequests/{pid}/caches.atl.tar.atl.gz",
                    self.pr_cache(pr))
            add(f"_/repository/hierarchy_end/{self.hid}", b"")
        return out_path


CONFIG_BYTES = (
    b"[include]\n"
    b"\t# Include the \"system\" gitconfig, which is used to apply common settings\n"
    b"\t# to all repositories in the system. Basic settings should be applied in\n"
    b"\t# the \"system\" gitconfig, not here, to avoid expensive upgrade tasks for\n"
    b"\t# applying changes to all existing repositories.\n"
    b"\tpath = ../../../config/git/system-config\n"
    b"\t# Include a special repository-config which includes details about the\n"
    b"\t# Java Repository associated with this on-disk repository.\n"
    b"\tpath = repository-config\n"
    b"[core]\n"
    b"\trepositoryformatversion = 0\n"
    b"\tfilemode = true\n"
    b"\tbare = true\n"
)


def _tar_buf(entries, mode=0o644):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in entries.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = 0
            ti.uid = ti.gid = 0
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()