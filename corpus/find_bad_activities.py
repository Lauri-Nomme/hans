#!/usr/bin/env python3
"""Find PR activity entries that emitter.pr_activities would crash on, so we
can fix emitter to handle real-world shapes without re-running the scrape.

Catches every hard access emitter makes:
  - any activity: user.slug / createdDate / action
  - MERGED:  commit.id            (the observed crash: MERGED without commit)
  - RESCOPED: fromHash / previousFromHash / previousToHash / toHash
COMMENTED-without-comment is skipped by emitter (safe), not reported.

Usage:
    python3 corpus/find_bad_activities.py scrape/SX
"""
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: find_bad_activities.py <scrape_dir>")
    rest = Path(sys.argv[1]) / "rest"
    files = sorted(rest.glob("pr_*_activities.json"))
    bad = 0
    for fp in files:
        pid = fp.name.removeprefix("pr_").removesuffix("_activities.json")
        try:
            acts = json.loads(fp.read_text())
        except Exception as e:
            print(f"[{pid}] UNREADABLE: {e}")
            bad += 1
            continue
        for i, a in enumerate(acts or []):
            problems = []
            if not isinstance(a, dict):
                problems.append("not an object")
                print(f"[{pid}] #{i} {problems}: {a!r}")
                bad += 1
                continue
            if "user" not in a or not isinstance(a.get("user"), dict):
                problems.append("missing user object")
            if "createdDate" not in a:
                problems.append("missing createdDate")
            action = a.get("action")
            if action == "MERGED":
                if "commit" not in a or not isinstance(a.get("commit"), dict):
                    problems.append("MERGED missing commit.id")
                elif "id" not in a["commit"]:
                    problems.append("MERGED commit missing id")
            elif action == "RESCOPED":
                for k in ("fromHash", "previousFromHash", "previousToHash",
                          "toHash"):
                    if k not in a:
                        problems.append(f"RESCOPED missing {k}")
            if problems:
                print(f"[{pid}] #{i} action={action} PROBLEMS: {problems}")
                print(json.dumps(a, indent=2)[:2000])
                bad += 1
    print(f"=== {bad} problematic activities across {len(files)} PR files ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())