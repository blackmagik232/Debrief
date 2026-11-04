"""2.18 Setup Lab trust (lane D): the share text uses the page's numbers in the label's order; no two setup ideas
move one setting opposite ways, and an idea never simply undoes his last change unless the laps showed it slower; a
fixed-setup series keeps the fuel reading and points the balance map at in-car dials or the driving, and a car known
to race fixed setups isn't asked about; "Now" hot pressures come from the same runs as the pressure table; every pair
of setups with enough laps is compared; one lap-time format; setups sharing a file name are told apart; the balance
map says why its lap count isn't the table's. Run: python tests/test_218_setuplab_trust.py"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-218-setuplab-"))

import fake_ibt as fi  # noqa: E402
from debrief import balance as bl, pressurelearn as pl, setuplab as sl, setupshare as ss  # noqa: E402
from debrief.desktop import carsetup as cs  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def folder(name):
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def mk(d, stamp, laps, car="mclaren570sgt4", track="zolder gp", **kw):
    return fi.make(d / f"{car}_{track} {stamp}.ibt", laps, car_path=car, track_code=track, **kw)


def with_arb(v):
    s = copy.deepcopy(fi.SETUP_GT4)
    s["Chassis"]["FrontBrakes"]["ArbSetting"] = str(v)
    return s


SLOW = [99.5, 99.6, 99.4, 99.5, 99.55, 99.45, 99.5, 99.6]
FAST = [99.0, 99.1, 98.9, 99.0, 99.05, 98.95, 99.0, 99.1]

# ---------------------------------------------------------------- lap format, names
check(sl.lap_text(60.029) == "1:00.029" and sl.lap_text(59.748) == "0:59.748" and sl.lap_text(95.849) == "1:35.849",
      "one lap format, as the tables show it")
check(sl.lap_text(None) == "-" and sl.day_text("2026-10-20 18:00") == "Oct 20", "lap and day helpers")

d = folder("three")
mk(d, "2026-10-20 18-00-00", SLOW, setup=with_arb(3), setup_name="lime.sto")
mk(d, "2026-10-24 18-00-00", SLOW, setup=with_arb(4), setup_name="lime.sto")
mk(d, "2026-11-01 18-00-00", FAST, setup=with_arb(2), setup_name="lime.sto")
mk(d, "2026-11-01 20-00-00", FAST, setup=with_arb(2), setup_name="lime.sto")
rep = sl.report(d, "mclaren570sgt4", "zolder gp")
labels = [s["label"] for s in rep["setups"]]
check(labels == ["Setup A (lime.sto, first driven Oct 20)", "Setup B (lime.sto, first driven Oct 24)",
                 "Setup C (lime.sto, first driven Nov 1)"], f"setups sharing a file name are told apart ({labels})")
d2 = folder("sameday")
mk(d2, "2026-10-20 18-00-00", SLOW, setup=with_arb(3), setup_name="x.sto")
mk(d2, "2026-10-20 20-00-00", FAST, setup=with_arb(2), setup_name="x.sto")
lb = [s["label"] for s in sl.report(d2, "mclaren570sgt4", "zolder gp")["setups"]]
check(lb == ["Setup A (x.sto, first driven Oct 20, 18:00)", "Setup B (x.sto, first driven Oct 20, 20:00)"],
      f"same day: the time too ({lb})")

# ---------------------------------------------------------------- every pair compared
C = rep["compare"]
pairs = [(p["a"], p["b"]) for p in C["pairs"]]
check(pairs == [("A", "B"), ("B", "C"), ("A", "C")], f"A vs C is compared too, after the consecutive pairs ({pairs})")
check(all(":" in p["text"].split("typical")[1][:20] for p in C["pairs"] if "typical" in p["text"]),
      "pair texts give laps as m:ss.sss")
faster = [p for p in C["pairs"] if p["verdict"] == "faster"]
check(faster and all(p["faster"] == "C" for p in faster) and C["headline"].endswith("Each comparison is below.")
      and not any(p["text"] in C["headline"] for p in C["pairs"]), f"a headline that never repeats a pair ({C['headline']})")
d3 = folder("few")
mk(d3, "2026-10-20 18-00-00", SLOW, setup=with_arb(3))
mk(d3, "2026-10-21 18-00-00", [99.5, 99.4, 99.6], setup=with_arb(4))
mk(d3, "2026-10-22 18-00-00", FAST, setup=with_arb(2))
C3 = sl.compare(sl.index(d3)["files"])
check([(p["a"], p["b"]) for p in C3["pairs"]] == [("A", "B"), ("B", "C"), ("A", "C")] and C3["not_compared"]
      and C3["not_compared"][0].startswith("Setup B") and "isn't compared with the other setups yet" in C3["not_compared"][0],
      f"a setup without enough laps: says why it isn't compared with the rest ({C3['not_compared']})")
two = sl.compare(sl.index(d2)["files"])
check(two["headline"] == two["pairs"][0]["text"] and two["not_compared"] == [], "one pair: its own text is the headline")

# ---------------------------------------------------------------- his change into a setup
ch = sl.change_into(sl.index(d)["files"], "C")
check(ch and ch["from"] == "Setup B" and ch["to"] == "Setup C" and ch["changes"][0]["old"] == "4"
      and ch["changes"][0]["new"] == "2", f"the change into Setup C ({ch and ch['changes']})")
check(ch["slower"] is False and ch["verdict"] in ("faster", "no clear difference"), "C was not slower than B")
check(sl.change_into(sl.index(d)["files"], "A") is None, "the first setup came from no change")

# ---------------------------------------------------------------- setup ideas: conflicts and undoing
def F(phase, label, conf="clear", corner="T9", cls="slow", p=0.001):
    return {"corner": corner, "corner_index": int(corner[1:]), "phase": phase, "label": label, "class": cls,
            "confidence": conf, "text": f"{corner} {phase}", "p_family": p, "median_deg": 12.0}


setup = {"Chassis": {"FrontBrakes": {"ArbSetting": "2"}, "Rear": {"ArbBlades": "1"}}}
two_ways = {"findings": [F("mid", "push", "clear", "T3", p=0.004), F("entry", "rotate", "clear", "T5", p=0.002)]}
I = bl.setup_ideas(two_ways, setup)
moves = {}
for i in I:
    if i["change"]:
        moves.setdefault(i["change"]["key"], set()).add(i["change"]["way"])
check(all(len(w) == 1 for w in moves.values()), f"never two ideas moving one part opposite ways ({moves})")
mid = next(i for i in I if i["phase"] == "mid")
ent = next(i for i in I if i["phase"] == "entry")
check(ent["change"]["text"].startswith("Front anti-roll bar 2 → 3") and mid["change"]["text"].startswith("Rear anti-roll bar 1 → 2")
      and mid["conflict"] and "T5" in mid["conflict"]["corner"] and "stronger evidence" in mid["text"],
      f"the stronger finding keeps the front bar; the other takes its next option and says why ({mid['text'][:200]})")
tie = bl.setup_ideas({"findings": [F("mid", "push", "clear", "T3", p=0.002), F("entry", "rotate", "clear", "T5", p=0.002)]},
                     {"Chassis": {"FrontBrakes": {"ArbSetting": "2"}}})
loser = next(i for i in tie if i["conflict"])
check(sum(1 for i in tie if i["change"]) == 1 and loser["change"] is None and "evidence about as strong" in loser["text"]
      and "one would undo the other" in loser["text"], f"equal evidence, no other option: says they conflict ({loser['text']})")
last = {"from": "Setup B", "to": "Setup C", "slower": False,
        "changes": [{"key": "Chassis.FrontBrakes.ArbSetting", "keys": ["Chassis.FrontBrakes.ArbSetting"], "old": "3",
                     "new": "2", "text": "Front anti-roll bar 3 → 2"}]}
U = bl.setup_ideas(two_ways, setup, last_change=last)
ue = next(i for i in U if i["phase"] == "entry")
check(ue["change"] is None and ue["undo"] and "would undo the change you made from Setup B to Setup C" in ue["text"]
      and "don't show that change made the car slower" in ue["text"], f"no plain undo of his last change ({ue['text'][:220]})")
um = next(i for i in U if i["phase"] == "mid")
check(um["change"]["text"].startswith("Front anti-roll bar 2 → 1") and not um["conflict"], "...so the other idea is free to use the front bar")
U2 = bl.setup_ideas(two_ways, setup, last_change=dict(last, slower=True))
check(any(i["change"] and i["change"]["text"].startswith("Front anti-roll bar 2 → 3") and "going back is reasonable" in i["text"]
          for i in U2), "the laps showed the change slower: going back is offered, and says why")
check(bl.setup_ideas(two_ways, setup, last_change={"from": "A", "to": "B", "slower": False, "changes": []})[0]["change"],
      "an unrelated last change blocks nothing")

# ---------------------------------------------------------------- fixed series: in-car dials, driving advice
fx = bl.setup_ideas({"findings": [F("entry", "push", corner="T2"), F("exit", "rotate", corner="T4"),
                                  F("mid", "rotate", corner="T6")]}, {}, series_setup="fixed", dials=["bias", "tc"])
by = {i["phase"]: i for i in fx}
check(all(i["change"] is None for i in fx), "fixed: never a garage change")
check(by["entry"]["dial"]["target"] == "bias" and by["entry"]["dial"]["way"] == -1 and "to the rear" in by["entry"]["text"],
      "push on the way in: brake bias to the rear")
check(by["exit"]["dial"]["target"] == "tc" and "traction control up one step" in by["exit"]["text"]
      and "other way round" in by["exit"]["text"], "rotation on the way out: traction control, with its caveat")
check(by["mid"]["dial"] is None and by["mid"]["driving"] and "adapt the driving" in by["mid"]["text"],
      "mid-corner: no sound dial, so driving advice")
nod = bl.setup_ideas({"findings": [F("entry", "push")]}, {}, series_setup="fixed")
check(nod[0]["dial"] is None and "brake a touch earlier" in nod[0]["text"], "no brake bias dial: driving advice")
bys = bl.setup_ideas({"findings": [F("entry", "push")]}, {"Chassis": {"InCarDials": {"BrakePressureBias": "52.0%"}}},
                     series_setup="fixed")
check(bys[0]["dial"] and bys[0]["dial"]["target"] == "bias", "a brake bias in the setup's in-car dials counts as a dial")
check(bl.setup_ideas({"findings": []}, {}, series_setup="fixed") == [], "fixed, nothing found: no ideas")
blob = json.dumps(fx + I + U, ensure_ascii=False)
check("–" not in blob and "—" not in blob, "no long dashes in the ideas")

# ---------------------------------------------------------------- fixed series: fuel kept, known cars, words
df = folder("fixedfuel")
mk(df, "2026-11-01 18-00-00", SLOW, fuel=(40.0, 2.5))
mk(df, "2026-11-01 19-00-00", SLOW, fuel=(40.0, 2.5))
rf = sl.report(df, "mclaren570sgt4", "zolder gp", series_setup="fixed")
rules = [i["rule"] for i in rf["suggestions"]["items"]]
check(rules[0] == "fixed_series" and "fuel" in rules, f"fixed: the fuel reading stays ({rules})")
ft = rf["suggestions"]["items"][0]["text"]
check("tyre pressures included" in ft and "usually tyre pressures" not in ft and "brake bias" in ft and "fuel" in ft,
      "fixed: pressures are locked too; fuel and the in-car dials are his")
check(sl.known_fixed("mx5 mx52016") and sl.known_fixed("", "Global Mazda MX-5 Cup") and sl.known_fixed("toyotagr86")
      and not sl.known_fixed("mclaren570sgt4", "McLaren 570S GT4"), "cars known to race fixed setups")
dm = folder("mx5")
mk(dm, "2026-11-01 18-00-00", SLOW, car="mx5 mx52016", track="lagunaseca", fuel=(40.0, 2.5))
rm = sl.report(dm, "mx5 mx52016", "lagunaseca")
check(rm["series_setup"] == "fixed" and "MX-5 Cup" in rm["series_setup_source"]
      and rm["series_setup_source"].startswith("Debrief's list"), f"MX-5 Cup: fixed without asking ({rm['series_setup_source']})")
check(sl.report(dm, "mx5 mx52016", "lagunaseca", series_setup="open")["series_setup"] == "open", "his own choice still wins")
dt = folder("official")
mk(dt, "2026-11-01 18-00-00", SLOW, fixed_setup=True, official=True)
src = sl.report(dt, "mclaren570sgt4", "zolder gp")["series_setup_source"]
check(src.startswith("the telemetry (") and "says" not in src, f"the source reads after 'From ' ({src})")

# ---------------------------------------------------------------- hot pressures: the same runs as the table
dh = folder("hot")
mk(dh, "2026-11-01 18-00-00", [99.5, 99.6, 99.4], pressure={"LF": 190, "RF": 190, "LR": 190, "RR": 190})
rh = sl.report(dh, "mclaren570sgt4", "zolder gp")
pr = pl.learn(sl.index(dh), "mclaren570sgt4", "zolder gp")
table_hot = [g["hot"] for b in pr["bands"] for g in b["groups"]]
check(not any(i["rule"] == "hot_pressure" for i in rh["suggestions"]["items"]) and not any(table_hot),
      f"a stint too short for the table is not shown as 'Now' either ({table_hot})")
mk(dh, "2026-11-01 19-00-00", SLOW, pressure={"LF": 191, "RF": 189, "LR": 187, "RR": 185})
rh = sl.report(dh, "mclaren570sgt4", "zolder gp")
hp = next((i for i in rh["suggestions"]["items"] if i["rule"] == "hot_pressure"), None)
check(hp and hp["values"] == {"LF": 191.0, "RF": 189.0, "LR": 187.0, "RR": 185.0}, f"a full run: Now from it ({hp and hp['values']})")
check(pl.MIN_RUN_LAPS == sl.HOT_RUN_LAPS == 3, "one definition of a run")
pr = pl.learn(sl.index(dh), "mclaren570sgt4", "zolder gp")
check(any("typical lap 1:39." in t for t in pr["text"]) and not any(" s. Not enough" in t for t in pr["text"]),
      f"pressure texts use the table's lap format ({pr['text'][:1]})")
check(not any("shuffle test" in t for t in pr["text"]), "no bare 'shuffle test' in the pressure texts")

# ---------------------------------------------------------------- share: the page's numbers, the label's order
r3 = sl.report(d, "mclaren570sgt4", "zolder gp")
sh = ss.share(r3)
t = sh["text"]
pace = {s["id"]: s["pace"] for s in r3["compare"]["setups"]}
check(sh["pair"] == ("B", "C") and "Setup C vs B" in t, "share: his latest change")
check(f"{pace['C']['clean_laps']} clean laps on C, {pace['B']['clean_laps']} on B" in t, f"lap counts in the label's order ({t})")
check(f"{sl.lap_text(pace['C']['median'])} on C, {sl.lap_text(pace['B']['median'])} on B" in t
      and sl.lap_text(pace["C"]["median"]) in r3["compare"]["verdict"], "the same typical laps as the page")
p = next(x for x in r3["compare"]["pairs"] if {x["a"], x["b"]} == {"B", "C"})
if p["verdict"] == "faster":
    check(f"by about {sl._fmt_s(abs(p['diff_s']))} s a lap" in t and f"by about {sl._fmt_s(abs(p['diff_s']))} s a lap" in p["text"],
          "the same gap as the page")
check("lime.sto" not in t and "first driven" not in t, "no file names (nor the new day suffix)")

# ---------------------------------------------------------------- balance lap counts
check(cs.balance_laps_note(17, 17, "Setup C", 2, 2) is None, "same count: nothing to explain")
n1 = cs.balance_laps_note(20, 17, "Setup C", 2, 2)
check(n1.startswith("Which was faster counts 17 of these laps for Setup C") and "warm-up laps" in n1, n1)
n2 = cs.balance_laps_note(80, 90, "Setup A", 20, 25)
check("newest 20 of 25 files" in n2, n2)
n3 = cs.balance_laps_note(15, 17, "Setup C", 2, 2)
check("7% off your typical lap" in n3, n3)

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
