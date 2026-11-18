"""Emitter git-metadata existence check: _present_shas + _add_ref.

PR `improve/wanted-subset-refs` swapped the full-object-set membership check for
a wanted-subset batch-check; these tests lock its parse and drop/keep behavior.
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

from bb_archiver.emitter import Emitter  # noqa: E402


def _emitter_with_repo():
    """Emitter whose m.dir/git is a fresh bare repo containing one blob."""
    parent = tempfile.mkdtemp()
    repo = os.path.join(parent, "git")
    subprocess.run(["git", "init", "-q", "--bare", repo], check=True)
    blob = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"],
                          input="hello\n", capture_output=True, text=True).stdout.strip()
    em = Emitter.__new__(Emitter)
    em.m = SimpleNamespace(dir=Path(parent))
    return em, blob


class PresentShasTest(unittest.TestCase):
    def setUp(self):
        self.em, self.blob = _emitter_with_repo()

    def test_present_and_absent(self):
        absent = "0" * 40
        self.assertEqual(self.em._present_shas({self.blob, absent}), {self.blob})

    def test_invalid_sha_treated_absent(self):
        # non-hex input -> 'missing', never crashes the parse
        self.assertEqual(self.em._present_shas({"not-a-sha"}), set())

    def test_none_and_empty_filtered(self):
        # regression: a malformed dump's null/empty target must not crash
        # sorted(); it is treated as absent (dropped+warned by _add_ref)
        self.assertEqual(self.em._present_shas({None, "", self.blob}), {self.blob})

    def test_no_mirror_returns_none(self):
        em = Emitter.__new__(Emitter)
        em.m = SimpleNamespace(dir=Path(tempfile.mkdtemp()))  # no git/objects
        self.assertIsNone(em._present_shas({self.blob}))


class AddRefTest(unittest.TestCase):
    def _em(self):
        em = Emitter.__new__(Emitter)
        em.m = SimpleNamespace(warnings=[])
        return em

    @mock.patch("bb_archiver.emitter._log")
    def test_missing_object_dropped_and_warned(self, log):
        em = self._em()
        entries = {}
        em._add_ref(entries, "refs/heads/main", "b" * 40, "branch", {"a" * 40})
        self.assertNotIn("refs/heads/main", entries)
        self.assertTrue(any("dropping" in w for w in em.m.warnings))

    @mock.patch("bb_archiver.emitter._log")
    def test_present_object_written(self, log):
        em = self._em()
        entries = {}
        sha = "a" * 40
        em._add_ref(entries, "refs/heads/main", sha, "branch", {sha})
        self.assertEqual(entries["refs/heads/main"], sha.encode() + b"\n")

    @mock.patch("bb_archiver.emitter._log")
    def test_unavailable_mirror_keeps_ref(self, log):
        # present=None (mirror unavailable) -> keep the ref (old behavior)
        em = self._em()
        entries = {}
        sha = "a" * 40
        em._add_ref(entries, "refs/heads/main", sha, "branch", None)
        self.assertEqual(entries["refs/heads/main"], sha.encode() + b"\n")


if __name__ == "__main__":
    unittest.main()
