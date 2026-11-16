"""tools/mypy_gate.sh must compare findings the same way in every locale.

The gate's job is set arithmetic: fail on findings that are not in the
committed baseline, and only on those. It does it with `comm`, which needs
both inputs in one collation. The first CI run (43846807449) showed what
happens otherwise: the baseline had been sorted under en_GB.UTF-8, CI runs
under C.UTF-8, lines starting with a quote (`"None" has no attribute ...`)
order differently in the two, and ten findings that ARE in the baseline were
reported as new.

These run the real script against a fake `mypy` and a baseline of our own,
in a temporary copy of the layout it expects (it reads
tools/mypy-baseline.txt relative to itself), so they need no mypy and touch
nothing in the repository.
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
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "tools", "mypy_gate.sh")

# Four findings whose order DIFFERS between collations: byte order puts the
# quote (0x22) before every letter, while en_GB.UTF-8 ignores it and files
# `"None"` under N. This is that en_GB order -- the order the real baseline
# was stored in -- and therefore out of order for C.
LOCALE_ORDER = [
    'pkg/a.py: error: Item "None" of "Match[str] | None" has no attribute "groups"  [union-attr]',
    'pkg/a.py: error: "None" has no attribute "get"  [attr-defined]',
    'pkg/a.py: error: Need type annotation for "x"  [var-annotated]',
    'pkg/b.py: error: Argument 1 to "loads" has incompatible type "Any | None"  [arg-type]',
]
NEW = 'pkg/a.py: error: "None" has no attribute "jellyfin"  [attr-defined]'


def _locales():
    """C and C.UTF-8 always; the locale the baseline was sorted in, and
    en_US, when this host has them. A missing locale is not skipped: glibc
    would quietly fall back to C, which the first two already cover."""
    names = ["C", "C.UTF-8"]
    try:
        have = subprocess.run(["locale", "-a"], capture_output=True,
                              text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        have = []
    norm = {n.lower().replace("utf8", "utf-8"): n for n in have}
    for want in ("en_GB.UTF-8", "en_US.UTF-8"):
        if want.lower() in norm:
            names.append(norm[want.lower()])
    return names


@unittest.skipIf(os.name == "nt", "the gate is a bash script with comm and sort")
class MypyGateComparisonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp = tmp
        os.mkdir(os.path.join(tmp, "tools"))
        shutil.copy(GATE, os.path.join(tmp, "tools", "mypy_gate.sh"))
        self.bin = os.path.join(tmp, "bin")
        os.mkdir(self.bin)
        self.baseline = os.path.join(tmp, "tools", "mypy-baseline.txt")

    def _baseline(self, lines):
        with open(self.baseline, "w", encoding="utf-8") as fh:
            fh.write("".join(l + "\n" for l in lines))

    def _mypy(self, findings, rc=None):
        """A `mypy` that prints ``findings`` the way mypy does: with line
        numbers (which the gate strips), plus a note (which it ignores)."""
        out = os.path.join(self.tmp, "mypy-out.txt")
        with open(out, "w", encoding="utf-8") as fh:
            for n, f in enumerate(findings, 7):
                path, rest = f.split(": ", 1)
                fh.write("%s:%d: %s\n" % (path, n * 3, rest))
            fh.write('pkg/a.py:1: note: See https://mypy.readthedocs.io\n')
            fh.write("Found %d errors in 2 files\n" % len(findings))
        if rc is None:
            rc = 1 if findings else 0
        fake = os.path.join(self.bin, "mypy")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\ncat '%s'\nexit %d\n" % (out, rc))
        os.chmod(fake, 0o755)

    def _gate(self, locale, *args):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LC_") and k not in ("LANG", "LANGUAGE")}
        env.update(PATH=self.bin + os.pathsep + env.get("PATH", ""),
                   LANG=locale, LC_ALL=locale)
        proc = subprocess.run(
            ["bash", os.path.join(self.tmp, "tools", "mypy_gate.sh")] + list(args),
            capture_output=True, text=True, env=env, timeout=60)
        return proc.returncode, proc.stdout + proc.stderr

    def test_the_fixture_really_is_out_of_byte_order(self):
        # Otherwise every test below would pass without the fix too.
        self.assertNotEqual(LOCALE_ORDER, sorted(LOCALE_ORDER))

    def test_a_baseline_stored_in_any_order_matches_in_every_locale(self):
        orders = {"locale order": LOCALE_ORDER,
                  "reversed": list(reversed(sorted(LOCALE_ORDER))),
                  "byte order": sorted(LOCALE_ORDER)}
        for name, order in orders.items():
            self._baseline(order)
            # mypy reports them in yet another order, at other line numbers.
            self._mypy(LOCALE_ORDER[2:] + LOCALE_ORDER[:2])
            for loc in _locales():
                with self.subTest(baseline=name, locale=loc):
                    rc, out = self._gate(loc)
                    self.assertEqual(rc, 0, out)
                    self.assertIn("no new mypy findings", out)
                    self.assertNotIn("comm:", out)
                    self.assertNotIn("NEW mypy findings", out)

    def test_a_new_finding_is_reported_and_only_it(self):
        self._baseline(LOCALE_ORDER)
        self._mypy(LOCALE_ORDER + [NEW])
        for loc in _locales():
            with self.subTest(locale=loc):
                rc, out = self._gate(loc)
                self.assertEqual(rc, 1, out)
                self.assertNotIn("comm:", out)
                reported = [l.strip() for l in out.splitlines()
                            if l.startswith("  pkg/")]
                self.assertEqual(reported, [NEW])

    def test_a_finding_dropped_from_the_baseline_is_new_again(self):
        # The direction that matters most: a real finding is never hidden.
        for i, line in enumerate(LOCALE_ORDER):
            self._baseline(LOCALE_ORDER[:i] + LOCALE_ORDER[i + 1:])
            self._mypy(LOCALE_ORDER)
            with self.subTest(dropped=i):
                rc, out = self._gate("C.UTF-8")
                self.assertEqual(rc, 1, out)
                self.assertIn("  " + line, out)

    def test_a_fixed_finding_is_counted_not_failed(self):
        self._baseline(LOCALE_ORDER)
        self._mypy(LOCALE_ORDER[1:])
        for loc in _locales():
            with self.subTest(locale=loc):
                rc, out = self._gate(loc)
                self.assertEqual(rc, 0, out)
                self.assertIn("1 baseline finding(s) no longer occur.", out)

    def test_duplicate_baseline_lines_are_harmless(self):
        self._baseline(LOCALE_ORDER + LOCALE_ORDER[:2])
        self._mypy(LOCALE_ORDER)
        rc, out = self._gate("C.UTF-8")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("no longer occur", out)

    def test_update_writes_the_baseline_in_byte_order(self):
        self._baseline([])
        self._mypy(LOCALE_ORDER)
        rc, out = self._gate("en_GB.UTF-8", "--update")
        self.assertEqual(rc, 0, out)
        with open(self.baseline, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(), sorted(LOCALE_ORDER))

    def test_a_mypy_that_crashed_still_fails(self):
        # Unchanged behaviour, pinned: no `: error:` lines plus a non-zero
        # exit must not read as "every finding fixed".
        self._baseline(LOCALE_ORDER)
        self._mypy([], rc=2)
        rc, out = self._gate("C.UTF-8")
        self.assertEqual(rc, 1, out)
        self.assertIn("mypy did not run cleanly", out)


if __name__ == "__main__":
    unittest.main()
