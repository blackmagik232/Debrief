"""Debrief 2.9.1 polish: race timeline by keyboard, lap labels at phone width, Save as PDF, the real-scan checker, solo profile.

    python3 tests/ci_run.py tests/test_291_polish.py

Synthetic data only, no network, temp folders from tempfile, utf-8 everywhere. Browser tests use Playwright + Chromium and
are skipped when either is missing. page.pdf proves the print styles; the real Edge print dialog is not tested here.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import run_all  # noqa: E402
import test_28_page as tp  # noqa: E402
import test_28_profile as tf  # noqa: E402
from debrief import profile, report  # noqa: E402

_spec = importlib.util.spec_from_file_location("check_real_scan", HERE / "check_real_scan.py")
crs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crs)


def race_page(folder, n_laps=21):
    d = tp.with_scan(tp.base())
    rows = [{"lap": n, "pos": 5 + n % 3, "state": "clear", "t": 92.0 + n * 0.01} for n in range(1, n_laps + 1)]
    d["racecap"].update(version=2, laps=rows, passes=[{"lap": 2, "made": True, "other": "Rival One", "how": "on track", "stuck": True}],
                        fights=[], incidents=[], pits=[], stories=[], craft={"made": 1, "lost": 0, "stuck": 1})
    return tp.page_file(d, folder, f"r{n_laps}")


def needs_browser():
    if tp.browser() is None:
        print("  SKIPPED: Playwright/Chromium not available")
        return False
    return True


# ================================================================ the race timeline
def test_timeline_is_one_tab_stop_and_arrows_show_laps():
    if not needs_browser():
        return
    with tp.Tmp() as f:
        pg, ctx, errs = tp.opened(race_page(f), 1200)
        r = pg.evaluate("""()=>{const h=document.getElementById('rlTimeline');return {host:h.tabIndex,inner:h.querySelectorAll('[tabindex],[role=button]').length,label:h.getAttribute('aria-label')}}""")
        assert r["host"] == 0 and r["inner"] == 0, r
        assert "arrow keys" in r["label"], r
        detail = lambda: pg.inner_text("#rlLapDetail")  # noqa: E731
        pg.focus("#rlTimeline")
        pg.keyboard.press("ArrowRight")
        assert "Lap 1" in detail(), detail()
        pg.keyboard.press("ArrowRight")
        assert "Lap 2" in detail() and "You passed Rival One" in detail(), detail()
        pg.keyboard.press("End")
        assert "Lap 21" in detail()
        pg.keyboard.press("ArrowLeft")
        assert "Lap 20" in detail()
        assert pg.evaluate("document.activeElement.id") == "rlTimeline"
        pg.keyboard.press("Escape")
        ctx.close()
        assert not errs, errs


def test_timeline_click_still_works_and_arrows_carry_on_from_it():
    if not needs_browser():
        return
    with tp.Tmp() as f:
        pg, ctx, errs = tp.opened(race_page(f), 1200)
        pg.evaluate("""()=>{const g=document.querySelectorAll('#rlTimeline svg g')[6];g.dispatchEvent(new MouseEvent('click',{bubbles:true}))}""")
        assert "Lap 7" in pg.inner_text("#rlLapDetail")
        assert pg.evaluate("document.querySelectorAll('#rlTimeline g.rlsel').length") == 1
        pg.focus("#rlTimeline")
        pg.keyboard.press("ArrowRight")
        assert "Lap 8" in pg.inner_text("#rlLapDetail")
        assert pg.evaluate("document.querySelectorAll('#rlTimeline g.rlsel').length") == 1
        ctx.close()
        assert not errs, errs


OVERLAPS = """()=>{const t=[...document.querySelectorAll('#rlTimeline svg text')].map(x=>({r:x.getBoundingClientRect(),t:x.textContent}));const out=[];
  for(let i=0;i<t.length;i++)for(let j=i+1;j<t.length;j++){const a=t[i].r,b=t[j].r;if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>0&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>0)out.push([t[i].t,t[j].t])}
  const svg=document.querySelector('#rlTimeline svg').getBoundingClientRect();
  return {out,labels:t.map(x=>x.t).filter(x=>x[0]==='L'),clipped:t.filter(x=>x.r.right>svg.right+0.5||x.r.left<svg.left-0.5).map(x=>x.t)}}"""


def test_lap_labels_never_overlap_at_390():
    if not needs_browser():
        return
    with tp.Tmp() as f:
        for n in (6, 21, 26, 41, 61, 120):
            pg, ctx, errs = tp.opened(race_page(f, n), 390)
            r = pg.evaluate(OVERLAPS)
            ctx.close()
            assert not r["out"] and not r["clipped"], (n, r)
            assert r["labels"][0] == "L1" and r["labels"][-1] == f"L{n}", (n, r)
            assert not errs, errs


# ================================================================ Save as PDF
def test_save_as_pdf_opens_the_folds_hides_the_window_and_restores():
    if not needs_browser():
        return
    with tp.Tmp() as f:
        pg, ctx, errs = tp.opened(race_page(f), 1200)
        pg.evaluate("window.print=()=>{window.__printed=(window.__printed||0)+1}")
        before = pg.evaluate("['deep','rlGapsWrap','rlMoments'].map(i=>document.getElementById(i).open)")
        assert before == [False, False, False], before
        pg.click("#btnPdf")
        pg.wait_for_function("window.__printed===1", timeout=5000)
        assert pg.evaluate("['deep','rlGapsWrap','rlMoments'].map(i=>document.getElementById(i).open)") == [True, True, True]
        pg.emulate_media(media="print")
        r = pg.evaluate("""()=>{const seen=e=>!!e&&e.getClientRects().length>0;const q=s=>document.querySelector(s);
          return {nav:seen(q('#jump')),btn:[...document.querySelectorAll('button:not(.sktile)')].filter(seen).length,hint:seen(q('.rlhint')),
            gaps:seen(q('#rlGaps table')),moments:seen(q('#rlRows')),compare:seen(q('#compare')),bg:getComputedStyle(document.body).backgroundColor,
            over:document.documentElement.scrollWidth-document.documentElement.clientWidth}}""")
        assert r["nav"] is False and r["btn"] == 0 and r["hint"] is False, r
        assert r["gaps"] and r["moments"] and r["compare"], r
        assert r["bg"] == "rgb(255, 255, 255)", r
        pdf = pg.pdf(format="A4", prefer_css_page_size=True)
        assert pdf[:4] == b"%PDF" and len(pdf) > 5000
        pg.set_viewport_size({"width": 718, "height": 1000})    # A4 less the 12 mm margins
        pg.wait_for_timeout(100)
        wide = pg.evaluate("""()=>[...document.querySelectorAll('table,td,th,.panel,p')].filter(e=>e.getClientRects().length&&(e.getBoundingClientRect().right>719||e.scrollWidth>e.clientWidth+1)&&e.clientWidth>0).map(e=>e.tagName+'#'+(e.closest('[id]')||{}).id+':'+e.textContent.slice(0,30))""")
        assert not wide, wide[:8]
        pg.emulate_media(media="screen")
        pg.evaluate("window.dispatchEvent(new Event('afterprint'))")
        assert pg.evaluate("['deep','rlGapsWrap','rlMoments'].map(i=>document.getElementById(i).open)") == [False, False, False]
        ctx.close()
        assert not errs, errs


def test_a_fold_you_opened_stays_open_after_printing():
    if not needs_browser():
        return
    with tp.Tmp() as f:
        pg, ctx, errs = tp.opened(race_page(f), 1200)
        pg.evaluate("document.getElementById('rlMoments').open=true;window.dispatchEvent(new Event('beforeprint'))")
        assert pg.evaluate("document.getElementById('deep').open") is True
        pg.evaluate("window.dispatchEvent(new Event('afterprint'))")
        assert pg.evaluate("[document.getElementById('rlMoments').open,document.getElementById('deep').open]") == [True, False]
        ctx.close()
        assert not errs, errs


# ================================================================ tests/check_real_scan.py
def good_rc(**over):
    rc = {"version": 2, "subsession": "7", "cars": 20, "finish": 5, "class_finish": 2, "class_size": 8, "official": False, "grid": 6,
          "car_class_name": "GT4", "pos_after_lap1": 6, "ended": None, "lap_map": {str(i): i + 1 for i in range(1, 11)},
          "positions": [{"lap": i, "pos": 5} for i in range(1, 12)],
          "laps": [{"lap": i, "pos": 5, "state": "clear" if i % 2 else "fight"} for i in range(1, 12)],
          "passes": [{"lap": 3, "made": True, "how": "on track", "stuck": True}, {"lap": 4, "made": False, "how": "on track"}, {"lap": 1, "made": True, "how": "on track"}],
          "craft": {"made": 1, "lost": 1, "stuck": 1},
          "pace": {"peers": 7, "peer_label": "your class", "peer_rank": 3, "vs_peers": 0.4, "typical": 92.0, "peer_typical": 91.6}}
    rc.update(over)
    return rc


def review_doc(rc, event="e1"):
    return {"meta": {"event_id": event, "driver": "Test Driver", "session": "Race"}, "median": 92.1,
            "laps": [{"n": i, "t": 92.0} for i in range(1, 11)], "racecap": rc}


def library_of(*docs):
    p = Path(tempfile.mkdtemp(prefix="dbf291"))
    for i, d in enumerate(docs):
        o = p / "reviews" / f"r{i}"
        o.mkdir(parents=True)
        (o / "review.json").write_text(json.dumps(d), encoding="utf-8")
    return p


def fingerprint(p):
    return sorted((str(f.relative_to(p)), hashlib.sha256(f.read_bytes()).hexdigest()) for f in p.rglob("*") if f.is_file())


def test_checker_reports_a_good_review_and_changes_nothing():
    p = library_of(review_doc(good_rc()), {"meta": {"event_id": "none"}, "laps": []})
    try:
        before = fingerprint(p)
        rep = crs.check_library(p)
        assert fingerprint(p) == before
        assert len(rep["reviews"]) == 1 and rep["without_scan"] == ["r1"], rep
        r = rep["reviews"][0]
        assert r["laps"]["coverage"] == 1.0 and r["laps"]["mapped"] == 10
        assert r["finish"]["overall"] == 5 and r["finish"]["in_class"] == 2
        assert r["passes"]["made"] == 1 and r["passes"]["suffered"] == 1    # lap 1 is left out
        assert r["states"] == {"clear": 6, "fight": 5}
        assert r["peers"]["peers"] == 7
        assert r["suspicious"] == [], r["suspicious"]
        text = crs.text_report(rep)
        assert "Lap mapping: 10 of 10" in text and "in class 2" in text and "Suspicious: nothing" in text
    finally:
        shutil.rmtree(p, ignore_errors=True)


def test_checker_flags_what_is_wrong():
    cases = [(dict(lap_map={"1": 1, "2": 2}), "Only 2 of 10"), (dict(lap_map={str(i): 11 - i for i in range(1, 11)}), "goes backwards"),
             (dict(class_finish=9, class_size=8), "the class has 8"), (dict(class_finish=6), "worse than"), (dict(finish=25), "outside the 20"),
             (dict(class_finish=None, class_size=None), "No class data"), (dict(finish=None), "No finishing position"),
             (dict(pace={"peers": 0}), "No peers"), (dict(pace={"peers": 7, "peer_rank": 12, "vs_peers": 0.1}), "Pace rank 12"),
             (dict(pace={"peers": 7, "vs_peers": 9.0}), "big gap"), (dict(craft={"made": 9, "lost": 1}), "don't match"),
             (dict(laps=[{"lap": i, "pos": 5, "state": "unknown"} for i in range(1, 12)]), "no known state"), (dict(version=1), "old scan format"),
             (dict(passes=[]), "No on-track passes"), (dict(pos_after_lap1=15), "big swing")]
    for over, words in cases:
        p = library_of(review_doc(good_rc(**over)))
        try:
            s = crs.check_library(p)["reviews"][0]["suspicious"]
            assert any(words in x for x in s), (words, s)
        finally:
            shutil.rmtree(p, ignore_errors=True)


def test_checker_bad_folder_and_broken_file():
    p = Path(tempfile.mkdtemp(prefix="dbf291"))
    try:
        assert crs.main([str(p)]) == 2
        o = p / "reviews" / "bad"
        o.mkdir(parents=True)
        (o / "review.json").write_text("{nope", encoding="utf-8")
        rep = crs.check_library(p)
        assert rep["unreadable"] == [{"review": "bad", "why": "JSONDecodeError"}], rep
    finally:
        shutil.rmtree(p, ignore_errors=True)


def test_checker_is_not_a_test_file():
    assert not any("check_real_scan" in f for f in run_all.discover())


# ================================================================ solo profile
def solo_rows(lib):
    c = [{"corner": "T1", "made": 3, "lost": 1}]
    lib.add("a", tf.review("2026-10-01", 1, racecap=tf.rc(1, grid=10, lap1=8, passes=[tf.pass_(2, True, other="Named Rival"), tf.pass_(3, False, other="Named Rival")], corners=c)))
    lib.add("b", tf.review("2026-10-08", 2, racecap=tf.rc(2, grid=10, lap1=7, passes=[tf.pass_(2, True)], corners=c)))
    third = tf.rc(3, grid=10, lap1=9, passes=[tf.pass_(2, True)], corners=c)
    third.update(class_finish=3, class_size=9)
    lib.add("c", tf.review("2026-10-15", 3, racecap=third))
    return lib.rows()


def test_summary_speaks_only_of_you():
    with tf.Lib() as lib:
        c = profile.racecraft(solo_rows(lib))
        assert c["enough"] and len(c["summary"]) == 3, c.get("summary")
        text = " ".join(c["summary"])
        assert "gain about 2 places" in text and "about 1.0 on-track passes" in text and "T1" in text, text
        assert "Rival" not in json.dumps({k: c[k] for k in ("summary", "races", "passes", "lap1")})
        assert "team" not in text.lower()


def test_summary_is_not_given_below_three_races():
    with tf.Lib() as lib:
        lib.add("a", tf.review("2026-10-01", 1, racecap=tf.rc(1)))
        c = profile.racecraft(lib.rows())
        assert "summary" not in c and not c["enough"]


def test_class_finish_reaches_the_table_data():
    with tf.Lib() as lib:
        rows = {r["date"]: r for r in profile.racecraft(solo_rows(lib))["races"]}
        assert rows["2026-10-15"]["class_finish"] == 3 and rows["2026-10-15"]["class_size"] == 9
        assert rows["2026-10-01"]["class_finish"] is None


def test_note_says_how_many_finishes_are_official():
    assert profile._craft_note(0, 3) == profile.RACECRAFT_NOTE
    assert "official in 1 of 3 races and on the road in the rest" in profile._craft_note(1, 3)
    assert profile._craft_note(3, 3).endswith("Positions are official.")


def test_thresholds_did_not_move():
    got = (profile.MIN_CLEAN, profile.TREND_MIN_RACES, profile.TREND_MIN_DAYS, profile.TREND_MIN_CHANGE_PM, profile.TREND_MAX_P,
           profile.RACECRAFT_MIN_RACES, profile.BEST_CORNER_MIN_MADE)
    assert got == (6, 4, 14, 0.5, 0.05, 3, 2), got


def test_skills_page_reads_for_one_driver():
    if not needs_browser():
        return
    with tf.Lib() as lib:
        rows = solo_rows(lib)
        out = lib.dir / "index.html"
        out.write_text(report.render_library(rows, {"skills": profile.build(rows), "season": {}, "goals": None, "digest": None}), encoding="utf-8")
        for width in (1200, 390):
            ctx = tp.browser().new_context(viewport={"width": width, "height": 900})
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.route("**/fonts.g*/**", lambda r: r.abort())
            pg.goto(out.as_uri() + "#skills")
            pg.wait_for_selector("#craft .craftsum li", timeout=10000)
            t = pg.inner_text("#craft")
            assert "On lap 1 you gain" in t and "P3 in class, P9 overall on the road" in t, t
            assert "Each race you make" in t, t
            assert "—" not in pg.inner_text("#craft .craftsum"), t
            assert pg.evaluate("document.documentElement.scrollWidth-document.documentElement.clientWidth") <= 0
            ctx.close()
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
    if tp._PW.get("b"):
        tp._PW["b"].close()
        tp._PW["p"].stop()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
