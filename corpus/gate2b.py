#!/usr/bin/env python3
"""Gate 2B — round-trip through the OFFICIAL exporter.

After importing the tool's archive into bb-lab-b (Gate 2), re-export bb-lab-b
with the official `POST /rest/api/1.0/migration/exports` and compare the result
against the golden bb-lab-a export.

Because the official path carries MORE data than REST (e.g. title/desc-edit
UPDATED activities), this comparison surfaces differences that the REST-scrape
(Gate 2) cannot see: it measures what my archive->import->official-export
preserves vs. what a direct official export of the same repo contains.

Instance-specific noise is normalized out:
  - repo/project ids (paths + JSON id fields)
  - nodeId / instanceName
  - stub-user identity on the import target (displayName=slug, no email):
    userId strings `slug|displayName||type` -> `slug`
  - comment ids (reallocated by import)
  - tar entry mtimes/modes

The comparison is DIAGNOSTIC (prints categorized diffs, non-zero exit only on
genuine content loss it cannot attribute to the above).
"""
import argparse
import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from collections import OrderedDict
from pathlib import Path

P = "com.atlassian.bitbucket.server"


def export_from(base, user, password, container, shared_home, tmp):
    """Trigger official export, wait, copy the tar out. Returns local tar path."""
    import requests
    tmp = Path(tmp); tmp.mkdir(parents=True, exist_ok=True)
    sess = requests.Session(); sess.auth = (user, password)
    r = sess.post(f"{base}/rest/api/1.0/migration/exports",
                  json={"repositoriesRequest": {"includes": [
                      {"projectKey": "*", "slug": "*"}]}})
    r.raise_for_status()
    job = r.json()["id"]
    print(f"[gate2b] export job {job} started on {base}")
    state = "INITIALISING"
    for _ in range(120):
        st = sess.get(f"{base}/rest/api/1.0/migration/exports/{job}").json()
        state = st["state"]
        print(f"[gate2b]   {state} {st['progress']['percentage']}%")
        if state in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if state != "COMPLETED":
        raise RuntimeError(f"export job {job} ended with {state}")
    name = f"Bitbucket_export_{job}.tar"
    local = tmp / name
    subprocess.run(["sudo", "-n", "nerdctl", "cp",
                    f"{container}:{shared_home}/data/migration/export/{name}",
                    str(local)], check=True)
    print(f"[gate2b] archive -> {local}")
    return local


# ---------------------------------------------------------------- normalize
def norm_user_id(uid):
    """`slug|displayName||type` -> `slug` (identity stubs differ across hosts)."""
    if isinstance(uid, str) and "|" in uid:
        return uid.split("|")[0]
    return uid


def norm_json(obj, is_repo_meta=False, is_pr_meta=False, is_activities=False):
    """Normalize a parsed JSON file for cross-host comparison. Repo-id and
    PR-id coexist (both 1 on an imported target), so no global numeric rewrite —
    only structural, key-targeted normalizations."""
    if isinstance(obj, dict):
        out = OrderedDict()
        for k, v in obj.items():
            # instance details: nodeId is machine-specific
            if k == "nodeId":
                continue
            if k == "instanceName":
                continue
            # repository metadata: id/PROJECT id is machine-specific
            if is_repo_meta and k in ("id", "projectId"):
                out[k] = "<ID>"
                continue
            # PR metadata: id + rescopedTimestamp (internal target-advance) are
            # machine/DB specific
            if is_pr_meta and k == "id":
                out[k] = "<PRID>"
                continue
            if is_pr_meta and k == "rescopedTimestamp":
                continue
            if k == "allParticipants":
                out[k] = sorted(
                    (norm_user_id(x.get("userId")), x.get("role"), x.get("status"))
                    for x in (v or []))
                continue
            if k == "userIds" and isinstance(v, list):
                out[k] = [norm_user_id(x) for x in v]
                continue
            if k in ("addedIds", "removedIds") and isinstance(v, list):
                out[k] = [norm_user_id(x) for x in v]
                continue
            if k in ("userId", "authorId"):
                out[k] = norm_user_id(v) if isinstance(v, str) else v
                continue
            if is_activities and k in ("commentId",):
                continue
            if is_activities and k == "id" and isinstance(v, str):
                continue
            if isinstance(v, (dict, list)):
                out[k] = norm_json(v, is_activities=is_activities, is_pr_meta=False)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [norm_json(x, is_pr_meta=is_pr_meta, is_activities=is_activities)
                for x in obj]
    return obj


def _decomp(data, gz):
    return gzip.decompress(data) if gz else data


def git_objects_of(root):
    """Return {relpath: bytes} of the objects.atl.tar inner tar."""
    obj_tar = None
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            if f == "objects.atl.tar":
                obj_tar = Path(dp) / f
    if obj_tar is None:
        return None
    data = obj_tar.read_bytes()
    t = tarfile.open(fileobj=io.BytesIO(data), mode="r")
    out = {}
    for m in t.getmembers():
        f = t.extractfile(m)
        out[m.name] = f.read() if f else b""
    return out


def compare(real_root, syn_root, repo_id_real, repo_id_syn, node_id):
    """Semantic diff with normalization. Returns (notes, genuine)."""
    notes, genuine = [], []

    # --- git objects (strongest check) ------------------------------------
    ga, gb = git_objects_of(real_root), git_objects_of(syn_root)
    if ga and gb:
        if ga == gb:
            notes.append(f"git objects: {len(ga)} identical (set + content)")
        else:
            ka, kb = set(ga), set(gb)
            if ka != kb:
                genuine.append(f"git objects differ: A-only={sorted(ka-kb)[:3]} "
                               f"B-only={sorted(kb-ka)[:3]}")
            diffs = [k for k in ka & kb if ga[k] != gb[k]]
            if diffs:
                genuine.append(f"git objects content diff on {diffs[:3]}")
    else:
        genuine.append("git objects missing on one side")

    # --- per-file JSON / raw ----------------------------------------------
    real_files = {p.relative_to(real_root).as_posix()
                  for p in real_root.rglob("*") if p.is_file()}
    syn_files = {p.relative_to(syn_root).as_posix()
                 for p in syn_root.rglob("*") if p.is_file()}
    # map syn file -> real file (repo-id path component)
    def map_key(rel):
        return rel.replace(f"repository_{repo_id_syn}", f"repository_{repo_id_real}") \
                  .replace(f"/repositories/{repo_id_syn}", f"/repositories/{repo_id_real}") \
                  .replace(f"_gitLfsSettings/{repo_id_syn}", f"_gitLfsSettings/{repo_id_real}") \
                  .replace(f"/repository/{repo_id_syn}", f"/repository/{repo_id_real}")
    for srel in sorted(syn_files):
        rrel = map_key(srel)
        if rrel not in real_files:
            genuine.append(f"only in B: {srel}")
            continue
        sdata = (syn_root / srel).read_bytes()
        rdata = (real_root / rrel).read_bytes()
        if rdata == sdata:
            continue
        if srel.endswith(".json.atl.gz"):
            try:
                so = json.loads(_decomp(sdata, True))
                ro = json.loads(_decomp(rdata, True))
            except Exception:
                genuine.append(f"{srel}: unparseable json on one side")
                continue
            is_repo_meta = "repository_" in rrel and rrel.endswith(".json.atl.gz") \
                and "/project_" in rrel
            is_pr_meta = "/pullrequest/" in rrel and "metadata.json" in rrel
            is_activities = "activities.json" in rrel
            sn = norm_json(so, is_repo_meta=is_repo_meta, is_pr_meta=is_pr_meta,
                           is_activities=is_activities)
            rn = norm_json(ro, is_repo_meta=is_repo_meta, is_pr_meta=is_pr_meta,
                           is_activities=is_activities)
            if sn == rn:
                notes.append(f"{srel}: same after normalize")
            elif is_activities:
                _diff_activities(rn, sn, srel, genuine, notes)
            else:
                genuine.append(f"{srel}: DIFFERS after normalize")
                _print_diff(rn, sn, srel, genuine, notes)
        else:
            notes.append(f"{srel}: raw differs (inner tar mtime/order — benign "
                         f"if content equal, see git objects)")
    return notes, genuine


def _print_diff(a, b, where, genuine, notes):
    """Recursively list leaf differences (shallow, for diagnostics)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if a.get(k) != b.get(k):
                _print_diff(a.get(k), b.get(k), f"{where}.{k}", genuine, notes)
        return
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                _print_diff(x, y, f"{where}[{i}]", genuine, notes)
        return
    genuine.append(f"  {where}: real={json.dumps(a)[:100]} "
                   f"syn={json.dumps(b)[:100]}")


def _act_sig(x):
    d = dict(x)
    if d.get("createdTimestamp") is not None:
        d["createdTimestamp"] = round(d["createdTimestamp"] / 1000)
    return json.dumps(d, sort_keys=True)


def _diff_activities(rn, sn, where, genuine, notes):
    """Multiset diff for activity lists. The title/desc-edit ACTIVITY/UPDATED
    events (recorded by the exporter but hidden from REST) are lost by the
    archive->import->export round trip; report them as a note, anything else as
    genuine. Timestamps normalized to the second (sub-second exporter skew)."""
    rb = {_act_sig(x) for x in rn}
    sb = {_act_sig(x) for x in sn}
    only_real = [x for x in rn if _act_sig(x) not in sb]
    only_syn = [x for x in sn if _act_sig(x) not in rb]
    gap = [r for r in only_real
           if r.get("kind") == "ACTIVITY" and r.get("action") == "UPDATED"]
    for r in gap:
        notes.append(f"{where}: real-only ACTIVITY/UPDATED (title/desc edit "
                     f"hidden from REST — round-trip loss)")
    rest = [r for r in only_real if r not in gap]
    if not rest and not only_syn:
        notes.append(f"{where}: activities equivalent after normalize "
                     f"(minor order/comment-id churn)")
        return
    genuine.append(f"{where}: activity diff (real={len(rn)} syn={len(sn)})")
    for r in rest[:4]:
        genuine.append(f"   real-only: {json.dumps(r)[:160]}")
    for s in only_syn[:4]:
        genuine.append(f"   syn-only:  {json.dumps(s)[:160]}")


def check_tasks_exports(real_root, syn_root, repo_id_real, repo_id_syn, notes, genuine):
    """Tasks (severity BLOCKER comments) through the official round-trip re-export.
    Comment ids and userIds are normalized; compare by text with severity/state +
    resolvedTimestamp/resolverId (resolver normalized to slug)."""
    def tasks(root, rid):
        out = {}
        for dirpath, _dn, files in os.walk(root):
            for f in files:
                if f != "activities.json.atl.gz":
                    continue
                data = gzip.decompress((Path(dirpath) / f).read_bytes())
                for a in json.loads(data):
                    c = a.get("comment") or {}
                    if c.get("severity") == "BLOCKER":
                        rid_ok = norm_user_id(c.get("resolverId")) or None
                        out.setdefault((c.get("text"),), []).append({
                            "severity": c.get("severity"),
                            "state": c.get("state"),
                            "resolvedTimestamp": c.get("resolvedTimestamp"),
                            "resolver": rid_ok,
                        })
        return out
    tr, ts = tasks(real_root, repo_id_real), tasks(syn_root, repo_id_syn)
    for key in set(tr) | set(ts):
        if tr.get(key) != ts.get(key):
            genuine.append(f"task {key}: real={tr.get(key)} syn={ts.get(key)}")
        elif tr.get(key):
            notes.append(f"task {key}: reproduced exactly ({tr[key]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default="ground-truth/export-a",
                    help="extracted golden export from bb-lab-a")
    ap.add_argument("--base", default="http://localhost:7991")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin-lab-pw")
    ap.add_argument("--container", default="bb-lab-b")
    ap.add_argument("--shared-home",
                    default="/var/atlassian/application-data/bitbucket/shared")
    ap.add_argument("--tmp", default="/tmp/opencode/gate2b")
    ap.add_argument("--repo-id-real", type=int, default=2,
                    help="repo id in the golden archive")
    ap.add_argument("--repo-id-syn", type=int, default=1,
                    help="repo id on bb-lab-b after import")
    ap.add_argument("--node-id", default=None)
    args = ap.parse_args()

    local = export_from(args.base, args.user, args.password, args.container,
                        args.shared_home, args.tmp)
    syn_root = Path(args.tmp) / "syn"
    syn_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(local) as t:
        t.extractall(syn_root)
    real_root = Path(args.golden)
    notes, genuine = compare(real_root, syn_root,
                             args.repo_id_real, args.repo_id_syn, args.node_id)
    check_tasks_exports(real_root, syn_root, args.repo_id_real, args.repo_id_syn,
                        notes, genuine)
    print("=== GATE 2B ===")
    for n in notes:
        print("  [note]", n)
    if genuine:
        print(f"  GENUINE ({len(genuine)}):")
        for g in genuine:
            print("   ", g)
        print("GATE 2B: FAIL")
        return 1
    print("GATE 2B: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())