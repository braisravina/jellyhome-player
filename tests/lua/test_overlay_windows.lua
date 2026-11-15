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

print("1.." .. n_tests)
os.exit(failed == 0 and 0 or 1)
