"""Debrief 2.4 workstream A: fuel and pit strategy (strategy.py) and race craft (racecraft.py).

    python3 tests/test_24_a.py

Offline: synthetic telemetry (tests/fixtures_a/synth.py), a fake sync store and a temp library.
"""
from __future__ import annotations

import faulthandler
import json
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FX = ROOT / "tests" / "fixtures_24_a"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FX))
_T = tempfile.mkdtemp(prefix="debrief-24a-")
for _k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR", "DEBRIEF_SCHEDULE_DIR", "DEBRIEF_UPDATE_STATE"):
    os.environ.setdefault(_k, os.path.join(_T, _k.lower()))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
warnings.simplefilter("ignore")

import samples  # noqa: E402
from debrief import analysis_plus, insights, racecraft, strategy  # noqa: E402

CAUSAL = re.compile(r"\b(will gain|costing you|because|you should|the reason|caused|makes you)\b", re.I)
_CACHE = {}


def review(**kw):
    key = json.dumps(kw, sort_keys=True)
    if key not in _CACHE:
        _CACHE[key] = samples.engine_review(**kw)
    return json.loads(json.dumps(_CACHE[key], default=float))


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError as e:
        msg = str(e)
        assert msg and msg[0].isupper() and msg.endswith(".") and "Traceback" not in msg, msg
        return msg
    raise AssertionError(f"no ValueError for {a}")


# ---------------------------------------------------------------- plan
def test_plan_15_laps_no_stop():
    p = strategy.plan({"laps": 15, "burn_l": 2.5, "tank_l": 47, "reserve_laps": 1})
    assert p["stops"] == 0 and p["fuel_needed_l"] == 40.0 and p["start_fuel_l"] == 40.0, p
    assert p["windows"] == [] and p["pit_time_s"] == 0.0 and p["laps_per_tank"] == 18 - 1, p
    assert p["stints"] == [{"from_lap": 1, "to_lap": 15, "laps": 15, "fuel_l": 40.0}]


def test_plan_40_laps_two_stops_valid_windows():
    p = strategy.plan({"laps": 40, "burn_l": 2.5, "tank_l": 47, "reserve_laps": 1, "pit_loss_s": 50})
    assert p["stops"] == 2, p
    assert len(p["windows"]) == 2 and all(w["close"] >= w["open"] >= 1 for w in p["windows"]), p["windows"]
    assert sum(s["laps"] for s in p["stints"]) == 40 and p["stints"][-1]["to_lap"] == 40
    assert all(s["fuel_l"] <= 47 for s in p["stints"]) and all(w["fuel_add_l"] <= 47 for w in p["windows"])
    # each stint has enough fuel for its laps
    lpt = p["laps_per_tank"]
    assert all(s["laps"] <= lpt for s in p["stints"])
    for w in p["windows"]:
        assert w["open"] <= w["at_lap"] <= w["close"], w
    assert p["pit_time_s"] == 100.0
    # pit loss unknown -> warning and no pit time
    p2 = strategy.plan({"laps": 40, "burn_l": 2.5, "tank_l": 47})
    assert p2["pit_time_s"] is None and any("Pit stop time unknown" in w for w in p2["warnings"])


def test_plan_timed_race():
    p = strategy.plan({"minutes": 40, "lap_t": 85.9, "burn_l": 2.5, "tank_l": 100})
    assert p["laps"] == 28, p
    assert any(w.startswith("Timed race") for w in p["warnings"])
    raises(strategy.plan, {"minutes": 40, "burn_l": 2.5, "tank_l": 100})     # lap time required


def test_plan_p90_and_warnings():
    p = strategy.plan({"laps": 10, "burn_l": 2.0, "p90": 2.4, "tank_l": 100, "burn_n": 3, "tank_estimated": True})
    assert p["burn_l"] == 2.4 and p["fuel_needed_l"] == 26.4, p
    assert any("only 3 laps" in w for w in p["warnings"]) and any("estimate" in w for w in p["warnings"])
    p = strategy.plan({"laps": 10, "burn_l": 2.0, "p90": 2.4, "tank_l": 100, "use_p90": False})
    assert p["burn_l"] == 2.0 and p["fuel_needed_l"] == 22.0


def test_plan_out_of_range_inputs():
    base = {"laps": 15, "burn_l": 2.5, "tank_l": 47}
    bad = [{"laps": 0}, {"laps": 2001}, {"laps": 2.5}, {"laps": "x"}, {"laps": None, "minutes": 0},
           {"laps": None, "minutes": 1441, "lap_t": 90}, {"laps": None, "minutes": 30, "lap_t": 5},
           {"laps": None, "minutes": 30, "lap_t": 1300}, {"burn_l": 0.01}, {"burn_l": 21}, {"burn_l": None},
           {"tank_l": 0.5}, {"tank_l": 201}, {"tank_l": None}, {"pit_loss_s": -1}, {"pit_loss_s": 301},
           {"reserve_laps": -0.5}, {"reserve_laps": 6}, {"use_p90": "yes"}, {"burn_l": float("nan")},
           {"laps": True}]
    for b in bad:
        raises(strategy.plan, {**base, **b})
    raises(strategy.plan, "not a dict")
    raises(strategy.plan, {"burn_l": 2.5, "tank_l": 47})


def test_plan_tank_too_small():
    msg = raises(strategy.plan, {"laps": 10, "burn_l": 3.0, "tank_l": 5, "reserve_laps": 1})
    assert msg == "The tank is too small for one lap at that fuel use."


def test_plan_recomputes_when_windows_dont_fit():
    for laps in range(1, 200):
        for tank in (10, 23, 47, 60):
            p = strategy.plan({"laps": laps, "burn_l": 2.3, "tank_l": tank, "reserve_laps": 1})
            assert all(w["close"] >= w["open"] for w in p["windows"]), (laps, tank, p)
            assert sum(s["laps"] for s in p["stints"]) == p["laps"]
            assert all(s["laps"] <= p["laps_per_tank"] for s in p["stints"]), (laps, tank, p)


# ---------------------------------------------------------------- review_block
def _lap(n, used=None, level=None, t=90.0):
    lap = {"id": f"l{n}", "lapNumber": n, "lapTime": t}
    if used is not None:
        lap["fuelUsed"] = used
    if level is not None:
        lap["fuel_level"] = level
    return {"n": n, "t": t, "clean": True, "lap": lap}


def test_review_block_filters_refuel_spikes():
    used = [2.4, 2.5, -30.0, 2.45, 80.0, 0.05, 2.42, 2.38]
    laps = [_lap(i + 1, u) for i, u in enumerate(used)]
    b = strategy.review_block(laps, [x["lap"] for x in laps], laps)
    burn = b["burn"]
    assert burn["source"] == "fuel_used" and burn["n"] == 5, burn
    assert set(burn["laps"]) == {"1", "2", "4", "7", "8"}
    assert 2.38 <= burn["median_l"] <= 2.5 and burn["p90_l"] >= burn["median_l"]
    assert b["pit"] is None and b["version"] == 1


def test_review_block_too_few_laps():
    laps = [_lap(1, 2.4), _lap(2, 2.5)]
    b = strategy.review_block(laps, [x["lap"] for x in laps], laps)
    assert b is not None and b["burn"] is None and b["note"]


def test_review_block_no_fuel_fields_is_none():
    laps = [_lap(i) for i in range(1, 10)]
    assert strategy.review_block(laps, [x["lap"] for x in laps], laps) is None


def test_review_block_fuel_level_fallback():
    levels = [40.0, 37.6, 35.1, 32.7, 44.0, 41.6, 39.2]       # a refuel between laps 4 and 5
    laps = [_lap(i + 1, level=v) for i, v in enumerate(levels)]
    b = strategy.review_block(laps, [x["lap"] for x in laps], laps)
    assert b["burn"]["source"] == "fuel_level" and b["burn"]["n"] == 5, b["burn"]
    assert "5" not in b["burn"]["laps"]
    assert b["level"] == {"start_l": 40.0, "end_l": 39.2, "max_seen_l": 44.0}


def test_review_block_from_engine_with_pit_pair():
    d = review()
    s = d["strategy"]
    assert s["burn"]["source"] == "fuel_used" and s["burn"]["n"] >= 10
    assert "10" not in s["burn"]["laps"] and "11" not in s["burn"]["laps"]      # pit laps never count
    pit = s["pit"]
    assert [r["kind"] for r in pit["laps"]] == ["in", "out"] and pit["n"] == 1
    assert 40 <= pit["loss_s"] <= 50, pit                # 25 s + 20 s added to two normal laps
    assert d["counts"]["pit"] == [10, 11]


def test_unpaired_pit_lap_has_no_loss():
    raw = [{"id": f"l{i}", "lapNumber": i, "lapTime": 90.0, **({"pitIn": True} if i == 5 else {})} for i in range(1, 8)]
    p = strategy.pit_stops(raw, 90.0)
    assert p["n"] == 1 and p["loss_s"] is None and p["laps"] == [{"n": 5, "t": 90.0, "kind": "in"}]
    raw = [{"id": f"l{i}", "lapNumber": i, "lapTime": 90.0 + (30 if i in (5, 6) else 0),
            **({"pitlane": True} if i in (5, 6) else {})} for i in range(1, 8)]
    p = strategy.pit_stops(raw, 90.0)
    assert [r["kind"] for r in p["laps"]] == ["in", "out"] and p["loss_s"] == 60.0


def test_block_from_review():
    d = {"stint": {"laps": [{"n": i, "t": 90, "fuel_used": None, "fuel_level": 40 - 2.4 * i} for i in range(1, 9)]}}
    b = strategy.block_from_review(d)
    assert b["burn"]["source"] == "fuel_level" and abs(b["burn"]["median_l"] - 2.4) < 1e-6 and b["pit"] is None
    d = {"stint": {"laps": [{"n": i, "t": 90, "fuel_used": 2.3, "fuel_level": None} for i in range(1, 9)]}}
    assert strategy.block_from_review(d)["burn"]["source"] == "fuel_used"
    for old in ({}, {"stint": None}, {"stint": {"laps": [{"n": 1, "t": 90, "fuel_used": None, "fuel_level": None}]}},
                json.loads((ROOT / "tests" / "fixtures_23_a" / "sample_review.json").read_text(encoding="utf-8"))):
        assert strategy.block_from_review(old) is None


def test_extend_guards_strategy_and_racecraft():
    d = review(fuel=None, pits=False)
    assert d["strategy"] is None and d["racecraft"] is not None
    orig_s, orig_r = strategy.review_block, racecraft.build
    try:
        strategy.review_block = lambda *a, **k: 1 / 0
        racecraft.build = lambda *a, **k: 1 / 0
        x = samples.engine_review(fuel="used")
        assert x["strategy"] is None and x["racecraft"] is None
        assert x["technique"] is not None and x["consistency"] is not None   # the rest survives
    finally:
        strategy.review_block, racecraft.build = orig_s, orig_r
    assert "strategy" in analysis_plus.KEYS and "racecraft" in analysis_plus.KEYS


# ---------------------------------------------------------------- save_car / load_cars
def test_save_and_load_cars():
    lib = Path(tempfile.mkdtemp(dir=_T))
    assert strategy.load_cars(lib) == {"schema": 1, "cars": {}}
    e = strategy.save_car(lib, "Ford Mustang GT4", tank_l=47)
    assert e["tank_l"] == 47.0 and e["updated"].endswith("Z")
    strategy.save_car(lib, "Ford Mustang GT4", pit_loss_s=51, track="Charlotte Motor Speedway (Roval 2019)")
    d = strategy.load_cars(lib)
    c = d["cars"]["Ford Mustang GT4"]
    assert c["tank_l"] == 47.0 and c["pit_loss_s"] == {"charlotte-motor-speedway-roval": 51.0}, c
    raw = json.loads((lib / "strategy.json").read_text(encoding="utf-8"))
    assert raw["schema"] == 1 and "Ford Mustang GT4" in raw["cars"]
    assert not [p for p in lib.iterdir() if p.name.startswith(".tmp-")]
    for kw in ({"tank_l": 0}, {"tank_l": 500}, {"pit_loss_s": 400, "track": "X"}, {"pit_loss_s": 30}, {}):
        raises(strategy.save_car, lib, "Ford Mustang GT4", *[kw.get(k) for k in ("tank_l", "pit_loss_s", "track")])
    raises(strategy.save_car, lib, "", 40)
    (lib / "strategy.json").write_text("{broken", encoding="utf-8")
    assert strategy.load_cars(lib)["cars"] == {}


# ---------------------------------------------------------------- prefill
class Exploding:
    """Every attribute is a method that raises: proves prefill never calls the API."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError(f"prefill called {name}")
        return boom


def test_prefill_precedence_full():
    tmp = Path(tempfile.mkdtemp(dir=_T))
    cfg, store, week = samples.prefill_world(tmp)
    p = strategy.prefill(cfg, store, Exploding(), samples.TRACK, [samples.CAR], week)
    assert p["race"] == {"laps": 15, "minutes": None, "source": "schedule", "text": "15 laps"}
    assert p["lap_t"]["value"] == 86.05 and p["lap_t"]["n"] == 2 and "last 2 races" in p["lap_t"]["source"]
    assert p["burn"]["value"] == 2.41 and p["burn"]["source"] == "your race on Oct 1"    # latest race, not practice
    assert p["tank"] == {"value": 47.0, "source": "saved for this car", "estimated": False}
    assert p["pit_loss"]["n"] >= 2      # synced pair + review stop
    assert p["reserve_laps"] == 1.0 and p["note"] is None
    # saved pit loss beats everything
    strategy.save_car(cfg["library_folder"], samples.CAR, pit_loss_s=55, track=samples.TRACK)
    p = strategy.prefill(cfg, store, None, samples.TRACK, [samples.CAR], week)
    assert p["pit_loss"]["value"] == 55.0 and "saved" in p["pit_loss"]["source"]


def test_prefill_falls_back_to_synced_laps():
    tmp = Path(tempfile.mkdtemp(dir=_T))
    cfg = {"library_folder": str(tmp / "lib")}
    store = samples.fake_store()
    week = {"length": "40 mins"}
    p = strategy.prefill(cfg, store, None, samples.TRACK, [samples.CAR], week)
    assert p["race"]["minutes"] == 40 and p["race"]["laps"] is None
    assert p["lap_t"]["value"] == round(85.8 * 1.01, 3) and p["lap_t"]["source"] == "estimated from your best lap"
    assert p["burn"]["value"] == 2.3 and "race laps" in p["burn"]["source"]
    assert p["tank"]["estimated"] is True and p["tank"]["value"] == 44.0 and "garage" in p["tank"]["source"]
    assert p["pit_loss"]["n"] == 1 and 44 <= p["pit_loss"]["value"] <= 47, p["pit_loss"]
    assert "sessions" in store.calls


def test_prefill_with_no_data():
    tmp = Path(tempfile.mkdtemp(dir=_T))
    for store in (None, samples.FakeStore(), samples.fake_store(fuel=False, pit=False)):
        p = strategy.prefill({"library_folder": str(tmp)}, store, None, "Nowhere Raceway", ["Some Car"], None)
        assert p["race"]["laps"] is None and p["race"]["minutes"] is None
        for k in ("lap_t", "burn", "tank", "pit_loss"):
            assert p[k] is None, (k, p[k])
        assert p["note"] == strategy.NO_FUEL_NOTE
    p = strategy.prefill({}, Exploding(), Exploding(), samples.TRACK, [], None)   # broken store: still opens
    assert p["burn"] is None


def test_race_length_parsing():
    rl = strategy.race_length
    assert rl({"length": "1 lap"})["laps"] == 1
    assert rl({"length": "1 hour 30 mins"})["minutes"] == 90
    assert rl({"length": "2 hours"})["minutes"] == 120
    assert rl({"length": None})["laps"] is None and rl(None)["source"] is None


# ---------------------------------------------------------------- racecraft
def test_racecraft_lap1_in_analysis_with_segments():
    d = review()
    rc = d["racecraft"]
    l1 = rc["lap1"]
    assert l1["in_analysis"] and l1["note"] == "includes the start" and l1["vs_median_s"] >= 2
    assert l1["segments"] and l1["segments"][0]["id"] == "C1" and all(s["lost_s"] >= 0.2 for s in l1["segments"])
    assert len(l1["segments"]) <= 2


def test_racecraft_lap1_excluded_uses_raw_time():
    d = review(fuel="level", pits=False, multiclass=False, lap1_out=True)
    assert 1 not in [l["n"] for l in d["laps"]] and d["counts"]["excluded_laps"] == [1]
    l1 = d["racecraft"]["lap1"]
    assert l1["in_analysis"] is False and l1["segments"] == [] and l1["t"] > d["median"] * 1.25
    row = d["racecraft"]["timeline"][0]
    assert row["n"] == 1 and set(row["tags"]) == {"lap1", "excluded"} and row["lost_s"] is None
    # report.write path (no raw laps): lap 1 missing entirely gives lap1 = None
    assert racecraft.build({k: v for k, v in d.items() if k != "racecraft"})["lap1"] is None


def test_racecraft_timeline_merges_tags():
    data = {"laps": [{"n": n, "t": 90.0 + (2.0 if n == 6 else 0) + (0.7 if n == 9 else 0) + (1.4 if n == 4 else 0),
                      "clean": n not in (4, 6, 9)} for n in range(1, 13) if n != 14],
            "median": 90.0, "best": {"n": 3, "t": 90.0},
            "counts": {"unclean": [6, 9], "slow": [4], "traffic": [9],
                       "traffic_detail": [{"n": 9, "at_m": 2310.0, "lost": 0.7}], "pit": [14], "excluded_laps": []}}
    rc = racecraft.build(data)
    tl = {r["n"]: r for r in rc["timeline"]}
    assert tl[6]["tags"] == ["unclean"] and tl[6]["lost_s"] == 2.0
    assert tl[9]["tags"] == ["unclean", "traffic"] and tl[9]["at_m"] == 2310.0 and tl[9]["lost_s"] == 0.7
    assert tl[4]["tags"] == ["slow"] and tl[3]["tags"] == ["best"] and tl[1]["tags"] == ["lap1"]
    assert tl[14]["tags"] == ["pit"] and tl[14]["lost_s"] is None and tl[14]["t"] is None
    assert [r["n"] for r in rc["timeline"]] == sorted(tl)
    assert "incident" not in json.dumps(rc)
    assert any("Most likely traffic cost about 0.7 s on lap 9." == s for s in rc["summary"]), rc["summary"]


def test_racecraft_summary_is_not_causal():
    for d in (review(), review(fuel="level", pits=False, multiclass=False, lap1_out=True)):
        s = d["racecraft"]["summary"]
        assert 1 <= len(s) <= 3
        for line in s:
            assert not CAUSAL.search(line), line
            assert "standing" not in line.lower() and "rolling" not in line.lower(), line
        json.dumps(d["racecraft"], allow_nan=False)
        assert "-0.0" not in json.dumps(d["racecraft"])


def test_racecraft_old_reviews():
    old = json.loads((ROOT / "tests" / "fixtures_23_a" / "sample_review.json").read_text(encoding="utf-8"))
    rc = racecraft.build(old)
    assert rc["version"] == 1 and rc["lap1"]["in_analysis"] is True
    for bad in ({}, {"laps": None}, {"laps": [{"n": 1}]}, None, {"laps": [{"n": 1, "t": 90}], "counts": "x"}):
        out = racecraft.build(bad)
        assert out is None or out["timeline"] is not None


def test_insights_lap1_detail_from_racecraft():
    d = review()
    ins = insights.build(d)
    it = next(i for i in ins["bad"] if i["key"] == "lap1")
    assert "Most of it was in C1" in it["detail"] and "includes the start" in it["detail"], it
    assert it["corners"][0] == "C1" and {"key", "title", "text", "cost"} <= set(it)
    d.pop("racecraft")
    it = next(i for i in insights.build(d)["bad"] if i["key"] == "lap1")
    assert "Most of it" not in it["detail"] and "corners" not in it


# ---------------------------------------------------------------- fixtures
def test_fixtures_match_shapes():
    s = json.loads((FX / "sample_strategy.json").read_text(encoding="utf-8"))
    assert s["version"] == 1 and set(s) >= {"burn", "level", "pit", "note"}
    assert set(s["burn"]) >= {"median_l", "p90_l", "n", "source", "laps"}
    assert set(s["level"]) == {"start_l", "end_l", "max_seen_l"}
    assert set(s["pit"]) >= {"laps", "loss_s", "n"} and set(s["pit"]["laps"][0]) == {"n", "t", "kind"}
    r = json.loads((FX / "sample_racecraft.json").read_text(encoding="utf-8"))
    assert set(r["lap1"]) == {"n", "t", "vs_median_s", "in_analysis", "segments", "note"}
    assert all({"n", "t", "lost_s", "tags"} <= set(x) for x in r["timeline"])
    pf = json.loads((FX / "sample_prefill.json").read_text(encoding="utf-8"))
    assert set(pf) >= {"race", "lap_t", "burn", "tank", "pit_loss", "reserve_laps"}
    pl = json.loads((FX / "sample_plan_40.json").read_text(encoding="utf-8"))
    assert set(pl) >= {"laps", "fuel_needed_l", "stops", "start_fuel_l", "stints", "windows", "pit_time_s",
                       "laps_per_tank", "warnings"}
    rv = json.loads((FX / "sample_review.json").read_text(encoding="utf-8"))
    assert rv["strategy"] and rv["racecraft"] and rv["meta"]["test_data"]


if __name__ == "__main__":
    faulthandler.dump_traceback_later(300, exit=True)
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    faulthandler.cancel_dump_traceback_later()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
