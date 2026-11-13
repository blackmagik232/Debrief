"""Debrief 2.35 "vs AI": a race against AI cars gets a small tag and nothing more.

    python3 tests/ci_run.py tests/test_235_vsai.py

No network; a temp folder. Checks vsai.ai_count / is_vs_ai, the library row's tag with no field strength or iRating,
old reviews unchanged, the library page carrying the tag, and a race with no iRacing session number being reviewed
from this PC without ever asking Garage 61.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-235vsai-"))
(TMP / "lib").mkdir()
(TMP / "settings.toml").write_text(f"library_folder = '{(TMP / 'lib').as_posix()}'\n", encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(TMP / "settings.toml"), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"),
                  DEBRIEF_NO_NETWORK="1")

from debrief import app, library, localrace, vsai  # noqa: E402

FAILS = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILS.append(what)


def drivers(*flags):
    return {"DriverInfo": {"Drivers": [dict(CarIdx=i, **f) for i, f in enumerate(flags)]}}


def test_ai_count():
    si = drivers({}, {"CarIsAI": 1}, {"CarIsAI": 1}, {"CarIsPaceCar": 1, "CarIsAI": 1}, {"IsSpectator": 1, "CarIsAI": 1},
                 {"CarIsAI": 0})
    check(vsai.ai_count(si) == 2, "two AI cars counted; pace car and spectator left out")
    check(vsai.ai_count(drivers({}, {"CarIsPaceCar": 1})) == 0, "pace car only: no AI")
    check(vsai.ai_count({}) == 0 and vsai.ai_count(None) == 0 and vsai.ai_count({"DriverInfo": 5}) == 0, "bad input: 0, no error")
    check(vsai.ai_count({"DriverInfo": {"Drivers": [None, 3, "x"]}}) == 0, "junk drivers: 0")


def test_is_vs_ai():
    check(vsai.is_vs_ai({"meta": {"vs_ai": True, "ai_cars": 11}}), "review data with vs_ai")
    check(vsai.is_vs_ai({"ai_cars": 5}), "meta / summary with ai_cars")
    check(not vsai.is_vs_ai({"meta": {"driver": "x"}}) and not vsai.is_vs_ai({}), "old review: not vs AI")
    check(not vsai.is_vs_ai(None) and not vsai.is_vs_ai("x") and not vsai.is_vs_ai({"meta": {"ai_cars": "n"}}), "junk: False")
    check(not vsai.is_vs_ai({"meta": {"vs_ai": False, "ai_cars": 3}}), "vs_ai False wins")
    check(vsai.TAG == "vs AI", "the tag words")


def review(ai):
    meta = {"driver": "T", "event_id": "ir1", "car": "Car", "track": "Track", "started_at": "2026-10-01T23:30:00+00:00",
            "session": "Race", "source": "pc"}
    if ai is not None:
        meta.update(vs_ai=bool(ai), ai_cars=ai)
    return {"meta": meta, "laps": [{"n": i, "t": 90 + i * .1} for i in range(1, 6)], "best": {"t": 90.1, "n": 1},
            "median": 90.3, "irating": 2100,
            "result": {"finish_pos": 3, "field_size": 12, "start_pos": 5, "sof": 2300, "irating_before": 2100,
                       "irating_after": 2130, "irating_change": 30, "incidents": 2}}


def test_row():
    p = TMP / "lib" / "reviews" / "r" / "review.json"
    ai = library._row_from(review(11), p)
    check(ai["vs_ai"] is True, "AI race row has vs_ai")
    check(ai["irating"] is None, "no iRating on the row")
    check(ai["result"]["finish_pos"] == 3 and ai["result"]["field_size"] == 12, "finish and field size stay")
    check(not any(k in ai["result"] for k in ("sof", "irating_before", "irating_after", "irating_change")), "no SOF or iRating in the result")
    old = library._row_from(review(None), p)
    check(old["vs_ai"] is False and old["irating"] == 2100 and old["result"]["sof"] == 2300, "an old review is unchanged")
    check(ai["laps"] == 5 and ai["best"] == old["best"] and ai["clean"] == old["clean"], "telemetry counts like any race")
    html = (ROOT / "debrief" / "templates" / "library.html").read_text(encoding="utf-8")
    check("r.vs_ai" in html and "'vs AI'" in html, "the library page draws the tag")


class NoAskApi:
    def __getattr__(self, name):
        raise AssertionError("Garage 61 was asked: " + name)


def test_no_subsession():
    cfg = app.load_settings()
    s = {"kind": "session", "id": "x|y|f.ibt", "track": "T", "car": "C", "sort": 1.0, "subsession": None, "paths": ["f.ibt"]}
    old = localrace.newest
    localrace.newest = lambda *a, **k: s
    try:
        got = app._newest_local_unseen(cfg, NoAskApi(), lambda *a: None)
        check(got is s, "no session number: reviewed from the PC, Garage 61 never asked")
        s2 = dict(s, subsession=777)

        class Asked:
            asked = []

            def event_for_subsession(self, sub):
                self.asked.append(sub)
                return None
        a = Asked()
        localrace.newest = lambda *a_, **k: s2
        check(app._newest_local_unseen(cfg, a, lambda *x: None) is s2 and a.asked == [777], "with a session number: still asks once")
    finally:
        localrace.newest = old


if __name__ == "__main__":
    test_ai_count()
    test_is_vs_ai()
    test_row()
    test_no_subsession()
    print("all passed" if not FAILS else f"{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
