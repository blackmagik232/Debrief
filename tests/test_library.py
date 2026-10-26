"""Tests for library PB logic, goals, results, season and prep helpers.
Run: python3 -m pytest tests/test_library.py   or   python3 tests/test_library.py"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debrief import library, goals, results, prep  # noqa: E402

ROVAL = "Charlotte Motor Speedway (Roval 2019)"
GT4 = "Ford Mustang GT4"


def _review(folder, name, best, started, track=ROVAL, car=GT4, ids=None, event=None, **extra):
    d = Path(folder) / "reviews" / name
    d.mkdir(parents=True, exist_ok=True)
    data = {"meta": {"track": track, "car": car, "started_at": started, "event_id": event or name},
            "best": {"n": 3, "t": best}, "median": best + 1.0,
            "laps": [{"n": 1, "t": best + 1, "clean": True}, {"n": 3, "t": best, "clean": True}],
            "corners": [{"id": "C1", "apex_m": 200.0, "avail": 0.1, "rank": 1}], **extra}
    if ids:
        data["combo_ids"] = {"track_id": ids[0], "car_id": ids[1]}
    (d / "review.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def _new(best, started, track=ROVAL, car=GT4, ids=None, event="NEW"):
    data = {"meta": {"track": track, "car": car, "started_at": started, "event_id": event}, "best": {"n": 1, "t": best}}
    if ids:
        data["combo_ids"] = {"track_id": ids[0], "car_id": ids[1]}
    return data


class Tmp:
    def __enter__(self):
        self.p = Path(tempfile.mkdtemp(prefix="debrief-test-"))
        return self.p

    def __exit__(self, *a):
        shutil.rmtree(self.p, ignore_errors=True)


# ---------------------------------------------------------------- pb_for
def test_pb_first_time():
    with Tmp() as f:
        pb = library.pb_for(f, _new(84.0, "2026-10-01T20:00:00+00:00"))
        assert pb["first_time"] is True and pb["is_pb"] is False
        assert pb["previous_best"] is None and pb["delta"] is None and pb["races_at_combo"] == 1


def test_pb_new_pb_negative_delta():
    with Tmp() as f:
        _review(f, "a", 84.5, "2026-09-01T20:00:00+00:00")
        _review(f, "b", 84.2, "2026-09-08T20:00:00+00:00")
        pb = library.pb_for(f, _new(84.0, "2026-10-01T20:00:00+00:00"))
        assert pb["is_pb"] is True and pb["first_time"] is False
        assert pb["previous_best"] == 84.2
        assert pb["delta"] == -0.2  # this best - previous best: negative = new PB
        assert pb["races_at_combo"] == 3
        assert pb["previous_date"] == library.local_day("2026-09-08T20:00:00+00:00")


def test_pb_not_pb_positive_delta_and_tie():
    with Tmp() as f:
        _review(f, "a", 84.0, "2026-09-01T20:00:00+00:00")
        pb = library.pb_for(f, _new(84.3, "2026-10-01T20:00:00+00:00"))
        assert pb["is_pb"] is False and pb["delta"] == 0.3
        tie = library.pb_for(f, _new(84.0, "2026-10-01T20:00:00+00:00"))
        assert tie["is_pb"] is False and tie["delta"] == 0.0


def test_pb_ignores_later_races_same_event_and_exclude_dir():
    with Tmp() as f:
        _review(f, "later", 83.0, "2026-11-01T20:00:00+00:00")
        _review(f, "same", 82.0, "2026-09-01T20:00:00+00:00", event="NEW")
        _review(f, "mine", 81.0, "2026-09-02T20:00:00+00:00")
        pb = library.pb_for(f, _new(84.0, "2026-10-01T20:00:00+00:00"), exclude_dir="mine")
        assert pb["first_time"] is True  # later race, same event and excluded dir all skipped


def test_pb_combo_matching_ids_vs_names():
    with Tmp() as f:
        # same names but different ids: NOT the same combo when both sides have ids
        _review(f, "other", 80.0, "2026-09-01T20:00:00+00:00", ids=(999, 190))
        assert library.pb_for(f, _new(84.0, "2026-10-01T20:00:00+00:00", ids=(468, 190)))["first_time"] is True
        # same ids, renamed track: IS the same combo
        _review(f, "renamed", 84.5, "2026-09-02T20:00:00+00:00", track="Charlotte Roval (renamed)", ids=(468, 190))
        pb = library.pb_for(f, _new(84.0, "2026-10-01T20:00:00+00:00", ids=(468, 190)))
        assert pb["first_time"] is False and pb["previous_best"] == 84.5 and pb["is_pb"] is True
        # new review without ids falls back to names (matches "other" by name; 80.0 is quicker)
        pb2 = library.pb_for(f, _new(84.0, "2026-10-01T20:00:00+00:00"))
        assert pb2["previous_best"] == 80.0 and pb2["is_pb"] is False
        # a different car by name never matches
        assert library.pb_for(f, _new(84.0, "2026-10-01T20:00:00+00:00", car="Other car"))["first_time"]


def test_rows_pb_chronological_and_old_format():
    with Tmp() as f:
        _review(f, "r1", 85.0, "2026-08-01T20:00:00+00:00")
        _review(f, "r2", 84.0, "2026-08-08T20:00:00+00:00", ids=(468, 190), irating=1100,
                season={"id": 35, "name": "2026 Season 4", "shortName": "2026S4"},
                consistency={"score": 71, "label": "Consistent", "std": 0.3})
        _review(f, "r3", 84.5, "2026-08-15T20:00:00+00:00")
        rows = {r["dir"]: r for r in library.list_reviews(f)}
        assert rows["r1"]["pb"]["first_time"] and not rows["r1"]["pb"]["is_pb"]
        assert rows["r2"]["pb"]["is_pb"] and rows["r2"]["pb"]["delta"] == -1.0
        assert not rows["r3"]["pb"]["is_pb"] and rows["r3"]["pb"]["delta"] == 0.5
        assert rows["r2"]["irating"] == 1100 and rows["r2"]["consistency"]["score"] == 71
        assert rows["r1"]["irating"] is None and rows["r1"]["result"] is None
        assert rows["r1"]["season"]["estimated"] is True and rows["r2"]["season"]["estimated"] is False
        assert rows["r1"]["track_id"] == 468  # borrowed from r2 (same names)
        out = library.rebuild(f)
        html = out.read_text(encoding="utf-8")
        assert "/*EXTRA*/" not in html and "/*ROWS*/" not in html


def test_season_from_date():
    assert library.season_from_date("2026-10-25")["name"] == "2026 Season 4"
    assert library.season_from_date("2026-09-14")["name"] == "2026 Season 3"
    assert library.season_from_date("2026-12-20")["name"] == "2027 Season 1"
    assert library.season_from_date("2026-01-10")["name"] == "2026 Season 1"
    assert library.season_from_date("") is None


# ---------------------------------------------------------------- goals
def test_goals_crud_and_progress():
    with Tmp() as f:
        assert goals.load(f) == []
        g1 = goals.save_goal(f, {"track": ROVAL, "car": GT4, "target_s": "1:23.500", "note": "top 5"})
        assert g1["target_s"] == 83.5 and g1["id"]
        g2 = goals.save_goal(f, {"id": g1["id"], "track": ROVAL, "car": GT4, "target_s": 84.2})
        assert g2["id"] == g1["id"] and len(goals.load(f)) == 1 and goals.load(f)[0]["target_s"] == 84.2
        _review(f, "a", 85.0, "2026-09-01T20:00:00+00:00")
        _review(f, "b", 84.4, "2026-09-08T20:00:00+00:00")
        pr = goals.progress(f)[0]
        assert pr["best"] == 84.4 and pr["gap"] == 0.2  # gap = best - target, positive = still to find
        assert pr["achieved"] is False and [h["best"] for h in pr["history"]] == [85.0, 84.4]
        assert abs(pr["pct"] - 0.75) < 1e-9  # (85.0-84.4)/(85.0-84.2)
        goals.save_goal(f, {"id": g1["id"], "track": "Roval", "car": GT4, "target_s": 84.5})  # short name matches
        pr = goals.progress(f)[0]
        assert pr["achieved"] is True and pr["gap"] == -0.1 and pr["pct"] == 1.0
        assert goals.delete_goal(f, g1["id"]) is True and goals.delete_goal(f, g1["id"]) is False
        assert goals.load(f) == []


def test_goals_validation():
    with Tmp() as f:
        bad = [{"track": ROVAL, "car": GT4, "target_s": -5}, {"track": ROVAL, "car": GT4, "target_s": "abc"},
               {"track": "", "car": GT4, "target_s": 84}, {"track": ROVAL, "car": GT4, "target_s": "1:75.0"},
               {"track": ROVAL, "car": GT4, "target_s": 84, "note": "x" * 501},
               {"track": ROVAL, "car": GT4, "target_s": float("nan")},
               {"id": "nope", "track": ROVAL, "car": GT4, "target_s": 84}]
        for b in bad:
            try:
                goals.save_goal(f, b)
            except ValueError:
                continue
            raise AssertionError(f"accepted {b}")
        assert goals.load(f) == []


def test_goals_damaged_file_reads_empty():
    with Tmp() as f:
        (f / "goals.json").write_text("{not json", encoding="utf-8")
        assert goals.load(f) == [] and goals.progress(f) == []


# ---------------------------------------------------------------- results
def test_results_validation():
    ok = results.validate({"finish_pos": "7", "start_pos": 9, "field_size": 18, "incidents": 2,
                           "irating_before": 1102, "irating_after": 1117, "sr_before": "2.93", "sr_after": 3.02,
                           "notes": "  good race ", "junk": 1})
    assert ok["finish_pos"] == 7 and ok["positions_gained"] == 2  # start - finish
    assert ok["irating_change"] == 15 and ok["sr_change"] == 0.09  # after - before
    assert ok["notes"] == "good race" and "junk" not in ok
    lost = results.validate({"finish_pos": 12, "start_pos": 10, "irating_before": 1071, "irating_after": 1058})
    assert lost["positions_gained"] == -2 and lost["irating_change"] == -13
    bad = [{}, {"finish_pos": 0}, {"finish_pos": -3}, {"finish_pos": 2.5}, {"finish_pos": "first"},
           {"finish_pos": 5, "field_size": 4}, {"finish_pos": 1, "start_pos": 30, "field_size": 20},
           {"finish_pos": 1, "incidents": -1}, {"finish_pos": 1, "sr_after": 5.5}, {"finish_pos": True},
           {"finish_pos": 1, "notes": "n" * 501}, {"finish_pos": float("inf")}]
    for b in bad:
        try:
            results.validate(b)
        except ValueError:
            continue
        raise AssertionError(f"accepted {b}")


def test_results_save_load_delete_rerenders():
    with Tmp() as f:
        _review(f, "race1", 84.0, "2026-09-01T20:00:00+00:00")
        res = results.save(f, "race1", {"finish_pos": 3, "field_size": 20, "incidents": 0})
        d = f / "reviews" / "race1"
        assert results.load(d)["finish_pos"] == 3
        data = json.loads((d / "review.json").read_text(encoding="utf-8"))
        assert data["result"]["finish_pos"] == 3 and res["finish_pos"] == 3
        assert '"result"' in (d / "index.html").read_text(encoding="utf-8")
        assert library.list_reviews(f)[0]["result"]["finish_pos"] == 3
        assert not [p for p in d.iterdir() if p.name.startswith(".")]  # no temp leftovers
        assert results.delete(f, "race1") is True
        assert results.load(d) is None and "result" not in json.loads((d / "review.json").read_text(encoding="utf-8"))
        assert results.delete(f, "race1") is False
        for name in ("../race1", "nope", "", "race1/../x"):
            try:
                results.save(f, name, {"finish_pos": 1})
            except ValueError:
                continue
            raise AssertionError(name)


# ---------------------------------------------------------------- prep helpers
def test_prep_target_sign_convention():
    # r < 0: higher metric went with quicker corner times -> target the 75th percentile
    t = prep._target({"metric": "min_mph", "r": -0.6, "n": 12, "p25": 60.0, "p75": 64.0}, apex=500.0)
    assert "64 mph" in t[0] and "more minimum speed" in t[1]
    # r > 0: lower went with quicker -> 25th percentile
    t = prep._target({"metric": "min_mph", "r": 0.6, "n": 12, "p25": 60.0, "p75": 64.0}, apex=500.0)
    assert "60 mph" in t[0] and "less" in t[1]
    # brake_on is a lap distance: later braking (higher) with r < 0 -> p75, shown as metres before apex
    t = prep._target({"metric": "brake_on", "r": -0.7, "n": 12, "p25": 380.0, "p75": 395.0}, apex=500.0)
    assert "105 m before the apex" in t[0] and "later" in t[1]
    t = prep._target({"metric": "brake_on", "r": 0.7, "n": 12, "p25": 380.0, "p75": 395.0}, apex=500.0)
    assert "120 m before the apex" in t[0] and "earlier" in t[1]


def test_prep_build_offline_and_unraced():
    with Tmp() as f:
        _review(f, "a", 85.0, "2026-09-01T20:00:00+00:00", ids=(468, 190))
        _review(f, "b", 84.4, "2026-09-08T20:00:00+00:00", ids=(468, 190), std_clean=0.6)
        d = prep.build(None, f, 468, 190, None, log=lambda m: None)
        assert d["raced"] and d["history"]["races"] == 2 and d["history"]["best"] == 84.4
        assert d["history"]["trend_s_per_race"] == -0.6 and 3 <= len(d["plan"]) <= 5
        assert d["weak"][0]["id"] == "C1" and d["weak"][0]["top3"] == 2 and d["requests"] == 0
        assert prep.write(f, d).exists()
        u = prep.build(None, f, 380, 152, None, log=lambda m: None)
        assert not u["raced"] and u["history"] is None and 3 <= len(u["plan"]) <= 5
        c = prep.combos(f)
        assert c == [{"track": ROVAL, "car": GT4, "track_short": "Roval", "track_id": 468, "car_id": 190,
                      "races": 2, "last_date": library.local_day("2026-09-08T20:00:00+00:00"), "best": 84.4}]



# ---------------------------------------------------------------- regression tests (integration fixes)
NASTY = "/*EXTRA*/null /*DATA*/null /*ROWS*/[] /*PREP*/null /*TEAM*/null <!--<script> </script> & done"


def _browser(path, hash_="", script=None):
    """Open a page in headless Chromium. Returns (errors, value of `script` evaluated after load),
    or None when Playwright isn't installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    errs = []
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception:  # noqa: BLE001  (Playwright installed without its browser, e.g. on CI)
            return None
        try:
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
            pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error"
                  and "fonts.g" not in (m.location or {}).get("url", "") and "ERR_" not in m.text else None)
            pg.goto(Path(path).resolve().as_uri() + hash_)
            pg.wait_for_timeout(300)
            val = pg.evaluate(script) if script else None
        finally:
            b.close()
    return errs, val


def test_library_tooltips_escape_garage61_and_user_text():
    evil = '<img src=x onerror="window.__xss=1">'
    with Tmp() as f:
        _review(f, "a", 84.5, "2026-09-01T20:00:00+00:00", car="GT4 " + evil,
                season={"id": 1, "name": "2026 S4 " + evil, "shortName": "S4" + evil}, irating=1500)
        _review(f, "b", 84.2, "2026-09-08T20:00:00+00:00", car="GT4 " + evil,
                season={"id": 1, "name": "2026 S4 " + evil, "shortName": "S4" + evil}, irating=1520)
        goals.save_goal(f, {"track": ROVAL, "car": "GT4 " + evil, "target_s": 84.0, "note": evil})
        page = library.rebuild(f)
        js = """async () => {
          const out = [];
          for (const tab of ['#season', '#overview', '#goals']) {
            location.hash = tab; await new Promise(r => setTimeout(r, 200));
            for (const n of document.querySelectorAll('svg rect, svg circle')) {
              const b = n.getBoundingClientRect(); if (!b.width) continue;
              n.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: b.x + b.width / 2, clientY: b.y + 2}));
              const t = document.querySelector('.tip, #tip'); if (t && !t.hidden) out.push(t.textContent);
            }
          }
          await new Promise(r => setTimeout(r, 200));
          return {xss: window.__xss || null, imgs: document.querySelectorAll('.tip img, #tip img').length,
                  tips: out.filter(x => x.includes('<img')).length};
        }"""
        r = _browser(page, "#season", js)
        if r is None:
            print("  SKIPPED: playwright not installed")
            return
        errs, v = r
        assert v["xss"] is None and v["imgs"] == 0, v
        assert v["tips"] >= 1, v              # the names show up as text in the tooltips
        assert not errs, errs


def test_marker_and_script_text_in_notes_never_breaks_pages():
    from test_analysis import mock_race
    from debrief import report
    data = mock_race()
    if data is None:
        print("  SKIPPED: mock data not found")
        return
    with Tmp() as f:
        _review(f, "old", 85.0, "2026-09-01T20:00:00+00:00", ids=(468, 190),
                result={"finish_pos": 3, "notes": NASTY})
        d = f / "reviews" / "new"
        d.mkdir(parents=True)
        data["combo_ids"] = {"track_id": 468, "car_id": 190}
        data["result"] = {"finish_pos": 5, "field_size": 20, "notes": NASTY}
        data["meta"]["driver"] = "Swazi " + NASTY
        review_page = report.write(d, data)
        goals.save_goal(f, {"track": ROVAL, "car": GT4, "target_s": 83.0, "note": NASTY})
        lib_page = library.rebuild(f)
        prep_page = prep.write(f, prep.build(None, f, 468, 190, None, log=lambda m: None))
        checks = [(review_page, "", "window.D && window.D.result.notes"),
                  (lib_page, "#overview", "window.ROWS.length === 2 && window.EXTRA.goals[0].goal.note"),
                  (lib_page, "#season", "window.ROWS.find(r => r.result && r.result.finish_pos === 3).result.notes"),
                  (lib_page, "#goals", "window.EXTRA.goals[0].goal.note"),
                  (prep_page, "", "window.PREP.history.races_list.find(r => r.result).result.notes")]
        for page, hash_, js in checks:
            html = Path(page).read_text(encoding="utf-8")
            assert "<!--<script>" not in html and NASTY not in html, page   # embedded escaped, not raw
            r = _browser(page, hash_, js)
            if r is None:
                print("  SKIPPED (browser part): playwright not installed")
                return
            errs, v = r
            assert not errs, (page, hash_, errs)
            assert v == NASTY, (page, hash_, v)


def test_unclean_or_partial_best_never_a_pb_or_goal():
    with Tmp() as f:
        _review(f, "a", 84.5, "2026-09-01T20:00:00+00:00", ids=(468, 190))
        # every lap flagged: best is display-only
        _review(f, "b", 83.0, "2026-09-08T20:00:00+00:00", ids=(468, 190))
        rj = f / "reviews" / "b" / "review.json"
        rj.write_text(json.dumps({**json.loads(rj.read_text()), "best": {"n": 3, "t": 83.0, "clean": False}}))
        # an old review (no flag) whose "best" is a partial lap: 6.9 s vs an 85 s median
        _review(f, "c", 6.9, "2026-09-10T20:00:00+00:00", ids=(468, 190), median=85.0)
        rows = {r["dir"]: r for r in library.list_reviews(f)}
        assert rows["a"]["best_clean"] and not rows["b"]["best_clean"] and not rows["c"]["best_clean"]
        assert rows["b"]["pb"]["is_pb"] is False and rows["c"]["pb"]["is_pb"] is False
        pb = library.pb_for(f, {**_new(84.4, "2026-10-01T20:00:00+00:00", ids=(468, 190))})
        assert pb["is_pb"] is True and pb["previous_best"] == 84.5     # b and c don't count as the PB to beat
        bad = _new(82.0, "2026-10-01T20:00:00+00:00", ids=(468, 190))
        bad["best"]["clean"] = False
        pb = library.pb_for(f, bad)
        assert pb["is_pb"] is False and pb["first_time"] is False and pb["delta"] is None and pb["previous_best"] == 84.5
        goals.save_goal(f, {"track": ROVAL, "car": GT4, "target_s": 84.0})
        gp = goals.progress(f)[0]
        assert gp["achieved"] is False and gp["best"] == 84.5, gp
        assert prep.combos(f)[0]["best"] == 84.5


def test_all_unclean_race_end_to_end_no_bogus_pb_goal_or_team_time():
    from test_analysis import mock_race
    from debrief import report, team
    data = mock_race(all_unclean=True)
    if data is None:
        print("  SKIPPED: mock data not found")
        return
    with Tmp() as f:
        _review(f, "earlier", 84.5, "2026-09-01T20:00:00+00:00", ids=(468, 190))
        data["combo_ids"] = {"track_id": 468, "car_id": 190}
        assert data["best"]["t"] < 84.5                     # quicker, but every lap was flagged
        data["pb"] = library.pb_for(f, data)
        assert data["pb"]["is_pb"] is False and data["pb"]["first_time"] is False
        d = f / "reviews" / "unclean"
        d.mkdir(parents=True)
        report.write(d, data)
        goals.save_goal(f, {"track": ROVAL, "car": GT4, "target_s": 84.0})
        assert goals.progress(f)[0]["achieved"] is False
        rows = {r["dir"]: r for r in library.list_reviews(f)}
        assert rows["unclean"]["pb"]["is_pb"] is False
        assert [c["best"] for c in team.library_combos(f)] == [84.5]   # team board uses the clean best
        errs = _browser(d / "index.html", "", "document.getElementById('pbBanner').classList.contains('pb')")
        if errs is not None:
            assert not errs[0] and errs[1] is False, errs       # no "New personal best" banner


def test_prep_drills_dont_mix_up_corners_with_the_same_name():
    with Tmp() as f:
        # older race: its "C2" is at 3,000 m and isn't in the latest race
        _review(f, "old", 85.0, "2026-09-01T20:00:00+00:00", ids=(468, 190), L=3800.0,
                corners=[{"id": "C1", "apex_m": 200.0, "avail": 0.1, "rank": 2},
                         {"id": "C2", "apex_m": 3000.0, "avail": 0.6, "rank": 1}])
        tech = [{"id": "C2", "apex_m": 1500.0, "typical": {"min_mph": 60.0}, "ref": {"min_mph": 66.0},
                 "vs_ref": {"min_mph": -6.0}, "notes": []}]
        _review(f, "new", 84.5, "2026-09-08T20:00:00+00:00", ids=(468, 190), L=3800.0, technique=tech,
                corners=[{"id": "C1", "apex_m": 200.0, "avail": 0.1, "rank": 1},
                         {"id": "C2", "apex_m": 1500.0, "avail": 0.05, "rank": 2}])
        d = prep.build(None, f, 468, 190, None, log=lambda m: None)
        old = [w for w in d["weak"] if not w["in_latest"]]
        assert len(old) == 1 and old[0]["id"] == "C2" and old[0]["label"] == "older C2 (~3,000 m)"
        drills = [x for x in d["plan"] if x.get("corner") == "C2"]
        older = [x for x in drills if x["title"].startswith("older C2 (~3,000 m)")]
        assert len(older) == 1, [x["title"] for x in drills]
        assert "comparison lap" not in older[0]["evidence"]   # the latest race's C2 is a different corner
        latest = [x for x in drills if x["title"].startswith("C2:")]
        assert latest and "comparison lap" in latest[0]["evidence"]


def test_prep_teammates_keeps_much_quicker_driver_drops_partial_laps():
    class Api:
        requests_made = 0

        def me(self):
            return {"slug": "me"}

        def find_laps(self, **kw):
            mk = lambda slug, t, **x: {"lapTime": t, "driver": {"slug": slug, "firstName": slug.title()}, **x}
            return [mk("me", 90.0), mk("jake", 70.0), mk("tyler", 6.0, incomplete=True), mk("sam", 30.0),
                    mk("dom", 91.0)]
    m = prep._teammates(Api(), 468, 190, "midfield-menace", 90.0, log=lambda x: None)
    names = [x["slug"] for x in m["laps"]]
    assert names == ["jake", "dom"], names       # 22% quicker teammate kept; 6 s and 30 s partial laps dropped
    assert m["skipped"] == 2


def test_replace_retries_briefly_locked_files():
    import os
    from debrief import team
    from debrief.desktop import settings_io
    real, fails = os.replace, {}

    def flaky(a, b):   # every file is "locked" twice (like antivirus holding it), then works
        fails[str(a)] = fails.get(str(a), 0) + 1
        if fails[str(a)] <= 2:
            e = PermissionError(13, "The process cannot access the file because it is being used")
            e.winerror = 32
            raise e
        return real(a, b)
    with Tmp() as f:
        _review(f, "a", 84.5, "2026-09-01T20:00:00+00:00", ids=(468, 190))
        st = f / "settings.toml"
        st.write_text(f'token = "mock-token-123"\nlibrary_folder = \'{f}\'\n', encoding="utf-8")
        old_env = os.environ.get("DEBRIEF_SETTINGS")
        os.environ["DEBRIEF_SETTINGS"] = str(st)
        os.replace = flaky
        try:
            assert library.rebuild(f).exists()
            goals.save_goal(f, {"track": ROVAL, "car": GT4, "target_s": 84.0})
            results.save(f, "a", {"finish_pos": 4})
            assert prep.write(f, prep.build(None, f, 468, 190, None, log=lambda m: None)).exists()
            assert team.write(f, {"combos": [], "h2h": [], "team": {"name": "T"}}).exists()
            settings_io.save({"team_slug": "midfield-menace"})
            # always locked: gives up after fsutil.RETRIES tries and re-raises
            from debrief import fsutil
            fails.clear()
            fsutil_calls = []

            def locked(a, b):
                fsutil_calls.append(a)
                raise PermissionError(13, "locked")
            os.replace = locked
            try:
                library.rebuild(f)
                raise AssertionError("expected PermissionError")
            except PermissionError:
                pass
            assert len(fsutil_calls) == fsutil.RETRIES
            # other errors are not retried
            fsutil_calls.clear()

            def missing(a, b):
                fsutil_calls.append(a)
                raise FileNotFoundError(2, "gone")
            os.replace = missing
            try:
                fsutil.replace(f / "x", f / "y")
                raise AssertionError("expected FileNotFoundError")
            except FileNotFoundError:
                pass
            assert len(fsutil_calls) == 1
        finally:
            os.replace = real
            if old_env is None:
                os.environ.pop("DEBRIEF_SETTINGS", None)
            else:
                os.environ["DEBRIEF_SETTINGS"] = old_env
        assert json.loads((f / "goals.json").read_text())["goals"][0]["target_s"] == 84.0
        assert "midfield-menace" in st.read_text()


if __name__ == "__main__":
    n = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
