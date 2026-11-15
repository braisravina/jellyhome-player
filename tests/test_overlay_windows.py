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
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore
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


if __name__ == "__main__":
    unittest.main()
