"""``tools/run_tests_parallel.py`` must run the same tests as discover.

The whole value of the parallel runner is that its result means what
``python3 -m unittest discover tests`` means. The way that quietly stops
being true is not a crash -- it is a module the runner never launches, which
looks exactly like a module whose tests all passed.

So this pins the one invariant a runner cannot check about itself: the set
of modules it would run is the set discover would collect. It does not run
anything (that would be the suite running itself), and it deliberately
compares FILENAMES rather than counts -- a count is the thing that silently
agrees when two modules are swapped for each other.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _runner():
    """``tools/run_tests_parallel.py`` as a module, without installing it.

    It lives in ``tools/`` rather than in a package, so there is nothing to
    import normally -- and adding it to the path permanently would put
    ``tools/`` ahead of the repo root, which is the exact shadowing hazard
    the runner's own docstring is about.
    """
    import importlib.util

    path = os.path.join(ROOT, "tools", "run_tests_parallel.py")
    spec = importlib.util.spec_from_file_location("_jms_par_runner", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ParallelRunnerCoversTheSuiteTest(unittest.TestCase):
    #: unittest's default. The runner matches on ``test_``; if a module ever
    #: lands that discover would take and the runner would not, that is a
    #: silently skipped module.
    DISCOVER_PATTERN = "test*.py"

    def _discovered(self):
        import fnmatch

        return {n for n in os.listdir(os.path.join(ROOT, "tests"))
                if fnmatch.fnmatch(n, self.DISCOVER_PATTERN)}

    def test_it_would_run_every_module_discover_collects(self):
        missing = self._discovered() - set(_runner()._modules())
        self.assertEqual(
            missing, set(),
            "tools/run_tests_parallel.py would skip these, and a skipped "
            "module reports as a passing one:\n  " + "\n  ".join(sorted(missing)))

    def test_it_would_not_invent_a_module(self):
        """The mirror image: a file the runner launches and discover does
        not is a worker that fails on an empty suite, or worse, runs
        something the real suite never does."""
        extra = set(_runner()._modules()) - self._discovered()
        self.assertEqual(extra, set(),
                         "not part of `discover tests`: %s" % sorted(extra))

    def test_the_module_list_is_not_empty(self):
        """Guard on the guard: both assertions above pass against a runner
        that collects nothing at all."""
        self.assertGreater(len(_runner()._modules()), 100)


def _mixed_suite():
    """One test of every outcome unittest knows, plus a class skipped in
    setUpClass -- whose two tests are never started at all."""

    class Mixed(unittest.TestCase):
        def test_passes(self):
            pass

        def test_passes_too(self):
            pass

        def test_fails(self):
            self.fail("boom")

        def test_errors(self):
            raise RuntimeError("bang")

        @unittest.skip("needs a frobnicator")
        def test_skipped(self):
            pass

        @unittest.expectedFailure
        def test_expected_failure(self):
            self.fail("known")

        @unittest.expectedFailure
        def test_unexpected_success(self):
            pass

    class SkippedClass(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise unittest.SkipTest("needs a frobnicator")

        def test_one(self):
            pass

        def test_two(self):
            pass

    loader = unittest.TestLoader()
    return unittest.TestSuite([loader.loadTestsFromTestCase(Mixed),
                               loader.loadTestsFromTestCase(SkippedClass)])


def _run(runner, suite):
    import io

    return unittest.TextTestRunner(
        stream=io.StringIO(), verbosity=0,
        resultclass=runner._counting_result()).run(suite)


class RunnerReportingTest(unittest.TestCase):
    """The summary must tell a skipped test from a passing one.

    RESULT (total, failures, errors) reads a skip exactly like a pass, so
    the 22 renderer tests that skip without a Lua interpreter were reported
    as part of a green suite on every machine that had none."""

    @classmethod
    def setUpClass(cls):
        cls.runner = _runner()

    def test_each_outcome_is_counted_separately(self):
        suite = _mixed_suite()
        outcome = self.runner._outcome(_run(self.runner, suite))
        self.assertEqual(suite.countTestCases(), 9)
        self.assertEqual(
            {k: outcome[k] for k in self.runner.COUNTS},
            {"run": 7, "passed": 2, "failures": 1, "errors": 1, "skipped": 2,
             "expected_failures": 1, "unexpected_successes": 1})
        self.assertEqual([r for _t, r in outcome["skips"]],
                         ["needs a frobnicator"] * 2)

    def test_skipped_tests_are_never_counted_as_passed(self):
        class OnlySkips(unittest.TestCase):
            @unittest.skip("no interpreter")
            def test_a(self):
                pass

            def test_b(self):
                self.skipTest("no interpreter")

        result = _run(self.runner,
                      unittest.TestLoader().loadTestsFromTestCase(OnlySkips))
        self.assertTrue(result.wasSuccessful())     # what RESULT sees
        outcome = self.runner._outcome(result)
        self.assertEqual((outcome["run"], outcome["passed"], outcome["skipped"]),
                         (2, 0, 2))

    def test_the_worker_lines_round_trip(self):
        suite = _mixed_suite()
        result = _run(self.runner, suite)
        lines = self.runner._report_lines(suite.countTestCases(), result)
        # RESULT keeps exactly four fields: tools/mutate_round.py unpacks it.
        self.assertEqual(lines[0].split(), ["JMS-RESULT", "9", "1", "1"])
        count, bad, reported, outcome = self.runner._parse_worker_output(
            "noise\n" + "\n".join(lines) + "\nPLAYER-TEARDOWN x\n")
        self.assertEqual((count, bad, reported), (9, 2, True))
        self.assertEqual(outcome, self.runner._outcome(result))

    def test_a_worker_that_never_reported_has_no_outcome(self):
        self.assertEqual(self.runner._parse_worker_output("Traceback ...\n"),
                         (0, 0, False, None))
        # A stray or malformed line from a test must not crash the parent.
        self.assertEqual(
            self.runner._parse_worker_output(
                "JMS-RESULT what\nJMS-OUTCOME {not json\n"),
            (0, 0, False, None))

    def test_the_summary_groups_skips_by_reason_and_module(self):
        def oc(run, passed, skips):
            return {"run": run, "passed": passed, "failures": 0, "errors": 0,
                    "skipped": len(skips), "expected_failures": 0,
                    "unexpected_successes": 0,
                    "skips": [["t%d" % i, r] for i, r in enumerate(skips)]}

        summary = self.runner._summarize([
            ("test_lua.py", 0, 3, oc(3, 0, ["no Lua"] * 3)),
            ("test_other.py", 0, 5, oc(5, 3, ["no Lua", "no msgfmt"])),
            ("test_class_skip.py", 0, 4, oc(1, 0, ["no msgfmt"])),
            ("test_crashed.py", 1, 7, None),
        ])
        t = summary["totals"]
        self.assertEqual((t["collected"], t["run"], t["passed"], t["skipped"],
                          t["not_run"]), (19, 9, 3, 6, 3))
        self.assertEqual(summary["unreported"], ["test_crashed.py"])
        self.assertEqual(summary["skips"], [
            {"reason": "no Lua", "count": 4,
             "modules": {"test_lua.py": 3, "test_other.py": 1}},
            {"reason": "no msgfmt", "count": 2,
             "modules": {"test_class_skip.py": 1, "test_other.py": 1}}])
        text = "\n".join(self.runner._format_summary(summary))
        self.assertIn("9 run -- 3 passed, 0 failed, 0 errors, 6 skipped", text)
        self.assertIn("      4  no Lua\n         in test_lua.py (3), "
                      "test_other.py (1)", text)
        self.assertIn("3 collected tests never started", text)
        self.assertIn("no outcome reported by 1 modules", text)
        self.assertIn("test_crashed.py", text)

    def test_a_run_without_skips_says_so(self):
        summary = self.runner._summarize([("test_a.py", 0, 1, {
            "run": 1, "passed": 1, "failures": 0, "errors": 0, "skipped": 0,
            "expected_failures": 0, "unexpected_successes": 0, "skips": []})])
        self.assertIn("skipped: none", self.runner._format_summary(summary))


if __name__ == "__main__":
    unittest.main()
