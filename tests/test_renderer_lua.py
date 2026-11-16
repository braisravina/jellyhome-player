"""Run the Lua-side renderer tests as part of the normal suite.

renderer.lua holds state Python cannot see — scroll offsets, textbox edits,
focus — and until now had no tests at all: two protocol additions (the
textbox `commit` event, `follow` scroll containers) were written and shipped
against nothing but hand testing. tests/lua/ loads the real renderer against
a faked mpv and drives it through the real script-message boundary.

Skipped when no Lua interpreter is installed, which keeps a bare machine
able to run the suite. That skip used to be the only behaviour, and it is
how all 22 tests went unrun everywhere without ever failing -- including on
the machines that ship the renderer. Now:

* nothing requested: the first interpreter found runs everything, or the
  whole class skips (and the runner reports the skip, not a pass);
* ``JMS_REQUIRE=luajit,lua5.2`` (tests/_requirements.py) or
  ``JMS_LUA=lua5.2``: one class per named interpreter, each running every
  test, and each FAILING if its interpreter is missing or is not the
  dialect it claims to be. Debian 13's libmpv embeds Lua 5.2; upstream
  builds usually embed LuaJIT. CI requires both.
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
import re
import subprocess
import unittest

from tests import _requirements

ROOT =os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUA_DIR = os.path.join(ROOT, "tests", "lua")
RENDERER = os.path.join(ROOT, "jellyfin_mpv_shim", "mpvtk", "renderer.lua")
THUMBFAST = os.path.join(ROOT, "jellyfin_mpv_shim", "thumbfast.lua")

# luajit first: it is what mpv itself usually embeds, so it is the dialect
# the renderer actually has to run under.
INTERPRETERS = ("luajit", "lua5.1", "lua5.2", "lua5.3", "lua5.4", "lua")


#: Pick the interpreters explicitly, comma-separated: JMS_LUA=lua5.2.
LUA_ENV = "JMS_LUA"


def find_lua():
    for name in INTERPRETERS:
        path = shutil.which(name)
        if path:
            return path
    return None


def selected_interpreters(environ=None):
    """The interpreters this run must use, or None for "the first found".

    ``JMS_LUA`` wins when set. Otherwise every Lua interpreter that
    ``JMS_REQUIRE`` names (see tests/_requirements.py) -- so a CI job that
    requires luajit and lua5.2 runs the whole renderer suite under each.
    Anything selected either way FAILS when it is missing; only the
    unselected default may skip."""
    env = os.environ if environ is None else environ
    explicit = [n for n in re.split(r"[\s,]+", env.get(LUA_ENV, "")) if n]
    if explicit:
        bad = [n for n in explicit if n not in INTERPRETERS]
        if bad:
            raise ValueError("%s names unknown interpreter(s) %s; known: %s"
                             % (LUA_ENV, ", ".join(repr(b) for b in bad),
                                ", ".join(INTERPRETERS)))
        return list(dict.fromkeys(explicit))
    req = _requirements.required(env)
    named = [n for n in INTERPRETERS if n in req]
    return named or None


LUA = find_lua()


class _RendererLuaTests:
    """Every renderer test, run under ``self.lua``. Not a TestCase itself:
    one is built per interpreter at the bottom of this module."""

    lua = None          # path of the interpreter this class runs under
    lua_name = None     # the name it was found or selected by

    def _run(self, script, env=None, target=None):
        return subprocess.run(
            [self.lua, os.path.join(LUA_DIR, script), target or RENDERER],
            cwd=LUA_DIR, capture_output=True, text=True, timeout=120,
            env=env)

    def _assert_suite_passed(self, proc):
        if proc.returncode != 0:
            self.fail("%s\n%s" % (proc.stdout, proc.stderr))
        # A silent pass would also be a pass if the script exited early
        # before running anything, so check it reported a plan.
        self.assertIn("1..", proc.stdout, "no test plan in the output")
        self.assertNotIn("not ok", proc.stdout)

    @staticmethod
    def _session_env(**overrides):
        """A pinned session for the clipboard tests. The renderer picks its
        clipboard helper off WAYLAND_DISPLAY / DISPLAY, so an unpinned
        environment silently tests whichever session the developer happens
        to be sitting in — and never the other one."""
        env = dict(os.environ)
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        env.update(overrides)
        return env

    def test_the_renderer_suite_passes_on_x11(self):
        self._assert_suite_passed(
            self._run("test_renderer.lua",
                      env=self._session_env(DISPLAY=":0")))

    def test_the_renderer_suite_passes_on_wayland(self):
        """Same suite, Wayland session: the clipboard block asserts wl-copy
        rather than xclip. A Wayland session usually also answers xclip
        through XWayland, which is a *different* clipboard, so picking the
        wrong one would look like copy working and paste returning stale
        text."""
        self._assert_suite_passed(
            self._run("test_renderer.lua",
                      env=self._session_env(WAYLAND_DISPLAY="wayland-0")))

    def test_the_renderer_suite_passes_without_update_clipboard(self):
        """Third session: an mpv with no `update-clipboard` command.

        It is master-only -- neither v0.40.0 nor v0.41.0 has it -- and the
        renderer probes `command-list` once and caches the answer, exactly
        as it caches `clip_tools`. So one process can only ever see one of
        the two answers, and the case that needs pinning is the one where
        the command is ABSENT: `mp.command_native` does not raise for a
        missing command, it returns nil and mpv logs an ERROR line, which
        would then appear on every paste for those users.
        """
        self._assert_suite_passed(
            self._run("test_renderer.lua",
                      env=self._session_env(DISPLAY=":0",
                                            JMS_TEST_NO_CLIP_UPDATE="1")))

    def test_the_renderer_suite_passes_with_the_test_observer_on(self):
        """The e2e harness runs every app with JMS_TEST_OBSERVE, which
        publishes each frame from inside render(). Its visibility check
        assumed every node has a size; a menu has none, so it raised on
        every frame with a menu open -- and in the real app that blanked the
        whole screen, in every e2e run, for as long as the menu was up.
        This suite opens menus, so it is the observer's test as well."""
        self._assert_suite_passed(
            self._run("test_renderer.lua",
                      env=self._session_env(DISPLAY=":0",
                                            JMS_TEST_OBSERVE="1")))

    def _tick_case(self, expect_ms, diag=True, tick=None):
        env = self._session_env(DISPLAY=":0",
                                JMS_TEST_EXPECT_TICK_MS=str(expect_ms))
        for k in ("JMS_FRAME_DIAG", "JMS_TICK_MS"):
            env.pop(k, None)
        if diag:
            env["JMS_FRAME_DIAG"] = "1"
        if tick is not None:
            env["JMS_TICK_MS"] = tick
        self._assert_suite_passed(self._run("test_tick_override.lua", env=env))

    def test_the_frame_floor_is_30ms_by_default(self):
        self._tick_case(30, diag=False)
        self._tick_case(30)

    def test_the_tick_override_needs_the_frame_diagnostics(self):
        """JMS_TICK_MS alone must not change production cadence."""
        self._tick_case(30, diag=False, tick="16.7")
        self._tick_case(30, diag=False, tick="50")

    def test_a_valid_tick_override_replaces_the_floor(self):
        for tick, want in (("16.7", 16.7), ("20", 20), ("5", 5),
                           ("100", 100), ("45.5", 45.5)):
            with self.subTest(tick=tick):
                self._tick_case(want, tick=tick)

    def test_an_invalid_tick_override_leaves_the_floor_at_30ms(self):
        for tick in ("0", "-20", "4.9", "100.1", "5000", "nan", "inf",
                     "-inf", "1e1", "0x14", "", " ", "abc", "20ms", "1.2.3",
                     ".5", "-"):
            with self.subTest(tick=tick):
                self._tick_case(30, tick=tick)

    # ---- the opt-in scroll recorder (JMS_SCROLL_DIAG) ----------------------

    def _sdiag_env(self, tmp, frame_diag, scroll_diag, expect):
        env = self._session_env(DISPLAY=":0", JMS_TEST_SDIAG_EXPECT=expect)
        for k in ("JMS_FRAME_DIAG", "JMS_TICK_MS", "JMS_SCROLL_DIAG"):
            env.pop(k, None)
        if frame_diag:
            env["JMS_FRAME_DIAG"] = "1"
        if scroll_diag:
            env["JMS_SCROLL_DIAG"] = os.path.join(tmp, "scroll.tsv")
        return env

    def _sdiag_trace(self, frame_diag, scroll_diag, expect, target=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run("test_scroll_diag.lua",
                             env=self._sdiag_env(tmp, frame_diag, scroll_diag,
                                                 expect), target=target)
            self._assert_suite_passed(proc)
            wrote = os.path.exists(os.path.join(tmp, "scroll.tsv"))
        trace = [l for l in proc.stdout.splitlines() if l.startswith("TRACE")]
        self.assertGreater(len(trace), 50, "the scripted gesture did not run")
        return trace, wrote

    def test_the_scroll_recorder_is_off_unless_both_variables_are_set(self):
        """Normal operation, the frame diagnostics alone, and the recorder
        variable alone all leave no file behind."""
        for frame_diag, scroll_diag in ((False, False), (True, False),
                                        (False, True)):
            with self.subTest(frame_diag=frame_diag, scroll_diag=scroll_diag):
                _trace, wrote = self._sdiag_trace(frame_diag, scroll_diag,
                                                  "off")
                self.assertFalse(wrote)

    def test_the_scroll_recorder_records_accurately(self):
        """Event and frame lines against the scripted gesture: scale and
        class (0.98 fine, 0.99 and 1.0 notches), the interval, want/before/
        after, clamps at both ends, glide flags, and per-frame displacement
        that adds up to the drawn travel."""
        _trace, wrote = self._sdiag_trace(True, True, "on")
        self.assertTrue(wrote)

    def test_enabling_the_scroll_recorder_changes_no_pixel(self):
        """The same gesture, stored and PAINTED offsets at every step, with
        the recorder on, off, with only half of its switches set, and on the
        renderer as it was before the recorder existed."""
        on, _ = self._sdiag_trace(True, True, "on")
        for frame_diag, scroll_diag in ((False, False), (True, False),
                                        (False, True)):
            with self.subTest(frame_diag=frame_diag, scroll_diag=scroll_diag):
                off, _ = self._sdiag_trace(frame_diag, scroll_diag, "off")
                self.assertEqual(on, off)

    def test_the_scroll_recorder_output_is_bounded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = self._sdiag_env(tmp, True, True, "on")
            self._assert_suite_passed(
                self._run("test_scroll_diag_bounds.lua", env=env))

    def test_the_renderer_suite_passes_with_the_scroll_recorder_on(self):
        """Every existing wheel, touchpad, glide, reversal and boundary
        test, with the recorder running underneath."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = self._sdiag_env(tmp, True, True, "on")
            self._assert_suite_passed(self._run("test_renderer.lua", env=env))

    def test_the_scroll_recorder_names_nothing(self):
        """The writer formats numbers, flags and numbered containers only:
        no node id, source path, title or address reaches a format call."""
        with open(RENDERER, encoding="utf-8") as fh:
            src = fh.read()
        block = src[src.index("function state.scroll_diag_new()"):
                    src.index("-- Opt-in frame-submission counters")]
        self.assertNotIn("node.src", block)
        self.assertNotIn("node.text", block)
        self.assertNotIn("node.label", block)
        self.assertNotIn("utils.format_json", block)
        self.assertNotIn("send(", block)                 # nothing leaves the process
        self.assertNotIn("mp.command", block)
        self.assertEqual(block.count("io.open("), 1)      # one file, the one named

    def test_every_image_overlay_stays_inside_its_image(self):
        """A seeded sweep of scene images at fractional positions, behind
        scrolls, layers and occluders, and scaled: every overlay-add must
        address bytes inside iw*ih*4. #800 was a 432.99999999999994 edge the
        integer-positioned tests above never produce; this fails on the
        renderer before #801."""
        self._assert_suite_passed(self._run("test_image_bounds.lua"))

    def test_combined_scroll_windows_change_no_pixel(self):
        """state.merge_windows: a grid's rows drawn as ONE overlay cut from
        their combined window buffer. Exactly one overlay-add per scrolling
        frame for an eligible grid, none for an idle one, separate overlays
        for file sources and carousels -- and, by a pixel oracle that
        replays every overlay into mpv's slot order, the same picture as
        drawing each row on its own, over a random sweep of clips,
        occluders, dialogs, narrower rows and bitmaps floated between rows.
        Each of the merge's safety rules has a case that fails without it.
        """
        self._assert_suite_passed(self._run("test_overlay_windows.lua"))

    def test_the_thumbfast_suite_passes(self):
        """`thumbfast.lua` is the other consumer of the trickplay frame
        file — the compatibility layer every thumbfast-style lua OSC talks
        to, and `mpv_options.mpv_scripts` loads it under every OSC style.

        It had no tests at all while renderer.lua had a suite, which is this
        repo's recurring shape: the right mechanism applied to one of two
        implementations. It matters here because the bounds check in it is
        what stands between a windowed frame file and a read past the end of
        it — a failed overlay-add on a current mpv, a SIGBUS on one old
        enough to still mmap what it is handed.
        """
        self._assert_suite_passed(
            self._run("test_thumbfast.lua", target=THUMBFAST))

    def test_the_suites_pass_without_overlay_scaling(self):
        """An mpv older than 0.38, whose overlay-add takes no display size.

        Both consumers probe `command-list` once and cache the answer, so one
        process only ever sees one of the two; this is the other. The extra
        arguments are rejected there rather than ignored, so a frame passed a
        display size on such an mpv is not drawn at all.
        """
        env = dict(os.environ, JMS_TEST_NO_OVERLAY_SCALE="1")
        self._assert_suite_passed(
            self._run("test_thumbfast.lua", env=env, target=THUMBFAST))
        self._assert_suite_passed(
            self._run("test_renderer.lua",
                      env=self._session_env(DISPLAY=":0",
                                            JMS_TEST_NO_OVERLAY_SCALE="1")))

    def test_thumbfast_parses_under_this_interpreter(self):
        proc = subprocess.run(
            [self.lua, "-e", "assert(loadfile(%r))" % THUMBFAST],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_the_renderer_parses_under_this_interpreter(self):
        """Cheap syntax gate, separate from the behavioural run: a parse
        error otherwise surfaces as a wall of failed assertions."""
        proc = subprocess.run(
            [self.lua, "-e", "assert(loadfile(%r))" % RENDERER],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(
            proc.returncode, 0,
            proc.stderr + "\n\n" + self.LOCAL_CEILING_HELP
            if "local variables" in proc.stderr else proc.stderr)

    #: Shown when the parse fails on Lua's per-function local limit, because
    #: the raw message ("main function has more than 200 local variables")
    #: names neither the cause nor the fix, and points at the last line of a
    #: 4,500-line file rather than at whatever was just added.
    LOCAL_CEILING_HELP = (
        "renderer.lua's main chunk is at Lua's 200-local ceiling, so the\n"
        "file-scope `local` that was just added is one too many. It is not\n"
        "the line the error points at — that is simply where the compiler\n"
        "gave up.\n\n"
        "Options, cheapest first:\n"
        "  * put single-use helpers inside their one caller;\n"
        "  * hang new tunables off the `state` table (rcost and render_duty do);\n"
        "  * group a family of related constants into one table local.\n\n"
        "tests/test_renderer_lua.py::test_there_is_a_local_budget_left\n"
        "reports how much room is left before this happens again.")

    #: Locals to probe for when measuring headroom. Above this we stop
    #: caring — anything that far from the ceiling is not a hazard.
    HEADROOM_PROBE = 24

    def _compile(self, source):
        """``(compiles, error)`` for ``source`` under this interpreter."""
        check = subprocess.run(
            [self.lua, "-e",
             "local f, e = load(io.read('*a')); if not f then "
             "io.stderr:write(e or 'err'); os.exit(1) end"],
            input=source, capture_output=True, text=True, timeout=60)
        return check.returncode == 0, check.stderr

    def _headroom(self, source=None):
        """How many more file-scope locals ``source`` (renderer.lua by
        default) could take; -1 when it does not compile as it is."""
        if source is None:
            with open(RENDERER, encoding="utf-8") as fh:
                source = fh.read()
        # After the first line, so the shebang-less header comment stays put
        # and the padding is unambiguously file scope.
        head, _nl, rest = source.partition("\n")
        for n in range(self.HEADROOM_PROBE + 1):
            pad = "\n".join("local __pad%d = %d" % (i, i) for i in range(n))
            probe = "%s\n%s\n%s" % (head, pad, rest)
            if not self._compile(probe)[0]:
                return n - 1
        return self.HEADROOM_PROBE

    def _assert_budget(self, headroom):
        """The verdict test_there_is_a_local_budget_left gives ``headroom``:
        fail below zero, skip AT the ceiling (a notice, not a missing tool),
        pass otherwise."""
        self.assertGreaterEqual(
            headroom, 0,
            "renderer.lua does not compile.\n\n" + self.LOCAL_CEILING_HELP)
        if headroom == 0:
            self.skipTest(
                "renderer.lua is AT the 200-local ceiling: the next "
                "file-scope local will break it.\n" + self.LOCAL_CEILING_HELP)

    @staticmethod
    def _chunk(n):
        """A main chunk with ``n`` file-scope locals and nothing else."""
        return "-- fixture\n" + "\n".join(
            "local v%d = %d" % (i, i) for i in range(n)) + "\n"

    def test_the_ceiling_check_detects_an_over_limit_chunk(self):
        """The ceiling guard is only worth anything if it fires: a chunk
        one local over Lua's limit must fail to compile here, be measured
        as over, and fail the budget test -- under THIS interpreter, since
        each dialect words the error its own way."""
        ok, err = self._compile(self._chunk(201))
        self.assertFalse(ok, "201 file-scope locals compiled under %s"
                         % self.lua_name)
        self.assertIn("local variables", err)
        self.assertTrue(self._compile(self._chunk(200))[0])
        self.assertEqual(self._headroom(self._chunk(201)), -1)
        self.assertEqual(self._headroom(self._chunk(200)), 0)
        self.assertEqual(self._headroom(self._chunk(195)), 5)
        with self.assertRaises(AssertionError) as over:
            self._assert_budget(self._headroom(self._chunk(201)))
        self.assertIn("does not compile", str(over.exception))
        with self.assertRaises(unittest.SkipTest):
            self._assert_budget(self._headroom(self._chunk(200)))
        self._assert_budget(self._headroom(self._chunk(195)))   # no raise

    def test_no_renderer_helper_is_defined_and_never_called(self):
        """A `function state.X` nobody calls is a feature that is wired up
        everywhere except where it fires.

        Written after exactly that: the comic reader's wheel handler was
        defined, the message that configures it arrived, the drag and the
        ctrl+wheel paths that read the same state both worked — and the
        branch that *calls* it never made it into `on_wheel`, so plain
        scrolling did nothing while everything around it behaved. Nothing
        failed, in Lua or in Python; the function was simply unreachable.

        Scoped to the `state.`/`keyclaim.` helpers because those are the
        ones that exist to
        dodge the local ceiling: a file-scope `local function` that nobody
        calls is at least visible as an unused name, while a table field
        assigned in one place and read in none looks exactly like a field
        that is used somewhere else in a 4,500-line file.
        """
        with open(RENDERER, encoding="utf-8") as fh:
            source = fh.read()
        defined = set(re.findall(
            r"^function (?:state|keyclaim)\.(\w+)\s*\(", source, re.M))
        self.assertTrue(defined, "no state helpers found — has the "
                                 "convention changed?")
        dead = []
        for name in sorted(defined):
            # Every mention that is not the definition line.
            uses = re.findall(r"(?:state|keyclaim)\.%s\b" % name,
                              source)
            if len(uses) < 2:
                dead.append(name)
        self.assertEqual(
            dead, [],
            "defined on `state` but never called:\n  "
            + "\n  ".join(dead)
            + "\n\nEither call it, or delete it. A helper that is only "
              "ever defined is a branch that silently does nothing.")

    def test_there_is_a_local_budget_left(self):
        """The ceiling is not a wall you should discover by hitting it.

        Lua allows 200 locals per function, and renderer.lua's main chunk is
        one function — every top-level `local`, constant or helper, spends
        from the same budget. It has been hit more than once, and the
        failure is opaque: a load error naming a line at the end of the file
        that has nothing to do with the change.

        This reports the remaining room, so running out is a decision rather
        than a surprise.
        """
        self._assert_budget(self._headroom())

    def test_the_interpreter_is_the_one_it_claims_to_be(self):
        """A `lua5.2` on PATH that is really a 5.4 alias, or a `luajit`
        that is a wrapper around PUC Lua, would run every test above under
        the wrong dialect and report it as the right one."""
        proc = subprocess.run(
            [self.lua, "-e", "io.write(_VERSION, '|', "
                             "type(jit) == 'table' and jit.version or '')"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        version, _bar, jit = proc.stdout.partition("|")
        self.assertTrue(version.startswith("Lua "), proc.stdout)
        if self.lua_name == "luajit":
            self.assertTrue(jit.startswith("LuaJIT"),
                            "%s is not LuaJIT: %r" % (self.lua, proc.stdout))
        elif self.lua_name in IDENTITY:
            self.assertEqual(version, IDENTITY[self.lua_name])
            self.assertEqual(jit, "", "%s is LuaJIT" % self.lua)


#: The `_VERSION` each versioned PUC interpreter must report.
IDENTITY = {"lua5.1": "Lua 5.1", "lua5.2": "Lua 5.2", "lua5.3": "Lua 5.3",
            "lua5.4": "Lua 5.4"}


def _renderer_case(class_name, lua_name, lua_path, missing):
    """A TestCase running every renderer test under one interpreter.

    ``missing`` decides what happens without that interpreter: "skip" (the
    old behaviour, for a run that asked for nothing) or "fail" (it was
    selected or required, so its absence must not read as a pass)."""
    # A class statement rather than type(): tests/test_no_uncollected_tests.py
    # reads the source, and recognises the mixin only from a base list
    # written out like this one.
    class cls(_RendererLuaTests, unittest.TestCase):
        lua = lua_path
        __doc__ = "renderer tests under %s" % (lua_name or "Lua")

    cls.lua_name = lua_name
    cls.__name__ = cls.__qualname__ = class_name
    if lua_path:
        return cls
    if missing == "skip":
        return unittest.skip(
            "no Lua interpreter (tried: %s)" % ", ".join(INTERPRETERS))(cls)
    return _requirements.fail_all(
        "%s is required for the renderer tests but is not on PATH (%s)"
        % (lua_name, _why_required(lua_name)))(cls)


def _why_required(name):
    if os.environ.get(LUA_ENV):
        return "%s=%s" % (LUA_ENV, os.environ[LUA_ENV])
    return "%s=%s" % (_requirements.ENV, os.environ.get(_requirements.ENV, ""))


SELECTED = selected_interpreters()
if SELECTED:
    for _name in SELECTED:
        _cls = _renderer_case("TestRendererLua_" + _name.replace(".", "_"),
                              _name, shutil.which(_name), "fail")
        globals()[_cls.__name__] = _cls
    del _name, _cls
else:
    TestRendererLua = _renderer_case(
        "TestRendererLua", LUA and os.path.basename(LUA), LUA, "skip")


class RendererInterpreterSelectionTest(unittest.TestCase):
    """The wiring that decides which interpreters run, which needs none."""

    def test_selection_follows_jms_lua_then_jms_require(self):
        self.assertIsNone(selected_interpreters({}))
        self.assertIsNone(selected_interpreters({"JMS_REQUIRE": "msgfmt"}))
        self.assertEqual(
            selected_interpreters({"JMS_REQUIRE": "mypy lua5.2,luajit"}),
            ["luajit", "lua5.2"])
        self.assertEqual(
            selected_interpreters({"JMS_LUA": "lua5.2",
                                   "JMS_REQUIRE": "luajit"}),
            ["lua5.2"])

    def test_an_unknown_interpreter_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            selected_interpreters({"JMS_LUA": "lua52"})
        self.assertIn("'lua52'", str(cm.exception))
        self.assertIn("lua5.2", str(cm.exception))

    def test_a_missing_selected_interpreter_fails_every_test(self):
        cls = _renderer_case("Probe", "lua5.2", None, "fail")
        result = unittest.TestResult()
        unittest.TestLoader().loadTestsFromTestCase(cls).run(result)
        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.failures), result.testsRun)
        self.assertGreaterEqual(result.testsRun, len(_test_names()))
        self.assertIn("lua5.2 is required", result.failures[0][1])

    def test_a_missing_unrequested_interpreter_still_skips(self):
        cls = _renderer_case("Probe", None, None, "skip")
        result = unittest.TestResult()
        unittest.TestLoader().loadTestsFromTestCase(cls).run(result)
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.skipped), len(_test_names()))

    def test_every_selected_interpreter_gets_every_renderer_test(self):
        """What CI relies on: each interpreter it names runs the WHOLE
        renderer suite, not a class that happens to be empty."""
        names = _test_names()
        self.assertGreater(len(names), 20)
        cases = [v for v in globals().values() if isinstance(v, type)
                 and issubclass(v, _RendererLuaTests)
                 and v is not _RendererLuaTests]
        self.assertEqual(len(cases), len(SELECTED or [None]))
        for case in cases:
            self.assertEqual(
                set(unittest.TestLoader().getTestCaseNames(case)), names,
                case.__name__)


def _test_names():
    return {n for n in dir(_RendererLuaTests) if n.startswith("test")}


if __name__ == "__main__":
    unittest.main()
