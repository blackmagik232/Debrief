"""2.15: setup share text (debrief/setupshare.py). Run: python tests/test_215_share.py

Setup Lab reports are made from fake .ibt files (tests/fake_ibt.py) with known setup changes, and from hand-made
compare verdicts for each kind of result. Checks: the text names the car, track, the changed settings and Setup
Lab's own verdict; never a setup file name, driver name, file name or folder; at most 1,500 characters; no long
dashes; a markdown variant. No network, temp folders only."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-215-share-"))

import fake_ibt as fi  # noqa: E402
from debrief import setuplab as sl, setupshare as ss  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def private_free(text, where):
    bad = [w for w in ("Swazi", "Williams", "swazi_zolder", ".sto", ".ibt", str(TMP), "telemetry", "—", "–", "@", "#")
           if w in text]
    check(not bad, f"{where}: nothing private or odd in it ({bad})")


folder = TMP / "telemetry"
folder.mkdir()
A = fi.SETUP_GT4
B = copy.deepcopy(A)
B["Chassis"]["Rear"]["WingAngle"] = "3.5 deg"
B["Chassis"]["LeftFront"]["Camber"] = "-3.6 deg"
B["Chassis"]["RightFront"]["Camber"] = "-3.6 deg"
B["Chassis"]["LeftFront"]["CornerWeight"] = "3300 N"           # a garage result: never listed
lapsA = [99.30, 99.05, 99.20, 99.10, 99.25, 99.15, 99.20, 99.12]
lapsB = [99.22, 99.08, 99.18, 99.11, 99.21, 99.14, 99.19, 99.16, 99.10]
fi.make(folder / "mclaren570sgt4_zolder gp 2026-11-01 10-00-00.ibt", lapsA, setup=A, setup_name="swazi_zolder.sto",
        start_date=1_790_000_000)
fi.make(folder / "mclaren570sgt4_zolder gp 2026-11-01 11-00-00.ibt", lapsB, setup=B, setup_name="swazi_zolder_v2.sto",
        start_date=1_790_003_600)
rep = sl.report(folder, "mclaren570sgt4", "zolder gp")
check(len(rep["diffs"]) == 1, f"Setup Lab sees one change of setup ({len(rep['diffs'])})")
r = ss.share(rep)
t = r["text"]
print("     " + t)
check(r["ok"] and r["pair"] == ("A", "B"), "share made for B vs A (his latest change)")
check(t.startswith("McLaren 570S GT4 at Circuit Zolder"), f"car and track first ({t[:40]})")   # 2.18: names.track
check("Setup B vs A: " in t, "names the setups by letter")
check("rear wing 2.5 → 3.5 deg" in t, "rear wing change")
check("front camber -4.0 → -3.6 deg (both sides)" in t, "camber both sides as one item")
check("corner weight" not in t.lower(), "garage results left out")
pair = rep["compare"]["pairs"][0]
check("Result:" in t, f"result from Setup Lab's verdict ({pair['verdict']})")
if pair["verdict"] == "no clear difference" and pair.get("more_laps"):
    check(f"about {pair['more_laps']} more on each would show a 0.1 s gap" in t, "says how many more laps would tell")
check(r["chars"] <= 1500 and len(r["markdown"]) <= 1500, f"short enough for Discord ({r['chars']} chars)")
private_free(t, "text")
private_free(r["markdown"], "markdown")
check(r["markdown"].startswith("**McLaren 570S GT4** at **Circuit Zolder") and "\n- rear wing" in r["markdown"]
      and "**Result:**" in r["markdown"], "markdown variant")

# ---------------------------------------------------------------- verdict kinds (hand-made compare)
base = copy.deepcopy(rep)


def with_pair(**kw):
    rr = copy.deepcopy(base)
    p = {"a": "A", "b": "B", "verdict": "no clear difference", "faster": None, "diff_s": 0.03, "more_laps": 4,
         "caution": None, "confounders": [], "missing": []}
    p.update(kw)
    rr["compare"] = {"pairs": [p], "setups": [{"id": "A", "pace": {"clean_laps": 6}},
                                              {"id": "B", "pace": {"clean_laps": 7}}]}
    return rr


t1 = ss.share_text(with_pair())
check("Result: no clear difference yet (6 and 7 clean laps; about 4 more on each would show a 0.1 s gap)." in t1, t1)
t2 = ss.share_text(with_pair(verdict="faster", faster="B", diff_s=0.31))
check("Result: B was faster than A by about 0.3 s a lap (6 and 7 clean laps)." in t2, t2)
t3 = ss.share_text(with_pair(verdict="faster", faster="B", diff_s=0.31,
                             caution="Setup B was also driven later, so part of this may be you getting quicker."))
check("Caution: B was also driven later" in t3 and "going back to A" in t3, "driving-order caution carried over")
t4 = ss.share_text(with_pair(verdict="confounded", confounders=["the track was 9 °C warmer for Setup B (swazi.sto)"]))
check("can't be compared fairly yet" in t4 and "9 °C warmer" in t4, t4)
t5 = ss.share_text(with_pair(verdict="not enough to tell",
                             missing=["Setup B (swazi_zolder_v2.sto) needs 2 more clean laps (has 3; 5 is the minimum)"]))
check("not enough laps to tell yet" in t5 and "swazi" not in t5.lower(), f"missing laps, file name removed: {t5}")
t6 = ss.share_text(with_pair(more_laps=None, diff_s=0.04))
check("smaller than 0.1 s" in t6, t6)
# a setup named with brackets of its own: nothing of the name may leak (2.15 review)
for nm in ("Swazi Williams (v2) quali", "Overdriver's, changed", "a (b (c)) d"):
    rp = with_pair(verdict="confounded",
                   confounders=[f"the track was 6 °C warmer for Setup B ({nm}) than for Setup A ({nm})"])
    rp["setups"] = [{"id": "A", "label": f"Setup A ({nm})", "name": nm, "names": [nm], "files": []},
                    {"id": "B", "label": f"Setup B ({nm})", "name": nm, "names": [nm], "files": []}]
    tt = ss.share_text(rp)
    check("for Setup B than for Setup A)." in tt and "quali" not in tt and "Williams" not in tt and "v2" not in tt
          and "Overdriver" not in tt and "))" not in tt, f"name with brackets removed: {tt}")
    # without the report knowing the name, brackets inside brackets still go whole
    rp.pop("setups")
    tt = ss.share_text(rp)
    check("quali" not in tt and "Williams" not in tt and "))" not in tt and "Overdriver" not in tt, f"... unknown name: {tt}")
# a name used on its own in a reason (not in brackets) is removed too
rp = with_pair(verdict="confounded", confounders=["Swazi Williams (v2) quali was driven 6 °C warmer"])
rp["setups"] = [{"id": "B", "label": "Setup B (Swazi Williams (v2) quali)", "name": "Swazi Williams (v2) quali"}]
tt = ss.share_text(rp)
check("Swazi" not in tt and "quali" not in tt, f"a bare known name is removed: {tt}")
for x in (t1, t2, t3, t4, t5, t6):
    private_free(x, "verdict text")

# chosen pair the other way round
rv = ss.share(rep, "B", "A")
check(rv["ok"] and "Setup A vs B: " in rv["text"] and "rear wing 3.5 → 2.5 deg" in rv["text"], f"reverse pair flips the changes ({rv['text'][:90]})")

# many changes: listed up to a point, the rest counted, still short
many = copy.deepcopy(rep)
many["diffs"][0]["changes"] = [{"kind": "setting", "text": f"Setting number {i} {i} → {i + 1} clicks " + "x" * 120}
                               for i in range(40)]
rm = ss.share(many)
check(rm["chars"] <= 1500 and "more change" in rm["text"], f"40 changes: trimmed to {rm['chars']} chars")

# a sneaky value can't inject markdown or a mention
sneaky = copy.deepcopy(rep)
sneaky["car"] = "@everyone **Mclaren 570s GT4** <script>"
rs = ss.share(sneaky)
check("@" not in rs["text"] and "<" not in rs["text"] and "**" not in rs["text"].split(" at ")[0], f"cleaned: {rs['text'][:60]}")

# nothing to share
check(not ss.share({"diffs": []})["ok"] and "two different setups" in ss.share_text({}), "one setup: nothing to share")
check(not ss.share(None)["ok"], "bad input: no crash")
json.dumps(r)

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
