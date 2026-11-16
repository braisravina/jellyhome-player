-- Combined scrolling overlays (renderer.lua state.merge_windows).
--
-- A grid's rows can arrive tagged with `win`: Python has also copied them
-- into one contiguous buffer, and the renderer may draw a run of them as ONE
-- overlay cut out of it. The contract is that this changes how many
-- overlay-adds a frame costs and NOTHING about the picture. So the core of
-- this file is a pixel oracle: every overlay-add/remove is replayed into
-- slots, the slots are composited in mpv's order (ascending id), and the
-- screen a windowed scene produces must equal the one the same scene
-- produces without windows -- across random geometry, fractional
-- positions, clips, occluders, narrower rows and bitmaps painted between
-- rows.
--   lua test_overlay_windows.lua ../../jellyfin_mpv_shim/mpvtk/renderer.lua
package.path = "./?.lua;" .. package.path
local fake = require("fake_mp"); fake.install()
fake.log.props["input-builtin-dragging"] = true
assert(loadfile(arg[1]))()
local SW, SH = 160, 120
fake.observe("osd-dimensions", { w = SW, h = SH })

local n_tests, failed = 0, 0
local function ok(cond, name, detail)
    n_tests = n_tests + 1
    if cond then
        print("ok " .. n_tests .. " - " .. name)
    else
        failed = failed + 1
        print("not ok " .. n_tests .. " - " .. name
              .. (detail and (" -- " .. tostring(detail)) or ""))
    end
end

local function scene(nodes) fake.send("mpvtk-scene", fake.token({ nodes = nodes })) end
local function settle() fake.advance(1.0); fake.fire_timers() end
local function scroll_to(id, to)
    fake.send("mpvtk-scroll", fake.token({ id = id, to = to }))
    settle()
end

-- ------------------------------------------------------------ the model
-- Every bitmap lives at its own address range. A row's pixel encodes
-- (row, line, column), so a wrong line or column -- not just a missing
-- picture -- shows up as a different value.
local ROWBASE, WINBASE, CHIPBASE = 1e7, 9e8, 5e8
local bitmaps = {}      -- {base, w, h, pix(x, y)}
local function add_bitmap(base, w, h, pix)
    bitmaps[#bitmaps + 1] = { base = base, w = w, h = h, pix = pix }
end
local function decode(addr)
    for _, b in ipairs(bitmaps) do
        local off = addr - b.base
        if off >= 0 and off < b.w * b.h * 4 then
            local px = math.floor(off / 4)
            return b, px % b.w, math.floor(px / b.w)
        end
    end
    return nil
end

-- Replays overlay commands into slots; composite() paints them.
local slots, cursor = {}, 1
local bad_reads = 0
local function sync_slots()
    local cmds = fake.log.commands
    for i = cursor, #cmds do
        local c = cmds[i]
        if c[1] == "overlay-add" then
            slots[tonumber(c[2])] = {
                x = tonumber(c[3]), y = tonumber(c[4]), src = c[5],
                off = tonumber(c[6]), w = tonumber(c[8]), h = tonumber(c[9]),
                stride = tonumber(c[10]),
            }
        elseif c[1] == "overlay-remove" then
            slots[tonumber(c[2])] = nil
        end
    end
    cursor = #cmds + 1
end
local function composite()
    sync_slots()
    local screen = {}
    for slot = 0, 63 do
        local o = slots[slot]
        if o and o.src:sub(1, 1) == "&" then
            local base = tonumber(o.src:sub(2)) + o.off
            -- the whole rectangle must lie inside ONE bitmap
            local b0 = decode(base)
            local b1 = decode(base + (o.h - 1) * o.stride + (o.w - 1) * 4)
            if not b0 or b0 ~= b1 then bad_reads = bad_reads + 1 end
            for j = 0, o.h - 1 do
                for i = 0, o.w - 1 do
                    local b, bx, by = decode(base + j * o.stride + i * 4)
                    local v = b and b.pix(bx, by) or -1
                    if v ~= 0 then
                        local sx, sy = o.x + i, o.y + j
                        if sx >= 0 and sx < SW and sy >= 0 and sy < SH then
                            screen[sy * SW + sx] = v
                        end
                    end
                end
            end
        end
    end
    return screen
end
local function same(a, b)
    for k, v in pairs(a) do if b[k] ~= v then return false, k end end
    for k, v in pairs(b) do if a[k] ~= v then return false, k end end
    return true
end
local function adds_since(mark)
    local n, out = 0, {}
    for i = mark + 1, #fake.log.commands do
        local c = fake.log.commands[i]
        if c[1] == "overlay-add" then n = n + 1; out[#out + 1] = c end
    end
    return n, out
end

-- A grid: rows of iw x ih at y0 + k*pitch inside scroll "sc". `opts.win`
-- tags them with a window laid out exactly as StripStore.window does.
local function grid(rows, opts)
    bitmaps = {}
    local nodes = {
        { id = "sc", t = "scroll", axis = "y", x = opts.sx or 0,
          y = opts.sy or 0, w = opts.vw or SW, h = opts.vh or SH,
          cw = opts.vw or SW, ch = 4000 },
    }
    local ww, wh = 0, 0
    for k, r in ipairs(rows) do
        ww = math.max(ww, r.iw)
        wh = math.max(wh, r.wy + r.ih)
    end
    local wbase = WINBASE
    add_bitmap(wbase, ww, wh, function(x, y)
        for k, r in ipairs(rows) do
            if y >= r.wy and y < r.wy + r.ih and x < r.iw then
                return k * 1000000 + (y - r.wy) * 1000 + x + 1
            end
        end
        return 0
    end)
    for k, r in ipairs(rows) do
        local base = ROWBASE * k
        add_bitmap(base, r.iw, r.ih, function(x, y)
            return k * 1000000 + y * 1000 + x + 1
        end)
        local node = { id = "row" .. k, t = "img", sc = "sc",
            src = "&" .. base, iw = r.iw, ih = r.ih, v = 1,
            x = r.x, y = r.y, w = r.w or r.iw, h = r.h or r.ih }
        if opts.win then
            node.win = { src = "&" .. wbase, w = ww, h = wh, y = r.wy,
                         i = k - 1, v = 3, g = "grid#win" }
        end
        if opts.file then node.src = "/row" .. k end
        nodes[#nodes + 1] = node
        nodes[#nodes + 1] = { id = "row" .. k .. "-t", t = "rect", sc = "sc",
            x = r.x, y = r.y, w = math.min(r.iw, 20), h = r.ih,
            ring = true, click = true, hev = true }
        if opts.chip == k then
            -- a bitmap floated over row k (the hover play chip)
            add_bitmap(CHIPBASE, 8, 8, function() return 77 end)
            nodes[#nodes + 1] = { id = "chip", t = "img", sc = "sc",
                src = "&" .. CHIPBASE, iw = 8, ih = 8, v = 1,
                x = r.x + (opts.chip_dx or 2),
                y = r.y + (opts.chip_dy or 2), w = 8, h = 8 }
        end
    end
    for _, extra in ipairs(opts.extra or {}) do nodes[#nodes + 1] = extra end
    return nodes
end

local function uniform(n, iw, ih, gap, y0, x, narrow_last)
    local rows = {}
    for k = 1, n do
        rows[k] = { iw = (narrow_last and k == n) and math.floor(iw / 2) or iw,
                    ih = ih, x = x or 4, y = (y0 or 0) + (k - 1) * (ih + gap),
                    wy = (k - 1) * (ih + gap) }
    end
    return rows
end

-- Draw `nodes` at scroll offset `off`, with and without windows, and
-- compare the composites. Each side starts from an empty scene.
local merged_frames = 0
local function compare(rows, opts, off)
    local plain = {}
    for k, v in pairs(opts) do plain[k] = v end
    plain.win = false
    scene({}); settle()
    scene(grid(rows, plain)); settle(); scroll_to("sc", off)
    local want = composite()
    local wopts = {}
    for k, v in pairs(opts) do wopts[k] = v end
    wopts.win = true
    scene({}); settle()
    local mark = #fake.log.commands
    scene(grid(rows, wopts)); settle(); scroll_to("sc", off)
    local got = composite()
    local _, adds = adds_since(mark)
    for _, c in ipairs(adds) do
        if c[5]:sub(1, 1) == "&" and tonumber(c[5]:sub(2)) >= WINBASE then
            merged_frames = merged_frames + 1
            break
        end
    end
    return same(want, got)
end

-- ============================================================ 1. one add
-- Six rows on screen, all inside the viewport: per scroll frame, ONE
-- overlay-add, cut from the window with the window's stride.
do
    local rows = uniform(8, 120, 14, 4, 0.5)
    scene({}); settle()
    scene(grid(rows, { win = true })); settle()
    local per, strides, all_win = {}, {}, true
    for step = 1, 6 do
        local mark = #fake.log.commands
        scroll_to("sc", step * 3)
        local n, adds = adds_since(mark)
        per[#per + 1] = n
        for _, c in ipairs(adds) do
            strides[c[10]] = true
            if tonumber(c[5]:sub(2)) < WINBASE then all_win = false end
        end
    end
    ok(table.concat(per, ",") == "1,1,1,1,1,1",
       "an eligible grid costs exactly one overlay-add per scrolling frame",
       table.concat(per, ","))
    ok(strides[tostring(120 * 4)] and all_win,
       "that add is cut from the window, at the window's stride")
    -- and the same grid without windows: one per visible row
    scene({}); settle()
    scene(grid(rows, { win = false })); settle()
    local mark = #fake.log.commands
    scroll_to("sc", 21)
    local n = adds_since(mark)
    ok(n >= 6, "without windows the same frame costs one add per row (" .. n .. ")")
end

-- ============================================================ 2. idle
do
    local rows = uniform(6, 120, 14, 4, 0)
    scene({}); settle()
    scene(grid(rows, { win = true })); settle(); scroll_to("sc", 10)
    local mark = #fake.log.commands
    settle(); settle()
    scene(grid(rows, { win = true })); settle()     -- identical re-push
    local n = adds_since(mark)
    ok(n == 0, "an unchanged frame or an identical scene issues no overlay-add",
       n .. " adds")
end

-- ============================================================ 3. fallbacks
do
    -- jsonipc: file sources are never merged
    local rows = uniform(6, 120, 14, 4, 0)
    scene({}); settle()
    scene(grid(rows, { win = true, file = true })); settle()
    local mark = #fake.log.commands
    scroll_to("sc", 7)
    local n, adds = adds_since(mark)
    local any_win = false
    for _, c in ipairs(adds) do if c[5]:sub(1, 1) == "&" then any_win = true end end
    ok(n >= 6 and not any_win, "file-backed (jsonipc) rows keep one overlay each")

    -- rows that are not consecutive in the window stay separate
    rows = uniform(4, 120, 14, 4, 0)
    rows[3].wy = rows[3].wy + 1
    scene({}); settle()
    local nodes = grid(rows, { win = true })
    scene(nodes); settle()
    mark = #fake.log.commands
    scroll_to("sc", 3)
    n = adds_since(mark)
    ok(n >= 2, "a row whose window line does not match its position is not merged ("
       .. n .. " adds)")

    -- a horizontal carousel: no win, untouched
    scene({}); settle()
    add_bitmap(ROWBASE * 40, 400, 30, function(x, y) return 1 end)
    scene({
        { id = "hs", t = "scroll", axis = "x", x = 0, y = 0, w = SW, h = 40,
          cw = 400, ch = 40 },
        { id = "car", t = "img", sc = "hs", src = "&" .. (ROWBASE * 40),
          iw = 400, ih = 30, x = 0, y = 0, w = 400, h = 30, v = 1 },
    }); settle()
    mark = #fake.log.commands
    scroll_to("hs", 25)
    local _, cadds = adds_since(mark)
    ok(#cadds == 1 and tonumber(cadds[1][5]:sub(2)) < WINBASE,
       "a horizontal carousel keeps its own single overlay")
end

-- ============================================================ 4. pixels
-- Deterministic cases first, then a random sweep.
do
    local cases = {
        { "plain rows", uniform(7, 120, 14, 4, 0), {}, 9 },
        { "half-pixel rows, fractional offset", uniform(7, 120, 14, 4, 0.5), {}, 9.25 },
        { "a narrower last row", uniform(5, 120, 14, 4, 0, 4, true), {}, 2 },
        { "top and bottom partial rows", uniform(9, 120, 20, 3, 0), {}, 31 },
        { "no gap", uniform(9, 120, 20, 0, 0), {}, 13 },
        { "a chip over a row", uniform(6, 120, 14, 4, 0), { chip = 2 }, 5 },
        { "a chip spilling onto the next row",
          uniform(6, 120, 14, 4, 0), { chip = 2, chip_dy = 10 }, 5 },
        { "a popup over the middle rows", uniform(7, 120, 14, 4, 0),
          { extra = { { id = "pop", t = "layer", x = 30, y = 30, w = 60, h = 25 } } }, 4 },
        { "an occluder across the grid", uniform(7, 120, 14, 4, 0),
          { extra = { { id = "oc", t = "occ", sc = "sc", x = 0, y = 40, w = 160, h = 9 } } }, 4 },
        { "a clipped viewport", uniform(9, 120, 14, 4, 0),
          { sx = 10, sy = 13.5, vw = 100, vh = 70 }, 17.5 },
        -- Full width, from inside one row to inside the next: each row
        -- keeps one piece (the top of one, the bottom of the other), with
        -- consecutive window lines -- only the interior-cut rule stops
        -- them merging across what the dialog hides.
        { "a dialog across a row boundary", uniform(7, 120, 14, 4, 0),
          { extra = { { id = "dlg", t = "layer", x = -10, y = 27, w = 200, h = 11 } } }, 0 },
    }
    for _, c in ipairs(cases) do
        local same_px, at = compare(c[2], c[3], c[4])
        ok(same_px, "same pixels with and without windows: " .. c[1],
           at and ("first difference at pixel " .. at))
    end
    ok(merged_frames >= 6, "and those cases did merge (" .. merged_frames .. ")")
end

do
    math.randomseed(4242)
    local fr = { 0, 0.5, 0.25, 0.75, 1e-9 }
    local bad, first
    merged_frames = 0
    for trial = 1, 250 do
        local n = math.random(2, 7)
        local ih, gap = math.random(3, 18), math.random(0, 6)
        local iw = math.random(20, 150)
        local y0 = math.random(-20, 30) + fr[math.random(#fr)]
        local rows = uniform(n, iw, ih, gap, y0, math.random(0, 8),
                             math.random() < 0.4)
        local opts = { sx = math.random(0, 6), sy = math.random(0, 6) + fr[math.random(#fr)],
                       vw = math.random(60, SW), vh = math.random(30, SH) }
        local r = math.random()
        if r < 0.2 then
            opts.chip = math.random(1, n); opts.chip_dy = math.random(-4, ih + gap)
        elseif r < 0.35 then
            opts.extra = { { id = "pop", t = "layer", x = math.random(0, 120),
                y = math.random(0, 100), w = math.random(5, 60), h = math.random(3, 40) } }
        elseif r < 0.5 then
            opts.extra = { { id = "oc", t = "occ", sc = "sc", x = math.random(0, 120),
                y = math.random(0, 100) + fr[math.random(#fr)],
                w = math.random(5, 160), h = math.random(2, 30) } }
        end
        local off = math.random(0, 80) + fr[math.random(#fr)]
        local s, at = compare(rows, opts, off)
        if not s then
            bad = (bad or 0) + 1
            first = first or string.format("trial %d n=%d ih=%d gap=%d iw=%d y0=%s off=%s at %s",
                trial, n, ih, gap, iw, tostring(y0), tostring(off), tostring(at))
        end
    end
    ok(bad == nil, "random sweep: every windowed frame matches the per-row picture",
       first)
    ok(merged_frames > 100, "and the sweep exercised merging (" .. merged_frames .. " frames)")
    ok(bad_reads == 0, "no overlay ever read outside one bitmap (" .. bad_reads .. ")")
end

-- ============================================================ 4a. dialogs
-- A dialog over the middle of a grid cuts every row under it into a left
-- and a right piece. Those are columns of the window too: the left pieces
-- merge with each other and the right pieces with each other, so the frame
-- costs a handful of adds rather than two per covered row.
do
    local rows = uniform(7, 150, 12, 3, 0)
    local dlg = { { id = "dlg", t = "layer", x = 50, y = 20, w = 40, h = 60 } }
    local function per_frame(win)
        scene({}); settle()
        scene(grid(rows, { win = win, extra = dlg })); settle()
        local mark = #fake.log.commands
        scroll_to("sc", 2)
        return (adds_since(mark))
    end
    local plain, windowed = per_frame(false), per_frame(true)
    ok(windowed <= 4 and windowed < plain,
       "a grid under a dialog merges into columns (" .. windowed
       .. " adds against " .. plain .. ")")
    local s, at = compare(rows, { extra = dlg }, 2)
    ok(s, "and still draws the same pixels", at)
end

-- ============================================================ 4b. bounds
-- A window that claims fewer lines than its rows need (a Python-side bug)
-- must never be read past its end: the renderer's bounds check is the last
-- line of defence on a raw address.
do
    local before = bad_reads
    local rows = uniform(6, 120, 14, 4, 0)
    scene({}); settle()
    local nodes = grid(rows, { win = true })
    for _, nd in ipairs(nodes) do
        if nd.win then nd.win.h = 40 end      -- really 6*18-4 = 104 lines
    end
    -- the model's window is as small as the scene says
    bitmaps[1].h = 40
    scene(nodes); settle(); scroll_to("sc", 3)
    composite()
    ok(bad_reads == before,
       "a window smaller than its rows is never read past its end ("
       .. (bad_reads - before) .. " bad reads)")
end

-- ============================================================ 5. input
-- Hit-testing and hover do not look at overlays at all; prove it by
-- comparing the events of the same pointer path with and without windows.
do
    local function events_for(win)
        local rows = uniform(6, 120, 14, 4, 0)
        scene({}); settle()
        scene(grid(rows, { win = win })); settle(); scroll_to("sc", 6)
        fake.reset_events()
        for _, p in ipairs({ { 10, 5 }, { 10, 30 }, { 10, 47 }, { 90, 30 } }) do
            fake.observe("mouse-pos", { x = p[1], y = p[2], hover = true })
            settle()
            fake.key("mbtn_left")
            settle()
        end
        local out = {}
        for _, e in ipairs(fake.log.events) do
            if type(e) == "table" then
                out[#out + 1] = tostring(e.t) .. ":" .. tostring(e.id)
            end
        end
        return table.concat(out, " ")
    end
    local a, b = events_for(false), events_for(true)
    ok(a == b and #a > 0, "clicks and hover resolve the same nodes with windows",
       "\n  plain:    " .. a .. "\n  windowed: " .. b)
end

-- ============================================================ 6. stacked carousels
-- The Home page: carousel strips, each in its OWN horizontal scroll, stacked
-- in one vertical scroll, with a heading between rows. Python groups them as
-- a SiblingWindow (strips.py) and copies only each strip's leading viewport
-- width -- so the window is narrower than the strips. Every carousel still
-- scrolls sideways on its own; whatever the offsets, the picture must be the
-- one the rows draw without a window.
local LEAD = 6
local function stack(rows, opts)
    bitmaps = {}
    local sx, sy = opts.sx or 0, opts.sy or 0
    local vw, vh = opts.vw or SW, opts.vh or SH
    local nodes = {
        { id = "home", t = "scroll", axis = "y", x = sx, y = sy, w = vw,
          h = vh, cw = vw, ch = 4000 },
    }
    -- Each carousel's own viewport width: the view's, unless `r.view` says
    -- otherwise or `opts.fit` lays it out as the real one is -- only as wide
    -- as its content when that is narrower (My Media, a short section).
    local views, copies = {}, {}
    for k, r in ipairs(rows) do
        views[k] = r.view or (opts.fit and math.min(vw, r.iw + 2 * LEAD)) or vw
        -- StripStore._sibling_copies: what that carousel at rest shows of its
        -- strip (its viewport's right edge minus the strip's inset), plus a
        -- rounding line, never more than the strip.
        copies[k] = math.min(r.iw, views[k] - LEAD + 1)
    end
    local ww, wh = 0, 0
    for k, r in ipairs(rows) do
        ww = math.max(ww, copies[k])
        wh = math.max(wh, r.wy + r.ih)
    end
    add_bitmap(WINBASE, ww, wh, function(x, y)
        for k, r in ipairs(rows) do
            if y >= r.wy and y < r.wy + r.ih and x < copies[k] then
                return k * 1000000 + (y - r.wy) * 1000 + x + 1
            end
        end
        return 0
    end)
    for k, r in ipairs(rows) do
        local base = ROWBASE * k
        add_bitmap(base, r.iw, r.ih, function(x, y)
            return k * 1000000 + y * 1000 + x + 1
        end)
        local cid = "c" .. k
        nodes[#nodes + 1] = { id = "h" .. k, t = "text", sc = "home",
            text = "Row " .. k, x = sx + LEAD, y = r.y - 3, w = 30, h = 3,
            size = 3, c = "ffffff" }
        nodes[#nodes + 1] = { id = cid, t = "scroll", axis = "x", sc = "home",
            x = sx, y = r.y - 1, w = views[k], h = r.ih + 2,
            cw = r.iw + 2 * LEAD, ch = r.ih + 2 }
        local node = { id = "row" .. k, t = "img", sc = cid,
            src = "&" .. base, iw = r.iw, ih = r.ih, v = 1,
            x = sx + LEAD, y = r.y, w = r.iw, h = r.ih }
        if opts.win then
            node.win = { src = "&" .. WINBASE, w = ww, h = wh, y = r.wy,
                         i = k - 1, v = 3, g = "home#win" }
        end
        nodes[#nodes + 1] = node
        nodes[#nodes + 1] = { id = "row" .. k .. "-t", t = "rect", sc = cid,
            x = sx + LEAD, y = r.y, w = math.min(r.iw, 20), h = r.ih,
            ring = true, click = true, hev = true }
        if opts.chip == k then
            -- the hover play chip, inside the carousel so it travels with it
            add_bitmap(CHIPBASE, 8, 8, function() return 77 end)
            nodes[#nodes + 1] = { id = "chip", t = "img", sc = cid,
                src = "&" .. CHIPBASE, iw = 8, ih = 8, v = 1,
                x = sx + LEAD + (opts.chip_dx or 2),
                y = r.y + (opts.chip_dy or 2), w = 8, h = 8 }
        end
        if opts.arrows == k then
            -- overlay-mode page arrows: bitmaps over the row's two edges,
            -- outside its scroll (hscroll_row's Stack)
            add_bitmap(CHIPBASE + 1e6, 6, 6, function() return 55 end)
            for i, ax in ipairs({ sx + 1, sx + vw - 7 }) do
                nodes[#nodes + 1] = { id = "arrow" .. i, t = "img", sc = "home",
                    src = "&" .. (CHIPBASE + 1e6), iw = 6, ih = 6, v = 1,
                    x = ax, y = r.y + 2, w = 6, h = 6 }
            end
        end
    end
    for _, extra in ipairs(opts.extra or {}) do nodes[#nodes + 1] = extra end
    return nodes
end

local function carousels(n, iw, ih, pitch, y0, widths)
    local rows = {}
    for k = 1, n do
        rows[k] = { iw = (widths and widths[k]) or iw, ih = ih,
                    y = (y0 or 4) + (k - 1) * pitch, wy = (k - 1) * pitch }
    end
    return rows
end

-- Show `rows` at vertical offset `voff`, carousel k at `hoffs[k]`.
local function show_stack(rows, opts, voff, hoffs)
    scene(stack(rows, opts)); settle()
    for k, off in pairs(hoffs or {}) do
        if off ~= 0 then scroll_to("c" .. k, off) end
    end
    scroll_to("home", voff)
end

local stack_merged = 0
local function compare_stack(rows, opts, voff, hoffs)
    local plain = {}
    for k, v in pairs(opts) do plain[k] = v end
    plain.win = false
    scene({}); settle()
    show_stack(rows, plain, voff, hoffs)
    local want = composite()
    local wopts = {}
    for k, v in pairs(opts) do wopts[k] = v end
    wopts.win = true
    scene({}); settle()
    local mark = #fake.log.commands
    show_stack(rows, wopts, voff, hoffs)
    local got = composite()
    local _, adds = adds_since(mark)
    for _, c in ipairs(adds) do
        if c[5]:sub(1, 1) == "&" and tonumber(c[5]:sub(2)) >= WINBASE then
            stack_merged = stack_merged + 1
            break
        end
    end
    return same(want, got)
end

-- 6a. counts: one add per vertical frame with every carousel at rest; a
-- paged carousel keeps its own overlay and splits the run around it.
do
    local rows = carousels(6, 400, 14, 22, 4)
    local function per_frame(win, hoffs)
        scene({}); settle()
        show_stack(rows, { win = win }, 0, hoffs)
        local per = {}
        for step = 1, 4 do
            local mark = #fake.log.commands
            scroll_to("home", step * 3)
            per[#per + 1] = (adds_since(mark))
        end
        return table.concat(per, ",")
    end
    ok(per_frame(true) == "1,1,1,1",
       "stacked carousels at rest cost one overlay-add per vertical frame",
       per_frame(true))
    local plain = per_frame(false)
    ok(plain ~= "1,1,1,1",
       "without windows the same frames cost one add per carousel (" .. plain .. ")")
    local paged = per_frame(true, { [3] = 40 })
    local worst = 0
    for n in paged:gmatch("%d+") do worst = math.max(worst, tonumber(n)) end
    ok(worst >= 2 and worst <= 3,
       "a carousel paged sideways keeps its own overlay; the rows either side "
       .. "still merge (" .. paged .. ")")
end

-- 6b. pixels: deterministic cases.
do
    local cases = {
        { "carousels at rest", carousels(6, 400, 14, 22, 4), {}, 9, nil },
        { "a short carousel (narrower than the window)",
          carousels(5, 400, 14, 22, 4, { [2] = 60 }), {}, 7, nil },
        { "one carousel paged", carousels(6, 400, 14, 22, 4), {}, 9, { [3] = 40 } },
        { "every carousel at a different offset", carousels(6, 400, 14, 22, 4),
          {}, 9, { 0, 13, 0, 200, 1, 0 } },
        { "two carousels paged identically", carousels(6, 400, 14, 22, 4),
          {}, 9, { [2] = 30, [3] = 30 } },
        { "a carousel paged just inside the copied columns",
          carousels(5, 400, 14, 22, 4), {}, 3, { [2] = 2 } },
        { "top and bottom partial rows", carousels(9, 400, 20, 27, 0), {}, 31.5, nil },
        { "fractional vertical offset", carousels(7, 400, 14, 22, 4), {}, 9.25, nil },
        { "a hover chip on a row", carousels(6, 400, 14, 22, 4), { chip = 2 }, 5, nil },
        { "a chip on a paged row", carousels(6, 400, 14, 22, 4), { chip = 2 }, 5,
          { [2] = 25 } },
        { "overlay-mode page arrows", carousels(6, 400, 14, 22, 4), { arrows = 3 }, 5, nil },
        { "a dialog over the middle", carousels(7, 400, 14, 22, 4),
          { extra = { { id = "dlg", t = "layer", x = 40, y = 30, w = 60, h = 40 } } },
          4, nil },
        { "a full-width dialog across a row boundary", carousels(7, 400, 14, 22, 4),
          { extra = { { id = "dlg", t = "layer", x = -10, y = 31, w = 200, h = 9 } } },
          0, nil },
        { "an occluder across the stack", carousels(7, 400, 14, 22, 4),
          { extra = { { id = "oc", t = "occ", sc = "home", x = 0, y = 40, w = 160, h = 9 } } },
          4, nil },
        { "a clipped viewport", carousels(9, 400, 14, 22, 0),
          { sx = 10, sy = 13.5, vw = 100, vh = 70 }, 17.5, { [4] = 33 } },
        -- The one shape where only "same columns" (b.wsx == a.wsx) keeps two
        -- rows apart: carousels barely wider than the view, scrolled to
        -- the two last positions of their few-pixel range. Both start at
        -- the clip's left edge, end at their own right edge, and stay
        -- inside the copied columns -- one column apart.
        { "two short carousels one column apart", carousels(4, 45, 14, 22, 4),
          { vw = 50 }, 3, { [2] = 6, [3] = 7 } },
    }
    for _, c in ipairs(cases) do
        local s, at = compare_stack(c[2], c[3], c[4], c[5])
        ok(s, "stacked carousels, same pixels with and without windows: " .. c[1],
           at and ("first difference at pixel " .. at))
    end
    ok(stack_merged >= 8, "and those cases did merge (" .. stack_merged .. ")")
end

-- 6c. a carousel moving sideways while the page scrolls vertically: every
-- frame of the interleaved sequence draws what the rows draw on their own.
do
    local rows = carousels(7, 400, 14, 22, 4)
    local steps = { { "home", 3 }, { "c2", 10 }, { "home", 7 }, { "c2", 35 },
                    { "home", 11.5 }, { "c4", 60 }, { "home", 16 }, { "c2", 0 },
                    { "home", 20 }, { "c4", 0 }, { "home", 24 } }
    local function run(win)
        scene({}); settle()
        scene(stack(rows, { win = win })); settle()
        local frames = {}
        for _, s in ipairs(steps) do
            scroll_to(s[1], s[2])
            frames[#frames + 1] = composite()
        end
        return frames
    end
    local a, b = run(false), run(true)
    local bad
    for i = 1, #steps do
        local s, at = same(a[i], b[i])
        if not s then bad = bad or ("step " .. i .. " at pixel " .. tostring(at)) end
    end
    ok(bad == nil, "a carousel paged during vertical scrolling: every frame matches", bad)
end

-- 6d. random sweep over geometry, vertical and per-carousel offsets, chips,
-- arrows, dialogs and occluders.
do
    math.randomseed(5151)
    local fr = { 0, 0.5, 0.25, 0.75 }
    local bad, first
    stack_merged = 0
    local reads0 = bad_reads
    for trial = 1, 200 do
        local n = math.random(2, 7)
        local ih = math.random(4, 18)
        local pitch = ih + math.random(1, 9)            -- heading in the gap
        local iw = math.random(30, 420)
        local widths = {}
        if math.random() < 0.3 then widths[math.random(1, n)] = math.random(10, 120) end
        local rows = carousels(n, iw, ih, pitch, math.random(-10, 20), widths)
        local opts = { sx = math.random(0, 6), sy = math.random(0, 6) + fr[math.random(#fr)],
                       vw = math.random(60, SW), vh = math.random(30, SH) }
        local r = math.random()
        if r < 0.15 then
            opts.chip = math.random(1, n); opts.chip_dy = math.random(-4, ih)
        elseif r < 0.25 then
            opts.arrows = math.random(1, n)
        elseif r < 0.4 then
            opts.extra = { { id = "pop", t = "layer", x = math.random(0, 120),
                y = math.random(0, 100), w = math.random(5, 60), h = math.random(3, 40) } }
        elseif r < 0.5 then
            opts.extra = { { id = "oc", t = "occ", sc = "home", x = math.random(0, 120),
                y = math.random(0, 100) + fr[math.random(#fr)],
                w = math.random(5, 160), h = math.random(2, 30) } }
        end
        local hoffs = {}
        for k = 1, n do
            if math.random() < 0.35 then hoffs[k] = math.random(0, 300) end
        end
        local voff = math.random(0, 80) + fr[math.random(#fr)]
        local s, at = compare_stack(rows, opts, voff, hoffs)
        if not s then
            bad = (bad or 0) + 1
            first = first or string.format("trial %d n=%d ih=%d pitch=%d iw=%d voff=%s at %s",
                trial, n, ih, pitch, iw, tostring(voff), tostring(at))
        end
    end
    ok(bad == nil, "stacked-carousel sweep: every windowed frame matches the per-row picture",
       first)
    ok(stack_merged > 60, "and the sweep exercised merging (" .. stack_merged .. " frames)")
    ok(bad_reads == reads0, "no stacked overlay ever read outside one bitmap ("
       .. (bad_reads - reads0) .. ")")
end

-- 6e. input: hit-testing and hover over stacked carousels are the same with
-- and without the window.
do
    local function events_for(win)
        local rows = carousels(5, 400, 14, 22, 4)
        scene({}); settle()
        show_stack(rows, { win = win }, 6, { [2] = 20 })
        fake.reset_events()
        for _, p in ipairs({ { 10, 5 }, { 10, 30 }, { 12, 47 }, { 90, 30 } }) do
            fake.observe("mouse-pos", { x = p[1], y = p[2], hover = true })
            settle()
            fake.key("mbtn_left")
            settle()
        end
        local out = {}
        for _, e in ipairs(fake.log.events) do
            if type(e) == "table" then
                out[#out + 1] = tostring(e.t) .. ":" .. tostring(e.id)
            end
        end
        return table.concat(out, " ")
    end
    local a, b = events_for(false), events_for(true)
    ok(a == b and #a > 0, "stacked carousels: clicks and hover resolve the same nodes",
       "\n  plain:    " .. a .. "\n  windowed: " .. b)
end

-- ============================================================ 8. mixed widths
-- Real carousels are not one width: a row narrower than the view is laid out
-- only as wide as its content (Run D: requiring one width refused every real
-- Home page). Each row's copy is what ITS viewport shows, so nothing its clip
-- hides is in the window, and the picture is the per-row one.
do
    -- a short My Media row first, then posters, a short section, posters
    local function home()
        local rows = carousels(6, 400, 14, 22, 4, { [1] = 70, [4] = 40 })
        return rows
    end
    local function per_frame(opts, hoffs)
        scene({}); settle()
        show_stack(home(), opts, 0, hoffs)
        local per = {}
        for step = 1, 4 do
            local mark = #fake.log.commands
            scroll_to("home", step * 3)
            per[#per + 1] = (adds_since(mark))
        end
        return table.concat(per, ",")
    end
    local merged = per_frame({ win = true, fit = true })
    local plain = per_frame({ win = false, fit = true })
    ok(merged == "1,1,1,1",
       "mixed-width carousels at rest cost one overlay-add per vertical frame ("
       .. merged .. "; without windows " .. plain .. ")")

    local narrow = carousels(5, 400, 14, 22, 4)
    narrow[3].view = 60                 -- a strip wider than a narrower view
    local cases = {
        { "at rest", home(), { fit = true }, 9 },
        { "a short row in the middle, partial rows", home(), { fit = true }, 31.5 },
        { "a strip wider than its own narrower viewport", narrow, {}, 5 },
        { "that row paged inside its narrow viewport", narrow, {}, 5, { [3] = 30 } },
        { "a poster row paged", home(), { fit = true }, 9, { [2] = 40 } },
        { "a chip on the short row", home(), { fit = true, chip = 1 }, 0 },
        { "a chip on a poster row", home(), { fit = true, chip = 2 }, 5 },
        { "overlay arrows on a poster row", home(), { fit = true, arrows = 3 }, 5 },
        { "a dialog over the stack", home(),
          { fit = true, extra = { { id = "dlg", t = "layer", x = 30, y = 20, w = 70, h = 50 } } }, 4 },
        { "a full-width dialog across a row boundary", home(),
          { fit = true, extra = { { id = "dlg", t = "layer", x = -10, y = 31, w = 200, h = 9 } } }, 0 },
        { "a clipped page", home(), { fit = true, sx = 10, sy = 13.5, vw = 100, vh = 70 }, 17.5 },
    }
    for _, c in ipairs(cases) do
        local s, at = compare_stack(c[2], c[3], c[4], c[5])
        ok(s, "mixed-width carousels, same pixels with and without windows: " .. c[1],
           at and ("first difference at pixel " .. at))
    end

    math.randomseed(7272)
    local bad, first
    local reads0, merged0 = bad_reads, stack_merged
    for trial = 1, 200 do
        local n = math.random(2, 7)
        local ih = math.random(4, 18)
        local widths = {}
        for k = 1, n do
            if math.random() < 0.4 then widths[k] = math.random(10, 140) end
        end
        local rws = carousels(n, math.random(30, 420), ih, ih + math.random(1, 9),
                              math.random(-10, 20), widths)
        local opts = { fit = true, sx = math.random(0, 6),
                       sy = math.random(0, 6) + 0.5 * math.random(0, 1),
                       vw = math.random(60, SW), vh = math.random(30, SH) }
        for k = 1, n do
            if math.random() < 0.15 then rws[k].view = math.random(20, opts.vw) end
        end
        local hoffs = {}
        for k = 1, n do
            if math.random() < 0.25 then hoffs[k] = math.random(0, 300) end
        end
        local r = math.random()
        if r < 0.15 then opts.chip = math.random(1, n)
        elseif r < 0.25 then opts.arrows = math.random(1, n)
        elseif r < 0.4 then
            opts.extra = { { id = "pop", t = "layer", x = math.random(0, 120),
                y = math.random(0, 100), w = math.random(5, 60), h = math.random(3, 40) } }
        end
        local s, at = compare_stack(rws, opts, math.random(0, 80) + 0.25 * math.random(0, 3), hoffs)
        if not s then
            bad = (bad or 0) + 1
            first = first or string.format("trial %d n=%d at %s", trial, n, tostring(at))
        end
    end
    ok(bad == nil, "mixed-width sweep: every windowed frame matches the per-row picture", first)
    ok(stack_merged - merged0 > 60, "and the sweep exercised merging ("
       .. (stack_merged - merged0) .. " frames)")
    ok(bad_reads == reads0, "and nothing read outside one bitmap ("
       .. (bad_reads - reads0) .. ")")
end

print("1.." .. n_tests)
os.exit(failed == 0 and 0 or 1)
