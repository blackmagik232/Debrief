"""Debrief 2.8 review page: the jump bar, the one-sentence verdict, fix-first cards with corner maps, chart labels.

    python3 tests/ci_run.py tests/test_28_page.py

Synthetic data only (tests/fixtures_tracks), no network, temp folders from tempfile, utf-8 everywhere. Browser
tests use Playwright + Chromium and are skipped when either is missing.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures_tracks as ft  # noqa: E402
from debrief import engine, insights, report  # noqa: E402

TPL = ROOT / "debrief" / "templates" / "review.html"
_BASE, _PW = {}, {}
XSS = '<img src=x onerror="window.__x=1">'


def base():
    if "d" not in _BASE:
        meta, laps, tel = ft.race("street")
        d = engine.analyze(meta, laps, tel, [], {}, log=lambda m: None)
        d["insights"] = insights.build(d)
        _BASE["d"] = json.loads(json.dumps(d, default=float))
    return copy.deepcopy(_BASE["d"])


def with_scan(d):
    """The fields of D.racecap the page needs to draw the whole race."""
    ns = [l["n"] for l in d["laps"]]
    d["racecap"] = {"version": 1, "subsession": "1", "laps": [{"lap": n, "pos": 5 + n % 3, "state": "clear"} for n in ns],
                    "verdict": "You held your place from start to finish.", "pace": {"typical": d["median"], "best": d["best"]["t"]},
                    "passes": [], "fights": [], "incidents": [], "pits": [], "stories": []}
    return d


class Tmp:
    def __enter__(self):
        self.p = Path(tempfile.mkdtemp(prefix="debrief-28p-"))
        return self.p

    def __exit__(self, *a):
        shutil.rmtree(self.p, ignore_errors=True)


def page_file(d, folder, name="r"):
    o = folder / name
    o.mkdir(parents=True, exist_ok=True)
    p = o / "index.html"
    p.write_text(report.fill(TPL.read_text(encoding="utf-8"), {"/*DATA*/null": report._embed(d)}), encoding="utf-8")
    return p


def browser():
    if "b" not in _PW:
        try:
            from playwright.sync_api import sync_playwright
            _PW["p"] = sync_playwright().start()
            _PW["b"] = _PW["p"].chromium.launch()
        except Exception:  # noqa: BLE001
            _PW["b"] = None
    return _PW["b"]


def opened(path, width=1200, scheme="light"):
    ctx = browser().new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "ERR_" not in m.text else None)
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    pg.goto(path.as_uri())
    pg.wait_for_function("()=>document.querySelector('#fixList .fix')||!document.getElementById('summary').hidden", timeout=15000)
    return pg, ctx, errs


# ================================================================ the verdict's words
def _no_figures(text):
    return not re.search(r"\d", re.sub(r"\bC\d+\b|Garage 61", "", text))


def test_verdict_is_one_sentence_without_figures():
    d = base()
    v = insights.build(d)["verdict"]
    assert len(v) == 1 and v[0].endswith(".") and _no_figures(v[0]), v
    assert v[0].count(". ") == 0, v


def test_verdict_words_follow_consistency_and_phase():
    d = base()
    d["consistency"] = {"label": "Scattered", "score": 12}
    d["skills"] = {"phases": {"braking": {"loss_s": 0.4}, "entry": {"loss_s": 0.05}, "mid": {"loss_s": None}, "exit": {"loss_s": 0.1}}}
    v = insights.build(d)["verdict"][0]
    assert v.startswith("Your laps vary a lot, so") and "under braking" in v and _no_figures(v), v
    d["skills"]["phases"] = {"braking": {"loss_s": 0.1}, "exit": {"loss_s": 0.1}, "mid": {"loss_s": 0.1}}
    assert "most of the time" not in insights.build(d)["verdict"][0]
    d["consistency"] = {"label": "Very consistent"}
    assert insights.build(d)["verdict"][0].startswith("Your laps are very steady, so")


def test_verdict_with_nothing_to_fix():
    d = base()
    for c in d["corners"]:
        c["avail"] = 0.0
        c["ref_gap"] = None
    out = insights.build(d)
    assert out["fix"] == [] and len(out["verdict"]) == 1 and out["verdict"][0].startswith("No corner stands out"), out["verdict"]


# ================================================================ in the browser
def test_jump_bar_story_links_and_menu():
    if not browser():
        return
    with Tmp() as f:
        pg, ctx, errs = opened(page_file(base(), f))
        r = pg.evaluate("""()=>({row:[...document.querySelectorAll('#jumpLinks a')].filter(a=>!a.hidden).map(a=>a.textContent),
            menu:[...document.querySelectorAll('#jumpPanel a')].filter(a=>!a.hidden).map(a=>a.getAttribute('href')),
            vis:getComputedStyle(document.getElementById('jumpPanel')).visibility,exp:document.getElementById('jumpMore').getAttribute('aria-expanded')})""")
        assert 1 <= len(r["row"]) <= 6 and "What to work on" in r["row"], r
        for want in ("#compare", "#corners", "#technique", "#consistency"):
            assert want in r["menu"], r
        assert r["vis"] == "hidden" and r["exp"] == "false", r
        pg.click("#jumpMore")
        pg.wait_for_function("()=>getComputedStyle(document.getElementById('jumpPanel')).visibility==='visible'")
        pg.keyboard.press("Escape")
        pg.wait_for_function("()=>getComputedStyle(document.getElementById('jumpPanel')).visibility==='hidden'")
        pg.evaluate("document.getElementById('deep').open=false")
        pg.click("#jumpMore")
        pg.click("#jumpPanel a[href='#corners']")
        pg.wait_for_function("()=>document.getElementById('deep').open&&getComputedStyle(document.getElementById('jumpPanel')).visibility==='hidden'")
        ctx.close()
        assert not errs, errs


def test_order_with_a_scan_keeps_every_section():
    if not browser():
        return
    with Tmp() as f:
        pg, ctx, errs = opened(page_file(with_scan(base()), f))
        pg.wait_for_function("()=>!document.getElementById('raceTl').hidden")
        r = pg.evaluate("""()=>({order:[...document.querySelectorAll('.wrap>section,.wrap>details')].filter(s=>!s.hidden).map(s=>s.id),deep:document.getElementById('deep').open,
            inDeep:['race','racecraft'].filter(i=>document.getElementById(i)&&document.getElementById(i).parentElement.id==='deep'),
            row:[...document.querySelectorAll('#jumpLinks a')].filter(a=>!a.hidden).map(a=>a.textContent)})""")
        ctx.close()
        assert r["order"] == ["raceTl", "summary", "coach", "racelog", "rlPace", "deep"] and not r["deep"], r
        assert r["inDeep"] == ["race", "racecraft"], r
        assert r["row"] == ["Your race", "Summary", "What to work on", "Racecraft", "Pace"], r
        assert not errs, errs


def _five_fixes(d):
    ins = d["insights"]
    have = {f["corner"] for f in ins["fix"]}
    for c in d["corners"]:
        if len(ins["fix"]) >= 5:
            break
        if c["id"] not in have:
            ins["fix"].append({"corner": c["id"], "gain": 0.06, "basis": "own", "tips": [], "best_lap": c["best_lap"]})
    return d


def test_three_cards_then_expander_nothing_removed():
    if not browser():
        return
    with Tmp() as f:
        d = _five_fixes(base())
        n = len(d["insights"]["fix"])
        assert n > 3, n
        pg, ctx, errs = opened(page_file(d, f))
        r = pg.evaluate("""()=>({top:document.querySelectorAll('#fixList>.fix').length,inside:document.querySelectorAll('#fixMore .fix').length,
            open:document.getElementById('fixMore').open,sum:document.querySelector('#fixMore>summary').textContent,
            first:[...document.querySelectorAll('#fixList>.fix .rk')].map(x=>x.textContent)})""")
        assert r["top"] == 3 and r["inside"] == n - 3 and not r["open"] and str(n - 3) in r["sum"], r
        assert r["first"] == ["1", "2", "3"], r
        pg.click("#fixMore>summary")
        pg.wait_for_function("()=>document.querySelector('#fixMore .fix .rk')&&document.querySelector('#fixMore .fix .rk').getClientRects().length>0")
        ctx.close()
        assert not errs, errs


def test_table_shows_only_when_something_stands_out():
    if not browser():
        return
    with Tmp() as f:
        d = base()
        fx = d["insights"]["fix"]
        assert len(fx) >= 2
        row = {"brake_on": 100.0, "min_mph": 50.0, "exit_mph": 80.0, "full_thr": 200.0, "coast_s": 0.5, "gear": 3}
        fx[0]["numbers"] = {"best": dict(row), "quick": dict(row), "typical": dict(row, brake_on=85.0), "ref": None}
        fx[1]["numbers"] = {"best": dict(row), "quick": dict(row), "typical": dict(row, brake_on=97.5), "ref": None}
        pg, ctx, errs = opened(page_file(d, f))
        r = pg.evaluate("""()=>[...document.querySelectorAll('#fixList .fix')].slice(0,2).map(c=>{const t=c.querySelector('table.fnums');
            return {table:!!t,seen:!!t&&(!t.closest('details')||t.closest('details').open),fold:!!c.querySelector('details.fnumbers')}})""")
        ctx.close()
        assert r[0] == {"table": True, "seen": True, "fold": False}, r
        assert r[1] == {"table": True, "seen": False, "fold": True}, r      # still there, one click away
        assert not errs, errs


CARD_MAP = """()=>{const c=document.querySelector('#fixList .fix');const m=c.querySelector('.fmap');return {tab:m.tabIndex,
    full:!!m.querySelector('polyline:not(.fmapcorner)'),corner:m.querySelectorAll('.fmapcorner').length,
    stroke:(m.querySelector('.fmapcorner')||{getAttribute(){}}).getAttribute('stroke')||(m.querySelector('.fmapcorner')||{getAttribute(){}}).getAttribute('fill'),
    marks:[...m.querySelectorAll('.fmapmark text')].map(t=>t.textContent),note:c.querySelector('.fmapbox .note').textContent,
    stops:m.querySelectorAll('[tabindex]').length,id:c.querySelector('h3').textContent.split(' ')[0]}}"""


def test_corner_map_with_gps_clicks_through_to_compare():
    if not browser():
        return
    with Tmp() as f:
        d = base()
        assert len(d.get("map") or []) >= 10
        pg, ctx, errs = opened(page_file(d, f))
        r = pg.evaluate(CARD_MAP)
        assert r["tab"] == 0 and r["stops"] == 0 and r["full"] and r["corner"] == 1 and r["stroke"] == "var(--b)", r
        assert "S" in r["marks"] and "slowest point" in r["note"], r
        # keyboard: one tab stop, arrows read out the marks, Enter opens Compare zoomed to the corner
        pg.focus("#fixList .fix .fmap")
        pg.keyboard.press("ArrowRight")
        pg.wait_for_function("()=>!document.getElementById('tip').hidden&&document.getElementById('srLive').textContent.length>0")
        pg.keyboard.press("Enter")
        pg.wait_for_function("()=>document.getElementById('zoomInfo').textContent.includes('Showing')")
        pg.evaluate("document.getElementById('zoomInfo').querySelector('button').click()")
        pg.wait_for_function("()=>!document.getElementById('zoomInfo').textContent.includes('Showing')")
        pg.focus("#fixList .fix .fmap")
        pg.keyboard.press("Enter")     # no mark picked yet: Enter still goes to the corner
        pg.wait_for_function("()=>document.getElementById('zoomInfo').textContent.includes('Showing')")
        pg.evaluate("document.getElementById('zoomInfo').querySelector('button').click()")
        pg.wait_for_function("()=>!document.getElementById('zoomInfo').textContent.includes('Showing')")
        pg.locator("#fixList .fix .fmap svg").first.click()
        pg.wait_for_function("()=>document.getElementById('zoomInfo').textContent.includes('Showing')")
        ctx.close()
        assert not errs, errs


def test_corner_map_across_the_start_line():
    if not browser():
        return
    with Tmp() as f:
        d = base()
        fx = d["insights"]["fix"][0]
        c = next(x for x in d["corners"] if x["id"] == fx["corner"])
        c["start_m"], c["end_m"] = -40.0, 60.0
        pg, ctx, errs = opened(page_file(d, f))
        pts = pg.evaluate("document.querySelector('#fixList .fix .fmapcorner').getAttribute('points').split(' ').length")
        ctx.close()
        assert pts > 2 and not errs, (pts, errs)


def test_corner_map_fallbacks():
    if not browser():
        return
    with Tmp() as f:
        d = base()
        d["map"] = []
        pg, ctx, errs = opened(page_file(d, f, "nogps"))
        r = pg.evaluate(CARD_MAP)
        ctx.close()
        assert r["corner"] >= 1 and "No GPS" in r["note"] and "S" in r["marks"], r
        assert not errs, errs
        d = base()
        d["map"] = []
        d["traces"] = {}
        d["L"] = 0
        for c in d["corners"]:
            c.pop("start_m", None), c.pop("end_m", None), c.pop("apex_m", None)
        for x in d["insights"]["fix"]:
            x.pop("start_m", None), x.pop("end_m", None), x.pop("apex_m", None), x.pop("numbers", None)
        pg, ctx, errs = opened(page_file(d, f, "nothing"))
        r = pg.evaluate("document.querySelector('#fixList .fix').textContent")
        ctx.close()
        assert "NaN" not in r and "undefined" not in r, r


def test_every_chart_tip_is_reachable_by_keyboard_and_labels_are_bigger():
    if not browser():
        return
    with Tmp() as f:
        pg, ctx, errs = opened(page_file(base(), f))
        pg.wait_for_timeout(200)
        r = pg.evaluate("""()=>{const bad=new Set();let n=0;document.querySelectorAll('body *').forEach(x=>{if(x._tip){n++;if(!x.closest('[tabindex="0"]'))bad.add(x.tagName)}});
            const small=[...document.querySelectorAll('.chart svg text')].filter(t=>parseFloat(t.getAttribute('font-size'))<11).length;
            return {n,bad:[...bad],small,map:document.getElementById('trackmap').tabIndex}}""")
        ctx.close()
        assert r["n"] > 10 and not r["bad"] and r["small"] == 0 and r["map"] == 0, r
        assert not errs, errs


def test_no_sideways_scroll_and_text_stays_text():
    if not browser():
        return
    with Tmp() as f:
        d = with_scan(base())
        d["insights"]["fix"][0]["why"] = XSS
        d["insights"]["verdict"] = [XSS]
        p = page_file(d, f)
        for w in (1200, 390):
            for sch in ("light", "dark"):
                pg, ctx, errs = opened(p, w, sch)
                r = pg.evaluate("""()=>({over:document.documentElement.scrollWidth-document.documentElement.clientWidth,x:window.__x||0,
                    imgs:document.querySelectorAll('#coach img').length,fail:!document.getElementById('failNote').hidden})""")
                ctx.close()
                assert r == {"over": 0, "x": 0, "imgs": 0, "fail": False}, (w, sch, r)
                assert not errs, errs


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"ok    {name} ({time.time() - t0:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    if _PW.get("b"):
        _PW["b"].close()
        _PW["p"].stop()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
