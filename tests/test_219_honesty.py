"""2.19 honesty and wording: one track under two iRacing codes is one entry in Practice and Setup Lab (same venue and
same layout only); every term the windows use has one plain sentence (glossary, first use or hover text); the Today
page shows the Sure / Likely / A hint / A guess legend as a visible line. The Setup Lab stint null/power simulations
are in test_218_setuplab_trust.py and the habit luck check's in test_218_advice.py. Run: python tests/test_219_honesty.py"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-219-"))

import fake_ibt as fi  # noqa: E402
from debrief import names, sessionibt as sib, setuplab as sl  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


LAPS = [99.5, 99.6, 99.4, 99.5, 99.55, 99.45, 99.5, 99.6]

# ---------------------------------------------------------------- one venue, one layout, two codes
d = TMP / "codes"
d.mkdir()
car = "mclaren570sgt4"
fi.make(d / f"{car}_a 2026-10-20 18-00-00.ibt", LAPS, car_path=car, track="Zolder Circuit", track_code="zolder gp")
fi.make(d / f"{car}_b 2026-10-25 18-00-00.ibt", LAPS, car_path=car, track="Circuit Zolder", track_code="zolder_gp_2")
fi.make(d / f"{car}_c 2026-10-26 18-00-00.ibt", LAPS, car_path=car, track="Circuit Zolder - Short", track_code="zolder short")
idx = sl.index(d)
cb = sl.combos(idx)
check(names.track_key("Zolder Circuit", "Grand Prix") == names.track_key("Circuit Zolder (Grand Prix)")
      and names.track_key("Circuit Zolder - Short", "Grand Prix") != names.track_key("Circuit Zolder", "Grand Prix")
      and names.track_key("", None) == "", "names.track_key: same venue + layout match, another layout or no name does not")
check(len(cb) == 2 and sorted(c["files"] for c in cb) == [1, 2], f"Setup Lab: two codes of one layout are one entry, the other layout stays ({[(c['track'], c['files']) for c in cb]})")
merged = next(c for c in cb if c["files"] == 2)
check(merged["track_code"] == "zolder_gp_2" and merged["track"] == "Circuit Zolder (Grand Prix)", "...under the newest file's code and the display name")
ss = sib.sessions(d)
check(len(ss) == 3 and len({s["track_code"] for s in ss}) == 2, f"Practice: the same two codes in the sessions ({[s['track_code'] for s in ss]})")
check(sum(1 for s in ss if s["track_code"] == "zolder_gp_2") == 2, "Practice: both sessions of the merged track carry one code")
rep = sl.report(d, car, "zolder gp")
check(rep["files"] == 2, f"Setup Lab report asked with the older code still finds both files ({rep['files']})")
rep2 = sl.report(d, car, "zolder_gp_2")
check(rep2["files"] == 2, "...and with the newer one")
check(sl.report(d, car, "zolder short")["files"] == 1, "the other layout's report has only its own file")
# a track known only by its code is never merged with another
a = {"file": "x.ibt", "sort": 1, "track": {"code": "aaa", "name": "", "config": ""}, "car": {"path": car}}
b = {"file": "y.ibt", "sort": 2, "track": {"code": "bbb", "name": "", "config": ""}, "car": {"path": car}}
check([r["track"]["code"] for r in sl.merge_track_codes([a, b])] == ["aaa", "bbb"], "no names: codes stay apart")
check(sl.merge_track_codes(sl.merge_track_codes(idx["files"])) == sl.merge_track_codes(idx["files"]), "merging twice changes nothing")

# ---------------------------------------------------------------- plain words
UI = ROOT / "debrief" / "desktop" / "ui"
js = (UI / "app.js").read_text(encoding="utf-8")
html = (UI / "index.html").read_text(encoding="utf-8")
words = js[js.index("words:["):js.index("keys:[['Ctrl+Shift+M'")] + js[js.index("HELP.words.push(['Pushes, Rotates'"):js.index("/* ---------- 2.16 Racing")]
need = {
    "Yaw rate": "How fast the car is turning",
    "kPa": "The unit for tyre pressure. 100 kPa is about 14.5 psi",
    "Lone Qualify": "Qualifying alone on track",
    "SHA-256 checksum": "A fingerprint that proves the update file wasn\\'t changed",
    "Discord webhook": "A link that lets Debrief post into one Discord channel",
    "Places gained or lost on lap 1": "places you gained",
}
for term, sentence in need.items():
    check(re.search(r"\['" + re.escape(term) + r"','[^']*" + re.escape(sentence), words) is not None, f"glossary: {term}")
check("Average on lap 1" not in js and "Places gained or lost on lap 1, on average" in js, "Racing: 'Average on lap 1' is now places gained or lost, on average")
check("function" in js and "rcAvgPlaces" in js and "held your place" in js, "...and the number says gained / lost / held")
check("yaw rate (how fast the car is turning)" in js, "the balance note explains yaw rate where it is used")
check("(qualifying alone on track)" in js and "sessWord" in js, "session types: Lone Qualify says what it is where it is shown")
check("SHA-256 checksum</abbr>: a fingerprint that proves the update file wasn't changed" in html, "Settings explains the SHA-256 checksum")
check("A Discord webhook is a link that lets Debrief post into one Discord channel" in html, "Settings explains the Discord webhook")
check("kPa, tyre pressure" in js and "100 kPa is about 14.5 psi" in js, "kPa explained where pressures are typed and shown")
# the old glossary is still all there, in plain words (no entry lost)
for t in ("Clean lap", "Typical lap", "Brake point", "Apex", "Coasting", "Trail braking", "Median", "Pace drift", "Consistency score",
          "Stint", "Reference lap", "Focus corner", "Corner coach", "Hot pressure", "Fixed setup", "Pushes, Rotates", "Not clear yet",
          "Countersteer", "Sure, Likely, A hint, A guess", "Quicker, No clear difference", "Hit it", "Best possible lap"):
    check(("['" + t + "','") in js, f"glossary keeps: {t}")
check("shuffle" in words and "bootstrap" not in words.lower() and "Bonferroni" not in words, "no statistics jargon the driver would not know")
# Today's legend is a visible line, not only a tooltip
m = re.search(r'<p class="note tdlegend" id="tdLegend"[^>]*>(.*?)</p>', html, re.S)
check(m is not None and all(w in m.group(1) for w in ("Sure", "Likely", "A hint", "A guess")), "Today: a legend line names all four badges")
check("$('tdLegend').hidden=!x" in js, "...shown whenever the badge is")

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
