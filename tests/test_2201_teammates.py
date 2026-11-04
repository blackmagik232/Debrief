"""Debrief 2.20.1: no teammate vanishes silently.

  - a teammate whose best lap is flagged gets their best clean lap instead (one extra request, named reason);
  - a team member with no laps is named with what to do;
  - a teammate with a lap time but no telemetry (403) is named as "lap time only";
  - a slower teammate is kept;
  - a lap with no driver name is explained, not dropped silently;
  - the "Compare with a teammate's lap" list carries the same names.

    python3 tests/test_2201_teammates.py

No network: stand-in Garage 61 answers only.
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("irsdk", None)

from debrief import prep, team, teamref  # noqa: E402

FAILS = []


def lap(slug, first, t, **kw):
    d = {"slug": slug, "firstName": first, "lastName": "X"} if slug else {"firstName": "", "lastName": ""}
    return {"id": f"{slug}-{t}", "driver": d, "lapTime": t, "startTime": "2026-11-01T18:00:00Z", **kw}


class Api:
    """Stand-in Garage 61: first call (group=driver) and the one extra call (group=none)."""
    def __init__(self, best, everything, roster=None):
        self.best, self.everything, self.calls = best, everything, []
        self.roster = roster

    def find_laps(self, **kw):
        self.calls.append(kw.get("group"))
        return list(self.best if kw.get("group") == "driver" else self.everything)

    def me(self):
        return {"slug": "me", "firstName": "Me", "lastName": "X"}

    def teams(self):
        t = {"slug": "team1", "name": "Team"}
        if self.roster:
            t["members"] = [{"slug": s, "firstName": n, "lastName": "X"} for s, n in self.roster.items()]
        return [t]


def check(name, fn):
    try:
        fn()
        print("ok  ", name)
    except Exception:  # noqa: BLE001
        FAILS.append(name)
        print("FAIL", name)
        traceback.print_exc()


def test_flagged_best_falls_back():
    api = Api([lap("josh", "Josh", 80.0, incomplete=True), lap("kenta", "Kenta", 91.0)],
              [lap("josh", "Josh", 80.0, incomplete=True), lap("josh", "Josh", 92.5), lap("josh", "Josh", 93.1),
               lap("kenta", "Kenta", 91.0)])
    out = prep._teammates(api, 1, 2, "team1", 90.0, lambda m: None)
    names = {x["name"]: x["t"] for x in out["laps"]}
    assert names == {"Josh X": 92.5, "Kenta X": 91.0}, names
    assert any(n.startswith("Josh X: best lap was partial or unclean, used their best clean lap") for n in out["notes"]), out["notes"]
    assert api.calls == ["driver", "none"], api.calls   # exactly one extra request


def test_all_flagged_named():
    api = Api([lap("josh", "Josh", 80.0, incomplete=True)], [lap("josh", "Josh", 80.0, incomplete=True)])
    out = prep._teammates(api, 1, 2, "team1", 90.0, lambda m: None)
    assert out["laps"] == []
    assert any("none of their laps here were clean" in n and n.startswith("Josh X") for n in out["notes"]), out["notes"]


def test_roster_member_without_rows():
    api = Api([lap("kenta", "Kenta", 91.0)], [lap("kenta", "Kenta", 91.0)], roster={"kenta": "Kenta", "josh": "Josh"})
    out = prep._teammates(api, 1, 2, "team1", 90.0, lambda m: None)
    assert [x["name"] for x in out["laps"]] == ["Kenta X"]
    assert any(n.startswith("Josh X is on your team but Garage 61 returned no laps") and "share laps and telemetry" in n
               for n in out["notes"]), out["notes"]
    assert api.calls == ["driver", "none"]


def test_no_extra_request_when_all_fine():
    api = Api([lap("kenta", "Kenta", 91.0)], [], roster={"kenta": "Kenta"})
    out = prep._teammates(api, 1, 2, "team1", 90.0, lambda m: None)
    assert api.calls == ["driver"] and out["notes"] == []


def test_slower_teammate_kept():
    api = Api([lap("josh", "Josh", 180.0), lap("kenta", "Kenta", 91.0), lap("amy", "Amy", 92.0)], [])
    out = prep._teammates(api, 1, 2, "team1", 90.0, lambda m: None)
    assert "Josh X" in [x["name"] for x in out["laps"]], out
    # but a clearly broken lap is still dropped, by name
    api = Api([lap("josh", "Josh", 12.0), lap("kenta", "Kenta", 91.0)], [lap("josh", "Josh", 12.0)])
    out = prep._teammates(api, 1, 2, "team1", 90.0, lambda m: None)
    assert [x["name"] for x in out["laps"]] == ["Kenta X"]
    assert any(n.startswith("Josh X: quickest lap") and "broken" in n for n in out["notes"]), out["notes"]


def test_empty_slug_has_reason():
    api = Api([lap("", "", 91.0), lap("kenta", "Kenta", 92.0)], [])
    out = prep._teammates(api, 1, 2, "team1", 90.0, lambda m: None)
    assert [x["name"] for x in out["laps"]] == ["Kenta X"]
    assert any("no driver name" in n for n in out["notes"]), out["notes"]


def test_team_gather_and_telemetry_note():
    mates, reasons = team.gather_mates([lap("josh", "Josh", 80.0, incomplete=True)],
                                       lambda: [lap("josh", "Josh", 92.0)], "me", 90.0)
    assert mates["josh"]["t"] == 92.0 and reasons[0]["name"] == "Josh X"
    # the "lap time only" line is what the Compare list shows by name
    notes = [team.reason_text({"name": "Josh X", "text": "lap time only, they aren't sharing telemetry with the team"})]
    rows = teamref.laps_on_hand(None, [], notes)
    assert rows and rows[0]["driver"] == "Josh X" and "lap time only" in rows[0]["note"], rows


def test_team_build_403_named():
    from debrief import team as T

    class A(Api):
        requests_made = 0
        def has_csv(self, i): return False
        def lap_csv(self, i): return None   # 403/404
        def tracks(self): return []
        def cars(self): return []
        def cached_items(self, k): return []
    tmp = Path(tempfile.mkdtemp(prefix="debrief-2201-"))
    api = A([lap("josh", "Josh", 91.0), lap("kenta", "Kenta", 92.0)], [])
    orig = T.library_combos
    T.library_combos = lambda f: [{"track": "Zolder", "car": "Car", "track_id": 1, "car_id": 2, "races": 1,
                                   "best": 90.0, "best_date": "2026-11-01", "last": "2026-11-01"}]
    try:
        d = T.build(api, tmp, "team1", log=lambda m: None)
    finally:
        T.library_combos = orig
    notes = d["combos"][0]["mate_notes"]
    assert any(n.startswith("Josh X: lap time only") for n in notes), notes


for n, f in list(globals().items()):
    if n.startswith("test_") and callable(f):
        check(n, f)
print("FAILED" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
