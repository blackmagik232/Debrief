"""2.18 lane F: advice that fits the car and says what to do.

    python3 tests/test_218_advice.py

  - review habits agree with each other: corner time that varies vs the same brake marker, lap 1 praise only when
    lap 1 really was as quick, brake and throttle together only when the typical laps do more of it than the best
  - "Fix first": corners with a measured cause come first; a corner without one says what to watch
  - Racing: an early, clearly labelled sign with generic racecraft when your race incidents bunch up in one place
    (at least 3 across 2 races), the statistics untouched
  - plain words in the window: glossary entries, no raw "? -> ?" start rows
(the corner coach's brake-pressure and coasting rules are tested in test_213_corners.py, section (i))
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import incidentmap as im  # noqa: E402
from debrief import insights  # noqa: E402

fails = 0


def check(ok, msg):
    global fails
    print(("ok   " if ok else "FAIL ") + msg)
    if not ok:
        fails += 1


def corner(cid, avail, iqr=0.05):
    return {"id": cid, "apex_m": 100.0, "start_m": 0.0, "end_m": 200.0, "best_t": 10.0, "median_t": 10.0 + avail,
            "p25_t": 10.0, "avail": avail, "iqr": iqr, "best_lap": 3, "summary": {"gear": 3, "min_mph": 60.0}}


def tech(cid, **typ):
    best = {"brake_on": 500.0, "min_mph": 60.0, "full_thr": 700.0, "coast_s": 0.5, "gear": 3}
    return {"id": cid, "best": best, "typical": dict(best, **typ), "ref": None}


def base(**kw):
    laps = [{"n": i, "t": 90.0 + (0.1 * (i % 3)), "clean": True} for i in range(1, 13)]
    d = {"laps": laps, "best": {"n": 2, "t": 90.0, "clean": True}, "median": 90.1, "refs": [],
         "counts": {"unclean": [], "slow": [], "traffic": [], "clean": 12}, "trend_total": 0.05,
         "consistency": {"score": 80, "label": "Very consistent", "std": 0.1,
                         "by_corner": [{"id": "C1", "iqr": 0.25}, {"id": "C2", "iqr": 0.2}, {"id": "C3", "iqr": 0.05}]},
         "corners": [corner("C1", 0.12), corner("C2", 0.21), corner("C3", 0.09)],
         "technique": [tech("C1"), tech("C2", brake_on=485.0), tech("C3")]}
    d.update(kw)
    return d


def keyed(out, which, key):
    return next((x for x in out[which] if x["key"] == key), None)


# ---------------------------------------------------------------- 1. "drive differently" vs "same brake marker"
spread = {"corners": [{"id": c, "iqr_m": 3.0, "n": 12, "label": "locked"} for c in ("C1", "C2", "C3")]}
out = insights.build(base(brake_spread=spread))
v, m = keyed(out, "bad", "vary"), keyed(out, "good", "brake_markers")
check(v is not None and m is not None, "both habits are there (corner time varies, brake marker steady)")
if v and m:
    check("keep your brake marker in C1 and C2" in v["try"] and "pick one braking marker" not in v["try"],
          f"the varying corners say keep the brake marker and fix turn-in and throttle: {v['try']}")
    check("not the brake point" in v["detail"], f"detail says it's corner time, not the brake point: {v['detail']}")
    check("C1" in m["text"] and v["scope"] == "corner" and m["scope"] == "corner",
          f"2.28.1: both habits are corner habits and the strip shows them side by side: {m['text']}")
# one varying corner with a steady brake marker, one with a moving one
spread2 = {"corners": [{"id": "C1", "iqr_m": 3.0, "n": 12, "label": "locked"}, {"id": "C2", "iqr_m": 12.0, "n": 12, "label": "ok"},
                       {"id": "C3", "iqr_m": 3.0, "n": 12, "label": "locked"}]}
v = keyed(insights.build(base(brake_spread=spread2)), "bad", "vary")
check(v and "in C1 keep your brake marker" in v["try"] and "pick one braking marker" in v["try"],
      f"mixed: says which corner is which: {v and v['try']}")
# no brake-point data: the old advice
v = keyed(insights.build(base(brake_spread={"corners": []})), "bad", "vary")
check(v and "pick one braking marker and one turn-in point" in v["try"], "no brake-point data: pick a braking marker")

# ---------------------------------------------------------------- 2. lap 1 praise only when it was really as quick
for t1, want in ((90.1, "as quick as"), (89.7, "0.4 s quicker"), (90.5, None), (90.14, "as quick as"), (90.04, "0.06 s quicker")):
    laps = [{"n": 1, "t": t1, "clean": True}] + [{"n": i, "t": 90.1, "clean": True} for i in range(2, 13)]
    out = insights.build(base(laps=laps))
    g = keyed(out, "good", "lap1")
    if want is None:
        check(g is None, f"lap 1 {t1 - 90.1:+.1f} s slower than typical: no 'Strong first lap'")
    else:
        check(g is not None and want in g["text"] and "within" not in g["text"], f"lap 1 {t1}: {g and g['text']}")

# ---------------------------------------------------------------- 3. brake and throttle together: only when it costs
# (2.19: and only when the quick laps beat random shuffles of the laps: the best lap alone could be luck)
def quick_laps(n=12):
    """Lap 3, 6, 9, 12 are the quick ones (90.0 s); the rest are 0.1 and 0.2 s slower."""
    return [{"n": i, "t": 90.0 + (0.1 * (i % 3)), "clean": True} for i in range(1, n + 1)]


QUICK = {3, 6, 9, 12}
style = {str(n): {"coast_s": 2.0, "overlap_s": 0.8} for n in range(1, 13)}
out = insights.build(base(style=style))
check(keyed(out, "bad", "overlap") is None, "0.8 s a lap of overlap on every lap, best lap too: not a bad habit")
style2 = {str(n): {"coast_s": 2.0, "overlap_s": 0.2 if n in QUICK else 0.8} for n in range(1, 13)}
b = keyed(insights.build(base(style=style2, best={"n": 3, "t": 90.0, "clean": True})), "bad", "overlap")
check(b is not None and "0.6 s more than on your best lap" in b["text"] and b["cost"] == 0.6 and "Luck check" in b["detail"],
      f"typical laps overlap 0.6 s more than the quick laps: a habit with its cost ({b and b['text']})")
only_best = dict(style, **{"2": {"coast_s": 2.0, "overlap_s": 0.2}})
check(keyed(insights.build(base(style=only_best)), "bad", "overlap") is None,
      "one lap with less overlap and the rest the same: could be luck, so no habit")
style3 = {str(n): {"coast_s": 1.0 if n in QUICK else 2.0, "overlap_s": 0.8} for n in range(1, 13)}
c = keyed(insights.build(base(style=style3, best={"n": 3, "t": 90.0, "clean": True})), "bad", "coast")
check(c and "Trail braking is a different thing" in c["detail"] and "lifts included" in c["detail"] and "Luck check" in c["detail"],
      f"coasting says what it counts: {c and c['detail']}")
check(keyed(insights.build(base(style=dict(style, **{"2": {"coast_s": 1.0, "overlap_s": 0.8}}))), "bad", "coast") is None,
      "coasting less on one lap only: could be luck, so no habit")

# the luck check on its own, and the null: pedal habits unrelated to lap time must almost never be called habits
check(insights.luck_p([90 + i * 0.1 for i in range(6)], [1.0] * 6) is None, "under 8 laps: no luck check possible, no claim")
check(insights.luck_p([90 + i * 0.1 for i in range(12)], [0.2] * 3 + [0.8] * 9) <= 0.01, "quick laps clearly lower: rare by chance")
check(insights.luck_p([90 + i * 0.1 for i in range(12)], [0.8] * 11 + [0.2]) > 0.3, "the slowest lap lower: nothing")
import numpy as _np  # noqa: E402


def _null_rate(runs, check_luck, n_laps=14, seed=19):
    rng = _np.random.default_rng(seed)
    hits = 0
    for _ in range(runs):
        ts = 90 + rng.normal(0, 0.3, n_laps)
        laps = [{"n": i + 1, "t": float(ts[i]), "clean": True} for i in range(n_laps)]
        sty = {str(i + 1): {"coast_s": float(rng.normal(2.0, 0.8)), "overlap_s": float(rng.normal(1.0, 0.5))}
               for i in range(n_laps)}
        bn = int(_np.argmin(ts)) + 1
        d = base(laps=laps, style=sty, best={"n": bn, "t": float(ts.min()), "clean": True})
        if not check_luck:
            old = insights.LUCK_ALPHA
            insights.LUCK_ALPHA = 2.0                     # the effect-size gate alone (how 2.18 behaved)
            try:
                o = insights.build(d)
            finally:
                insights.LUCK_ALPHA = old
        else:
            o = insights.build(d)
        hits += keyed(o, "bad", "coast") is not None or keyed(o, "bad", "overlap") is not None
    return hits / runs


_with, _without = _null_rate(300, True), _null_rate(300, False)
check(_with <= 0.08, f"habits unrelated to lap time: coasting or overlap called a habit in {_with:.1%} of 300 runs with the luck check (<= 8%)")
check(_without > 0.2, f"... and in {_without:.1%} without it (2.18: best lap vs typical lap only)")

# ---------------------------------------------------------------- 4. fix first: a measured cause first, never empty
d = base(corners=[corner("C1", 0.5), corner("C2", 0.21), corner("C3", 0.09)],
         technique=[tech("C1"), tech("C2", brake_on=485.0), tech("C3", min_mph=57.0)])
out = insights.build(d)
order = [f["corner"] for f in out["fix"]]
check(order == ["C2", "C3", "C1"], f"corners with a tip first, biggest first; the biggest without one last: {order}")
c1 = out["fix"][-1]
check(not c1["tips"] and c1.get("watch") and "Try:" in c1["watch"] and c1["try"].startswith("Try:") and "lap 3" in c1["watch"],
      f"a corner without a tip says what to watch: {c1.get('watch')}")
check(all("watch" not in f for f in out["fix"] if f["tips"]), "corners with tips have no watch line")
check(out["time_to_find"] == 0.8, f"time to find still adds every corner ({out['time_to_find']})")

# ---------------------------------------------------------------- 5. Racing: early sign, not proven yet
zs = im.zones([{"id": f"C{k}", "end_m": 400.0 * k} for k in range(1, 6)], 2000.0)


def row(sess, zone, pts=4, lap1=True, typ="contact", kind="race", before=False):
    return {"session": sess, "kind_of_session": kind, "zone": zone, "points": pts, "lap1": lap1, "type": typ,
            "before_green": before}


def m_of(rows, patterns=None):
    return {"zones": zs, "list": rows, "patterns": patterns or []}


rows = [row("r1", "C1"), row("r2", "C1"), row("r3", "C1", pts=1, typ="own", lap1=False), row("r3", "C3", pts=2, lap1=False, typ="own"),
        row("p1", "C2", pts=4, kind="practice"), row("r2", "C4", pts=1, lap1=False, typ="own")]
E = im.early_signs(m_of(rows))
check(len(E) == 1 and E[0]["zone"] == "C1", f"Turn 1 is the early sign ({E})")
if E:
    e = E[0]
    check(e["text"].startswith("Early sign, not proven yet: Lap 1, Turn 1 is where most of your race incident points came from (9 of 12"),
          f"wording: {e['text']}")
    check("isn't enough to call it a pattern" in e["text"] and "chance" not in e["text"], "says it isn't a pattern, claims no statistics")
    check(e["lap1"] and any("lap 1" in t for t in e["tries"]) and any("car's width" in t for t in e["tries"])
          and any("before the braking zone" in t for t in e["tries"]), f"lap 1 racecraft tips: {e['tries']}")
    check(e["events"] == 3 and e["races"] == 3 and e["points"] == 9 and e["of_points"] == 12, "counts race incidents only")
check(im.early_signs(m_of(rows[:2])) == [], "2 incidents: too few for an early sign")
one_race = [row("r1", "C1"), row("r1", "C1"), row("r1", "C1", pts=1)]
check(im.early_signs(m_of(one_race)) == [], "3 incidents in one race: not an early sign (needs 2 races)")
pat = [{"kind": "corner", "zone": "C1", "text": "Turn 1 is a pattern"}]
check(im.early_signs(m_of(rows, pat)) == [], "a place that is already a proven pattern gets no early sign")
pre = [row("r1", "C1", before=True), row("r2", "C1", before=True), row("r3", "C1", before=True)]
check(im.early_signs(m_of(pre)) == [], "pace-lap incidents don't count")
own = [row("r1", "C2", pts=2, lap1=False, typ="own"), row("r2", "C2", pts=2, lap1=False, typ="own"),
       row("r2", "C2", pts=1, lap1=False, typ="own")]
E = im.early_signs(m_of(own))
check(E and not E[0]["lap1"] and E[0]["kind"] == "own" and "mostly your own mistake" in E[0]["text"]
      and E[0]["tries"] == im.EARLY_TRIES["own"], f"own mistakes: driving tips, not lap 1 tips ({E and E[0]['text']})")

minor = [row("r1", "C1", pts=1, lap1=False, typ="own"), row("r2", "C1", pts=1, lap1=False, typ="own"),
         row("r3", "C1", pts=1, lap1=False, typ="own"), row("r1", "C5", pts=8, lap1=False, typ="own")]
E = [e for e in im.early_signs(m_of(minor)) if e["zone"] == "C1"]
check(E and "a lot" not in E[0]["text"] and "where most" not in E[0]["text"]
      and "27% of your race incident points" in E[0]["text"],
      f"a minority share is said as the real share, never 'a lot' or 'most' ({E and E[0]['text']})")

# the statistics are untouched: the same track gives the same sentences and patterns with the early sign beside them
o = [{"id": f"r{k}", "kind": "race", "track": "Zolder", "type": "Race", "_zones": zs,
      "_events": [{"t": 10.0, "lap": 1, "lap1": True, "pct": 0.1, "zone": "C1", "where": "Turn 1", "points": 4, "parts": [4],
                   "class": {"type": "contact", "sure": "sure", "why": "iRacing counted contact"}}]} for k in range(1, 4)]
tm = im.track_map("Zolder", o)
check(tm["early_signs"] and tm["early_signs"][0]["zone"] == "C1", f"track_map carries the early sign ({tm['early_signs']})")
check(tm["patterns"] == [] and any("not a pattern" in s or "chance" in s for s in tm["sentences"])
      and not any("Early sign" in s for s in tm["sentences"]), f"sentences unchanged, no pattern: {tm['sentences']}")

# ---------------------------------------------------------------- 6. the window's words
app = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
rev = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
for term in ("'Trail braking'", "'Median'", "'Pace drift'", "'Consistency score'"):
    check(f"[{term}," in app, f"glossary explains {term}")
check("'?')+' → '" not in app and "'Not known'" in app.split("function rcRaceTable", 1)[1].split("rcSigned(r.change)", 1)[0],
      "start rows: 'Not known' instead of '? → ?'")
check("t.early_signs" in app and "Early sign, not proven yet" in app, "Racing shows the early sign")
check("each compared only with its own kind" in app, "corner coach intro: no 'practice ... only; race laps only'")
check("f.watch" in rev and "No single measure stands out" not in rev, "fix card shows what to watch, never the empty line")
check("tips.length?'See the numbers':" in rev, "a card with a tip never says 'nothing big stands out'")
check("half your laps were quicker, half slower" in rev and "Out of 100: higher means" in rev
      and "Plus means they got slower" in rev, "summary tiles explain median, Consistency and Pace drift")

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
