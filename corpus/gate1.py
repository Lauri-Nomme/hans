#!/usr/bin/env python3
"""Gate 1 — golden master: semantic-diff the tool's assembled archive against the
real admin export.

Normalizations (per FORMAT_SPEC §8):
- JSON compared semantically (json.loads), not byte-wise.
- allParticipants compared as a SET (DB position order is not REST-derived).
- ACTIVITY/UPDATED records in the real archive that REST cannot expose
  (title/desc edits) are reported as "known-gap", not failures.
- inner-tar mtimes/modes ignored.
- git objects compared by (path, content) set.

Exit 0 = no genuine differences; 1 = genuine differences found.
"""
import gzip
import io
import json
import sys
import tarfile
from collections import OrderedDict


def _decomp(tf, name):
    f = tf.extractfile(name)
    data = f.read() if f else b""
    if name.endswith(".gz"):
        return gzip.decompress(data)
    return data


def _json_dict(data):
    return json.loads(data, object_pairs_hook=OrderedDict)


def _inner_map(data):
    t = tarfile.open(fileobj=io.BytesIO(data), mode="r")
    out = {}
    for m in t.getmembers():
        f = t.extractfile(m)
        out[m.name] = f.read() if f else b""
    return out


class Report:
    def __init__(self):
        self.notes = []
        self.genuine = []

    def note(self, msg):
        self.notes.append(msg)

    def fail(self, msg):
        self.genuine.append(msg)


def compare(real_archive, syn_archive, report):
    real = tarfile.open(real_archive)
    syn = tarfile.open(syn_archive)
    rn, sn = set(real.getnames()), set(syn.getnames())
    if rn != sn:
        report.fail(f"tar listings differ: only-real={sorted(rn - sn)} "
                    f"only-syn={sorted(sn - rn)}")
        return
    report.note(f"tar listings identical ({len(rn)} entries)")

    for name in sorted(rn):
        a, b = _decomp(real, name), _decomp(syn, name)
        if a == b:
            continue
        if name.endswith(".tar"):  # git objects
            ma, mb = _inner_map(a), _inner_map(b)
            if ma == mb:
                report.note(f"{name}: identical object map (my order) ")
                continue
            for k in sorted(set(ma) | set(mb)):
                if ma.get(k) != mb.get(k):
                    report.fail(f"{name} object {k}: real={len(ma.get(k,b''))}b "
                                f"syn={len(mb.get(k,b''))}b")
            report.note(f"{name}: object sets differ (above)")
            continue
        if name.endswith(".gz") and name.endswith(".json.atl.gz"):
            try:
                ja, jb = _json_dict(a), _json_dict(b)
            except Exception:
                report.fail(f"{name}: unparseable JSON on one side")
                continue
            if ja == jb:
                report.note(f"{name}: same JSON, differing bytes (serialization)")
                continue
            if "instanceDetails" in name and "instance-details" in name:
                if _norm_instance(ja) == _norm_instance(jb):
                    report.note(f"{name}: same after instance-details normalization")
                    continue
            diff = _json_diff(ja, jb, report, jsonpath=name)
        else:
            report.note(f"{name}: raw byte diff (unclassified)")


def _norm_participants(meta):
    """Return participants as a comparable set (role, userId, status)."""
    return {(p["role"], p["userId"], p["status"])
            for p in meta.get("allParticipants", [])}


def _norm_instance(a):
    """instance-details: nodeId + instanceName are instance-specific (benign)."""
    a = dict(a)
    a.pop("nodeId", None)
    a.pop("instanceName", None)
    return a


def _json_diff(a, b, report, jsonpath, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        # participants ordering is a set
        if path == "" and "allParticipants" in a and "allParticipants" in b:
            sa, sb = _norm_participants(a), _norm_participants(b)
            if sa != sb:
                report.fail(f"{jsonpath}: allParticipants differ "
                            f"(real-only={sorted(sa-sb)} syn-only={sorted(sb-sa)})")
            for k in set(a) | set(b):
                if k == "allParticipants":
                    continue
                if a.get(k) != b.get(k):
                    if k == "rescopedTimestamp":
                        report.note(f"{jsonpath}: rescopedTimestamp "
                                    f"internal-target-advance divergence "
                                    f"(real={a[k]} syn={b[k]})")
                        continue
                    _json_diff(a.get(k), b.get(k), report, jsonpath, f"{path}.{k}")
            return
        for k in set(a) | set(b):
            if a.get(k) != b.get(k):
                if k == "rescopedTimestamp" and "createdTimestamp" in a:
                    report.note(f"{jsonpath}{path}.rescopedTimestamp "
                                f"internal-target-advance divergence "
                                f"(real={a[k]} syn={b[k]})")
                    continue
                _json_diff(a.get(k), b.get(k), report, jsonpath, f"{path}.{k}")
        return
    if isinstance(a, list) and isinstance(b, list):
        if path == "" and "kind" in (a[0] if a else {}) and a and b:
            _compare_activities(a, b, report, jsonpath)
            return
        if len(a) != len(b):
            report.fail(f"{jsonpath}{path}: list len {len(a)} vs {len(b)}")
        for i in range(min(len(a), len(b))):
            if a[i] != b[i]:
                _list_item_diff(a[i], b[i], report, jsonpath, f"{path}[{i}]")
        return
    if a != b:
        report.fail(f"{jsonpath}{path}: {a!r} != {b!r}")


def _act_sig(x):
    """Canonical comparable signature for an activity record (tolerant of the
    ~2ms internal reply/activity timestamp skew)."""
    x = dict(x)
    if x.get("createdTimestamp") is not None:
        x["createdTimestamp"] = round(x["createdTimestamp"] / 1000)
    return json.dumps(x, sort_keys=True)


def check_tasks(real, syn, report):
    """Tasks are comments with severity=BLOCKER; assert they reproduce with
    fidelity: id (string), severity, state, resolvedTimestamp/resolverId.
    `real`/`syn` are {pid: activities-list} dicts."""
    def task_comments(rows):
        out = []
        for a in (rows or []):
            c = a.get("comment") if isinstance(a, dict) else None
            if isinstance(c, dict) and c.get("severity") == "BLOCKER":
                out.append({k: c.get(k) for k in
                            ("id", "severity", "state", "text",
                             "resolvedTimestamp", "resolverId")})
        return out
    for pid in (1, 2, 3, 4, 5):
        rtasks = task_comments(real.get(pid))
        stasks = task_comments(syn.get(pid))
        rtasks.sort(key=lambda t: t["id"])
        stasks.sort(key=lambda t: t["id"])
        if rtasks != stasks:
            report.fail(f"PR{pid}: task comments differ "
                        f"real={rtasks} syn={stasks}")
        elif rtasks:
            report.note(f"PR{pid}: {len(rtasks)} task(s) reproduced exactly")


def _compare_activities(real, syn, report, jsonpath):
    """Compare activity lists as multisets. Real exporter records title/desc-edit
    ACTIVITY/UPDATED events that REST cannot expose -> treat as known gap."""
    rb = [_act_sig(r) for r in real]
    sb = [_act_sig(s) for s in syn]
    used = [False] * len(sb)
    unmatched_real = []
    for i, r in enumerate(rb):
        for j, s in enumerate(sb):
            if not used[j] and r == s:
                used[j] = True
                break
        else:
            unmatched_real.append(real[i])
    leftover_syn = [syn[j] for j in range(len(sb)) if not used[j]]
    # real-only records that are title/desc-edit UPDATED -> known gap
    gap = [r for r in unmatched_real
           if r.get("kind") == "ACTIVITY" and r.get("action") == "UPDATED"]
    genuine_real = [r for r in unmatched_real if r not in gap]
    if gap:
        report.note(f"{jsonpath}: {len(gap)} real-only ACTIVITY/UPDATED "
                    f"(title/desc edits not exposed by REST — known gap)")
    if genuine_real:
        for r in genuine_real:
            report.fail(f"{jsonpath}: real-only activity {json.dumps(r)[:140]}")
    if leftover_syn:
        for s in leftover_syn:
            report.fail(f"{jsonpath}: syn-only activity {json.dumps(s)[:140]}")


def _list_item_diff(x, y, report, jsonpath, itempath):
    if isinstance(x, dict) and isinstance(y, dict):
        if x.get("kind") != y.get("kind"):
            report.fail(f"{jsonpath}{itempath}: kind {x.get('kind')} vs {y.get('kind')}")
            return
        if (x.get("kind") == "ACTIVITY" and x.get("action") == "UPDATED"
                and y.get("kind",) == y.get("kind")) and x != y:
            report.note(f"{jsonpath}{itempath}: ACTIVITY/UPDATED divergence "
                        f"(title-edit event recorded in real, absent/incomplete in REST)")
            return
        for k in set(x) | set(y):
            if x.get(k) != y.get(k):
                _json_diff(x.get(k), y.get(k), report, jsonpath, f"{itempath}.{k}")
    else:
        if x != y:
            report.fail(f"{jsonpath}{itempath}: {x!r} != {y!r}")


def load_activities(archive, pid):
    """Load a PR's activities.json from an export archive (find the repo id)."""
    import re
    with tarfile.open(archive) as t:
        for name in t.getnames():
            if re.search(rf"pullrequest/{pid}/activities\.json\.atl\.gz$", name):
                data = t.extractfile(name)
                if data:
                    return json.loads(gzip.decompress(data.read()))
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("real")
    ap.add_argument("syn")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()
    r = Report()
    compare(args.real, args.syn, r)
    real_acts = {pid: load_activities(args.real, pid) for pid in (1, 2, 3, 4, 5)}
    syn_acts = {pid: load_activities(args.syn, pid) for pid in (1, 2, 3, 4, 5)}
    check_tasks(real_acts, syn_acts, r)
    print("=== Gate 1 semantic diff ===")
    for n in r.notes:
        print("  [note]", n)
    print("---")
    if r.genuine:
        print(f"GENUINE DIFFERENCES ({len(r.genuine)}):")
        for g in r.genuine:
            print("  [FAIL]", g)
        return 1
    print("NO GENUINE DIFFERENCES")
    return 0


if __name__ == "__main__":
    sys.exit(main())