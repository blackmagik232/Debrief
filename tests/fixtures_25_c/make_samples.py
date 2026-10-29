"""Builds the 25-C page fixtures (SPEC §3.1 shapes) until 25-A's own samples are used instead.

    python3 tests/fixtures_25_c/make_samples.py

sample_team_24.json     a 2.4 team.json: combos (board + kings), h2h; no `weekly`, no `learn`
sample_team_25.json     the same plus `weekly` (leaderboards.view: one single-class series, one multi-class
                        series, season head-to-heads) and `learn` per combo (team.learn_from rows)
sample_learn_from.json  review.json `learn_from` for tests/fixtures_24_c/sample_review_24.json (review corner
                        ids + team_corner, as 25-A writes them)
(the review with learn_from is built at test time: tests/fixtures_24_c/sample_review_24.json + this list)
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def dump(name, obj):
    (HERE / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


TRACK = "Charlotte Motor Speedway (Roval 2019)"
CAR = "Ford Mustang GT4"


def kings():
    apex = [485.0, 1225.0, 2010.0, 2735.0, 3700.0, 3950.0, 5470.0]
    cuts = [0.0, 800.0, 1560.0, 2330.0, 3200.0, 3825.0, 5050.0, 5793.6]
    base = [11.2, 12.9, 13.4, 11.9, 9.6, 21.7, 16.3]
    off = {"me": [0.10, 0.00, 0.21, 0.12, 0.00, 0.30, 0.05], "jordan-x": [0.00, 0.06, 0.00, 0.03, 0.07, 0.00, 0.00],
           "sam-y": [0.04, 0.09, 0.05, 0.00, 0.10, 0.11, 0.08]}
    out = []
    for j, cid in enumerate(["C1", "C2", "C3", "C4", "C5", "C6", "C7"]):
        times = {k: round(base[j] + v[j], 3) for k, v in off.items()}
        order = sorted(times, key=times.get)
        out.append({"id": cid, "apex_m": apex[j], "from_m": cuts[j], "to_m": cuts[j + 1], "king": order[0],
                    "king_t": times[order[0]], "second": order[1], "margin": round(times[order[1]] - times[order[0]], 3),
                    "times": times, "your_gap": round(times["me"] - times[order[0]], 3)})
    wins = {}
    for r in out:
        wins[r["king"]] = wins.get(r["king"], 0) + 1
    return {"L": 5793.6, "fastest": "jordan-x", "drivers": ["me", "jordan-x", "sam-y"],
            "names": {"me": "Test Driver", "jordan-x": "Jordan X", "sam-y": "Sam Y"}, "corners": out, "wins": wins}


def team_24():
    roval = {"track": TRACK, "track_short": "Roval", "car": CAR, "track_id": 468, "car_id": 190, "races": 3,
             "last_date": "2026-10-27", "note": None, "kings": kings(), "my_tel_lap": {"t": 97.018, "date": "2026-10-27"},
             "board": [
                 {"key": "jordan-x", "name": "Jordan X", "t": 96.512, "is_me": False, "date": "2026-10-20", "irating": 2210, "pos": 1, "gap": 0.0},
                 {"key": "sam-y", "name": "Sam Y", "t": 96.845, "is_me": False, "date": "2026-10-22", "irating": 1880, "pos": 2, "gap": 0.333},
                 {"key": "me", "name": "Test Driver", "t": 97.018, "is_me": True, "date": "2026-10-27", "source": "your reviewed races", "irating": 1910, "pos": 3, "gap": 0.506}]}
    okayama = {"track": "Okayama International Circuit (Full Course)", "track_short": "Okayama", "car": "Global Mazda MX-5 Cup",
               "track_id": 51, "car_id": 67, "races": 1, "last_date": "2026-10-12", "note": None, "kings": None,
               "kings_note": "Not enough telemetry from these laps to compare corners.",
               "board": [
                   {"key": "me", "name": "Test Driver", "t": 98.903, "is_me": True, "date": "2026-10-12", "source": "Garage 61", "irating": 1910, "pos": 1, "gap": 0.0},
                   {"key": "jordan-x", "name": "Jordan X", "t": 99.001, "is_me": False, "date": "2026-10-11", "irating": 2210, "pos": 2, "gap": 0.098}]}
    h2h = [{"key": "jordan-x", "name": "Jordan X", "ahead": 1, "behind": 1, "corners_you": 2, "corners_them": 5, "shared": 2,
            "avg_gap": 0.204, "combos": [
                {"track": TRACK, "track_short": "Roval", "car": CAR, "you": 97.018, "them": 96.512, "gap": 0.506, "corners": {"you": 2, "them": 5}},
                {"track": okayama["track"], "track_short": "Okayama", "car": okayama["car"], "you": 98.903, "them": 99.001, "gap": -0.098, "corners": None}]},
           {"key": "sam-y", "name": "Sam Y", "ahead": 0, "behind": 1, "corners_you": 3, "corners_them": 4, "shared": 1,
            "avg_gap": 0.173, "combos": [
                {"track": TRACK, "track_short": "Roval", "car": CAR, "you": 97.018, "them": 96.845, "gap": 0.173, "corners": {"you": 3, "them": 4}}]}]
    return {"version": 1, "team": {"slug": "midfield-menace", "name": "Midfield Menace"},
            "me": {"name": "Test Driver", "key": "me"}, "generated_at": "2026-10-28T19:00:00+00:00",
            "combos": [roval, okayama], "h2h": h2h, "requests": 9,
            "notes": ["Showing your 6 most recent track and car combinations (1 older one left out to keep Garage 61 traffic light)."]}


def learn_rows():
    """team.learn_from rows (SPEC §3.1 shape). Corner ids are the kings' ids; apex_m is the kings' apex."""
    return [
        {"corner": "C4", "apex_m": 2010.0, "teammate": {"key": "jordan-x", "name": "Jordan X"},
         "gap_s": 0.21, "your_t": 13.61, "their_t": 13.40, "basis": "best laps", "similar_pace": True, "lap_gap_pct": 0.52,
         "how": {"brake_before_apex_m": [92, 81], "min_mph": [61, 64], "full_thr_after_apex_m": [140, 118], "gear": [3, 3]},
         "text": "Jordan's best lap was 0.21 s quicker than yours through C4. On that lap they braked about 10 m later and carried 3 mph more through the slowest point. Both are best laps, so this compares your best driving, not your typical lap.",
         "alt": {"key": "sam-y", "name": "Sam Y", "gap_s": 0.16}},
        {"corner": "C2", "apex_m": 1225.0, "teammate": {"key": "sam-y", "name": "Sam Y"},
         "gap_s": 0.09, "your_t": 13.0, "their_t": 12.91, "basis": "best laps", "similar_pace": True, "lap_gap_pct": 0.18,
         "how": {"gear": [3, 2]},
         "text": "Sam's best lap was 0.09 s quicker than yours through C2. On that lap they used 2nd gear where you used 3rd. Both are best laps, so this compares your best driving, not your typical lap.",
         "alt": None},
        {"corner": "C5", "apex_m": 2735.0, "teammate": {"key": "jordan-x", "name": "Jordan X"},
         "gap_s": 0.09, "your_t": 12.02, "their_t": 11.93, "basis": "best laps", "similar_pace": False, "lap_gap_pct": 1.9,
         "how": {"full_thr_after_apex_m": [95, 70]},
         "text": "Jordan's best lap was 0.09 s quicker than yours through C5. On that lap they were at full throttle about 25 m sooner after the apex. Both are best laps, so this compares your best driving, not your typical lap.",
         "alt": None}]


def review_rows():
    """review.json learn_from (as 25-A writes it): `corner` is the REVIEW's corner id at the review's apex,
    `team_corner` the kings' id. The review's C3 (2010 m) is the kings' C4, its C4 (2735 m) the kings' C5."""
    ids = {"C4": "C3", "C5": "C4", "C2": "C2"}
    out = []
    for r in learn_rows():
        r = dict(r, team_corner=r["corner"], corner=ids[r["corner"]])
        r["text"] = r["text"].replace("through " + r["team_corner"] + ".", "through " + r["corner"] + ".")
        out.append(r)
    return out


def board(rows, multiclass=False):
    out = []
    by_cls = {}
    for r in sorted(rows, key=lambda r: r[2]):
        by_cls.setdefault(r[4], []).append(r)
    for pos, (key, name, t, car, cls, date, ir) in enumerate(sorted(rows, key=lambda r: r[2]), 1):
        grp = by_cls[cls] if multiclass else sorted(rows, key=lambda r: r[2])
        lead = grp[0][2]
        out.append({"key": key, "name": name, "t": t, "car": car, "class": cls, "date": date, "irating": ir, "pos": pos,
                    "class_pos": [x[0] for x in grp].index(key) + 1, "gap": round(t - lead, 3),
                    "gap_pct": round((t / lead - 1) * 100, 3), "is_me": key == "me"})
    return out


def weekly():
    mx5 = [("jordan-x", "Jordan X", 98.412, "Global Mazda MX-5 Cup", "MX-5", "2026-10-29", 2210),
           ("me", "Test Driver", 98.903, "Global Mazda MX-5 Cup", "MX-5", "2026-10-29", 1910),
           ("sam-y", "Sam Y", 99.310, "Global Mazda MX-5 Cup", "MX-5", "2026-10-30", 1880)]
    mx5_w6 = [("me", "Test Driver", 103.200, "Global Mazda MX-5 Cup", "MX-5", "2026-10-22", 1905),
              ("jordan-x", "Jordan X", 103.350, "Global Mazda MX-5 Cup", "MX-5", "2026-10-23", 2200)]
    imsa = [("dom-c", "Dom C", 104.100, "Mercedes-AMG GT3 2020", "GT3", "2026-10-29", 2600),
            ("jordan-x", "Jordan X", 104.650, "BMW M4 GT3", "GT3", "2026-10-29", 2210),
            ("sam-y", "Sam Y", 109.020, "Porsche 718 Cayman GT4 Clubsport MR", "GT4", "2026-10-30", 1880),
            ("me", "Test Driver", 109.400, "Ford Mustang GT4", "GT4", "2026-10-30", 1910)]
    return {
        "season": "2026 Season 4",
        "series": [
            {"id": "global-mazda-mx-5-fanatec-cup", "name": "Global Mazda MX-5 Fanatec Cup", "multiclass": False, "weeks": [
                {"week": 6, "track": "Lime Rock Park - Full", "start": "2026-10-21", "end": "2026-10-28", "final": True,
                 "multiclass": False, "fetched_at": "2026-10-30T19:00:00Z", "board": board(mx5_w6), "note": None},
                {"week": 7, "track": "Okayama International Circuit - Full", "start": "2026-10-28", "end": "2026-11-04",
                 "final": False, "multiclass": False, "fetched_at": "2026-10-30T19:00:00Z", "board": board(mx5), "note": None}]},
            {"id": "imsa-sportscar-championship", "name": "IMSA Sportscar Championship", "multiclass": True, "weeks": [
                {"week": 7, "track": "Road Atlanta - Full Course", "start": "2026-10-28", "end": "2026-11-04", "final": False,
                 "multiclass": True, "fetched_at": "2026-10-30T19:00:00Z", "board": board(imsa, True), "note": None},
                {"week": 5, "track": "Sebring International Raceway - International", "start": "2026-10-14", "end": "2026-10-21",
                 "final": True, "multiclass": True, "board": [], "note": "No team laps this week."}]}],
        "h2h": [
            {"key": "jordan-x", "name": "Jordan X", "shared_weeks": 5, "ahead": 2, "behind": 3, "median_gap_pct": 0.42,
             "verdict": "slightly quicker than you", "series": [
                 {"id": "global-mazda-mx-5-fanatec-cup", "shared_weeks": 5, "ahead": 2, "behind": 3, "median_gap_pct": 0.42,
                  "weeks": [{"week": 3, "you": 98.9, "them": 98.5, "gap_pct": 0.41}, {"week": 4, "you": 101.2, "them": 101.5, "gap_pct": -0.3},
                            {"week": 5, "you": 99.8, "them": 99.2, "gap_pct": 0.6}, {"week": 6, "you": 103.2, "them": 103.35, "gap_pct": -0.15},
                            {"week": 7, "you": 98.903, "them": 98.412, "gap_pct": 0.5}]}]},
            {"key": "sam-y", "name": "Sam Y", "shared_weeks": 1, "ahead": 1, "behind": 0, "median_gap_pct": -0.41,
             "verdict": None, "series": [
                 {"id": "global-mazda-mx-5-fanatec-cup", "shared_weeks": 1, "ahead": 1, "behind": 0, "median_gap_pct": -0.41,
                  "weeks": [{"week": 7, "you": 98.903, "them": 99.31, "gap_pct": -0.41}]}]}]}


def team_25():
    d = team_24()
    d["combos"][0]["learn"] = learn_rows()
    d["combos"][1]["learn"] = []
    d["weekly"] = weekly()
    return d


def main():
    dump("sample_team_24.json", team_24())
    dump("sample_team_25.json", team_25())
    dump("sample_learn_from.json", review_rows())


if __name__ == "__main__":
    main()
