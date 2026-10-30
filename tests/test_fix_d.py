"""Team D (UI) regression tests for Debrief 2.2.2: review page, library/prep/team pages, app window.

    python3 tests/test_fix_d.py

Browser tests use Playwright + Chromium and are skipped (not failed) when either is missing. Nothing here
talks to Garage 61: review pages are built from synthetic laps, and the app window runs against a fake API
served through Playwright routes.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import engine, report  # noqa: E402
from pw_settle import settle  # noqa: E402

UI = ROOT / "debrief" / "desktop" / "ui"
TPL = ROOT / "debrief" / "templates"
XSS = '<img src=x onerror="window.__x=(window.__x||0)+1">'


# ================================================================ fixtures
def _race(n_laps=18, multiclass_traffic=False, with_ref=False, seed=3):
    """A synthetic 3.6 km race with seven corners (the same shape as the review's sample generator)."""
    rng = np.random.default_rng(seed)
    L, n = 3600.0, 1800
    corners = [(300, 30, 3), (760, 22, 2), (1250, 35, 3), (1700, 18, 2), (2300, 40, 4), (2900, 26, 3), (3400, 20, 2)]
    sd_v, sd_b = [0.4, 0.5, 3.5, 0.4, 2.2, 0.6, 0.4], [3, 3, 4, 3, 18, 3, 3]

    def lap_csv(slow=None):
        p = np.linspace(0, 0.9995, n); d = p * L
        v = np.full(n, 62.0); b = np.zeros(n); th = np.full(n, 100.0); gear = np.full(n, 5)
        for i, (apex, depth, gr) in enumerate(corners):
            dep = depth + rng.normal(0, sd_v[i]) + (slow[1] if slow and slow[0] == i else 0)
            v -= dep * np.exp(-((d - apex) / 90.0) ** 2)
            bz = (d > apex - 140 - rng.normal(0, sd_b[i])) & (d < apex - 10)
            b[bz] = 85; th[(d > apex - 150) & (d < apex + 40)] = 0
            gear[np.abs(d - apex) < 120] = gr
        lat = 35.35 + 0.004 * np.sin(2 * np.pi * p); lon = -80.68 + 0.006 * np.cos(2 * np.pi * p)
        rows = ["Speed,LapDistPct,Brake,Throttle,Gear,LatAccel,LongAccel,SteeringWheelAngle,RPM,Lat,Lon"]
        rows += [f"{a:.3f},{q:.5f},{bb/100:.3f},{t/100:.3f},{g},0,0,0,7000,{la:.6f},{lo:.6f}"
                 for a, q, bb, t, g, la, lo in zip(v, p, b, th, gear, lat, lon)]
        lapt = float(np.sum(np.diff(d, append=L) / (np.maximum(v, 5) * 0.44704 * 1.6)))
        return "\n".join(rows), lapt

    laps, tel = [], {}
    for k in range(1, n_laps + 1):
        csv, t = lap_csv((k % 7, 3.0) if k % 4 == 0 else None)
        laps.append({"id": f"L{k}", "lapTime": t + (1.6 if k == 1 else 0), "lapNumber": k, "clean": k not in (6, 13),
                     "_start": 1.79e9 + k * 86})
        tel[f"L{k}"] = csv
    refs, rtel = [], {}
    if with_ref:
        csv, t = lap_csv()
        refs = [{"id": "R1", "lapTime": t - 0.6, "driver": {"firstName": "Jake", "lastName": "Morrow"},
                 "_main": True, "_role": "teammate"}]
        rtel = {"R1": csv}
    meta = {"driver": "Test Driver", "event_id": "EV1", "car": "Ford Mustang GT4",
            "track": "Charlotte Motor Speedway (Roval 2019)", "started_at": "2026-10-27T01:00:00+00:00",
            "session": "Race", "salutation": "", "generated_at": "2026-10-27T02:00:00+00:00"}
    data = engine.analyze(meta, laps, tel, refs, rtel, track_length=3.6)
    if multiclass_traffic:
        data["counts"]["traffic"] = [9, 16]
        for x in data["laps"]:
            if x["n"] in (9, 16):
                x["clean"] = False
    return data


def _with_new_insights(data):
    """The 2.2.2 insights fields agreed with team A (fix.numbers / fix.times, up to 5 fixes, detail/laps/
    corners on habits), added here so these tests don't depend on insights.py having them yet."""
    from debrief import insights
    d = copy.deepcopy(data)
    d["insights"] = insights.build(d)
    tech = {t["id"]: t for t in d.get("technique") or []}
    cs = {c["id"]: c for c in d["corners"]}
    fix = d["insights"]["fix"]
    for c in sorted(cs.values(), key=lambda c: -(c.get("avail") or 0)):
        if len(fix) >= 5:
            break
        if c["id"] not in {f["corner"] for f in fix}:
            fix.append({"corner": c["id"], "gain": round(max(c["avail"], 0.06), 2), "basis": "own", "tips": [],
                        "why": "Your typical lap loses time here.", "best_lap": c["best_lap"]})
    keys = ("brake_on", "min_mph", "exit_mph", "full_thr", "coast_s", "gear")
    for f in fix:
        t = tech.get(f["corner"]) or {}
        pick = lambda m: {k: m.get(k) for k in keys} if m else None  # noqa: E731
        f["numbers"] = {"best": pick(t.get("best")), "typical": pick(t.get("typical")), "ref": pick(t.get("ref"))}
        c = cs[f["corner"]]
        f["times"] = {"best_t": c["best_t"], "typical_t": c["median_t"], "p25_t": c["p25_t"], "ref_t": None}
    for it in d["insights"]["bad"]:
        if it["key"] == "unclean":
            it["detail"] = "Garage 61 flagged these laps for an incident or track limits."
            it["laps"] = list(d["counts"]["unclean"])
        if it["key"] == "vary":
            it["corners"] = ["C1", "C2"]
    return d


def _page(data, folder: Path, name="r") -> Path:
    o = folder / name
    o.mkdir(parents=True, exist_ok=True)
    p = o / "index.html"
    p.write_text(report.fill((TPL / "review.html").read_text(encoding="utf-8"), {"/*DATA*/null": report._embed(data)}),
                 encoding="utf-8")
    return p


class Tmp:
    def __enter__(self):
        self.p = Path(tempfile.mkdtemp(prefix="debrief-fixd-"))
        return self.p

    def __exit__(self, *a):
        shutil.rmtree(self.p, ignore_errors=True)


_BASE = {}


def base():
    if "d" not in _BASE:
        _BASE["d"] = _race()
    return copy.deepcopy(_BASE["d"])


# ================================================================ browser helper
_PW = {}


def _browser():
    """A shared headless Chromium, or None (test skipped) when Playwright or its browser is missing."""
    if "b" in _PW:
        return _PW["b"]
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b = pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        _PW["b"] = None
        return None
    _PW["pw"], _PW["b"] = pw, b
    return b


def _open(path, scheme="light", width=1280, init=None, wait=500):
    """Open a page; returns (page, context, errors) or None when no browser."""
    b = _browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error"
          and "fonts.g" not in (m.location or {}).get("url", "") and "ERR_" not in m.text
          and "could not be drawn" not in m.text else None)
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    if init:
        pg.add_init_script(init)
    pg.goto(Path(path).resolve().as_uri() if not str(path).startswith("http") else path)
    settle(pg, cap_ms=max(1500, 3 * wait))     # loaded, painted and quiet (was a fixed `wait` ms)
    return pg, ctx, errs


def _skip():
    return _browser() is None


# ================================================================ H1 / M4: one missing key never blanks the page
def test_h1_missing_keys_degrade_section_by_section():
    if _skip():
        return
    variants = {
        "no_traces": lambda d: d.__setitem__("traces", {}),
        "no_corners": lambda d: d.pop("corners"),
        "no_style": lambda d: d.pop("style"),
        "no_segments_refs_mismatch": lambda d: (d.__setitem__("refs", [{"key": "ref9", "name": "Ghost", "t": 90.0,
                                                                        "main": True}]),),
        "broken_heat": lambda d: d.__setitem__("heat", {"zones": ["C1"], "laps": [1, 2], "values": None}),
        "broken_stint": lambda d: d.__setitem__("stint", {"phases": "nope", "corner_drift": [{"id": "C1"}]}),
        "old_format": lambda d: [d.pop(k, None) for k in ("insights", "consistency", "technique", "best_worst", "stint",
                                                         "segments", "on_track", "conditions", "season", "pb")],
        "no_best_median": lambda d: (d.pop("best"), d.pop("median"), d.pop("trend_total")),
        "technique_garbage": lambda d: d.__setitem__("technique", [None, {"id": "C1"}, 5]),
    }
    with Tmp() as f:
        for name, mut in variants.items():
            d = base()
            mut(d)
            p = _page(d, f, name)
            r = _open(p)
            pg, ctx, errs = r
            info = pg.evaluate("""()=>({title:document.title, tiles:document.querySelectorAll('#stats .stat').length,
                lap:!!document.querySelector('#lapChart svg'), notes:document.querySelectorAll('#notes li').length,
                deepOpen:document.getElementById('deep').open, story:document.getElementById('story').textContent.length,
                bad:/NaN|undefined|Infinity|Invalid Date/.test(document.body.innerText)})""")
            ctx.close()
            assert not [e for e in errs if e.startswith("pageerror")], (name, errs)
            assert info["tiles"] >= 2 and info["notes"] >= 4 and info["story"] > 20, (name, info)
            assert info["lap"], (name, info)               # the lap-time chart survives every one of these
            assert info["title"] != "Race Review" and not info["bad"], (name, info)


def test_h1_no_telemetry_hides_tel_sections_with_a_note():
    if _skip():
        return
    with Tmp() as f:
        d = base()
        d["traces"] = {}
        pg, ctx, errs = _open(_page(d, f))
        info = pg.evaluate("""()=>({cmp:document.getElementById('compare').hidden, poss:document.getElementById('possible').hidden,
            note:document.getElementById('deep').innerText.includes('no telemetry'),
            jumpCompare:[...document.querySelectorAll('#jump a')].find(a=>a.getAttribute('href')==='#compare').hidden})""")
        ctx.close()
        assert info == {"cmp": True, "poss": True, "note": True, "jumpCompare": True}, info
        assert not errs, errs


def test_h1_a_throwing_section_shows_a_note_not_a_blank_page():
    """Force a section to throw (corner table) and check the page says so and the rest still draws."""
    if _skip():
        return
    with Tmp() as f:
        d = base()
        d["corners"][0]["best_t"] = {"oops": 1}          # .toFixed on an object would throw in the corner table
        d["corners"][0]["median_t"] = {"oops": 1}
        pg, ctx, errs = _open(_page(d, f))
        info = pg.evaluate("""()=>({fail:!document.getElementById('failNote').hidden,
            lap:!!document.querySelector('#lapChart svg'), tiles:document.querySelectorAll('#stats .stat').length})""")
        ctx.close()
        assert not [e for e in errs if e.startswith("pageerror")], errs
        assert info["lap"] and info["tiles"] >= 3, info


def test_d1_footer_without_generated_at():
    if _skip():
        return
    with Tmp() as f:
        d = base()
        d["meta"].pop("generated_at")
        pg, ctx, errs = _open(_page(d, f))
        txt = pg.evaluate("document.getElementById('notes').innerText")
        ctx.close()
        assert "Invalid Date" not in txt and "Generated by an older version" in txt, txt


# ================================================================ H2: one tab stop per chart, arrow keys inside
def test_h2_charts_are_single_tab_stops_with_arrow_keys():
    if _skip():
        return
    with Tmp() as f:
        d = _race(n_laps=40, seed=5)
        pg, ctx, errs = _open(_page(d, f), wait=700)
        n = pg.evaluate("""()=>({svg:[...document.querySelectorAll('svg [tabindex], svg[tabindex]')].length,
            rows:[...document.querySelectorAll('.split[tabindex],.drow[tabindex],.irow[tabindex],.ribbon [tabindex]')].length,
            total:[...document.querySelectorAll('[tabindex="0"],button,a[href],select,input')].filter(e=>e.getClientRects().length).length})""")
        assert n["svg"] == 0 and n["rows"] == 0, n
        assert n["total"] < 120, n                      # was ~2,000 on a long race
        # arrow keys inside the lap chart show the tooltip and announce it
        pg.focus("#lapChart")
        pg.keyboard.press("ArrowRight"); pg.keyboard.press("ArrowRight")
        r = pg.evaluate("({tip:!document.getElementById('tip').hidden, t:document.getElementById('tip').textContent, live:document.getElementById('srLive').textContent})")
        assert r["tip"] and r["t"].startswith("Lap 2") and "Lap 2" in r["live"], r
        # heat map: down moves to the next corner row on the same lap
        pg.focus("#heatChart")
        pg.keyboard.press("ArrowRight"); pg.keyboard.press("ArrowDown")
        t = pg.evaluate("document.getElementById('tip').textContent")
        assert t.startswith("Lap") and "C2" in t, t
        # Enter on a lap opens it in Compare
        pg.focus("#lapChart")
        pg.keyboard.press("Home"); pg.keyboard.press("ArrowRight"); pg.keyboard.press("ArrowRight"); pg.keyboard.press("Enter")
        pg.wait_for_timeout(100)
        a = pg.evaluate("document.getElementById('selA').value")
        ctx.close()
        assert a == "me3", a
        assert not errs, errs


# ================================================================ H3: no unescaped innerHTML sinks
def test_h3_salutation_fix_corner_and_names_are_escaped():
    if _skip():
        return
    with Tmp() as f:
        d = _with_new_insights(_race(with_ref=True))
        d["meta"]["salutation"] = XSS
        d["insights"]["fix"][0]["corner"] = "C1" + XSS
        d["insights"]["fix"][0]["why"] = XSS
        d["insights"]["bad"][0]["detail"] = XSS
        d["insights"]["bad"][0]["title"] = XSS
        d["refs"][0]["name"] = XSS
        d["segments"]["names"][0] = XSS
        d["meta"]["track"] = XSS
        d["insights"]["verdict"] = [XSS]
        pg, ctx, errs = _open(_page(d, f), init="window.__x=0", wait=700)
        pg.evaluate("document.querySelectorAll('button').forEach(b=>{if(/Show me|^\\d+$/.test(b.textContent))b.click()})")
        pg.wait_for_timeout(200)
        r = pg.evaluate("({x:window.__x||0, imgs:document.querySelectorAll('img').length, txt:document.body.innerText.includes('<img')})")
        ctx.close()
        assert r["x"] == 0 and r["imgs"] == 0 and r["txt"], r


def test_h3_review_template_has_no_data_innerhtml():
    """Static check: every innerHTML left in review.html is a fixed string or goes through esc()."""
    s = (TPL / "review.html").read_text(encoding="utf-8")
    for m in re.finditer(r"innerHTML=([^;]*)", s):
        expr = m.group(1)
        for piece in re.findall(r"\+\s*([A-Za-z_$][\w$.()\[\]']*)", expr):
            ok = (piece.startswith(("esc(", "fmt(", "ab(", "sg(", "sc", "main", "sub")) or piece in ("w",))
            assert ok, (piece, expr[:120])
    assert "$('story').innerHTML" not in s and "vv.innerHTML=v" not in s and "c.innerHTML=ctx" not in s


def test_h3_csp_blocks_api_calls_from_pages():
    for name in ("review.html", "library.html", "prep.html", "team.html"):
        s = (TPL / name).read_text(encoding="utf-8")
        assert "connect-src 'none'" in s and 'name="referrer" content="no-referrer"' in s, name


# ================================================================ owner's layout request
def test_layout_numbers_open_and_jump_links_always_shown():
    if _skip():
        return
    with Tmp() as f:
        d = _with_new_insights(base())
        pg, ctx, errs = _open(_page(d, f))
        r = pg.evaluate("""()=>({open:document.getElementById('deep').open,
            links:[...document.querySelectorAll('#jump a')].filter(a=>!a.hidden&&a.getClientRects().length).map(a=>a.getAttribute('href'))})""")
        assert r["open"], r
        for want in ("#compare", "#corners", "#technique", "#pace", "#consistency", "#habits"):
            assert want in r["links"], r
        pg.evaluate("document.querySelector('#deep > summary').click()")
        pg.wait_for_timeout(100)
        r2 = pg.evaluate("""()=>({open:document.getElementById('deep').open,
            links:[...document.querySelectorAll('#jump a')].filter(a=>!a.hidden&&a.getClientRects().length).length})""")
        # still collapsible, and the section links still show when folded; clicking one opens it again
        assert not r2["open"] and r2["links"] == len(r["links"]), r2
        pg.evaluate("document.querySelector('#jump a[href=\"#corners\"]').click()")
        pg.wait_for_timeout(150)
        assert pg.evaluate("document.getElementById('deep').open")
        ctx.close()
        assert not errs, errs


def test_layout_fix_cards_new_fields():
    if _skip():
        return
    with Tmp() as f:
        d = _with_new_insights(base())
        assert len(d["insights"]["fix"]) == 5
        # (2.2.2 integration: the card compares Typical with "Quicker laps" when insights carries numbers.quick,
        # the target the tips aim at, and with the best lap otherwise)
        nb = d["insights"]["fix"][0]["numbers"]
        nb["typical"]["brake_on"] = (nb.get("quick") or nb["best"])["brake_on"] - 20
        pg, ctx, errs = _open(_page(d, f), wait=700)
        r = pg.evaluate("""()=>{const c=[...document.querySelectorAll('#fixList .fix')];return {n:c.length,
            tables:c.filter(x=>x.querySelector('table.fnums')).length, sparks:c.filter(x=>x.querySelector('.spark svg')).length,
            hl:c[0].querySelectorAll('td.hl').length, brake:c[0].querySelector('table.fnums').innerText,
            times:c[0].querySelector('.ftimes').innerText}}""")
        ctx.close()
        assert r["n"] == 5 and r["tables"] == 5 and r["sparks"] == 5, r
        assert r["hl"] >= 2 and "20 m earlier" in r["brake"], r
        assert "Best" in r["times"] and "Typical" in r["times"] and "Better laps" in r["times"], r
        assert not errs, errs


def test_layout_fix_cards_old_reviews_fall_back_to_technique_and_corners():
    if _skip():
        return
    from debrief import insights
    with Tmp() as f:
        # 2.2.1 review: insights without numbers/times
        d = base()
        d["insights"] = insights.build(d)
        for x in d["insights"]["fix"]:
            x.pop("numbers", None); x.pop("times", None)
        pg, ctx, errs = _open(_page(d, f, "old221"), wait=600)
        r = pg.evaluate("[...document.querySelectorAll('#fixList .fix')].map(c=>!!c.querySelector('table.fnums')&&!!c.querySelector('.spark svg'))")
        ctx.close()
        assert r and all(r), r
        assert not errs, errs
        # pre-2.2.1 review: no insights at all -> fix cards worked out from corners
        d2 = base()
        d2.pop("insights", None)
        pg, ctx, errs = _open(_page(d2, f, "old2"), wait=600)
        r = pg.evaluate("({coach:!document.getElementById('coach').hidden, n:document.querySelectorAll('#fixList .fix').length})")
        ctx.close()
        assert not errs, errs
        assert r["n"] <= 5


def test_layout_habit_detail_and_clickable_laps():
    if _skip():
        return
    with Tmp() as f:
        d = _with_new_insights(base())
        pg, ctx, errs = _open(_page(d, f))
        r = pg.evaluate("""()=>{const li=[...document.querySelectorAll('#badList li')].find(x=>x.innerText.includes('Incidents'));
            return {det:li.querySelector('.det').textContent, laps:[...li.querySelectorAll('.src button')].map(b=>b.textContent),
            corners:[...document.querySelectorAll('#badList .src button')].map(b=>b.textContent).filter(t=>t.startsWith('C'))}}""")
        # 2.3 (23-A): the new brake_spread habit can add its own corner buttons after the "vary" ones
        want = [c for b in d["insights"]["bad"] for c in b.get("corners") or []]
        assert want[:2] == ["C1", "C2"], want
        assert "Garage 61 flagged" in r["det"] and r["laps"] == ["6", "13"] and r["corners"] == want, r
        pg.evaluate("[...document.querySelectorAll('#badList .src button')].find(b=>b.textContent==='13').click()")
        pg.wait_for_timeout(100)
        a = pg.evaluate("document.getElementById('selA').value")
        ctx.close()
        assert a == "me13", a
        assert not errs, errs


def test_layout_summary_tiles_and_story_under_verdict():
    if _skip():
        return
    with Tmp() as f:
        d = _with_new_insights(_race(with_ref=True))
        pg, ctx, errs = _open(_page(d, f))
        r = pg.evaluate("""()=>({tiles:[...document.querySelectorAll('#stats .stat .lab')].map(x=>x.textContent),
            story:!!document.querySelector('#coach #story'), txt:document.getElementById('story').textContent})""")
        ctx.close()
        assert r["tiles"] == ["Best lap", "Consistency", "Time to find", "Pace drift", "Gap to comparison"], r
        assert r["story"] and "You completed 18 full laps" in r["txt"], r
        assert not errs, errs


def test_layout_race_story_has_both_charts_phases_and_lap_kinds():
    if _skip():
        return
    with Tmp() as f:
        d = _with_new_insights(_race(multiclass_traffic=True))
        pg, ctx, errs = _open(_page(d, f), wait=700)
        r = pg.evaluate("""()=>{const R=document.getElementById('race');return {lap:!!R.querySelector('#lapChart svg'),
            stint:!!R.querySelector('#stintChart svg'), phases:R.querySelectorAll('.phase').length,
            legend:R.querySelector('#lapLegend').innerText, slegend:R.querySelector('#stintLegend').innerText,
            stintMarks:R.querySelectorAll('#stintChart svg rect[stroke="var(--bt)"]').length,
            lapMarks:R.querySelectorAll('#lapChart svg rect[stroke="var(--bt)"]').length}}""")
        ctx.close()
        assert r["lap"] and r["stint"] and r["phases"] == 3, r
        for k in ("Traffic", "Flagged by Garage 61", "Off-pace"):
            assert k in r["legend"] and k in r["slegend"], (k, r)
        assert r["stintMarks"] == 2 and r["lapMarks"] == 2, r       # laps 9 and 16 drawn as traffic squares
        assert not errs, errs


# ================================================================ M1: saved theme in every template (no flash)
def test_m1_templates_read_saved_theme_in_head():
    for name in ("review.html", "library.html", "prep.html", "team.html"):
        s = (TPL / name).read_text(encoding="utf-8")
        head = s.split("</head>", 1)[0]
        assert "localStorage.getItem('debrief-theme')" in head, name
    if _skip():
        return
    with Tmp() as f:
        p = _page(base(), f)
        pg, ctx, errs = _open(p, scheme="light", init="try{localStorage.setItem('debrief-theme','dark')}catch(e){}")
        bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        ctx.close()
        assert bg in ("rgb(14, 17, 20)",), bg


# ================================================================ M3: contrast
def _lum(hexs):
    c = [int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _cr(a, b):
    la, lb = sorted([_lum(a), _lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tokens(s, dark):
    blocks = re.findall(r":root\{(.*?)\}", s, re.S) if not dark else re.findall(r':root\[data-theme="dark"\]\{(.*?)\}', s, re.S)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", blocks[0]))


def test_m3_text_tokens_pass_aa():
    for name in ("review.html", "library.html", "prep.html", "team.html"):
        s = (TPL / name).read_text(encoding="utf-8")
        lt, dk = _tokens(s, False), _tokens(s, True)
        assert _cr(lt["panel"], lt["a"]) >= 4.5 and _cr(lt["bg"], lt["a"]) >= 4.5, name     # links / primary text
        assert _cr(lt["a"], lt["on-a"]) >= 4.5 and _cr(dk["a"], dk["on-a"]) >= 4.5, name     # text on primary buttons
        assert _cr(lt["panel"], lt["bt"]) >= 4.5 and _cr(dk["panel"], dk["bt"]) >= 4.5, name  # orange text
        assert _cr(lt["panel"], lt["warn"]) >= 4.5, name
    css = (UI / "app.css").read_text(encoding="utf-8")
    assert "color:var(--on-a,#fff)" in css and ".chip.mc{border-color:var(--ref);color:var(--bt" in css
    rv = (TPL / "review.html").read_text(encoding="utf-8")
    assert ".fix .rk{" in rv and "color:var(--bt)}" in rv.split(".fix .rk{", 1)[1].split("\n", 1)[0]


# ================================================================ M2: library / prep / team tooltips reachable by keyboard
def _lib_page(folder):
    from debrief import library
    sys.path.insert(0, str(ROOT / "tests"))
    import make_fixture_library
    make_fixture_library.main(str(folder), with_goals=True)
    return library.rebuild(folder)


def test_m2_library_charts_keyboard_tooltips():
    if _skip():
        return
    with Tmp() as f:
        page = _lib_page(f)
        b = _browser()
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        pg = ctx.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
        pg.goto(page.resolve().as_uri() + "#season")
        pg.wait_for_timeout(400)
        n = pg.evaluate("[...document.querySelectorAll('svg[tabindex=\"0\"]')].filter(s=>s.getBoundingClientRect().width>0).length")
        assert n >= 1, n
        pg.evaluate("[...document.querySelectorAll('svg[tabindex=\"0\"]')].find(s=>s.getBoundingClientRect().width>0).focus()")
        pg.wait_for_timeout(900)   # focusing smooth-scrolls the page, and the page hides tooltips on scroll
        pg.keyboard.press("ArrowRight")
        r = pg.evaluate("({tip:!document.getElementById('tip').hidden, live:document.getElementById('srLive').textContent})")
        ctx.close()
        assert r["tip"] and r["live"], r
        assert not errs, errs


def test_m2_team_and_prep_templates_wire_keytips():
    for name, need in (("team.html", 2), ("prep.html", 1), ("library.html", 3)):
        s = (TPL / name).read_text(encoding="utf-8")
        assert s.count("keyTips(") >= need + 1, (name, s.count("keyTips("))
        assert 'id="srLive"' in s


# ================================================================ M7 / L4: plan links, single render
def test_m7_plan_links_open_the_corner_and_l4_single_render():
    if _skip():
        return
    with Tmp() as f:
        d = base()
        d.pop("insights", None)
        d["plan"] = [{"title": "C1: something", "gain": "≈0.10s per lap", "text": "Brake later. More text."}]
        p = _page(d, f)
        pg, ctx, errs = _open(p)
        btn = pg.evaluate("[...document.querySelectorAll('#planList button')].map(b=>b.textContent)")
        assert btn == ["See C1 in Compare"], btn
        pg.evaluate("[...document.querySelectorAll('#planList button')][0].click()")
        pg.wait_for_timeout(100)
        z = pg.evaluate("document.getElementById('zoomInfo').textContent")
        assert "Showing" in z, z
        ctx.close()
    rv = (TPL / "review.html").read_text(encoding="utf-8")
    assert "DEEP.open=true;renderAll()" not in rv


# ================================================================ app window (fake API through Playwright routes)
def _app(api, width=1280):
    """Load the real app window (index.html + app.js + app.css) against a fake API dict path -> json."""
    b = _browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": width, "height": 860})
    pg = ctx.new_page(); errs = []; posts = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    base_url = "http://debrief.test/s3cret/"
    tokens = ":root{--bg:#f3f4f6;--panel:#fff;--fg:#161a1f;--fg2:#47505a;--muted:#68717b;--grid:#e3e6ea;--axis:#c4c9d0;--band:#f2f4f6;--me:#1f6cc4;--on-a:#fff;--warn:#946000;--good:#0a7a2f;--lose:#c43c3c;--ref:#eb6834;--bt:#b8461a}"

    def handle(route):
        url = route.request.url
        rel = url[len(base_url):].split("?")[0]
        if rel in ("", "index.html"):
            return route.fulfill(body=(UI / "index.html").read_text(encoding="utf-8"), content_type="text/html")
        if rel == "static/app.js":
            return route.fulfill(body=(UI / "app.js").read_text(encoding="utf-8"), content_type="text/javascript")
        if rel == "static/app.css":
            return route.fulfill(body=(UI / "app.css").read_text(encoding="utf-8"), content_type="text/css")
        if rel == "static/tokens.css":
            return route.fulfill(body=tokens, content_type="text/css")
        if rel.startswith("api/"):
            key = rel[4:]
            if route.request.method == "POST":
                try:
                    posts.append((key, json.loads(route.request.post_data or "{}")))
                except ValueError:
                    posts.append((key, None))
            v = api.get(key, {"ok": True})
            v = v(route.request) if callable(v) else v
            return route.fulfill(status=v.pop("_status", 200) if isinstance(v, dict) else 200,
                                 body=json.dumps(v), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    pg.goto(base_url)
    settle(pg)
    return pg, ctx, errs, posts


def _api_base():
    return {
        "settings": {"ok": True, "token_saved": True, "version": "2.2.2", "installed": True, "data_folder": "C:/x"},
        "reviews": {"ok": True, "folder": "C:/lib", "groups": [{"track": "Roval", "car": "GT4", "reviews": [
            {"dir": "2026-10-26_a", "date": "2026-10-26", "best": "1:37.018", "laps": 18, "session": "Race"}]}]},
        "sessions": {"ok": True, "token": True, "sessions": [], "summary": {"synced": True, "last_sync": "2026-10-28T10:00:00Z",
                     "laps": 10, "sessions": 2, "combos": 1, "warnings": ["Lap 7 of session 123 had no telemetry."]}},
        "status": {"state": "idle"}, "sync": {"ok": True, "skipped": True}, "update": {"ok": True, "skipped": True},
        "pages": {"ok": True, "prep": []}, "combos": {"ok": True},
    }


def test_app_sidebar_list_semantics_dates_and_current():
    r = _app(_api_base())
    if not r:
        return
    pg, ctx, errs, posts = r
    info = pg.evaluate("""()=>({role:document.getElementById('revlist').getAttribute('role'),
        li:document.querySelectorAll('#revlist ul > li > button.rev').length, listitem:document.querySelectorAll('[role=listitem]').length,
        d:document.querySelector('.rev .d').textContent, greet:document.getElementById('welcomeTitle').textContent})""")
    assert info["role"] is None and info["li"] == 1 and info["listitem"] == 0, info
    assert info["d"] == "Oct 26, 2026", info                         # L3
    assert "sir" not in info["greet"].lower(), info                   # L6
    pg.evaluate("document.querySelector('.navbtn[data-nav=settings]').click()")
    pg.wait_for_timeout(100)
    cur = pg.evaluate("[...document.querySelectorAll('[aria-current=page]')].map(b=>b.dataset.nav)")
    ctx.close()
    assert cur == ["settings"], cur                                   # L2
    assert not errs, errs


def test_app_radio_groups_roving_tabindex_and_arrows():
    r = _app(_api_base())
    if not r:
        return
    pg, ctx, errs, posts = r
    t = pg.evaluate("[...document.querySelectorAll('[data-theme-set]')].map(b=>b.tabIndex)")
    assert sorted(t) == [-1, -1, 0], t
    pg.focus("[data-theme-set][tabindex='0']")
    pg.keyboard.press("ArrowRight")
    st = pg.evaluate("({theme:localStorage.getItem('debrief-theme'), focus:document.activeElement.dataset.themeSet})")
    assert st["theme"] == st["focus"] and st["theme"] in ("light", "dark"), st
    pg.evaluate("document.querySelector('.navbtn[data-nav=sessions]').click()")
    pg.wait_for_timeout(200)
    pg.focus("#sessFilter [tabindex='0']")
    pg.keyboard.press("ArrowRight")
    f = pg.evaluate("[...document.querySelectorAll('#sessFilter [data-f]')].map(b=>[b.dataset.f,b.getAttribute('aria-checked'),b.tabIndex])")
    ctx.close()
    assert f[1] == ["3", "true", 0] and f[0][1] == "false", f
    assert not errs, errs


def test_app_narrow_window_collapses_sidebar():
    r = _app(_api_base(), width=600)
    if not r:
        return
    pg, ctx, errs, posts = r
    s = pg.evaluate("""()=>({vis:getComputedStyle(document.querySelector('.side')).visibility, btn:getComputedStyle(document.getElementById('btnMenu')).display,
        mainW:document.getElementById('main').getBoundingClientRect().width})""")
    assert s["vis"] == "hidden" and s["btn"] != "none" and s["mainW"] > 560, s
    pg.click("#btnMenu"); pg.wait_for_timeout(300)
    v = pg.evaluate("[getComputedStyle(document.querySelector('.side')).visibility, document.getElementById('btnMenu').getAttribute('aria-expanded')]")
    pg.keyboard.press("Escape"); pg.wait_for_timeout(50)
    v2 = pg.evaluate("document.body.classList.contains('navopen')")
    ctx.close()
    assert v == ["visible", "true"] and v2 is False, (v, v2)
    assert not errs, errs


def test_app_sync_warnings_shown():
    r = _app(_api_base())
    if not r:
        return
    pg, ctx, errs, posts = r
    strip = pg.evaluate("({t:document.getElementById('syncText').textContent, title:document.getElementById('syncStrip').title})")
    assert "1 warning" in strip["t"] and "no telemetry" in strip["title"], strip
    pg.evaluate("document.querySelector('.navbtn[data-nav=sessions]').click()")
    pg.wait_for_timeout(200)
    w = pg.evaluate("({h:document.getElementById('sessWarn').hidden, t:document.getElementById('sessWarn').innerText})")
    ctx.close()
    assert not w["h"] and "no telemetry" in w["t"], w
    assert not errs, errs


def test_app_no_warnings_field_degrades():
    api = _api_base()
    api["sessions"]["summary"].pop("warnings")
    r = _app(api)
    if not r:
        return
    pg, ctx, errs, posts = r
    pg.evaluate("document.querySelector('.navbtn[data-nav=sessions]').click()")
    pg.wait_for_timeout(200)
    w = pg.evaluate("[document.getElementById('sessWarn').hidden, document.getElementById('syncText').textContent]")
    ctx.close()
    assert w[0] is True and "warning" not in w[1], w
    assert not errs, errs


def _prep_api(card_updated="2026-10-20T10:00:00Z", schedule_warnings=None, conflict=False):
    api = _api_base()
    sched = {"season": "2026 S4", "current_week": 7, "series": 40, "weeks": 12, "first_day": "2026-09-01", "last_day": "2026-11-24"}
    if schedule_warnings is not None:
        sched["warnings"] = schedule_warnings
    wk = {"week": 7, "start": "2026-10-27", "track": "Road Atlanta", "has_notes": False, "history": {}, "track_id": 1, "car_id": 2}
    api["prepare"] = {"ok": True, "schedule": sched, "chosen": True, "mine": [{"id": "s1", "name": "GT4 Challenge", "cars": ["GT4"], "this_week": wk}]}
    api["notes"] = lambda req: ({"ok": True, "card": {"track": "Road Atlanta", "fields": {"lap1": "x"}, "car_notes": {},
                                                      "updated": card_updated}} if req.method == "GET" else
                                ({"ok": False, "error": "This track card was changed somewhere else after you opened it. "
                                  "Close it and open it again to see the newer notes, then make your change.", "_status": 400}
                                 if conflict else {"ok": True}))
    return api


def test_app_track_card_sends_base_updated_and_handles_conflict():
    r = _app(_prep_api(conflict=True))
    if not r:
        return
    pg, ctx, errs, posts = r
    pg.evaluate("document.querySelector('.navbtn[data-nav=prep]').click()")
    pg.wait_for_timeout(300)
    pg.evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='Add track card').click()")
    pg.wait_for_timeout(300)
    pg.evaluate("document.getElementById('cardSave').click()")
    pg.wait_for_timeout(300)
    sent = [b for k, b in posts if k == "notes"]
    st = pg.evaluate("({msg:document.getElementById('cardMsg').textContent, reload:!document.getElementById('cardReload').hidden, open:document.getElementById('dlgCard').open})")
    assert sent and sent[-1]["card"]["base_updated"] == "2026-10-20T10:00:00Z", sent
    assert "changed somewhere else" in st["msg"] and st["reload"] and st["open"], st
    pg.evaluate("document.getElementById('cardReload').click()")
    pg.wait_for_timeout(200)
    st2 = pg.evaluate("({reload:!document.getElementById('cardReload').hidden, lap1:document.getElementById('c_lap1').value, dis:document.getElementById('cardSave').disabled})")
    ctx.close()
    assert not st2["reload"] and st2["lap1"] == "x" and not st2["dis"], st2
    assert not errs, errs


def test_app_new_track_card_sends_null_base_and_schedule_warnings():
    r = _app(_prep_api(card_updated=None, schedule_warnings=["Week 9 track name couldn't be read."]))
    if not r:
        return
    pg, ctx, errs, posts = r
    pg.evaluate("document.querySelector('.navbtn[data-nav=prep]').click()")
    pg.wait_for_timeout(300)
    w = pg.evaluate("({h:document.getElementById('schWarn').hidden, t:document.getElementById('schWarn').innerText})")
    assert not w["h"] and "Week 9" in w["t"], w
    pg.evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='Add track card').click()")
    pg.wait_for_timeout(300)
    pg.evaluate("document.getElementById('cardSave').click()")
    pg.wait_for_timeout(300)
    sent = [b for k, b in posts if k == "notes"]
    closed = pg.evaluate("!document.getElementById('dlgCard').open")
    ctx.close()
    assert sent and "base_updated" in sent[-1]["card"] and sent[-1]["card"]["base_updated"] is None, sent
    assert closed
    assert not errs, errs


def test_app_l8_check_button_reenabled_on_failure_and_l9_titles():
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "catch(e){$('btnCheck').disabled=false;" in js
    assert "TITLES.review=s.title||TITLES0.review" in js and "TITLES0={" in js


# ================================================================ runner
if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    if _PW.get("b"):
        _PW["b"].close(); _PW["pw"].stop()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
