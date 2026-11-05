"""Debrief 2.21: a fully working Team page.

  1. the Team page sees the series picked on Prepare; it works with no series and no reviews (Prepare's track and car)
  2. one failing track and car never costs the others (a plain reason on it)
  3. the team is Midfield Menace (three fixed members, always listed); other Overdriver Academy drivers are apart
  4. a member nobody matches is named; matching ignores case and accents
  5. teammate laps are kept per driver: two teammates for one car and track both stay (old store migrated)
  6. speeds follow Settings > Units
  7. Prepare reuses the Team page's rules, says who was left out
  8. swallowed errors now say so on the page
  9. refresh feedback: stale after a Settings change, "Up to date" when nothing was requested
 10. iRating is on every teammate row
 11. Car vs car iRating bands: three, wider

    python3 tests/test_221_team.py

No network: stand-in Garage 61 answers only.
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

from debrief import brand, carcompare, prep, series, team, units  # noqa: E402

FAILS = []
CAR, TRACK = "Ford Mustang GT4", "Spa-Francorchamps"
PEOPLE = {"josh": "Josh Spence", "kenta": "Kenta Young", "swazi": "Swazi Williams"}


def lap(slug, name, t, **kw):
    f, _, l = name.partition(" ")
    return {"id": f"{slug}-{t}", "driver": {"slug": slug, "firstName": f, "lastName": l}, "lapTime": t,
            "startTime": "2026-11-01T18:00:00Z", "driverRating": kw.pop("ir", 2500), **kw}


class Api:
    requests_made = 0

    def __init__(self, laps_by_track, group="overdriver-academy", fail_tracks=()):
        self.laps_by_track, self.group, self.fail = laps_by_track, group, set(fail_tracks)
        self.calls = []

    def me(self):
        return {"slug": "swazi", "firstName": "Swazi", "lastName": "Williams"}

    def teams(self, refresh=False):
        return [{"slug": self.group, "name": "Overdriver Academy"}]

    def tracks(self):
        return [{"id": 1, "name": "Spa-Francorchamps"}, {"id": 3, "name": "Monza"}]

    def cars(self):
        return [{"id": 2, "name": CAR}]

    def cached_items(self, k):
        return []

    def find_laps(self, **kw):
        self.calls.append(kw)
        tid = kw["tracks"][0]
        if tid in self.fail:
            raise RuntimeError("Garage 61 timed out")
        rows = self.laps_by_track.get(tid, [])
        if kw.get("drivers") == ["me"]:
            return [l for l in rows if l["driver"]["slug"] == "swazi"]
        rows = [l for l in rows if l["driver"]["slug"] != "swazi"]
        if kw.get("group") == "driver":
            best = {}
            for l in rows:
                if l["driver"]["slug"] not in best or l["lapTime"] < best[l["driver"]["slug"]]["lapTime"]:
                    best[l["driver"]["slug"]] = l
            return list(best.values())
        return rows

    def has_csv(self, i):
        return False

    def lap_csv(self, i):
        return None


def combos(*tracks):
    return [{"track": t, "car": CAR, "track_id": i, "car_id": 2, "races": 1, "best": 140.0, "best_date": "2026-11-01",
             "last": "2026-11-01T18:00:00Z"} for t, i in tracks]


def run_build(api, cc, members=None, monkey=True):
    folder = Path(tempfile.mkdtemp())
    team.library_combos = lambda f: cc
    cfg = {"team_name": "Midfield Menace", "team_slug": "overdriver-academy",
           "team_members": list(brand.MEMBERS) if members is None else members}
    logs = []
    return team.build(api, folder, "overdriver-academy", log=logs.append, cfg=cfg), logs, folder


def check(name, fn):
    try:
        fn()
        print("ok  ", name)
    except Exception:  # noqa: BLE001
        FAILS.append(name)
        print("FAIL", name)
        traceback.print_exc()


def test_series_from_prepare():
    folder = Path(tempfile.mkdtemp())
    sched = {"season": "S", "series": [{"id": "ford-mustang-challenge-by-skip-barber",
                                         "name": "Ford Mustang Challenge by Skip Barber", "weeks": []}]}
    assert series.effective(folder, sched)["series"] == []        # nothing saved, nothing picked
    series.save_choice(folder, {"series": "Ford Mustang Challenge by Skip Barber", "car": "Ford Mustang GT4"})
    e = series.effective(folder, sched)
    assert e["from_prepare"] and e["series"][0]["name"] == "Ford Mustang Challenge by Skip Barber", e
    series.save(folder, ["ford-mustang-challenge-by-skip-barber"], sched)
    assert series.effective(folder, sched)["from_prepare"] is False   # an explicit list wins


def test_no_reviews_uses_prepare_track_and_car():
    folder = Path(tempfile.mkdtemp())
    series.save_choice(folder, {"series": "X", "car": CAR, "track": TRACK})
    c = team.prepare_combos(folder)
    assert c and c[0]["track"] == TRACK and c[0]["car"] == CAR
    assert team.prepare_combos(Path(tempfile.mkdtemp())) == []


def test_zero_one_three_twentyfive():
    mine = lap("swazi", "Swazi Williams", 140.0)
    for n in (0, 1, 3, 25):
        names = ["Josh Spence", "Kenta Young"] + [f"Driver{i} Extra" for i in range(30)]
        rows = [mine] + [lap(f"s{i}", names[i], 141.0 + i * 0.1, ir=1800 + i * 50) for i in range(n)]
        d, _, _ = run_build(Api({1: rows}), combos((TRACK, 1)))
        c = d["combos"][0]
        assert c["note"] is None or n == 0
        if n == 0:
            assert "No teammate lap" in c["note"] or "No teammate lap" in (c["note"] or ""), c["note"]
        assert d["me"]["name"] == "Swazi Williams"
        assert {m["name"] for m in d["members"]} == set(brand.MEMBERS), d["members"]   # always all three
    # 25 drivers: only the members are on the team's board; the rest are listed apart
    assert len(c["board"]) <= 3 and len(c["academy"]) >= 20, (len(c["board"]), len(c["academy"]))
    assert all(r["name"] in PEOPLE.values() for r in c["board"])
    assert d["h2h"] and all(p["name"] in PEOPLE.values() for p in d["h2h"])      # Academy extras never in head-to-heads


def test_missing_member_named_and_logged():
    rows = [lap("swazi", "Swazi Williams", 140.0), lap("josh", "Josh Spence", 141.2, ir=3100)]
    d, logs, _ = run_build(Api({1: rows}), combos((TRACK, 1)))
    c = d["combos"][0]
    assert any(n.startswith("Kenta Young: no laps came back from Garage 61 in Overdriver Academy") for n in c["mate_notes"]), c["mate_notes"]
    assert any("Garage 61 rows for Kenta Young: 0 laps at this track and car" in l for l in logs), logs     # raw counts in the log
    assert any("Garage 61 rows for Josh Spence: 1 returned, 1 usable" in l for l in logs), logs
    kenta = next(m for m in d["members"] if m["name"] == "Kenta Young")
    assert not kenta["matched"] and "Kenta Young" in kenta["problem"] and "Settings > Team" in kenta["problem"]


def test_matching_ignores_case_and_accents():
    m, left = team.match_members(["Kenta Young", "Josh Spence", "Swazi Williams", "Zoë Brown"],
                                 {"kenta-young": "x", "j": "JOSH SPENCE", "zoe": "Zoe Brown"}, "swazi", "Swazi Williams")
    assert m == {"Kenta Young": "kenta-young", "Josh Spence": "j", "Swazi Williams": "me", "Zoë Brown": "zoe"} and not left, (m, left)
    assert team.match_members(["Nobody Here"], {"a": "A B"})[1] == ["Nobody Here"]


def test_one_combo_failing_keeps_the_rest():
    rows = [lap("swazi", "Swazi Williams", 140.0), lap("josh", "Josh Spence", 141.0, ir=2900)]
    d, logs, _ = run_build(Api({1: rows, 3: rows}, fail_tracks=[3]), combos((TRACK, 1), ("Monza", 3)))
    ok, bad = d["combos"]
    assert ok["board"] and not bad["board"] and bad.get("failed")
    assert bad["note"] == "Garage 61 didn't answer for Monza / Ford Mustang GT4. Try Refresh again later. The other tracks are fine.", bad["note"]
    assert any("RuntimeError" in l for l in logs)


def test_irating_on_every_row_and_template():
    rows = [lap("swazi", "Swazi Williams", 140.0, ir=2700), lap("josh", "Josh Spence", 141.0, ir=2900),
            lap("kenta", "Kenta Young", 142.0, ir=3300), lap("zed", "Zed Other", 139.0, ir=1500)]
    d, _, folder = run_build(Api({1: rows}), combos((TRACK, 1)))
    c = d["combos"][0]
    assert {r["name"]: r["irating"] for r in c["board"]} == {"Swazi Williams": 2700, "Josh Spence": 2900, "Kenta Young": 3300}
    assert c["academy"][0]["name"] == "Zed Other" and c["academy"][0]["irating"] == 1500
    page = team.write(folder, d, unit="mph")
    html = page.read_text(encoding="utf-8")
    assert "iRating" in html and "Midfield Menace" in html and "Join Overdriver Academy" not in html.split("<script>")[0]
    j = json.loads((folder / "team" / "team.json").read_text(encoding="utf-8"))
    assert j["brand"]["join_title"] == "Join Overdriver Academy on Garage 61"


def test_join_words_name_the_group():
    assert brand.join_title("Some Group") == "Join Some Group on Garage 61"
    assert "Overdriver Academy" in brand.JOIN_TEXT and "Midfield Menace" not in brand.JOIN_TEXT
    assert team.team_info()["name"] == "Overdriver Academy"
    assert brand.parse_members(None) == list(brand.MEMBERS) and brand.parse_members("") == []


def test_units_in_learn_text():
    d = {"combos": [{"learn": [{"text": "They carried about 10 mph more through the slowest point."}]}]}
    team._in_unit(d, "kmh")
    assert "16 km/h" in d["combos"][0]["learn"][0]["text"] and d["units"] == "kmh", d
    team._in_unit(d, "mph")
    assert "10 mph" in d["combos"][0]["learn"][0]["text"] and d["units"] == "mph"


def test_stale_and_cache_reuse():
    rows = [lap("swazi", "Swazi Williams", 140.0), lap("josh", "Josh Spence", 141.0)]
    cfg = {"team_name": "Midfield Menace", "team_slug": "overdriver-academy", "team_members": list(brand.MEMBERS)}
    d, _, folder = run_build(Api({1: rows}), combos((TRACK, 1)))
    team.write(folder, d)
    assert team.mark_stale(folder, cfg) is False
    cfg2 = dict(cfg, team_members=["Josh Spence", "Swazi Williams"])
    assert team.mark_stale(folder, cfg2) is True
    saved = json.loads((folder / "team" / "team.json").read_text(encoding="utf-8"))
    assert saved["stale"].startswith("Team settings changed")
    assert "Team settings changed" in (folder / "team" / "index.html").read_text(encoding="utf-8")


def test_prepare_same_rules_and_cap():
    rows = [lap("swazi", "Swazi Williams", 140.0)] + [lap(f"m{i}", f"Member{i} Fast", 141.0 + i) for i in range(15)] \
        + [lap("zed", "Zed Other", 145.0)]
    api = Api({1: rows})
    members = [f"Member{i} Fast" for i in range(15)]
    out = prep._teammates(api, 1, 2, "overdriver-academy", 140.0, lambda m: None, members)
    assert len(out["laps"]) == prep.MAX_TEAMMATES
    assert any("3 slower teammates left out" in n and "Member14 Fast" in n for n in out["notes"]), out["notes"]
    assert any(n.startswith("Also in Overdriver Academy (not on your team): Zed Other") for n in out["notes"]), out["notes"]
    assert all(c.get("age") == -2 for c in api.calls), api.calls     # the Team page's window


def test_corner_cap_note():
    # 6 members, no telemetry sharing: the note says how many corners are compared and who was left out
    names = [f"M{i} Q" for i in range(6)]
    rows = [lap("swazi", "Swazi Williams", 140.0)] + [lap(f"m{i}", names[i], 141.0 + i * 0.1) for i in range(6)]
    d, _, _ = run_build(Api({1: rows}), combos((TRACK, 1)), members=["Swazi Williams"] + names)
    c = d["combos"][0]
    assert "quickest teammates only" in c["kings_note"] and "M5 Q" in c["kings_note"], c.get("kings_note")


def test_teammate_store_per_driver():
    from debrief.desktop import carsetup, carcompare as dcc
    cache = Path(tempfile.mkdtemp())
    cfg = {"cache_dir": str(cache)}
    car, track = "Ford Mustang GT4", "Zolder GP"

    def entry(driver):
        return {"summary": {"driver": driver, "car": car, "track": track, "label": driver + "'s lap", "lap_time": 100.0},
                "livecoach": None, "cornercoach": {"t": 100.0}, "kept": True}
    assert carsetup._team_save(cfg, car, track, entry("Josh Spence"))
    assert carsetup._team_save(cfg, car, track, entry("Kenta Young"))
    assert {carsetup.dkey(r["summary"]) for _f, r, _e in carsetup.kept_files(cfg, car, track)} == {"josh spence", "kenta young"}
    # a pre-2.21 file (one per car and track, no driver in its name) is still found, and moves on the next save
    old = carsetup._team_file(cfg, car, track)
    old.write_text(json.dumps({"version": carsetup.VERSION, "car_path": car, "track_code": track,
                               "summary": entry("Old Mate")["summary"], "livecoach": None, "cornercoach": {"t": 99.0}}))
    assert "old mate" in {carsetup.dkey(r["summary"]) for _f, r, _e in carsetup.kept_files(cfg, car, track)}
    assert carsetup._team_save(cfg, car, track, entry("Old Mate")) and not old.exists()
    assert len(carsetup.kept_files(cfg, car, track)) == 3
    # Car vs car sees all of them, one per teammate
    class S:  # noqa: D401
        pass
    srv = S()
    seen = dcc._teammates(srv, {"cache_dir": str(cache)})
    assert sorted(t["driver"] for t in seen) == ["Josh Spence", "Kenta Young", "Old Mate"], seen
    # forgetting one leaves the others
    carsetup._unlink_kept(cfg, car, track, "josh spence")
    assert len(carsetup.kept_files(cfg, car, track)) == 2


def test_carcompare_bands():
    assert [b[2] for b in carcompare.BANDS] == ["under 2000", "2000 to 3500", "over 3500"]
    assert carcompare.MIN_DRIVERS == 15            # the evidence bar is not lowered


def test_group_self_corrects():
    T = lambda *a: [{"slug": x, "name": n} for x, n in a]
    logs = []
    r = team.resolve_group(T(("overdriver-academy", "Overdriver Academy"), ("other", "O")), "overdriver-academy", logs.append)
    assert r["slug"] == "overdriver-academy" and r["why"] == "the group set in Settings" and logs, r
    r = team.resolve_group(T(("od-acad", "Overdriver  Academy"), ("x", "X")), "wrong-guess")           # name match
    assert r["slug"] == "od-acad" and r["ok"], r
    r = team.resolve_group(T(("midfield-menace", "Midfield Menace"), ("x", "X")), "wrong-guess")       # earlier default
    assert r["slug"] == "midfield-menace" and r["why"].startswith("the earlier default"), r
    r = team.resolve_group(T(("solo", "Solo Team")), "wrong-guess")                                    # only team
    assert r["slug"] == "solo" and r["why"] == "the only team you are in", r
    r = team.resolve_group(T(("a", "A"), ("b", "B")), "wrong-guess")                                   # none fits: never guess
    assert not r["ok"] and r["teams"] == ["A", "B"] and "You are in: A, B." in team.no_group_note(r), r
    assert team.resolve_group(None, "keep-me")["slug"] == "keep-me"                                    # unreadable: keep
    # build uses it: a wrong saved slug does not blank the page
    rows = [lap("swazi", "Swazi Williams", 140.0), lap("kenta", "Kenta Young", 141.0)]
    api = Api({1: rows}, group="midfield-menace")
    folder = Path(tempfile.mkdtemp())
    team.library_combos = lambda f: combos((TRACK, 1))
    d = team.build(api, folder, "overdriver-academy", log=lambda m: None, cfg={"team_members": list(brand.MEMBERS)})
    assert d["group"]["slug"] == "midfield-menace" and d["combos"][0]["board"] and "Laps come from" in d["laps_from"], d["group"]
    api = Api({1: rows}, group="zzz")
    api.teams = lambda refresh=False: [{"slug": "a", "name": "A"}, {"slug": "b", "name": "B"}]
    d = team.build(api, folder, "overdriver-academy", log=lambda m: None, cfg={})
    assert not d["group"]["ok"] and "You are in: A, B." in d["laps_from"] and all(r["is_me"] for r in d["combos"][0]["board"])
    m, t = prep._membership(Api({}, group="midfield-menace"), "overdriver-academy", lambda m: None)
    assert m == brand.MEMBER and t["slug"] == "midfield-menace"


def _laps_for(n_tracks):
    rows = {}
    for i in range(1, n_tracks + 1):
        rows[i] = [lap("swazi", "Swazi Williams", 140.0), lap("josh", "Josh Spence", 141.0 + i, ir=3000),
                   lap("kenta", "Kenta Young", 142.0 + i, ir=3100)]
    return rows


def test_every_combo_listed_lazy_load_and_no_silent_cap():
    folder = Path(tempfile.mkdtemp())
    names = [(f"Track {i}", i) for i in range(1, 10)]
    # his 2 races + 7 more known only from a previous Team page and weekly boards (nothing he has driven)
    team.library_combos = lambda f: combos(*names[:2])
    (folder / "team").mkdir()
    (folder / "team" / "team.json").write_text(json.dumps({"combos": [
        {"track": n, "car": CAR, "track_id": i, "car_id": 2, "last_date": "2026-10-0%d" % (i % 9 + 1), "loaded": False,
         "who": ["Josh Spence", "Kenta Young"], "board": []} for n, i in names[2:6]]}), encoding="utf-8")
    snap = folder / "team" / "weekly" / "s1" / "ser"
    snap.mkdir(parents=True)
    for n, i in names[6:]:
        (snap / f"w{i}.json").write_text(json.dumps({"schema": 1, "track": n, "track_id": i, "fetched_at": "2026-09-01T00:00:00Z",
            "board": [{"key": "josh", "name": "Josh Spence", "car": CAR, "t": 141.0}, {"key": "me", "name": "Swazi Williams", "car": CAR, "t": 140.0}]}), encoding="utf-8")
    api = Api(_laps_for(9))
    cfg = {"team_name": "Midfield Menace", "team_slug": "overdriver-academy", "team_members": list(brand.MEMBERS)}
    d = team.build(api, folder, "overdriver-academy", log=lambda m: None, cfg=cfg, max_combos=6)
    cs = d["combos"]
    assert len(cs) == 9, [c["track"] for c in cs]                       # nothing dropped
    assert sum(c["loaded"] for c in cs) == 6 and d["coverage"]["note"] == "9 tracks \u00b7 6 loaded \u00b7 open a track to load it", d["coverage"]
    un = [c for c in cs if not c["loaded"]]
    assert len(un) == 3 and all(not c["board"] and "Load this track" in c["note"] for c in un)
    assert all("you haven't driven this" in c["who_note"] for c in cs if not c["loaded"])
    assert any(c["who_note"].startswith("Josh, Kenta") for c in cs), [c["who_note"] for c in cs]
    team.write(folder, d)
    n_before = api.requests_made
    pick = un[0]
    d2 = team.load_combo(api, folder, pick["track"], pick["car"], "overdriver-academy", log=lambda m: None, cfg=cfg)
    c2 = next(c for c in d2["combos"] if c["track"] == pick["track"])
    assert c2["loaded"] and c2["board"] and d2["coverage"]["loaded"] == 7, d2["coverage"]
    assert len(d2["combos"]) == 9
    try:
        team.load_combo(api, folder, "Nowhere", CAR, "overdriver-academy", cfg=cfg)
        raise AssertionError("should refuse an unknown track")
    except ValueError:
        pass
    html = team.write(folder, d2).read_text(encoding="utf-8")
    assert "coverage" in html and "who_note" in html


class DiscApi(Api):
    """laps_page with no track: answers with laps at many tracks, or refuses."""
    def __init__(self, *a, mode="ok", **k):
        super().__init__(*a, **k)
        self.mode, self.pages = mode, []

    def laps_page(self, **kw):
        self.pages.append(kw)
        assert kw.get("tracks"), kw
        if self.mode == "400":
            from debrief.g61 import Garage61Error
            raise Garage61Error("track is required", status=400)
        out = []
        for i, who in ((7, ("josh", "Josh Spence")), (8, ("kenta", "Kenta Young")), (9, ("swazi", "Swazi Williams"))):
            l = lap(who[0], who[1], 140.0 + i)
            l["track"] = {"id": i, "name": f"Far Track {i}"}
            l["car"] = {"id": 2, "name": CAR}
            out.append(l)
        return out, len(out)


def test_discovery_merges_and_falls_back():
    cfg = {"team_name": "Midfield Menace", "team_slug": "overdriver-academy", "team_members": list(brand.MEMBERS)}
    team.library_combos = lambda f: combos((TRACK, 1))
    logs = []
    api = DiscApi(_laps_for(9))
    d = team.build(api, Path(tempfile.mkdtemp()), "overdriver-academy", log=logs.append, cfg=cfg)
    tr = {c["track"] for c in d["combos"]}
    assert {"Far Track 7", "Far Track 8", "Far Track 9", TRACK} <= tr, tr           # tracks he never drove
    assert len(api.pages) == 1 and api.pages[0]["teams"] == ["overdriver-academy"]   # ONE request, all tracks
    assert d["discovery"] == "Found 3 tracks where the team has laps on Garage 61.", d["discovery"]
    assert any("Discovery: Josh Spence: 1 laps, 1 track and car combinations" in l for l in logs), logs
    assert any("Discovery: Kenta Young: 1 laps" in l for l in logs) and any("Discovery: Swazi Williams: 1 laps" in l for l in logs)
    far = next(c for c in d["combos"] if c["track"] == "Far Track 7")
    assert far["who_note"].startswith("Josh") and "you haven't driven this" in far["who_note"] or far["loaded"], far
    # a 400 falls back and never fails the refresh
    logs = []
    d = team.build(DiscApi(_laps_for(9), mode="400"), Path(tempfile.mkdtemp()), "overdriver-academy", log=logs.append, cfg=cfg)
    assert any(l.startswith("  Garage 61 team discovery failed: 400") and "track is required" in l for l in logs), logs
    assert d["discovery"].startswith("Garage 61 couldn't list the team's tracks") and d["combos"][0]["board"]


def test_settings_names():
    from debrief.desktop import settings_io
    assert "team_name" in settings_io.public_view() and settings_io.public_view()["team_members"] == list(brand.MEMBERS)


for n, f in list(globals().items()):
    if n.startswith("test_") and callable(f):
        check(n, f)

if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("all passed")
