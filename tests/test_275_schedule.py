"""2.7.5+: the schedule that ships with Debrief, read from iRacing's real 2026 Season 4 PDF. Run: python tests/test_275_schedule.py"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import prepare, schedule as s  # noqa: E402


def sched():
    return next(x for x in s._bundled() if x["season"] == "2026 Season 4")


def test_bundled_schedule_is_whole_and_passes_the_reader_checks():
    d = sched()
    assert len(d["series"]) == 154 and len(d["weeks"]) == 12 and s.check(d) == []
    assert all(x.get("license") for x in d["series"]) and all(w.get("length") for x in d["series"] for w in x["weeks"])


def test_known_series_and_car_names():
    d = sched()
    f = next(x for x in d["series"] if x["name"] == "GT4 Falken Tyre Challenge")
    assert f["weeks"][0]["track"] == "Silverstone Circuit - Grand Prix" and "Aston Martin Vantage GT4" in f["cars"] and "McLaren 570S GT4" in f["cars"]
    p = next(x for x in d["series"] if x["name"] == "iRacing Porsche Cup by CONSPIT")
    assert p["weeks"][-1]["track"] == "Circuit Zandvoort - Grand Prix"


def test_year_long_tours_map_to_the_season_week_by_date():
    d = sched()
    long = next(x for x in d["series"] if len(x["weeks"]) > 30)
    now = [w for w in long["weeks"] if w.get("season_week") == 7]
    assert len(now) == 1 and now[0]["week"] != 7 and now[0]["start"] == d["weeks"][6]["start"]


def test_bundled_is_used_with_nothing_saved_and_warmups_only_look_back():
    assert s.load_current(date(2026, 11, 2))["season"]
    a = {"id": "a", "name": "A", "weeks": [{"week": 3, "start": "2026-10-01", "track": "Spa"}]}
    b = {"id": "b", "name": "B", "weeks": [{"week": 1, "start": "2026-09-15", "track": "Spa"}, {"week": 9, "start": "2026-11-20", "track": "Spa"}]}
    assert prepare.warmups(a, a["weeks"][0], [a, b]) == [{"series": "B", "start": "2026-09-15"}]


if __name__ == "__main__":
    fails = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            try:
                fn()
                print("ok", k)
            except Exception:  # noqa: BLE001
                import traceback
                fails += 1
                traceback.print_exc()
    sys.exit(1 if fails else 0)
