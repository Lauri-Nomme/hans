"""Gate 1 (corpus/gate1.py): verdict parity + the single-pass activity loader."""
import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "corpus"))

import gate1  # noqa: E402


def gz(data):
    return gzip.compress(data)


def inner_tar(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def add(tar, name, data):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    tar.addfile(ti, io.BytesIO(data))


def build(path, n_prs=10, pr_body=None):
    """A minimal valid archive: metadata + objects + n_prs activities members."""
    sha = "a" * 40
    with tarfile.open(path, "w") as t:
        add(t, "x/repositories/1/metadata/metadata.atl.tar.atl.gz",
            gz(inner_tar([("refs/heads/main", sha.encode() + b"\n")])))
        add(t, "x/repositories/1/contents/objects.atl.tar",
            inner_tar([(f"{sha[:2]}/{sha[2:]}", b"x")]))
        for i in range(1, n_prs + 1):
            body = pr_body if pr_body is not None else json.dumps(
                [{"kind": "ACTIVITY", "action": "OPENED", "createdTimestamp": i,
                  "userId": "a|A||NORMAL"}]).encode()
            add(t, f"x/repositories/1/pullrequest/{i}/activities.json.atl.gz",
                gz(body))


class CompareTest(unittest.TestCase):
    def test_self_compare_is_clean(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as f:
            build(f.name)
            r = gate1.Report()
            gate1.compare(f.name, f.name, r)
            self.assertEqual(r.genuine, [])

    def test_listing_diff_is_genuine(self):
        a = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        b = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        build(a.name, n_prs=3)
        build(b.name, n_prs=4)
        r = gate1.Report()
        gate1.compare(a.name, b.name, r)
        self.assertTrue(any("tar listings differ" in g for g in r.genuine))

    def test_content_diff_is_genuine(self):
        a = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        b = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        build(a.name, n_prs=3)
        build(b.name, n_prs=3,
              pr_body=json.dumps([{"kind": "ACTIVITY", "action": "DECLINED",
                                   "createdTimestamp": 1, "userId": "a|A||NORMAL"}]).encode())
        r = gate1.Report()
        gate1.compare(a.name, b.name, r)
        self.assertTrue(r.genuine)


class LoadActivitiesAllTest(unittest.TestCase):
    def test_duplicate_name_resolves_to_last(self):
        # the single-pass loader must match getmember(name)'s reverse-scan
        # (last occurrence wins) for a duplicated member name
        f = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        with tarfile.open(f.name, "w") as t:
            for body in (b'[{"first": 1}]', b'[{"second": 2}]'):
                add(t, "x/pullrequest/1/activities.json.atl.gz", gz(body))
        got = gate1.load_activities_all(f.name, (1,))
        self.assertEqual(got[1], [{"second": 2}])

    def test_only_wanted_pids_loaded(self):
        f = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        with tarfile.open(f.name, "w") as t:
            for i in range(1, 4):
                add(t, f"x/pullrequest/{i}/activities.json.atl.gz",
                    gz(json.dumps([{"pid": i}]).encode()))
        got = gate1.load_activities_all(f.name, (1, 3))
        self.assertEqual(set(got), {1, 3})

    def test_missing_pid_absent(self):
        f = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        with tarfile.open(f.name, "w") as t:
            add(t, "x/pullrequest/1/activities.json.atl.gz", gz(b"[]"))
        self.assertEqual(gate1.load_activities_all(f.name, (1, 99)), {1: []})


class ScalingTest(unittest.TestCase):
    def test_time_grows_linearly_with_entries(self):
        def timed(n):
            with tempfile.NamedTemporaryFile(suffix=".tar") as f:
                build(f.name, n_prs=n)
                r = gate1.Report()
                t0 = time.perf_counter()
                gate1.compare(f.name, f.name, r)
                return time.perf_counter() - t0

        small = timed(1000)
        large = timed(4000)
        self.assertLess(large, 8 * small + 0.3,
                        f"1000 entries {small:.2f}s, 4000 entries {large:.2f}s")


if __name__ == "__main__":
    unittest.main()
