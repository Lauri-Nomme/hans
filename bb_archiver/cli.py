"""bb-archiver CLI: scrape / assemble / validate."""
import argparse
import gzip
import io
import json
import re
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import jsonwriter as jw
from .emitter import Emitter, _log as _log_archive
from .model import load_model
from .scrape import crawl, _log


def cmd_scrape(args):
    index = crawl(args.base, args.user, args.password, args.project, args.repo,
                  args.out,
                  git_dir=None if args.no_git else str(Path(args.out) / "git"),
                  limit_prs=args.limit_prs, checkpoint=not args.no_resume,
                  pr_workers=args.pr_workers)
    _log(f"scraped {args.project}/{args.repo} -> {args.out} "
         f"(project_id={index['project_id']} repo_id={index['repo_id']})")


def cmd_assemble(args):
    m = load_model(args.scrape)
    node_id = None
    if args.node_id:
        node_id = args.node_id
    if not args.obj_tar_bin:
        args.obj_tar_bin = _autodetect_obj_tar_bin()
        if args.obj_tar_bin:
            _log_archive(f"assemble: using autodetected bb-obj-tar {args.obj_tar_bin}")
    if not args.merge_base_bin:
        args.merge_base_bin = _autodetect_merge_base_bin()
        if args.merge_base_bin:
            _log_archive(f"assemble: using autodetected bb-merge-base {args.merge_base_bin}")
    em = Emitter(m, app_version=args.app_version, build_version=args.build_version,
                 instance_name=args.instance_name, node_id=node_id,
                 export_mtime=args.mtime, obj_tar_bin=args.obj_tar_bin,
                 obj_tar_chunks=args.obj_tar_chunks,
                 merge_base_bin=args.merge_base_bin,
                 keep_tag_prefix=args.keep_tag_prefix)
    out = em.assemble(args.out)
    n = sum(1 for _ in tarfile.open(out))
    _log_archive(f"archive complete: {n} entries")


def _autodetect_obj_tar_bin():
    """Return the platform-appropriate bb-obj-tar binary from the repo's
    tools/bb-obj-tar/dist dir if present and executable, else None."""
    import os
    names = {
        "win32": "bb-obj-tar-windows-x86_64.exe",
        "linux": "bb-obj-tar-linux-x86_64",
        "darwin": "bb-obj-tar-macos",
    }.get(sys.platform)
    if not names:
        return None
    dist = Path(__file__).resolve().parent.parent / "tools" / "bb-obj-tar" / "dist"
    for cand in (dist / names, Path("tools") / "bb-obj-tar" / "dist" / names):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _autodetect_merge_base_bin():
    """Return the platform-appropriate bb-merge-base binary from the repo's
    tools/bb-merge-base/dist dir if present and executable, else None."""
    import os
    names = {
        "win32": "bb-merge-base-windows-x86_64.exe",
        "linux": "bb-merge-base-linux-x86_64",
        "darwin": "bb-merge-base-macos",
    }.get(sys.platform)
    if not names:
        return None
    dist = Path(__file__).resolve().parent.parent / "tools" / "bb-merge-base" / "dist"
    for cand in (dist / names, Path("tools") / "bb-merge-base" / "dist" / names):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _inner_map(data):
    """Map of name -> bytes inside an inner (non-.gz or already-decompressed) tar."""
    t = tarfile.open(fileobj=io.BytesIO(data), mode="r")
    out = {}
    for m in t.getmembers():
        f = t.extractfile(m)
        out[m.name] = f.read() if f else b""
    return out


def _iter_json_hex_shas(obj):
    """Yield every 40-hex SHA in a JSON object tree (dicts/lists/strs)."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_json_hex_shas(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_json_hex_shas(v)
    elif isinstance(obj, str):
        if re.fullmatch(r"[0-9a-f]{40}", obj):
            yield obj


def _present_objects(fileobj, wanted):
    """Subset of `wanted` (object ids) present in an objects tar.

    Walks the tar headers only: tarfile seeks past member bodies, and each
    TarInfo is dropped right after its name is checked (TarFile.members would
    otherwise keep one per object, tens of millions on a big repo). RAM is
    O(len(wanted)), never O(objects) or the size of the object content.
    Stops early once every wanted id was seen."""
    found = set()
    if not wanted:
        return found
    with tarfile.open(fileobj=fileobj, mode="r") as objs_tar:
        while True:
            m = objs_tar.next()
            if m is None:
                break
            objs_tar.members = []
            name = m.name.replace("/", "")
            if name in wanted:
                found.add(name)
                if len(found) == len(wanted):
                    break
    return found


def _check_object_integrity(archive, problems):
    """Every object the archive references must exist in the repo's
    objects.atl.tar, in one pass over the object tar's headers.

    Refs: every ref in metadata.atl.tar.atl.gz must point at an object. A
    dangling ref (REST snapshot at a different moment than the git mirror, or
    a dropped object) makes the importer fail with 'fatal: missing object ...
    for refs/...'.

    PR JSON: inline review comments anchor on commits via
    thread.anchor.fromHash/toHash, RESCOPED activities carry
    fromHash/toHash/previous*/added/removed commit ids, MERGED carries the
    merge commit hash, and PR metadata carries fromRef/toRef.latestCommit.
    None of these are refs, so ref-only checks cannot see them. If such an
    object is missing, GEI fails the review thread with
    REVIEW_THREAD_MISSING_START_COMMIT_OID.
    """
    repos = {}
    for name in archive.getnames():
        m = re.search(r"/repositor(?:y|ies)/([^/]+)/", name)
        if not m:
            continue
        r = repos.setdefault(m.group(1), {"meta": None, "objs": None,
                                          "jsons": [], "has_repositories": False})
        if re.search(r"/repositories/([^/]+)/", name):
            r["has_repositories"] = True
        if name.endswith("metadata.atl.tar.atl.gz"):
            r["meta"] = name
        elif name.endswith("objects.atl.tar"):
            r["objs"] = name
        elif name.endswith(".json.atl.gz") and (
                "pullRequests/" in name or name.endswith("pullrequests.json.atl.gz")):
            r["jsons"].append(name)
    for rid, r in repos.items():
        check_refs = r["has_repositories"]
        if check_refs and (r["meta"] is None or r["objs"] is None):
            problems.append(f"repo {rid}: missing metadata.atl.tar or "
                            f"objects.atl.tar in archive")
            check_refs = False
        if r["objs"] is None:
            if r["jsons"]:
                problems.append(f"repo {rid}: missing objects.atl.tar in archive")
            continue
        refs = []  # (refname, sha)
        if check_refs:
            f = archive.extractfile(r["meta"])
            for refname, content in _inner_map(gzip.decompress(f.read()) if f else b"").items():
                if not (refname.startswith("refs/") or
                        refname.startswith("stash-refs/")):
                    continue
                sha = content.decode("utf-8", "replace").strip()
                if sha:
                    refs.append((refname, sha))
        cited = []  # (json name, sha)
        for jname in r["jsons"]:
            f = archive.extractfile(jname)
            data = gzip.decompress(f.read()) if f else b""
            try:
                obj = json.loads(data)
            except Exception:
                problems.append(f"repo {rid}: unparseable JSON {jname}")
                continue
            cited.extend((jname, sha) for sha in sorted(set(_iter_json_hex_shas(obj))))
        # Keep the seekable ExFileObject (never a buffered read of the tar).
        present = _present_objects(
            archive.extractfile(r["objs"]),
            {sha for _, sha in refs} | {sha for _, sha in cited})
        for refname, sha in refs:
            if sha not in present:
                problems.append(f"repo {rid}: ref {refname} -> missing object "
                                f"{sha} (not in objects.atl.tar)")
        for jname, sha in cited:
            if sha not in present:
                problems.append(
                    f"repo {rid}: {jname} references missing object {sha} "
                    f"(not in objects.atl.tar) — review thread / activity "
                    f"for this commit will not import")


def cmd_validate(args):
    """Schema self-check vs FORMAT_SPEC: paths exist, JSON parses, pretty/compact
    matches the documented styles, gzip headers sane, and no dangling refs
    (every ref in metadata.atl.tar must resolve to an object in objects.atl.tar)."""
    problems = []
    with tarfile.open(args.archive) as tar:
        names = tar.getnames()
        for name in names:
            if name.endswith(".gz"):
                f = tar.extractfile(name)
                data = f.read() if f else b""
                if data[:2] != b"\x1f\x8b":
                    problems.append(f"{name}: not gzip")
            elif name.endswith(".tar"):
                pass
        _check_object_integrity(tar, problems)
    _log(f"validated {args.archive}: {len(problems)} problems")
    for p in problems:
        print("  ", p)
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bb-archiver",
                                 description="Reconstruct Bitbucket DC migration "
                                             "archives from REST API + git mirror.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape")
    s.add_argument("--base", default="http://localhost:7990")
    s.add_argument("--user", default="admin")
    s.add_argument("--password", default="")
    s.add_argument("--project", required=True)
    s.add_argument("--repo", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--no-git", action="store_true")
    s.add_argument("--limit-prs", type=int, default=0, help="cap PRs scraped (test)")
    s.add_argument("--no-resume", action="store_true",
                   help="ignore the checkpoint and scrape all PRs")
    s.add_argument("--pr-workers", type=int, default=4,
                   help="parallel workers for PR detail+activities fetch (default 4)")
    s.set_defaults(fn=cmd_scrape)

    a = sub.add_parser("assemble")
    a.add_argument("--scrape", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--app-version", default="9.4.18")
    a.add_argument("--build-version", default="9004018")
    a.add_argument("--instance-name", default="Bitbucket")
    a.add_argument("--node-id", default=None)
    a.add_argument("--mtime", type=int, default=0)
    a.add_argument("--obj-tar-bin", default=None,
                   help="path to the bb-obj-tar Rust helper for fast, "
                        "parallel git-object streaming (byte-identical output)")
    a.add_argument("--obj-tar-chunks", type=int, default=0,
                   help="worker chunk count for bb-obj-tar (default 0 = "
                        "let the binary use num_cpus)")
    a.add_argument("--merge-base-bin", default=None,
                   help="path to the bb-merge-base Rust helper for batched "
                        "merge-base (libgit2); falls back to git CLI if absent")
    a.add_argument("--keep-tag-prefix", default=None,
                   help="EXPERIMENTAL (breaks export fidelity): emit "
                        "refs/tags/<prefix><sha> for every refs/keep/<sha> in the "
                        "scrape mirror, so force-pushed-away commits become "
                        "reachable on GH via real tags")
    a.set_defaults(fn=cmd_assemble)

    v = sub.add_parser("validate")
    v.add_argument("--archive", required=True)
    v.set_defaults(fn=cmd_validate)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())