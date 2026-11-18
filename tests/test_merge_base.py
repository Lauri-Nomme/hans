"""Emitter merge-base sharding: partition correctness + determinism.

PR `improve/merge-base-shard` splits pairs across parallel bb-merge-base
processes; these tests lock the shard partition (exactly-once coverage, worker
cap, empty guard, error propagation) and worker-count determinism.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from bb_archiver import emitter as emitter_mod  # noqa: E402
from bb_archiver.emitter import Emitter  # noqa: E402

_BIN = os.path.join(_ROOT, "tools", "bb-merge-base", "dist",
                    "bb-merge-base-linux-x86_64")
_ENV = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}


def _pairs(n):
    return [(f"{i:040x}", f"{i+1:040x}") for i in range(n)]


def _pair_for(pairs):
    return {p: i for i, p in enumerate(pairs)}


def _em():
    em = Emitter.__new__(Emitter)
    em.merge_base_bin = "bb-merge-base"
    em.m = SimpleNamespace(dir=Path(tempfile.mkdtemp()))
    return em


def _fake_run(calls):
    def run(cmd, **kw):
        payload = kw["input"]
        n = payload.count("\n")
        calls.append(payload)
        return subprocess.CompletedProcess(cmd, 0,
                                           stdout=("b" * 40 + "\n") * n, stderr="")
    return run


class ShardTest(unittest.TestCase):
    def test_shards_cover_all_pairs_exactly_once(self):
        pairs = _pairs(10)
        calls = []
        with mock.patch.object(emitter_mod.subprocess, "run",
                               side_effect=_fake_run(calls)):
            result = _em()._merge_bases_rust("/tmp/x", pairs, _pair_for(pairs), workers=3)
        seen = set()
        for payload in calls:
            for line in payload.splitlines():
                seen.add(tuple(line.split()))
        self.assertEqual(seen, set(pairs), "every pair fetched exactly once")
        self.assertEqual(len(calls), 3, "one subprocess per worker")
        self.assertEqual(set(result.values()), {"b" * 40})

    def test_workers_capped_at_n(self):
        pairs = _pairs(3)
        calls = []
        with mock.patch.object(emitter_mod.subprocess, "run",
                               side_effect=_fake_run(calls)):
            _em()._merge_bases_rust("/tmp/x", pairs, _pair_for(pairs), workers=8)
        self.assertEqual(len(calls), 3)

    def test_empty_pairs(self):
        calls = []
        with mock.patch.object(emitter_mod.subprocess, "run",
                               side_effect=_fake_run(calls)):
            result = _em()._merge_bases_rust("/tmp/x", [], {}, workers=4)
        self.assertEqual(result, {})
        self.assertEqual(calls, [])

    def test_error_propagates(self):
        pairs = _pairs(4)

        def failing(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        with mock.patch.object(emitter_mod.subprocess, "run", side_effect=failing):
            with self.assertRaises(RuntimeError):
                _em()._merge_bases_rust("/tmp/x", pairs, _pair_for(pairs), workers=2)

    def test_line_count_mismatch_raises(self):
        pairs = _pairs(4)

        def short(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="b" * 40 + "\n", stderr="")

        with mock.patch.object(emitter_mod.subprocess, "run", side_effect=short):
            with self.assertRaises(RuntimeError):
                _em()._merge_bases_rust("/tmp/x", pairs, _pair_for(pairs), workers=1)


def _chain_repo(n):
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "--bare", d], check=True)
    tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # empty tree
    shas, prev = [], None
    for i in range(n):
        args = ["git", "-C", d, "commit-tree", "-m", f"c{i}", tree]
        if prev:
            args += ["-p", prev]
        prev = subprocess.run(args, capture_output=True, text=True, env=_ENV).stdout.strip()
        shas.append(prev)
    subprocess.run(["git", "-C", d, "update-ref", "refs/heads/main", prev], check=True)
    return d, shas


@unittest.skipUnless(os.path.exists(_BIN), "bb-merge-base binary not built")
class DeterminismTest(unittest.TestCase):
    def test_workers_1_vs_n_identical(self):
        repo, shas = _chain_repo(40)
        pairs = [(shas[i], shas[-1]) for i in range(0, 40, 2)]
        pair_for = _pair_for(pairs)

        def run(workers):
            em = Emitter.__new__(Emitter)
            em.merge_base_bin = _BIN
            em.m = SimpleNamespace(dir=Path(tempfile.mkdtemp()))
            with mock.patch.object(emitter_mod, "_log", lambda *a, **k: None):
                return em._merge_bases_rust(repo, pairs, pair_for, workers=workers)

        self.assertEqual(run(1), run(4))


if __name__ == "__main__":
    unittest.main()
