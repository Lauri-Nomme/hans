"""bb_archiver git-objects path: `repack -ad` -> single pack, complete object tar.

PR `improve/repack-no-force` dropped `-f` from the emitter's repack; these tests
lock the two invariants that make that safe: `-ad` still yields exactly one pack
(bb-obj-tar refuses more than one), and no object is lost.
"""
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from bb_archiver.emitter import Emitter  # noqa: E402

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}


def _bare_repo_with_commits():
    """A bare repo with a two-commit chain (6 reachable objects)."""
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "--bare", d], check=True)

    def commit(msg, content, parent=None):
        blob = subprocess.run(["git", "-C", d, "hash-object", "-w", "--stdin"],
                              input=content, capture_output=True, text=True,
                              env=_ENV).stdout.strip()
        tree = subprocess.run(["git", "-C", d, "mktree"],
                              input=f"100644 blob {blob}\tf\n",
                              capture_output=True, text=True, env=_ENV).stdout.strip()
        args = ["git", "-C", d, "commit-tree", "-m", msg, tree]
        if parent:
            args += ["-p", parent]
        return subprocess.run(args, capture_output=True, text=True, env=_ENV).stdout.strip()

    c1 = commit("c1", "one\n")
    c2 = commit("c2", "two\n", parent=c1)
    subprocess.run(["git", "-C", d, "update-ref", "refs/heads/main", c2], check=True)
    return d


def _object_set(repo):
    return set(subprocess.run(
        ["git", "-C", repo, "cat-file", "--batch-all-objects",
         "--batch-check=%(objectname)"],
        capture_output=True, text=True).stdout.split())


class RepackTest(unittest.TestCase):
    def test_repack_ad_yields_single_pack_and_preserves_objects(self):
        repo = _bare_repo_with_commits()
        before = _object_set(repo)
        subprocess.run(["git", "-C", repo, "repack", "-ad", "-q"], check=True)
        idxs = [f for f in os.listdir(os.path.join(repo, "objects", "pack"))
                if f.endswith(".idx")]
        self.assertEqual(len(idxs), 1, "repack -ad must leave exactly one pack")
        self.assertEqual(_object_set(repo), before, "no object may be lost")


class GitObjectsTest(unittest.TestCase):
    def _emitter(self, repo):
        parent = tempfile.mkdtemp()
        shutil.copytree(repo, os.path.join(parent, "git"))
        em = Emitter.__new__(Emitter)
        em.m = SimpleNamespace(dir=Path(parent))
        em.obj_tar_bin = None  # force the pure-Python path (no Rust binary)
        return em

    def test_git_objects_python_path_emits_every_object(self):
        repo = _bare_repo_with_commits()
        expected = _object_set(repo)
        em = self._emitter(repo)
        out = os.path.join(tempfile.mkdtemp(), "objects.atl.tar")
        em.git_objects(out)
        with tarfile.open(out) as t:
            names = {m.name.replace("/", "") for m in t.getmembers()}
        self.assertEqual(names, expected)


if __name__ == "__main__":
    unittest.main()
