"""2.13: session review from .ibt files (debrief/sessionibt.py). Run: python tests/test_213_session.py"""
from __future__ import annotations

import re
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-213-session-"))

import fake_ibt as fi  # noqa: E402
from debrief import sessionibt as si  # noqa: E402
from debrief import setuplab as sl  # noqa: E402

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
    """A fake file named like iRacing's: '<carpath>_<trackcode> YYYY-MM-DD HH-MM-SS.ibt'."""
    return fi.make(d / f"{car}_{track} {stamp}.ibt", laps, car_path=car, track_code=track, **kw)


def by_lap(rv, lap, session=None):
    return next(r for r in rv["laps"] if r["lap"] == lap and (session is None or r["session"] == session))


# ---------------------------------------------------------------- grouping files into sessions
g = folder("group")
mk(g, "2026-10-28 17-00-00", [99.5, 99.2])
mk(g, "2026-10-28 17-30-00", [99.4, 99.1])                         # same evening: same session
mk(g, "2026-10-28 21-00-00", [99.3])                               # > 3 h after the last one ended: new session
mk(g, "2026-10-28 21-10-00", [90.0], car="mx5 mx52016", track="oulton fosters")   # another car: new session
mk(g, "2026-10-28 21-20-00", [99.0])                               # back in the McLaren: not the 21-00 session
mk(g, "2026-10-29 10-00-00", [99.0], subsession=111)
mk(g, "2026-10-29 10-10-00", [99.0], subsession=222)               # same car, minutes later, another iRacing session
mk(g, "2026-10-29 10-20-00", [99.0], subsession=222)
(g / "mclaren570sgt4_zolder gp 2026-10-29 10-30-00.ibt").write_bytes(b"MZ" + b"\0" * 400)   # not telemetry
ss = si.sessions(g)
check(len(ss) == 6, f"sessions found: {len(ss)} (gap, car change and SubSessionID rules)")
check(ss[0]["sort"] >= ss[-1]["sort"] and ss[0]["subsession"] == 222 and len(ss[0]["files"]) == 2,
      "newest first; files of one SubSessionID together")
check(ss[1]["subsession"] == 111 and len(ss[1]["files"]) == 1, "a different SubSessionID is a different session")
first = ss[-1]
check(len(first["files"]) == 2 and first["laps"] >= 4 and first["best"] == 99.1, f"first evening: 2 files, best {first['best']}")
check(all(Path(p).is_file() for s in ss for p in s["paths"]), "paths given when the folder is known")
check(ss[3]["car_path"] == "mx5 mx52016", "car change splits sessions")
idx = sl.index(g)
check(len(si.sessions(idx["files"])) == 5, "bare records (no SubSessionID): gap and car rules only")
check(si.sessions(TMP / "nope") == [], "missing folder: no sessions, no error")

# ---------------------------------------------------------------- lap reasons
d = folder("reasons")
p = mk(d, "2026-10-30 18-00-00", [99.5, 99.0, 99.1, 101.0, 99.2, 99.0, 99.1, 99.05, 110.0, 99.0, 99.2],
       offs=(3,), incidents=(4,), untimed=(5,), pits=(10,))
rv = si.review([p])
reasons = {r["lap"]: r["reasons"] for r in rv["laps"]}
check(reasons[1] == ["warmup"] and by_lap(rv, 1)["valid"] and not by_lap(rv, 1)["clean"], "first flying lap: warm-up, valid")
check(reasons[2] == [] and by_lap(rv, 2)["clean"], "a normal lap is clean")
check(reasons[4] == ["off"], "off-track lap")
check(reasons[5] == ["incident"] and "+2x" in by_lap(rv, 5)["reason"], "incident lap, with the count")
check(by_lap(rv, 6)["timed_by"] == "samples" and by_lap(rv, 6)["clean"], "untimed lap timed from samples, still clean")
check(reasons[9] == ["slow"], "a 110 s lap among 99 s laps is slow")
check("pit" in reasons[11], "pit lap")
check(by_lap(rv, 0)["reasons"] == ["untimed"], "file starts mid-lap: untimed")
check(rv["summary"]["best"] == 99.0 and rv["summary"]["valid_laps"] == 8, f"best and valid laps {rv['summary']['valid_laps']}")
check(all(r["reason"] is None for r in rv["laps"] if r["clean"]) and all(r["reason"] for r in rv["laps"] if not r["clean"]),
      "every lap that isn't clean says why")
r2 = si.review([mk(folder("race"), "2026-10-30 19-00-00", [104.0, 99.5, 99.1], session="Race")])
check(by_lap(r2, 1)["reasons"] == ["start"], "race lap 1 is a start lap")
check(json.loads(json.dumps(rv)) is not None, "JSON-ready")

# ---------------------------------------------------------------- consistency
d = folder("cons")
p = mk(d, "2026-10-30 20-00-00", [100.0, 99.0, 99.2, 99.4, 99.6, 99.8, 100.2])
rv = si.review([p])
c = rv["consistency"]
check(rv["summary"]["clean_laps"] == 6 and c["enough"], "6 clean laps (warm-up left out)")
check(abs(c["median"] - 99.5) < 0.01 and abs(c["mad"] - 0.3) < 0.01, f"median {c['median']} and MAD {c['mad']}")
check(abs(c["iqr"] - 0.5) < 0.01, f"IQR {c['iqr']}")
check(c["near_best"] == 3 and c["near_best_pct"] == 50, f"laps within 0.5 s of best: {c['near_best']}")
check("3 of 6 clean laps within half a second of your best" in c["text"], c["text"])
few = si.review([mk(d, "2026-10-30 21-00-00", [100.0, 99.0, 99.2])])
check(not few["consistency"]["enough"] and "5 are needed" in few["consistency"]["text"], "too few laps: says so")

# ---------------------------------------------------------------- best possible lap (segments)


def pace(li, pct):
    if li == 1 and pct < 0.45:
        return 1.2                     # lap 2: quick in the first half of the lap
    if li == 3 and pct > 0.55:
        return 1.2                     # lap 4: quick in the second half
    return 1.0


d = folder("theo")
p = mk(d, "2026-10-31 18-00-00", [100.0] * 6, pace=pace)
rv = si.review([p])
th = rv["theoretical"]
check(th["basis"] == "corners" and th["segments"] == ["C1", "C2"], f"segments from corners: {th['segments']}")
check(th["enough"] and th["possible"] is not None and th["possible"] < 97.0, f"best possible {th['possible']}")
own = {b["segment"]: b["lap"] for b in th["best_segments"]}
check(own == {"C1": 2, "C2": 4}, f"different laps own different segments: {own}")
check(abs(th["gap_s"] - (100.0 - th["possible"])) < 0.01 and len(th["gains"]) == 2, "gap to the best lap and the gains")
check(all(abs(sum(r["segments"]) - r["time"]) < 0.01 for r in rv["laps"] if r.get("segments")), "segments add up to the lap")
sp = rv["segment_spread"]
check(sp["enough"] and sp["top"] in ("C1", "C2") and "Most of your lap-to-lap variation is in" in sp["text"], sp["text"])
check(re.search(r"variation is in Turn [12] \(C[12], ±", sp["text"]) is not None, f"spread says Turn N (CN): {sp['text']}")
check(re.search(r"Most of it is in Turn [12] \(C[12], [0-9.]+ s, best on lap \d+\)", th["text"]) is not None,
      f"best possible lap says Turn N (CN): {th['text']}")
check(si._seg_name("S2") == "S2" and si._seg_name("S2", "0.05 s") == "S2 (0.05 s)", "sectors keep their names")
gate = si.review([mk(d, "2026-10-31 19-00-00", [100.0] * 4, pace=pace)])
check(gate["theoretical"]["possible"] is None and not gate["theoretical"]["enough"]
      and "needs 5 valid laps" in gate["theoretical"]["text"], gate["theoretical"]["text"])
check(not gate["segment_spread"]["enough"], "segment spread gated too")


def flat(li, pct):
    return 40.0, 0.0                   # no corners at all


fl = si.review([mk(d, "2026-10-31 20-00-00", [100.0] * 6, corner=flat)])
check(fl["theoretical"]["basis"] == "equal thirds" and len(fl["theoretical"]["segments"]) == 3,
      f"no corners: fixed sectors ({fl['theoretical']['basis']})")

# ---------------------------------------------------------------- fuel
d = folder("fuel")
p = mk(d, "2026-11-01 18-00-00", [100.0] * 6, fuel=(60.0, 2.5), fuel_max=83,
       session=["Practice", "Lone Qualify", "Race"], session_secs=1800)
fu = si.review([p])["fuel"]
check(abs(fu["per_lap_l"] - 2.5) < 0.01 and fu["laps_measured"] == 6, f"fuel per lap {fu['per_lap_l']}")
check(fu["tank_l"] == 83 and abs(fu["laps_per_tank"] - 33.2) < 0.1, "tank from the session text")
rc = fu["race"]
check(rc and rc["minutes"] == 30 and rc["est_laps"] == 19 and abs(rc["need_l"] - 50.0) < 0.2,
      f"30-minute race: {rc and rc['est_laps']} laps, {rc and rc['need_l']} L with a lap spare")
check(rc["carried_l"] == 58.0 and rc["carried_source"] == "the setup's fuel" and abs(rc["spare_l"] - 8.0) < 0.2,
      "practice only: compared with the setup's fuel")
fu60 = si.review([p], race_minutes=60)["fuel"]["race"]
check(fu60["est_laps"] == 37 and fu60["stops_needed"] == 1 and fu60["spare_l"] < 0, "a 60-minute race needs a stop")
pr = mk(d, "2026-11-01 19-00-00", [104.0] + [100.0] * 5, fuel=(45.0, 2.5), session="Race", session_secs=1200)
frc = si.review([pr])["fuel"]
check(frc["race"]["carried_source"] == "fuel at the race start" and frc["race"]["carried_l"] == 45.0,
      "race file: fuel at the start")
check(frc["tank_source"].startswith("most fuel seen"), "no tank size in the session: most fuel seen, said so")
nofuel = si.review([mk(d, "2026-11-01 20-00-00", [100.0] * 3)])["fuel"]
check(nofuel["per_lap_l"] is None and "can't be measured" in nofuel["text"], "no fuel channel: says so")

# ---------------------------------------------------------------- history
d = folder("hist")
mk(d, "2026-10-20 18-00-00", [100.0, 99.6, 99.5, 99.7, 99.6, 99.5])
mk(d, "2026-10-25 18-00-00", [100.0, 99.3, 99.2, 99.4, 99.3, 99.2])
mk(d, "2026-10-26 18-00-00", [95.0, 95.0], car="mx5 mx52016", track="oulton fosters")
new = mk(d, "2026-11-02 18-00-00", [100.0, 98.9, 99.0, 98.95, 99.1, 99.0])
ss = si.sessions(d)
old = [s for s in ss if new.name not in s["files"]]
reviews = [si.review(s) for s in old]
rv = si.review([new], history=reviews)
h = rv["history"]
check(h["sessions"] == 2, f"only this car + track compared ({h['sessions']})")
check(abs(h["vs_last"]["best_s"] + 0.3) < 0.01 and "0.30 s faster than your last session here" in h["text"], h["text"])
check(h["vs_best_before"] < 0 and "new best" in h["text"], "a new best here")
h2 = si.review([new], history=sl.index(d)["files"])["history"]
check(h2["sessions"] == 2 and abs(h2["vs_last"]["best_s"] + 0.3) < 0.01, "history from index records too")
h3 = si.review([new], history=[s for s in ss])["history"]
check(h3["sessions"] == 2, "history from sessions() dicts; this session itself is not history")
check(si.review([new])["history"] is None, "no history asked: none given")

# ---------------------------------------------------------------- setups, conditions, never raising
s_b = {"Chassis": {"Rear": {"WingAngle": "4.0 deg"}}}
d = folder("misc")
a = mk(d, "2026-11-03 18-00-00", [100.0, 99.5], track_temp=25.0, air_temp=18.0)
b = mk(d, "2026-11-03 18-20-00", [100.0, 99.4], setup=s_b, track_temp=28.0)
rv = si.review([b, a], idx=sl.index(d))
check(rv["files"] == [a.name, b.name], "files put in time order")
check(len(rv["setups"]) == 2 and rv["stints"][0]["setup"] != rv["stints"][-1]["setup"], "setup per stint")
check(any("2 different setups" in n for n in rv["notes"]), "mixed setups noted")
co = rv["conditions"]
check(co["track_temp_start"] == 25.0 and co["track_temp_end"] == 28.0 and co["air_temp_start"] == 18.0
      and co["wet"] is False, f"conditions {co['text']}")
bad = d / "bad.ibt"
bad.write_bytes(b"MZ" + b"\0" * 400)
cut = d / "cut.ibt"
cut.write_bytes(Path(a).read_bytes()[: Path(a).stat().st_size // 2])
try:
    r1 = si.review([bad])
    r2 = si.review([a, bad, cut, d / "missing.ibt"])
    r3 = si.review([])
    ok = (r1["bad_files"] and r1["summary"]["best"] is None and r2["summary"]["best"] is not None
          and len(r2["bad_files"]) == 2 and r3["notes"])
    check(bool(ok), "bad, cut and missing files never raise; good files still reviewed")
except Exception as e:  # noqa: BLE001
    check(False, f"review raised {e!r}")
check(any("spotter" in n for n in rv["notes"]) is False, "fake files have CarLeftRight: no spotter note")
nr = si.review([mk(folder("wet"), "2026-11-03 20-00-00", [100.0, 99.0], wet=True)])
check(nr["conditions"]["wet"] and nr["conditions"]["state"] == "wet", "wet session flagged")


# ---------------------------------------------------------------- 2.13 review
# consistency: the laps left out are counted, and the spread over every valid lap is given beside the clean one
d = folder("cons2")
p = mk(d, "2026-10-30 22-00-00", [100.0, 99.0, 99.02, 99.04, 99.03, 99.01, 99.02, 102.5, 101.9])
c = si.review([p])["consistency"]
check(c["left_out"] == {"warmup": 1, "slow": 2} and "3 more valid laps were left out of the clean laps (1 warm-up, 2 slow)"
      in c["text"], c["text"])
check("counting all 9 valid laps" in c["text"] and "counting only the 6 clean ones" in c["text"], c["text"])
check("under 0.05 s" in c["text"] and "within 0.0" not in c["text"], f"a tiny spread is 'under 0.05 s': {c['text']}")
check(c["valid_iqr"] > c["iqr"], f"valid spread {c['valid_iqr']} > clean spread {c['iqr']}")

# history: never "about the same" beside "a new best"; medians compared only with enough laps on both sides
d = folder("hist2")
mk(d, "2026-10-20 18-00-00", [100.0, 99.5, 99.6, 99.7, 99.6, 99.55])
new2 = mk(d, "2026-11-02 18-00-00", [100.0, 99.48, 99.6, 99.65, 99.62, 99.6])
h = si.review([new2], history=si.sessions(d))["history"]
check("about the same" in h["text"] and "new best" not in h["text"], f"0.02 s quicker: about the same, no new best ({h['text']})")
d = folder("hist3")
mk(d, "2026-10-20 18-00-00", [100.0, 99.9])
new3 = mk(d, "2026-11-02 18-00-00", [100.0, 99.0, 99.2, 99.1, 99.3, 99.2])
h = si.review([new3], history=si.sessions(d))["history"]
check("median" not in h["text"] and h.get("median_skipped"), f"1 clean lap then: typical laps not compared ({h['text']})")
check("faster than your last session here" in h["text"] and "new best" in h["text"], h["text"])

# fuel: a race longer than a tank says the stop and how much to add, not "short; add fuel"
d = folder("fuel2")
p = mk(d, "2026-11-01 18-00-00", [100.0] * 6, fuel=(60.0, 2.5), fuel_max=83,
       session=["Practice", "Lone Qualify", "Race"], session_secs=1800)
fu = si.review([p], race_minutes=60)["fuel"]
check("1 stop needed" in fu["text"] and "at the stop" in fu["text"] and "short; add fuel" not in fu["text"]
      and fu["race"]["add_at_stops_l"] > 0, fu["text"])

# a file without a SubSessionID (offline testing) doesn't join an official session
d = folder("group2")
mk(d, "2026-10-29 10-00-00", [99.0], subsession=333)
mk(d, "2026-10-29 10-10-00", [99.0])
ss = si.sessions(d)
check(len(ss) == 2, f"offline file next to an official one: two sessions ({[x['subsession'] for x in ss]})")

# the SubSessionID memo stays small
for k in range(si.IDS_MEMO_MAX + 50):
    si._ids_memo[("x", k, 0)] = None
one = next(d.glob("*.ibt"))
for key_ in [k_ for k_ in si._ids_memo if k_[0] == str(one)]:
    del si._ids_memo[key_]
si._subsession(one)
check(len(si._ids_memo) <= si.IDS_MEMO_MAX, f"memo capped ({len(si._ids_memo)})")

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
