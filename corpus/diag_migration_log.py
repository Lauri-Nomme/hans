#!/usr/bin/env python3
"""Compare GEI (gh bbs2gh) migration warning logs across two runs.

Classifies every WARN/ERROR line by the failure category encoded in the
message and prints a side-by-side breakdown (old vs new + delta), so a
fixed archive's re-migration can be checked for which classes shrank.

Usage:
  python diag_migration_log.py OLD_LOG NEW_LOG [--csv]

Recognized failure categories (regex-based, order matters):
  - REVIEW_THREAD_MISSING_START_COMMIT_OID   missing anchor commit
  - UNABLE_TO_COMPARE                         file-level / uncomparable thread
  - LINE_NOT_FOUND_IN_DIFF                    line absent from GEI's diff
  - INVALID_REVIEW_THREAD
  - OUTSIDE_CONTEXT                           "placed outside of 10 lines of context"
  - SKIPPED_NO_VALID_THREADS                  "did not have any valid threads or comments"
  - MISSING_COMMITS                           "review comments could not be transformed due to missing commits"
  - FALLBACK_IMPORT_FAILED                    LoadIssueCommentJob ... could not be imported
  - OTHER                                     anything else (shown so a new class is never silently counted away)
"""
import argparse
import collections
import csv
import re
import sys

# (category, regex) — matched in order; first hit wins.
PATTERNS = [
    ("REVIEW_THREAD_MISSING_START_COMMIT_OID",
     re.compile(r"REVIEW_THREAD_MISSING_START_COMMIT_OID")),
    ("UNABLE_TO_COMPARE", re.compile(r"UNABLE_TO_COMPARE")),
    ("LINE_NOT_FOUND_IN_DIFF", re.compile(r"LINE_NOT_FOUND_IN_DIFF")),
    ("INVALID_REVIEW_THREAD", re.compile(r"INVALID_REVIEW_THREAD")),
    ("OUTSIDE_CONTEXT",
     re.compile(r"placed outside of 10 lines of context")),
    ("SKIPPED_NO_VALID_THREADS",
     re.compile(r"did not have any valid threads or comments")),
    ("MISSING_COMMITS",
     re.compile(r"could not be transformed due to missing commits")),
    ("FALLBACK_IMPORT_FAILED",
     re.compile(r"LoadIssueCommentJob.*could not be imported")),
]

_HAS_ERROR = re.compile(r"\b(ERROR|WARN)\b", re.IGNORECASE)


def classify(line):
    for cat, rx in PATTERNS:
        if rx.search(line):
            return cat
    return "OTHER"


def analyze(path):
    counts = collections.Counter()
    samples = collections.defaultdict(list)
    total_warn_err = 0
    seen = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if not _HAS_ERROR.search(ln):
                continue
            total_warn_err += 1
            cat = classify(ln)
            counts[cat] += 1
            # keep a short unique sample per category for triage
            key = (cat, ln.strip()[:200])
            if key not in seen:
                seen.add(key)
                samples[cat].append(ln.strip()[:200])
    return counts, samples, total_warn_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old_log")
    ap.add_argument("new_log")
    ap.add_argument("--csv", action="store_true",
                    help="emit CSV (old,new,delta,category) to stdout")
    args = ap.parse_args()

    o_counts, o_samp, o_total = analyze(args.old_log)
    n_counts, n_samp, n_total = analyze(args.new_log)

    cats = sorted(set(o_counts) | set(n_counts))
    if args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["category", "old", "new", "delta"])
        for c in cats:
            w.writerow([c, o_counts[c], n_counts[c], n_counts[c] - o_counts[c]])
        w.writerow(["TOTAL_WARN_ERROR", o_total, n_total, n_total - o_total])
        return

    print(f"{'category':<38} {'old':>8} {'new':>8} {'Δ':>8}")
    print("-" * 66)
    for c in cats:
        print(f"{c:<38} {o_counts[c]:>8} {n_counts[c]:>8} "
              f"{n_counts[c] - o_counts[c]:>+8}")
    print("-" * 66)
    print(f"{'TOTAL WARN/ERROR lines':<38} {o_total:>8} {n_total:>8} "
          f"{n_total - o_total:>+8}")
    print()

    for c in cats:
        if n_counts[c] > 0:
            print(f"[{c}] new-log samples:")
            for s in n_samp[c][:3]:
                print(f"    {s}")
        if o_counts[c] > 0 and n_counts[c] == 0:
            print(f"[{c}] RESOLVED (present in old, absent in new)")

    # categories appearing in NEW that weren't in OLD need attention
    for c in cats:
        if n_counts[c] > 0 and o_counts[c] == 0:
            print(f"[{c}] NEW category appeared in new run")


if __name__ == "__main__":
    main()