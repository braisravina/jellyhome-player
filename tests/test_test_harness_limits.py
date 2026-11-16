"""The test harness must not be able to fill a workstation's RAM.

Neither test imports jellyfin_mpv_shim, so neither opens an mpv window.
"""
# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_tests_parallel", os.path.join(ROOT, "tools", "run_tests_parallel.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DefaultJobsTest(unittest.TestCase):
    def test_default_workers_are_capped_whatever_the_cpu_count(self):
        runner = _load_runner()
        for cpus in (1, 2, 4, 12, 32, 128):
            with mock.patch.object(os, "cpu_count", return_value=cpus):
                self.assertTrue(2 <= runner._default_jobs() <= 3, cpus)


class LimitedRunTest(unittest.TestCase):
    def test_refuses_to_start_without_headroom(self):
        env = dict(os.environ, JH_MIN_AVAIL_MIB="99999999")
        r = subprocess.run(
            [os.path.join(ROOT, "tools", "limited_run.sh"), "--", "true"],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 75, r.stderr)
        self.assertIn("not starting", r.stderr)


def _fake_mpv(tmp):
    """A harmless process whose comm is `mpv` (a renamed copy of sleep)."""
    exe = os.path.join(tmp, "mpv")
    shutil.copy(shutil.which("sleep"), exe)
    return exe


def _is_ours(pid, exe):
    """Whether ``pid`` is still the process running ``exe``, our fake.

    Checked before every signal: a pid the fake no longer holds may already
    belong to something else, and that must never be killed."""
    try:
        return os.readlink("/proc/%d/exe" % pid) == os.path.realpath(exe)
    except OSError:
        return False


def _foreign_fake_mpv(test, seconds=30):
    """Start a fake mpv that is NOT a child of this process; its pid.

    Identified by the pid it reports itself, never by name: `pgrep -n mpv`
    took the newest process called mpv on the machine, which with parallel
    workers (CI runs two) can be another worker's real player. A shell
    writes its own pid and then execs the fake, so the pid is the fake's;
    setsid and the exiting outer shell leave it re-parented, as a player
    from another process would be. Cleanup is registered before the start,
    so a failed start or a failed assertion still kills it -- and only it.
    """
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    exe = _fake_mpv(tmp)
    pidfile = os.path.join(tmp, "pid")

    def kill():
        try:
            with open(pidfile, encoding="ascii") as fh:
                pid = int(fh.read())
        except (OSError, ValueError):
            return
        if _is_ours(pid, exe):
            os.kill(pid, 9)
    test.addCleanup(kill)

    subprocess.run(
        ["sh", "-c", "setsid sh -c 'echo $$ > \"$1\"; exec \"$2\" %d' "
                     "sh \"$0\" \"$1\" >/dev/null 2>&1 &" % seconds,
         pidfile, exe], check=True, timeout=10)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with open(pidfile, encoding="ascii") as fh:
                pid = int(fh.read())
        except (OSError, ValueError):
            pid = None
        if pid is not None and _is_ours(pid, exe):   # exec has happened
            return pid, exe
        time.sleep(0.01)
    test.fail("the fake mpv did not start within 5 s")


@unittest.skipUnless(os.path.isdir("/proc"), "needs /proc")
class WorkerTeardownTest(unittest.TestCase):
    def test_an_mpv_child_of_the_worker_does_not_outlive_teardown(self):
        # os._exit skips atexit, so before this a worker's external mpv was
        # orphaned. The helper must reap worker-owned mpv children, bounded.
        runner = _load_runner()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        child = subprocess.Popen([_fake_mpv(tmp), "60"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        t0 = time.time()
        _, signalled = runner._teardown_players(grace=3)
        self.assertGreaterEqual(signalled, 1)
        self.assertLess(time.time() - t0, 8)
        self.assertIsNotNone(child.wait(timeout=5))

    def test_a_process_that_is_not_ours_is_left_alone(self):
        runner = _load_runner()
        # Not a child of this process: started via a shell that exits.
        pid, exe = _foreign_fake_mpv(self)
        with open("/proc/%d/stat" % pid, encoding="ascii") as fh:
            ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        self.assertNotEqual(ppid, os.getpid())     # really not our child
        runner._teardown_players(grace=1)
        os.kill(pid, 0)   # still alive: raises if it was killed
        # ...and still the same process, not a reused pid.
        self.assertTrue(_is_ours(pid, exe))

    def test_the_fake_is_found_by_pid_even_beside_a_newer_mpv(self):
        """Another worker's mpv started just after ours must not be taken
        for ours: that is what the old `pgrep -n mpv` lookup did."""
        mine, my_exe = _foreign_fake_mpv(self)
        newer, newer_exe = _foreign_fake_mpv(self)
        self.assertNotEqual(mine, newer)
        self.assertTrue(_is_ours(mine, my_exe))
        self.assertFalse(_is_ours(mine, newer_exe))
        self.assertTrue(_is_ours(newer, newer_exe))
        with open("/proc/%d/comm" % newer, encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), "mpv")   # same name, other pid


@unittest.skipUnless(os.path.isdir("/proc"), "needs /proc")
class IntegrationEntryPointTeardownTest(unittest.TestCase):
    """`python -m tests._outcomes` is what every integration leg runs. A
    player a module leaves behind must not outlive it."""

    SRC = (
        "import subprocess, sys, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_leaves_an_mpv_behind(self):\n"
        "        p = subprocess.Popen([%r, '60'])\n"
        "        open(%r, 'w').write(str(p.pid))\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n")

    def _run(self, fail=False):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pidfile = os.path.join(tmp, "pid")
        src = self.SRC % (_fake_mpv(tmp), pidfile)
        if fail:
            src = src.replace("p = subprocess.Popen", "self.fail('boom'); p = subprocess.Popen")
        with open(os.path.join(tmp, "leaky_mod.py"), "w", encoding="utf-8") as fh:
            fh.write(src)
        env = dict(os.environ, PYTHONPATH=tmp + os.pathsep + ROOT)
        r = subprocess.run([sys.executable, "-m", "tests._outcomes", "leaky_mod"],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=60)
        return r, pidfile

    def test_a_player_left_behind_is_reaped_when_the_entry_point_exits(self):
        r, pidfile = self._run()
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        with open(pidfile, encoding="utf-8") as fh:
            pid = int(fh.read())
        def state():
            try:
                with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError:                      # gone, even from /proc
                return None, "gone"
            fields = raw.rsplit(")", 1)[1].split()
            return fields[0], "state=%s ppid=%s" % (fields[0], fields[1])

        # Poll: a signalled process takes a moment to finish dying, and the
        # check itself must not race its disappearance from /proc.
        deadline = time.time() + 3
        st, detail = state()
        while st not in (None, "Z") and time.time() < deadline:
            time.sleep(0.05)
            st, detail = state()
        self.assertIn(st, (None, "Z"),
                      "the test's mpv child survived the entry point: %s; "
                      "stderr tail: %r" % (detail, r.stderr[-300:]))
        self.assertIn("PLAYER-TEARDOWN", r.stderr)

    def test_a_failing_run_still_reports_failure(self):
        r, _ = self._run(fail=True)
        self.assertNotEqual(r.returncode, 0, "teardown swallowed the exit status")


_HAVE_SCOPES = (shutil.which("systemd-run") is not None
                and subprocess.run(["systemctl", "--user", "is-active",
                                    "default.target"],
                                   capture_output=True).returncode == 0)


@unittest.skipUnless(_HAVE_SCOPES, "needs a systemd user manager")
class ScopeMonitorTest(unittest.TestCase):
    def _run(self, script, **env):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = _fake_mpv(tmp)
        return subprocess.run(
            [os.path.join(ROOT, "tools", "limited_run.sh"), "-t", "30", "--",
             "bash", "-c", script.replace("MPV", fake)],
            env=dict(os.environ, **env), capture_output=True, text=True,
            timeout=60)

    def test_an_orphan_that_outlives_the_command_is_cleanup_failure(self):
        r = self._run("(setsid MPV 40 &); sleep 1", JH_MAX_MPV="2")
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertIn("cleanup=FAILED", r.stderr)

    def test_more_than_the_threshold_stops_the_scope(self):
        r = self._run("MPV 20 & MPV 20 & (setsid MPV 20 &); wait",
                      JH_MAX_MPV="2")
        self.assertEqual(r.returncode, 78, r.stderr)

    def test_a_clean_run_passes(self):
        r = self._run("MPV 2 & wait", JH_MAX_MPV="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cleanup=ok", r.stderr)


if __name__ == "__main__":
    unittest.main()
