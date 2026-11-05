"""Debrief 2.22: team member pages.

  1. a member page with full data (their best, yours, the gap, corners, latest laps)
  2. a page with nothing loaded says why in every empty part
  3. your own page has no "vs you" columns
  4. an unknown name gets a plain message
  5. opening a page makes no Garage 61 request (members never imports the client; the saved file is all it reads)
  6. the Members strip, the server route and the Team page links exist

    python3 tests/test_222_members.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("irsdk", None)

from debrief import members, team  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
        print("ok  ", name)
    except Exception:  # noqa: BLE001
        FAILS.append(name)
        print("FAIL", name)
        traceback.print_exc()


def data(loaded=True):
    full = {"track": "Spa-Francorchamps", "track_short": "Spa", "car": "GT4", "loaded": True,
            "board": [{"key": "josh", "name": "Josh Spence", "t": 140.0, "irating": 2600, "date": "2026-11-01T18:00:00Z"},
                      {"key": "me", "name": "Swazi Williams", "t": 140.42, "irating": 2500, "date": "2026-10-20T18:00:00Z"}],
            "kings": {"drivers": ["me", "josh"], "names": {}, "corners": [
                {"id": "5", "times": {"me": 10.0, "josh": 9.7}, "metrics": {"me": {"min_mph": 60}, "josh": {"min_mph": 64}}}]}}
    other = {"track": "Monza", "track_short": "Monza", "car": "GT4", "loaded": False, "board": [], "kings": None,
             "who": ["Josh Spence"], "races": 1}
    return {"me": {"name": "Swazi Williams", "key": "me"}, "generated_at": "2026-11-02T10:00:00+00:00",
            "members": [{"name": "Josh Spence", "key": "josh", "is_me": False}, {"name": "Swazi Williams", "key": "me", "is_me": True},
                        {"name": "Kenta Young", "key": None, "is_me": False}],
            "combos": [full, other] if loaded else [other]}


def test_full():
    v = members.view(data(), "josh spence")
    assert v["ok"] and v["irating"] == 2600 and v["last_lap"] == "2026-11-01", v
    t = v["tracks"][0]
    assert t["best"] == "2:20.000" and t["yours"] == "2:20.420" and t["gap_text"] == "Josh is 0.42 s quicker.", t
    assert v["corners"][0]["text"] == "Turn 5: Josh carries 4 mph more (64 mph at the slowest point, you 60)", v["corners"]
    assert v["latest"][0]["date"] == "2026-11-01" and v["unloaded"][0]["label"].startswith("Monza")
    h = members.html(v)
    assert "Load this track" in h and "Josh is 0.42 s quicker." in h and "Last refreshed 2026-11-02" in h
    kph = members.view(data(), "Josh Spence", "kph")["corners"][0]["text"]
    assert "mph" not in kph and "km/h" in kph, kph


def test_you_quicker_and_not_driven():
    d = data()
    d["combos"][0]["board"][0]["t"] = 141.0
    assert members.view(d, "Josh")["ok"] is False   # a first name alone is not a known name
    assert members.view(d, "Josh Spence")["tracks"][0]["gap_text"] == "You're 0.58 s quicker."
    d["combos"][0]["board"] = [d["combos"][0]["board"][0]]
    assert members.view(d, "Josh Spence")["tracks"][0]["gap_text"] == "You haven't driven this."


def test_nothing_loaded():
    d = data(False)
    v = members.view(d, "Kenta Young")
    assert v["ok"] and not v["tracks"] and v["irating"] is None
    h = members.html(v)
    assert "No laps for Kenta are saved" in h and "Not available" in h and "No dated laps" in h
    v = members.view(d, "Josh Spence")
    assert v["why"]["tracks"].startswith("Not loaded yet: open the track on the Team page")
    assert "Not loaded yet" in members.html(v)


def test_own_page():
    v = members.view(data(), "Swazi Williams")
    assert v["is_me"] and v["tracks"][0]["best"] == "2:20.420" and "gap_text" not in v["tracks"][0] and not v["corners"]
    h = members.html(v)
    assert "Your best" in h and "Corners where" not in h and "(you)" in h
    assert members.view(data(), "you")["is_me"]


def test_unknown():
    v = members.view(data(), "Nobody <b>")
    assert not v["ok"] and "doesn't know" in v["message"] and "Josh Spence" in v["message"]
    assert "<b>" not in members.html(v)
    assert not members.view({}, "x")["ok"] and not members.view(data(), "")["ok"]


def test_no_requests():
    src = (ROOT / "debrief" / "members.py").read_text(encoding="utf-8")
    assert "g61" not in src and "Garage61" not in src and "api." not in src.replace("api/team", "")
    folder = Path(tempfile.mkdtemp())
    assert "been built yet" in members.page_for(folder, "Josh Spence")
    (folder / "team").mkdir()
    (folder / "team" / "team.json").write_text(json.dumps(data()), encoding="utf-8")
    assert "Josh Spence" in members.page_for(folder, "Josh Spence")


def test_strip_route_links():
    c = members.cards(data())
    assert [x["name"] for x in c] == ["Josh Spence", "Swazi Williams", "Kenta Young"] and c[0]["tracks"] == 1 and c[2]["tracks"] == 0
    folder = Path(tempfile.mkdtemp())
    d = data()
    d.update({"version": 2, "h2h": [], "requests": 0, "notes": [], "coverage": {"total": 2, "loaded": 1, "note": ""}})
    team.write(folder, d, "mph")
    saved = json.loads((folder / "team" / "team.json").read_text(encoding="utf-8"))
    assert saved["cards"][0]["name"] == "Josh Spence"
    tpl = (ROOT / "debrief" / "templates" / "team.html").read_text(encoding="utf-8")
    assert "api/team/member?name=" in tpl and 'id="members"' in tpl and tpl.count("mlink(") >= 3
    srv = (ROOT / "debrief" / "desktop" / "server.py").read_text(encoding="utf-8")
    assert 'r == "api/team/member"' in srv
    assert "debrief-team-load" in (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")


for n, f in list(globals().items()):
    if n.startswith("test_") and callable(f):
        check(n, f)

if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("all passed")
