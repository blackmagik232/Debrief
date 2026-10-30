"""Workstream 24-C (Debrief 2.4 pages): review.html Race craft section and fuel panel, prep.html "Fuel for
this race" box.

    python3 tests/test_24_c.py

Fixtures are in tests/fixtures_24_c/ (rebuild with tests/fixtures_24_c/make_samples.py; they reuse the 23-C
review samples and add the 2.4 keys in the SPEC §2.1 shapes). Browser checks use Playwright + Chromium and
print SKIPPED when it isn't installed; the static checks always run. Nothing here talks to Garage 61.
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
FX = Path(__file__).resolve().parent / "fixtures_24_c"
FX23 = Path(__file__).resolve().parent / "fixtures_23_c"
XSS = '<img src=x onerror="window.__x=(window.__x||0)+1">'
# sibling samples, used when they exist (24-A / 24-B commit them to their own copies first)
SIBLINGS = [ROOT.parent / "24-A" / "tests" / "fixtures_24_a", ROOT.parent / "24-B" / "tests" / "fixtures_24_b"]


def fx(name):
    p = FX / name if (FX / name).exists() else FX23 / name
    return json.loads(p.read_text(encoding="utf-8"))


class Tmp:
    def __enter__(self):
        self.p = Path(tempfile.mkdtemp(prefix="debrief-24c-"))
        return self.p

    def __exit__(self, *a):
        shutil.rmtree(self.p, ignore_errors=True)


def _page(data, folder: Path, name="r") -> Path:
    p = folder / f"{name}.html"
    p.write_text(report.fill((TPL / "review.html").read_text(encoding="utf-8"), {"/*DATA*/null": report._embed(data)}),
                 encoding="utf-8")
    return p


def _prep(data, folder: Path, name="p") -> Path:
    p = folder / f"{name}.html"
    p.write_text(report.fill((TPL / "prep.html").read_text(encoding="utf-8"), {"/*PREP*/null": report._embed(data)}),
                 encoding="utf-8")
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


def _open(path, scheme="light", width=1280, init=None, wait=450):
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
    pg.goto(Path(path).resolve().as_uri())
    settle(pg, cap_ms=max(1500, 3 * wait))     # loaded, painted and quiet (was a fixed `wait` ms)
    return pg, ctx, errs


def _skip():
    return _browser() is None


STATE = """()=>({fail:!document.getElementById('failNote').hidden,
  rc:!document.getElementById('racecraft').hidden,
  cells:document.querySelectorAll('#rcTimeline svg rect.cell').length,
  chips:[...document.querySelectorAll('#rcChips .chip')].map(c=>c.textContent),
  summary:[...document.querySelectorAll('#rcSummary li')].map(c=>c.textContent),
  lap1:document.getElementById('rcLap1').hidden?null:document.getElementById('rcLap1').innerText,
  fuel:!document.getElementById('fuelPanel').hidden, fuelTxt:document.getElementById('fuelPanel').innerText,
  stint:!document.getElementById('stint').hidden,
  jump:[...document.querySelectorAll('#jump a')].filter(a=>!a.hidden).map(a=>a.textContent),
  bad:/NaN|undefined|Infinity|Invalid Date|\\[object/.test(document.body.innerText)})"""


# ================================================================ static checks (always run)
def _block(s, start):
    i = s.index(start)
    j = s.find("// ----------", i + 20)
    return s[i:j if j > 0 else len(s)]


def test_static_ids_guards_and_order():
    s = (TPL / "review.html").read_text(encoding="utf-8")
    for i in ('id="racecraft"', 'href="#racecraft"', "rcTimeline", "rcLap1", "rcChips", "fuelPanel", "fuelRows"):
        assert i in s, i
    assert s.index('<section id="race"') < s.index('<section id="racecraft"') < s.index('<details class="deep"')
    assert s.index('href="#race"') < s.index('href="#racecraft"')
    assert "const RC=isObj(D.racecraft)?D.racecraft:null" in s and "const RX=isObj(D.result)?D.result:null" in s
    assert "const STRAT=isObj(D.strategy)?D.strategy:null" in s
    assert "if(!RC&&!RX)return" in s and "if(!STRAT)return" in s
    for name in ("race craft", "fuel", "race craft timeline"):
        assert f"'{name}'" in s and (f"safe('{name}'" in s or f"['{name}'," in s), name
    assert "Plan your next race's fuel on the Prepare page." in s
    p = (TPL / "prep.html").read_text(encoding="utf-8")
    assert 'id="fuelsec"' in p and "D.strategy&&typeof D.strategy==='object'" in p
    assert "Fuel numbers come from your own laps. Check the garage before the race." in p


def test_static_no_data_innerhtml_and_tokens_only():
    s = (TPL / "review.html").read_text(encoding="utf-8")
    for blk in ("// ---------- 2.4: race craft", "// ---------- 2.4: fuel"):
        assert "innerHTML" not in _block(s, blk), blk
    a, b = s.index("/* 2.4: race craft and fuel */"), s.index("/* v2: stint */")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", s[a:b]), "2.4 CSS must use the design tokens only"
    blk = _block(s, "// ---------- 2.4: race craft")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", blk)
    p = (TPL / "prep.html").read_text(encoding="utf-8")
    i = p.index("// fuel for this race (2.4")
    j = p.index("// goal", i)
    assert "innerHTML" not in p[i:j]
    for csp in (s, p):
        assert "connect-src 'none'" in csp


def test_static_words_correlational_and_units():
    s = (TPL / "review.html").read_text(encoding="utf-8")
    seg = _block(s, "// ---------- 2.4: race craft") + _block(s, "// ---------- 2.4: fuel")
    for w in (" will gain", "costing you", " because ", "standing start", "rolling start"):
        assert w not in seg, w
    assert "3.785" in seg and " gal)" in seg            # litres with US gallons in brackets
    assert "includes the start" in seg and "most likely traffic" in seg.lower()
    assert "That includes refuelling time." in seg


# ================================================================ review.html in the browser
def test_new_review_racecraft_and_fuel():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_24.json")
        pg, ctx, errs = _open(_page(d, f))
        st = pg.evaluate(STATE)
        icons = pg.evaluate("document.querySelectorAll('#rcKey svg').length")
        ctx.close()
        assert not errs and not st["fail"] and not st["bad"], (errs, st)
        assert st["rc"] and "Race craft" in st["jump"], st
        assert st["cells"] == 18, st                        # 18 analysed laps (pit laps 14/15 are analysed too)
        assert "P9 after lap 1 (up 2)" in st["chips"] and "1 pit stop" in st["chips"] and "1 penalty" in st["chips"], st
        assert "Running at the finish" in st["chips"] and "3 incidents logged (7x)" in st["chips"], st
        assert len(st["summary"]) == 2 and st["summary"][0].startswith("Lap 1 was 5.9 s slower"), st
        assert "Most of it in C1 (2.1 s) and C2 (1.4 s)." in st["lap1"] and "This includes the start." in st["lap1"], st
        assert icons == 5, icons                           # best, flag, car, wrench, ! all present
        assert st["fuel"] and st["stint"], st
        t = st["fuelTxt"]
        assert "Fuel use: 2.41 L (0.64 gal) a lap (17 laps; 2.52 L on your thirstier laps)" in t, t
        assert "45.2 L (11.9 gal) at the start" in t and "4.10 L (1.08 gal) at the end" in t, t
        assert "Pit stop loss: about 50.1 s" in t and "refuelling" in t, t
        assert "Plan your next race's fuel on the Prepare page." in t, t


def test_timeline_hover_and_keyboard():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_24.json"), f))
        pg.evaluate("document.getElementById('racecraft').scrollIntoView()")
        gs = pg.query_selector_all("#rcTimeline svg > g")
        gs[8].hover()
        pg.wait_for_timeout(60)
        t9 = pg.inner_text("#tip")
        gs[12].hover()
        pg.wait_for_timeout(60)
        t13 = pg.inner_text("#tip")
        gs[13].hover()
        pg.wait_for_timeout(60)
        t14 = pg.inner_text("#tip")
        # one tab stop for the whole strip; arrow keys step through the laps
        n = pg.evaluate("document.querySelectorAll('#racecraft [tabindex]').length")
        pg.focus("#rcTimeline")
        pg.keyboard.press("ArrowRight")
        k1 = pg.inner_text("#tip")
        live = pg.inner_text("#srLive")
        ctx.close()
        assert not errs, errs
        assert t9.startswith("Lap 9 · 1:") and "+0.7 s · most likely traffic around 2,310 m" in t9, t9
        assert "Incidents you logged: contact (4x), spin (2x)" in t13 and "Flagged by Garage 61" in t13, t13
        assert "Pit lap" in t14 and "against your typical lap" not in t14, t14
        assert n == 1, n
        assert k1.startswith("Lap 1") and "includes the start" in k1 and live.startswith("Lap 1"), (k1, live)


def test_old_review_with_new_result_no_racecraft():
    """2.2.1 review + a 2.4 result: the strip is built from D.laps + D.counts + result.incident_laps."""
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_old_result.json")
        assert "racecraft" not in d and "strategy" not in d
        pg, ctx, errs = _open(_page(d, f))
        st = pg.evaluate(STATE)
        pg.query_selector_all("#rcTimeline svg > g")[8].hover()
        pg.wait_for_timeout(60)
        t9 = pg.inner_text("#tip")
        inc = pg.evaluate("[...document.querySelectorAll('#rcTimeline svg > g')].map((g,i)=>[i+1,g.querySelectorAll('circle').length>0&&!!g.querySelector('path[stroke=\"var(--panel)\"]')]).filter(x=>x[1]).map(x=>x[0])")
        ctx.close()
        assert not errs and not st["fail"] and not st["bad"], (errs, st)
        assert st["rc"] and st["cells"] == len(d["laps"]) and not st["summary"], st
        assert st["lap1"] and "This includes the start." in st["lap1"], st
        assert not st["fuel"], st
        assert "most likely traffic around 2,310 m" in t9, t9
        assert inc == [6, 13], inc


def test_hidden_without_keys():
    """old review, no result: no Race craft, no fuel panel; practice: no Race craft; nomap: still fine."""
    if _skip():
        return
    with Tmp() as f:
        for name, want_rc in (("sample_review_old.json", False), ("sample_review_new.json", False),
                              ("sample_review_practice.json", False), ("sample_review_nomap.json", False),
                              ("sample_review_quali.json", False)):
            pg, ctx, errs = _open(_page(fx(name), f, name[:-5]))
            st = pg.evaluate(STATE)
            ctx.close()
            assert not errs and not st["fail"] and not st["bad"], (name, errs, st)
            assert st["rc"] == want_rc and "Race craft" not in st["jump"] and not st["fuel"], (name, st)


def test_result_only_old_style_finish_pos():
    """a v2 result (finish position only) still shows the strip; no chips for missing fields."""
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_old.json")
        d["result"] = {"finish_pos": 3, "start_pos": 5, "field_size": 20}
        pg, ctx, errs = _open(_page(d, f))
        st = pg.evaluate(STATE)
        ctx.close()
        assert not errs and st["rc"] and st["chips"] == [] and st["cells"] == len(d["laps"]), (errs, st)


def test_strategy_only_fuel_panel():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_page(fx("sample_review_strategy_only.json"), f))
        st = pg.evaluate(STATE)
        title = pg.inner_text("#stintTitle")
        ctx.close()
        assert not errs and not st["fail"], errs
        assert st["fuel"] and st["stint"] and title == "Fuel" and "Stint" in st["jump"], (st, title)
        assert "3.10 L (0.82 gal) a lap (9 laps; 3.30 L on your thirstier laps)" in st["fuelTxt"], st["fuelTxt"]
        assert "drop in fuel level" in st["fuelTxt"] and "Pit stop loss" not in st["fuelTxt"], st["fuelTxt"]
        assert "at the end" not in st["fuelTxt"], st["fuelTxt"]


def test_garbage_2_4_keys_degrade():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_24.json")
        d["racecraft"] = {"lap1": "x", "timeline": [None, {"n": "9"}, {"n": 3, "tags": "traffic", "lost_s": "a"}, {"n": 99999}],
                          "summary": "no"}
        d["strategy"] = {"burn": {"median_l": "2"}, "level": [], "pit": {"laps": "x", "loss_s": None}}
        d["result"] = {"finish_pos": 4, "incident_laps": "lots", "pos_after_lap1": 500, "penalties": -1,
                       "finish_status": "<b>x</b>"}
        pg, ctx, errs = _open(_page(d, f))
        st = pg.evaluate(STATE)
        ctx.close()
        assert not [e for e in errs if e.startswith("pageerror")] and not st["fail"] and not st["bad"], (errs, st)
        assert st["rc"] and st["chips"] == [] and not st["fuel"], st


def test_escaping_in_new_sections():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_review_24.json")
        d["racecraft"]["summary"] = [XSS]
        d["racecraft"]["lap1"]["segments"][0]["id"] = XSS
        d["strategy"]["note"] = XSS
        d["result"]["incident_laps"][0]["kind"] = XSS
        pg, ctx, errs = _open(_page(d, f), init="window.__x=0")
        r = pg.evaluate("({x:window.__x||0, imgs:document.querySelectorAll('img').length})")
        txt = pg.inner_text("#rcSummary")
        ctx.close()
        assert r == {"x": 0, "imgs": 0} and "<img" in txt, (r, txt)


def test_dark_and_narrow_no_horizontal_scroll():
    if _skip():
        return
    with Tmp() as f:
        for name in ("sample_review_24.json", "sample_review_old_result.json", "sample_review_strategy_only.json"):
            pg, ctx, errs = _open(_page(fx(name), f, name[:-5]), scheme="dark", width=390)
            sw = pg.evaluate("document.documentElement.scrollWidth")
            cells = pg.evaluate("document.querySelectorAll('#rcTimeline svg rect.cell').length")
            ctx.close()
            assert not errs and sw <= 390, (name, sw, errs)
            assert cells == 0 or cells >= 18, (name, cells)


def test_sibling_samples_render_if_present():
    """24-A / 24-B sample_*.json review samples (when committed) render without errors."""
    found = [p for d in SIBLINGS if d.is_dir() for p in sorted(d.glob("sample_*.json"))]
    if not found:
        print("  SKIPPED: no 24-A/24-B samples yet")
        return
    if _skip():
        return
    with Tmp() as f:
        base = fx("sample_review_new.json")
        for p in found:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                continue
            d = obj if "laps" in obj and "meta" in obj else dict(base)
            if d is not obj:
                for k in ("racecraft", "strategy", "result"):
                    if k in obj:
                        d[k] = obj[k]
                if obj.get("version") == 1 and "timeline" in obj:
                    d["racecraft"] = obj
                elif obj.get("version") == 1 and "burn" in obj:
                    d["strategy"] = obj
                elif "incident_laps" in obj or "pos_after_lap1" in obj:
                    d["result"] = obj
            pg, ctx, errs = _open(_page(d, f, p.stem))
            st = pg.evaluate(STATE)
            ctx.close()
            assert not errs and not st["fail"] and not st["bad"], (p.name, errs, st)


# ================================================================ prep.html
def test_prep_fuel_box():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_prep(fx("sample_prep_strategy.json"), f))
        r = pg.evaluate("({vis:!document.getElementById('fuelsec').classList.contains('hidden'),"
                        "txt:document.getElementById('fuelsec').innerText})")
        ctx.close()
        assert not errs and r["vis"], (errs, r)
        t = r["txt"]
        for s in ("40 laps", "1:25.900", "2.41 L (0.64 gal)", "2.52 L on thirstier laps", "47.0 L (12.4 gal)",
                  "check it in the garage", "~50.1 s", "You need about 103.4 L", "at least 2 stops",
                  "Check the garage before the race."):
            assert s in t, (s, t)


def test_prep_no_fuel_and_no_strategy():
    if _skip():
        return
    with Tmp() as f:
        pg, ctx, errs = _open(_prep(fx("sample_prep_nofuel.json"), f))
        r = pg.evaluate("({vis:!document.getElementById('fuelsec').classList.contains('hidden'),"
                        "txt:document.getElementById('fuelsec').innerText,sum:!document.getElementById('fuelsum').hidden})")
        ctx.close()
        assert not errs and r["vis"] and not r["sum"], (errs, r)
        assert "40 minutes" in r["txt"] and "couldn’t find fuel use" in r["txt"], r
        d = fx("sample_prep_strategy.json")
        d.pop("strategy")
        pg, ctx, errs = _open(_prep(d, f, "p2"))
        vis = pg.evaluate("!document.getElementById('fuelsec').classList.contains('hidden')")
        ctx.close()
        assert not errs and not vis, errs
        d["strategy"] = {"race": "x", "burn": [1], "tank": {"value": "big"}}
        pg, ctx, errs = _open(_prep(d, f, "p3"))
        vis = pg.evaluate("!document.getElementById('fuelsec').classList.contains('hidden')")
        ctx.close()
        assert not errs and not vis, errs


def test_prep_escaping_and_narrow():
    if _skip():
        return
    with Tmp() as f:
        d = fx("sample_prep_strategy.json")
        d["strategy"]["burn"]["source"] = XSS
        d["strategy"]["tank"]["source"] = XSS
        pg, ctx, errs = _open(_prep(d, f), scheme="dark", width=390, init="window.__x=0")
        r = pg.evaluate("({x:window.__x||0,imgs:document.querySelectorAll('img').length,sw:document.documentElement.scrollWidth})")
        ctx.close()
        assert not errs and r["x"] == 0 and r["imgs"] == 0 and r["sw"] <= 390, (errs, r)


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
