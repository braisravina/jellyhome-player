"""Optional test dependencies that a run can declare REQUIRED.

Several modules skip when a tool is missing -- no Lua interpreter, no GNU
msgfmt, no mpv on PATH -- because a bare development machine must still be
able to run the suite. A skip is the right answer there and the wrong one in
CI: the 22 renderer tests skipped on every machine without Lua, and the run
read OK.

    JMS_REQUIRE=luajit,lua5.2,msgfmt,mypy,mpv  python3 tools/run_tests_parallel.py

Each name listed turns "missing, so skip" into "missing, so FAIL" for the
tests that need it. Unset or empty, nothing changes: every skip stays a skip.
An unknown or misspelled name is an error, never ignored -- a typo that
silently dropped a requirement would make the run vouch for tests it never
ran.

Enforcement only. How skips are COUNTED and reported is the runner's
business (tools/run_tests_parallel.py); this module never decides whether a
test passed.
"""

import difflib
import os
import re
import unittest

ENV = "JMS_REQUIRE"

#: Every requirement a run may name, and what it stands for.
KNOWN = {
    "luajit": "the LuaJIT interpreter, which upstream mpv builds embed",
    "lua5.2": "the Lua 5.2 interpreter, which Debian 13's libmpv embeds",
    "msgfmt": "GNU msgfmt, the reference for the gettext catalog checks",
    "mypy": "mypy, which tools/mypy_gate.sh needs",
    "mpv": "an mpv executable on PATH, for the option and property checks",
}


class RequirementError(ValueError):
    """JMS_REQUIRE names something this suite does not know."""


def parse(value):
    """The set of requirements in ``value`` (commas and/or whitespace).

    Raises RequirementError, naming every unknown entry and the nearest
    known one, rather than dropping what it does not recognise."""
    names = [n for n in re.split(r"[\s,]+", value or "") if n]
    unknown = [n for n in names if n not in KNOWN]
    if unknown:
        hints = []
        for n in unknown:
            close = difflib.get_close_matches(n, KNOWN, n=1)
            hints.append("%r%s" % (n, " (did you mean %r?)" % close[0]
                                   if close else ""))
        raise RequirementError(
            "%s names unknown requirement%s: %s. Known: %s."
            % (ENV, "s" if len(unknown) > 1 else "", ", ".join(hints),
               ", ".join(sorted(KNOWN))))
    return frozenset(names)


def required(environ=None):
    """The requirements this run declares."""
    env = os.environ if environ is None else environ
    return parse(env.get(ENV, ""))


def is_required(name, environ=None):
    if name not in KNOWN:
        # A test asking about a name nobody can declare is a bug in the
        # test, and would make the requirement impossible to enforce.
        raise RequirementError("no such requirement: %r" % (name,))
    return name in required(environ)


def message(name, reason):
    """The failure text for a required dependency that is missing."""
    return ("%s is required (%s=%s) but unavailable: %s. It is %s."
            % (name, ENV, os.environ.get(ENV, ""), reason, KNOWN[name]))


def skip_or_fail(test, name, reason):
    """Inside a test: skip with ``reason``, or fail if ``name`` is required.

    For the checks that only discover at run time that the tool is missing."""
    if is_required(name):
        test.fail(message(name, reason))
    test.skipTest(reason)


def unless_available(name, available, reason):
    """Decorator for a test function or TestCase class that needs ``name``.

    Available: the test is untouched. Missing and not required: skipped
    with ``reason``, exactly like ``unittest.skipIf``. Missing and required:
    every test it covers FAILS with a message naming the requirement."""
    if available:
        return lambda obj: obj
    if not is_required(name):
        return unittest.skip(reason)
    return fail_all(message(name, reason))


def fail_all(text):
    """Decorator: every test the function or class covers fails with
    ``text``. For a dependency that a run asked for and does not have."""

    def failing(_self):
        raise AssertionError(text)

    def decorate(obj):
        if isinstance(obj, type):
            # dir(), not vars(): inherited tests (a mixin, as in
            # test_renderer_lua.py) must fail too, not run without the tool.
            for attr in dir(obj):
                if attr.startswith("test") and callable(getattr(obj, attr)):
                    setattr(obj, attr, _named(failing, attr))
            # setUpClass might itself need the tool; it must not turn the
            # failure into an error that hides which requirement it was.
            obj.setUpClass = classmethod(lambda cls: None)
            obj.tearDownClass = classmethod(lambda cls: None)
            return obj
        return _named(failing, obj.__name__)

    return decorate


def _named(fn, name):
    def wrapper(self):
        return fn(self)
    wrapper.__name__ = name
    return wrapper
