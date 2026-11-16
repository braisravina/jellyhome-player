#!/usr/bin/env python3
"""Run the unit suite across processes, one process per test module.

    xvfb-run -a python3 tools/run_tests_parallel.py

Same tests as ``python3 -m unittest discover tests``, same import semantics,
in a fraction of the wall clock. Stdlib only, like the suite it runs.

**One xvfb for the whole run, not one per worker.** Put `xvfb-run -a` in
front of THIS script rather than around the workers: `-a` picks a free
display by probing, and thirty-two of them probing at once race for the same
number. The workers inherit `DISPLAY` and share one server. (They still need
one — importing `player.py` opens a real mpv window; see
`docs/testing.md`.)

Why a process per module rather than threads: the suite is full of
module-level singletons, `sys.modules` eviction and a real mpv per process.
Threads would share all of it.

Two things this has to get right, and both have cost a session before:

* **`sys.argv` is neutralised before anything under `jellyfin_mpv_shim` is
  imported.** Importing almost anything there reaches `args.get_args()` at
  import time, which parses the real argv and exits with the app's usage
  line. `discover tests` gets away with it because unittest rewrites argv
  first; a worker taking arguments of its own does not.
* **The repo root goes on `sys.path` explicitly.** A script run from
  `tools/` has `tools/` as `sys.path[0]`, so `jellyfin_mpv_shim` resolves to
  whatever is pip-installed in `~/.venv` — silently, and it *runs*, just
  against the previous release. That is the trap in CLAUDE.md's "run tests
  from the repo root" rule, reached from a direction the rule does not
  cover.

Selection is by `TestLoader.discover(start_dir, pattern=...)` rather than by
module name, so each worker imports the module exactly as `discover tests`
would -- as a top-level `test_foo`, with `tests/` on the path. Loading it as
`tests.test_foo` instead would give it a different identity in `sys.modules`
than the serial run gives it.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")


def _start_mypy_gate():
    """tools/mypy_gate.sh beside the suite: with nothing running it, the
    baseline drifted for two months unseen. None where it cannot run --
    Windows, where `bash` may be WSL's, or a machine without mypy."""
    if os.name == "nt" or not shutil.which("mypy"):
        return None
    return subprocess.Popen(
        ["bash", os.path.join(ROOT, "tools", "mypy_gate.sh")], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")


def _declared_requirements():
    """``JMS_REQUIRE`` as validated by tests/_requirements.py.

    Loaded by path rather than imported, for the same reason the tests
    load this file by path: putting the repo root ahead of `tools/` here
    is the shadowing hazard the module docstring is about. Raises
    ValueError (RequirementError) for an unknown name."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_jms_requirements", os.path.join(TESTS, "_requirements.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.required()


#: What a worker prints once its tests are done. The parent reads THIS, not
#: the exit status -- see _worker.
RESULT = "JMS-RESULT"

#: The full outcome, one JSON object on one line, printed beside RESULT.
#: RESULT keeps its four fields (total, failures, errors) because
#: tools/mutate_round.py and tools/run_modules_serial.sh read it as is.
#: This line is what tells a skipped test from a passing one: RESULT
#: counts both as "not a failure", which is how 22 renderer tests went
#: unrun on every machine without a Lua interpreter and still read OK.
OUTCOME = "JMS-OUTCOME"

#: Outcome counters, in report order. "passed" is counted from addSuccess,
#: never derived by subtraction, so nothing else can be mistaken for it.
COUNTS = ("run", "passed", "failures", "errors", "skipped",
          "expected_failures", "unexpected_successes")


def _counting_result():
    """A TextTestResult that also counts successes. Built lazily so that
    importing this module does not import unittest's runner for nothing."""
    import unittest

    class CountingResult(unittest.TextTestResult):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.successes = 0

        def addSuccess(self, test):
            super().addSuccess(test)
            self.successes += 1

    return CountingResult


def _outcome(result):
    """The JSON-able outcome of one module's run.

    ``skips`` holds every skip with its reason. A skip raised in
    setUpClass or setUpModule is recorded once, against the class or
    module, and the tests under it are not run at all -- so ``run`` can
    be lower than the module's test count, and the difference is reported
    as ``not_run`` by the parent.
    """
    def ident(test):
        try:
            return test.id()
        except Exception:
            return str(test)

    return {
        "run": result.testsRun,
        "passed": getattr(result, "successes", 0),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "skips": [[ident(t), str(reason)] for t, reason in result.skipped],
    }


def _report_lines(total, result):
    """The two lines a worker prints once its tests are done."""
    return ["%s %d %d %d" % (RESULT, total, len(result.failures),
                             len(result.errors)),
            "%s %s" % (OUTCOME, json.dumps(_outcome(result),
                                           separators=(",", ":")))]


def _parse_worker_output(out):
    """``(count, bad, reported, outcome)`` from one worker's output.

    ``reported`` is False when the RESULT line never arrived -- a crash,
    an import that exited, a kill -- and the module is then a failure
    however the process exited. ``outcome`` is None for a worker that
    printed RESULT but no OUTCOME (it cannot happen with this runner, but
    a module that writes a stray RESULT line of its own must not crash
    the parent)."""
    count, bad, reported, outcome = 0, 0, False, None
    for line in out.splitlines():
        if line.startswith(RESULT + " "):
            try:
                _tag, n, fails, errs = line.split()
                count, bad, reported = int(n), int(fails) + int(errs), True
            except ValueError:
                continue
        elif line.startswith(OUTCOME + " "):
            try:
                outcome = json.loads(line[len(OUTCOME) + 1:])
            except ValueError:
                continue
    return count, bad, reported, outcome


def _summarize(records):
    """Totals over every module, from ``(module, rc, count, outcome)``.

    A module without an outcome (it crashed, timed out or never
    reported) contributes nothing to the counters and is listed under
    ``unreported``: its tests were neither passed nor skipped, and adding
    its declared count to anything would claim otherwise."""
    totals = {k: 0 for k in COUNTS}
    totals["collected"] = 0
    totals["not_run"] = 0
    by_reason = {}          # reason -> {module: count}
    unreported = []
    for mod, _rc, count, outcome in records:
        totals["collected"] += count
        if outcome is None:
            unreported.append(mod)
            continue
        for k in COUNTS:
            totals[k] += int(outcome.get(k, 0))
        # A setUpClass skip covers tests that were never started; count
        # them so that collected = run + not_run + (unreported modules).
        totals["not_run"] += max(0, count - int(outcome.get("run", 0)))
        for _test, reason in outcome.get("skips", ()):
            mods = by_reason.setdefault(reason, {})
            mods[mod] = mods.get(mod, 0) + 1
    skips = sorted(
        ({"reason": r, "count": sum(m.values()), "modules": dict(sorted(m.items()))}
         for r, m in by_reason.items()),
        key=lambda s: (-s["count"], s["reason"]))
    return {"totals": totals, "skips": skips, "unreported": sorted(unreported)}


def _format_summary(summary):
    """The human-readable counter and skip lines for ``summary``."""
    t = summary["totals"]
    lines = ["results: %d run -- %d passed, %d failed, %d errors, %d skipped, "
             "%d expected failures, %d unexpected successes"
             % (t["run"], t["passed"], t["failures"], t["errors"],
                t["skipped"], t["expected_failures"],
                t["unexpected_successes"])]
    if t["not_run"]:
        lines.append("not run: %d collected tests never started (a "
                     "setUpClass/setUpModule skip or error)" % t["not_run"])
    if summary["unreported"]:
        lines.append("no outcome reported by %d modules (crashed or timed "
                     "out): %s" % (len(summary["unreported"]),
                                   " ".join(summary["unreported"])))
    if summary["skips"]:
        lines.append("skipped, by reason:")
        for s in summary["skips"]:
            reason = (s["reason"].splitlines() or [""])[0]
            if len(reason) > 160:
                reason = reason[:157] + "..."
            lines.append("  %5d  %s" % (s["count"], reason or "(no reason given)"))
            lines.append("         in " + ", ".join(
                "%s (%d)" % (m, n) for m, n in s["modules"].items()))
    else:
        lines.append("skipped: none")
    return lines


def _utf8(stream):
    """Make *stream* carry any test output at all.

    A redirected stdout on Windows is cp1252, and a test whose name or
    docstring holds a character outside it (this suite uses arrows and box
    drawing) makes `print` raise UnicodeEncodeError. In a worker that loses
    the module; in the parent it lost the entire run -- 18 modules in, at the
    first failure detail, with no summary and no traceback, because the
    traceback could not be printed either.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass                        # already wrapped, or not a text stream


def _worker(pattern):
    """Run one module's tests and report on stdout. Never returns."""
    _utf8(sys.stdout)
    _utf8(sys.stderr)
    # Before importing anything under jellyfin_mpv_shim -- see the module
    # docstring. Both lines are load-bearing.
    sys.argv = [sys.argv[0]]
    sys.path.insert(0, ROOT)

    import unittest

    suite = unittest.TestLoader().discover(TESTS, pattern=pattern)
    total = suite.countTestCases()
    result = unittest.TextTestRunner(
        verbosity=1, resultclass=_counting_result()).run(suite)
    for line in _report_lines(total, result):
        print(line)
    sys.stdout.flush()

    # os._exit, deliberately: skip interpreter teardown entirely.
    #
    # A module that imported player.py has a real libmpv in this process,
    # and tearing several dozen of those down at once -- which is what
    # running the suite in parallel does -- aborts with "pure virtual method
    # called" *after* the tests have passed. Both of the modules it hit are
    # green alone and green in the serial suite; the crash is in the exit
    # path, which is not what any of these tests are about.
    #
    # This is the reason the parent trusts the line above rather than the
    # exit status. A worker that dies BEFORE printing it is still a hard
    # failure -- that is a real crash mid-test, and it is reported as one.
    # Before the hard exit, because atexit will not run after it: the
    # tests' self-cleaning temp directories (tests/_tmpdirs) are registered
    # with atexit for a direct `unittest` run, and this path skips it. Left
    # out, each worker leaked its temp directories on every run -- which is
    # how /tmp reached five thousand entries.
    try:
        from tests import _tmpdirs

        _tmpdirs.cleanup_all()
    except Exception:
        pass

    # Same reason, different owner: the mpvtk scratch cache is product code
    # and cleans up via its own atexit hook, which this exit path skips too.
    # Only if the module was actually imported -- most workers never build a
    # browser and there is nothing to remove. Left out, a run leaked one
    # cache dir per worker that imported it, and on Windows nothing reclaims
    # those (rawimage._process_alive cannot tell a dead pid from a live one).
    rawimage = sys.modules.get("jellyfin_mpv_shim.mpvtk.rawimage")
    if rawimage is not None:
        try:
            rawimage.cleanup_this_process()
        except Exception:
            pass

    closed, signalled = _teardown_players()
    print("PLAYER-TEARDOWN handles_closed=%d children_signalled=%d "
          "test_modules_imported=%d backend=%s"
          % (closed, signalled,
             len([m for m in sys.modules if m.startswith("test_")]),
             "external" if getattr(sys.modules.get("jellyfin_mpv_shim.player"),
                                   "is_using_ext_mpv", None) else
             ("libmpv" if "jellyfin_mpv_shim.player" in sys.modules else "none")))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)


def _teardown_players(grace=5.0):
    """Guaranteed player teardown before the worker's ``os._exit``.

    ``os._exit`` skips atexit and interpreter teardown, so a player created
    at import time, in setUpModule/setUpClass, in a fixture or by backend
    initialisation was never closed: with the external-mpv backend (macOS,
    or no loadable libmpv) its ``mpv`` child outlived the worker as an
    orphan -- 25 of them in one serial run. Two layers, both bounded:

    1. in-process libmpv handles (``mpv.MPV``) and the external backend's
       process are terminated through their own API;
    2. any ``mpv`` process still parented to THIS worker (read from /proc by
       ppid, so it is provably ours) gets SIGTERM, then SIGKILL after
       ``grace``.

    Returns ``(handles_closed, children_signalled)``; the worker prints it so
    the orchestrator can tell a clean module from one this had to rescue.
    """
    closed = signalled = 0
    import gc
    import threading

    def close_handles():
        nonlocal closed
        for modname in ("mpv", "python_mpv_jsonipc"):
            mod = sys.modules.get(modname)
            cls = getattr(mod, "MPV", None) if mod else None
            if not isinstance(cls, type):
                continue
            for obj in [o for o in gc.get_objects() if isinstance(o, cls)]:
                try:
                    if modname == "mpv" and not getattr(obj, "handle", None):
                        continue
                    obj.terminate()
                    closed += 1
                except Exception as e:   # reported, never swallowed silently
                    print("PLAYER-TEARDOWN error: %s: %s"
                          % (type(e).__name__, e), file=sys.stderr)

    t = threading.Thread(target=close_handles, daemon=True)
    t.start()
    t.join(grace)   # a hung terminate() must not hang the worker

    if os.name == "posix" and os.path.isdir("/proc"):
        me = os.getpid()

        def mine():
            out = []
            for pid in filter(str.isdigit, os.listdir("/proc")):
                try:
                    with open("/proc/%s/stat" % pid, encoding="utf-8", errors="replace") as f:
                        stat = f.read()
                    comm = stat[stat.index("(") + 1:stat.rindex(")")]
                    fields = stat[stat.rindex(")") + 2:].split()
                    ppid = int(fields[1])
                except (OSError, ValueError, IndexError):
                    continue
                # A zombie is already dead: it only waits for a wait() this
                # process is about to skip. Counting it made the teardown
                # sit out its whole grace period twice (measured: 10 s).
                if fields[0] == "Z":
                    continue
                if ppid == me and comm.startswith("mpv"):
                    out.append(int(pid))
            return out

        kids = mine()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in kids:
                try:
                    os.kill(pid, sig)
                    if sig == signal.SIGTERM:
                        signalled += 1
                except OSError:
                    pass
            deadline = time.time() + grace
            while time.time() < deadline and mine():
                time.sleep(0.1)
            kids = mine()
            if not kids:
                break
    return closed, signalled


def _default_jobs():
    """Half the CPUs, not all of them, and that is measured rather than
    cautious.

    Every worker that imports `player.py` creates a real mpv window, and
    they all share one Xvfb, which is single-threaded. On this 32-CPU box
    `-j32` finished in 50s on an idle machine and **starved** on a busy one:
    four modules that take 12-17s were still unfinished at 90s. `-j16` is
    64s and has not wobbled. The extra 14s buys a result you can trust
    while something else is running, which is most of the time.

    Raise it with `-j` on an otherwise idle machine; the ceiling is the
    slowest single module (~43s here), because that is one process and
    nothing splits it.
    """
    # JellyHome: also capped at 3. Tuned on a 32-CPU box, "half the CPUs" is
    # 6 real-mpv processes on a 12-thread laptop; on a 16-20 GB daily
    # workstation that was enough to fill RAM. Wrap the run in
    # tools/limited_run.sh (one shared 3 GiB cgroup) and pass -j higher
    # deliberately on a machine you know can take it.
    return min(3, max(2, (os.cpu_count() or 4) // 2))


def _modules():
    """Every module `discover tests` would collect, biggest file first.

    Size is a rough proxy for cost, and it only has to be rough: workers
    pull from a queue, so a bad guess costs one slot for one module rather
    than unbalancing a fixed split. Starting the big ones first is what
    keeps the tail short.

    `tests/integration` and `tests/e2e` are NOT included, for the same
    reason `discover tests` does not reach them -- neither is a package, and
    they have their own runners.
    """
    names = [n for n in os.listdir(TESTS)
             if n.startswith("test_") and n.endswith(".py")]
    return sorted(names, key=lambda n: -os.path.getsize(os.path.join(TESTS, n)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-j", "--jobs", type=int, default=0,
                    help="worker processes (default: half the CPUs -- see "
                         "DEFAULT_JOBS)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only print the summary and any failures")
    ap.add_argument("-t", "--timeout", type=float, default=300.0,
                    help="seconds a single module may take before it is "
                         "killed and reported as failed (default: 300)")
    ap.add_argument("--report", metavar="PATH",
                    help="also write the totals, every skip with its reason, "
                         "and each module's outcome to PATH as JSON")
    ap.add_argument("--worker", metavar="PATTERN",
                    help=argparse.SUPPRESS)   # internal
    args = ap.parse_args()

    if args.worker:
        return _worker(args.worker)

    _utf8(sys.stdout)
    _utf8(sys.stderr)
    # Before launching anything: a misspelled requirement must stop the
    # run, not quietly require nothing.
    try:
        required = _declared_requirements()
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if required:
        print("required (JMS_REQUIRE): " + ", ".join(sorted(required)),
              flush=True)
    modules = _modules()
    jobs = args.jobs or _default_jobs()
    # os.name: Windows has a desktop and no DISPLAY, so the bare check told
    # every Windows run to go and find xvfb. Kept in step with
    # run_integration._have_display and _harness.HAVE_DISPLAY, which answer
    # the same question for the other two suites.
    if (os.name != "nt" and not os.environ.get("DISPLAY")
            and not os.environ.get("WAYLAND_DISPLAY")):
        print("warning: no DISPLAY. Importing player.py opens a real mpv "
              "window, so run this under `xvfb-run -a`.", file=sys.stderr)

    gate = _start_mypy_gate()
    pending = list(modules)
    running = {}          # Popen -> (module, started)
    done = []             # (module, rc, seconds, output, count, outcome)
    started = time.time()

    def launch():
        while pending and len(running) < jobs:
            mod = pending.pop(0)
            proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--worker", mod],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # Explicit, not text=True: that decodes with the locale
                # encoding, and cp1252 has undefined bytes -- so the parent
                # could fail to READ output the worker managed to write.
                text=True, encoding="utf-8", errors="replace",
                # Its own process group, so killing a worker takes the mpv
                # it started with it. A killed run that leaves mpv and Xvfb
                # children behind is how a machine accumulates a graveyard
                # of them across sessions -- one on this box outlived its
                # run by five days -- and the waste is what made a *serial*
                # suite look like it needed OOM headroom.
                start_new_session=True)
            # Drain the pipe NOW, in its own thread, rather than after the
            # worker exits. Nothing was reading it until then, so a worker
            # that outran the pipe buffer blocked in write() and never
            # exited -- and the parent, waiting for it to exit before
            # reading, waited out the full --timeout. Latent on Linux,
            # where the buffer is 64K and a passing module prints almost
            # nothing; reached on Windows, where one module's failure
            # tracebacks are enough. run_integration.py's pump() has always
            # done this.
            sink = []
            pump = threading.Thread(target=_drain, args=(proc, sink),
                                    daemon=True)
            pump.start()
            running[proc] = (mod, time.time(), sink, pump)

    def _drain(proc, sink):
        """Copy one worker's output into ``sink`` as it is produced."""
        try:
            for line in proc.stdout:
                sink.append(line)
        except (ValueError, OSError):
            pass                     # killed worker: the pipe went away
        finally:
            try:
                proc.stdout.close()
            except (ValueError, OSError):
                pass

    def reap(proc):
        """Kill a worker and everything it started."""
        if os.name != "posix":
            # No process groups to kill, and os.getpgid does not exist -- an
            # AttributeError here is not caught below and takes the runner
            # down instead of the worker it was asked to time out.
            proc.kill()
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    launch()
    while running:
        for proc in list(running):
            if proc.poll() is None:
                if time.time() - running[proc][1] > args.timeout:
                    # A module that hangs must not hang the run. This is a
                    # real hazard here rather than a theoretical one:
                    # importing player.py starts a real mpv, and enough of
                    # them racing for one X server can block in creation.
                    reap(proc)
                continue
            mod, t0, sink, pump = running.pop(proc)
            # The writer is gone, so the reader is at EOF or a breath away.
            pump.join(timeout=30)
            out = "".join(sink)
            count, bad, reported, outcome = _parse_worker_output(out)
            # No result line means the worker died before it could report:
            # a crash during the tests, an import that exited, a kill. That
            # is a failure however the process happened to exit.
            rc = 1 if (not reported or bad) else 0
            if not reported and time.time() - t0 >= args.timeout:
                out += ("\n*** killed after %.0fs (--timeout). It passes "
                        "alone; suspect contention for the X server or for "
                        "mpv, or a real hang inside the module.\n"
                        % args.timeout)
            done.append((mod, rc, time.time() - t0, out, count, outcome))
            if not args.quiet:
                skipped = (outcome or {}).get("skipped", 0)
                print("%-4s %5.1fs %4d  %s%s"
                      % ("FAIL" if rc else "ok",
                         time.time() - t0, count, mod,
                         "  (%d skipped)" % skipped if skipped else ""),
                      flush=True)
            launch()
        if running:
            time.sleep(0.02)

    elapsed = time.time() - started
    failed = [d for d in done if d[1]]
    total = sum(d[4] for d in done)
    slowest = sorted(done, key=lambda d: -d[2])[:5]

    for mod, _rc, secs, out, _n, _oc in failed:
        print("\n" + "=" * 70 + "\nFAILED: %s (%.1fs)\n" % (mod, secs) + "=" * 70)
        print(out.rstrip())

    summary = _summarize([(m, rc, n, oc) for m, rc, _s, _o, n, oc in done])
    print("\n%d tests in %d modules, %d workers, %.1fs wall clock"
          % (total, len(done), jobs, elapsed))
    print("slowest: " + ", ".join("%s %.1fs" % (m, s)
                                  for m, _r, s, *_rest in slowest))
    for line in _format_summary(summary):
        print(line)
    gate_failed = False
    if gate is None and "mypy" in required:
        gate_status = "missing"
        gate_failed = True
        print("mypy gate: FAILED -- required (JMS_REQUIRE) but cannot run: "
              "needs mypy on PATH, and not Windows")
    elif gate is None:
        gate_status = "skipped"
        print("mypy gate: SKIPPED (needs mypy, and not Windows)")
    else:
        out, _ = gate.communicate()
        gate_failed = gate.returncode != 0
        gate_status = "failed" if gate_failed else "ok"
        if gate_failed:
            print("\n" + "=" * 70 + "\nFAILED: tools/mypy_gate.sh\n"
                  + "=" * 70 + "\n" + out.rstrip())
        else:
            print("mypy gate: ok")
    if failed:
        print("FAILED modules: " + " ".join(sorted(m for m, *_ in failed)))
    if args.report:
        _write_report(args.report, summary, done, gate_status, jobs, elapsed)
    if failed or gate_failed:
        return 1
    skipped = summary["totals"]["skipped"]
    print("OK (%d skipped)" % skipped if skipped else "OK")
    return 0


def _write_report(path, summary, done, gate_status, jobs, elapsed):
    """``--report``: the summary plus one record per module, as JSON."""
    modules = []
    for mod, rc, secs, _out, count, outcome in sorted(done):
        rec = {"module": mod, "status": "failed" if rc else "ok",
               "seconds": round(secs, 1), "collected": count,
               "reported": outcome is not None}
        if outcome is not None:
            rec.update({k: outcome.get(k, 0) for k in COUNTS})
            rec["skips"] = outcome.get("skips", [])
        modules.append(rec)
    doc = dict(summary, mypy_gate=gate_status, workers=jobs,
               seconds=round(elapsed, 1), modules=modules)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    sys.exit(main())
