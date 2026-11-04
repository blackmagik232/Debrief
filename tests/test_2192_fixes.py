"""Debrief 2.19.2 quick fixes from four testers: skippable setup steps, the overlay's next step, why skill scores are
missing, what a goal is based on, a failed review that says which laps and how to fix it, Setup Lab remembering
open / fixed, and the Team page listing teammates when no series is picked.

    python3 tests/ci_run.py tests/test_2192_fixes.py

Synthetic data only. Words and wiring only: no statistic changes.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-2192-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import engine, firstrun as fr, goals, overlay, profile, setuplab, team  # noqa: E402

JS = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
LIB = (ROOT / "debrief" / "templates" / "library.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. setup steps can be skipped
def test_skip_a_setup_step():
    state = TMP / "firstrun.json"
    w = fr.walkthrough(facts={}, state={})
    assert w["next"] == "folder" and w["left"] == 6 and all(s["skipped"] is False for s in w["steps"])
    assert fr.skip_step("folder", path=state) and fr.skip_step("voice", path=state)
    st = fr.load_state(state)
    w = fr.walkthrough(facts={}, state=st)
    sk = {s["id"] for s in w["steps"] if s["skipped"]}
    assert sk == {"folder", "voice"} and w["next"] == "record" and w["left"] == 4 and w["skipped"] == 2
    assert fr.skip_step("folder", path=state, skip=False)
    assert fr.load_state(state)["skipped"] == ["voice"]
    assert not fr.skip_step("nonsense", path=state)
    # skipping everything not done: nothing is next and the checklist is not shown
    w = fr.walkthrough(facts={}, state={"skipped": list(fr.STEP_IDS)})
    assert w["next"] is None and w["show"] is False and w["left"] == 0
    # a done step is never "skipped"
    w = fr.walkthrough(facts={"token": True}, state={"skipped": ["token"]})
    assert not [s for s in w["steps"] if s["id"] == "token"][0]["skipped"]


def test_voice_step_says_what_it_does_and_the_page_has_skip_and_why():
    voice = {s["id"]: s for s in fr.steps({})}["voice"]
    assert "says one short line out loud" in voice["text"]
    assert "skipBtn(" in JS and "firstrun/skip" in JS and "'Skip'" in JS
    assert 'id="tdWhyBox"' in HTML and "<summary" in HTML.split('id="tdWhyBox"')[1][:120] and "Why?" in HTML
    assert "'Setup: '+plural(left,'step')+' left'" in JS      # a small badge, not a banner sentence


# ---------------------------------------------------------------- 3. overlay that can't open
def test_overlay_next_step():
    assert overlay.next_step("linux", "The overlay needs Windows.") == "The overlay works on Windows only; the coach still speaks."
    w = overlay.next_step("win32", "RuntimeError: no display")
    assert "borderless windowed" in w and "close and reopen Debrief" in w
    assert "Install Debrief again" in overlay.next_step("win32", "no Tk window library")
    assert overlay.next_step("win32", "") == ""

    class Mem:
        d = {}

        def load(self):
            return dict(self.d)

        def save(self, v):
            self.d = dict(v)

    c = overlay.Overlay(overlay.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="linux")
    s = c.status()
    assert s["usable"] is False and s["problem"] == overlay.OFF_WINDOWS
    assert "o.usable===false" in JS and ".ovunavail .row" in (ROOT / "debrief/desktop/ui/app.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 4. progress words
def test_skills_reason_in_one_line():
    line = profile.why_line({"no_traces": 3, "few_clean": 1}, 4)
    assert line == "None of your 4 race reviews has skill scores yet: they were saved without lap traces (open one and press Rebuild)."
    assert profile.why_line({}, 0) == "" and "1 race review has" in profile.why_line({"few_clean": 1}, 1)
    assert "the page below says why" not in JS and "p.why_line" in JS


def test_goal_basis_words():
    assert "time you set" in goals.basis(0)
    b = goals.basis(3)
    assert "3 reviewed races" in b and "quickest clean lap" in b and "minus the target" in b
    assert "1 reviewed race here" in goals.basis(1)
    d = Path(tempfile.mkdtemp(prefix="g-", dir=TMP))
    goals.save_goal(d, {"track": "Road Atlanta", "car": "MX-5", "target_s": 90.0})
    p = goals.progress(d, rows=[])
    assert p and p[0]["basis"] == goals.basis(0) and p[0]["gap"] is None


def test_season_has_a_track_picker_and_months():
    assert 'id="seasontrack"' in LIB and "monthlyPace" in LIB and "r.track===trk" in LIB


# ---------------------------------------------------------------- 5. failed review
def test_failed_review_says_which_laps_and_how_to_fix():
    m = engine.few_laps_message([3, 4, 5], [7], 1)
    assert m.startswith("not enough laps with readable telemetry in this race.")
    assert "laps 3, 4 and 5" in m and "lap 7 couldn't be read" in m.replace("The telemetry for ", "") .replace("lap 7", "lap 7")
    assert "Alt+L" in m and "can't retry just this step" in m
    assert engine.few_laps_message([], [], 0).count("No telemetry came back") == 0
    assert "and 4 more" in engine.few_laps_message(list(range(1, 13)), [], 0)

    def lap(i, t=90.0):
        return {"id": f"L{i}", "lap_time": t, "lap_number": i}
    try:
        engine.analyze({"driver": "x"}, [lap(1), lap(2), lap(3)], {"L1": "garbage"}, [], {})
    except ValueError as e:
        msg = str(e)
        assert "laps 2 and 3" in msg and "Alt+L" in msg, msg
    else:
        raise AssertionError("expected ValueError")


# ---------------------------------------------------------------- 6. Racing leads with races
def test_racing_leads_with_races():
    assert "plural(races,'race')+' to look at'" in JS and 'id="rcFilesNote"' in HTML and "<details" in HTML.split('id="rcFoundNote"')[1][:80]


# ---------------------------------------------------------------- 7. Setup Lab remembers open / fixed
def test_setuplab_remembers_the_answer():
    s = setuplab.store_choice({}, "mx5", "road", "fixed", series_id="S1")
    assert setuplab.saved_choice(s, "mx5", "road") == "fixed"
    assert setuplab.saved_choice(s, "gt3", "spa", "S1") == "fixed"      # another car in the same series
    assert setuplab.saved_choice(s, "gt3", "spa") is None
    assert setuplab.remembered_for_car(s, "MX5") == "fixed"              # the same car at another track
    assert setuplab.remembered_for_car(s, "gt3") is None
    s = setuplab.store_choice(s, "mx5", "road", "", series_id="S1")
    assert s == {}
    try:
        setuplab.store_choice({f"k{i}": "open" for i in range(499)}, "a", "b", "open", limit=500)
    except ValueError:
        pass
    else:
        raise AssertionError("limit")
    assert "series:sl.series||undefined" in JS


# ---------------------------------------------------------------- 8. Team roster
def test_team_roster_lists_teammates_with_what_is_missing():
    data = {"me": {"key": "me", "name": "Me"}, "combos": [
        {"board": [{"key": "me", "name": "Me", "t": 90}, {"key": "ann", "name": "Ann", "t": 91}], "kings": None},
        {"board": [{"key": "bob", "name": "Bob", "t": 95}], "kings": None}], "weekly": {"series": []}}
    r = {x["name"]: x for x in team.roster(data)}
    assert list(r) == ["Ann", "Bob"]
    assert r["Ann"]["missing"] == [team.NO_TELEMETRY, team.PICK_SERIES]
    assert r["Bob"]["missing"] == [team.NO_SHARED, team.PICK_SERIES]
    data["weekly"] = {"series": [{"id": 1, "weeks": []}]}
    assert team.PICK_SERIES not in team.roster(data)[0]["missing"]
    assert team.roster(None) == [] and team.roster({}) == []
    d = Path(tempfile.mkdtemp(prefix="t-", dir=TMP))
    page = team.write(d, dict(data, weekly={"series": []}, h2h=[], notes=[], team={"slug": "x", "name": "X"}))
    j = json.loads((page.parent / "team.json").read_text(encoding="utf-8"))
    assert [x["name"] for x in j["roster"]] == ["Ann", "Bob"] and 'id="roster"' in page.read_text(encoding="utf-8")
