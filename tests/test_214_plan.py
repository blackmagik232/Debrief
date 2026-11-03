"""2.14: practice loop, "one thing this session" (practiceplan.py), and the live per-corner measurement it uses
(cornercoach.phases_window, livecoach.corner_phases, Coach.set_focus). Run: python tests/test_214_plan.py

Plans are made from hand-built corner coach reports (one per metric) and from fake .ibt laps (tests/corner_laps.py:
a McLaren GT4 at Zolder with driving differences). No network, temp folders only."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-214-plan-"))

import corner_laps as cl  # noqa: E402
from debrief import cornercoach as cc, ibt, livecoach as lc, practiceplan as pp  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def no_long_dash(obj, where):
    txt = json.dumps(obj, ensure_ascii=False)
    check("—" not in txt and "–" not in txt, f"{where}: no long dashes")


# ---------------------------------------------------------------- hand-built reports
CORNERS = [{"id": f"C{k}", "name": f"T{k}", "apex_m": 300.0 + 500 * k, "start_m": 150.0 + 500 * k,
            "end_m": 450.0 + 500 * k} for k in range(1, 6)]


def card(cid, rule, nums, lost=0.2, conf="likely", secondary=None):
    return {"corner": "T" + cid[1:], "id": cid, "rule": rule, "secondary": secondary, "lost_s": lost,
            "confidence": conf, "how": "Try it.", "why": "Because.", "evidence": "4 best laps vs 8 typical laps.",
            "voice": f"T{cid[1:]}: x",
            "numbers": {k: {"best": b, "typical": t, "z": 3.0} for k, (b, t) in nums.items()}}


def report(cards=(), unclear=(), laps=14, ok=True, per_corner=None):
    return {"ok": ok, "laps": laps, "cards": list(cards), "unclear": list(unclear), "fine": [],
            "corners": CORNERS, "per_corner": per_corner or {},
            "info": {"car": "McLaren 570S GT4", "track": "Zolder", "length_m": 3980.0}}


print("== make_plan: one plan per metric")
p = pp.make_plan(report([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)})]))
check(p["kind"] == "focus" and p["metric"] == "brake_point" and p["target"] == 125, "brake later: target 125 m")
check("15 m later" in p["text"] and p["voice"] == "T2: brake later", f"brake later text: {p['text']}")
check(p["success_rule"] == {"key": "brake_m", "op": "between", "lo": -130.0, "hi": -120.0}, "brake point +/- 5 m")
check(p["now"] == {"typical": 140, "best": 125} and p["zone"]["id"] == "C2" and p["length_m"] == 3980.0,
      "now numbers, zone and track length kept")
check(p["how"] == "Try it." and "T2" in p["reason"], "how and reason from the card")
p = pp.make_plan(report([card("C2", "brake_earlier", {"brake_m": (-150.0, -135.0)})]))
check(p["metric"] == "brake_point" and "earlier" in p["text"] and p["voice"] == "T2: brake earlier", "brake earlier")
p = pp.make_plan(report([card("C4", "lighter", {"peak_pct": (86.76, 100.0)})]))
check(p["metric"] == "peak_brake" and p["target"] == 85, "peak brake: target 85%")
check(p["text"] == "T4: press the brake to about 85%, not all the way" and p["voice"] == "T4: lighter brake",
      f"peak brake text: {p['text']}")
check(p["success_rule"] == {"key": "peak_pct", "op": "<=", "value": 88.0} and "88%" in p["check"],
      f"peak brake check: {p['check']}")
p = pp.make_plan(report([card("C4", "lighter", {"peak_pct": (70.0, 85.0)})]))
check("not 85%" in p["text"], "peak brake when typical laps don't press all the way")
p = pp.make_plan(report([card("C3", "trail", {"trail_pct": (22.7, 0.0), "coast_s": (0.6, 1.2)})]))
check(p["metric"] == "trail" and p["target"] == 25 and p["success_rule"]["op"] == ">=" and
      p["success_rule"]["value"] == 20, "trail: at least target - 5")
p = pp.make_plan(report([card("C3", "coast", {"coast_s": (0.42, 0.95)})]))
check(p["metric"] == "coast" and p["target"] == 0.4 and p["success_rule"]["value"] == 0.5, "coast: target + 0.1 s")
p = pp.make_plan(report([card("C3", "coast", {"coast_s": (0.42, 0.95), "min_kmh": (65.0, 64.0)})]))
check(p["tolerance"] == 0.3 and p["success_rule"]["value"] == 0.7,
      "coast at a slow corner: tolerance is one 5 m step at the slowest speed (0.3 s at 65 km/h)")
p = pp.make_plan(report([card("C5", "throttle", {"pickup_m": (-5.0, 15.0)})]))
check(p["metric"] == "throttle_point" and p["target"] == -5 and p["success_rule"]["value"] == 0.0,
      "throttle point: target + 5 m")
check("at the apex" in p["text"] and "20 m earlier" in p["text"], f"throttle text: {p['text']}")
p = pp.make_plan(report([card("C5", "throttle", {"pickup_m": (-30.0, 0.0)})]))
check("30 m before the apex" in p["text"], "throttle before the apex in words")
p = pp.make_plan(report([card("C3", "carry", {"min_kmh": (136.5, 126.9)})]))
check(p["metric"] == "min_speed" and p["target"] == 137 and p["success_rule"]["value"] == 135, "min speed: target - 2")
no_long_dash(p, "plan")

print("== make_plan: choosing")
cards = [card("C1", "spin", {"spin_share": (0.0, 0.3)}, lost=0.4),
         card("C2", "brake_later", {"brake_m": (-125.0, -140.0)}, lost=0.3, conf="thin"),
         card("C3", "release", {"coast_s": (0.4, 0.9)}, lost=0.2),
         card("C4", "lighter", {"peak_pct": (86.0, 100.0)}, lost=0.1)]
p = pp.make_plan(report(cards))
check(p["id"] == "C4" and p["metric"] == "peak_brake",
      "spin and release have no tested target (skipped), thin waits: the next card with a tested number")
p = pp.make_plan(report([card("C3", "coast", {"coast_s": (0.4, 0.9)}, conf="clear", secondary="trail")]))
check(p["confidence"] == "clear" and p["from_rule"] == "main" and not p["experiment"], "main rule: the card's confidence")
c2 = card("C2", "brake_later", {"brake_m": (-138.0, -140.0), "peak_pct": (80.0, 100.0)}, conf="clear",
          secondary="lighter")
p = pp.make_plan(report([c2]))
check(p["metric"] == "peak_brake" and p["confidence"] == "likely" and p["from_rule"] == "second",
      "second rule: never more than likely")
p = pp.make_plan(report([card("C4", "lighter", {"peak_pct": (86.0, 100.0)}, conf="thin")]))
check(p["confidence"] == "thin" and "few laps" in p["text"], f"thin card: said in the text ({p['text']})")
untested = card("C3", "trail", {"trail_pct": (22.7, 0.0), "coast_s": (0.6, 1.2)})
untested["numbers"]["trail_pct"]["z"] = 1.2
p = pp.make_plan(report([untested]))
check(p["metric"] == "coast", "a number that didn't pass the noise gate is never a target")
untested["numbers"]["coast_s"]["z"] = 0.5
p = pp.make_plan(report([untested]))
check(p["kind"] != "focus", f"no tested number: no focus plan ({p['kind']})")
lockc = card("C4", "lock", {"peak_pct": (80.0, 100.0)})
check(pp.make_plan(report([lockc]))["kind"] != "focus", "lock-up card: peak brake wasn't its test, no focus plan")
p = pp.make_plan(report([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)}, conf="thin")]))
check(p["id"] == "C2", "a thin card when nothing else")
p = pp.make_plan(report([card("C2", "brake_later", {"brake_m": (-138.0, -140.0)}, secondary="lighter")
                         if False else card("C2", "brake_later", {"brake_m": (-138.0, -140.0), "peak_pct": (80.0, 100.0)},
                                            secondary="lighter")]))
check(p["metric"] == "peak_brake", "too small a brake point change: the card's second rule is used")
pc = {"C3": {"numbers": {"t": {"best": 8.9, "typical": 9.2, "z": 1.0}}},
      "C5": {"numbers": {"t": {"best": 10.0, "typical": 10.1, "z": 1.0}}}}
uncl = [{"corner": "T5", "id": "C5", "lost_s": 0.1, "best_laps": 4, "typical_laps": 8},
        {"corner": "T3", "id": "C3", "lost_s": 0.3, "best_laps": 4, "typical_laps": 8}]
p = pp.make_plan(report([], uncl, per_corner=pc))
check(p["kind"] == "consistency" and p["id"] == "C3" and p["metric"] == "corner_time" and p["target"] == 9.05,
      f"no card: the most variable corner ({p['id']}, {p['target']})")
check(p["voice"] == "T3: same every lap", "consistency voice")
check(p["text"].startswith("Try this and see") and p["experiment"] and p["confidence"] == "unclear",
      f"unclear corner: worded as an experiment ({p['text']})")
check("not a known fix" in p["why"] and "scatter" in p["reason"], "experiment: why and reason say so")
p = pp.make_plan(report([]))
check(p["kind"] == "none" and "steady" in p["text"], "no card, nothing variable: none")
p = pp.make_plan(report([], laps=3, ok=False))
check(p["kind"] == "laps_first" and p["text"].startswith("Drive 10 clean laps first") and "3 clean laps" in p["text"],
      f"too few laps: {p['text']}")
mixed = dict(report([], laps=0, ok=False), groups=[{"label": "race laps only", "laps": 5}, {"label": "practice", "laps": 4}])
p = pp.make_plan(mixed)
check(p["kind"] == "laps_first" and "9 clean laps" in p["text"] and "same kind" in p["text"],
      f"laps of mixed kinds: said ({p['text']})")
for bad in (None, {}, {"ok": True, "laps": 20, "cards": "x"}, {"ok": True, "laps": 20, "cards": [None, 3]}, 7):
    p = pp.make_plan(bad)
    check(isinstance(p, dict) and p["kind"] in ("laps_first", "none"), f"bad report {str(bad)[:30]}: no crash")

print("== make_plan: history")
rep = report([card("C1", "carry", {"min_kmh": (120.0, 110.0)}, lost=0.4),
              card("C2", "brake_later", {"brake_m": (-125.0, -140.0)}, lost=0.3)])
first = pp.make_plan(rep)
kept = dict(first, score={"hits": 3, "measured": 10, "next": {"action": "keep"}})
p = pp.make_plan(rep, history=[kept])
check(p["id"] == "C1" and p["repeat"] and "3 of 10" in p["reason"] and p["plan_id"] != first["plan_id"],
      "keep: same corner, says why")
gone = pp.make_plan(report([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)})]), history=[kept])
check(gone["id"] == "C1" and gone["metric"] == "min_speed" and gone["target"] == 120,
      "keep when the card is gone: the old target is kept")
check(gone["experiment"] and gone["confidence"] == "unclear" and gone["text"].startswith("Try this and see")
      and "experiment" in gone["reason"], f"... but as an experiment now ({gone['text']})")
moved = dict(first, score={"hits": 8, "measured": 10, "next": {"action": "next"}})
p = pp.make_plan(rep, history=[moved])
check(p["id"] == "C2" and not p["repeat"], "move on: the next card")
p = pp.make_plan(report([card("C1", "carry", {"min_kmh": (120.0, 110.0)})]), history=[moved])
check(p["id"] == "C1" and "already got" in p["reason"], "move on but nothing else: same corner, said so")


# ---------------------------------------------------------------- scoring
print("== score_plan")
plan = pp.make_plan(report([card("C4", "lighter", {"peak_pct": (86.0, 100.0)})]))


def ph(peak, t):
    return {"peak_pct": peak, "t": t}


rng = np.random.default_rng(5)
laps = [ph(84 + rng.normal(0, 1), 10.20 + rng.normal(0, 0.01)) for _ in range(7)] + \
       [ph(100, 10.38 + rng.normal(0, 0.01)) for _ in range(3)]
s = pp.score_plan(plan, laps)
check(s["hits"] == 7 and s["misses"] == 3 and s["measured"] == 10 and s["text"] == "You hit it on 7 of 10 laps.",
      f"hit/miss counts: {s['text']}")
check(s["verdict"] == "worth" and 0.15 <= s["gain_s"] <= 0.21 and "quicker through T4" in s["verdict_text"]
      and "a lap" not in s["verdict_text"], f"worth it, no lap-time promise: {s['verdict_text']}")
check(s["shuffles"] >= 200 and s["p"] is not None and s["p"] < pp.SHUFFLE_P, f"shuffle test: p {s['p']}, {s['shuffles']}")
check(pp.score_plan(plan, laps)["p"] == s["p"], "fixed seed: the same laps give the same p")
r = pp.shuffle_test([1, 1, 1], [1, 1])
check(r is None, "shuffle test: fewer than 3 misses, no verdict")
r = pp.shuffle_test([10.0, 10.1, 10.2, 10.3], [10.05, 10.15, 10.25, 10.35], n=50)
check(r["n"] >= 200 and r["p"] > 0.3, f"shuffle test: at least 200 shuffles, overlapping laps not significant ({r['p']:.2f})")
check(s["next"]["action"] == "next" and "move on" in s["next"]["text"], "70% or more: move on")
spread = (10.22, 10.30, 10.38, 10.26, 10.34, 10.30)
same = [ph(84, x) for x in spread[:4]] + [ph(100, x) for x in spread]
s = pp.score_plan(plan, same)
check(s["verdict"] == "unclear" and "scatter" in s["verdict_text"], f"same times: no gain claimed (noise gate): {s['verdict']}")
check(s["next"]["action"] == "keep", "under 70%: keep the focus")
tiny = [ph(84, 10.2 - 0.001 * k) for k in range(4)] + [ph(100, 10.21 + 0.001 * k) for k in range(4)]
s = pp.score_plan(plan, tiny)
check(s["verdict"] != "worth", f"0.01 s difference with no scatter is under the noise floor ({s['verdict']})")
s = pp.score_plan(plan, [ph(84, 10.2), ph(84, 10.2), ph(100, 10.4), ph(100, 10.4), ph(100, 10.4)])
check(s["verdict"] == "too_few" and "Not enough laps to tell yet" in s["verdict_text"], "2 hits: not enough laps")
slow = [ph(84, 10.5 + rng.normal(0, 0.01)) for _ in range(4)] + [ph(100, 10.2 + rng.normal(0, 0.01)) for _ in range(4)]
s = pp.score_plan(plan, slow)
check(s["verdict"] == "slower" and "not quicker" in s["verdict_text"], "hits slower: said honestly")
s = pp.score_plan(plan, [])
check(s["verdict"] == "never" and "didn't get a clean lap through T4" in s["text"] and s["next"]["action"] == "keep",
      "no laps: never reached the corner")
s = pp.score_plan(plan, [{"lap": 3, "phases": None}, {"lap": 4, "phases": {"t": 10.0}}])
check(s["verdict"] == "never" and s["laps"] == 2 and s["measured"] == 0, "laps where the corner couldn't be read")
s = pp.score_plan(plan, [{"lap": 7, "file": "x.ibt", "phases": ph(80, 10.2)}])
check(s["per_lap"][0] == {"lap": 7, "file": "x.ibt", "value": 80, "hit": True, "t": 10.2}, "per lap row")
bp = pp.make_plan(report([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)})]))
s = pp.score_plan(bp, [{"brake_m": -124.0, "t": 9}, {"brake_m": -140.0, "t": 9}, {"brake_m": -118.0, "t": 9},
                       {"brake_m": None, "t": 9}])
check(s["hits"] == 1 and s["measured"] == 3 and s["per_lap"][0]["value"] == 124, "brake point: window, metres before")
rep2 = report([card("C4", "lighter", {"peak_pct": (86.0, 100.0)}, lost=0.3),
               card("C2", "brake_later", {"brake_m": (-125.0, -140.0)}, lost=0.2)])
s = pp.score_plan(plan, laps, report=rep2)
check(s["next_plan"]["id"] == "C2", "with the report: next plan moves on to the next card")
s = pp.score_plan(plan, laps[7:], report=rep2)
check(s["next_plan"]["id"] == "C4" and s["next_plan"]["repeat"], "with the report: next plan keeps the focus")
lf = pp.make_plan(report([], laps=2, ok=False))
s = pp.score_plan(lf, [ph(90, 10)] * 6)
check(s["text"] == "You drove 6 clean laps." and s["next"]["action"] == "next", "laps-first plan: laps counted")
s = pp.score_plan(None, None)
check(isinstance(s, dict) and "text" in s, "bad input: no crash")
no_long_dash(s, "score")

print("== from fake laps (tests/corner_laps.py)")
f1 = cl.make(TMP / "s1.ibt", 15)
rep = cc.analyze_files([f1])
plan = pp.make_plan(rep, car="mclaren570sgt4", track="zolder gp")
check(plan["kind"] == "focus" and plan["id"] == rep["cards"][0]["id"], f"plan from the top card: {plan['text']}")
check(plan["zone"]["start_m"] < plan["zone"]["apex_m"] < plan["zone"]["end_m"], "zone from the report's corners")
f2 = cl.make(TMP / "s2.ibt", 12, seed=7)
s = pp.score_files(plan, [f2], report=rep)
laps2, _ = cc.load_laps([f2])
z = plan["zone"]
by_hand = [pp.measure(plan, cc.phases(x["R"], z))[1] for x in laps2]
check(s["laps"] == len(laps2) and s["hits"] == sum(1 for h in by_hand if h), f"score_files: {s['text']}")
check(s["verdict"] in ("worth", "unclear", "too_few", "slower") and s["next_plan"]["kind"] == "focus",
      f"verdict {s['verdict']}: {s['verdict_text']}")
# every metric, scored on the same laps
for m, rule in (("brake_point", "brake_later"), ("peak_brake", "lighter"), ("min_speed", "carry")):
    c = next((c for c in rep["cards"] if c["rule"] == rule), None)
    if c is None:
        continue
    q = pp._from_card(rep, c, m)
    check(q is not None and pp.score_files(q, [f2])["measured"] > 0, f"{m} ({c['corner']}) scored on fake laps")


# ---------------------------------------------------------------- persistence
print("== plans saved per car + track")
F = TMP / "plans"
check(pp.load_plans(F, "car", "track") == [] and pp.current_plan(F, "car", "track") is None, "nothing saved yet")
a = pp.make_plan(rep2)
b = pp.make_plan(rep2)
check(pp.save_plan(F, "mclaren570sgt4", "zolder gp", a) and pp.save_plan(F, "mclaren570sgt4", "zolder gp", b),
      "saved two plans")
got = pp.load_plans(F, "mclaren570sgt4", "zolder gp")
check([x["plan_id"] for x in got] == [b["plan_id"], a["plan_id"]], "newest first")
check(pp.plan_path(F, "mclaren570sgt4", "zolder gp").name == "mclaren570sgt4__zolder-gp.json", "file name")
sc = pp.score_plan(a, laps, report=rep2)
check(pp.save_score(F, "mclaren570sgt4", "zolder gp", a["plan_id"], sc), "score saved")
got = pp.load_plans(F, "mclaren570sgt4", "zolder gp")
check(got[1]["score"]["hits"] == 7 and "next_plan" not in got[1]["score"], "score on the right plan (no next plan)")
check(not pp.save_score(F, "mclaren570sgt4", "zolder gp", "nope", sc), "unknown plan id: False")
nxt = pp.make_plan(rep2, history=got)
check(nxt["id"] == "C2", "history from the file feeds the next plan")
check(pp.load_plans(F, "other", "zolder gp") == [], "another car: nothing")
pp.save_plan(F, "mclaren570sgt4", "zolder gp", dict(b, text="changed"))
got = pp.load_plans(F, "mclaren570sgt4", "zolder gp")
check(len(got) == 2 and got[0]["text"] == "changed", "same plan id replaced, not doubled")
path = pp.plan_path(F, "bad", "file")
path.write_text("{not json", encoding="utf-8")
check(pp.load_plans(F, "bad", "file") == [] and pp.current_plan(F, "bad", "file") is None, "corrupt file: []")
check(not pp.save_score(F, "bad", "file", "x", sc), "corrupt file: score not saved")
check(pp.save_plan(F, "bad", "file", a) and path.with_name(path.name + ".bad").exists(),
      "corrupt file set aside as .bad, a new one started")
check(pp.load_plans(F, "bad", "file")[0]["plan_id"] == a["plan_id"], "new file readable")
pp.plan_path(F, "odd", "x").write_text(json.dumps({"plans": [1, None, {"plan_id": "k"}]}), encoding="utf-8")
check(pp.load_plans(F, "odd", "x") == [{"plan_id": "k"}], "junk entries dropped")
pp.plan_path(F, "odd2", "x").write_bytes(b"\xff\xfe\x00bad")
check(pp.load_plans(F, "odd2", "x") == [], "binary junk: []")
for k in range(pp.MAX_HISTORY + 5):
    pp.save_plan(F, "many", "x", {"plan_id": str(k)})
check(len(pp.load_plans(F, "many", "x")) == pp.MAX_HISTORY, f"kept the last {pp.MAX_HISTORY}")
check(not pp.save_plan(F, "x", "y", None), "not a plan: False")
check(not [p for p in F.iterdir() if p.name.endswith(".tmp")], "no temp files left")


# ---------------------------------------------------------------- progress cues
print("== progress_cue")
pk = pp.make_plan(report([card("C4", "lighter", {"peak_pct": (86.0, 100.0)})]))


def words(s):
    return len(s.replace(",", " ").split())


cases = [(pk, {"peak_pct": 84.4}, "Good, 84"), (pk, {"peak_pct": 99.0}, "Too much brake"),
         (pp.make_plan(report([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)})])), {"brake_m": -139.0},
          "Braked too early"),
         (pp.make_plan(report([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)})])), {"brake_m": -110.0},
          "Braked too late"),
         (pp.make_plan(report([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)})])), {"brake_m": -126.0},
          "Good brake point"),
         (pp.make_plan(report([card("C3", "trail", {"trail_pct": (25.0, 0.0)})])), {"trail_pct": 0.0},
          "Hold the brake in"),
         (pp.make_plan(report([card("C3", "coast", {"coast_s": (0.4, 1.0)})])), {"coast_s": 0.45}, "Good, no coasting"),
         (pp.make_plan(report([card("C3", "coast", {"coast_s": (0.4, 1.0)})])), {"coast_s": 0.9}, "Less coasting"),
         (pp.make_plan(report([card("C5", "throttle", {"pickup_m": (-5.0, 15.0)})])), {"pickup_m": 15.0},
          "Throttle earlier"),
         (pp.make_plan(report([card("C3", "carry", {"min_kmh": (136.5, 126.9)})])), {"min_kmh": 129.0}, "More speed"),
         (pp.make_plan(report([card("C3", "carry", {"min_kmh": (136.5, 126.9)})])), {"min_kmh": 136.0}, "Good speed")]
for plan_, phx, want in cases:
    got = pp.progress_cue(plan_, phx)
    check(got == want and 2 <= words(got) <= 4 and got in pp.cue_vocabulary(plan_), f"{plan_['metric']}: {got!r}")
check(pp.progress_cue(pk, {"peak_pct": 84.0, "_reliable": False}) is None, "unreliable pass: nothing")
check(pp.progress_cue(pk, None) is None and pp.progress_cue(pk, {"peak_pct": None}) is None, "no number: nothing")
check(pp.progress_cue(pp.make_plan(report([], laps=2, ok=False)), {"peak_pct": 80}) is None, "no focus: nothing")
check(pp.progress_cue(None, {"peak_pct": 80}) is None, "no plan: nothing")


# ---------------------------------------------------------------- live: one corner from live samples
print("== live measurement (cornercoach.phases_window / livecoach.corner_phases)")
laps1, info1 = cc.load_laps([f1])
corners = rep["corners"]
KEYS = ("brake_m", "peak_pct", "trail_pct", "coast_s", "pickup_m", "min_kmh", "t")
TOL = {"brake_m": 5.01, "peak_pct": 2.0, "trail_pct": 5.0, "coast_s": 0.3, "pickup_m": 5.01, "min_kmh": 0.6, "t": 0.03}
worst = {k: 0.0 for k in KEYS}
n_cmp = 0
with ibt.open_ibt(f1) as ib:
    names = ("SessionTime", "LapDistPct", "Speed", "Brake", "Throttle", "SteeringWheelAngle", "Gear")
    cols = {nm: ib.col(nm) for nm in names}
    step = max(1, int(round(ib.tick_rate / 20)))
    for x in ibt.laps(ib):
        lp = next((q for q in laps1 if q["lap"] == x["lap"]), None)
        if lp is None:
            continue
        a, b = max(0, x["start"] - 400), min(len(cols["Speed"]), x["end"] + 400)
        for c in corners:
            whole = cc.phases(lp["R"], c)
            # (coasting: one 5 m cell at the slowest speed, about 0.28 s at 65 km/h, can flip when the throttle steps up)
            # the samples the live coach would keep: 20 a second, from 30 m before the zone to just past it
            L = info1["length_m"]
            pct = np.asarray(cols["LapDistPct"][a:b:step], float)
            samples = [{nm: float(cols[nm][a + i * step]) for nm in names} for i in range(len(pct))]
            rel = ((pct * L - c["apex_m"] + L / 2) % L) - L / 2
            lapno = np.asarray(ib.col("Lap")[a:b:step])
            sel = [i for i in range(len(pct)) if lapno[i] in (x["lap"], x["lap"] - 1, x["lap"] + 1)]
            # one pass: the run of samples inside the window that belongs to this lap's pass
            inside = [i for i in sel if (c["start_m"] - c["apex_m"] - 30) <= rel[i] <= (c["end_m"] - c["apex_m"])]
            runs, cur = [], []
            for i in inside:
                if cur and i != cur[-1] + 1:
                    runs.append(cur)
                    cur = []
                cur.append(i)
            if cur:
                runs.append(cur)
            for run in runs:
                if run[-1] + 1 >= len(samples):
                    continue
                win = [samples[i] for i in run] + [samples[run[-1] + 1]]
                live = lc.corner_phases(win, c, L)
                if live is None or abs(live["t"] - whole["t"]) > 0.5:
                    continue                      # the pass on the neighbouring lap
                n_cmp += 1
                for k in KEYS:
                    if whole.get(k) is None or live.get(k) is None:
                        if (whole.get(k) is None) != (live.get(k) is None):
                            worst[k] = 99.0
                        continue
                    worst[k] = max(worst[k], abs(whole[k] - live[k]))
check(n_cmp >= 30, f"compared {n_cmp} live passes with the whole-lap numbers")
for k in KEYS:
    check(worst[k] <= TOL[k], f"{k}: live window within {TOL[k]} of the whole lap (worst {worst[k]:.3f})")
c = corners[1]
short = [{"SessionTime": i * 0.05, "LapDistPct": (c["apex_m"] + i) / 3980.0, "Speed": 30.0, "Brake": 0.0,
          "Throttle": 1.0} for i in range(40)]
check(lc.corner_phases(short, c, 3980.0) is None, "samples not covering the zone: None")
check(lc.corner_phases([], c, 3980.0) is None and lc.corner_phases(None, None, None) is None, "no samples: None")
check(cc.phases_window({"LapDistPct": [0.1] * 3}, c, 3980.0) is None, "bad window: None")


print("== live: LiveFocus in the coach (an .ibt played back as if live)")
ref = lc.build_reference([f1])
check(ref is not None, "reference from the fake file")
for top in (rep["cards"][0],):
    plan = pp.make_plan(rep)
src = lc.IbtPlaybackSource(f2, speed=0)
focus = pp.LiveFocus(plan, repeat_laps=1)
coach = lc.Coach(ref, [], lc.Settings())
coach.set_focus(focus)
spoken = []
while True:
    s = src.read()
    if s is None:
        break
    spoken += [c.text for c in coach.feed(s)]
st = focus.status()
laps2, _ = cc.load_laps([f2])
check(st["measured"] >= len(laps2) - 2, f"live: {st['measured']} passes measured ({len(laps2)} clean laps in the file)")
by_lap = {x["lap"]: pp.measure(plan, cc.phases(x["R"], plan["zone"])) for x in laps2}
agree = [p for p in st["passes"] if p["lap"] in by_lap and by_lap[p["lap"]][1] == p["hit"]]
check(len(agree) >= len([p for p in st["passes"] if p["lap"] in by_lap]) - 1,
      f"live hit/miss agrees with the session score ({len(agree)} of {len(st['passes'])})")
vocab = set(pp.cue_vocabulary(plan))
said = [t for t in spoken if t in vocab]
check(len(said) >= 1, f"progress cues spoken: {said[:4]}")
check(all(2 <= words(t) <= 4 for t in said), "progress cues are 2-4 words")
check(coach.status()["focus"]["progress"].startswith("Hit "), f"status: {coach.status()['focus']['progress']}")
check(set(focus.vocabulary()) <= set(coach.vocabulary()), "the coach's vocabulary includes the focus cues")
# repeat rule: the same result is not said every lap
f3 = pp.LiveFocus(plan, repeat_laps=3)
src = lc.IbtPlaybackSource(f2, speed=0)
outs = []
while True:
    s = src.read()
    if s is None:
        break
    r = f3.feed(s, ok=True, lap=s.get("Lap"))
    if r:
        outs.append((s.get("Lap"), r))
check(len(outs) < f3.status()["measured"], f"cues limited ({len(outs)} for {f3.status()['measured']} passes)")
# not ok (car alongside, off lap ...): nothing measured
f4 = pp.LiveFocus(plan)
src = lc.IbtPlaybackSource(f2, speed=0)
while True:
    s = src.read()
    if s is None:
        break
    f4.feed(s, ok=False, lap=s.get("Lap"))
check(f4.status()["measured"] == 0 and "No clean pass" in f4.status()["progress"], "passes that aren't ok don't count")
# muted coach: the focus still measures, nothing is said
coach2 = lc.Coach(ref, [], lc.Settings(muted=True))
coach2.set_focus(pp.LiveFocus(plan))
src = lc.IbtPlaybackSource(f2, speed=0)
out = []
while True:
    s = src.read()
    if s is None:
        break
    out += coach2.feed(s)
check(out == [] and coach2.status()["focus"]["measured"] > 0, "muted: measured, never spoken")
check(not pp.LiveFocus(pp.make_plan(report([], laps=1, ok=False))).active, "no focus plan: LiveFocus inactive")
check(pp.LiveFocus(None).feed({"LapDistPct": 0.5}) is None, "LiveFocus(None): no crash")


class Boom:
    corner_id = "C1"

    def feed(self, *a, **k):
        raise RuntimeError("boom")

    def status(self):
        raise RuntimeError("boom")


coach3 = lc.Coach(ref, [], lc.Settings())
coach3.set_focus(Boom())
src = lc.IbtPlaybackSource(f2, speed=0)
for _ in range(500):
    coach3.feed(src.read())
check(coach3.status()["focus"] is None, "a failing focus never stops the coach")
check("SteeringWheelAngle" in lc.CHANNELS, "live samples carry steering (for the trail number)")

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
