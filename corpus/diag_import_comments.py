#!/usr/bin/env python3
"""Compare a Bitbucket scrape's inline review comments against an imported
GitHub repo's review comments, per PR, and classify each BB comment as:

  placed    - a GH review comment whose body matches, anchored (line set)
  outdated  - a GH review comment whose body matches but line/original_line
              are null (GH kept it, couldn't anchor it to the final diff)
  missing   - no GH review comment body matches

Purpose: after importing the golden fixture with GEI, see exactly which inline
comments survived, which went "outdated", and which vanished — per PR and split
by whether the BB anchor was orphaned.

Usage:
  python corpus/diag_import_comments.py --scrape scrape/golden9 \
      --repo ORG/REPO [--pat ghp_...]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate3 import (load_scrape, load_activities, bb_inline_comments,   # noqa: E402
                   norm_comment, load_gh_review_comments, RateLimitClient)


def match_gh(bb_text, gh_comments):
    """Return the first GH comment whose normalized body matches bb_text."""
    if not bb_text:
        return None
    for c in gh_comments:
        g = norm_comment(c.get("body"))
        if g and (bb_text in g or g in bb_text):
            return c
    return None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scrape", required=True)
    ap.add_argument("--repo", required=True, help="ORG/REPO on GitHub")
    ap.add_argument("--pat", default=os.environ.get("GH_PAT", ""))
    args = ap.parse_args()
    if not args.pat:
        print("--pat or GH_PAT required", file=sys.stderr)
        return 2

    index, prs, branches, tags, rest = load_scrape(args.scrape)
    client = RateLimitClient(args.pat)
    print(f"BB {index['project']}/{index['repo']}  ->  GH {args.repo}")
    gh_by_pr = load_gh_review_comments(client, args.repo)
    print(f"GH review comments: {sum(len(v) for v in gh_by_pr.values())} "
          f"across {len(gh_by_pr)} PRs\n")

    summ = {"placed": 0, "outdated": 0, "missing": 0,
            "missing_orphaned": 0, "missing_nonorphaned": 0,
            "gh_only": 0}
    for pr in prs:
        pid = pr["id"]
        bb = bb_inline_comments(load_activities(rest, pid))
        gh = gh_by_pr.get(pid, [])
        if not bb and not gh:
            continue
        rows = []
        matched_ids = set()
        for b in bb:
            c = match_gh(b["text"], gh)
            if c is None:
                st = "missing"
                summ["missing"] += 1
                summ["missing_orphaned" if b["orphaned"] else
                     "missing_nonorphaned"] += 1
            else:
                matched_ids.add(id(c))
                # GH "outdated": the line is no longer in the diff -> line is
                # null (original_line keeps the old position).
                anchored = c.get("line") is not None
                st = "placed" if anchored else "outdated"
                summ[st] += 1
            rows.append((st, b, c))
        extra = [c for c in gh if id(c) not in matched_ids]
        summ["gh_only"] += len(extra)

        origins = "orphaned" if any(b["orphaned"] for b in bb) else "live"
        print(f"PR {pid}  {pr.get('title','')[:60]!r}  "
              f"[{len(bb)} BB inline, {origins}, {len(gh)} GH]")
        for st, b, c in rows:
            anchor = f"{b['path']}:{b['line']}" if b.get("path") else "-"
            tag = {"placed": "placed  ",
                   "outdated": "OUTDATED",
                   "missing": "MISSING "}[st]
            extra_ = " orphaned" if b["orphaned"] else ""
            ghpos = (f"gh_line={c.get('line')} gh_orig={c.get('original_line')}"
                     if c is not None else f"anchor={anchor}{extra_}")
            print(f"   [{tag}] id={b['id']} d{b['depth']} {ghpos}  "
                  f"{b['text'][:60]!r}")
        if extra:
            print(f"   (+{len(extra)} GH comment(s) with no BB match)")
        print()

    print("=== summary ===")
    print(f"placed={summ['placed']}  outdated={summ['outdated']}  "
          f"missing={summ['missing']} "
          f"(orphaned={summ['missing_orphaned']}, "
          f"non-orphaned={summ['missing_nonorphaned']})  "
          f"gh_only={summ['gh_only']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
