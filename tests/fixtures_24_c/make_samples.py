"""Builds the 24-C page fixtures from the 23-C samples plus the 2.4 keys shaped exactly as SPEC §2.1.

    python3 tests/fixtures_24_c/make_samples.py

sample_review_24.json        new race review: racecraft + strategy + result with the new fields
sample_review_old_result.json 2.2.1 review (no racecraft, no strategy) + a result with incident_laps
sample_review_strategy_only.json  old review with strategy but no stint (fuel panel on its own)
sample_prep_strategy.json    prep page data carrying the strategy prefill (24-D passes it in)
sample_prep_nofuel.json      prefill with no fuel data (burn None)
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "fixtures_23_c"


def load(name):
    return json.loads((SRC / name).read_text(encoding="utf-8"))


def dump(name, obj):
    (HERE / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


RESULT = {"finish_pos": 7, "start_pos": 11, "field_size": 24, "incidents": 6, "sof": 1840,
          "pos_after_lap1": 9, "pit_stops": 1, "penalties": 1, "finish_status": "running",
          "positions_lap1": 2,
          "incident_laps": [{"lap": 6, "kind": "off", "points": 1}, {"lap": 13, "kind": "contact", "points": 4},
                            {"lap": 13, "kind": "spin", "points": 2}]}

STRATEGY = {"version": 1,
            "burn": {"median_l": 2.41, "p90_l": 2.52, "n": 17, "source": "fuel_used",
                     "laps": {str(n): round(2.38 + 0.01 * (n % 5), 2) for n in range(2, 19)}},
            "level": {"start_l": 45.2, "end_l": 4.1, "max_seen_l": 47.0},
            "pit": {"laps": [{"n": 14, "t": 112.3, "kind": "in"}, {"n": 15, "t": 109.8, "kind": "out"}],
                    "loss_s": 50.1, "n": 1},
            "note": None}


def racecraft(d):
    med = d["median"]
    tl = [{"n": 1, "t": d["laps"][0]["t"], "lost_s": round(d["laps"][0]["t"] - med, 2), "tags": ["lap1"]},
          {"n": 4, "t": d["laps"][3]["t"], "lost_s": round(d["laps"][3]["t"] - med, 2), "tags": ["slow"]},
          {"n": 5, "t": d["laps"][4]["t"], "lost_s": round(d["laps"][4]["t"] - med, 2), "tags": ["best"]},
          {"n": 6, "t": d["laps"][5]["t"], "lost_s": round(d["laps"][5]["t"] - med, 2), "tags": ["unclean"]},
          {"n": 9, "t": d["laps"][8]["t"], "lost_s": 0.7, "tags": ["traffic"], "at_m": 2310.0},
          {"n": 13, "t": d["laps"][12]["t"], "lost_s": round(d["laps"][12]["t"] - med, 2), "tags": ["unclean"]},
          {"n": 14, "t": 112.3, "lost_s": None, "tags": ["pit"]},
          {"n": 15, "t": 109.8, "lost_s": None, "tags": ["pit"]}]
    return {"version": 1,
            "lap1": {"n": 1, "t": d["laps"][0]["t"], "vs_median_s": round(d["laps"][0]["t"] - med + 5.3, 2),
                     "in_analysis": True, "segments": [{"id": "C1", "lost_s": 2.1}, {"id": "C2", "lost_s": 1.4}],
                     "note": "includes the start"},
            "timeline": tl,
            "summary": ["Lap 1 was 5.9 s slower than your typical lap, most of it in C1 and C2 (that includes the start).",
                        "Traffic cost about 0.7 s on lap 9 (most likely traffic)."]}


def main():
    new = load("sample_review_new.json")
    new["racecraft"] = racecraft(new)
    new["strategy"] = STRATEGY
    new["result"] = dict(RESULT)
    dump("sample_review_24.json", new)

    old = load("sample_review_old.json")
    old["result"] = dict(RESULT)
    old["counts"]["traffic"] = [9]
    old["counts"]["traffic_detail"] = [{"n": 9, "lost": 0.7, "at_m": 2310.0}]
    dump("sample_review_old_result.json", old)

    so = load("sample_review_old.json")
    so.pop("stint", None)
    so["strategy"] = {"version": 1, "burn": {"median_l": 3.1, "p90_l": 3.3, "n": 9, "source": "fuel_level", "laps": {}},
                      "level": {"start_l": 60.0, "end_l": None, "max_seen_l": 60.0}, "pit": None, "note": None}
    dump("sample_review_strategy_only.json", so)

    prep = {"version": 1, "generated_at": "2026-11-20T18:00:00+00:00", "track": "Charlotte Motor Speedway (Roval 2019)",
            "car": "Ford Mustang GT4", "track_short": "Roval", "raced": False, "history": None, "weak": [],
            "technique": [], "teammates": {"laps": []}, "goal": None, "plan": [], "requests": 0,
            "strategy": {"race": {"laps": 40, "minutes": None, "source": "schedule", "text": "40 laps"},
                         "lap_t": {"value": 85.9, "source": "median clean lap, your last 3 races here", "n": 3},
                         "burn": {"value": 2.41, "p90": 2.52, "source": "your race on Oct 1", "n": 17},
                         "tank": {"value": 47.0, "source": "estimated: the most fuel seen in this car", "estimated": True},
                         "pit_loss": {"value": 50.1, "source": "1 pit stop at this track", "n": 1},
                         "reserve_laps": 1.0}}
    dump("sample_prep_strategy.json", prep)
    p2 = json.loads(json.dumps(prep))
    p2["strategy"] = {"race": {"laps": None, "minutes": 40, "source": "schedule", "text": "40 mins"},
                      "lap_t": None, "burn": None, "tank": None, "pit_loss": None, "reserve_laps": 1.0}
    dump("sample_prep_nofuel.json", p2)


if __name__ == "__main__":
    main()
