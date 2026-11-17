"""`bb-archiver validate`: verdicts and run time on synthetic archives."""
import contextlib
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bb_archiver import cli  # noqa: E402

DASH = "—"
META = "x/repositories/1/metadata.atl.tar.atl.gz"
OBJS = "x/repositories/1/contents/objects.atl.tar"


def sha(i):
    return hashlib.sha1(str(i).encode()).hexdigest()


def pr_name(i):
    return f"x/repositories/1/pullRequests/{i}/p.json.atl.gz"


def add(tar, name, data):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    tar.addfile(ti, io.BytesIO(data))


def inner_tar(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for n, d in entries:
            add(t, n, d)
    return buf.getvalue()


def pr_json(commit):
    return gzip.compress(json.dumps({"fromRef": {"latestCommit": commit}}).encode())


def build(path, objs=range(50), refs=(("refs/heads/main", 5),), prs=((1, 7),),
          extra=(), meta=True, objects=True, meta_name=META, objs_name=OBJS):
    """Archive with object ids sha(i) for i in `objs`; `refs` is (name, i)
    pairs, `prs` (pr id, i) pairs naming the commit a PR json cites."""
    with tarfile.open(path, "w") as t:
        if meta:
            entries = [(n, sha(i).encode()) for n, i in refs] + [("other", b"zzz")]
            add(t, meta_name, gzip.compress(inner_tar(entries)))
        if objects:
            add(t, objs_name, inner_tar(
                [(f"{sha(i)[:2]}/{sha(i)[2:]}", b"x" * 40) for i in objs]))
        for pid, i in prs:
            add(t, pr_name(pid), pr_json(sha(i)))
        for name, data in extra:
            add(t, name, data)


class Args:
    def __init__(self, archive):
        self.archive = archive


def validate(path):
    """cmd_validate end to end: (exit code, problem lines it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.cmd_validate(Args(path))
    return rc, [ln[3:] for ln in out.getvalue().splitlines() if ln.startswith("   ")]


def run(path):
    return validate(path)[1]


class PresentObjects(unittest.TestCase):
    """_present_objects walks tar headers only: it must not read object
    bodies or keep a TarInfo per object."""

    class CountingIO(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.bytes_read = 0

        def read(self, n=-1):
            out = super().read(n)
            self.bytes_read += len(out)
            return out

    def objects_tar(self, count, body=20000):
        return inner_tar([(f"{sha(i)[:2]}/{sha(i)[2:]}", b"x" * body)
                          for i in range(count)])

    def test_returns_only_wanted_ids_that_are_present(self):
        f = io.BytesIO(self.objects_tar(30))
        got = cli._present_objects(f, {sha(3), sha(29), sha(999)})
        self.assertEqual(got, {sha(3), sha(29)})

    def test_no_wanted_ids_does_not_open_the_tar(self):
        self.assertEqual(cli._present_objects(None, set()), set())

    def bytes_read(self, data, wanted):
        f = self.CountingIO(data)
        cli._present_objects(f, wanted)
        return f.bytes_read

    def test_object_bodies_are_skipped_not_read(self):
        # What is read must not grow with the size of the object bodies
        # (compression probing in tarfile.open adds a fixed overhead).
        small = self.bytes_read(self.objects_tar(100, body=1000), {sha(-1)})
        large = self.bytes_read(self.objects_tar(100, body=200000), {sha(-1)})
        self.assertLess(large, small + 100 * 1024,
                        f"1 KB bodies read {small}, 200 KB bodies read {large}")

    def test_stops_once_every_wanted_id_was_seen(self):
        data = self.objects_tar(500, body=1000)
        first = self.bytes_read(data, {sha(0)})
        everything = self.bytes_read(data, {sha(-1)})
        self.assertLess(first, everything - 100 * 512)

    def test_no_tarinfo_is_kept_per_object(self):
        data = self.objects_tar(100)
        seen = []
        real_open = tarfile.open

        def spy(*a, **kw):
            t = real_open(*a, **kw)
            seen.append(t)
            return t

        with mock.patch.object(tarfile, "open", spy):
            cli._present_objects(io.BytesIO(data), {sha(-1)})
        self.assertTrue(seen)
        self.assertLessEqual(max(len(t.members) for t in seen), 1)


class ValidateVerdicts(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.path = os.path.join(self.d.name, "a.tar")

    def problems(self, **kw):
        build(self.path, **kw)
        return run(self.path)

    def test_good_archive(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(validate(self.path)[0], 0)

    def test_dangling_ref(self):
        self.assertEqual(self.problems(refs=(("refs/heads/main", 900),)), [
            f"repo 1: ref refs/heads/main -> missing object {sha(900)} "
            f"(not in objects.atl.tar)"])

    def test_dangling_pr_sha(self):
        self.assertEqual(self.problems(prs=((1, 7), (2, 901))), [
            f"repo 1: {pr_name(2)} references missing object {sha(901)} "
            f"(not in objects.atl.tar) {DASH} review thread / activity for "
            f"this commit will not import"])

    def test_dangling_ref_and_pr_keep_order(self):
        got = self.problems(refs=(("refs/heads/main", 900),), prs=((1, 901),))
        self.assertEqual(len(got), 2)
        self.assertIn("ref refs/heads/main", got[0])
        self.assertIn(pr_name(1), got[1])
        self.assertEqual(validate(self.path)[0], 1)

    def test_stash_refs_checked_other_entries_ignored(self):
        got = self.problems(refs=(("stash-refs/x", 902),))
        self.assertEqual(len(got), 1)
        self.assertIn("stash-refs/x", got[0])

    def test_missing_objects_tar(self):
        self.assertEqual(self.problems(objects=False), [
            "repo 1: missing metadata.atl.tar or objects.atl.tar in archive",
            f"repo 1: missing objects.atl.tar in archive"])

    def test_missing_metadata(self):
        self.assertEqual(self.problems(meta=False), [
            "repo 1: missing metadata.atl.tar or objects.atl.tar in archive"])

    def test_not_gzip_reported_first(self):
        got = self.problems(refs=(("refs/heads/main", 900),),
                            extra=[("x/repositories/1/junk.json.atl.gz", b"plain")])
        self.assertEqual(got[0], "x/repositories/1/junk.json.atl.gz: not gzip")
        self.assertEqual(len(got), 2)

    def test_unparseable_json(self):
        got = self.problems(extra=[(
            "x/repositories/1/pullRequests/9/bad.json.atl.gz",
            gzip.compress(b"{not json"))])
        self.assertEqual(got, [
            "repo 1: unparseable JSON "
            "x/repositories/1/pullRequests/9/bad.json.atl.gz"])

    def test_repository_without_repositories_path_skips_ref_check(self):
        # "/repository/1/..." (singular) is a repo too, but its refs are not
        # checked; PR json citations still are
        meta = gzip.compress(inner_tar([("refs/heads/main", sha(900).encode())]))
        objs = inner_tar([(f"{sha(1)[:2]}/{sha(1)[2:]}", b"x")])
        pr = "x/repository/1/pullRequests/1/p.json.atl.gz"
        got = self.problems(meta=False, objects=False, prs=(), extra=[
            ("x/repository/1/metadata.atl.tar.atl.gz", meta),
            ("x/repository/1/contents/objects.atl.tar", objs),
            (pr, pr_json(sha(906)))])
        self.assertEqual(got, [
            f"repo 1: {pr} references missing object {sha(906)} "
            f"(not in objects.atl.tar) {DASH} review thread / activity for "
            f"this commit will not import"])

    def test_sha_cited_twice_in_one_pr_reported_once(self):
        obj = {"a": sha(903), "b": [sha(903), {"c": sha(903)}]}
        got = self.problems(extra=[(
            "x/repositories/1/pullRequests/9/dup.json.atl.gz",
            gzip.compress(json.dumps(obj).encode()))])
        self.assertEqual(len(got), 1)

    def test_pullrequests_json_at_repo_level_is_scanned(self):
        obj = {"x": sha(904)}
        got = self.problems(extra=[(
            "x/repositories/1/pullrequests.json.atl.gz",
            gzip.compress(json.dumps(obj).encode()))])
        self.assertEqual(len(got), 1)
        self.assertIn("pullrequests.json.atl.gz", got[0])

    def test_two_repos_are_separate(self):
        build(self.path)
        with tarfile.open(self.path, "a") as t:
            add(t, "x/repositories/2/metadata.atl.tar.atl.gz",
                gzip.compress(inner_tar([("refs/heads/main", sha(905).encode())])))
            add(t, "x/repositories/2/contents/objects.atl.tar",
                inner_tar([(f"{sha(1)[:2]}/{sha(1)[2:]}", b"x")]))
        got = run(self.path)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("repo 2: ref refs/heads/main"))


if __name__ == "__main__":
    unittest.main()
