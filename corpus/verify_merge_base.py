#!/usr/bin/env python3
"""Verify the bb-merge-base Rust tool against `git merge-base` (the ground
truth) over every unique (fromRef,toRef) commit pair of the scrape's PRs.

The expected side uses the same parallel `git merge-base` subprocess approach
as emitter._merge_bases; the actual side pipes all pairs through the Rust
binary in one batch. Any mismatch (or pair where one side finds a base and the
other doesn't) is reported as a failure.

Usage:
    python3 corpus/verify_merge_base.py --scrape scrape/SX [--bin PATH]
        [--jobs 16] [--limit N] [--pairs FILE]
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bb_archiver.emitter import _log


def find_pr_file(scrape):
    hits = glob.glob(str(Path(scrape) / "rest" / "pull-requests_*.json"))
    if not hits:
        raise SystemExit(f"no pull-requests_*.json under {scrape}/rest")
    return hits[0]


def collect_pairs(scrape):
    pr_file = find_pr_file(scrape)
    prs = json.load(open(pr_file))
    pairs = []
    for pr in prs:
        try:
            f = pr["fromRef"]["latestCommit"]
            t = pr["toRef"]["latestCommit"]
        except (KeyError, TypeError):
            continue
        pairs.append((f, t))
    # dedupe, preserve first-seen order for stable comparison
    seen = set()
    unique = []
    for p in pairs:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique, len(pairs)


def git_merge_base(gitdir, pair):
    try:
        out = subprocess.run(
            ["git", "merge-base", pair[0], pair[1]],
            capture_output=True, text=True, check=True, cwd=gitdir)
        return out.stdout.strip() or ""
    except Exception:
        return ""


def expected_parallel(gitdir, pairs, jobs):
    """Ground truth: git merge-base per unique pair, in parallel."""
    _log(f"expected: git merge-base for {len(pairs)} unique pairs ({jobs} workers)")
    results = {}
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(git_merge_base, gitdir, p): p for p in pairs}
        for fut in as_completed(futs):
            p = futs[fut]
            results[p] = fut.result()
            done += 1
            if done % 500 == 0 or done == len(pairs):
                el = time.time() - t0
                _log(f"expected: {done}/{len(pairs)} "
                     f"({done / max(el, 0.001):,.0f}/s, "
                     f"ETA {int((len(pairs) - done) / max(done / max(el, 0.001), 0.001))}s)")
    return results


def actual_rust(bin_path, gitdir, pairs):
    """bb-merge-base: one process, all pairs through stdin, results in order."""
    if not bin_path or not os.path.isfile(bin_path):
        raise SystemExit(f"bb-merge-base binary not found: {bin_path}")
    _log(f"actual: bb-merge-base {bin_path} (single batch)")
    payload = "".join(f"{f} {t}\n" for f, t in pairs)
    t0 = time.time()
    proc = subprocess.run([bin_path, gitdir], input=payload,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"bb-merge-base failed ({proc.returncode}): {proc.stderr}")
    lines = proc.stdout.splitlines()
    if len(lines) != len(pairs):
        raise SystemExit(f"bb-merge-base returned {len(lines)} lines for "
                         f"{len(pairs)} pairs")
    _log(f"actual: done in {time.time() - t0:.1f}s")
    return dict(zip(pairs, lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scrape", required=True, help="scrape dir with git mirror")
    ap.add_argument("--bin", default=None, help="bb-merge-base binary path "
                                                "(default: autodetect)")
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap unique pairs (test)")
    ap.add_argument("--pairs", default=None,
                    help="write all unique pairs to FILE")
    args = ap.parse_args()

    gitdir = Path(args.scrape) / "git"
    if not (gitdir / "objects").exists():
        raise SystemExit(f"no git mirror at {gitdir}")

    if args.bin is None:
        cands = [
            Path("tools/bb-merge-base/target/release/bb-merge-base"),
            Path("tools/bb-merge-base/dist/bb-merge-base-linux-x86_64"),
        ]
        args.bin = next((str(c) for c in cands if c.is_file()), None)
        if args.bin:
            _log(f"using autodetected bb-merge-base: {args.bin}")

    pairs, total = collect_pairs(args.scrape)
    if args.limit:
        pairs = pairs[: args.limit]
    _log(f"{total} PR pairs -> {len(pairs)} unique")
    if args.pairs:
        with open(args.pairs, "w") as f:
            for a, b in pairs:
                f.write(f"{a} {b}\n")

    exp = expected_parallel(str(gitdir), pairs, args.jobs)
    act = actual_rust(args.bin, str(gitdir), pairs)

    mismatches = []
    for p in pairs:
        e, a = exp[p], act[p]
        if e != a:
            mismatches.append((p, e, a))
    print("=== bb-merge-base vs git merge-base ===")
    if mismatches:
        print(f"MISMATCHES ({len(mismatches)}):")
        for p, e, a in mismatches[:20]:
            print(f"  {p[0]} {p[1]}: git={e or '<none>'} rust={a or '<none>'}")
        return 1
    print(f"ALL {len(pairs)} UNIQUE PAIRS MATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())