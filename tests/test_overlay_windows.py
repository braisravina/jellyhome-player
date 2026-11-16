"""Combined scroll windows: StripStore.window / build_windows and their
plumbing (ImageMap.win -> layout ``wg`` -> MpvtkApp.scene_windows).

mpv rebuilds its whole overlay atlas on every overlay-add, so a scrolling
grid that re-issued one overlay per row paid that 4-6 times a frame. The
rows near the viewport are now ALSO copied into one immutable buffer, and
renderer.lua (state.merge_windows) crops runs of them out of it as one
overlay. These tests pin the Python half: the bytes, the layout rules that
decide eligibility, the lifetime of the buffer, and that every fallback --
jsonipc above all -- leaves the scene exactly as it was. The pixel-level
equivalence of the Lua half is tests/lua/test_overlay_windows.lua.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ctypes
import json
import shutil
import tempfile
import types
import unittest

from PIL import Image

from jellyfin_mpv_shim.mpvtk import scaling
from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore
from jellyfin_mpv_shim.mpvtk.widgets import Column, ImageMap
from jellyfin_mpv_shim.mpvtk_browser.strips import SiblingWindow, StripStore
from jellyfin_mpv_shim.mpvtk_browser.scroll_state import ScrollState
from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
from tests.integration._harness import FakeMPV

PITCH = 283      # a 271-line poster row plus the 12 px grid gap


def _image(w, h, seed):
    """Every pixel distinct, so a wrong line or column cannot compare equal."""
    img = Image.new("RGBA", (w, h))
    img.putdata([((x * 7 + seed) % 256, (y * 13 + seed) % 256,
                  (x + y + seed * 31) % 256, 255)
                 for y in range(h) for x in range(w)])
    return img


class _Grid:
    """A store plus a laid-out-looking grid scene of its bitmaps."""

    def __init__(self, test, mem=True, rows=4, iw=60, ih=20, gap=6,
                 narrow_last=False, y0=243.5):
        self.tmp = None
        if mem:
            self.store = StripStore(mem_store=MemoryStore())
        else:
            self.tmp = tempfile.mkdtemp(prefix="mpvtk-win-")
            test.addCleanup(shutil.rmtree, self.tmp, True)
            self.store = StripStore(cache_dir=self.tmp)
        test.addCleanup(self.store.shutdown)
        self.iw, self.ih, self.gap, self.y0 = iw, ih, gap, y0
        self.entries = []
        for k in range(rows):
            w = iw // 2 if (narrow_last and k == rows - 1) else iw
            self.entries.append(self.store.bitmap(("row", k),
                                                  _image(w, ih, k + 1)))

    def nodes(self, wg="grid#win", over=None):
        out = []
        for k, e in enumerate(self.entries):
            n = {"t": "img", "id": "grid-%d#strip" % k, "sc": "grid",
                 "src": e["src"], "iw": e["iw"], "ih": e["ih"], "v": e["v"],
                 "x": 16.0, "y": self.y0 + k * (self.ih + self.gap),
                 "w": float(e["iw"]), "h": float(e["ih"])}
            if wg is not None:
                n["wg"] = wg
            n.update((over or {}).get(k, {}))
            out.append(n)
        return out

    def buf(self, src):
        return self.store.mem._bufs.get(src)


class TestWindowBuffer(unittest.TestCase):

    def test_rows_land_on_their_lines_and_the_rest_is_transparent(self):
        g = _Grid(self, rows=4, narrow_last=True)
        nodes = g.nodes()
        g.store.build_windows(nodes)
        wins = [n["win"] for n in nodes]
        self.assertEqual(len({w["src"] for w in wins}), 1, "one window")
        w0 = wins[0]
        pitch = g.ih + g.gap
        self.assertEqual((w0["w"], w0["h"]), (g.iw, 3 * pitch + g.ih))
        self.assertEqual([w["y"] for w in wins], [0, pitch, 2 * pitch, 3 * pitch])
        self.assertEqual([w["i"] for w in wins], [0, 1, 2, 3])
        self.assertTrue(all("wg" not in n for n in nodes))
        win = bytes(g.buf(w0["src"]))
        self.assertEqual(len(win), w0["w"] * w0["h"] * 4)
        stride = w0["w"] * 4
        for k, e in enumerate(g.entries):
            src = bytes(g.buf(e["src"]))
            line = e["iw"] * 4
            for y in range(e["ih"]):
                at = (wins[k]["y"] + y) * stride
                self.assertEqual(win[at:at + line], src[y * line:(y + 1) * line],
                                 "row %d line %d" % (k, y))
                # right of a narrower row: transparent padding
                self.assertEqual(win[at + line:at + stride],
                                 bytes(stride - line))
            if k + 1 < len(g.entries):
                gap0 = (wins[k]["y"] + e["ih"]) * stride
                gap1 = wins[k + 1]["y"] * stride
                self.assertEqual(win[gap0:gap1], bytes(gap1 - gap0),
                                 "the gap after row %d is transparent" % k)

    def test_half_pixel_rows_share_a_window_fractional_spacing_does_not(self):
        g = _Grid(self, y0=243.5)
        nodes = g.nodes()
        g.store.build_windows(nodes)
        self.assertTrue(all("win" in n for n in nodes),
                        "rows at .5 a whole number of lines apart")
        nodes = g.nodes(over={2: {"y": 243.5 + 2 * 26 + 0.25}})
        g.store.build_windows(nodes)
        self.assertFalse(any("win" in n for n in nodes))

    def test_rows_that_cannot_share_a_window_are_left_alone(self):
        g = _Grid(self)
        cases = {
            "another column": {1: {"x": 17.0}},
            "another container": {1: {"sc": "other"}},
            "overlapping rows": {1: {"y": 243.5 + 10}},
            "a scaled image": {2: {"dw": 120}},
            "an offset source": {2: {"base": 4}},
        }
        for name, over in cases.items():
            with self.subTest(name):
                nodes = g.nodes(over=over)
                g.store.build_windows(nodes)
                self.assertFalse(any("win" in n for n in nodes))
                self.assertTrue(all("wg" not in n for n in nodes))
        one = g.nodes()[:1]
        g.store.build_windows(one)
        self.assertNotIn("win", one[0], "a single row has nothing to merge")

    def test_jsonipc_gets_no_window_and_loses_the_markers(self):
        g = _Grid(self, mem=False)
        nodes = g.nodes()
        before = [dict(n) for n in nodes]
        g.store.build_windows(nodes)
        for b, n in zip(before, nodes):
            b.pop("wg")
            self.assertEqual(n, b, "the scene is otherwise untouched")
        self.assertIsNone(g.store.window(
            [(n["src"], n["v"], n["iw"], n["ih"], 0) for n in nodes],
            60, 200))

    def test_an_unchanged_scene_reuses_the_same_window(self):
        g = _Grid(self)
        a, b = g.nodes(), g.nodes()
        g.store.build_windows(a)
        held = len(g.store.mem._bufs)
        g.store.build_windows(b)
        self.assertEqual([n["win"] for n in a], [n["win"] for n in b],
                         "same win, so the renderer's overlay args repeat "
                         "and it re-issues nothing")
        self.assertEqual(len(g.store.mem._bufs), held, "no new buffer")

    def test_artwork_arrival_makes_a_new_window_and_the_old_one_is_intact(self):
        g = _Grid(self)
        first = g.nodes()
        g.store.build_windows(first)
        old_src = first[0]["win"]["src"]
        old_bytes = bytes(g.buf(old_src))
        # a poster lands: row 1 recomposites under a new src and v
        g.entries[1] = g.store.bitmap(("row", 1, "poster"), _image(60, 20, 99))
        second = g.nodes()
        g.store.build_windows(second)
        new_src = second[0]["win"]["src"]
        self.assertNotEqual(new_src, old_src)
        self.assertNotEqual(second[0]["win"]["v"], first[0]["win"]["v"])
        self.assertEqual(bytes(g.buf(old_src)), old_bytes,
                         "a published window is never written again")
        stride = 60 * 4
        at = second[1]["win"]["y"] * stride
        self.assertEqual(bytes(g.buf(new_src))[at:at + 60 * 20 * 4],
                         bytes(g.buf(g.entries[1]["src"])))

    def test_a_recycled_member_address_does_not_find_the_old_window(self):
        g = _Grid(self)
        a = g.nodes()
        g.store.build_windows(a)
        b = g.nodes(over={1: {"v": a[1]["v"] + 1000}})   # same src, newer content
        # the bumped v names no live buffer state of its own here, but it is
        # a different key: the cache must not hand back the old window
        g.store.build_windows(b)
        self.assertNotEqual(a[0]["win"]["src"], b[0]["win"]["src"])

    def test_windows_are_freed_once_out_of_the_protected_generations(self):
        g = _Grid(self)
        s = g.store
        nodes = g.nodes()
        s.build_windows(nodes)
        src = nodes[0]["win"]["src"]
        strips = {e["src"] for e in g.entries}
        for _ in range(s.PROTECT_GENERATIONS):
            s.on_scene_pushed()
            self.assertIn(src, s.mem._bufs, "still possibly bound")
        s.on_scene_pushed()
        self.assertNotIn(src, s.mem._bufs, "aged out: freed at the push")
        self.assertEqual(len(s.mem._window_graveyard), 1,
                         "parked in the window graveyard")
        self.assertEqual(len(s.mem._graveyard), 0,
                         "not in the strip graveyard")
        self.assertTrue(strips <= set(s.mem._bufs),
                        "strips are left to the ordinary LRU")
        self.assertEqual(s._bytes, sum(e["iw"] * e["ih"] * 4
                                       for e in g.entries))

    def test_a_window_in_use_is_kept_while_rebuilt_each_scene(self):
        g = _Grid(self)
        s = g.store
        for _ in range(6):
            nodes = g.nodes()
            s.build_windows(nodes)
            s.on_scene_pushed()
        self.assertIn(nodes[0]["win"]["src"], s.mem._bufs)
        self.assertEqual(sum(1 for e in s._cache.values() if e.get("window")),
                         1)

    def test_a_freed_member_means_no_window(self):
        g = _Grid(self)
        nodes = g.nodes()
        g.store.mem.remove(nodes[2]["src"])
        g.store.build_windows(nodes)
        self.assertFalse(any("win" in n for n in nodes))

    def test_the_cap_keeps_the_middle_rows_and_shrinks_on_a_tight_budget(self):
        g = _Grid(self, rows=6)
        s = g.store
        row = 60 * 4 * 26
        s.WINDOW_MAX_BYTES = 3 * row          # room for three rows
        nodes = g.nodes()
        s.build_windows(nodes)
        tagged = [k for k, n in enumerate(nodes) if "win" in n]
        self.assertEqual(tagged, [1, 2, 3], "trimmed from both ends")
        self.assertLessEqual(nodes[1]["win"]["w"] * nodes[1]["win"]["h"] * 4,
                             s.WINDOW_MAX_BYTES)
        self.assertEqual(nodes[1]["win"]["i"], 0)
        del s.WINDOW_MAX_BYTES
        s.set_memory_pressure(True)
        self.assertEqual(s.window_cap(), s.TIGHT_MAX_BYTES // 4)

    def test_dead_srcs_checks_the_window_too(self):
        g = _Grid(self)
        nodes = g.nodes()
        g.store.build_windows(nodes)
        self.assertEqual(g.store.dead_srcs(nodes), [])
        bad = dict(nodes[3], win=dict(nodes[3]["win"], y=nodes[3]["win"]["h"]))
        self.assertTrue(any("cannot hold" in d
                            for d in g.store.dead_srcs([bad])))
        g.store.mem.remove(nodes[0]["win"]["src"])
        self.assertTrue(any("window" in d and "freed" in d
                            for d in g.store.dead_srcs(nodes)))

    def test_clear_frees_windows(self):
        g = _Grid(self)
        nodes = g.nodes()
        g.store.build_windows(nodes)
        g.store.clear()
        self.assertNotIn(nodes[0]["win"]["src"], g.store.mem._bufs)
        self.assertEqual(g.store._bytes, 0)


class TestPlumbing(unittest.TestCase):

    def test_layout_marks_only_imagemaps_that_ask(self):
        a = ImageMap("&1", 10, 10, w=10, h=10, id="a", win="g#win")
        b = ImageMap("&2", 10, 10, w=10, h=10, id="b")
        nodes, _ = layout(Column([a, b], w=10, h=20), 10, 20)
        img = {n["id"]: n for n in nodes if n["t"] == "img"}
        self.assertEqual(img["a"].get("wg"), "g#win")
        self.assertNotIn("wg", img["b"])

    def _app(self, build):
        app = MpvtkApp.attach(FakeMPV(), ext=False)
        sent = []
        app.backend = types.SimpleNamespace(
            command=lambda *a: sent.append(a))
        app.size = (200, 100)
        app._build = build
        return app, sent

    @staticmethod
    def _scene(sent):
        msgs = [a for a in sent if a[:2] == ("script-message", "mpvtk-scene")]
        return json.loads(msgs[-1][2])["nodes"]

    def _tree(self, size):
        # rasterized at the current scale, as a real producer would be
        iw, ih = scaling.px(20), scaling.px(10)
        return Column([ImageMap("&1", iw, ih, w=20, h=10, id="r0", win="g"),
                       ImageMap("&2", iw, ih, w=20, h=10, id="r1", win="g")],
                      w=size[0], h=size[1])

    def test_no_builder_means_no_markers_in_the_pushed_scene(self):
        app, sent = self._app(self._tree)
        app.scene_windows = None
        app._render()
        self.assertTrue(all("wg" not in n for n in self._scene(sent)))

    def test_the_builder_sees_the_physical_scene_before_it_is_checked(self):
        seen, order = [], []

        def builder(nodes):
            order.append("windows")
            seen.extend(dict(n) for n in nodes if n.get("t") == "img")
            for n in nodes:
                n.pop("wg", None)

        app, sent = self._app(self._tree)
        app.scene_windows = builder
        app.check_scenes = True
        app.scene_check = lambda nodes: order.append("check") or []
        scaling.set_scale(1.5)
        try:
            app._render()
        finally:
            scaling.set_scale(1.0)
        self.assertEqual(order, ["windows", "check"])
        self.assertEqual([n["wg"] for n in seen], ["g", "g"])
        self.assertEqual(seen[1]["y"], 15, "physical: 10 logical at 1.5x")

    def test_a_failing_builder_leaves_rows_drawing_on_their_own(self):
        def builder(nodes):
            raise RuntimeError("boom")

        app, sent = self._app(self._tree)
        app.scene_windows = builder
        app._render()
        nodes = self._scene(sent)
        self.assertEqual(sum(1 for n in nodes if n.get("t") == "img"), 2)
        self.assertTrue(all("wg" not in n and "win" not in n for n in nodes))

    @staticmethod
    def _renderer(offset):
        """Just enough TileRenderer for its two window methods."""
        fake = types.SimpleNamespace(
            scroll=types.SimpleNamespace(offset=lambda _id: offset[0]),
            WINDOW_SLACK=TileRenderer.WINDOW_SLACK,
            WINDOW_LOOKAHEAD=TileRenderer.WINDOW_LOOKAHEAD)
        fake.row_window = lambda *a: TileRenderer.row_window(fake, *a)
        return fake

    def test_the_window_covers_the_viewport_inside_the_composited_rows(self):
        geom = types.SimpleNamespace(strip_h=271)
        pitch = 271 + 12
        for h in (240, 720, 1080, 2160):
            offset = [0.0]
            fake = self._renderer(offset)
            moves, last = 0, None
            for off in range(0, 40000, 120):     # one push per STEP
                offset[0] = float(off)
                rf, rl = TileRenderer.row_window(fake, (1000, h), geom, "s")
                wf, wl = TileRenderer.overlay_window_rows(fake, (1000, h),
                                                          geom, "s")
                self.assertLessEqual(rf, wf)
                self.assertLessEqual(wl, rl)
                # the viewport, and what the renderer can scroll before the
                # next push, are inside the window
                ahead = TileRenderer.WINDOW_LOOKAHEAD
                self.assertLessEqual(wf, int(max(0, off - ahead) // pitch))
                self.assertGreaterEqual(wl, int((off + h + ahead) // pitch))
                moves += (wf, wl) != last
                last = (wf, wl)
            pushes = 40000 // 120
            # Sticky: re-centred on a minority of pushes. Recomputed every
            # push, the range moved on ~62% of them at 1080p; sticky it is
            # ~28% there and ~43% on a 240 px viewport, where nearly every
            # 120 px push crosses a 283 px row.
            self.assertLess(moves, pushes * 0.5, "h=%d: %d/%d" % (h, moves, pushes))

    def test_the_window_range_does_not_leak_across_screens(self):
        geom = types.SimpleNamespace(strip_h=271)
        offset = [500.0]
        fake = self._renderer(offset)
        for n in range(40):
            TileRenderer.overlay_window_rows(fake, (1000, 1080), geom, "s%d" % n)
        self.assertLessEqual(len(fake._overlay_windows), 16)


class _Stack:
    """A store plus a Home-page-looking scene: carousel strips, each in its
    own horizontal scroll, stacked in one vertical scroll ("home")."""

    VIEW_W = 100.0      # the carousels' viewport, physical
    LEAD = 16.0         # the strip's inset inside it (ROW_LEAD)

    def __init__(self, test, mem=True, rows=4, iw=240, ih=20, pitch=37,
                 y0=117.0, widths=None):
        self.tmp = None
        if mem:
            self.store = StripStore(mem_store=MemoryStore())
        else:
            self.tmp = tempfile.mkdtemp(prefix="mpvtk-win-")
            test.addCleanup(shutil.rmtree, self.tmp, True)
            self.store = StripStore(cache_dir=self.tmp)
        test.addCleanup(self.store.shutdown)
        self.ih, self.pitch, self.y0 = ih, pitch, y0
        self.entries = []
        for k in range(rows):
            w = (widths or {}).get(k, iw)
            self.entries.append(self.store.bitmap(("car", k),
                                                  _image(w, ih, k + 3)))

    def nodes(self, wg=None, over=None, scroll_over=None):
        wg = SiblingWindow("home#win") if wg is None else wg
        out = [{"t": "scroll", "id": "home", "axis": "y", "x": 0.0,
                "y": 60.0, "w": 120.0, "h": 400.0}]
        for k, e in enumerate(self.entries):
            y = self.y0 + k * self.pitch
            sc = {"t": "scroll", "id": "row-%d" % k, "axis": "x",
                  "sc": "home", "x": 0.0, "y": y - 5, "w": self.VIEW_W,
                  "h": float(self.ih + 10)}
            sc.update((scroll_over or {}).get(k, {}))
            out.append(sc)
            n = {"t": "img", "id": "row-%d#strip" % k, "sc": "row-%d" % k,
                 "src": e["src"], "iw": e["iw"], "ih": e["ih"], "v": e["v"],
                 "x": self.LEAD, "y": y, "w": float(e["iw"]),
                 "h": float(e["ih"]), "wg": wg}
            n.update((over or {}).get(k, {}))
            out.append(n)
        return out

    @staticmethod
    def strips(nodes):
        return [n for n in nodes if n.get("t") == "img"]

    def buf(self, src):
        return self.store.mem._bufs.get(src)


class TestSiblingCarouselWindows(unittest.TestCase):
    """SiblingWindow: the Home page's stacked carousels share one window."""

    def test_sibling_carousels_share_a_viewport_wide_window(self):
        st = _Stack(self, widths={2: 50})          # row 2 is a short carousel
        nodes = st.nodes()
        st.store.build_windows(nodes)
        strips = st.strips(nodes)
        wins = [n["win"] for n in strips]
        self.assertEqual(len({w["src"] for w in wins}), 1, "one window")
        w0 = wins[0]
        # What a carousel at rest shows of its strip, plus a rounding line:
        # not the 240 columns of the strip.
        ww = int(_Stack.VIEW_W - _Stack.LEAD) + 1
        self.assertEqual(w0["w"], ww)
        self.assertEqual(w0["h"], 3 * st.pitch + st.ih)
        self.assertEqual([w["y"] for w in wins],
                         [k * st.pitch for k in range(4)])
        self.assertEqual({w["g"] for w in wins}, {"home#win"})
        self.assertIs(type(w0["g"]), str, "a plain string for the renderer")
        self.assertTrue(all("wg" not in n for n in nodes))
        win = bytes(st.buf(w0["src"]))
        self.assertEqual(len(win), ww * w0["h"] * 4)
        stride = ww * 4
        for k, e in enumerate(st.entries):
            src = bytes(st.buf(e["src"]))
            line = e["iw"] * 4
            copy = min(e["iw"], ww) * 4
            for y in range(e["ih"]):
                at = (wins[k]["y"] + y) * stride
                self.assertEqual(win[at:at + copy],
                                 src[y * line:y * line + copy],
                                 "row %d line %d: its leading columns" % (k, y))
                self.assertEqual(win[at + copy:at + stride],
                                 bytes(stride - copy),
                                 "row %d line %d: transparent past it" % (k, y))
            if k + 1 < len(st.entries):
                g0 = (wins[k]["y"] + e["ih"]) * stride
                g1 = wins[k + 1]["y"] * stride
                self.assertEqual(win[g0:g1], bytes(g1 - g0),
                                 "the gap (the next heading) is transparent")

    def test_carousels_of_different_widths_share_a_window(self):
        """Run D: a carousel narrower than the window is laid out only as
        wide as its content (My Media with three libraries), and requiring
        one width refused every real Home page. Widths may differ; each row
        copies what ITS viewport shows, and the rest of its lines are
        transparent."""
        st = _Stack(self, rows=4, iw=240, widths={1: 50, 3: 70})
        # row 1: a short row, its container hugging its content; row 3: a
        # strip wider than a container that is narrower than the others
        scroll_over = {1: {"w": 50 + 2 * _Stack.LEAD}, 3: {"w": 60.0}}
        nodes = st.nodes(scroll_over=scroll_over)
        st.store.build_windows(nodes)
        strips = st.strips(nodes)
        self.assertTrue(all("win" in n for n in strips))
        wins = [n["win"] for n in strips]
        self.assertEqual(len({w["src"] for w in wins}), 1)
        views = [_Stack.VIEW_W, 50 + 2 * _Stack.LEAD, _Stack.VIEW_W, 60.0]
        copies = [min(e["iw"], int(v - _Stack.LEAD) + 1)
                  for e, v in zip(st.entries, views)]
        self.assertEqual(copies, [85, 50, 85, 45])
        ww = wins[0]["w"]
        self.assertEqual(ww, max(copies))
        win = bytes(st.buf(wins[0]["src"]))
        stride = ww * 4
        for k, e in enumerate(st.entries):
            src = bytes(st.buf(e["src"]))
            line, copy = e["iw"] * 4, copies[k] * 4
            for y in range(e["ih"]):
                at = (wins[k]["y"] + y) * stride
                self.assertEqual(win[at:at + copy],
                                 src[y * line:y * line + copy],
                                 "row %d line %d: what its viewport shows" % (k, y))
                self.assertEqual(win[at + copy:at + stride],
                                 bytes(stride - copy),
                                 "row %d line %d: nothing its clip hides" % (k, y))

    def test_a_member_wider_than_the_window_is_copied_within_its_bounds(self):
        st = _Stack(self, rows=2, iw=240, ih=7)
        e0, e1 = st.entries
        entry = st.store.window([(e0["src"], e0["v"], 240, 7, 0),
                                 (e1["src"], e1["v"], 240, 7, 9)], 33, 16)
        self.assertIsNotNone(entry)
        win = bytes(st.buf(entry["src"]))
        self.assertEqual(len(win), 33 * 16 * 4)
        for wy, e in ((0, e0), (9, e1)):
            src = bytes(st.buf(e["src"]))
            for y in range(7):
                self.assertEqual(win[(wy + y) * 132:(wy + y + 1) * 132],
                                 src[y * 960:y * 960 + 132])

    def test_only_an_opted_in_group_may_span_containers(self):
        st = _Stack(self)
        nodes = st.nodes(wg="home#win")          # a plain (grid) group
        st.store.build_windows(nodes)
        self.assertFalse(any("win" in n for n in nodes),
                         "a grid group still needs one container")
        self.assertTrue(all("wg" not in n for n in nodes))

    def test_sibling_structure_that_is_not_provable_is_left_alone(self):
        st = _Stack(self)
        cases = {
            "a row not in a horizontal scroll": dict(
                scroll_over={1: {"axis": "y"}}),
            "rows under different outer scrolls": dict(
                scroll_over={2: {"sc": "elsewhere"}}),
            "a carousel at another x (left edge)": dict(
                scroll_over={1: {"x": 4.0}}),
            "a strip at another x": dict(over={1: {"x": 17.0}}),
            "a container the scene does not have": dict(
                over={2: {"sc": "missing"}}),
            "fractional spacing": dict(over={2: {"y": 117.0 + 74.5}}),
            "overlapping rows": dict(over={1: {"y": 117.0 + 5}}),
            "a scaled strip": dict(over={1: {"dw": 300}}),
            "a file-backed strip": dict(over={1: {"src": "/tmp/x.bgra"}}),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                nodes = st.nodes(**kw)
                st.store.build_windows(nodes)
                self.assertFalse(any("win" in n for n in nodes))
                self.assertTrue(all("wg" not in n for n in nodes))

    def test_jsonipc_gets_no_window_and_loses_the_markers(self):
        st = _Stack(self, mem=False)
        nodes = st.nodes()
        before = [dict(n) for n in nodes]
        st.store.build_windows(nodes)
        for b, n in zip(before, nodes):
            b.pop("wg", None)
            self.assertEqual(n, b, "the scene is otherwise untouched")

    def test_artwork_arrival_makes_a_new_window_and_the_old_one_is_intact(self):
        st = _Stack(self)
        first = st.nodes()
        st.store.build_windows(first)
        old = st.strips(first)[0]["win"]
        old_bytes = bytes(st.buf(old["src"]))
        st.entries[2] = st.store.bitmap(("car", 2, "poster"),
                                        _image(240, 20, 77))
        second = st.nodes()
        st.store.build_windows(second)
        new = st.strips(second)[0]["win"]
        self.assertNotEqual((new["src"], new["v"]), (old["src"], old["v"]))
        self.assertEqual(bytes(st.buf(old["src"])), old_bytes,
                         "a published window is never written again")
        stride = new["w"] * 4
        at = st.strips(second)[2]["win"]["y"] * stride
        src = bytes(st.buf(st.entries[2]["src"]))
        self.assertEqual(bytes(st.buf(new["src"]))[at:at + new["w"] * 4],
                         src[:new["w"] * 4])

    def test_the_cap_counts_the_copied_width_not_the_strip(self):
        st = _Stack(self, rows=4, iw=2000)
        s = st.store
        ww = int(_Stack.VIEW_W - _Stack.LEAD) + 1
        need = ww * 4 * (3 * st.pitch + st.ih)
        s.WINDOW_MAX_BYTES = need       # all four, at the copied width
        nodes = st.nodes()
        s.build_windows(nodes)
        self.assertTrue(all("win" in n for n in st.strips(nodes)),
                        "2000-column strips, but only %d are copied" % ww)
        w = st.strips(nodes)[0]["win"]
        self.assertLessEqual(w["w"] * w["h"] * 4, s.WINDOW_MAX_BYTES)

    def test_a_sibling_group_over_the_cap_is_refused_not_trimmed(self):
        """A grid group is trimmed to the rows that fit; a sibling group is
        not: a window holding some of the carousels on screen was slower
        than none (4K, measured). Every row then draws on its own."""
        st = _Stack(self, rows=6, iw=2000)
        s = st.store
        ww = int(_Stack.VIEW_W - _Stack.LEAD) + 1
        s.WINDOW_MAX_BYTES = ww * 4 * (2 * st.pitch + st.ih)   # three of six
        nodes = st.nodes()
        before = len(s.mem._bufs)
        s.build_windows(nodes)
        self.assertFalse(any("win" in n for n in st.strips(nodes)))
        self.assertTrue(all("wg" not in n for n in nodes))
        self.assertEqual(len(s.mem._bufs), before, "nothing was copied")

    def test_a_row_left_out_leaves_transparent_lines_between_members(self):
        """A carousel paged sideways is not marked (TileRenderer), so its
        neighbours form a group with a gap: the window spans it, and the
        lines where it sits are transparent for the renderer to draw the
        row itself on top -- the rows either side still one rectangle."""
        st = _Stack(self, rows=4)
        nodes = st.nodes()
        strips = st.strips(nodes)
        strips[1].pop("wg")                     # row 1 is paged: left out
        st.store.build_windows(nodes)
        self.assertNotIn("win", strips[1])
        wins = [strips[k]["win"] for k in (0, 2, 3)]
        self.assertEqual([w["i"] for w in wins], [0, 1, 2],
                         "consecutive in the window, so they merge")
        self.assertEqual([w["y"] for w in wins],
                         [0, 2 * st.pitch, 3 * st.pitch],
                         "but on their real lines")
        w0 = wins[0]
        win = bytes(st.buf(w0["src"]))
        stride = w0["w"] * 4
        gap0, gap1 = st.ih * stride, 2 * st.pitch * stride
        self.assertEqual(win[gap0:gap1], bytes(gap1 - gap0),
                         "the paged row's lines are transparent")

    def test_windows_age_out_like_any_other(self):
        st = _Stack(self)
        s = st.store
        nodes = st.nodes()
        s.build_windows(nodes)
        src = st.strips(nodes)[0]["win"]["src"]
        for _ in range(s.PROTECT_GENERATIONS):
            s.on_scene_pushed()
            self.assertIn(src, s.mem._bufs)
        s.on_scene_pushed()
        self.assertNotIn(src, s.mem._bufs)

    def test_display_scales_merge_only_on_whole_line_spacing(self):
        """A Home-like tree through the real layout and scaling. Wherever
        the rows land a fractional number of lines apart the group is
        refused, and the scene is the one the rows would have drawn
        anyway -- correct, just not merged."""
        from jellyfin_mpv_shim.mpvtk.widgets import Box, HScroll, Spacer, VScroll

        results = {}
        for scale in (1.0, 1.25, 1.5, 1.75, 2.0):
            scaling.set_scale(scale)
            try:
                store = StripStore(mem_store=MemoryStore())
                self.addCleanup(store.shutdown)
                lw, lh = 300, 271
                iw, ih = scaling.px(lw), scaling.px(lh)
                blocks = []
                for k in range(3):
                    e = store.bitmap(("scale", scale, k), _image(iw, ih, k))
                    strip = ImageMap(e["src"], iw, ih, w=lw, h=lh,
                                     id="row-%d#strip" % k, v=e["v"],
                                     win=SiblingWindow("home#win"))
                    blocks.append(Column([
                        Spacer(h=30),       # the section heading
                        HScroll(Box([strip], pad=(16, 5)), id="row-%d" % k,
                                h=lh + 10, flex=1),
                    ], gap=4))
                tree = VScroll(Column(blocks, gap=20, pad=(0, 16)), id="home",
                               flex=1)
                nodes, _ = layout(Column([tree], w=200, h=600), 200, 600)
                scaling.scale_scene(nodes)
                strips = [n for n in nodes if n.get("t") == "img"]
                ys = [float(n["y"]) for n in strips]
                whole = all(abs((y - ys[0]) - round(y - ys[0])) < 1e-6
                            for y in ys)
                plain = [dict(n) for n in nodes]
                store.build_windows(nodes)
                merged = all("win" in n for n in strips)
                results[scale] = merged
                self.assertEqual(merged, whole, "scale %s" % scale)
                if not merged:
                    for b, n in zip(plain, nodes):
                        b.pop("wg", None)
                        self.assertEqual(n, b, "scale %s: untouched" % scale)
            finally:
                scaling.set_scale(1.0)
        self.assertTrue(results[1.0] and results[2.0],
                        "whole scales merge: %s" % results)


class TestRealHomeScene(unittest.TestCase):
    """The committed Home snapshot's scene, through the real window pipeline.

    Every other test here builds its own carousels, and all of them were the
    same width -- which is how a check that refused every real Home page
    (unequal carousel widths, Run D) passed them all. This one builds the
    scene the snapshot test pins, with the real Home page code, and asks
    the real StripStore to window it."""

    def test_the_real_home_scene_gets_one_window(self):
        from jellyfin_mpv_shim.mpvtk.layout import layout
        from tests._scene_snapshot import frozen_clock, load, normalize
        from tests.test_scene_snapshots import SCREENS, build_browser

        size = (1280, 720)
        browser = build_browser()
        with frozen_clock():
            browser.nav_stack = [dict(SCREENS["home"])]
            browser._load_route(browser.route)
            browser._pool.shutdown(wait=True)
            nodes, _handlers = layout(browser.build(size), *size)
        self.assertEqual(normalize([dict(n) for n in nodes]), load("home"),
                         "the scene is the committed snapshot")
        marked = [n for n in nodes if n.get("t") == "img" and "wg" in n]
        self.assertGreaterEqual(len(marked), 3)
        self.assertTrue(all(isinstance(n["wg"], SiblingWindow) for n in marked))
        widths = {n["id"]: [c for c in nodes if c.get("id") == n["sc"]][0]["w"]
                  for n in marked}
        self.assertGreater(len(set(widths.values())), 1,
                           "the carousels differ in width, as real ones do: %s"
                           % widths)
        # Real buffers of the scene's own sizes, so the copy is real too.
        store = StripStore(mem_store=MemoryStore())
        self.addCleanup(store.shutdown)
        for k, n in enumerate(marked):
            e = store.bitmap(("home", k), _image(n["iw"], n["ih"], k + 1))
            n["src"], n["v"] = e["src"], e["v"]
        store.build_windows(nodes)
        self.assertTrue(all("win" in n for n in marked),
                        "every Home carousel is in the window")
        self.assertEqual(len({n["win"]["src"] for n in marked}), 1)
        self.assertEqual([n["win"]["i"] for n in marked],
                         list(range(len(marked))))
        self.assertTrue(all("wg" not in n for n in nodes))


class TestHomeWindowMarking(unittest.TestCase):
    """TileRenderer.window_rows(windows=True): which carousels are marked."""

    class _Block(Column):
        pass

    def _renderer(self, offset, rows=None, cap=24 * 2 ** 20, view_w=1280):
        """Just enough TileRenderer. ``offset[0]`` is the page's offset;
        ``rows`` maps a carousel id to its offset -- an id it does not hold
        is one ScrollState has no answer for, which reads as 0."""
        rows = {} if rows is None else rows
        fake = types.SimpleNamespace(
            scroll=types.SimpleNamespace(
                offset=lambda i: offset[0] if i == "home"
                else float(rows.get(i) or 0.0)),
            art=types.SimpleNamespace(strips=types.SimpleNamespace(
                window_cap=lambda: cap)),
            row_view_w=lambda: view_w,
            WINDOW_SLACK=TileRenderer.WINDOW_SLACK,
            WINDOW_LOOKAHEAD=TileRenderer.WINDOW_LOOKAHEAD)
        fake.overlay_window_span = (
            lambda *a: TileRenderer.overlay_window_span(fake, *a))
        fake._mark_window = lambda *a: TileRenderer._mark_window(fake, *a)
        return fake

    def _blocks(self, n=12, h=320, awaiting=()):
        """Carousel-shaped blocks: a fixed-height Column whose strip is the
        ImageMap window_rows looks at (``_jms_strip``), and whose carousel
        is ``row-<k>`` (``_jms_row_id``)."""
        out = []
        for k in range(n):
            strip = ImageMap("&%d" % (k + 1), 10, 10, w=10, h=10,
                             id="row-%d#strip" % k)
            strip._jms_awaiting_art = k in awaiting
            b = self._Block([], h=h)
            b._jms_strip = strip
            b._jms_row_id = "row-%d" % k
            out.append(b)
        return out

    def _mark(self, fake, blocks, h=720):
        for b in blocks:
            b._jms_strip.win = None
        TileRenderer.window_rows(fake, blocks, "home", (1280, h), 20,
                                 windows=True)
        return self._marked(blocks)

    def test_a_paged_carousel_is_left_out_and_its_neighbours_kept(self):
        for paged in (0, 1, 2):                 # first, middle, last marked
            with self.subTest(paged=paged):
                fake = self._renderer([0.0], rows={"row-%d" % paged: 250.0})
                marked = self._mark(fake, self._blocks(n=3))
                self.assertEqual(sorted(set(range(3)) - {paged}), marked)

    def test_a_carousel_back_at_its_start_is_marked_again(self):
        rows = {"row-1": 250.0}
        fake = self._renderer([0.0], rows=rows)
        blocks = self._blocks(n=3)
        self.assertNotIn(1, self._mark(fake, blocks))
        rows["row-1"] = 0.0
        self.assertIn(1, self._mark(fake, blocks))
        rows["row-1"] = ScrollState.EDGE         # within the edge slack
        self.assertIn(1, self._mark(fake, blocks))

    def test_an_unknown_position_keeps_the_row(self):
        # No entry: ScrollState could not say (mpv < 0.36, never scrolled).
        # The row stays in, and the renderer's per-frame checks are what
        # keep the picture right -- the behaviour before rows were left out.
        fake = self._renderer([0.0], rows={})
        self.assertEqual(self._mark(fake, self._blocks(n=3)), [0, 1, 2])
        bare = self._blocks(n=3)
        for b in bare:
            del b._jms_row_id                    # a block that names none
        self.assertEqual(self._mark(fake, bare), [0, 1, 2])

    def test_4k_falls_back_to_no_window_when_the_viewport_does_not_fit(self):
        # The real cap; 337-line Home sections, a 1920-wide view at 2x.
        fake = self._renderer([1500.0], view_w=1920)
        blocks = self._blocks(n=12, h=337)
        self.assertTrue(self._mark(fake, blocks, h=1020), "1x: a window")
        scaling.set_scale(2.0)
        try:
            self.assertEqual(self._mark(fake, blocks, h=1020), [],
                             "no partial window: every row draws alone")
        finally:
            scaling.set_scale(1.0)

    def test_rows_left_out_do_not_shrink_a_window_into_fitting(self):
        """At 4K the visible carousels do not fit; leaving some out (still
        loading, or paged) must not let the REST through as a partial
        window -- the shape that was slower than no window at all."""
        # Rows 4-7 are on screen. Only 4 and 5 are eligible -- 6 is paged,
        # the rest are loading -- and those two alone WOULD fit the cap.
        fake = self._renderer([1500.0], view_w=1920, rows={"row-6": 300.0})
        blocks = self._blocks(n=12, h=337,
                              awaiting=set(range(12)) - {4, 5, 6})
        two = (5 * 357 + 337 - 4 * 357) * 2 * 3840 * 4
        self.assertLess(two, 24 * 2 ** 20, "the eligible pair alone fits")
        scaling.set_scale(2.0)
        try:
            self.assertEqual(self._mark(fake, blocks, h=1020), [])
        finally:
            scaling.set_scale(1.0)

    def test_the_viewport_alone_is_marked_when_the_span_does_not_fit(self):
        # A cap between the two: the sticky span's rows do not fit, the
        # viewport-plus-lookahead's do.
        blocks = self._blocks(n=20, h=337)
        top, vh = 3000.0, 720.0
        ahead = TileRenderer.WINDOW_LOOKAHEAD
        y = [k * (337 + 20) for k in range(20)]
        need = [k for k in range(20)
                if y[k] + 337 > top - ahead and y[k] < top + vh + ahead]
        h_need = y[need[-1]] + 337 - y[need[0]]
        fake = self._renderer([top], cap=1280 * 4 * int(h_need) + 64)
        marked = self._mark(fake, blocks, h=int(vh))
        self.assertEqual(marked, need)
        fake = self._renderer([top], cap=1280 * 4 * int(h_need) - 64)
        self.assertEqual(self._mark(fake, self._blocks(n=20, h=337), h=int(vh)),
                         [], "and nothing when even that does not fit")

    def _marked(self, blocks):
        return [k for k, b in enumerate(blocks)
                if b._jms_strip.win is not None]

    def test_rows_near_the_viewport_are_marked_with_a_sibling_group(self):
        offset = [0.0]
        fake = self._renderer(offset)
        blocks = self._blocks()
        TileRenderer.window_rows(fake, blocks, "home", (1280, 720), 20,
                                 pad=16, windows=True)
        marked = self._marked(blocks)
        self.assertTrue(marked and marked[0] == 0)
        self.assertLess(marked[-1], 11, "far rows are left alone")
        g = blocks[0]._jms_strip.win
        self.assertIsInstance(g, SiblingWindow)
        self.assertEqual(g, "home#win")

    def test_without_windows_nothing_is_marked(self):
        fake = self._renderer([0.0])
        blocks = self._blocks()
        TileRenderer.window_rows(fake, blocks, "home", (1280, 720), 20)
        self.assertEqual(self._marked(blocks), [])

    def test_a_row_still_waiting_for_posters_is_left_out(self):
        fake = self._renderer([0.0])
        blocks = self._blocks(awaiting={1})
        TileRenderer.window_rows(fake, blocks, "home", (1280, 720), 20,
                                 windows=True)
        marked = self._marked(blocks)
        self.assertNotIn(1, marked)
        self.assertIn(0, marked)
        self.assertIn(2, marked)

    def test_a_lazy_row_is_marked_once_it_is_made_real(self):
        fake = self._renderer([0.0])
        real = self._blocks(n=1)[0]
        stand_in = self._Block([], h=320)
        stand_in._jms_realize = lambda: real
        out = TileRenderer.window_rows(fake, [stand_in], "home", (1280, 720),
                                       20, windows=True)
        self.assertIs(out[0], real)
        self.assertIsNotNone(real._jms_strip.win)

    def test_the_span_is_sticky_and_covers_the_viewport(self):
        """Every push, the viewport and what the renderer may scroll before
        the next one are inside the span; and the MARKED ROWS -- which is
        what decides whether a new window is copied -- change on a minority
        of pushes. Rows of 337 + 20, the Home page's poster sections."""
        ahead = TileRenderer.WINDOW_LOOKAHEAD
        for h in (240, 720, 1080, 2160):
            offset = [0.0]
            fake = self._renderer(offset)
            blocks = self._blocks(n=60, h=337)
            changes, last, pushes = 0, None, 0
            for off in range(0, 60 * 357 - h, 120):     # one push per STEP
                offset[0] = float(off)
                for b in blocks:
                    b._jms_strip.win = None
                TileRenderer.window_rows(fake, blocks, "home", (1280, h), 20,
                                         windows=True)
                y0, y1 = fake._overlay_windows[("span", "home",
                                                float(max(240, h)))]
                self.assertLessEqual(y0, max(0.0, off - ahead))
                self.assertGreaterEqual(y1, off + max(240, h) + ahead)
                marked = tuple(self._marked(blocks))
                changes += marked != last
                last = marked
                pushes += 1
            self.assertLess(changes, pushes * 0.5,
                            "h=%d: the window would be rebuilt on %d of %d "
                            "pushes" % (h, changes, pushes))

    def test_art_in_flight(self):
        fake = types.SimpleNamespace(
            _requested={"a", "b", "c"},
            _img_retry={"c": (TileRenderer.IMG_MAX_ATTEMPTS + 1, 0.0)},
            IMG_MAX_ATTEMPTS=TileRenderer.IMG_MAX_ATTEMPTS,
            art=types.SimpleNamespace(thumbs=types.SimpleNamespace(
                is_gone=lambda k: k == "b")))
        f = lambda k: TileRenderer._art_in_flight(fake, k)
        self.assertTrue(f("a"), "requested, still coming")
        self.assertFalse(f("b"), "the server has no such image")
        self.assertFalse(f("c"), "given up on")
        self.assertFalse(f("d"), "never asked for, or cooling off")


if __name__ == "__main__":
    unittest.main()
