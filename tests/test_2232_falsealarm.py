"""2.23.2: Set-Up Lab's automatic advice only blames the setup when the evidence is strong.

Pure-noise laps (small yaw noise with bump blips, heavy steering noise, clean) must give at most 5 % false setup calls;
injected mid-corner understeer, exit traction loss and entry problems must still be reported correctly (entry as driving,
relative to the typical entry); a corner that swings is driving; and the 2.23.2 screen fixes (card titles, garage line,
why there are no laps, the felt-answer, open or fixed setup).
    python3 tests/test_2232_falsealarm.py
No network, temp folders only."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


_src = (ROOT / "tests" / "test_215_balance.py").read_text(encoding="utf-8")
NS = {"__name__": "balance_builder_2232", "__file__": str(ROOT / "tests" / "test_215_balance.py")}
exec(compile(_src[:_src.index("# ---------------------------------------------------------------- 0. the filter")],
             "test_215_balance.py", "exec"), NS)
from debrief import handling as hd, setupadvice as sa, balance as bl  # noqa: E402

make_lap, INFO, CORNERS, L, RATE = (NS[k] for k in ("make_lap", "INFO", "CORNERS", "L", "RATE"))
N = 12                                                   # sessions per scenario (seeded)


def noisy(sig):
    def f(rng, **kw):
        x = make_lap(rng, **kw)
        n = len(x["r"])
        x["r"] = np.asarray(x["r"], float) + sig * rng.standard_normal(n)
        for _ in range(6):                                # bump blips: 0.15 s bursts
            k = int(rng.integers(0, n - 10))
            x["r"][k:k + 9] += rng.choice([-1, 1]) * sig * 4
        return x
    return f


def rate(maker, nlaps, seed0, **kw):
    fa = 0
    for k in range(N):
        rng = np.random.default_rng(seed0 + k)
        laps = []
        for i in range(nlaps):
            x = maker(rng, **kw)
            x["lap"] = i + 2
            laps.append(x)
        if hd.analyze(laps, info=INFO)["setup_issues"]:
            fa += 1
    return fa / N


for name, mk, nl, kw in (("small yaw noise 0.02", noisy(0.02), 10, {}), ("yaw noise 0.05", noisy(0.05), 10, {}),
                         ("heavy steering noise, 14 laps", make_lap, 14, {"lap_noise": 3.0, "corner_noise": 4.0}),
                         ("heavy steering noise, 6 laps", make_lap, 6, {"lap_noise": 3.0, "corner_noise": 4.0}),
                         ("medium noise", make_lap, 10, {"lap_noise": 1.5, "corner_noise": 2.5}),
                         ("clean car", make_lap, 10, {})):
    r = rate(mk, nl, 4000 + len(name), **kw)
    check(r <= 0.05, f"{name}: false setup calls {r:.0%} (limit 5 %)")


def laps_with(seed, n, post=None, extra=None):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        x = make_lap(rng, extra=extra)
        x["lap"] = i + 2
        if post:
            post(x, i, np.asarray(x["pct"]) * L)
        out.append(x)
    return out


def mids(amount):
    def f(d, kap, ay):
        out = np.zeros_like(d)
        for A, _v, _s in CORNERS:
            m = np.abs(d - A) < 18
            out[m] += np.sign(kap[m]) * amount
        return out
    return f


# ---- real problems are still found
ru = hd.analyze(laps_with(2, 12, extra=mids(40.0)), info=INFO)
check(any(i["kind"] == "understeer" and i["phase"] == "mid" and i["verdict"] == "setup" for i in ru["setup_issues"]),
      "injected mid-corner understeer: a setup issue")


def spin(x, i, d):
    x["r"] = np.asarray(x["r"], float)
    for A, _v, s in CORNERS:
        m = (d > A + 30) & (d < A + 100)
        x["r"][m] += 0.2 * s


rt = hd.analyze(laps_with(5, 12, post=spin), info=INFO)
check(any(i["kind"] == "traction" for i in rt["setup_issues"]), "injected exit traction loss: reported as setup")


def entry_problem(d, kap, ay):                           # two corners' entries need 25 degrees more steering
    out = np.zeros_like(d)
    for A, _v, _s in (CORNERS[1], CORNERS[3]):
        m = (d > A - 70) & (d < A - 18)
        out[m] += np.sign(kap[m]) * 25.0
    return out


re_ = hd.analyze(laps_with(6, 12, extra=entry_problem), info=INFO)
check(not any(i["phase"] == "entry" and i["kind"] in ("understeer", "oversteer") and i["verdict"] == "setup"
              for i in re_["issues"]), "entry understeer/oversteer is never a setup verdict")
ent = [i for i in re_["issues"] if i["phase"] == "entry" and i["kind"] in ("understeer", "oversteer")]
check(bool(ent) and all(i["verdict"] == "driving" and "typical entry" in i["title"] for i in ent),
      f"entry is driving advice, 'relative to your typical entry' ({[i['title'] for i in ent]})")


# ---- a corner that swings is driving
def swing(x, i, d):
    x["sw"] = np.asarray(x["sw"], float)
    A = CORNERS[2][0]
    m = np.abs(d - A) < 18
    x["sw"][m] += (1 if i % 2 else -1) * 40.0 * np.sign(np.asarray(x["r"])[m])


rs = hd.analyze(laps_with(8, 12, post=swing), info=INFO)
check(not any("T3" in i["corners"] for i in rs["setup_issues"]), "a corner that swings is not a setup issue")

# ---- the map says neutral / feel cannot lift a hint
check(sa.adjust_sureness("hint", "agree")[0] == "hint" and sa.adjust_sureness("guess", "agree")[0] == "hint",
      "feel 'agree' never raises a read above A hint")
check(hd._sureness({"verdict": "setup", "map_neutral": True, "laps": 14, "laps_showing": 14,
                    "strong": [1, 2, 3], "frac": {1: 1, 2: 1, 3: 1}}) == "hint", "map says neutral: capped at A hint")
check(hd._wilson_lb(9, 10) < 0.8 and hd._binom_p(10, 10, 0.2) < 1e-6, "interval and binomial helpers")

# ---- the screen fixes
SETUP = {"Chassis": {"Front": {"ArbSetting": "3", "SpringRate": "180 N/mm"}, "Rear": {"ArbSetting": "3"}}}
adv = sa.advise(ru, SETUP)
check(adv["top"] and adv["top"]["change"], "a setup card proposes a change")
js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
check("'Next change: '+i.change.label" in js and "i.alternative?'Next change: '" in js and "'No setup change'" in js,
      "card title says 'Next change: ...' when a change is proposed, 'No setup change' only when there is none")
check("anyChange" in js and "these are driving points" in js,
      "the garage line shows only above setup cards, never above driving-only hints")
rl = hd.analyze([{"time": 90.0, "lap": i, "v": [1.0] * 200, "sw": [0.0] * 200, "ay": [0.0] * 200} for i in range(52)],
                info=INFO)
check("52 laps, but none has yaw-rate data" in rl["message"], f"no yaw channel is named ({rl['message'][:70]})")
ro = hd.analyze([{"time": 90.0 + i, "lap": i, "off_track": True} for i in range(5)], info=INFO)
check("all were left out" in ro["message"], f"all filtered laps are named ({ro['message'][:70]})")
check("What did you feel?" in js and "no grip on exit" in js and "'Open setup? '" in js,
      "felt choices and the Open/Fixed question are on the screen")
check(set(sa.FELT) == {"understeer", "oversteer", "nervous entry", "no grip on exit", "fine"}, "five felt choices")
fx = sa.advise(ru, SETUP, series_setup="fixed", dials=["bias"])
check(all(i["change"] is None for i in fx["items"]) and fx["items"], "switching to Fixed: no garage change, dials or driving only")

# ---- the diff ramp respects the undo guard
o = {"target": "ramp", "key": "Rear/DiffRamp", "keys": ["Rear/DiffRamp"], "manual": True}
check(sa._touched(o, {"changes": [{"key": "Rear/DiffRamp"}]}) and not sa._touched(o, {"changes": [{"key": "Front/X"}]}),
      "a ramp he just changed counts as the change a suggestion would undo")

print("\nFAILED" if fails else "\nall ok", fails or "")
sys.exit(1 if fails else 0)
