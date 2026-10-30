"""Workstream 23-C (Debrief 2.3 pages): review.html time-lost map, where in the corner, deeper technique,
brake-point spread, practice/qualifying; library.html Skills tab.

    python3 tests/test_23_c.py

Fixtures are in tests/fixtures_23_c/ (rebuild with tests/fixtures_23_c/make_samples.py; a_*.json come from
23-A, b_*.json from 23-B). Browser checks use Playwright + Chromium and print SKIPPED when it isn't
installed; the static checks always run. Nothing here talks to Garage 61.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import report  # noqa: E402
from pw_settle import settle  # noqa: E402

TPL = ROOT / "debrief" / "templates"
FX = Path(__file__).resolve().parent / "fixtures_23_c"
XSS = '<img src=x onerror="window.__x=(window.__x||0)+1">'


def fx(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


class Tmp:
    def __enter__(self):
        self.p = Path(tempfile.mkdtemp(prefix="debrief-23c-"))
        return self.p

    def __exit__(self, *a):
        shutil.rmtree(self.p, ignore_errors=True)


def _page(data, folder: Path, name="r") -> Path:
    p = folder / f"{name}.html"
    p.write_text(report.fill((TPL / "review.html").read_text(encoding="utf-8"), {"/*DATA*/null": report._embed(data)}),
                 encoding="utf-8")
    return p


def _lib(rows, extra, folder: Path, name="lib") -> Path:
    p = folder / f"{name}.html"
    p.write_text(report.render_library(rows, extra), encoding="utf-8")
    return p


_PW = {}


def _browser():
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


def _open(path, scheme="light", width=1280, hash_="", init=None, wait=450):
    b = _browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error"
          and "fonts.g" not in (m.location or {}).get("url", "") and "ERR_" not in m.text else None)
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    if init:
        pg.add_init_script(init)
    pg.goto(Path(path).resolve().as_uri() + hash_)
    settle(pg, cap_ms=max(1500, 3 * wait))     # loaded, painted and quiet (was a fixed `wait` ms)
    return pg, ctx, errs


def _skip():
    return _browser() is None


STATE = """()=>({fail:!document.getElementById('failNote').hidden,
  toggle:!document.getElementById('mapMode').hidden,
  wic:!document.getElementById('wicPanel').hidden, wicLede:document.getElementById('wicLede').textContent,
  wicRows:document.querySelectorAll('#wicRows .wicrow').length,
  session:!document.getElementById('session').hidden,
  spread:[...document.querySelectorAll('#cornerTable th')].some(t=>/spread/i.test(t.textContent)),
  tp:document.querySelectorAll('#techTable tr.tpsub').length,
  tech:document.getElementById('techTable').innerText,
  raceTitle:document.getElementById('raceTitle').textContent,
  jump:[...document.querySelectorAll('#jump a')].filter(a=>!a.hidden).map(a=>a.textContent),
  bad:/NaN|undefined|Infinity|Invalid Date|\\[object/.test(document.body.innerText)})"""


# ================================================================ static checks (always run)
def test_static_new_ids_and_guards():
    s = (TPL / "review.html").read_text(encoding="utf-8")
    for i in ("mapMode", "lossKey", "wicPanel", "wicRows", "session", "sessRuns", "sessTargets", "sessQuali",
              "raceTitle", 'href="#session"'):
        assert i in s, i
    # every 2.3 key is read through a guard (missing -> hidden)
    assert "isObj(D.skills)?D.skills:null" in s
    assert "isObj(D.technique_plus)&&Array.isArray(D.technique_plus.corners)" in s
    assert "isObj(D.brake_spread)&&Array.isArray(D.brake_spread.corners)" in s
    assert "isObj(D.session_review)" in s
    assert "TP.channels.steering===true" in s           # steering rows only when the channel is really there
    # each new section runs inside safe()
    for name in ("time-lost map", "where in the corner", "practice and qualifying", "technique detail", "map colour toggle"):
        assert f"safe('{name}'" in s, name
    lib = (TPL / "library.html").read_text(encoding="utf-8")
    assert 'data-tab="skills"' in lib and 'id="skills"' in lib and "'skills'" in lib
    assert "EX.skills&&typeof EX.skills==='object'" in lib
    assert "Skill scores appear after a race with at least 6 clean laps." in lib


def test_static_no_new_colours_and_no_data_innerhtml():
    s = (TPL / "review.html").read_text(encoding="utf-8")
    a, b = s.index("/* 2.3: time-lost map"), s.index("/* v2: stint */")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", s[a:b]), "2.3 CSS must use the design tokens only"
    lib = (TPL / "library.html").read_text(encoding="utf-8")
    a, b = lib.index("/* 2.3: skills tab */"), lib.index(".lib .gbarbox{")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", lib[a:b])
    # the 2.3 script blocks build DOM nodes only
    for blk in ("// ---------- 2.3: \"Where time goes\"", "// ---------- 2.3: deeper technique",
                "// ---------- 2.3: where in the corner", "// ---------- 2.3: practice and qualifying"):
        i = s.index(blk)
        j = s.find("// ----------", i + 20)
        assert "innerHTML" not in s[i:j], blk
    i = lib.index("// ================================================================ Skills tab")
    j = lib.index("// ================================================================ tabs")
    assert "innerHTML" not in lib[i:j]


def test_static_coaching_words_correlational():
    s = (TPL / "review.html").read_text(encoding="utf-8") + (TPL / "library.html").read_text(encoding="utf-8")
    for blk in ("// ---------- 2.3", "Skills tab"):
        for m in re.finditer(re.escape(blk), s):
            seg = s[m.start():m.start() + 6000]
            for w in (" will gain", "costing you", " because "):
                assert w not in seg, (w, blk)


# ================================================================ review.html in the browser
def test_new_review_all_keys():
    if _skip():
        return
    with Tmp() as f:
        for name in ("sample_review_new.json", "sample_review_a.json"):
            pg, ctx, errs = _open(_page(fx(name), f))
            st = pg.evaluate(STATE)
            assert not errs, (name, errs)
            assert st["toggle"] and st["wic"] and st["wicRows"] == 4 and not st["session"], (name, st)
            assert st["spread"] and st["tp"] == 1 and "Brake release length" in st["tech"], (name, st)
            assert not st["fail"] and not st["bad"], (name, st)
            assert "Runs" not in st["jump"] and "Race story" in st["jump"], st
            ctx.close()


def test_time_lost_map_mode_colours_and_hover():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_new.json"), f))
        ab = pg.evaluate("[...document.querySelectorAll('#trackmap svg g line')].map(l=>l.getAttribute('stroke'))")
        assert any("--a" in c or "--b" in c for c in ab), "A/B colouring is the default"
        assert pg.evaluate("document.querySelector('#mapMode [data-mode=ab]').getAttribute('aria-pressed')") == "true"
        pg.click("#mapMode [data-mode=loss]")
        pg.wait_for_timeout(100)
        cols = pg.evaluate("[...document.querySelectorAll('#trackmap svg g line')].map(l=>l.getAttribute('stroke'))")
        assert any("--bad" in c for c in cols), "loss view mixes toward --bad"
        assert not any("--good" in c or "--a)" in c or "--b)" in c for c in cols), "no gain colours in the loss view"
        assert pg.evaluate("!document.getElementById('lossKey').hidden && document.getElementById('abScale').hidden")
        assert "Darker red: where your typical lap loses time" in pg.inner_text("#lossKey")
        # hover over the map shows a phase line from skills markers
        box = pg.query_selector("#trackmap svg").bounding_box()
        hit = pg.evaluate("""()=>{const s=document.querySelector('#trackmap svg');const pl=s.querySelector('polyline[pointer-events]');
            const pts=pl.getAttribute('points').split(' ').map(p=>p.split(',').map(Number));const r=s.getBoundingClientRect();const vb=s.viewBox.baseVal;
            return pts.map(([x,y])=>[r.left+x*r.width/vb.width,r.top+y*r.height/vb.height])}""")
        seen = set()
        for x, y in hit[::25]:
            pg.mouse.move(x, y)
            pg.wait_for_timeout(15)
            seen.add(pg.inner_text("#tip"))
        txt = " ".join(seen)
        assert "your typical lap" in txt and "your better laps" in txt, txt[:300]
        assert any(w in txt for w in ("Braking", "Turn-in to apex", "Apex to throttle", "Exit", "Approach")), txt[:300]
        _ = box
        ctx.close()
        assert not errs, errs


def test_where_in_the_corner_click_switches_map_and_highlights():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_new.json"), f))
        assert pg.inner_text("#wicLede").startswith("How close your typical laps are to your own better laps")
        pg.click("#wicRows .wicrow[data-phase=exit]")
        pg.wait_for_timeout(150)
        r = pg.evaluate("""()=>({mode:document.querySelector('#mapMode [data-mode=loss]').getAttribute('aria-pressed'),
            pressed:document.querySelector('#wicRows .wicrow[data-phase=exit]').getAttribute('aria-pressed'),
            outline:document.querySelectorAll('#trackmap svg > g:not([style]) > line[stroke-width="12"]').length,
            what:document.getElementById('lossWhat').textContent})""")
        assert r["mode"] == "true" and r["pressed"] == "true" and r["outline"] > 0 and "exit" in r["what"], r
        pg.click("#wicRows .wicrow[data-phase=exit]")   # click again clears the highlight
        pg.wait_for_timeout(100)
        assert pg.evaluate("document.getElementById('lossWhat').textContent") == ""
        ctx.close()
        assert not errs, errs


def test_old_221_review_degrades():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_old.json"), f))
        st = pg.evaluate(STATE)
        ctx.close()
        assert not errs, errs
        assert st["toggle"], "old reviews get the heat map too (JS better-laps rule)"
        assert not st["wic"] and not st["session"] and not st["spread"] and st["tp"] == 0, st
        assert not st["fail"] and not st["bad"], st


def test_no_map_hides_toggle():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_nomap.json"), f))
        st = pg.evaluate(STATE)
        # without the map the phase rows are plain (not buttons that would do nothing)
        tags = pg.evaluate("[...document.querySelectorAll('#wicRows .wicrow')].map(r=>r.tagName)")
        ctx.close()
        assert not errs, errs
        assert not st["toggle"] and st["wic"] and set(tags) == {"DIV"}, (st, tags)


def test_few_clean_laps_hides_toggle():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_old.json")
        keep = 0
        for lap in d["laps"]:
            if lap["clean"]:
                keep += 1
                if keep > 2:
                    lap["clean"] = False
        pg, ctx, errs = _open(_page(d, f))
        st = pg.evaluate(STATE)
        ctx.close()
        assert not errs, errs
        assert not st["toggle"], st


def test_skills_reason_shows_note():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_reason.json"), f))
        st = pg.evaluate(STATE)
        ctx.close()
        assert not errs, errs
        assert st["wic"] and st["wicRows"] == 0, st
        assert st["wicLede"] == "Needs at least 6 clean laps to split the time by corner phase.", st


def test_steering_row_only_with_channel():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_a.json"), f))
        t0 = pg.inner_text("#techTable")
        ctx.close()
        pg, ctx, errs2 = _open(_page(fx("sample_review_a_steer.json"), f, "s"))
        t1 = pg.inner_text("#techTable")
        ctx.close()
        assert not errs and not errs2, (errs, errs2)
        assert "Steering corrections" not in t0 and "Steering corrections" in t1
        for lab in ("Brake release length (m)", "Off the brake vs slowest point (m)", "Release shape",
                    "Throttle pickup after slowest point (m)", "Throttle ramp to flat (m)", "Lifts on the way to full throttle"):
            assert lab in t0, lab


def test_technique_plus_null_groups_and_notes():
    """braking/throttle/steering can each be null; a corner with all null shows its note instead of rows."""
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_a.json")
        cs = d["technique_plus"]["corners"]
        for c in cs:
            c["braking"] = None
            c["throttle"] = None
            c["per_lap"] = {}
            c["notes"] = ["Not enough clean laps through this corner to say."]
        d["technique_plus"]["corners"].append({"id": "C9", "garbage": True})
        d["technique_plus"]["corners"].append(None)
        pg, ctx, errs = _open(_page(d, f))
        t = pg.inner_text("#techTable")
        notes = pg.inner_text("#techNotes")
        fail = pg.evaluate("!document.getElementById('failNote').hidden")
        ctx.close()
        assert not errs and not fail, errs
        assert "Brake release length" not in t and "Not enough clean laps through this corner to say." in t, t
        assert "Not enough clean laps" in notes


def test_brake_spread_column_values():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_a.json")
        bs = {c["id"]: c for c in d["brake_spread"]["corners"]}
        pg, ctx, errs = _open(_page(d, f))
        cells = pg.evaluate("""()=>{const ths=[...document.querySelectorAll('#cornerTable thead th')];const k=ths.findIndex(t=>/spread/i.test(t.textContent));
            return [...document.querySelectorAll('#cornerTable tbody tr')].map(tr=>[tr.id.replace('corner-',''),tr.children[k].innerText])}""")
        ctx.close()
        assert not errs, errs
        for cid, txt in cells:
            if cid in bs:
                assert txt.startswith("±" + str(int(bs[cid]["iqr_m"] / 2 + 0.5))) and bs[cid]["label"].upper() in txt.upper(), (cid, txt)
            else:
                assert txt == "–", (cid, txt)


def test_practice_review_session_section():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_practice.json")
        pg, ctx, errs = _open(_page(d, f))
        st = pg.evaluate(STATE)
        r = pg.evaluate("""()=>({runs:document.querySelectorAll('#sessRuns .run').length,targets:!document.getElementById('sessTargets').hidden,
            prog:document.getElementById('sessProgress').innerText,rows:document.querySelectorAll('#sessTargetTable tbody tr').length,
            quali:!document.getElementById('sessQuali').hidden})""")
        ctx.close()
        assert not errs, errs
        assert st["session"] and "Runs" in st["jump"] and st["raceTitle"] == "Session story" and "Session story" in st["jump"], st
        assert not st["toggle"], "the practice sample has no GPS map"
        assert r["runs"] == len(d["session_review"]["runs"]) and r["targets"] and r["rows"] == len(d["session_review"]["targets"]), r
        assert r["prog"].startswith("Your last run’s typical lap was") and "C2" in r["prog"] and not r["quali"], r


def test_qualifying_review():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_quali.json")
        pg, ctx, errs = _open(_page(d, f))
        st = pg.evaluate(STATE)
        r = pg.evaluate("""()=>({strip:[...document.querySelectorAll('#sessQuali .qlap')].map(x=>x.className),
            txt:document.getElementById('sessQuali').innerText,targets:!document.getElementById('sessTargets').hidden})""")
        ctx.close()
        assert not errs, errs
        assert st["session"] and "Qualifying" in st["jump"], st
        assert r["strip"] == ["qlap warm-up", "qlap warm-up", "qlap best", "qlap push", "qlap cool"], r
        assert "Your best lap came on lap 3 of the run." in r["txt"] and "0.30 s" in r["txt"] and not r["targets"], r


def test_race_has_no_session_section_and_garbage_degrades():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_new.json")
        d["session_review"] = {"kind": "practice", "runs": "nope", "progress": 5, "targets": [None, {"corner": "C1"}], "quali": []}
        d["skills"] = {"reason": None, "phases": {"exit": None}, "corners": "x", "ref_laps": ["a", 999]}
        d["brake_spread"] = {"corners": [None, {"id": "C1", "iqr_m": "x"}]}
        pg, ctx, errs = _open(_page(d, f))
        st = pg.evaluate(STATE)
        ctx.close()
        assert not [e for e in errs if e.startswith("pageerror")], errs
        assert st["session"] and not st["bad"], st     # garbage session_review: section shows "not enough laps", no crash


def test_escaping_in_new_sections():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_practice.json")
        d["session_review"]["targets"][0]["corner"] = XSS
        d["session_review"]["progress"]["corners"][1]["id"] = XSS
        d["corners"][0]["id"] = XSS
        d["brake_spread"]["corners"][0]["label"] = XSS
        pg, ctx, errs = _open(_page(d, f), init="window.__x=0")
        r = pg.evaluate("({x:window.__x||0, imgs:document.querySelectorAll('img').length})")
        ctx.close()
        assert r == {"x": 0, "imgs": 0}, r


def test_dark_and_narrow_no_horizontal_scroll():
    if _skip():
        return
    with Tmp() as f:
        for name in ("sample_review_new.json", "sample_review_practice.json", "sample_review_quali.json"):
            pg, ctx, errs = _open(_page(fx(name), f), scheme="dark", width=390)
            sw = pg.evaluate("document.documentElement.scrollWidth")
            ctx.close()
            assert not errs and sw <= 390, (name, sw, errs)


def test_new_sections_add_no_tab_stops_per_mark():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_new.json"), f))
        pg.click("#mapMode [data-mode=loss]")
        n = pg.evaluate("document.querySelectorAll('#trackmap [tabindex], #wicPanel [tabindex], #techChart svg [tabindex]').length")
        ctx.close()
        assert n == 0, n


# ================================================================ library.html
def _rows_for(profile):
    races = profile["groups"][0]["phases"]["exit"]["races"]
    return [{"dir": r["dir"], "path": f"reviews/{r['dir']}/index.html", "date": r["date"], "started_at": "",
             "track": r["track_short"], "track_short": r["track_short"], "car": r["car"], "best": 90.0 + i,
             "median": 91.0 + i, "laps": 12, "clean": 10, "analysed": 12,
             "season": {"name": "2026 Season 3" if i == 0 else "2026 Season 4"}}
            for i, r in enumerate(races)]


def test_library_skills_empty_states():
    if _skip():
        return
    with Tmp() as f:
        rows = _rows_for(fx("b_sample_profile.json"))
        for i, extra in enumerate([None, {"goals": [], "skills": None}, {"skills": {"groups": []}}, {"skills": {"groups": "x"}}]):
            pg, ctx, errs = _open(_lib(rows, extra, f, f"l{i}"), hash_="#skills")
            r = pg.evaluate("({vis:!document.getElementById('tab-skills').hidden, txt:document.getElementById('skpanel').innerText,"
                            "cur:document.querySelector('.tabs a[aria-current]').dataset.tab})")
            ctx.close()
            assert not errs, (extra, errs)
            assert r["vis"] and r["cur"] == "skills", r
            assert "Skill scores appear after a race with at least 6 clean laps. Older reviews get them when Debrief refreshes the library." in r["txt"], r


def test_library_skills_profile():
    if _skip():
        return
    with Tmp() as f:
        prof = fx("b_sample_profile.json")
        pg, ctx, errs = _open(_lib(_rows_for(prof), {"goals": [], "skills": prof}, f), hash_="#skills")
        r = pg.evaluate("""()=>({tiles:[...document.querySelectorAll('#sktiles .sktile')].map(b=>b.innerText.replace(/\\n/g,' ')),
            svg:!!document.querySelector('#skpanel svg'),groups:[...document.querySelectorAll('#skgroup option')].map(o=>o.value),
            lede:document.querySelector('#skills .lede').textContent})""")
        assert not errs, errs
        assert len(r["tiles"]) == 5 and r["svg"] and r["groups"][0] == "all" and len(r["groups"]) == 3, r
        assert r["lede"].startswith("Each race compares your typical laps with your own better laps in that race."), r
        ex = prof["groups"][0]["phases"]["exit"]
        assert any(t.startswith("Exit") and f"{ex['latest']}/100" in t and "Improving" in t for t in r["tiles"]), r["tiles"]
        # weekly and season medians toggle extra lines
        n0 = pg.evaluate("document.querySelectorAll('#skpanel svg polyline').length")
        pg.click("text=Weekly median")
        pg.click("text=Season median")
        n1 = pg.evaluate("document.querySelectorAll('#skpanel svg polyline').length")
        assert n1 == n0 + 2, (n0, n1)
        # a class group with trend null says "Not enough races yet"
        pg.select_option("#skgroup", "class:GT3")
        pg.wait_for_timeout(80)
        tiles = pg.evaluate("[...document.querySelectorAll('#sktiles .sktile')].map(b=>b.innerText)")
        assert all("Not enough races yet" in t for t in tiles), tiles
        # one tab stop for the chart, arrow keys show a tooltip
        pg.focus("#skpanel svg")
        pg.keyboard.press("ArrowRight")
        assert "Time lost" in pg.inner_text("#tip")
        ctx.close()
        assert not errs, errs


def test_library_skills_span_ok_false_and_direction_hidden():
    if _skip():
        return
    with Tmp() as f:
        prof = fx("b_sample_profile.json")
        for ph in prof["groups"][0]["phases"].values():
            if ph.get("trend"):
                ph["trend"]["span_ok"] = False
        pg, ctx, errs = _open(_lib(_rows_for(prof), {"skills": prof}, f), hash_="#skills")
        tiles = pg.evaluate("[...document.querySelectorAll('#sktiles .sktile')].map(b=>b.innerText)")
        ctx.close()
        assert not errs, errs
        assert all("Not enough races yet" in t and "Improving" not in t for t in tiles), tiles


def test_library_other_tabs_unchanged():
    if _skip():
        return
    with Tmp() as f:
        prof = fx("b_sample_profile.json")
        p = _lib(_rows_for(prof), {"goals": [], "skills": prof}, f)
        for h in ("", "#season", "#goals"):
            pg, ctx, errs = _open(p, hash_=h)
            vis = pg.evaluate("[...document.querySelectorAll('.tabpane')].filter(x=>!x.hidden).map(x=>x.id)")
            ctx.close()
            assert not errs and vis == ["tab-" + (h[1:] or "overview")], (h, vis, errs)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            print("ok  ", name)
        except Exception as e:  # noqa: BLE001
            fails += 1
            import traceback
            traceback.print_exc()
            print("FAIL", name, e)
    if "pw" in _PW:
        try:
            _PW["b"].close()
            _PW["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
    print(f"{fails} failed" if fails else "all passed")
    sys.exit(1 if fails else 0)
