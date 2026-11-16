"""The CI job's verdict: tools/ci_check_report.py and what feeds it.

The Linux full-test job (.github/workflows/tests.yml) is only as trustworthy
as the check on its report. These tests pin that check -- a clean run passes,
and each way a run can LOOK fine without being so fails -- and keep the
workflow, the skip allowlist and tests/_requirements.py from drifting apart:
a requirement the workflow forgot, an allowlist entry that would excuse a
missing tool, an expected module that no longer exists.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import contextlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest

from tests import _requirements

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "tests.yml")
ALLOWLIST = os.path.join(ROOT, "tools", "ci_allowed_skips.txt")
MYPY_LOCK = os.path.join(ROOT, "tools", "ci-mypy-cp313-amd64.lock")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checker = _load("_jms_ci_check_report", os.path.join("tools", "ci_check_report.py"))

CEILING = ("renderer.lua is AT the 200-local ceiling: the next file-scope "
           "local will break it.\nmore text")


def _report(**over):
    """A clean report from a run that required everything."""
    rep = {
        "totals": {"collected": 10, "run": 10, "passed": 9, "failures": 0,
                   "errors": 0, "skipped": 1, "expected_failures": 0,
                   "unexpected_successes": 0, "not_run": 0},
        "unreported": [],
        "mypy_gate": "ok",
        "required": sorted(_requirements.KNOWN),
        "workers": 2, "seconds": 1.0,
        "modules": [
            {"module": "test_renderer_lua.py", "reported": True, "run": 6,
             "skips": [["tests.test_renderer_lua.T.test_budget", CEILING]]},
            {"module": "test_msgfmt.py", "reported": True, "run": 4,
             "skips": []},
        ],
    }
    rep.update(over)
    return rep


class _Rules:
    def rules(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                          encoding="utf-8")
        self.addCleanup(os.unlink, tmp.name)
        with tmp:
            tmp.write(text)
        return checker.load_rules(tmp.name)


class AllowlistParsingTest(_Rules, unittest.TestCase):
    def test_comments_and_blank_lines_are_ignored(self):
        rules = self.rules("# c\n\ntest_a.py  ^x y$\n")
        self.assertEqual([(r.module, r.pattern, r.line) for r in rules],
                         [("test_a.py", "^x y$", 3)])

    def test_a_line_without_a_module_file_is_an_error(self):
        with self.assertRaises(ValueError):
            self.rules("just a reason\n")

    def test_a_bad_regex_is_an_error(self):
        with self.assertRaises(ValueError):
            self.rules("test_a.py  (unclosed\n")


class CheckTest(_Rules, unittest.TestCase):
    def setUp(self):
        self.allowed = self.rules(
            "test_renderer_lua.py  ^renderer\\.lua is AT the 200-local ceiling\n")
        self.req = sorted(_requirements.KNOWN)

    def _problems(self, report, expect_run=()):
        return checker.check(report, self.allowed, self.req, expect_run)[0]

    def test_a_clean_run_has_no_problems(self):
        problems, rows = checker.check(_report(), self.allowed, self.req,
                                       ["test_renderer_lua.py"])
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][3])

    def test_failures_errors_and_unexpected_successes_fail(self):
        for key in ("failures", "errors", "unexpected_successes"):
            rep = _report()
            rep["totals"][key] = 1
            self.assertEqual(len(self._problems(rep)), 1, key)

    def test_a_module_without_an_outcome_fails(self):
        problems = self._problems(_report(unreported=["test_hung.py"]))
        self.assertIn("test_hung.py", problems[0])

    def test_tests_that_never_started_fail(self):
        rep = _report()
        rep["totals"]["not_run"] = 3
        self.assertIn("3 collected tests never started", self._problems(rep)[0])

    def test_an_undeclared_requirement_fails(self):
        problems = self._problems(_report(required=["luajit"]))
        self.assertEqual(len(problems), 1)
        self.assertIn("did not declare JMS_REQUIRE=", problems[0])
        self.assertIn("lua5.2", problems[0])

    def test_a_report_without_requirements_fails(self):
        rep = _report()
        del rep["required"]
        self.assertIn("declared nothing", self._problems(rep)[0])

    def test_a_mypy_gate_that_did_not_pass_fails(self):
        for status in ("failed", "skipped", "missing"):
            self.assertEqual(self._problems(_report(mypy_gate=status)),
                             ["mypy gate: %s" % status])

    def test_an_expected_module_must_have_run(self):
        rep = _report()
        rep["modules"][1]["run"] = 0
        self.assertIn("ran 0 tests",
                      self._problems(rep, ["test_msgfmt.py"])[0])
        self.assertIn("not in the report",
                      self._problems(_report(), ["test_gone.py"])[0])
        rep = _report()
        rep["modules"][1]["reported"] = False
        self.assertIn("reported no outcome",
                      self._problems(rep, ["test_msgfmt.py"])[0])

    def test_an_unlisted_skip_fails(self):
        rep = _report()
        rep["modules"][1]["skips"] = [["t.x", "GNU msgfmt is not installed"]]
        self.assertEqual(self._problems(rep), ["1 skip not in the allowlist"])

    def test_a_rule_covers_only_its_own_module(self):
        rep = _report()
        rep["modules"][1]["skips"] = [["t.x", CEILING]]
        self.assertEqual(self._problems(rep), ["1 skip not in the allowlist"])

    def test_a_rule_matches_only_the_first_line_of_the_reason(self):
        rules = self.rules("test_msgfmt.py  more text\n")
        rep = _report()
        rep["modules"][1]["skips"] = [["t.x", "something else\nmore text"]]
        _problems, rows = checker.check(rep, rules, self.req)
        (row,) = [r for r in rows if r[0] == "test_msgfmt.py"]
        self.assertIsNone(row[3])


class MainTest(unittest.TestCase):
    def _main(self, report, *extra):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "r.json")
        if report is not None:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(report, fh)
        summary = os.path.join(tmp.name, "summary.md")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = checker.main([path, "--allowed-skips", ALLOWLIST,
                               "--require", ",".join(_requirements.KNOWN),
                               "--summary", summary] + list(extra))
        with open(summary, encoding="utf-8") as fh:
            return rc, out.getvalue(), fh.read()

    def test_a_clean_report_passes_and_is_summarised(self):
        rc, out, md = self._main(_report(), "--expect-run", "test_msgfmt.py")
        self.assertEqual(rc, 0, out)
        self.assertIn("**PASSED**", md)
        self.assertIn("| 10 | 10 | 9 | 0 | 0 | 1 |", md)
        self.assertIn("renderer.lua is AT the 200-local ceiling", md)
        self.assertIn("trustworthy", out)

    def test_an_unexpected_skip_fails_with_an_annotation(self):
        rep = _report()
        rep["modules"][1]["skips"] = [["t.x", "mpv is not on PATH"]]
        rc, out, md = self._main(rep)
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Unexpected skip::test_msgfmt.py t.x: "
                      "mpv is not on PATH", out)
        self.assertIn("**FAILED**", md)
        self.assertIn("| **NO** | test_msgfmt.py |", md)

    def test_a_missing_report_fails_and_says_so(self):
        rc, _out, md = self._main(None)
        self.assertEqual(rc, 2)
        self.assertIn("**FAILED**", md)
        self.assertIn("did not finish", md)


class RunnerReportTest(unittest.TestCase):
    def test_the_report_records_what_was_required(self):
        runner = _load("_jms_par_runner_ci", os.path.join("tools", "run_tests_parallel.py"))
        summary = runner._summarize([])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            runner._write_report(path, summary, [], "ok", 1, 0.0,
                                 frozenset({"mypy", "luajit"}))
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["required"], ["luajit", "mypy"])


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class WorkflowConsistencyTest(unittest.TestCase):
    """tests.yml, the allowlist and _requirements.py must agree.

    Plain text checks: the suite has no YAML parser, and these are the few
    lines whose drift would make the job vouch for less than it says."""

    @classmethod
    def setUpClass(cls):
        cls.wf = _read(WORKFLOW)

    def test_the_job_requires_every_known_tool(self):
        m = re.search(r"^\s+JMS_REQUIRE:\s*(\S+)\s*$", self.wf, re.M)
        self.assertIsNotNone(m, "tests.yml sets no JMS_REQUIRE")
        self.assertEqual(_requirements.parse(m.group(1)),
                         frozenset(_requirements.KNOWN))

    def _container_options(self):
        m = re.search(r"^\s+options:\s*(.+)$", self.wf, re.M)
        self.assertIsNotNone(m, "tests.yml gives the container no options")
        return m.group(1).split()

    def test_the_job_is_bounded(self):
        opts = self._container_options()
        self.assertIn("--memory=3g", opts)
        self.assertIn("--memory-swap=3g", opts)
        self.assertRegex(self.wf, r"linux-full-tests:\n(?:.*\n){0,6}?\s+timeout-minutes: \d+")
        self.assertRegex(self.wf, r"Run the full suite\n\s+timeout-minutes: \d+")

    def test_the_container_reaps_orphans(self):
        """--init: without an init that reaps, an orphan that exits stays a
        zombie, its process group still answers kill(-pgid, 0), and the
        integration runner's leak probe fails on a leak that is not there
        (CI run 43846807449). The fix belongs to the host, not the probe."""
        self.assertIn("--init", self._container_options())

    def test_debian_13_python_3_13_and_the_cp313_lock(self):
        self.assertIn("image: debian:13", self.wf)
        self.assertIn("linux/requirements-linux-cp313-amd64.lock", self.wf)
        self.assertTrue(os.path.exists(os.path.join(
            ROOT, "linux", "requirements-linux-cp313-amd64.lock")))

    def test_the_check_always_runs_and_reads_the_allowlist(self):
        self.assertRegex(self.wf, r"Check the report\n(?:\s+#.*\n)*\s+if: always\(\)")
        self.assertIn("--allowed-skips tools/ci_allowed_skips.txt", self.wf)
        self.assertIn('--require "$JMS_REQUIRE"', self.wf)

    def test_every_expected_module_exists(self):
        mods = re.findall(r"--expect-run (\S+)", self.wf)
        self.assertGreater(len(mods), 3)
        for mod in mods:
            self.assertTrue(os.path.exists(os.path.join(ROOT, "tests", mod)), mod)

    def test_the_full_suite_runs_in_one_job_only(self):
        main = _read(os.path.join(ROOT, ".github", "workflows", "main.yml"))
        self.assertNotIn("run_tests_parallel", main)
        self.assertEqual(self.wf.count("tools/run_tests_parallel.py"), 1)

    def test_the_mypy_lock_is_fully_pinned(self):
        lines = [l for l in _read(MYPY_LOCK).splitlines()
                 if l.strip() and not l.startswith("#")]
        self.assertTrue(any(l.startswith("mypy==") for l in lines))
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[^ ]+ (--hash=sha256:[0-9a-f]{64} ?)+$")


class AllowlistContentTest(unittest.TestCase):
    """The shipped allowlist: valid, pointing at real modules, and unable
    to excuse a missing required tool."""

    MISSING_TOOL_REASONS = [
        "GNU msgfmt is not installed",
        "mpv is not on PATH",
        "no Lua interpreter (tried: luajit, lua5.1, lua5.2, lua5.3, lua5.4, lua)",
        "mypy is not installed",
    ]

    @classmethod
    def setUpClass(cls):
        cls.rules = checker.load_rules(ALLOWLIST)

    def test_every_rule_names_an_existing_module(self):
        self.assertTrue(self.rules)
        for r in self.rules:
            self.assertTrue(os.path.exists(os.path.join(ROOT, "tests", r.module)),
                            "line %d: %s" % (r.line, r.module))

    def test_no_rule_excuses_a_missing_required_tool(self):
        for r in self.rules:
            for reason in self.MISSING_TOOL_REASONS:
                self.assertIsNone(r.regex.search(reason),
                                  "line %d would allow %r" % (r.line, reason))
            for name in _requirements.KNOWN:
                self.assertNotIn(name, r.pattern.replace("renderer\\.lua", ""),
                                 "line %d names required tool %r" % (r.line, name))

    def test_every_rule_matches_a_reason_the_suite_can_give(self):
        """A rule whose text appears in no test is dead, or a typo."""
        for r in self.rules:
            src = _read(os.path.join(ROOT, "tests", r.module))
            literal = re.sub(r"\\(.)", r"\1", r.pattern.strip("^$"))
            self.assertIn(literal[:30], src, "line %d" % r.line)


if __name__ == "__main__":
    unittest.main()
