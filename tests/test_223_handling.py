"""2.23: automatic handling read (debrief/handling.py) and setup advice (debrief/setupadvice.py).

Synthetic laps come from the 2.15 balance test's car builder (a car with a known steering law); each scenario adds a
known problem on purpose: understeer in every mid-corner, instability on the way in, traction loss on the way out,
trouble on only some laps, trouble in one corner, laps that must be left out (traffic, off track), too few laps.
    python3 tests/test_223_handling.py
No network, temp folders only."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-223-"))

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


_src = (ROOT / "tests" / "test_215_balance.py").read_text(encoding="utf-8")
NS = {"__name__": "balance_builder_223", "__file__": str(ROOT / "tests" / "test_215_balance.py")}
exec(compile(_src[:_src.index("# ---------------------------------------------------------------- 0. the filter")],
             "test_215_balance.py", "exec"), NS)

from debrief import handling as hd, setupadvice as sa, balance as bl  # noqa: E402

make_lap, session, INFO, CORNERS, L, RATE = (NS[k] for k in ("make_lap", "session", "INFO", "CORNERS", "L", "RATE"))


def mids(amount):
    def f(d, kap, ay):
        out = np.zeros_like(d)
        for A, _v, _s in CORNERS:
            m = np.abs(d - A) < 18
            out[m] += np.sign(kap[m]) * amount
        return out
    return f


def laps_with(seed, n, post=None, extra=None, every=None):
    """n laps; post(lap, i, d) may change a lap's arrays (d = distance in metres)."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        x = make_lap(rng, extra=extra if (every is None or i % every == 0) else None)
        x["lap"] = i + 2
        if post:
            post(x, i, (np.asarray(x["pct"]) * L))
        out.append(x)
    return out


def ids(rep, verdict=None):
    return [i["id"] for i in rep["issues"] if verdict is None or i["verdict"] == verdict]


def find(rep, kind, phase):
    return next((i for i in rep["issues"] if i["kind"] == kind and i["phase"] == phase), None)


# ---------------------------------------------------------------- 1. nothing wrong
r0 = hd.analyze(laps_with(1, 10), info=INFO)
check(r0["ok"] and not r0["setup_issues"], f"a balanced car: no setup issue ({ids(r0)})")
check(not r0["driving_issues"], f"a balanced car: no driving issue either ({ids(r0)})")
check(r0["channels"]["wheel_speeds"] is False and "wheel-speed" in r0["channels"]["note"],
      "no wheel-speed channels: said, and the read still ran")

# ---------------------------------------------------------------- 2. understeer in every mid-corner
ru = hd.analyze(laps_with(2, 11, extra=mids(40.0)), info=INFO)
mu = [i for i in ru["setup_issues"] if i["kind"] == "understeer" and i["phase"] == "mid"]
check(bool(mu), f"mid-corner understeer found as a SETUP issue ({ids(ru)})")
check(all(i["laps_showing"] >= 9 and i["laps"] == 11 for i in mu), f"it shows on most of the 11 laps ({[(i['laps_showing'], i['laps']) for i in mu]})")
check(all(len(i["corners"]) >= 2 for i in mu), "in several corners of the same kind")
check(not any(i["kind"] == "oversteer" for i in ru["issues"]), "and no oversteer is invented")
check(all(i["sureness"] in hd.SURE_WORDS for i in ru["issues"]), "sureness is on the app's own scale")
rus = hd.analyze(laps_with(2, 14, extra=mids(40.0)), info=INFO)
check(any(i["sureness"] in ("likely", "sure") for i in rus["setup_issues"]), "14 laps, 100 %: Likely or Sure")
# oversteer is the mirror image
ro = hd.analyze(laps_with(3, 10, extra=mids(-40.0)), info=INFO)
check(any(i["kind"] == "oversteer" and i["phase"] == "mid" and i["verdict"] == "setup" for i in ro["issues"]),
      f"mid-corner oversteer found ({ids(ro)})")
check(not any(i["kind"] == "understeer" for i in ro["issues"]), "and no understeer is invented")

# ---------------------------------------------------------------- 3. instability on the way in
def wobble(x, i, d):
    t = np.arange(len(d)) / RATE
    for A, _v, s in CORNERS:
        m = (d > A - 120) & (d < A - 30)
        x["sw"] = np.asarray(x["sw"], float)
        x["r"] = np.asarray(x["r"], float)
        x["sw"][m] += 9.0 * np.sin(2 * np.pi * 2.2 * t[m]) * s
        x["r"][m] += 0.18 * np.sin(2 * np.pi * 2.2 * t[m]) * s


ri = hd.analyze(laps_with(4, 10, post=wobble), info=INFO)
ii = find(ri, "instability", "entry")
check(ii is not None and ii["verdict"] == "setup", f"instability on the way in is a setup issue ({ids(ri)})")

# ---------------------------------------------------------------- 4. exit traction loss
def spin(x, i, d):
    x["r"] = np.asarray(x["r"], float)
    for A, _v, s in CORNERS:
        m = (d > A + 30) & (d < A + 100)
        x["r"][m] += 0.2 * s


rt = hd.analyze(laps_with(5, 10, post=spin), info=INFO)
ti = [i for i in rt["setup_issues"] if i["kind"] == "traction"]
check(bool(ti), f"traction loss on the way out is a setup issue ({ids(rt)})")
# speed not rising while the throttle is down, with wheel speeds in the file
def stall(x, i, d):
    x["v"] = np.asarray(x["v"], float)
    for A, _v, s in CORNERS:
        m = (d > A + 20) & (d < A + 110)
        if i % 5:                                       # 8 of 10 laps; the 2 that hook up show what the car can do
            x["v"][m] = x["v"][m][0]


rs = hd.analyze(laps_with(6, 10, post=stall), info=INFO)
check(any(i["kind"] == "traction" for i in rs["issues"]), f"speed not rising with the throttle down is traction ({ids(rs)})")
def wheelspin(x, i, d):
    n = len(d)
    x["wsf"] = np.asarray(x["v"], float).copy()
    x["wsr"] = x["wsf"].copy()
    for A, _v, s in CORNERS:
        m = (d > A + 30) & (d < A + 100)
        x["wsr"][m] *= 1.15


rw = hd.analyze(laps_with(7, 10, post=wheelspin), info=INFO)
check(rw["channels"]["wheel_speeds"] is True and any(i["kind"] == "traction" and i["verdict"] == "setup" for i in rw["issues"]),
      f"rear wheels spinning faster than the fronts is traction ({ids(rw)})")

# ---------------------------------------------------------------- 5. driving, not setup
rh = hd.analyze(laps_with(8, 12, extra=mids(40.0), every=2), info=INFO)       # on half the laps
check(not rh["setup_issues"], f"trouble on half the laps only is not called the setup ({ids(rh)})")
dh = [i for i in rh["driving_issues"] if i["kind"] == "understeer"]
check(dh and all(i["why_driving"] == "inconsistent" and i["sureness"] == "hint" for i in dh),
      f"it is called driving, inconsistent lap to lap, at A hint ({[(i['why_driving'], i['sureness']) for i in dh]})")


def t3_only(d, kap, ay):
    out = np.zeros_like(d)
    m = np.abs(d - CORNERS[2][0]) < 18
    out[m] += np.sign(kap[m]) * 40.0
    return out


r1 = hd.analyze(laps_with(9, 10, extra=t3_only), info=INFO)
d1 = [i for i in r1["issues"] if i["kind"] == "understeer"]
check(d1 and all(i["verdict"] == "driving" and i["why_driving"] in ("one_corner", "few_corners") for i in d1),
      f"one corner only: driving ({[(i['verdict'], i['why_driving']) for i in d1]})")
check(any("corner coach" in i["text"] for i in d1), "and it points to the corner coach")

# ---------------------------------------------------------------- 6. laps that must not count
bad = laps_with(10, 6, extra=mids(40.0))
for k, x in enumerate(bad):
    x["traffic" if k < 3 else "off_track"] = True
good = laps_with(11, 8)
rx = hd.analyze(good + bad, info=INFO)
check(rx["laps"] == 8 and rx["left_out"] == {"traffic": 3, "off track": 3}, f"traffic and off-track laps left out ({rx['left_out']})")
check(not rx["issues"], f"and their understeer did not count ({ids(rx)})")
race = laps_with(12, 8)
race[0]["session"], race[0]["lap"] = "Race", 1
check(hd.analyze(race, info=INFO)["left_out"].get("first lap of the race") == 1, "lap 1 of a race left out")

# ---------------------------------------------------------------- 7. not enough laps
rn = hd.analyze(laps_with(13, 4), info=INFO)
check(not rn["ok"] and "Not enough clean laps yet: 4 of 6" in rn["message"], f"too few laps says why ({rn['message']})")
check(hd.analyze([], info=INFO)["message"].startswith("Not enough clean laps yet: 0 of 6"), "no laps says why")
check(not hd.analyze(None)["ok"] and not hd.analyze([{"x": 1}])["ok"], "bad input does not raise")

# ---------------------------------------------------------------- 8. the advice
SETUP = {"Chassis": {"Front": {"ArbSetting": "3", "SpringRate": "180 N/mm", "RideHeight": "50.0 mm"},
                     "Rear": {"ArbSetting": "3", "SpringRate": "160 N/mm", "RideHeight": "55.0 mm"},
                     "FrontBrakes": {"BrakePressureBias": "54.0%"}}}
adv = sa.advise(ru, SETUP)
top = adv["top"]
check(adv["ok"] and top and top["change"] and top["rank"] == 1 and top["first"], f"the top item is one ranked change ({adv['card']})")
check("anti-roll bar" in top["change"]["text"] and "Expect" in top["text"] and "watch for" in top["text"],
      "it has a plain reason, what to expect and what to watch for")
check(adv["card"].startswith("Car balance: ") and "Try:" in adv["card"] and "(" in adv["card"], f"short card ({adv['card']})")
check(sum(1 for i in adv["items"] if i["rank"]) == len([i for i in adv["items"] if i["verdict"] == "setup" and (i["change"] or i["dial"])]),
      "each setup issue has at most one change, ranked")
# a car without bars falls through to springs; without anything it says so
adv2 = sa.advise(ru, {"Chassis": {"Front": {"SpringRate": "180 N/mm"}, "Rear": {"SpringRate": "160 N/mm"}}})
check(adv2["top"]["change"] and "springs" in adv2["top"]["change"]["text"].lower(), "no bars: the next area is used")
adv3 = sa.advise(ru, {"Chassis": {"Front": {"Camber": "-3.0 deg"}}})
check(adv3["top"] is not None and adv3["top"]["change"] is None and "nothing Debrief can step" in adv3["top"]["text"],
      "nothing to change on this car: said plainly")
# never undoing his last change unless the laps showed it slower
bar = next(i for i in ru["setup_issues"] if i["kind"] == "understeer")
want = sa._option(sa.ibt.flatten(SETUP), "front_arb", -1)
last = {"from": "Setup A", "to": "Setup B", "slower": False, "verdict": "no clear difference",
        "changes": [{"key": want["key"], "keys": [want["key"]], "old": want["to"], "new": want["from"], "text": "Front anti-roll bar stiffer"}]}
adv4 = sa.advise(ru, SETUP, last_change=last)
check(not (adv4["top"]["change"] and adv4["top"]["change"]["key"] == want["key"] and adv4["top"]["change"]["to"] == want["to"]),
      "it does not simply undo his last change")
adv5 = sa.advise(ru, SETUP, last_change=dict(last, slower=True))
check(adv5["top"]["change"] and adv5["top"]["change"]["to"] == want["to"] and "reasonable" in adv5["top"]["text"],
      "unless the laps showed that change slower")
# two issues never pull the same part opposite ways
two = {"ok": True, "laps": 10, "issues": [
    {"id": "understeer:mid:slow", "kind": "understeer", "phase": "mid", "class": "slow", "title": "Mid-corner understeer in slow corners",
     "verdict": "setup", "why_driving": None, "corners": ["T1", "T3"], "laps_showing": 9, "laps": 10, "sureness": "likely", "share": 0.9, "text": ""},
    {"id": "oversteer:entry:any", "kind": "oversteer", "phase": "entry", "class": None, "title": "Oversteer on the way in",
     "verdict": "setup", "why_driving": None, "corners": ["T2", "T4"], "laps_showing": 7, "laps": 10, "sureness": "hint", "share": 0.7, "text": ""}]}
adv6 = sa.advise(two, {"Chassis": {"Front": {"ArbSetting": "3"}}})
moves = [(i["change"]["key"], i["change"]["way"]) for i in adv6["items"] if i["change"]]
check(len(moves) == 1 and moves[0][1] == -1, f"only the stronger idea moves the front bar ({moves})")
check(any("pull against" in i["text"] for i in adv6["items"] if not i["change"]), "the other says why it waits")
# driving issues get no setup change
advd = sa.advise(rh, SETUP)
check(advd["top"]["verdict"] == "driving" and advd["top"]["change"] is None and "corner coach" in advd["card"]
      and advd["empty"], "driving only: no setup change, points to the corner coach")
# empty states say why
check("Not enough clean laps yet: 4 of 6" in sa.advise(rn, SETUP)["empty"], "empty advice says why (too few laps)")
check("balanced" in sa.advise(r0, SETUP)["empty"], "empty advice says why (nothing wrong)")

# ---------------------------------------------------------------- 9. fixed-setup series
fx_i = sa.advise(ri, SETUP, series_setup="fixed", dials=["bias"])
check(fx_i["top"]["dial"] and fx_i["top"]["dial"]["target"] == "bias" and fx_i["top"]["change"] is None,
      f"fixed series: the brake bias dial ({fx_i['card']})")
fx_n = sa.advise(ri, {"Chassis": {"Front": {"Camber": "-3.0 deg"}}}, series_setup="fixed")
check(fx_n["top"]["dial"] is None and fx_n["top"]["driving"] and fx_n["top"]["change"] is None,
      "fixed series, no dial on the car: driving advice only")
fx_u = sa.advise(ru, SETUP, series_setup="fixed", dials=["bias"])
check(all(i["change"] is None for i in fx_u["items"]), "fixed series: never a garage change (bars, springs)")

# ---------------------------------------------------------------- 10. feel, history
check(sa.adjust_sureness("guess", "agree")[0] == "hint" and sa.adjust_sureness("hint", "agree")[0] == "hint"
      and sa.adjust_sureness("likely", "agree")[0] == "likely",
      "agree: one step surer, never above A hint (2.23.2)")
check(sa.adjust_sureness("likely", "disagree")[0] == "hint" and sa.adjust_sureness("guess", "disagree")[0] == "guess",
      "not what I felt: one step less sure, never below A guess")
fa = sa.advise(two, {"Chassis": {"Front": {"ArbSetting": "3"}}}, feel={"understeer:mid:slow": {"answer": "disagree"}})
check(fa["items"][0]["sureness"] == "hint" and fa["items"][0]["feel"] == "disagree", "his answer lowers the sureness on the next read")
store = {}
k = sa.history_key("mclaren570sgt4", "limerock full")
check(sa.record_shown(store, k, top, "fpA", "2026-11-05 10:00") and store[k][0]["status"] == "shown", "a suggestion shown is kept")
check(not sa.record_shown(store, k, top, "fpA", "2026-11-05 10:05"), "the same suggestion on the same setup is kept once")
flat_b = sa.ibt.flatten(SETUP)
flat_b[top["change"]["key"]] = top["change"]["to"]
check(sa.evaluate(store, k, "fpB", "fpA", flat_b, {"verdict": "faster", "slower": False, "changes": [1]}, []) and
      store[k][0]["status"] == "followed" and store[k][0]["helped"] and "no longer shows" in store[k][0]["result"],
      f"he did it and the issue is gone ({store[k][0].get('result')})")
store2 = {}
sa.record_shown(store2, k, top, "fpA", "t")
sa.evaluate(store2, k, "fpB", "fpA", sa.ibt.flatten(SETUP), {"verdict": "no clear difference", "slower": False, "changes": [1]}, [])
check(store2[k][0]["status"] == "did something else", "he changed something else: said so")
store3 = {}
sa.record_shown(store3, k, top, "fpA", "t")
sa.evaluate(store3, k, "fpB", "fpA", flat_b, {"verdict": "faster", "slower": True, "changes": [1]}, [top["issue_id"]])
check("slower" in store3[k][0]["result"] and "still shows" in store3[k][0]["result"] and not store3[k][0]["helped"],
      "slower laps and the issue still there: not helped (Setup Lab's own verdict)")

# ---------------------------------------------------------------- 11. house rules
blob = json.dumps([ru, adv, adv6, advd, rn], ensure_ascii=False, default=str)
check("—" not in blob and "–" not in blob, "no long dashes in anything it says")
check(all(c in hd.SURE_WORDS for c in ("sure", "likely", "hint", "guess")), "the scale is the app's")
old = bl.analyze_laps(session(1, n_laps=8), info=INFO)
check(old["ok"], "the balance map still works with the new wheel-speed plumbing")

print(f"\n{'ALL OK' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
