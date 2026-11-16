#!/usr/bin/env python3
"""Decide whether a full-suite run is trustworthy, from its ``--report``.

    python3 tools/ci_check_report.py REPORT.json \\
        --allowed-skips tools/ci_allowed_skips.txt \\
        --require luajit,lua5.2,msgfmt,mypy,mpv \\
        --expect-run test_renderer_lua.py --expect-run test_msgfmt.py \\
        [--summary "$GITHUB_STEP_SUMMARY"]

tools/run_tests_parallel.py already exits non-zero on a failed module or a
failed mypy gate. It cannot know which skips are acceptable on a given host,
and an exit status of 0 says nothing about whether the run that produced it
had the tools it was meant to have. This reads the JSON report and fails
(exit 1) unless ALL of these hold:

* no failures, errors or unexpected successes, and every module reported an
  outcome (a crashed or timed-out module counts as nothing, not as a pass);
* every collected test ran: nothing was lost to a setUpClass/setUpModule
  skip that the per-test skip list cannot show;
* the run declared every name in --require through JMS_REQUIRE (the report
  records it), so a dropped environment variable cannot pass for enforcement;
* the mypy gate said "ok" when mypy is required;
* every module named by --expect-run reported, and ran at least one test;
* every skip matches an entry in --allowed-skips. An unlisted skip FAILS:
  new skips are where coverage quietly disappears.

It writes a Markdown summary (totals, gate, every skip with its reason and
whether it was allowed) to --summary, or to $GITHUB_STEP_SUMMARY when that
is set, and prints the same verdict to stdout. A missing or unreadable
report is exit 2: the run did not finish, which is a failure too.
"""

import argparse
import json
import os
import re
import sys

COUNTS = ("run", "passed", "failures", "errors", "skipped",
          "expected_failures", "unexpected_successes")


class Rule:
    """One allowed skip: a module file and a regex for the reason."""

    def __init__(self, module, pattern, line):
        self.module = module
        self.pattern = pattern
        self.regex = re.compile(pattern)
        self.line = line
        self.used = 0

    def matches(self, module, reason):
        first = (reason.splitlines() or [""])[0]
        return module == self.module and self.regex.search(first) is not None


def load_rules(path):
    """Parse the allowlist: ``<module.py> <regex>`` per line, ``#`` comments.

    The regex is searched in the FIRST line of the skip reason, so a rule
    cannot be widened by text further down a long explanation."""
    rules = []
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[0].endswith(".py"):
                raise ValueError("%s:%d: expected '<module.py> <regex>', got %r"
                                 % (path, n, line))
            try:
                rules.append(Rule(parts[0], parts[1], n))
            except re.error as e:
                raise ValueError("%s:%d: bad regex %r: %s"
                                 % (path, n, parts[1], e))
    return rules


def check(report, rules, require=(), expect_run=()):
    """(problems, skip_rows) for ``report``. No problems means trustworthy."""
    problems = []
    t = report.get("totals", {})
    for key in ("failures", "errors", "unexpected_successes"):
        if t.get(key, 0):
            problems.append("%d %s" % (t[key], key.replace("_", " ")))
    if report.get("unreported"):
        problems.append("no outcome from %d modules (crashed or timed out): %s"
                        % (len(report["unreported"]),
                           " ".join(report["unreported"])))
    if t.get("not_run", 0):
        problems.append("%d collected tests never started (a setUpClass or "
                        "setUpModule skip or error)" % t["not_run"])

    declared = set(report.get("required", ()))
    missing = sorted(set(require) - declared)
    if missing:
        problems.append("the run did not declare JMS_REQUIRE=%s (it declared "
                        "%s): skips of those tools were not enforced"
                        % (",".join(missing),
                           ",".join(sorted(declared)) or "nothing"))
    if "mypy" in require and report.get("mypy_gate") != "ok":
        problems.append("mypy gate: %s" % report.get("mypy_gate", "absent"))

    by_module = {m["module"]: m for m in report.get("modules", ())}
    for mod in expect_run:
        rec = by_module.get(mod)
        if rec is None:
            problems.append("%s: expected to run, not in the report" % mod)
        elif not rec.get("reported"):
            problems.append("%s: expected to run, reported no outcome" % mod)
        elif not rec.get("run"):
            problems.append("%s: expected to run, ran 0 tests" % mod)

    rows = []
    for mod in sorted(by_module):
        for test, reason in by_module[mod].get("skips", ()):
            rule = next((r for r in rules if r.matches(mod, reason)), None)
            if rule is not None:
                rule.used += 1
            rows.append((mod, test, reason, rule))
    unexpected = [r for r in rows if r[3] is None]
    if unexpected:
        problems.append("%d skip%s not in the allowlist"
                        % (len(unexpected), "" if len(unexpected) == 1 else "s"))
    return problems, rows


def _first(reason, limit=200):
    line = (reason.splitlines() or [""])[0]
    return line if len(line) <= limit else line[:limit - 3] + "..."


def _md(text):
    return text.replace("|", "\\|").replace("\n", " ")


def summary_markdown(report, problems, rows, rules):
    t = report.get("totals", {})
    out = ["## Full test suite", ""]
    out.append("**%s**" % ("PASSED" if not problems else "FAILED"))
    out.append("")
    out.append("| collected | run | passed | failed | errors | skipped | "
               "expected failures | unexpected successes | not run |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    out.append("| " + " | ".join(str(t.get(k, 0)) for k in
                                  ("collected",) + COUNTS + ("not_run",)) + " |")
    out.append("")
    out.append("- mypy gate: `%s`" % report.get("mypy_gate", "absent"))
    out.append("- required (JMS_REQUIRE): `%s`"
               % (",".join(report.get("required", ())) or "nothing"))
    out.append("- %s modules, %s workers, %ss"
               % (len(report.get("modules", ())), report.get("workers", "?"),
                  report.get("seconds", "?")))
    if problems:
        out += ["", "### Problems", ""] + ["- " + _md(p) for p in problems]
    out += ["", "### Skips (%d)" % len(rows), ""]
    if rows:
        out.append("| allowed | module | test | reason |")
        out.append("|---|---|---|---|")
        for mod, test, reason, rule in rows:
            out.append("| %s | %s | %s | %s |"
                       % ("yes (line %d)" % rule.line if rule else "**NO**",
                          mod, _md(test), _md(_first(reason))))
    else:
        out.append("none")
    unused = [r for r in rules if not r.used]
    if unused:
        out += ["", "Allowlist entries not needed by this run (informational: "
                "most depend on which fonts the host has):", ""]
        out += ["- line %d: `%s` `%s`" % (r.line, r.module, r.pattern)
                for r in unused]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report")
    ap.add_argument("--allowed-skips", metavar="FILE", required=True)
    ap.add_argument("--require", default="",
                    help="comma/space-separated names the run must have "
                         "declared in JMS_REQUIRE")
    ap.add_argument("--expect-run", action="append", default=[],
                    metavar="MODULE", help="a test module file that must "
                                           "have run (repeatable)")
    ap.add_argument("--summary", metavar="PATH",
                    default=os.environ.get("GITHUB_STEP_SUMMARY"),
                    help="append the Markdown summary here "
                         "(default: $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)

    try:
        rules = load_rules(args.allowed_skips)
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    try:
        with open(args.report, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as e:
        text = ("no usable test report (%s): the run did not finish, so "
                "nothing it would have checked can be trusted" % e)
        print("error: " + text, file=sys.stderr)
        if args.summary:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write("## Full test suite\n\n**FAILED**: %s\n" % _md(text))
        return 2

    require = [n for n in re.split(r"[\s,]+", args.require) if n]
    problems, rows = check(report, rules, require, args.expect_run)
    md = summary_markdown(report, problems, rows, rules)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(md)

    for mod, test, reason, rule in rows:
        if rule is None:
            # A GitHub annotation, so the reason shows on the run page.
            print("::error title=Unexpected skip::%s %s: %s"
                  % (mod, test, _first(reason)))
    print(md)
    if problems:
        print("NOT TRUSTWORTHY: " + "; ".join(problems))
        return 1
    print("trustworthy: %d run, %d skipped (all allowed)"
          % (report["totals"].get("run", 0), len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
