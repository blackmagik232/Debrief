"""Debrief 2.19.1 clarity fixes from two testers' reports: a gap equals the difference of the speeds shown beside it,
a review with no usable laps never prints null/NaN, the Corner coach's How line says the distance it knows, Setup Lab's
Try next with one setup is one concrete test, the coach's reference lap agrees with Practice's, and counts say what
they count.

    python3 tests/ci_run.py tests/test_2191_clarity.py

Synthetic data only. Browser parts use Playwright + Chromium and are skipped when either is missing.
"""
from __future__ import annotations

import random
import re
import sys
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_28_page as tp  # noqa: E402
from debrief import cornercoach as cc, livecoach as lc, units  # noqa: E402

JS = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
GAP = re.compile(r"(\d+(?:\.\d+)?) (?:km/h|mph)[^()\d]*\((\d+(?:\.\d+)?) vs (\d+(?:\.\d+)?) (?:km/h|mph)\)")


def _gaps(text):
    return [(float(g), abs(float(a) - float(b))) for g, a, b in GAP.findall(text)]


# ---------------------------------------------------------------- 1. a gap equals the difference of the numbers shown
def test_gap_is_the_difference_of_the_shown_speeds_in_mph():
    t = units.convert_text("about 7 km/h more through the middle (101 vs 94 km/h)", "mph")
    assert t == "about 5 mph more through the middle (63 vs 58 mph)", t
    t = units.convert_text("about 7 km/h more through the middle (101 vs 94 km/h) and 3 km/h more on the way out (130 vs 127 km/h)", "mph")
    assert [g for g, _d in _gaps(t)] == [d for _g, d in _gaps(t)], t
    # unchanged in km/h, and a sentence with no pair of speeds is left alone
    assert units.convert_text("about 7 km/h more through the middle (101 vs 94 km/h)", "kmh") == "about 7 km/h more through the middle (101 vs 94 km/h)"
    assert units.convert_text("carry 4 km/h more at the slowest point", "mph") == "carry 2 mph more at the slowest point"


def test_gap_sweep_every_unit():
    rnd = random.Random(7)
    for _ in range(400):
        b = rnd.uniform(60, 220)
        gap = rnd.uniform(1.0, 12.0)
        a = b + gap
        text = (f"about {cc._round_to(gap, 1)} km/h more through the middle "
                f"({cc._round_to(a, 1)} vs {cc._round_to(b, 1)} km/h)")
        for unit in ("mph", "kmh"):
            for g, d in _gaps(units.convert_text(text, unit)):
                assert abs(g - d) < 1e-9 or g == 1 and d == 0, (unit, text, g, d)


def test_corner_coach_gets_line_uses_the_shown_numbers():
    # 100.4 -> 100 and 93.6 -> 94: the old text said "about 7 km/h" (6.8) beside "100 vs 94"
    D = {"min_kmh": {"best": 100.4, "typical": 93.6, "diff": 6.8, "z": 9.0}, "exit_kmh": {"best": 150.4, "typical": 146.6, "diff": 3.8, "z": 9.0}}
    gets = cc._gets(D)
    g = _gaps(gets)
    assert len(g) == 2 and all(abs(a - b) < 1e-9 for a, b in g), gets
    assert "about 6 km/h more through the middle (100 vs 94 km/h)" in gets, gets
    # through the server's mph conversion as well
    for a, b in _gaps(units.convert_text(gets, "mph")):
        assert abs(a - b) < 1e-9, gets


def test_js_text_fallback_matches():
    m = re.search(r"function gapFix[\s\S]*?\n(?=function mphText)", JS)
    assert m, "gapFix"
    pg = _blank_page()
    if pg is None:
        return
    pg.evaluate("() => { window.spdOf = (v) => Math.round(v / 1.609344); window.spdLabel = () => 'mph'; "
                + m.group(0).replace("function gapFix", "window.gapFix=function") + "}")
    assert pg.evaluate("gapFix('about 7 km/h more through the middle (101 vs 94 km/h)')") == "about 5 mph more through the middle (63 vs 58 mph)"
    assert pg.evaluate("gapFix('carry 4 km/h more (63 km/h)')") == "carry 4 km/h more (63 km/h)"


# ---------------------------------------------------------------- 3. the How line says the number the coach knows
def _carry_D(release=True):
    D = {"min_kmh": {"best": 27.0, "typical": 25.0, "diff": 2.0, "z": 9.0, "n": 5},
         "entry_kmh": {"best": 120.0, "typical": 120.0, "diff": 0.0, "z": 0.0}}
    if release:
        D["release_end_m"] = {"best": 40.0, "typical": 50.0, "diff": -10.0, "z": 9.0}
    return D


def test_how_line_names_the_release_distance():
    diff, how, _why = cc._phrase("carry", _carry_D(), {"abs": False}, name="Turn 6")
    assert how.startswith("keep your usual Turn 6 brake point; release the brake about 10 m earlier"), how
    assert "a little earlier" not in how
    diff, how, _why = cc._phrase("carry", _carry_D(release=False), {"abs": False}, name="Turn 6")
    assert not re.search(r"\d+ m\b", how), how            # no number the card doesn't have


# ---------------------------------------------------------------- 4. Setup Lab: Try next with one setup
def test_try_next_with_one_setup_is_one_concrete_test():
    m = re.search(r"function slOneSetupNext[\s\S]*?\n(?=function slIdeaLi)", JS)
    assert m and "slOneSetupNext(items)" in JS
    pg = _blank_page()
    if pg is None:
        return
    pg.evaluate("() => {" + m.group(0).replace("function slOneSetupNext", "window.f=function") + "}")
    a = pg.evaluate("f([{kind:'change',action:'Lower the front hot pressure by 0.5 psi.'}])")
    assert a == "Next run: Lower the front hot pressure by 0.5 psi, then drive at least 3 separate runs on each setup.", a
    b = pg.evaluate("f([{kind:'note',text:'x'}])")
    assert b == "Next run: make one change from the ideas below, then drive at least 3 separate runs on each setup.", b


# ---------------------------------------------------------------- 5. the coach's reference agrees with Practice's
def _ref(kind, lap, **src):
    z = np.zeros(4)
    return lc.Reference(lap_time=lap, length_m=4000.0, car={}, track={}, source=dict({"kind": kind}, **src),
                        pct=z, speed=z, brake=z, throttle=z, gear=z, time=z, corners=[])


def _index():
    rec = lambda f, best: {"file": f, "ok": True, "best": best, "car": {"path": "gt4"}, "track": {"code": "zolder"}, "start": 0}
    return {"files": [rec("gt4_zolder 2026-11-02 19-30-00.ibt", 95.012), rec("gt4_zolder 2026-11-03 18-04-12.ibt", 94.965)]}


def test_reference_dates_and_choice_agree_with_practice():
    idx = _index()
    ch = lc.reference_choice(idx, "gt4", "zolder")
    assert ch["lap_text"] == "1:34.965" and ch["date"] == "2026-11-03", ch       # what Practice names
    # a lap taken from a file is dated as Practice dates it
    assert _ref("ibt", 95.012, date="2026-11-02").describe() == "1:35.012 from Nov 2"
    # a lap adopted while driving carries no calendar day that could disagree with the Practice list
    live = _ref("live", 95.012, date="2026-11-03")
    assert live.describe() == "1:35.012 from this session", live.describe()
    assert lc.reference_why(live) == "your best clean lap this session"
    # and a quicker clean lap in Practice's list is said, not hidden
    qk = lc.quicker_known(idx, "gt4", "zolder", live.lap_time)
    assert qk and qk["text"].startswith("1:34.965") and lc.quicker_known(idx, "gt4", "zolder", 94.965) is None
    ns = types.SimpleNamespace(coach=types.SimpleNamespace(reference=live, keep_reference=None), _key=("gt4", "zolder"),
                               _reference_index=lambda: idx)
    ns._quicker_known = types.MethodType(lc.LiveRunner._quicker_known, ns)
    why = lc.LiveRunner._why_with_quicker(ns)
    assert why.startswith("your best clean lap this session; Practice lists a quicker clean lap, 1:34.965 from Nov 3"), why


# ---------------------------------------------------------------- 2. no lap number is ever "null"
def _no_lap_page(folder):
    d = tp.base()
    d["laps"], d["best"], d["median"], d["corners"], d["traces"] = [], {"n": None, "t": None}, None, [], {}
    d["insights"] = {"verdict": [], "fix": []}
    d["counts"] = {}
    return tp.page_file(d, folder, "nolaps")


def test_review_without_laps_never_prints_null():
    if not tp.browser():
        return
    with tp.Tmp() as f:
        ctx = tp.browser().new_context(viewport={"width": 1200, "height": 900})
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
        pg.goto(_no_lap_page(f).as_uri())
        pg.wait_for_timeout(800)
        text = pg.evaluate("() => document.body.innerText")
        assert not re.search(r"\b(null|undefined|NaN)\b", text), re.findall(r".{30}\b(?:null|undefined|NaN)\b.{20}", text)
        assert "No usable laps in this race, so there is nothing to compare." in text, text[:800]
        assert "Your best was" not in text and "Nothing big to fix" not in text
        assert not errs, errs
        ctx.close()


def test_other_lap_numbers_in_the_window_are_guarded():
    for good in ("ok(g.from_lap)?'best on lap '", "ok(bl.lap)?'Best on lap '", "ok(c.lap)?', lap '", "ok(s.result.lap)?' '+s.result.lap"):
        assert good in JS, good


# ---------------------------------------------------------------- 6. counts say what they count
def test_counts_are_labelled():
    assert "' of your '+plural(p.n_rows,'race review')" in JS
    assert "plural(races,'race')+' to look at'" in JS and "rcFilesNote" in JS
    assert "Practice counts every telemetry session" in JS


_PG = {}


def _blank_page():
    if "pg" not in _PG:
        b = tp.browser()
        _PG["pg"] = b.new_page() if b else None
    return _PG["pg"]
