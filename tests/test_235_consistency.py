"""Debrief 2.35 "no contradictions": every fact on the review page has one owner, and every other section that mentions
it repeats the owner's value or says plainly why it differs. Each test gives the page deliberately mismatched data and
checks it shows one value (or the one plain note near the top); a consistent review shows no note.

    python3 tests/ci_run.py tests/test_235_consistency.py

No network; temp folders from tempfile; the browser tests are skipped without Playwright + Chromium.
"""
from __future__ import annotations

import copy
import json
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = Path(tempfile.mkdtemp(prefix="debrief-235c-"))
ST = TMP / "settings.toml"
ST.write_text(f"library_folder = '{TMP / 'lib'}'\n", encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import make_fixture_library as MFL  # noqa: E402
from debrief import localrace, recordings, report, results  # noqa: E402
import test_235_seeit as SEEIT  # noqa: E402
from test_235_seeit import SUMMARY, _loop, _sec  # noqa: E402

START = "2026-10-01T23:30:00+00:00"


def _review(sec=None, result=None, strategy=None, racecap=None, laps_note=None, lap_check="same"):
    data = MFL.fake_review(MFL.ROVAL, START, 0.4, True, MFL.S4, 1100, None, random.Random(7))
    data["meta"]["session"] = "Race"
    data["map"] = _loop()
    if sec is not False:
        v = recordings.view(sec or _sec(), SUMMARY, laps_note=laps_note)
        v["facts"]["lap_check"] = lap_check
        data["from_iracing"] = v
    if result is not None:
        data["result"] = result
    if strategy is not None:
        data["strategy"] = strategy
    if racecap is not None:
        data["racecap"] = racecap
    return data


def _write(name, data):
    d = TMP / name / "reviews" / "2026-10-01_charlotte_ford_ev1"
    d.mkdir(parents=True, exist_ok=True)
    report.write(d, data)
    return d


# a race result that agrees with _sec(): P1 of 16 in class, P4 of 31 overall, 6 incident points, from the telemetry file
GOOD = {"finish_pos": 1, "field_size": 16, "overall_pos": 4, "overall_size": 31, "start_pos": 3, "incidents": 6,
        "source": "iracing", "saved_at": START}


# ================================================================ Python: the owner's words
def test_finish_words_are_one_phrase():
    fw = recordings.finish_words
    assert fw(1, 16, 4, 31) == "P1 in your class of 16, P4 of 31 overall"
    assert fw(None, None, 4, 31) == "P4 of 31" and fw(1) == "P1 in your class" and fw() == ""
    v = recordings.view(_sec(), SUMMARY)
    pos = next(b for b in v["blocks"] if b["key"] == "position")
    assert pos["lines"] == ["You finished P1 in your class of 16, P4 of 31 overall."], pos["lines"]
    assert v["facts"]["finish"] == {"pos": 4, "cpos": 1, "size": 31, "class_size": 16, "multi": True, "last_row": False}
    one = recordings.view(_sec(multi=False), SUMMARY)
    assert next(b for b in one["blocks"] if b["key"] == "position")["lines"] == ["You finished P4 of 31."]
    tyres = next(b for b in v["blocks"] if b["key"] == "tyres")
    assert any(l.startswith("At the end of the session: track 31.0 C") for l in tyres["lines"]), "the chips give the middle value"


def test_result_from_iracing_records_its_source_and_overall_place():
    drivers = [{"CarIdx": 0, "IRating": 2000, "CarClassID": 1}, {"CarIdx": 3, "IRating": 1700, "CarClassID": 1},
               {"CarIdx": 5, "IRating": 3000, "CarClassID": 2}, {"CarIdx": 6, "IRating": 3100, "CarClassID": 2}]
    si = {"DriverInfo": {"DriverCarIdx": 3, "Drivers": drivers},
          "SessionInfo": {"Sessions": [{"SessionNum": 2, "ResultsPositions": [
              {"CarIdx": 5, "Position": 1, "ClassPosition": 0}, {"CarIdx": 3, "Position": 3, "ClassPosition": 1, "Incidents": 4}]}]}}
    r = localrace.result_from({"session_info": si, "race_num": 2})
    assert (r["finish_pos"], r["field_size"], r["overall_pos"], r["overall_size"], r["source"]) == (2, 2, 3, 4, "iracing"), r
    r2 = localrace.result_from({"session_info": si, "race_num": 2}, {"race": {"finish": {"pos": 2, "cpos": 1}}})
    assert (r2["finish_pos"], r2["overall_pos"], r2["source"]) == (1, 2, "recording"), r2
    assert "source" not in localrace.result_from({"session_info": {}, "race_num": 0})
    assert set(localrace.EXTRA_RESULT) == {"source", "overall_pos", "overall_size"}


def test_a_result_saved_from_the_form_is_yours():
    d = _write("save", _review(sec=False))
    res = results.save(TMP / "save", d.name, {"finish_pos": 3, "field_size": 20})
    assert res["source"] == "you"
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert data["agree"]["source_label"] == "Entered by you" and data["result"]["source"] == "you"


def test_agree_finish_incidents_fuel_and_pits():
    a = recordings.agree(_review(result=GOOD))
    assert a["finish"]["words"] == "P1 in your class of 16, P4 of 31 overall" and a["finish"]["value"] == "P1"
    assert a["finish"]["ctx"] == "in your class of 16, P4 of 31 overall"
    assert a["blocks"]["position"]["lines"] == ["You finished P1 in your class of 16, P4 of 31 overall, the same as the race result."]
    assert "notes" not in a and "incidents" not in a["blocks"], a
    assert a["fuel"]["median_l"] == 3.0 and "same figure as in From iRacing" in a["fuel"]["src"]
    assert a["source_label"].startswith("From iRacing")
    # (9) a typed place in a multi-class race isn't assumed to be either: said as typed, beside the recording's words
    typed = {k: v for k, v in GOOD.items() if not k.startswith("overall")}
    t = recordings.agree(_review(result=dict(typed, finish_pos=4, field_size=31, source="you")))
    assert t["finish"]["words"] == "P4 of 31" and t["finish"]["kind"] == "unknown" and "notes" not in t, t
    assert t["blocks"]["position"]["lines"] == ["You finished P1 in your class of 16, P4 of 31 overall. The race result says P4 of 31."]
    # ... and the recording's class place confirms a typed class place
    assert recordings.agree(_review(result=dict(typed, source="you")))["finish"]["words"] == "P1 in your class of 16, P4 of 31 overall"
    # a mismatch: one plain sentence, which was used and why
    bad = recordings.agree(_review(result=dict(typed, finish_pos=3, incidents=4, source="you")))
    fn = next(n for n in bad["notes"] if n["fact"] == "finish")["text"]
    assert "P1 in your class of 16, P4 of 31 overall" in fn and "the race result says P3 of 16." in fn and "because you entered it" in fn, fn
    inc = next(n for n in bad["notes"] if n["fact"] == "incidents")["text"]
    assert inc == "iRacing counted 6 incident points for you while Debrief was recording, but the race result says 4. This page uses 4 because you entered it.", inc
    # a team race: the result counts the whole team, the recording you
    sec = _sec()
    sec["incidents"]["team_total"] = 10
    team = recordings.agree(_review(sec=sec, result=dict(GOOD, incidents=10)))
    assert team["blocks"]["incidents"]["lines"] == ["6 of your team's 10 incident points were yours. The race result counts the whole team, so it says 10."]
    assert "notes" not in team
    # old result.json (no source): a neutral label
    assert recordings.agree(_review(sec=False, result={"finish_pos": 5}))["source_label"] == "Saved with this review"


def test_lap_numbers_are_matched_on_lap_times_and_pit_laps_agree():
    data = _review(sec=False)
    laps = [{"lap": l["n"] - 1, "time": l["t"], "pit": False} for l in data["laps"]]       # iRacing one lower
    assert recordings.lap_offset(data["laps"], laps) == 1
    assert recordings.lap_offset(data["laps"], [{"lap": l["n"], "time": l["t"]} for l in data["laps"]]) == 0
    assert recordings.lap_offset(data["laps"], [{"lap": n, "time": 70.0 + n} for n in range(1, 20)]) is None
    assert recordings.lap_offset(data["laps"], laps[:2]) is None, "two laps are too few to be sure"
    # end to end through for_review: the recording's laps renumbered to the review's
    sec = _sec(laps=20)
    sec["lap_times"] = laps
    sec["pit_stops"][0]["lap"] = 13                       # review lap 14
    data["counts"]["pit"] = [14, 15]
    old = recordings.sections, recordings.find
    try:
        recordings.sections = lambda lib: [{"id": "rec1#2", "kind": 3, "start": START, "end": START, "track": data["meta"]["track"],
                                            "car": data["meta"]["car"], "subsession": None}]
        recordings.find = lambda lib, rid: (copy.deepcopy(sec), SUMMARY)
        v = recordings.for_review(data, TMP)
    finally:
        recordings.sections, recordings.find = old
    assert v["facts"]["lap_check"] == "shifted" and v["facts"]["lap_shift"] == 1 and v["facts"]["pit_laps"] == [14]
    assert any("renumbered to match" in n for n in v["notes"]), v["notes"]
    assert v["charts"]["position"]["laps"][0]["lap"] == 2 and v["charts"]["fuel"]["laps"][0]["lap"] == 2
    data["from_iracing"] = v
    a = recordings.agree(data)
    assert a["pit_line"] == "Pit stop on lap 14 (from iRacing). Laps 14 and 15 include time in the pit lane, so they are left out of your pace numbers.", a
    # a stop Garage 61 didn't mark says so; numbering that couldn't be checked gives no pit line and says so in the section
    data["counts"]["pit"] = [9]
    assert any(n["fact"] == "pit_laps" for n in recordings.agree(data)["notes"])
    data["from_iracing"]["facts"]["lap_check"] = "unknown"
    assert "pit_line" not in recordings.agree(data)
    assert "may be one off" in recordings.LAPS_UNSURE


# ================================================================ the page
def _browser(fn, width=1280):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("  (skipped: no Playwright)")
        return
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": width, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
            pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
            pg.route("**/fonts.gstatic.com/**", lambda r: r.abort())
            fn(pg)
            b.close()
            assert not errs, errs
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            print("  (skipped: no Chromium)")
            return
        raise


def _open(pg, d):
    pg.goto((d / "index.html").as_uri())
    pg.wait_for_selector("#fromIracing:not([hidden]), #resultPanel:not([hidden])", timeout=15000)
    assert pg.locator(".secfail").count() == 0, pg.eval_on_selector_all(".secfail", "els => els.map(e => e.textContent)")


def test_a_consistent_review_shows_one_value_and_no_note():
    d = _write("ok", _review(result=GOOD, strategy={"burn": {"median_l": 3.0, "n": 20}, "level": {"start_l": 60.0}}))

    def go(pg):
        _open(pg, d)
        assert pg.locator("#agreeNote").is_hidden(), pg.text_content("#agreeNote")
        assert pg.text_content("#resultSub") == "From iRacing (the results in your telemetry file)"
        tile = pg.text_content("#resultStats")
        assert "P1in your class of 16, P4 of 31 overall" in tile, tile
        fi = pg.text_content("#fiBody")
        assert "You finished P1 in your class of 16, P4 of 31 overall, the same as the race result." in fi
        fuel = pg.text_content("#fuelRows")
        assert "3.00 L (0.79 gal)" in fuel and "from iRacing, recorded by Debrief" in fuel, fuel
    _browser(go)


def test_mismatched_sources_show_one_value_and_one_plain_note():
    typed = {k: v for k, v in GOOD.items() if not k.startswith("overall")}
    data = _review(result=dict(typed, finish_pos=3, incidents=4, source="you", pit_stops=2),
                   strategy={"burn": {"median_l": 2.71, "p90_l": 2.9, "n": 18}, "level": {"start_l": 58.0, "end_l": 4.0}})
    d = _write("bad", data)

    def go(pg):
        _open(pg, d)
        note = pg.text_content("#agreeNote")
        assert pg.locator("#agreeNote").is_visible()
        assert "Debrief's recording of iRacing has you finishing P1 in your class of 16, P4 of 31 overall, but the race result says " \
            "P3 of 16. This page uses the race result because you entered it." in note, note
        assert "This page uses 4 because you entered it." in note, note
        assert "Pit stops" not in note, "a typed pit stop count isn't compared with the recording's (defined differently)"
        assert pg.text_content("#resultSub") == "Entered by you"
        body, fuel = pg.inner_text("body"), pg.text_content("#fuelRows")
        assert "2.71" not in body and "58.0 L" not in body, "one litres-a-lap figure, one starting level"
        assert "thirstier" not in fuel and "3.00 L (0.79 gal) a lap" in fuel and "60.0 L" in fuel, fuel
        assert "4.00 L" not in fuel, "Garage 61's end level isn't put beside the recording's start level"
        fi = pg.text_content("#fiBody")
        assert "but the race result says 4" in fi and "6 incident points in this session" not in fi
    _browser(go)


def test_the_incident_map_places_incidents_as_the_corner_maps_do():
    d = _write("map", _review(result=GOOD))
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    ev = data["from_iracing"]["charts"]["incidents"]
    n, st, L = len(data["map"]), data["step"], data["L"]
    want = [round(e["d"] * L / st) % n for e in ev]

    def go(pg):
        _open(pg, d)
        got = pg.eval_on_selector_all("#fiBody circle.fiinc", "els => els.map(e => +e.dataset.i)")
        assert got == want, (got, want)
        labs = pg.eval_on_selector_all("#fiBody text.fiinclab", "els => els.map(e => e.textContent)")
        assert labs and all(lab.startswith("C") for lab in labs), labs
        svg = pg.locator("#fiBody .fichart svg").filter(has=pg.locator("circle.fiinc"))
        assert svg.locator("polyline").count() == 1 and svg.locator("line").count() >= 1, "the corner maps' line and start/finish tick"
        assert svg.locator("circle:not(.fiinc)").count() == 0, "no start/finish dot any more"
    _browser(go)


def _scan(laps):
    return {"version": 2, "subsession": 123, "official": True, "finish": 4, "class_finish": 1, "grid": 6,
            "laps": [{"lap": n, "pos": 5, "t": 86.0, "state": "clear"} for n in range(1, laps + 1)]}


def test_one_position_chart():
    rc = _write("rc", _review(result=GOOD, racecap=_scan(40)))     # the scan has more laps: drawn there only
    fi = _write("fi", _review(result=GOOD, racecap=_scan(10)))     # the recording has more: drawn in From iRacing only

    def go(pg):
        _open(pg, rc)
        assert pg.locator("#rlTimeline svg").count() == 1
        pos = pg.locator("#fiBody .panel").filter(has_text="Your position over the race")
        assert pos.locator(".fichart").count() == 0 and "drawn once, under Your race" in pos.text_content()
        assert pos.locator("details.finums table").count() == 1, "its numbers stay under Show the numbers"
        assert pg.locator("#agreeNote").is_hidden(), pg.text_content("#agreeNote")
        _open(pg, fi)
        assert pg.locator("#rlTimeline svg").count() == 0 and "drawn once, in From iRacing" in pg.text_content("#rlTimeline")
        assert pg.locator("#fiBody .panel").filter(has_text="Your position over the race").locator(".fichart svg").count() == 1
        assert "Started P6 overall" in pg.text_content("#rlChips"), "the scan's grid is overall; the result's start is your class"
    _browser(go)


def test_a_scan_that_disagrees_with_the_result_is_named_once():
    d = _write("scan", _review(sec=False, result=dict(GOOD, overall_pos=5), racecap=_scan(20)))

    def go(pg):
        _open(pg, d)
        note = pg.text_content("#agreeNote")
        assert "Finishing place overall: the race result says P5, but the replay scan says P4. The race result is the one this page goes by." in note, note
        assert note.count("Finishing place") == 1, note
    _browser(go)


def test_4_a_note_for_the_recording_still_lets_the_scan_be_compared():
    # result P2 in class (iRacing), recording P1 in class (a note), scan P3 in class: the scan's mismatch must show too
    d = _write("n4", _review(result=dict(GOOD, finish_pos=2, overall_pos=4), racecap=dict(_scan(10), class_finish=3)))

    def go(pg):
        _open(pg, d)
        note = pg.text_content("#agreeNote")
        assert "Debrief's recording of iRacing has you finishing P1 in your class" in note, note
        assert "Finishing place in your class: the race result says P2, but the replay scan says P3." in note, note
        assert "From iRacing says" not in note, "the recording's difference is said once, by its own note"
    _browser(go)


def _pit_scan(kinds):
    sc = _scan(10)
    sc["pit_cost"] = {"total_s": 90, "stops": [{"lap": 5 + i, "seconds": 40, "kind": k} for i, k in enumerate(kinds)]}
    return sc


def test_5_pit_counts_compare_only_like_with_like():
    ok = _write("p5a", _review(result=dict(GOOD, pit_stops=3), racecap=_pit_scan(["stop", "drive"])))      # a drive-through: not compared
    bad = _write("p5b", _review(result=dict(GOOD, pit_stops=1), racecap=_pit_scan(["stop", "stop"])))     # two stops against one

    def go(pg):
        _open(pg, ok)
        assert pg.locator("#agreeNote").is_hidden(), pg.text_content("#agreeNote")
        _open(pg, bad)
        assert "Pit stops: From iRacing says 1, but the replay scan says 2. From iRacing is the one this page goes by." in pg.text_content("#agreeNote")
    _browser(go)


def test_8_the_source_and_the_overall_place_survive_a_save():
    v = results.validate({"finish_pos": 1, "field_size": 16, "overall_pos": 4, "overall_size": 31, "source": "results_file"})
    assert (v["source"], v["overall_pos"], v["overall_size"]) == ("results_file", 4, 31)
    assert "source" not in results.validate({"finish_pos": 1, "source": "made up"})
    try:
        results.validate({"finish_pos": 1, "overall_pos": 9, "overall_size": 5})
        raise AssertionError("an overall place past the field is refused")
    except ValueError:
        pass
    d = _write("s8", _review(sec=False))
    res = results.save(TMP / "s8", d.name, {"finish_pos": 1, "field_size": 16, "overall_pos": 4, "overall_size": 31, "source": "results_file"})
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert res["source"] == "results_file" and data["result"]["overall_pos"] == 4
    assert data["agree"]["source_label"] == "From your iRacing results file"
    assert results.save(TMP / "s8", d.name, {"finish_pos": 2})["source"] == "you", "a typed result without a source is yours"


def test_9_a_typed_place_in_a_multiclass_race_is_said_as_typed():
    data = _review(sec=False, result={"finish_pos": 7, "field_size": 25, "source": "you"})
    data["meta"]["multiclass"] = True
    d = _write("m9", data)

    def go(pg):
        _open(pg, d)
        t = pg.text_content("#resultStats")
        assert "P7of 25" in t and "class" not in t, t
    _browser(go)


def test_10_the_position_chart_is_drawn_somewhere_even_when_the_chosen_one_cannot_be():
    # the recording has more laps, but its block draws no chart: the scan's timeline is drawn after all
    data = _review(result=GOOD, racecap=_scan(10))
    next(b for b in data["from_iracing"]["blocks"] if b["key"] == "position")["chart"] = None
    a = _write("c10a", data)
    # the scan has "more" laps but none it can draw: From iRacing draws its own and drops its pointer
    sc = _scan(40)
    sc["laps"] = [dict(x, lap=x["lap"] + 5000) for x in sc["laps"]]
    b = _write("c10b", _review(result=GOOD, racecap=sc))

    def go(pg):
        _open(pg, a)
        assert pg.locator("#rlTimeline svg").count() == 1 and "drawn once" not in pg.text_content("#rlTimeline")
        _open(pg, b)
        pos = pg.locator("#fiBody .panel").filter(has_text="Your position over the race")
        assert pos.locator(".fichart svg").count() == 1 and "drawn once" not in pos.text_content()
    _browser(go)


def test_12_an_older_from_iracing_section_is_checked_too():
    v = recordings.view(_sec(), SUMMARY)
    old = json.loads(json.dumps(v))
    old.pop("facts")
    for blk in old["blocks"]:
        blk.pop("key", None)
    next(blk for blk in old["blocks"] if blk["h"] == "Your position over the race")["lines"] = ["You finished P4 (P1 in your class)."]
    data = _review(sec=False, result=dict(GOOD, finish_pos=2))
    data["from_iracing"] = old
    d = _write("o12", data)            # no recording in this library: the saved section is upgraded in write()
    saved = json.loads((d / "review.json").read_text(encoding="utf-8"))
    f = saved["from_iracing"]["facts"]
    assert f["finish"]["pos"] == 4 and f["finish"]["cpos"] == 1 and f["incidents"]["total"] == 6 and f["fuel"]["median_l"] == 3.0, f
    assert any("may be one off" in n for n in saved["from_iracing"]["notes"])
    # an old view never said whether its finish was the final order (it may be only the last lap row): not compared
    assert f["finish"]["last_row"] is None
    assert not any(n["fact"] == "finish" for n in (saved.get("agree") or {}).get("notes") or []), saved.get("agree")

    def go(pg):
        _open(pg, d)
        fi = pg.text_content("#fiBody")
        assert "but the race result says" not in fi, fi
    _browser(go)


def test_pit_line_words_a_drive_through_as_a_pass_not_a_stop():
    sec = _sec()
    sec["pit_stops"] = [{"lap": 14, "lane_s": 52, "stall_s": 31}, {"lap": 20, "lane_s": 18, "stall_s": 0}]
    data = _review(sec=sec, result=GOOD)
    data["counts"]["pit"] = [14]
    f = data["from_iracing"]["facts"]
    assert f["pit_stops_n"] == 1 and f["pit_laps"] == [14, 20] and f["pass_laps"] == [20]
    line = recordings.agree(data)["pit_line"]
    assert line.startswith("Pit stop on lap 14 (from iRacing). You drove through the pit lane without stopping on lap 20 (from iRacing)."), line
    only = _sec()
    only["pit_stops"] = [{"lap": 20, "lane_s": 18, "stall_s": 0}]
    data2 = _review(sec=only, result=GOOD)
    line2 = recordings.agree(data2)["pit_line"]
    assert "Pit stop" not in line2 and line2.startswith("You drove through the pit lane"), line2


def test_an_old_view_finish_is_not_compared_as_final():
    data = _review(sec=False, result=dict(GOOD, overall_pos=4, finish_pos=1))
    v = recordings.view(_sec(), SUMMARY)
    v["facts"]["finish"].update(pos=5, cpos=2, last_row=None)
    data["from_iracing"] = v
    a = recordings.agree(data)
    assert not any(n["fact"] == "finish" for n in a.get("notes") or []), a
    v["facts"]["finish"]["last_row"] = False
    assert any(n["fact"] == "finish" for n in recordings.agree(data)["notes"])


def test_multiclass_without_a_class_place_keeps_the_overall_place_apart():
    drivers = [{"CarIdx": 0, "IRating": 2000, "CarClassID": 1}, {"CarIdx": 3, "IRating": 1700, "CarClassID": 1},
               {"CarIdx": 5, "IRating": 3000, "CarClassID": 2}, {"CarIdx": 6, "IRating": 3100, "CarClassID": 2}]
    si = {"DriverInfo": {"DriverCarIdx": 3, "Drivers": drivers},
          "SessionInfo": {"Sessions": [{"SessionNum": 2, "ResultsPositions": [{"CarIdx": 3, "Position": 3, "Incidents": 4}]}]}}
    r = localrace.result_from({"session_info": si, "race_num": 2})
    assert "finish_pos" not in r and r["overall_pos"] == 3 and r["overall_size"] == 4 and r["field_size"] == 2, r
    assert "source" not in r


def test_saving_a_result_does_not_read_telemetry_for_the_setup_section():
    from debrief import racesetup
    d = _write("nosetup", _review(sec=False))
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    data.pop("setup", None)
    (d / "review.json").write_text(json.dumps(data), encoding="utf-8")
    calls = []
    old, old_tf = racesetup.for_review, localrace.telemetry_folder
    racesetup.for_review = lambda *a, **k: calls.append(a) or None
    localrace.telemetry_folder = lambda *a, **k: str(TMP)
    try:
        results.save(TMP / "nosetup", d.name, {"finish_pos": 3, "field_size": 20})
        results.delete(TMP / "nosetup", d.name)
        assert not calls
        report.write(d, data)          # building a review does read it (so the check above can fail)
        assert calls, "a build asks for the Setup section"
        calls.clear()
    finally:
        racesetup.for_review, localrace.telemetry_folder = old, old_tf
    assert not calls, "saving or deleting a result must not read telemetry"


# ================================================================ 2.35: races against AI
def test_vsai_event_ids_differ_for_two_races_at_the_same_time_of_day():
    a = {"id": "mclaren_zolder 2026-11-09 20-00-00.ibt|x", "subsession": None}
    b = {"id": "mclaren_zolder 2026-11-10 20-00-00.ibt|x", "subsession": None}
    ea, eb = localrace.event_id(a), localrace.event_id(b)
    assert ea != eb and ea.startswith("ir") and len(ea) == 12, (ea, eb)
    assert localrace.event_id({"id": "x", "subsession": 81234567}) == "ir81234567"


def test_vsai_result_has_no_sof_or_irating_and_meta_says_vs_ai():
    import fake_ibt as F
    si = {"DriverInfo": {"DriverCarIdx": 0, "Drivers": [
        {"CarIdx": 0, "IRating": 1700, "CarClassID": 1}, {"CarIdx": 1, "IRating": 0, "CarClassID": 1, "CarIsAI": 1},
        {"CarIdx": 2, "IRating": 0, "CarClassID": 1, "CarIsAI": 1}]},
        "SessionInfo": {"Sessions": [{"SessionNum": 0, "ResultsPositions": [{"CarIdx": 0, "Position": 2}]}]}}
    r = localrace.result_from({"session_info": si, "race_num": 0})
    assert r["finish_pos"] == 2 and r["field_size"] == 3 and "sof" not in r and "irating_before" not in r, r
    # a review built from this PC: meta.ai_cars / vs_ai from the file's session info
    tel = TMP / "tel"
    tel.mkdir(exist_ok=True)
    F.make(tel / "mclaren570sgt4_zolder gp 2026-11-10 20-00-00.ibt", [99.4, 98.8, 98.6, 98.9, 98.7], session="Race",
           subsession=9100, start_date=1_794_340_000)
    s = localrace.races(folder=tel, cache_path=TMP / "ibt-cache.json")[0]
    real = localrace.read_laps

    def with_ai(session, eid):
        laps, t, facts = real(session, eid)
        drv = facts["session_info"].setdefault("DriverInfo", {}).setdefault("Drivers", [])
        drv += [{"CarIdx": 40 + i, "UserName": f"AI {i}", "CarIsAI": 1, "CarClassID": 999} for i in range(3)]
        return laps, t, facts
    localrace.read_laps = with_ai
    try:
        data, _out = localrace.build({"library_folder": str(TMP / "ai-lib")}, s)
    finally:
        localrace.read_laps = real
    assert data["meta"]["ai_cars"] == 3 and data["meta"]["vs_ai"] is True, data["meta"]
    assert "sof" not in (data.get("result") or {}) and "irating_before" not in (data.get("result") or {})


def test_vsai_a_recording_against_ai_marks_the_review():
    data = _review(sec=False)
    old = recordings.sections, recordings.find
    try:
        recordings.sections = lambda lib: [{"id": "rec1#2", "kind": 3, "start": START, "end": START, "track": data["meta"]["track"],
                                            "car": data["meta"]["car"], "subsession": None}]
        recordings.find = lambda lib, rid: (_sec(), dict(SUMMARY, ai_cars=7))
        assert recordings.for_review(data, TMP)
    finally:
        recordings.sections, recordings.find = old
    assert data["meta"]["ai_cars"] == 7 and data["meta"]["vs_ai"] is True


def test_vsai_page_has_the_tag_and_no_sof_or_irating():
    data = _review(sec=False, result=dict(GOOD, sof=2300, irating_before=2100, irating_after=2130))
    data["meta"].update(vs_ai=True, ai_cars=11)
    data["irating"] = 2100
    d = _write("ai", data)
    old = _write("notai", _review(sec=False, result=dict(GOOD, sof=2300, irating_before=2100, irating_after=2130)))

    def go(pg):
        _open(pg, d)
        chip = pg.locator("#chipAi")
        assert chip.text_content() == "vs AI" and "Strength of field and iRating don" in chip.get_attribute("title")
        body = pg.inner_text("body")
        assert "Strength of field" not in body and "iRating" not in body and "2300" not in body, body[:400]
        assert pg.locator("#agreeNote").is_hidden() and "Finished" in pg.text_content("#resultStats")
        _open(pg, old)
        assert pg.locator("#chipAi").count() == 0 and "Strength of field" in pg.text_content("#resultStats")
    _browser(go)


def test_setup_dials_are_checked_against_the_recording():
    def mk(name, val):
        sec = _sec()
        sec["aids"]["seen"]["bb"] = [54.0]
        data = _review(sec=sec, result=GOOD)
        data["setup"] = {"ok": True, "fixed": True, "setup": {"text": "Fixed setup series."},
                         "dials": [{"dial": "bias", "label": "Brake bias", "value": val, "from": "in-car dial", "text": f"Brake bias: {val}."}]}
        return _write(name, data)
    same, diff = mk("dial1", "54.00"), mk("dial2", "52.00")

    def go(pg):
        _open(pg, same)
        assert pg.locator("#agreeNote").is_hidden(), pg.text_content("#agreeNote")
        _open(pg, diff)
        assert "Brake bias: From iRacing says 54, but the Setup section says 52." in pg.text_content("#agreeNote")
    _browser(go)


def test_old_result_and_a_page_without_agree_still_draw():
    d = _write("old", _review(sec=False, result={"finish_pos": 5, "field_size": 20, "incidents": 2}))
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    data.pop("agree", None)
    page = d / "noagree.html"            # as restore_backup draws a page: the template and review.json only
    page.write_text(report.fill((report.TPL / "review.html").read_text(encoding="utf-8"),
                                {"/*DATA*/null": report._embed(data)}), encoding="utf-8")

    def go(pg):
        _open(pg, d)
        assert pg.text_content("#resultSub") == "Saved with this review" and "P5of 20" in pg.text_content("#resultStats")
        assert pg.locator("#agreeNote").is_hidden()
        pg.goto(page.as_uri())
        pg.wait_for_selector("#resultPanel:not([hidden])", timeout=15000)
        assert pg.text_content("#resultSub") == "Saved with this review" and pg.locator(".secfail").count() == 0
    _browser(go)


if __name__ == "__main__":
    import shutil
    import traceback
    failed = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(SEEIT.TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
