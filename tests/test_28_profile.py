"""Debrief 2.8: racecraft profile, trends over time, pre-race brief, print styles, weekly Discord digest draft.

Run:  python3 tests/ci_run.py tests/test_28_profile.py      (plain script: no pytest needed, Windows-safe)
Synthetic data only, no network, temp folders from tempfile, utf-8 everywhere.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import discord, library, prep, profile  # noqa: E402

TPL = ROOT / "debrief" / "templates"


def rc(sub, grid=10, lap1=8, passes=(), fights=(), incidents=(), costs=(), positions=None, corners=(), version=2, stops=()):
    on = [p for p in passes if p.get("how", "on track") == "on track" and p.get("lap", 2) > 1]
    made = [p for p in on if p.get("made")]
    return {"version": version, "subsession": str(sub), "grid": grid, "finish": lap1, "official": False, "pos_after_lap1": lap1,
            "positions": positions if positions is not None else [{"lap": 1, "pos": lap1}, {"lap": 2, "pos": lap1}, {"lap": 3, "pos": lap1}],
            "passes": list(passes), "fights": list(fights), "incidents": list(incidents), "incident_cost": list(costs),
            "pass_corners": list(corners), "pit_cost": {"stops": list(stops)},
            "craft": {"made": len(made), "lost": len(on) - len(made), "stuck": sum(1 for p in made if p.get("stuck")),
                      "fights": len(fights), "incidents": len(incidents)}}


def review(day, sub=None, track="Circuit Zolder (Grand Prix)", car="McLaren 570S GT4", best=92.0, racecap=None, corners=None, session="Race", result=None):
    laps = [{"n": i + 1, "t": best + 0.5 + i * 0.01, "clean": True} for i in range(12)]
    d = {"meta": {"track": track, "car": car, "started_at": f"{day}T19:00:00+00:00", "session": session, "driver": "Test Driver",
                  "event_id": f"e{sub or day}"},
         "L": 4000.0, "laps": laps, "best": {"t": best, "n": 1}, "median": best + 0.5, "std_clean": 0.2, "counts": {"analysed": 12},
         "corners": corners if corners is not None else [{"id": "T1", "apex_m": 500.0, "avail": 0.2, "rank": 1},
                                                         {"id": "T2", "apex_m": 1500.0, "avail": 0.1, "rank": 2}]}
    if racecap:
        d["racecap"] = racecap
    if result:
        d["result"] = result
    return d


class Lib:
    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dbf28"))
        return self

    def __exit__(self, *a):
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, name, d):
        p = self.dir / "reviews" / name
        p.mkdir(parents=True)
        (p / "review.json").write_text(json.dumps(d), encoding="utf-8")

    def rows(self):
        return library.list_reviews(self.dir)


def pass_(lap, made, other="Rival One", stuck=True, how="on track"):
    return {"t": lap * 100.0, "lap": lap, "made": made, "other": other, "stuck": stuck, "how": how}


def three(lib):
    corners = [{"corner": "T1", "made": 2, "lost": 1}]
    lib.add("a", review("2026-10-01", 1, racecap=rc(1, grid=10, lap1=8, passes=[pass_(2, True), pass_(3, True, stuck=False), pass_(4, False)], corners=corners)))
    lib.add("b", review("2026-10-08", 2, racecap=rc(2, grid=10, lap1=12, passes=[pass_(2, True)], corners=corners)))
    lib.add("c", review("2026-10-15", 3, track="Spa", racecap=rc(3, grid=5, lap1=5, passes=[pass_(1, True), pass_(2, False, how="pit")], corners=corners)))


def test_scan_more_message_below_three():
    with Lib() as lib:
        lib.add("a", review("2026-10-01", 1, racecap=rc(1)))
        lib.add("b", review("2026-10-08", 2, racecap=rc(2)))
        lib.add("old", review("2026-10-02", 9, racecap={"version": 1, "subsession": "9"}))
        lib.add("none", review("2026-10-03"))
        c = profile.build(lib.rows())["racecraft"]
        assert c["n"] == 2 and c["enough"] is False and c["waiting"] == 1 and "passes" not in c
    assert "Scan more races" in (TPL / "library.html").read_text(encoding="utf-8")


def test_racecraft_numbers():
    with Lib() as lib:
        three(lib)
        c = profile.build(lib.rows())["racecraft"]
    assert c["enough"] and c["n"] == 3
    assert c["lap1"]["avg"] == round((2 - 2 + 0) / 3, 1) and (c["lap1"]["gained"], c["lap1"]["lost"], c["lap1"]["same"]) == (1, 1, 1)
    # lap 1 and non-track passes don't count; a pass swapped back didn't stick
    assert c["passes"] == {"made": 3, "lost": 1, "stuck": 2, "stick_pct": 67}
    assert "replay camera" in c["note"] and "on the road" in c["note"]


def test_same_corner_name_at_two_tracks_stays_apart():
    with Lib() as lib:
        three(lib)
        cs = profile.build(lib.rows())["racecraft"]["corners"]
    assert {(c["track"], c["made"]) for c in cs} == {("Zolder", 4), ("Spa", 2)} or len(cs) == 2
    assert sorted(c["made"] for c in cs) == [2, 4]


def test_corner_names_shifting_at_one_track_are_matched_by_apex():
    with Lib() as lib:
        pc = lambda name: [{"corner": name, "made": 1, "lost": 0}]  # noqa: E731
        lib.add("a", review("2026-10-01", 1, racecap=rc(1, corners=pc("T2")), corners=[{"id": "T2", "apex_m": 1500.0, "avail": 0.1, "rank": 1}]))
        lib.add("b", review("2026-10-08", 2, racecap=rc(2, corners=pc("T3")), corners=[{"id": "T3", "apex_m": 1510.0, "avail": 0.1, "rank": 1}]))
        lib.add("c", review("2026-10-15", 3, racecap=rc(3)))
        cs = profile.build(lib.rows())["racecraft"]["corners"]
    assert len(cs) == 1 and cs[0]["made"] == 2 and cs[0]["corner"] == "T3"     # the newest name


def test_one_race_reviewed_twice_counts_once():
    with Lib() as lib:
        three(lib)
        lib.add("a2", review("2026-10-02", 1, racecap=rc(1, grid=10, lap1=8, passes=[pass_(2, True)])))
        c = profile.build(lib.rows())["racecraft"]
    assert c["n"] == 3 and c["passes"]["made"] == 2


def test_incident_recovery():
    pos = [{"lap": n, "pos": p} for n, p in [(1, 10), (2, 15), (3, 12), (4, 10), (5, 10)]]
    r = profile._recovery(rc(1, grid=8, positions=pos, incidents=[
        {"lap": 1, "places_lost": 2},               # from the grid slot (8): never back at 8
        {"lap": 2, "places_lost": 5},               # from P10: back by lap 4, two laps later
        {"lap": 3, "places_lost": -1},              # gained places: nothing to recover
        {"lap": 4, "places_lost": 3, "out": True},  # race over
        {"lap": 5, "places_lost": 2},               # no laps left: can't be judged
        {"lap": 2, "places_lost": None},
    ], costs=[{"seconds": 9.0}]))
    assert [x["status"] for x in r] == ["not_back", "back", "no_loss", "ended", "unknown", "unknown"]
    assert r[1]["laps_back"] == 2 and r[0]["seconds"] == 9.0 and r[1]["seconds"] is None


def test_odd_scan_never_loses_the_review():
    with Lib() as lib:
        lib.add("bad", review("2026-10-01", 1, racecap={"version": 2, "craft": "nope", "passes": 7}))
        lib.add("worse", review("2026-10-02", 2, racecap={"version": 2, "craft": {}, "passes": [3], "fights": ["x"], "incidents": [None], "positions": "x" and [], "pass_corners": [None], "pit_cost": 4, "grid": True}))
        rows = lib.rows()
        assert len(rows) == 2
        assert profile.build(rows)["racecraft"]["n"] == 1


def test_practice_is_left_out():
    with Lib() as lib:
        lib.add("p", review("2026-10-01", 1, session="Open Practice", racecap=rc(1)))
        assert profile.build(lib.rows())["racecraft"]["n"] == 0


def test_over_time_directions_use_the_same_rule():
    rows = []
    for i in range(6):
        rows.append({"dir": f"d{i}", "date": (date(2026, 9, 1) + timedelta(days=7 * i)).isoformat(), "started_at": "", "session": "Race", "laps": 10, "clean": 4 + i,
                     "result": {"start_pos": 10, "finish_pos": 10, "incidents": 12 - 2 * i}})
    m = {x["key"]: x for x in profile.over_time(rows)}
    assert m["clean"]["trend"]["direction"] == "improving" and m["clean"]["latest"] == 90.0
    assert m["incidents"]["trend"]["direction"] == "improving" and m["incidents"]["lower_better"]
    assert m["places"]["trend"]["direction"] == "steady"
    worse = [dict(r, clean=9 - i) for i, r in enumerate(rows)]
    assert {x["key"]: x for x in profile.over_time(worse)}["clean"]["trend"]["direction"] == "slipping"
    # too few races for a word, and one race for no chart at all
    assert {x["key"]: x for x in profile.over_time(rows[:3])}["clean"]["trend"] is None
    assert profile.over_time(rows[:1]) == []
    assert "lap1" not in m


class NoRequests:
    requests_made = 0

    def __getattr__(self, name):
        raise AssertionError("the brief must make no request: " + name)


def test_brief_saved_data_only_and_hides_what_is_missing():
    with Lib() as lib:
        fights = [{"other": "Rival One", "seconds": 30.0}, {"other": "Passing Glance", "seconds": 3.0}]
        lib.add("a", review("2026-10-01", 1, best=92.0, racecap=rc(1, fights=fights, passes=[pass_(2, True)])))
        lib.add("b", review("2026-10-08", 2, best=91.5, racecap=rc(2, fights=fights[:1], passes=[pass_(3, False)])))
        lib.add("spa", review("2026-10-09", 3, track="Spa", racecap=rc(3, fights=[{"other": "Spa Only", "seconds": 90.0}])))
        rows = library.race_rows(lib.rows())
        mine = [r for r in rows if r["track"].startswith("Circuit")]
        two = prep.brief(rows, "Circuit Zolder (Grand Prix)", "McLaren 570S GT4", prep._history(mine), prep.weak_corners(mine))
        assert two["rivals"] == []      # two scans at this track: too few to call anyone a rival
        lib.add("c", review("2026-10-10", 4, best=91.7, racecap=rc(4, fights=fights[:1])))
        rows = library.race_rows(lib.rows())
        mine = [r for r in rows if r["track"].startswith("Circuit")]
        b = prep.brief(rows, "Circuit Zolder (Grand Prix)", "McLaren 570S GT4", prep._history(mine), prep.weak_corners(mine))
    assert b["pace"]["best"] == 91.5 and b["pace"]["races"] == 3 and b["pace"]["other_car"] is None
    assert [c["id"] for c in b["corners"]] == ["T1", "T2"] and b["corners"][0]["top3"] == 3
    assert [r["name"] for r in b["rivals"]] == ["Rival One"]     # the other track's rival and a 3 s brush aren't in
    assert b["rivals"][0]["races"] == 3 and b["rivals"][0]["seconds"] == 90.0 and b["rivals"][0]["made"] == 1 and b["rivals"][0]["lost"] == 1
    assert b["note"]


def test_brief_one_race_has_no_repeat_corners_and_no_scan_no_rivals():
    with Lib() as lib:
        lib.add("a", review("2026-10-01", 1))
        rows = library.race_rows(lib.rows())
        b = prep.brief(rows, "Circuit Zolder (Grand Prix)", "McLaren 570S GT4", prep._history(rows), prep.weak_corners(rows))
    assert b["pace"] and b["corners"] == [] and b["rivals"] == [] and b["note"] is None
    assert prep.brief([], "Nowhere", "Car", None, []) is None


def test_brief_other_car_is_labelled_and_build_makes_no_extra_request():
    with Lib() as lib:
        lib.add("a", review("2026-10-01", 1, car="BMW M4 GT4", best=95.0))
        rows = library.race_rows(lib.rows())
        b = prep.brief(rows, "Circuit Zolder (Grand Prix)", "McLaren 570S GT4", None, [])
        assert b["pace"]["other_car"] == "BMW M4 GT4" and b["pace"]["best"] == 95.0
        d = prep.build(None, lib.dir, None, None, None, log=lambda m: None, track="Circuit Zolder (Grand Prix)", car="McLaren 570S GT4")
        assert d["brief"]["pace"]["other_car"] == "BMW M4 GT4" and d["requests"] == 0
        html = prep.write(lib.dir, d).read_text(encoding="utf-8")
        assert "BMW M4 GT4" in html


def test_print_styles_only_before_head_end():
    for n in ("review.html", "library.html", "prep.html"):
        s = (TPL / n).read_text(encoding="utf-8")
        at = s.index("</head>")
        block = s[s.rindex("<style", 0, at):at]
        assert "@media print" in block and block.rstrip().endswith("</style>"), n
        assert "window.print" not in block


def digest_rows():
    today = date(2026, 11, 2)
    with Lib() as lib:
        lib.add("in1", review("2026-11-02", 1, best=91.5, racecap=rc(1, passes=[pass_(2, True), pass_(3, False)]), result={"finish_pos": 4, "start_pos": 9}))
        lib.add("in2", review("2026-10-27", 2, best=92.0))
        lib.add("old", review("2026-10-26", 3, best=93.0))
        lib.add("prac", review("2026-11-01", 4, session="Open Practice", best=90.0))
        d = review("2026-11-01", 5)
        d["meta"]["test_data"] = True
        lib.add("test", d)
        return lib.rows(), today


def test_digest_text():
    rows, today = digest_rows()
    d = discord.digest(rows, today=today)
    assert d["races"] == 2 and d["from"] == "2026-10-27" and d["to"] == "2026-11-02"
    t = d["text"]
    assert t.startswith("Test Driver: 2 races from Oct 27 to Nov 2")
    assert "best 1:31.500" in t and "P4 from P9" in t and "new PB" in t
    assert "1 on-track passes made, 1 suffered" in t and "replay camera" in t
    assert "1:30.000" not in t and "1:33.000" not in t
    assert "—" not in t


def test_digest_empty_long_and_mentions():
    rows, today = digest_rows()
    assert discord.digest(rows, today=date(2027, 1, 1)) == {"races": 0, "from": "2026-12-26", "to": "2027-01-01", "text": "", "lines": [], "pbs": 0}
    assert discord.digest([], today=today)["text"] == ""
    base = rows[0]
    many = [dict(base, track_short="T" * 300, dir=str(i)) for i in range(40)]
    d = discord.digest(many, today=today)
    assert len(d["text"]) <= discord.MAX_CONTENT and any(x.startswith("...and") for x in d["text"].splitlines())
    assert "camera)." in d["text"].splitlines()[-1]      # fewer race lines, never a sentence cut off
    assert "...and 28 more" in discord.digest([dict(base, dir=str(i)) for i in range(40)], today=today)["text"]
    bad = discord.digest([dict(base, track_short="@everyone track", driver="@here")], today=today)["text"]
    assert "@everyone" not in bad and "@here" not in bad


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError("the digest must never send anything")


def test_digest_never_posts():
    import requests as real
    keep = (real.post, real.get, discord.post)
    boom = NoNetwork()
    real.post = real.get = discord.post = lambda *a, **k: boom.x
    try:
        rows, today = digest_rows()
        assert discord.digest(rows, today=today)["text"]
        with Lib() as lib:
            lib.add("a", review(date.today().isoformat(), 1))
            page = library.rebuild(lib.dir).read_text(encoding="utf-8")
        assert '"digest"' in page
    finally:
        real.post, real.get, discord.post = keep
    s = (TPL / "library.html").read_text(encoding="utf-8")
    assert "discord-post" not in s and "fetch(" not in s and "debrief:share-card" in s and "window.parent===window" in s


def main():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for k, v in tests:
        try:
            v()
            print("  ok ", k)
        except Exception:
            bad += 1
            print("FAIL ", k)
            traceback.print_exc()
    print(f"{len(tests) - bad} of {len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
