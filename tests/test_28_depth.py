"""Debrief 2.9: the depth block (root-cause chains, cost to the next brake point, comparison-lap matching, the same
mistake across races, conditions-aware baseline), on made-up reviews with known answers.

    python3 tests/test_28_depth.py
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

from debrief import app, depth, engine, report  # noqa: E402

DRY = {"track_temp_c": 20.0, "air_temp_c": 18.0, "wetness": 0.0, "track_usage": 50.0}
WET = {"track_temp_c": 17.0, "air_temp_c": 18.0, "wetness": 50.0, "track_usage": 50.0}
L = 4000.0


def corner_row(rng, latent=0.0, noise=1.0):
    """One lap through a corner. latent > 0: braked later, lower minimum, later throttle, lower exit, more time."""
    n = lambda s: rng.gauss(0, s * noise)
    return {"t": round(10 + 0.3 * latent + n(0.03), 3), "min_mph": round(70 - 3 * latent + n(0.3), 3), "min_at": 300.0,
            "gear": 3, "peak_brake": 95.0, "brake_on": round(160 + 10 * latent + n(1), 3),
            "full_thr": round(400 + 20 * latent + n(2), 3), "abs_s": 0.1, "coast_s": 1.0,
            "exit_mph": round(95 - 4 * latent + n(0.3), 3)}


def review(n_laps=14, conditions=DRY, started="2026-10-01T18:00:00+00:00", event="E1", seed=1, linked=True, median=90.0):
    rng = random.Random(seed)
    per = {}
    laps, traces = [], {}
    d = [i * 10.0 for i in range(int(L / 10))]
    for n in range(1, n_laps + 1):
        z = rng.gauss(0, 1) if linked else None
        per[str(n)] = corner_row(rng, z) if linked else {k: v for k, v in corner_row(rng, 0.0, 12.0).items()}
        ex = per[str(n)]["exit_mph"]
        # the straight after C1 runs 500 m .. 1500 m; a slower exit means more time on it
        tt, now = [], 0.0
        for x in d:
            tt.append(round(now, 2))
            v = 100.0 if not 450 <= x <= 1500 else 100.0 + (ex - 95.0) * (1 - (x - 450) / 1050)
            now += 10.0 / (v * 0.44704)
        traces[f"me{n}"] = {"tt": tt, "v": [100] * len(tt), "t": round(now, 3)}
        laps.append({"n": n, "t": round(now, 3), "clean": True})
    c1 = {"id": "C1", "apex_m": 300.0, "start_m": 140.0, "end_m": 495.0, "exit_at_m": 150.0, "per_lap": per}
    c2_rows = {str(n): dict(corner_row(rng), brake_on=1500.0 + rng.random(), min_at=1700.0) for n in range(1, n_laps + 1)}
    c2 = {"id": "C2", "apex_m": 1700.0, "start_m": 1490.0, "end_m": 1900.0, "exit_at_m": 150.0, "per_lap": c2_rows}
    return {"meta": {"session": "Race", "started_at": started, "event_id": event, "track": "T", "car": "K"},
            "combo_ids": {"track_id": 1, "car_id": 2}, "L": L, "step": 10.0, "median": median, "laps": laps,
            "corners": [c1, c2], "traces": traces, "conditions": conditions, "counts": {}}


def save(folder, name, d):
    p = Path(folder) / "reviews" / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "review.json").write_text(json.dumps(d), encoding="utf-8")


def test_chain_found_in_order_and_worded_as_correlation():
    out = depth.chains(review())
    c1 = [c for c in out if c["corner"] == "C1"]
    assert c1, out
    c = c1[0]
    assert c["steps"][:4] == ["braking later", "a lower minimum speed", "later full throttle", "a lower exit speed"], c
    assert c["worth_s"] > 0 and "went together" in c["sentence"]
    assert all(w not in c["sentence"].lower() for w in ("because", "caused"))
    assert "—" not in c["sentence"]


def test_chain_needs_enough_clean_laps():
    assert not [c for c in depth.chains(review(n_laps=9)) if c["corner"] == "C1"]
    d = review()
    for lap in d["laps"][:6]:
        lap["clean"] = False
    assert not [c for c in depth.chains(d) if c["corner"] == "C1"]


def test_chain_from_noise_is_rare():
    hits = 0
    for seed in range(200):
        d = review(n_laps=12, seed=seed, linked=False)
        hits += bool(depth.chains(d))
    assert hits <= 5, hits    # 2 corners a review, 400 corners: the gate holds well under the 5% per corner


def test_shared_trend_through_the_race_is_not_a_link():
    """A track drying: every number moves with the lap number, with no link left once that is taken out."""
    rng = random.Random(3)
    d = review(n_laps=16, linked=False)
    for n, row in d["corners"][0]["per_lap"].items():
        k = int(n) + rng.gauss(0, 0.05)
        q = lambda s: rng.gauss(0, s)
        row.update(t=10 + 0.2 * k + q(0.05), min_mph=70 - k + q(1), brake_on=160 + k + q(2), full_thr=400 + 3 * k + q(3),
                   exit_mph=95 - k + q(1))
    assert not [c for c in depth.chains(d) if c["corner"] == "C1"]


def test_cost_to_next_brake_point_size_and_thirds():
    out = depth.costs(review())
    c = next(x for x in out if x["corner"] == "C1")
    assert c["next"] == "C2" and 800 < c["length_m"] < 1100, c
    # 1 mph at the exit fades to nothing over the straight: about 1000 m x 0.5 / (100 mph) worth, roughly 0.1 s/mph
    assert 0.03 < c["s_per_mph"] < 0.2, c
    assert abs(sum(c["thirds_s"]) - c["cost_s"]) < 0.01, c
    assert c["thirds_s"][0] > c["thirds_s"][2] > 0, c


def test_cost_hidden_with_few_laps_or_no_traces_or_a_short_gap():
    assert not depth.costs(review(n_laps=9))
    d = review()
    d.pop("traces")
    assert not depth.costs(d)
    d = review()
    for row in d["corners"][1]["per_lap"].values():
        row["brake_on"] = 480.0   # next brake point is before the exit point, ie wraps past half the lap
    assert not [c for c in depth.costs(d) if c["corner"] == "C1"]


def test_thresholds_scale_with_the_lap():
    d = review()
    assert depth._scales(d)[0] == 1.0
    d["median"] = 180.0
    assert depth._scales(d)[0] == 2.0
    assert depth._min_effect("t", 2.0, 1.0) == 2 * depth._min_effect("t", 1.0, 1.0)
    assert depth._min_effect("brake_on", 1.0, 1.5) == 1.5 * depth._min_effect("brake_on", 1.0, 1.0)


def _lap(t, wet=0.0, temp=20.0, kind=3, **kw):
    return {"lapTime": t, "trackWetness": wet, "trackTemp": temp, "airTemp": 18.0, "sessionType": kind, **kw}


def _time(c):
    return c["lapTime"]


def test_pick_main_target_scales_and_is_0_8_on_a_90_s_lap():
    near, far = _lap(89.2), _lap(89.9)
    assert depth.pick_main([near, far], 90.0, None, _time) is near
    # on a 180 s lap the target gap is 1.6 s, so 178.4 beats 179.2
    a, b = _lap(178.4), _lap(179.2)
    assert depth.pick_main([a, b], 180.0, None, _time) is a


def test_pick_main_prefers_conditions_then_session():
    me = depth.context_of([_lap(90, wet=50.0)])
    wet_far, dry_near = _lap(85.0, wet=50.0), _lap(89.2)
    assert depth.pick_main([dry_near, wet_far], 90.0, me, _time) is wet_far
    me = depth.context_of([_lap(90)])
    race_far, prac_near = _lap(85.0), _lap(89.2, kind=1)
    assert depth.pick_main([prac_near, race_far], 90.0, me, _time) is race_far


def test_pick_refs_without_conditions_on_a_90_s_lap_is_as_before():
    class Api:
        def teams(self):
            return []

        def find_laps(self, **kw):
            return [{"id": i, "lapTime": t, "clean": True, "driver": {"slug": f"d{i}"}}
                    for i, t in enumerate((88.0, 89.2, 89.8))]
    out = app.pick_refs(Api(), 1, 2, 90.0, "me", 3)
    assert out[0]["lapTime"] == 89.2 and out[0]["_main"] and out[0]["_role"].startswith("Main comparison")


def test_match_refs_swaps_a_mismatched_main_and_says_so():
    mine = [_lap(100.0, wet=50.0), _lap(101.0, wet=50.0)]
    dry = _lap(90.0, _main=True, _role="Main comparison (10.00s quicker than your best)")
    wet = _lap(98.0, wet=50.0, _main=False, _role="Close to your pace (2.00s quicker than your best)")
    out = depth.match_refs([dry, wet], mine)
    assert wet["_main"] and not dry["_main"] and out["switched"] and out["status"] == "similar"
    assert wet["_role"] == "Main comparison (2.00s quicker than your best)"
    assert dry["_role"] == "Close to your pace (10.00s quicker than your best)"


def test_match_refs_keeps_main_says_different_never_a_rival_unknown_is_quiet():
    mine = [_lap(100.0, wet=50.0)]
    dry = _lap(90.0, _main=True, _role="Main comparison (x)")
    rival = _lap(98.0, wet=50.0, _main=False, _rival=True, _role="Rival (y)")
    out = depth.match_refs([dry, rival], mine)
    assert dry["_main"] and not rival["_main"] and out["status"] == "different" and "different conditions" in out["sentence"]
    assert depth.match_refs([{"lapTime": 90.0, "_main": True}], [{"lapTime": 100.0}]) is None
    assert depth.match_refs([], mine) is None


def test_compare_conditions():
    assert depth.compare_conditions(DRY, dict(DRY, track_temp_c=27.0)) == "similar"
    assert depth.compare_conditions(DRY, dict(DRY, track_temp_c=29.0)) == "different"
    assert depth.compare_conditions(DRY, WET) == "different"
    assert depth.compare_conditions(DRY, {"track_temp_c": 20.0}) == "unknown"
    assert depth.compare_conditions(DRY, None) == "unknown"


def _with_mistake(d, n, **dev):
    for c in d["corners"][:1]:
        r = c["per_lap"][str(n)]
        r["t"] = 13.0
        for k, v in dev.items():
            r[k] = r[k] + v
    return d


def test_same_mistake_across_races_in_similar_conditions_only():
    a = review(started="2026-10-01T18:00:00+00:00", event="A", seed=5)
    b = review(started="2026-10-08T18:00:00+00:00", event="B", seed=6, conditions=WET)
    me = review(started="2026-10-15T18:00:00+00:00", event="C", seed=7)
    for d in (a, me):
        _with_mistake(d, 5, brake_on=40)
        _with_mistake(d, 9, brake_on=40)
    _with_mistake(b, 5, brake_on=40)
    out = depth.same_mistakes(me, [b, a])
    assert len(out) == 1 and out[0]["corner"] == "C1" and out[0]["kind"] == "late_brake", out
    assert out[0]["here"] == 2 and out[0]["total"] == 4 and len(out[0]["races"]) == 1, out
    assert depth.same_mistakes(me, [b]) == []


def test_same_mistake_needs_three_and_skips_first_lap_and_traffic():
    a = _with_mistake(review(event="A", seed=5), 5, brake_on=40)
    me = _with_mistake(review(started="2026-10-15T18:00:00+00:00", event="C", seed=7), 5, brake_on=40)
    assert depth.same_mistakes(me, [a]) == []
    me = _with_mistake(me, 1, brake_on=40)     # the first lap reviewed doesn't count
    assert depth.same_mistakes(me, [a]) == []
    me = _with_mistake(me, 8, brake_on=40)
    me["counts"]["traffic_possible"] = [8]
    assert depth.same_mistakes(me, [a]) == []


def test_baseline_only_similar_conditions():
    me = review(started="2026-10-15T18:00:00+00:00", event="C")
    a, b = copy.deepcopy(me), copy.deepcopy(me)
    for x in a["laps"]:
        x["t"] += 1.0
    b["conditions"] = WET
    for x in b["laps"]:
        x["t"] += 10.0
    out = depth.baseline(me, [a, b])
    assert out["n"] == 1 and abs(out["delta_s"] + 1.0) < 0.01 and "quicker" in out["sentence"], out
    assert out["left_out"][0]["why"] == "different conditions"
    only = depth.baseline(me, [b])
    assert only["n"] == 0 and "delta_s" not in only and "aren't compared" in only["sentence"]
    a["conditions"] = None
    assert depth.baseline(me, [a]) is None
    assert depth.baseline(me, []) is None


def test_earlier_reviews_only_earlier_same_combo_same_kind():
    with tempfile.TemporaryDirectory() as tmp:
        save(tmp, "a", review(started="2026-10-01T18:00:00+00:00", event="A"))
        save(tmp, "later", review(started="2026-11-01T18:00:00+00:00", event="L"))
        save(tmp, "same", review(started="2026-10-01T18:00:00+00:00", event="C"))
        other = review(started="2026-10-02T18:00:00+00:00", event="O")
        other["combo_ids"]["track_id"] = 99
        save(tmp, "other", other)
        prac = review(started="2026-10-03T18:00:00+00:00", event="P")
        prac["meta"]["session"] = "Practice"
        save(tmp, "prac", prac)
        me = review(started="2026-10-15T18:00:00+00:00", event="C")
        got = depth.earlier_reviews(tmp, me)
        assert [d["meta"]["event_id"] for d in got] == ["A"], got
        assert depth.earlier_reviews(None, me) == []
        assert depth.earlier_reviews(os.path.join(tmp, "nothing"), me) == []


def test_old_review_gets_block_and_no_number_changes():
    """report.write on a saved review: nothing that was there changes; only depth is added."""
    with tempfile.TemporaryDirectory() as tmp:
        d = review()
        before = {k: json.dumps(v, sort_keys=True) for k, v in d.items()}
        os.environ["DEBRIEF_SETTINGS"] = str(Path(tmp) / "no-settings.toml")
        try:
            report._extras(d)
        finally:
            del os.environ["DEBRIEF_SETTINGS"]
        assert isinstance(d.get("depth"), dict) and d["depth"]["chains"]
        for k, v in before.items():
            assert json.dumps(d[k], sort_keys=True) == v, k


def test_nothing_to_say_is_none_and_bad_data_never_raises():
    assert depth.for_review({"laps": []}, folder="") is None
    assert depth.for_review({"laps": [{"n": 1}], "corners": "x", "traces": 3, "conditions": 1}, folder="") is None
    assert depth.for_review("nope") is None
    d = review(linked=False, n_laps=9)
    d["conditions"] = None
    assert depth.for_review(d, folder="") is None


def test_saved_depth_is_not_worked_out_again():
    d = review()
    d["depth"] = {"chains": [{"sentence": "saved"}], "version": 1}
    report._extras(d)
    assert d["depth"]["chains"][0]["sentence"] == "saved"


def test_page_block_placed_where_agreed_and_uses_textcontent():
    t = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    assert t.count('<section id="depth" hidden>') == 1
    assert t.index('<section id="depth" hidden>') < t.index('<details class="deep"')
    assert t[t.index('<section id="depth" hidden>'):t.index('<details class="deep"')].count("<section") == 1
    a, b = t.index("safe('depth',"), t.index("// ---------- 2.8: the race, from a replay scan")
    block = t[a:b]
    assert a < b and "innerHTML" not in block and "—" not in block


def _synth_race():
    import importlib.util
    spec = importlib.util.spec_from_file_location("synth_a_28", ROOT / "tests" / "fixtures_a" / "synth.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.race(n=16)


def test_real_engine_swaps_main_and_writes_the_note_and_no_refs_means_no_note():
    meta, laps, tel = _synth_race()
    for lap in laps:
        lap.update(trackWetness=50.0, trackTemp=20.0)
    base = engine.analyze(meta, laps, tel, [], {}, log=lambda m: None)
    assert "ref_match" not in base
    lid = next(iter(tel))
    t = float(next(x for x in laps if x["id"] == lid)["lapTime"])
    dry = {"id": "dry", "lapTime": t - 0.3, "clean": True, "driver_name": "Dry Lap", "trackWetness": 0.0, "trackTemp": 20.0,
           "_main": True, "_role": "Main comparison (a)"}
    wet = {"id": "wet", "lapTime": t - 0.2, "clean": True, "driver_name": "Wet Lap", "trackWetness": 50.0, "trackTemp": 20.0,
           "_main": False, "_role": "Close to your pace (b)"}
    d = engine.analyze(meta, laps, tel, [dry, wet], {"dry": tel[lid], "wet": tel[lid]}, log=lambda m: None)
    assert d["ref_match"]["switched"] and d["ref_match"]["status"] == "similar"
    assert next(r for r in d["refs"] if r["main"])["name"] == "Wet Lap"


def test_real_engine_review_written_and_read_back_keeps_every_number():
    meta, laps, tel = _synth_race()
    d = engine.analyze(meta, laps, tel, [], {}, log=lambda m: None)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DEBRIEF_SETTINGS"] = str(Path(tmp) / "no-settings.toml")
        try:
            before = json.loads(json.dumps(report.sanitize(copy.deepcopy(d))))
            report.write(Path(tmp), d)
            saved = json.loads((Path(tmp) / "review.json").read_text(encoding="utf-8"))
        finally:
            del os.environ["DEBRIEF_SETTINGS"]
    for k, v in before.items():
        if v is not None:     # an empty optional key is left out on writing, as it always was
            assert saved[k] == v, k


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"All {len(tests)} depth tests passed.")
