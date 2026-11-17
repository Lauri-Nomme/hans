"""SHA-fetch behaviour of bb_archiver.scrape, with git faked out (no network)."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bb_archiver import scrape  # noqa: E402

GONE = b"fatal: remote error: upload-pack: not our ref "


class FakeGit:
    """Stands in for subprocess.run. Records every SHA-fetch invocation.

    `rules` maps a sha to a list of per-attempt results for single-SHA fetches:
    "ok", "gone" or "err". A batch containing any sha whose next result is not
    "ok" fails as a whole (like `git fetch origin <sha...>`). Unlisted shas
    always succeed.
    """

    def __init__(self, rules=None):
        self.rules = {k: list(v) for k, v in (rules or {}).items()}
        self.calls = []          # (flags, [shas]) per SHA-fetch
        self.lock = threading.Lock()

    def _peek(self, sha):
        queue = self.rules.get(sha)
        return queue[0] if queue else "ok"

    def _consume(self, sha):
        queue = self.rules.get(sha)
        if queue:
            queue.pop(0)

    def __call__(self, cmd, **kwargs):
        if "--no-write-fetch-head" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        shas = cmd[cmd.index("origin") + 1:]
        with self.lock:
            self.calls.append((cmd[1:cmd.index("fetch")], list(shas)))
            results = [self._peek(s) for s in shas]
            if len(shas) == 1:
                self._consume(shas[0])
        bad = [r for r in results if r != "ok"]
        if not bad:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        stderr = GONE if "gone" in bad else b"fatal: connection reset"
        return subprocess.CompletedProcess(cmd, 128, b"", stderr)

    def single_calls(self, sha):
        return [c for c in self.calls if c[1] == [sha]]


def sha(n):
    return "%040x" % n


class FetchBatchTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(scrape.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_batch(self, git, shas, **kw):
        with mock.patch.object(scrape.subprocess, "run", git):
            return scrape._fetch_objects_batch(".", [], shas, **kw)

    def test_all_fetched_in_one_call(self):
        git = FakeGit()
        shas = [sha(1), sha(2), sha(3)]
        self.assertEqual(self.run_batch(git, shas), shas)
        self.assertEqual(len(git.calls), 1)

    def test_sha_failing_once_is_retried_and_fetched(self):
        git = FakeGit({sha(2): ["err", "ok"]})
        got = self.run_batch(git, [sha(1), sha(2)])
        self.assertEqual(sorted(got), [sha(1), sha(2)])
        self.assertEqual(len(git.single_calls(sha(2))), 2)

    def test_other_failures_retry_a_bounded_number_of_times(self):
        git = FakeGit({sha(2): ["err"] * 10})
        got = self.run_batch(git, [sha(2)])
        self.assertEqual(got, [])
        # one batch attempt, then _SHA_FETCH_ATTEMPTS per-SHA attempts
        self.assertEqual(len(git.single_calls(sha(2))),
                         1 + scrape._SHA_FETCH_ATTEMPTS)

    def test_failure_is_logged_not_silently_dropped(self):
        git = FakeGit({sha(2): ["err"] * 10})
        with mock.patch.object(scrape, "_log") as log:
            self.run_batch(git, [sha(2)])
        self.assertTrue(any("could not be fetched" in str(c) for c in log.call_args_list))

    def test_large_failing_batch_is_split(self):
        shas = [sha(i) for i in range(1, 13)]
        git = FakeGit({sha(7): ["gone"] * 5})
        got = self.run_batch(git, shas, batch_limit=3)
        self.assertEqual(sorted(got), sorted(set(shas) - {sha(7)}))

    def test_gc_and_maintenance_are_disabled_for_every_fetch(self):
        git = FakeGit({sha(2): ["err", "ok"]})
        self.run_batch(git, [sha(1), sha(2)])
        self.assertGreater(len(git.calls), 1)
        for flags, _ in git.calls:
            self.assertIn("gc.auto=0", flags)
            self.assertIn("maintenance.auto=false", flags)


class FetchMirrorTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(scrape.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.git_dir = os.path.join(tmp.name, "mirror.git")
        os.makedirs(os.path.join(self.git_dir, "objects"))
        self.present = set()

    def fetch_mirror(self, git, needed, **kw):
        avail = self.present
        with mock.patch.object(scrape.subprocess, "run", git), \
                mock.patch.object(scrape, "_available_objects",
                                  lambda *a, **k: set(avail)), \
                mock.patch.object(scrape, "_current_refs", lambda *a, **k: {}), \
                mock.patch.object(scrape, "_write_refs_direct",
                                  lambda g, refs, git="git": len(refs)), \
                mock.patch.object(scrape, "_log"):
            return scrape.fetch_mirror(self.git_dir, "https://bb.example", "u",
                                       "p", "PRJ", "repo", None,
                                       needed_refs=needed, **kw)

    def needed(self, *nums):
        return [("refs/stash-refs/%d" % n, sha(n)) for n in nums]

    def test_default_is_sequential_in_sorted_batches_of_50(self):
        git = FakeGit()
        self.fetch_mirror(git, self.needed(*range(1, 121)))
        sizes = [len(shas) for _, shas in git.calls]
        self.assertEqual(sizes, [50, 50, 20])
        order = [s for _, shas in git.calls for s in shas]
        self.assertEqual(order, sorted(order))

    def test_batch_size_is_configurable(self):
        git = FakeGit()
        self.fetch_mirror(git, self.needed(*range(1, 11)), fetch_batch=4)
        self.assertEqual([len(shas) for _, shas in git.calls], [4, 4, 2])

    def test_parallel_workers_fetch_every_sha_exactly_once(self):
        git = FakeGit()
        nums = list(range(1, 201))
        self.fetch_mirror(git, self.needed(*nums), fetch_batch=7, sha_workers=4)
        asked = [s for _, shas in git.calls for s in shas]
        self.assertEqual(sorted(asked), sorted(sha(n) for n in nums))

    def test_present_objects_are_not_fetched(self):
        self.present = {sha(1)}
        git = FakeGit()
        self.fetch_mirror(git, self.needed(1, 2))
        asked = {s for _, shas in git.calls for s in shas}
        self.assertEqual(asked, {sha(2)})

    def test_defaults_match_previous_behaviour(self):
        import inspect
        params = inspect.signature(scrape.fetch_mirror).parameters
        self.assertEqual(params["fetch_batch"].default, 50)
        self.assertEqual(params["sha_workers"].default, 1)
        crawl = inspect.signature(scrape.crawl).parameters
        self.assertEqual(crawl["sha_batch"].default, 50)
        self.assertEqual(crawl["sha_workers"].default, 1)


if __name__ == "__main__":
    unittest.main()
