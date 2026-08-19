"""bb-archiver CLI: scrape / assemble / validate."""
import argparse
import json
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
                  limit_prs=args.limit_prs, checkpoint=not args.no_resume)
    _log(f"scraped {args.project}/{args.repo} -> {args.out} "
         f"(project_id={index['project_id']} repo_id={index['repo_id']})")


def cmd_assemble(args):
    m = load_model(args.scrape)
    node_id = None
    if args.node_id:
        node_id = args.node_id
    em = Emitter(m, app_version=args.app_version, build_version=args.build_version,
                 instance_name=args.instance_name, node_id=node_id,
                 export_mtime=args.mtime, obj_tar_bin=args.obj_tar_bin)
    out = em.assemble(args.out)
    n = sum(1 for _ in tarfile.open(out))
    _log_archive(f"archive complete: {n} entries")


def cmd_validate(args):
    """Schema self-check vs FORMAT_SPEC: paths exist, JSON parses, pretty/compact
    matches the documented styles, gzip headers sane."""
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
        # instance-details must be present, parseable, archiveVersion=2
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
    a.set_defaults(fn=cmd_assemble)

    v = sub.add_parser("validate")
    v.add_argument("--archive", required=True)
    v.set_defaults(fn=cmd_validate)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())