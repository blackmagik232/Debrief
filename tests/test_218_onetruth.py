"""Debrief 2.18 lane B: one truth across pages.

  - library.counts: the sidebar, Progress, Skills and Racecraft start from the same numbers, and the parts add up
    (saved = races + practice + no lap times); the Progress header and Skills empty state say which part they use;
  - skill scores are worked out for a review saved without them, and a race with none says why;
  - names.track / names.car: one spelling per track layout and car, idempotent, layouts never merged; the
    sidebar groups and the library rows use it;
  - units: Settings > Units (mph default, km/h), the text converter, the window's replies, the library pages'
    script, and the coach's spoken speeds; one lap-time format;
  - Review last race with a saved review: nothing is downloaded; Rebuild says what changed; a missing review page is
    made again from review.json, or explained with a Rebuild button (never a bare "Not found");
  - wording inside one review: the first lap's sign, a finish in the start's place after moving.

    python3 tests/test_218_onetruth.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-218-onetruth-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import test_28_page as T28  # noqa: E402
from debrief import app, insights, library, livecoach, names, profile, race_analysis, units  # noqa: E402
from debrief.desktop import server as S, settings_io  # noqa: E402

LIB = TMP / "lib"
ENV = {}


def review(name, track="Circuit Zolder (Grand Prix)", car="McLaren 570S GT4", session="Race", event="EV123456",
           started="2026-10-20T19:00:00+00:00", laps=True, skills=True):
    """A saved review from the full-page fixture (traces, corners, laps), renamed."""
    d = T28.base()
    d["meta"].update(track=track, car=car, session=session, event_id=event, started_at=started)
    if not laps:
        d["laps"], d["best"] = [], {}
    if not skills:
        d.pop("skills", None)
    out = LIB / "reviews" / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.json").write_text(json.dumps(d), encoding="utf-8")
    return out


def settings(extra=""):
    LIB.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\n" + extra, encoding="utf-8")


def srv():
    if "srv" not in ENV:
        settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def get(rel, header=True, raw=False):
    s = srv()
    rq = urllib.request.Request(s.url + rel, headers={"X-Debrief": "1"} if header else {})
    try:
        with ENV["opener"].open(rq, timeout=120) as r:
            body = r.read()
            return r.status, (body if raw else json.loads(body or b"null"))
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(rel, js):
    s = srv()
    rq = urllib.request.Request(s.url + rel, data=json.dumps(js).encode(), method="POST",
                                headers={"X-Debrief": "1", "Content-Type": "application/json"})
    try:
        with ENV["opener"].open(rq, timeout=120) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


# ================================================================ names
def test_track_names_one_spelling():
    same = ["Circuit Zolder (Grand Prix)", "Circuit Zolder - Grand Prix", "Circuit Zolder (Grand Prix) - Grand Prix",
            "Zolder Circuit - Grand Prix"]
    assert {names.track(x) for x in same} == {"Circuit Zolder (Grand Prix)"}, [names.track(x) for x in same]
    assert names.track("Circuit Zolder", "Grand Prix") == "Circuit Zolder (Grand Prix)"
    assert names.track("Circuit Zolder (Grand Prix)", "Grand Prix") == "Circuit Zolder (Grand Prix)"
    assert names.track("Charlotte Motor Speedway - Roval 2019") == "Charlotte Motor Speedway (Roval 2019)"
    # layouts are never merged
    assert names.track("Watkins Glen International (Boot)") != names.track("Watkins Glen International (Classic)")
    assert names.track("Lime Rock Park (Grand Prix)") != names.track("Lime Rock Park (Classic)")
    for x in same + ["Watkins Glen International (Boot)", "Road America", ""]:
        assert names.track(names.track(x)) == names.track(x), x


def test_car_names_one_spelling():
    assert names.car("Mclaren 570s GT4") == "McLaren 570S GT4"
    assert names.car("McLaren 570S GT4") == "McLaren 570S GT4"
    assert names.car("Global Mazda Mx-5 Cup") == "Global Mazda MX-5 Cup"
    for c in ("Porsche 911 GT3 R (992)", "Ford Mustang GT4", "BMW M4 G82 GT4 Evo".replace("Evo", "EVO")):
        assert names.car(c) == c, c
    assert names.car(names.car("Mclaren 720s GT3 Evo")) == names.car("Mclaren 720s GT3 Evo") == "McLaren 720S GT3 EVO"


def test_rows_and_sidebar_use_one_name():
    shutil.rmtree(LIB, ignore_errors=True)
    review("2026-10-20_circuit-zolder-grand-prix_mclaren-570s-gt4_aaaaaa", track="Circuit Zolder - Grand Prix",
           car="Mclaren 570s GT4", event="EVaaaaaa")
    review("2026-10-21_circuit-zolder-grand-prix_mclaren-570s-gt4_bbbbbb", event="EVbbbbbb",
           started="2026-10-21T19:00:00+00:00")
    rows = library.list_reviews(LIB)
    assert {(r["track"], r["car"]) for r in rows} == {("Circuit Zolder (Grand Prix)", "McLaren 570S GT4")}
    groups = S.ReviewIndex().list(LIB)
    assert len(groups) == 1 and len(groups[0]["reviews"]) == 2, groups


# ================================================================ counts
def test_counts_agree_and_add_up():
    shutil.rmtree(LIB, ignore_errors=True)
    review("2026-10-01_a_b_r00001", event="EVr00001", started="2026-10-01T19:00:00+00:00")
    review("2026-10-02_a_b_r00002", event="EVr00002", started="2026-10-02T19:00:00+00:00")
    review("2026-10-03_a_b_p00003_practice", session="Practice", event="EVp00003", started="2026-10-03T19:00:00+00:00")
    review("2026-10-04_a_b_n00004", event="EVn00004", started="2026-10-04T19:00:00+00:00", laps=False)
    c = library.counts(LIB)
    sidebar = sum(len(g["reviews"]) for g in S.ReviewIndex().list(LIB))
    rows = library.list_reviews(LIB)
    assert c["saved"] == sidebar == 4, (c, sidebar)
    assert c["with_laps"] == len(rows) == 3 and c["races"] == len(library.race_rows(rows)) == 2
    assert c["practice"] == 1 and c["no_laps"] == 1
    assert c["races"] + c["practice"] + c["no_laps"] == c["saved"]
    # the library page carries the same numbers, and its header says what isn't counted
    page = library.rebuild(LIB).read_text(encoding="utf-8")
    assert '"counts":{"saved":4,' in page.replace(" ", ""), page[:200]
    tpl = (ROOT / "debrief" / "templates" / "library.html").read_text(encoding="utf-8")
    assert "saved reviews have no lap times and aren\\u2019t counted" in tpl
    # the profile counts the same race rows (scored or not)
    prof = profile.build(rows)
    assert prof["n_rows"] == c["races"], prof


def test_skill_scores_for_reviews_saved_without_them():
    shutil.rmtree(LIB, ignore_errors=True)
    review("2026-10-01_a_b_s00001", event="EVs00001", skills=False)
    rows = library.list_reviews(LIB)
    assert rows[0]["skills"] is not None and rows[0]["skills_why"] is None, rows[0].get("skills_why")
    prof = profile.build(rows)
    assert prof["n_races"] == 1, prof["n_races"]
    # a review with no traces: no score, and the reason is there for the Skills tab
    d = T28.base()
    d.pop("traces")
    d.pop("skills", None)
    row = library._row_from(d, LIB / "reviews" / "x" / "review.json")
    assert row["skills"] is None and row["skills_why"] == "no_traces"
    prof = profile.build([row])
    assert prof["why"] == {"no_traces": 1} and prof["n_rows"] == 1
    tpl = (ROOT / "debrief" / "templates" / "library.html").read_text(encoding="utf-8")
    assert "Older reviews get them when Debrief refreshes the library" not in tpl
    assert "skWhyText()" in tpl


# ================================================================ units
def test_units_text_and_numbers():
    assert units.convert_text("Carry 3 km/h more (101 vs 99 km/h)", "mph") == "Carry 2 mph more (63 vs 62 mph)"
    assert units.convert_text("Carry 2 mph more at the slowest point (63.1 mph)", "kmh") == \
        "Carry 3 km/h more at the slowest point (101.5 km/h)"
    assert units.convert_text("4-20 km/h", "mph") == "2-12 mph"
    assert units.convert_text("slower by 1-2 km/h", "mph") == "slower by about 1 mph"
    assert units.convert_text("1 to 2 km/h", "mph") == "about 1 mph"
    assert units.convert_text("Turn 6 - 101 km/h", "mph") == "Turn 6 - 63 mph"   # a corner number is not a speed
    assert units.convert_text("63 mph", "mph") == "63 mph" and units.convert_text("40 km/h", "kmh") == "40 km/h"
    assert units.convert({"a": ["8 km/h"], "b": 3}, "mph") == {"a": ["5 mph"], "b": 3}
    assert units.lap(95.849) == "1:35.849" and units.lap(59.1234) == "59.123" and units.lap(None) == ""
    assert units.norm("km/h") == "kmh" and units.norm(None) == "mph"


def test_units_setting_default_and_save():
    settings()
    assert settings_io.public_view()["units"] == "mph" and settings_io.units() == "mph"
    v = settings_io.save({"units": "kmh"})
    assert v["units"] == "kmh" and settings_io.units() == "kmh"
    try:
        settings_io.save({"units": "furlongs"})
        raise AssertionError("bad unit saved")
    except ValueError:
        pass
    settings_io.save({"units": "mph"})
    assert settings_io.units() == "mph"


def test_window_replies_and_pages_use_the_unit():
    srv()
    settings_io.save({"units": "mph"})
    shutil.rmtree(LIB, ignore_errors=True)
    review("2026-10-01_a_b_u00001", event="EVu00001")
    h = S.Handler.__new__(S.Handler)
    sent = {}
    h._send = lambda code, body, *a, **k: sent.update(code=code, body=body)
    h._json({"text": "carry about 101 km/h (you usually have 99 km/h)"})
    assert json.loads(sent["body"])["text"] == "carry about 63 mph (you usually have 62 mph)", sent
    # library pages get the converter for the saved unit
    code, body = get("lib/reviews/2026-10-01_a_b_u00001/index.html", header=False, raw=True)
    assert code == 200 and b"debrief-units" in body and b"U='mph'" in body, code
    tag = S.with_units(b"<html><head></head><body></body></html>", "kmh")
    assert b"debrief-units" in tag and b"U='kmh'" in tag
    assert b"U='mph'" in S.with_units(b"<html><head></head><body></body></html>", "anything")


def test_coach_says_speeds_in_the_unit():
    st = livecoach.Settings(units="mph")
    assert livecoach.Settings().units == "mph"     # the coach's own default (mph since 2.17.2); the window passes the setting
    co = livecoach.Coach(settings=st)
    co._push(livecoach.Cue(text="T6: carry 8 km/h more", priority=1, key="feedback:C6"))
    assert [c.text for c in co.queue.items] == ["T6: carry 5 mph more"], [c.text for c in co.queue.items]
    assert livecoach.speed_text("T6", int(round(units.speed(8, "kmh", "mph")))) == "T6: more speed, 5 down"
    # the server hands the saved unit to the coach
    srv()
    settings_io.save({"units": "mph"})
    assert srv().coach_settings().units == "mph"
    settings_io.save({"units": "kmh"})
    assert srv().coach_settings().units == "kmh"
    settings_io.save({"units": "mph"})


# ================================================================ opening reviews
class _Api:
    requests_made = 0

    def me(self):
        return {"slug": "swazi-williams", "firstName": "Swazi", "lastName": "Williams"}

    def lap_csv(self, _id):
        raise AssertionError("telemetry was downloaded for a race that already has a saved review")


def test_review_last_race_opens_the_saved_one_without_downloading():
    shutil.rmtree(LIB, ignore_errors=True)
    d = review("2026-10-05_circuit-zolder-grand-prix_mclaren-570s-gt4_abc123", event="EVabc123")
    cfg = {"library_folder": str(LIB), "include_practice": False, "cache_dir": str(TMP / "cache"), "salutation": ""}
    real_api, real_laps = app.make_api, app.laps_for
    app.make_api = lambda _cfg: _Api()

    def no_laps(*a, **k):
        raise AssertionError("laps were searched for a race that already has a saved review")
    app.laps_for = no_laps
    try:
        info = {}
        page = app.cmd_review(cfg, "EVabc123", open_after=False, session_kind=3, track_id=1, car_id=2, info=info)
        assert Path(page).parent == d and info == {"saved": True}, (page, info)
        # the same with the newest race found by the search
        real_find = app.find_last_event
        app.find_last_event = lambda *a, **k: ("EVabc123", 1, 2)
        try:
            info = {}
            page = app.cmd_review(cfg, None, open_after=False, info=info)
            assert Path(page).parent == d and info.get("saved") is True
        finally:
            app.find_last_event = real_find
        # a practice review of the same event is not the race's review
        assert app.saved_review_dir(LIB, "EVabc123", 1) is None
    finally:
        app.make_api, app.laps_for = real_api, real_laps


def test_rebuild_says_what_changed():
    old = T28.base()
    new = json.loads(json.dumps(old))
    assert app.rebuild_changes(old, new) == ["Nothing changed: the same laps, best lap and comparison laps as the saved review."]
    new["laps"] = new["laps"] + [dict(new["laps"][-1], n=99)]
    new["best"] = dict(new["best"], t=new["best"]["t"] - 0.25)
    new["refs"] = [{"id": "r1", "t": 70.0}]
    ch = app.rebuild_changes(old, new)
    assert any(x.startswith("Laps: ") for x in ch) and any(x.startswith("Best lap: ") for x in ch), ch
    assert any(x.startswith("Comparison laps: ") for x in ch), ch
    assert app.rebuild_changes(None, new)[0].startswith("This review had no saved data")
    # the window's Rebuild finds the race from the saved review
    shutil.rmtree(LIB, ignore_errors=True)
    d = review("2026-10-05_x_y_def456", event="EVdef456")
    data = json.loads((d / "review.json").read_text())
    data["combo_ids"] = {"track_id": 199, "car_id": 135}
    (d / "review.json").write_text(json.dumps(data))
    p = S.review_rebuild_params(d)
    assert p == {"event": "EVdef456", "session_type": 3, "track_id": 199, "car_id": 135, "rebuild": True}, p
    assert S.clean_review_params({"event": "EV1", "session_type": 3, "track_id": 1, "car_id": 2}).get("rebuild") is None
    data.pop("combo_ids")
    (d / "review.json").write_text(json.dumps(data))
    assert S.review_rebuild_params(d)["track_id"] is None     # an older review: found by its event


def test_missing_review_page_is_remade_or_explained():
    srv()
    shutil.rmtree(LIB, ignore_errors=True)
    d = review("2026-10-06_a_b_ghi789", event="EVghi789")
    assert not (d / "index.html").exists()
    code, body = get("lib/reviews/" + d.name + "/index.html", header=False, raw=True)
    assert code == 200 and (d / "index.html").is_file() and b"debrief-frame-bridge" in body, code
    # no usable review.json: a page that says why and offers Rebuild, never "Not found"
    e = LIB / "reviews" / "2026-10-07_a_b_jkl000"
    e.mkdir(parents=True)
    (e / "review.json").write_text(json.dumps({"meta": {"event_id": "EVjkl000"}, "laps": []}))
    code, body = get("lib/reviews/" + e.name + "/index.html", header=False, raw=True)
    assert code == 200 and b"This review's page is missing" in body and b"Rebuild this review" in body, body[:300]
    f = LIB / "reviews" / "2026-10-08_a_b_mno000"
    f.mkdir(parents=True)
    code, body = get("lib/reviews/" + f.name + "/index.html", header=False, raw=True)
    assert code == 200 and b"can't tell which race" in body and b"Rebuild this review" not in body
    # a folder that isn't there is still not found
    code, _ = get("lib/reviews/nope/index.html", header=False, raw=True)
    assert code == 404
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "rebuild|page[0-9]" in js and "function rebuildReview(" in js and "R.saved" in js


# ================================================================ one review agrees with itself
def test_first_lap_words_follow_the_sign():
    d = T28.base()
    ins = insights.build(d)
    goods = [g for g in ins.get("good") or [] if g.get("key") == "lap1"] if isinstance(ins, dict) else []
    for g in goods:
        txt = g.get("text") or ""
        assert "within 0.0 s" not in txt, txt
        assert "quicker" in txt or "slower" in txt or "as quick" in txt, txt
    src = (ROOT / "debrief" / "insights.py").read_text(encoding="utf-8")
    assert "within {max(d, 0):.1f} s" not in src


def test_same_place_after_moving_is_said():
    rows = [{"lap": 1, "pos": 5}, {"lap": 2, "pos": 7}, {"lap": 3, "pos": 5}]
    v = race_analysis._verdict({"grid": 5, "finish": 5}, rows, {}, {})
    assert v.startswith("P5 to P5 overall on the road, after running P5 to P7"), v
    v = race_analysis._verdict({"grid": 5, "finish": 5}, [{"lap": 1, "pos": 5}], {}, {})
    assert v == "P5 to P5 overall on the road.", v
    tpl = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    assert "'Held position'" not in tpl and "Finished where you started, after running P" in tpl
    assert "A corner here runs from its braking zone to its exit" in tpl


def test_corners_read_turn_everywhere():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "'(C'+n+')'" not in js and "TURN_NOTE" in js
    assert "C(\\d{1,2})(?!\\w|\\.\\w)" in S.FRAME_UNITS and "'Turn '+n" in S.FRAME_UNITS


if __name__ == "__main__":
    fails = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok", k)
            except (Exception, SystemExit):  # noqa: BLE001
                import traceback
                fails += 1
                print("FAIL", k)
                traceback.print_exc()
    if "srv" in ENV:
        try:
            ENV["srv"].shutdown()
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    print("FAILED" if fails else "ALL OK", fails)
    sys.exit(1 if fails else 0)
