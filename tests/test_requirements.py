"""``JMS_REQUIRE`` must turn a missing required tool into a failure.

tests/_requirements.py is what stops "the tool is missing, so skip" from
reading as a pass in CI. These tests pin both directions: with nothing
required, every skip stays a skip (a bare development machine still runs the
suite); with a requirement declared, the same absence FAILS -- and a
misspelled requirement is an error rather than a requirement silently
dropped. The end-to-end cases run real test modules in a subprocess with a
PATH that holds nothing, so the tool really is missing whatever this machine
has installed.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import _requirements as req

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(**over):
    env = dict(os.environ)
    env.pop(req.ENV, None)
    env.pop("JMS_LUA", None)
    env.update(over)
    return env


class ParseTest(unittest.TestCase):
    def test_unset_or_empty_requires_nothing(self):
        for value in (None, "", " ", ",, ,"):
            self.assertEqual(req.parse(value), frozenset(), repr(value))
        self.assertEqual(req.required({}), frozenset())

    def test_commas_and_whitespace_both_separate(self):
        self.assertEqual(req.parse("luajit, lua5.2 msgfmt,mypy\tmpv"),
                         frozenset(req.KNOWN))

    def test_a_misspelled_requirement_fails_with_a_suggestion(self):
        with self.assertRaises(req.RequirementError) as cm:
            req.parse("luajit,lua52")
        text = str(cm.exception)
        self.assertIn("'lua52'", text)
        self.assertIn("did you mean 'lua5.2'?", text)
        for name in req.KNOWN:
            self.assertIn(name, text)

    def test_every_unknown_name_is_reported(self):
        with self.assertRaises(req.RequirementError) as cm:
            req.parse("msgfnt LUAJIT nonsense")
        text = str(cm.exception)
        for bad in ("'msgfnt'", "'LUAJIT'", "'nonsense'"):
            self.assertIn(bad, text)
        self.assertIn("requirements:", text)

    def test_a_requirement_error_is_a_value_error(self):
        # tools/run_tests_parallel.py catches ValueError.
        self.assertTrue(issubclass(req.RequirementError, ValueError))

    def test_asking_about_an_unknown_name_is_a_bug(self):
        with self.assertRaises(req.RequirementError):
            req.is_required("lua")


def _outcome(case):
    result = unittest.TestResult()
    unittest.TestLoader().loadTestsFromTestCase(case).run(result)
    return result


class DecoratorTest(unittest.TestCase):
    def _case(self, available):
        class Case(unittest.TestCase):
            ran = []

            @req.unless_available("msgfmt", available, "no msgfmt here")
            def test_needs_it(self):
                Case.ran.append(1)

            def test_does_not(self):
                pass
        return Case

    def test_available_runs_the_test_whatever_is_required(self):
        for value in ("", "msgfmt"):
            with mock.patch.dict(os.environ, {req.ENV: value}):
                case = self._case(True)
                result = _outcome(case)
            self.assertTrue(result.wasSuccessful())
            self.assertEqual((result.skipped, case.ran), ([], [1]))

    def test_missing_and_not_required_skips(self):
        with mock.patch.dict(os.environ, {req.ENV: "luajit"}):
            result = _outcome(self._case(False))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual([r for _t, r in result.skipped], ["no msgfmt here"])

    def test_missing_and_required_fails(self):
        with mock.patch.dict(os.environ, {req.ENV: "msgfmt"}):
            case = self._case(False)
            result = _outcome(case)
        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("msgfmt is required (JMS_REQUIRE=msgfmt) but "
                      "unavailable: no msgfmt here", result.failures[0][1])
        self.assertEqual(case.ran, [])

    def test_a_required_class_fails_every_test_without_setting_up(self):
        with mock.patch.dict(os.environ, {req.ENV: "mpv"}):
            @req.unless_available("mpv", False, "no mpv")
            class Case(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise RuntimeError("needs the tool")

                def test_a(self):
                    pass

                def test_b(self):
                    pass

            result = _outcome(Case)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(sorted(t.id().rsplit(".", 1)[1]
                                for t, _m in result.failures),
                         ["test_a", "test_b"])

    def test_skip_or_fail_both_ways(self):
        class Case(unittest.TestCase):
            def test_it(self):
                req.skip_or_fail(self, "mpv", "mpv is not on PATH")

        with mock.patch.dict(os.environ, {req.ENV: ""}):
            result = _outcome(Case)
        self.assertEqual(len(result.skipped), 1)
        with mock.patch.dict(os.environ, {req.ENV: "mpv"}):
            result = _outcome(Case)
        self.assertEqual((len(result.skipped), len(result.failures)), (0, 1))


def _runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_jms_par_runner_req", os.path.join(ROOT, "tools", "run_tests_parallel.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RunnerEnforcementTest(unittest.TestCase):
    """The runner validates JMS_REQUIRE first and owns the mypy gate."""

    def _main(self, value, gate=None):
        runner = _runner()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {req.ENV: value}), \
                mock.patch.object(sys, "argv", ["run_tests_parallel.py", "-q"]), \
                mock.patch.object(runner, "_modules", return_value=[]), \
                mock.patch.object(runner, "_start_mypy_gate",
                                  return_value=gate), \
                mock.patch.object(runner.subprocess, "Popen") as popen, \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            rc = runner.main()
        return rc, out.getvalue(), err.getvalue(), popen

    def test_an_unknown_requirement_stops_the_run_before_it_starts(self):
        rc, _out, err, popen = self._main("luajit,mypi")
        self.assertEqual(rc, 2)
        self.assertIn("'mypi' (did you mean 'mypy'?)", err)
        popen.assert_not_called()

    def test_a_required_mypy_that_cannot_run_fails_the_run(self):
        rc, out, _err, _p = self._main("mypy")
        self.assertEqual(rc, 1)
        self.assertIn("mypy gate: FAILED -- required", out)
        self.assertIn("required (JMS_REQUIRE): mypy", out)

    def test_an_unrequired_mypy_still_only_skips(self):
        rc, out, _err, _p = self._main("")
        self.assertEqual(rc, 0)
        self.assertIn("mypy gate: SKIPPED", out)
        self.assertNotIn("required (JMS_REQUIRE)", out)


class EndToEndTest(unittest.TestCase):
    """Real modules, run with a PATH that holds nothing (or only fakes)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.empty_path = self.tmp.name

    def _unittest(self, target, **env):
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", target],
            cwd=ROOT, env=_env(**dict({"PATH": self.empty_path}, **env)),
            capture_output=True, text=True, timeout=180)
        return proc.returncode, proc.stderr

    @staticmethod
    def _counts(log):
        m = re.search(r"^Ran (\d+) tests?", log, re.M)
        tail = log[log.rfind("\nRan "):]
        def n(word):
            hit = re.search(r"%s=(\d+)" % word, tail)
            return int(hit.group(1)) if hit else 0
        return int(m.group(1)) if m else 0, n("failures"), n("errors"), n("skipped")

    def test_msgfmt_skips_when_not_required_and_fails_when_required(self):
        target = "tests.test_msgfmt.TestAgainstGnuMsgfmt"
        rc, log = self._unittest(target)
        self.assertEqual(rc, 0, log)
        self.assertEqual(self._counts(log), (2, 0, 0, 2))
        rc, log = self._unittest(target, JMS_REQUIRE="msgfmt")
        self.assertNotEqual(rc, 0, log)
        self.assertEqual(self._counts(log), (2, 2, 0, 0))
        self.assertIn("msgfmt is required", log)

    def test_mpv_option_checks_fail_when_mpv_is_required(self):
        target = "tests.test_mpv_stat_properties"
        rc, log = self._unittest(target)
        self.assertEqual(rc, 0, log)
        self.assertEqual(self._counts(log)[3], 1)
        rc, log = self._unittest(target, JMS_REQUIRE="mpv")
        self.assertNotEqual(rc, 0, log)
        self.assertEqual(self._counts(log)[1:], (1, 0, 0))
        self.assertIn("mpv is required", log)

    def test_the_renderer_skips_only_when_nothing_asked_for_lua(self):
        rc, log = self._unittest("tests.test_renderer_lua")
        self.assertEqual(rc, 0, log)
        ran, fails, errs, skipped = self._counts(log)
        self.assertEqual((fails, errs), (0, 0))
        self.assertGreater(skipped, 20)
        self.assertIn("no Lua interpreter", log)

    def test_required_interpreters_each_fail_every_renderer_test(self):
        rc, log = self._unittest("tests.test_renderer_lua",
                                 JMS_REQUIRE="luajit,lua5.2")
        self.assertNotEqual(rc, 0, log)
        ran, fails, errs, skipped = self._counts(log)
        self.assertEqual((errs, skipped), (0, 0), log[-3000:])
        self.assertGreater(fails, 40)       # the whole suite, twice
        self.assertIn("TestRendererLua_luajit", log)
        self.assertIn("TestRendererLua_lua5_2", log)
        self.assertIn("luajit is required for the renderer tests", log)
        self.assertIn("lua5.2 is required for the renderer tests", log)

    def test_a_misspelled_requirement_breaks_the_module(self):
        rc, log = self._unittest("tests.test_renderer_lua",
                                 JMS_REQUIRE="lua52")
        self.assertNotEqual(rc, 0, log)
        self.assertIn("did you mean 'lua5.2'?", log)

    def test_an_interpreter_that_is_not_what_its_name_says_fails(self):
        """A `luajit` that is really PUC Lua 5.4 must not pass for LuaJIT.

        The fake answers every `-e` probe like a 5.4 interpreter would."""
        fake = os.path.join(self.empty_path, "luajit")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nprintf 'Lua 5.4|'\n")
        os.chmod(fake, 0o755)
        rc, log = self._unittest(
            "tests.test_renderer_lua.TestRendererLua_luajit"
            ".test_the_interpreter_is_the_one_it_claims_to_be",
            JMS_LUA="luajit", PATH=self.empty_path + os.pathsep + "/bin")
        self.assertNotEqual(rc, 0, log)
        self.assertIn("is not LuaJIT", log)


if os.name == "nt":       # the fakes are /bin/sh scripts; PATH games differ
    EndToEndTest = unittest.skip("POSIX-only end-to-end checks")(EndToEndTest)


if __name__ == "__main__":
    unittest.main()
