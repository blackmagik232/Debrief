"""Debrief 2.4 shell (workstream 24-D): report.write extras (strategy, racecraft), layout 2.4.0, the fuel
planner and Track Intel import in the window, team track card settings and team notes, the new race-result
fields, endpoints, feature modules, version and build lists.

    python3 tests/test_24_d.py

The 2.4 data modules (strategy, racecraft, xlsx, intel and the notes additions) belong to 24-A and 24-B.
These tests put small stand-ins with the agreed §2.1 shapes (tests/fixtures_24_d/sample_*.json) in their
place, so they pass before and after the merge; a few tests also run the real modules when they're present.
Never contacts Garage 61: the server is in-process on 127.0.0.1 and the browser checks are skipped without
Playwright.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-24d-"))
LIB = TMP / "lib"
(LIB / "reviews").mkdir(parents=True)
SHARED = TMP / "OneDrive Menace"
SHARED.mkdir()
ST = TMP / "settings.toml"


def write_settings(extra=""):
    ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{LIB}\'\n' + extra, encoding="utf-8")


write_settings()
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import requests  # noqa: E402


def _blocked(*a, **k):
    raise requests.ConnectionError("network blocked in tests")


requests.get = _blocked
requests.post = _blocked

import debrief  # noqa: E402
from debrief import app, library, notes, report, results  # noqa: E402
import debrief.desktop as desktop  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
from debrief.desktop import settings_io  # noqa: E402
import make_fixture_library  # noqa: E402

FX = ROOT / "tests" / "fixtures_24_d"
UI = ROOT / "debrief" / "desktop" / "ui"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
NEW = ("strategy", "racecraft", "xlsx", "intel")


def sample(name):
    return json.loads((FX / f"sample_{name}.json").read_text(encoding="utf-8"))


# ================================================================ stand-in modules (§2.1 shapes)
def _load_real_strategy():
    """The real strategy module under a private name, so modules() can still swap debrief.strategy out."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("debrief._real_strategy_for_24d", ROOT / "debrief" / "strategy.py")
    m = importlib.util.module_from_spec(spec)
    m.__package__ = "debrief"
    spec.loader.exec_module(m)
    return m


_REAL_STRATEGY = _load_real_strategy()


def stub_plan(inp):
    """strategy.plan: the real one (2.4.0 review H2: a hand-made stand-in let the window and the planner
    disagree on the thirstier-laps field name, so the shell now tests against the planner itself)."""
    return _REAL_STRATEGY.plan(inp)


class XlsxError(Exception):
    pass


@contextmanager
def modules(calls=None, fail=(), missing=(), notes_extra=True, shared=None):
    """Put stand-ins for the 2.4 modules in sys.modules (and the notes additions on notes). calls records the
    call order; a name in fail raises; a name in missing can't be imported at all."""
    calls = calls if calls is not None else []
    st = {"cars": {}}

    def mk(name, **fns):
        m = types.ModuleType("debrief." + name)
        m.VERSION = 1
        for k, f in fns.items():
            def wrap(*a, _f=f, _k=k, **kw):
                calls.append((name + "." + _k, a, kw))
                if name in fail:
                    raise RuntimeError(name + " broke")
                return _f(*a, **kw)
            setattr(m, k, wrap)
        return m

    def save_car(folder, car, tank_l=None, pit_loss_s=None, track=None):
        c = st["cars"].setdefault(car, {"tank_l": None, "pit_loss_s": {}})
        if tank_l is not None:
            c["tank_l"] = tank_l
        if pit_loss_s is not None:
            c["pit_loss_s"][notes.track_key(track)] = pit_loss_s
        return c

    def parse_upload(data, filename):
        if filename.lower().endswith(".xlsx"):
            if b"BROKEN" in data:
                raise XlsxError("That workbook is damaged. Save it again and try again.")
            return {"sheets": [{"name": "Tracks", "header": ["Track", "Crash spots", "Overtaking"],
                                "rows": [["Roval", "T1 on lap 1", "Back straight"]] * 12,
                                "suggest": {0: "track", 1: "hotspots", 2: "passing"}},
                               {"name": "Other", "header": ["A"], "rows": [["x"]], "suggest": {}}]}
        rows = [r.split(",") for r in data.decode("utf-8-sig").splitlines() if r.strip()]
        return {"sheets": [{"name": "CSV", "header": rows[0], "rows": rows[1:],
                            "suggest": {0: "track"} if rows else {}}]}

    def preview(parsed, sheet, mapping, mode, cfg, known):
        return {"tracks": [{"track": "Roval", "matched": "Charlotte Motor Speedway (Roval 2019)",
                            "existing": {}, "incoming": {"hotspots": "T1 on lap 1"},
                            "result": {"hotspots": "T1 on lap 1"}, "changes": ["hotspots"],
                            "warnings": ["Passing spots was cut to 300 characters."]},
                           {"track": "Nowhere Ring", "matched": None, "existing": {}, "incoming": {},
                            "result": {}, "changes": [], "warnings": []}]}

    def apply(parsed, sheet, mapping, mode, cfg, known):
        return {"tracks": 12, "changed": 9, "skipped": 3}

    fake = {"strategy": mk("strategy", block_from_review=lambda d: sample("strategy"),
                           review_block=lambda *a: None, prefill=lambda *a: copy.deepcopy(sample("prefill")),
                           plan=stub_plan, load_cars=lambda f: {"schema": 1, "cars": st["cars"]}, save_car=save_car),
            "racecraft": mk("racecraft", build=lambda d, raw_laps=None: sample("racecraft")),
            "xlsx": mk("xlsx", read=lambda *a, **k: {"sheets": []}),
            "intel": mk("intel", parse_upload=parse_upload, preview=preview, apply=apply)}
    fake["xlsx"].XlsxError = XlsxError
    saved = {n: sys.modules.get("debrief." + n, Ellipsis) for n in NEW}
    saved_attr = {n: getattr(debrief, n, Ellipsis) for n in NEW}
    notes_saved = {k: getattr(notes, k, Ellipsis) for k in ("shared_for", "shared_root", "author_from_cache", "publish")}
    try:
        for n in NEW:
            sys.modules["debrief." + n] = None if n in missing else fake[n]
            if n in missing:
                if hasattr(debrief, n):
                    delattr(debrief, n)
            else:
                setattr(debrief, n, fake[n])
        if notes_extra:
            notes.shared_root = lambda cfg: (Path(cfg["team_cards_folder"]) / "Debrief track cards"
                                             if cfg.get("team_cards_folder") else None)
            notes.shared_for = lambda cfg, track, me: copy.deepcopy(shared if shared is not None else sample("shared"))
            notes.author_from_cache = lambda cfg: {"slug": "alex-driver", "name": "Alex Driver"}
            notes.publish = lambda cfg, card, author: False
        yield calls
    finally:
        for n in NEW:
            if saved[n] is Ellipsis:
                sys.modules.pop("debrief." + n, None)
            else:
                sys.modules["debrief." + n] = saved[n]
            if saved_attr[n] is Ellipsis:
                if hasattr(debrief, n):
                    delattr(debrief, n)
            else:
                setattr(debrief, n, saved_attr[n])
        for k, v in notes_saved.items():
            if v is Ellipsis:
                if hasattr(notes, k):
                    delattr(notes, k)
            else:
                setattr(notes, k, v)


_OLD = {}


def old_review():
    """A 2.2.1-format review (make_fixture_library) with none of the 2.4 keys."""
    if "d" not in _OLD:
        import random
        _OLD["d"] = make_fixture_library.fake_review(make_fixture_library.ROVAL, "2026-10-01T23:30:00+00:00", 0.4,
                                                     True, make_fixture_library.S4, 1100, None, random.Random(7))
    d = copy.deepcopy(_OLD["d"])
    for k in ("strategy", "racecraft", "insights"):
        d.pop(k, None)
    return d


def written(folder):
    return json.loads((Path(folder) / "review.json").read_text(encoding="utf-8"))


# ================================================================ report.write extras
def test_write_old_review_adds_strategy_and_racecraft():
    out = Path(tempfile.mkdtemp(dir=TMP))
    with modules() as calls:
        report.write(out, old_review())
    d = written(out)
    assert d["strategy"] == sample("strategy") and d["racecraft"] == sample("racecraft")
    names = [c[0] for c in calls]
    assert "strategy.block_from_review" in names and "racecraft.build" in names
    rc = next(c for c in calls if c[0] == "racecraft.build")
    assert len(rc[1]) == 1 and not rc[2]                    # build(data): no raw laps for an old review
    html = (out / "index.html").read_text(encoding="utf-8")
    assert '"racecraft":' in html


def test_write_keeps_engine_values():
    """A new review already carries strategy/racecraft from the engine: report.write leaves them alone."""
    out = Path(tempfile.mkdtemp(dir=TMP))
    d = old_review()
    d["strategy"] = {"version": 1, "burn": None, "level": None, "pit": {"laps": [], "loss_s": 49.0, "n": 1}, "note": None}
    d["racecraft"] = {"version": 1, "lap1": None, "timeline": [], "summary": ["engine"]}
    with modules() as calls:
        report.write(out, d)
    w = written(out)
    assert w["strategy"]["pit"]["loss_s"] == 49.0 and w["racecraft"]["summary"] == ["engine"]
    assert not [c for c in calls if c[0] in ("strategy.block_from_review", "racecraft.build")]


def test_extras_run_before_insights():
    seen = {}
    from debrief import insights
    real = insights.build

    def spy(data):
        seen.update(strategy="strategy" in data, racecraft="racecraft" in data)
        return real(data)
    insights.build = spy
    try:
        with modules():
            report.write(Path(tempfile.mkdtemp(dir=TMP)), old_review())
    finally:
        insights.build = real
    assert seen == {"strategy": True, "racecraft": True}, seen


def test_failing_or_missing_extra_drops_only_its_key():
    out = Path(tempfile.mkdtemp(dir=TMP))
    logs = []
    with modules(fail={"racecraft"}):
        report.write(out, old_review(), log=logs.append)
    d = written(out)
    assert "racecraft" not in d and d["strategy"] == sample("strategy") and d.get("laps")
    assert any("racecraft" in m for m in logs), logs
    out2 = Path(tempfile.mkdtemp(dir=TMP))
    logs = []
    with modules(missing={"strategy", "racecraft"}):
        report.write(out2, old_review(), log=logs.append)
    d = written(out2)
    assert "strategy" not in d and "racecraft" not in d and "insights" in d
    assert not logs, logs                                          # a copy without the modules is quiet


def test_none_result_leaves_key_out():
    out = Path(tempfile.mkdtemp(dir=TMP))
    with modules() as _:
        sys.modules["debrief.strategy"].block_from_review = lambda d: None
        report.write(out, old_review())
    assert "strategy" not in written(out)


def test_layout_is_240_and_refresh_adds_keys():
    assert tuple(int(x) for x in report.LAYOUT.split(".")) >= (2, 4, 0)   # 2.4.0 or later (2.5 bumped it)
    lib = Path(tempfile.mkdtemp(dir=TMP))
    for i in range(2):
        out = lib / "reviews" / f"2026-10-0{i + 1}_r{i}"
        out.mkdir(parents=True)
        (out / "review.json").write_text(json.dumps(old_review()), encoding="utf-8")
    (lib / ".layout").write_text("2.3.0", encoding="utf-8")
    with modules():
        n = report.refresh_all(lib)
        assert n == 2 and (lib / ".layout").read_text(encoding="utf-8").strip() == report.LAYOUT
        assert report.refresh_all(lib) == 0                       # once per layout
    for p in (lib / "reviews").glob("*/review.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["strategy"]["burn"]["n"] == 17 and d["racecraft"]["lap1"]["note"] == "includes the start"


def test_real_modules_when_present():
    import importlib
    have = []
    for n in ("strategy", "racecraft"):
        try:
            importlib.import_module("debrief." + n)
            have.append(n)
        except ImportError:
            pass
    if len(have) < 2:
        print("  SKIPPED: 2.4 analysis modules not in this copy yet")
        return
    out = Path(tempfile.mkdtemp(dir=TMP))
    report.write(out, old_review())
    d = written(out)
    assert d.get("laps") and "insights" in d


# ================================================================ race result fields
def test_clean_result_fields_new_fields():
    f = S.clean_result_fields({"finish_pos": 5, "start_pos": 9, "field_size": 20, "pos_after_lap1": "7",
                               "pit_stops": 1, "penalties": 0, "finish_status": "running",
                               "incident_laps": [{"lap": 9, "kind": "contact", "points": 4},
                                                 {"lap": 1, "kind": "off", "points": "1"}]})
    assert f["pos_after_lap1"] == 7 and f["pit_stops"] == 1 and f["penalties"] == 0 and f["finish_status"] == "running"
    assert f["incident_laps"] == [{"lap": 1, "kind": "off", "points": 1}, {"lap": 9, "kind": "contact", "points": 4}]
    base = {"finish_pos": 5, "field_size": 20}
    bad = [({"pos_after_lap1": 0}, "Position after lap 1"), ({"pos_after_lap1": 21}, "number of cars"),
           ({"pos_after_lap1": 201, "field_size": None}, "between"), ({"pit_stops": 51}, "Pit stops"),
           ({"pit_stops": -1}, "Pit stops"), ({"penalties": 1.5}, "whole"), ({"penalties": True}, "number"),
           ({"finish_status": "crashed"}, "how the race ended"), ({"incident_laps": "lap 3"}, "list"),
           ({"incident_laps": [{"lap": 3, "kind": "off", "points": 0}] * 31}, "30"),
           ({"incident_laps": [{"lap": 3, "kind": "bump", "points": 0}]}, "contact"),
           ({"incident_laps": [{"lap": 3, "kind": "off", "points": 3}]}, "0, 1, 2 or 4"),
           ({"incident_laps": [{"lap": 0, "kind": "off", "points": 0}]}, "lap"),
           ({"incident_laps": [{"lap": 2001, "kind": "off", "points": 0}]}, "lap"),
           ({"incident_laps": [{"lap": float("nan"), "kind": "off", "points": 0}]}, "lap"),
           ({"incident_laps": ["x"]}, "row 1")]
    for extra, want in bad:
        body = dict(base)
        body.update({k: v for k, v in extra.items() if v is not None})
        if extra.get("field_size", 1) is None:
            body.pop("field_size")
        try:
            S.clean_result_fields(body)
        except ValueError as e:
            assert want.lower() in str(e).lower(), (extra, str(e))
        else:
            raise AssertionError(f"accepted {extra}")
    assert len(S.clean_result_fields({"finish_pos": 1, "incident_laps": [{"lap": 3, "kind": "off", "points": 0}] * 30})
               ["incident_laps"]) == 30


def test_result_with_incident_laps_round_trips():
    """POST api/result with the new fields, then GET it back (results.save with or without 24-B's fields)."""
    _fill_lib()
    d = sorted(p.parent.name for p in (LIB / "reviews").glob("*/review.json"))[0]
    fields = {"finish_pos": 4, "start_pos": 8, "field_size": 18, "pos_after_lap1": 6, "pit_stops": 1, "penalties": 1,
              "finish_status": "dnf", "incident_laps": [{"lap": 3, "kind": "spin", "points": 2}]}
    real_validate = results.validate
    if "incident_laps" not in getattr(results, "FIELDS", {}):
        # before 24-B's merge results.validate drops unknown keys: stand in for the agreed behaviour
        def validate(f):
            out = real_validate(f)
            out.update({k: f[k] for k in ("pos_after_lap1", "pit_stops", "penalties", "finish_status", "incident_laps") if k in f})
            if "start_pos" in out and "pos_after_lap1" in out:
                out["positions_lap1"] = out["start_pos"] - out["pos_after_lap1"]
            return out
        results.validate = validate
    try:
        code, _, body = post("api/result", {"review": d, "action": "save", "fields": fields})
    finally:
        results.validate = real_validate
    j = json.loads(body)
    assert code == 200 and j["ok"], j
    code, _, body = get("api/result?review=" + d)
    r = json.loads(body)["result"]
    assert r["incident_laps"] == [{"lap": 3, "kind": "spin", "points": 2}] and r["finish_status"] == "dnf"
    assert r["pos_after_lap1"] == 6 and r["positions_lap1"] == 2
    rev = json.loads((LIB / "reviews" / d / "review.json").read_text(encoding="utf-8"))
    assert rev["result"]["incident_laps"][0]["kind"] == "spin"
    rows = library.list_reviews(LIB)
    row = next(x for x in rows if (x.get("result") or {}).get("pit_stops") == 1)
    assert "incident_laps" not in row["result"] and row["result"]["positions_lap1"] == 2
    code, _, body = post("api/result", {"review": d, "action": "save",
                                        "fields": dict(fields, incident_laps=[{"lap": 3, "kind": "x", "points": 0}])})
    assert code == 400 and "contact" in json.loads(body)["error"]
    post("api/result", {"review": d, "action": "delete"})


# ================================================================ settings: team track cards
def test_settings_team_cards_folder():
    write_settings()
    try:
        settings_io.save({"team_cards_folder": str(TMP / "nope")})
        raise AssertionError("missing folder accepted")
    except ValueError as e:
        assert str(e) == "That folder doesn't exist. Pick the shared folder your team uses."
    for bad in ("x" * 401, 5, "C:\\a|b"):
        try:
            settings_io.save({"team_cards_folder": bad})
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    try:
        settings_io.save({"share_my_cards": "yes"})
        raise AssertionError("string bool accepted")
    except ValueError:
        pass
    v = settings_io.save({"team_cards_folder": str(SHARED), "share_my_cards": True})
    assert v["team_cards_folder"] == str(SHARED) and v["team_cards_folder_ok"] and v["share_my_cards"] is True
    cfg = app.load_settings()
    assert cfg["team_cards_folder"] == str(SHARED) and cfg["share_my_cards"] is True
    # ~ and env vars are expanded for the check and in cfg; the typed form is what's kept
    os.environ["DEBRIEF_TEST_SHARE"] = str(SHARED)
    var = "%DEBRIEF_TEST_SHARE%" if os.name == "nt" else "$DEBRIEF_TEST_SHARE"
    v = settings_io.save({"team_cards_folder": var})
    assert v["team_cards_folder"] == var and v["team_cards_folder_ok"]
    assert app.load_settings()["team_cards_folder"] == str(SHARED)
    v = settings_io.save({"team_cards_folder": "", "share_my_cards": False})
    assert v["team_cards_folder"] is None and v["share_my_cards"] is False
    assert app.load_settings()["team_cards_folder"] == ""
    ex = (ROOT / "settings.example.toml").read_text(encoding="utf-8")
    assert "# team_cards_folder =" in ex and "# share_my_cards = false" in ex
    app._parse_toml(ex)
    write_settings()


def test_settings_public_view_shares_as_cached_name():
    write_settings(f"team_cards_folder = '{SHARED}'\nshare_my_cards = true\n")
    try:
        with modules():
            v = settings_io.public_view()
            assert v["team_cards_folder_ok"] and v["share_my_cards"]
            assert v["share_as"] is None            # no token saved: no name, and no request either
    finally:
        write_settings()


# ================================================================ server helpers
_SRV = {}


def srv():
    if "s" not in _SRV:
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def get(rel, headers=None):
    rq = urllib.request.Request(srv().url + rel, headers=headers if headers is not None else {"X-Debrief": "1"})
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def post(rel, body, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    h = {"X-Debrief": "1", "Content-Type": "application/json"} if headers is None else headers
    rq = urllib.request.Request(srv().url + rel, data=data, method="POST", headers=h)
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _fill_lib():
    if not any((LIB / "reviews").iterdir()):
        make_fixture_library.main(str(LIB))


# ================================================================ api/strategy
def test_api_strategy_prefill_and_plan():
    with modules() as calls:
        code, _, body = get("api/strategy?track=" + urllib.request.quote("Charlotte Roval") + "&car=Ford%20Mustang%20GT4")
        j = json.loads(body)
        assert code == 200 and j["ok"] and j["prefill"]["burn"]["value"] == 2.5, j
        pf = next(c for c in calls if c[0] == "strategy.prefill")
        cfg, store, sched, track, cars, week = pf[1]
        assert track == "Charlotte Roval" and cars == ["Ford Mustang GT4"] and store is None and week is None
        code, _, body = get("api/strategy?track=Roval")
        assert code == 400 and json.loads(body)["error"] == "Pick a car."
        assert get("api/strategy?track=Roval&car=x", headers={})[0] == 404
        # plan: 15 laps → no stop; 40 laps → 2 stops with windows
        code, _, body = post("api/strategy", {"action": "plan", "inputs": {"laps": 15, "burn_l": 2.5, "tank_l": 47, "reserve_laps": 1}})
        p = json.loads(body)["plan"]
        assert code == 200 and p["stops"] == 0 and p["fuel_needed_l"] == 40.0
        code, _, body = post("api/strategy", {"action": "plan", "inputs": {"laps": 40, "burn_l": 2.5, "tank_l": 47}})
        p = json.loads(body)["plan"]
        assert p["stops"] == 2 and all(w["close"] >= w["open"] for w in p["windows"])
        code, _, body = post("api/strategy", {"action": "plan", "inputs": {"laps": 40, "burn_l": 30, "tank_l": 47}})
        assert code == 400 and "between" in json.loads(body)["error"]
        code, _, body = post("api/strategy", {"action": "plan", "inputs": {"laps": 4, "burn_l": 10, "tank_l": 15}})
        assert code == 400 and json.loads(body)["error"] == "The tank is too small for one lap at that fuel use."
        for bad in ({"action": "plan"}, {"action": "plan", "inputs": [1]}, {"action": "nuke"}):
            code, _, body = post("api/strategy", bad)
            assert code == 400 and json.loads(body)["ok"] is False
        # save_car
        code, _, body = post("api/strategy", {"action": "save_car", "car": "Ford Mustang GT4", "tank_l": 47.5})
        assert code == 200 and json.loads(body)["car"]["tank_l"] == 47.5
        code, _, body = post("api/strategy", {"action": "save_car", "car": "Ford Mustang GT4", "pit_loss_s": 50,
                                              "track": "Charlotte Roval"})
        assert code == 200
        for bad in ({"car": "Ford", "tank_l": 0}, {"car": "Ford", "tank_l": 999}, {"car": "Ford", "pit_loss_s": 50},
                    {"car": "", "tank_l": 40}, {"car": "Ford"}, {"car": "<b>", "tank_l": 40},
                    {"car": "Ford", "tank_l": True}):
            code, _, body = post("api/strategy", dict(bad, action="save_car"))
            assert code == 400, (bad, body)
    with modules(missing={"strategy"}):
        code, _, body = get("api/strategy?track=Roval&car=x")
        assert code == 400 and "isn't available" in json.loads(body)["error"]


def test_api_strategy_makes_no_requests():
    """The prefill gets the sync store, never an API client (and the Garage61 class is never asked anything)."""
    from debrief import g61
    hits = []
    orig = g61.Garage61._get
    g61.Garage61._get = lambda self, *a, **k: hits.append(a) or (_ for _ in ()).throw(AssertionError("request"))
    try:
        with modules():
            assert get("api/strategy?track=Roval&car=Ford")[0] == 200
    finally:
        g61.Garage61._get = orig
    assert not hits


# ================================================================ api/notes: team notes
def test_api_notes_adds_shared_and_sharing():
    write_settings(f"team_cards_folder = '{SHARED}'\nshare_my_cards = true\n")
    try:
        with modules():
            j = json.loads(get("api/notes?track=Roval")[2])
            assert j["ok"] and j["shared"][0]["author"]["slug"] == "josh-spence"
            assert j["sharing"] == {"on": True, "folder_ok": True, "author": {"slug": "alex-driver", "name": "Alex Driver"},
                                    "folder": str(SHARED), "available": True}, j["sharing"]
        with modules(notes_extra=False):          # notes without 24-B's additions: no team notes, no error
            if not hasattr(notes, "shared_for"):
                j = json.loads(get("api/notes?track=Roval")[2])
                assert j["ok"] and j["shared"] == [] and j["sharing"]["available"] is False
        with modules():
            notes.shared_for = lambda *a: (_ for _ in ()).throw(OSError("drive offline"))
            j = json.loads(get("api/notes?track=Roval")[2])
            assert j["ok"] and j["shared"] == []
    finally:
        write_settings()
    with modules():
        j = json.loads(get("api/notes?track=Roval")[2])
        assert j["shared"] == [] and j["sharing"]["on"] is False and j["sharing"]["folder_ok"] is False


# ================================================================ Track Intel upload / preview / import
def xlsx_bytes(extra=b""):
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>" + "x" * 2000)
        z.writestr("xl/workbook.xml", "<workbook/>")
    return b.getvalue() + extra


def upload(data, name="intel.xlsx", ctype="application/octet-stream"):
    return post("api/intel-upload?name=" + urllib.request.quote(name), None, raw=data,
                headers={"X-Debrief": "1", "Content-Type": ctype})


def raw_headers_only(rel, length):
    """POST with a Content-Length but only part of the body, then read the reply (http.client)."""
    import http.client
    s = srv()
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    c.putrequest("POST", f"/{s.secret}/{rel}")
    for k, v in (("X-Debrief", "1"), ("Content-Type", "application/octet-stream"), ("Content-Length", str(length))):
        c.putheader(k, v)
    c.endheaders()
    if length <= 5 * 1024 * 1024:
        c.send(b"PK\x03\x04" + b"\0" * 2000)
        c.sock.shutdown(1)   # the client stops sending (socket.SHUT_WR, also on Windows)
    r = c.getresponse()
    out = (r.status, {k: v for k, v in r.getheaders()}, r.read())
    c.close()
    return out


def test_intel_upload_checks():
    with modules():
        code, _, body = upload(xlsx_bytes())
        j = json.loads(body)
        assert code == 200 and j["ok"] and len(j["upload_id"]) >= 16
        sh = j["sheets"][0]
        assert sh["header"][0] == "Track" and len(sh["rows"]) == 10 and sh["n_rows"] == 12
        assert sh["suggest"] == {"0": "track", "1": "hotspots", "2": "passing"}
        cases = [(xlsx_bytes(), "intel.xls", "application/octet-stream", "Pick an .xlsx or .csv"),
                 (xlsx_bytes(), "intel.xlsx", "application/json", "Pick an .xlsx or .csv"),
                 (xlsx_bytes(), "../x.xlsx", "application/octet-stream", "Pick an .xlsx or .csv"),
                 (b"PK\x03\x04" + b"\0" * 100, "intel.xlsx", "application/octet-stream", "too small"),
                 (b"\xd0\xcf\x11\xe0" + b"\0" * 2000, "intel.xlsx", "application/octet-stream", "old .xls"),
                 (b"<html>" + b"x" * 2000, "intel.xlsx", "application/octet-stream", "isn't a real .xlsx"),
                 (xlsx_bytes(), "intel.csv", "application/octet-stream", "isn't a CSV"),
                 (xlsx_bytes(b"BROKEN"), "intel.xlsx", "application/octet-stream", "damaged")]
        for data, name, ct, want in cases:
            code, _, body = upload(data, name, ct)
            assert code == 400 and want in json.loads(body)["error"], (name, body)
        code, _, body = upload(b"a,b\n", "intel.csv")
        assert code == 400 and "too small" in json.loads(body)["error"]          # a near-empty CSV is refused
        csv_ok = ("Track,Notes\n" + "".join(f"Roval {i},Brake early into T1 and keep it tidy\n" for i in range(30))).encode("utf-8")
        assert len(csv_ok) >= 1024
        code, _, body = upload(csv_ok, "intel.csv")
        assert code == 200 and json.loads(body)["sheets"][0]["header"] == ["Track", "Notes"]
        # over 5 MB: refused from the Content-Length alone (the body is never read, the connection closes)
        code, h, body = raw_headers_only("api/intel-upload?name=big.xlsx", 5 * 1024 * 1024 + 1)
        assert code == 400 and "too big" in json.loads(body)["error"] and h.get("Connection") == "close"
        code, h, body = raw_headers_only("api/intel-upload?name=ok.xlsx", 5 * 1024 * 1024)
        assert code == 400 and "didn't arrive" in json.loads(body)["error"]   # 5 MB exactly is allowed in
        code, _, _ = post("api/intel-upload?name=a.xlsx", None, raw=xlsx_bytes(), headers={"Content-Type": "application/octet-stream"})
        assert code == 404                                      # needs the window's header


def test_intel_uploads_cap_and_expiry():
    up = S.IntelUploads()
    now = [1000.0]
    up.clock = lambda: now[0]
    ids = [up.add({"sheets": []}, f"{i}.csv") for i in range(4)]
    assert len(up.items) == 3 and ids[0] not in up.items        # at most 3: the oldest goes
    up.get(ids[3])
    now[0] += 30 * 60 + 1
    try:
        up.get(ids[3])
        raise AssertionError("expired upload served")
    except ValueError as e:
        assert "expired" in str(e)
    for bad in (None, 5, "../../etc", "x" * 100):
        try:
            up.get(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_intel_preview_and_import():
    with modules() as calls:
        uid = json.loads(upload(xlsx_bytes())[2])["upload_id"]
        good = {"upload_id": uid, "sheet": 0, "mapping": {"0": "track", "1": "hotspots", "2": "passing"}, "mode": "fill_empty"}
        code, _, body = post("api/intel-preview", good)
        j = json.loads(body)
        assert code == 200 and j["preview"]["tracks"][0]["changes"] == ["hotspots"]
        pv = next(c for c in calls if c[0] == "intel.preview")
        assert pv[1][1] == 0 and pv[1][2] == {0: "track", 1: "hotspots", 2: "passing"} and pv[1][3] == "fill_empty"
        assert isinstance(pv[1][5], list)                              # known tracks
        bad = [dict(good, mapping={}), dict(good, mapping={"0": "hotspots"}),           # no track column
               dict(good, mapping={"0": "track", "1": "track"}), dict(good, mapping={"9": "track"}),
               dict(good, mapping={"0": "track", "1": "password"}), dict(good, mapping={"-1": "track"}),
               dict(good, mapping=["track"]), dict(good, sheet=5), dict(good, sheet=True), dict(good, sheet="0"),
               dict(good, mode="merge"), dict(good, upload_id="nope-nope-nope"), dict(good, upload_id=None)]
        for b in bad:
            code, _, body = post("api/intel-preview", b)
            assert code == 400 and json.loads(body)["ok"] is False, (b, body)
        # import needs confirm:true
        code, _, body = post("api/intel-import", good)
        assert code == 400 and "Import" in json.loads(body)["error"]
        assert not [c for c in calls if c[0] == "intel.apply"]
        code, _, body = post("api/intel-import", dict(good, confirm="yes"))
        assert code == 400
        code, _, body = post("api/intel-import", dict(good, confirm=True, mode="replace"))
        j = json.loads(body)
        assert code == 200 and j == {"ok": True, "tracks": 12, "changed": 9, "skipped": 3}, j
        # the upload is dropped after the import
        code, _, body = post("api/intel-preview", good)
        assert code == 400 and "expired" in json.loads(body)["error"]


def test_intel_expired_upload_id():
    s = srv()
    with modules():
        uid = json.loads(upload(xlsx_bytes())[2])["upload_id"]
        orig = s.intel.clock
        s.intel.clock = lambda: orig() + 31 * 60
        try:
            code, _, body = post("api/intel-preview", {"upload_id": uid, "sheet": 0, "mapping": {"0": "track"}})
        finally:
            s.intel.clock = orig
        assert code == 400 and json.loads(body)["error"] == "That upload has expired. Pick the file again."


def test_intel_upload_keeps_connection_in_step():
    """A refused upload on a kept-alive connection never lets its unread body become the next request."""
    import http.client
    s = srv()
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    c.putrequest("POST", f"/{s.secret}/api/intel-upload?name=a.txt")
    c.putheader("X-Debrief", "1")
    c.putheader("Content-Type", "application/octet-stream")
    c.putheader("Content-Length", "3000")
    c.endheaders()
    c.send(b"GET /x HTTP/1.1\r\n\r\n" + b"x" * 2980)
    r = c.getresponse()
    assert r.status == 400 and r.getheader("Connection") == "close"
    r.read()
    c.close()


# ================================================================ prep page: fuel box data
def test_prep_strategy_box():
    with modules() as calls:
        out = app.prep_strategy({"library_folder": str(LIB)}, None, {"track": "Roval", "car": "Ford"})
        assert out["strategy"]["race"]["laps"] == 40
    with modules(fail={"strategy"}):
        assert app.prep_strategy({"library_folder": str(LIB)}, None, {"track": "Roval", "car": "Ford"}) == {}
    with modules(missing={"strategy"}):
        assert app.prep_strategy({"library_folder": str(LIB)}, None, {"track": "Roval", "car": "Ford"}) == {}
    src = (ROOT / "debrief" / "app.py").read_text(encoding="utf-8")
    body = src[src.index("def cmd_prep("):src.index("def cmd_team(")]
    assert body.index("prep_strategy(") < body.index("prep.write(")


# ================================================================ modules, version, build lists
def test_feature_modules_spec_ci_version():
    for n in NEW:
        assert n in desktop.FEATURE_MODULES, n
    spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
    must = spec[spec.index("for _must in"):spec.index("if _must not in")]
    for n in NEW:
        assert f'"debrief.{n}"' in must, n
    ci = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8").replace("python tests/ci_run.py tests/", "python tests/")
    sys.path.insert(0, str(ROOT / "tests"))
    from run_all import ci_runs              # each file has its own step, or run_all.py runs them all
    for x in "abcd":
        assert ci_runs(ci, f"test_24_{x}.py") and ci_runs(ci, f"test_23_{x}.py"), x
    assert ci_runs(ci, "test_230_fixes.py")
    assert tuple(int(x) for x in debrief.__version__.split(".")) >= (2, 4, 0)
    for k in ("pos_after_lap1", "pit_stops", "penalties", "positions_lap1"):
        assert k in library._RESULT_KEYS


def test_selftest_passes_with_new_modules():
    """The real selftest, with the 2.4 modules stood in when a copy doesn't have them yet."""
    import io
    import contextlib
    import importlib
    home = Path(tempfile.mkdtemp(prefix="home", dir=TMP))
    old = app.HERE
    app.HERE = home
    missing = []
    for n in NEW:
        try:
            importlib.import_module("debrief." + n)
        except ImportError:
            missing.append(n)
    buf = io.StringIO()
    try:
        ctx = modules() if missing else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(buf):
            rc = desktop.selftest()
    finally:
        app.HERE = old
    res = json.loads((home / "selftest.json").read_text(encoding="utf-8"))
    assert rc == 0 and res["ok"], res
    assert res["version"] == debrief.__version__


def test_window_security_model_unchanged():
    code, h, body = get("", headers={})
    csp = h["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "connect-src 'self'" in csp
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "eval(" not in js and "new Function" not in js and "innerHTML" not in js and "insertAdjacentHTML" not in js
    assert f'sandbox="{S.FRAME_SANDBOX}"' in html and "allow-same-origin" not in S.FRAME_SANDBOX
    _fill_lib()
    code, h, body = get("lib/index.html", headers={})
    assert code == 200 and h["Content-Security-Policy"] == S.LIB_CSP and "connect-src 'none'" in S.LIB_CSP


# ================================================================ app window (Playwright)
_PW = {}


def _browser():
    if "b" in _PW:
        return _PW["b"]
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b = pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        _PW["b"] = None
        return None
    _PW["pw"], _PW["b"] = pw, b
    return b


WEEK = {"week": 7, "start": "2026-10-27", "track": "Charlotte Motor Speedway (Roval 2019)", "length": "40 laps",
        "race_time": None, "conditions": None, "history": {"sessions": 3, "races": 2, "best": 85.4, "last": "2026-10-01"},
        "track_id": 1, "car_id": 2, "card": {"fields": {}, "car_notes": {}}, "has_notes": False, "shared_notes": 1}
PREPARE = {"ok": True, "schedule": {"season": "2026 Season 4", "series": 150, "weeks": 12, "first_day": "2026-09-15",
                                    "last_day": "2026-12-07", "current_week": 7, "out_of_date": False, "warnings": []},
           "chosen": True, "current_week": 7, "suggestions": [], "all": [], "token": False,
           "mine": [{"id": "gt4-falken", "name": "GT4 Falken Tyre Challenge", "missing": False, "category": "Sports Car",
                     "cars": ["Ford Mustang GT4"], "classes": ["GT4"], "multiclass": False, "setup": "fixed",
                     "this_week": WEEK, "next_week": None, "season": []}]}


def _window():
    b = _browser()
    if not b:
        return None
    _fill_lib()
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "net::ERR_FAILED" not in m.text else None)
    pg.on("requestfailed", lambda r: errs.append("failed " + r.url + " " + str(r.failure))
          if "fonts.g" not in r.url and "ERR_ABORTED" not in str(r.failure) else None)
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    pg.route("**/fonts.gstatic.com/**", lambda r: r.abort())
    # the Prepare page needs a loaded season schedule: serve a fixed one; everything else is the real server
    pg.route("**/api/prepare", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(PREPARE)))
    pg.goto(srv().url)
    pg.wait_for_function("() => document.querySelectorAll('#revlist .rev').length > 0", timeout=15000)
    return pg, ctx, errs


def test_window_fuel_dialog_40_laps():
    with modules():
        r = _window()
        if not r:
            return
        pg, ctx, errs = r
        try:
            pg.click(".navbtn[data-nav=prep]")
            pg.wait_for_function("() => document.querySelector('#mySeries button[data-fuel]') !== null", timeout=10000)
            pg.click("#mySeries button[data-fuel]")
            pg.wait_for_function("() => document.getElementById('dlgFuel').open", timeout=5000)
            pg.wait_for_function("() => document.getElementById('f_burn').value === '2.50'", timeout=10000)
            vals = pg.evaluate("() => ({laps: document.getElementById('f_laps').value, lap: document.getElementById('f_lap_t').value,"
                               " tank: document.getElementById('f_tank').value, pit: document.getElementById('f_pit').value,"
                               " stank: document.getElementById('s_tank').textContent, sburn: document.getElementById('s_burn').textContent,"
                               " cls: document.getElementById('s_tank').className, nob: document.getElementById('fuelNoBurn').hidden,"
                               " p90: !document.getElementById('fP90Wrap').hidden})")
            assert vals["laps"] == "40" and vals["lap"] == "1:25.900" and vals["tank"] == "47" and vals["pit"] == "50.1", vals
            assert "from your race on Oct 1" == vals["sburn"] and "check it in the garage" in vals["stank"] and "est" in vals["cls"], vals
            assert vals["nob"] and vals["p90"]
            pg.uncheck("#f_p90")                    # plan with 2.5 L a lap (the 24-A test numbers)
            pg.click("#fuelGo")
            pg.wait_for_function("() => !document.getElementById('fuelRes').hidden", timeout=10000)
            res = pg.evaluate("() => ({k: [...document.querySelectorAll('#fuelKpis .kpi')].map(e => e.textContent),"
                              " w: [...document.querySelectorAll('#fuelWindows li')].map(e => e.textContent)})")
            assert res["k"][0].startswith("Stops2"), res
            wins = [x for x in res["w"] if x.startswith("Stop ")]
            assert len(wins) == 2 and re.match(r"Stop 1 after lap \d+ \(window \d+–\d+\): add ", wins[0]), res
            for w in wins:
                at, a, b = map(int, re.search(r"after lap (\d+) \(window (\d+)–(\d+)\)", w).groups())
                assert a <= at <= b, w
            # out of range: the window says which box before sending anything
            pg.fill("#f_burn", "30")
            pg.click("#fuelGo")
            pg.wait_for_function("() => document.getElementById('fuelMsg').classList.contains('bad')", timeout=5000)
            assert pg.evaluate("() => document.getElementById('fuelMsg').textContent").startswith("Fuel a lap (litres): ")
            # the server's own plain message (ValueError -> 400) is shown as it is
            pg.fill("#f_burn", "10")
            pg.fill("#f_tank", "15")
            pg.fill("#f_laps", "4")
            pg.evaluate("() => document.getElementById('fuelMsg').textContent = ''")
            pg.click("#fuelGo")
            pg.wait_for_function("() => document.getElementById('fuelMsg').textContent === "
                                 "'The tank is too small for one lap at that fuel use.'", timeout=5000)
            assert pg.evaluate("() => document.getElementById('fuelRes').hidden")
            errs[:] = [e for e in errs if "status of 400" not in e]
            pg.fill("#f_burn", "2.5")
            pg.fill("#f_tank", "47")
            pg.click("#fuelSaveCar")
            pg.wait_for_function("() => document.getElementById('fuelMsg').textContent.startsWith('Saved for the Ford Mustang GT4')", timeout=5000)
            pg.click("#fuelClose")
            pg.wait_for_function("() => !document.getElementById('dlgFuel').open")
            assert not errs, errs
        finally:
            ctx.close()


def test_window_team_notes_copy_into_mine():
    write_settings(f"team_cards_folder = '{SHARED}'\nshare_my_cards = true\n")
    try:
        with modules():
            r = _window()
            if not r:
                return
            pg, ctx, errs = r
            try:
                pg.click(".navbtn[data-nav=prep]")
                pg.wait_for_function("() => document.querySelector('#mySeries button[data-team-notes]') !== null", timeout=10000)
                assert pg.evaluate("() => document.querySelector('#mySeries button[data-team-notes]').textContent") == "Team notes (1)"
                pg.click("#mySeries button[data-team-notes]")
                pg.wait_for_function("() => !document.getElementById('cardTeam').hidden", timeout=10000)
                t = pg.evaluate("() => ({who: document.querySelector('#cardTeamList .who').textContent,"
                                " imgs: document.querySelectorAll('#cardTeamList img').length,"
                                " txt: document.getElementById('cardTeamList').textContent,"
                                " share: document.getElementById('cardShareNote').textContent})")
                assert t["who"].startswith("Josh Spence") and t["imgs"] == 0 and "<img src=x" in t["txt"], t
                assert "as Alex Driver" in t["share"], t
                pg.click("#cardTeamList button[data-copy=lap1]")
                assert pg.evaluate("() => document.getElementById('c_lap1').value") == "Hold the outside into T1"
                pg.click("#cardTeamList button[data-copy=lap1]")       # twice: not duplicated
                assert pg.evaluate("() => document.getElementById('c_lap1').value") == "Hold the outside into T1"
                pg.click("#cardClose")
                assert not errs, errs
            finally:
                ctx.close()
    finally:
        write_settings()


def test_window_intel_import_flow():
    with modules():
        r = _window()
        if not r:
            return
        pg, ctx, errs = r
        try:
            pg.click(".navbtn[data-nav=prep]")
            pg.wait_for_function("() => !document.getElementById('intelPanel').hidden", timeout=10000)
            f = TMP / "Track Intel.xlsx"
            f.write_bytes(xlsx_bytes())
            pg.set_input_files("#intelFile", str(f))
            pg.wait_for_function("() => !document.getElementById('intelMap').hidden", timeout=10000)
            sel = pg.evaluate("() => [...document.querySelectorAll('#intelCols select')].map(s => s.value)")
            assert sel == ["track", "hotspots", "passing"], sel
            assert pg.evaluate("() => !document.getElementById('intelSheetWrap').hidden")
            assert pg.evaluate("() => document.getElementById('btnIntelImport').disabled")
            pg.click("#btnIntelPreview")
            pg.wait_for_function("() => document.querySelectorAll('#intelPreview .item').length === 2", timeout=10000)
            txt = pg.evaluate("() => document.getElementById('intelPreview').textContent")
            assert "Changes: crash hotspots" in txt and "New: kept as typed" in txt and "cut to 300" in txt, txt
            pg.check("input[name=intelMode][value=append]")                 # a change needs a new preview
            assert pg.evaluate("() => document.getElementById('btnIntelImport').disabled")
            pg.click("#btnIntelPreview")
            pg.wait_for_function("() => !document.getElementById('btnIntelImport').disabled", timeout=10000)
            pg.click("#btnIntelImport")
            pg.wait_for_function("() => document.getElementById('intelMsg').textContent === 'Updated 9 track cards (3 unchanged).'", timeout=10000)
            assert pg.evaluate("() => document.getElementById('intelMap').hidden")
            # a wrong file type is refused in the window before anything is sent
            bad = TMP / "notes.txt"
            bad.write_text("hello", encoding="utf-8")
            pg.set_input_files("#intelFile", str(bad))
            pg.wait_for_function("() => document.getElementById('intelMsg').textContent === 'Pick an .xlsx or .csv file.'")
            assert not errs, errs
        finally:
            ctx.close()


def test_window_settings_team_cards():
    with modules():
        r = _window()
        if not r:
            return
        pg, ctx, errs = r
        try:
            pg.click(".navbtn[data-nav=settings]")
            pg.wait_for_function("() => document.getElementById('cardsPill').textContent === 'Off'", timeout=10000)
            pg.click("#pExtras > summary")   # 2.6: track cards sit under "Team and extras"
            pg.fill("#cardsFolder", str(TMP / "missing"))
            pg.click("#fCards button[type=submit]")
            pg.wait_for_function("() => document.getElementById('cardsMsg').classList.contains('bad')", timeout=5000)
            assert pg.evaluate("() => document.getElementById('cardsMsg').textContent") == \
                "That folder doesn't exist. Pick the shared folder your team uses."
            pg.fill("#cardsFolder", str(SHARED))
            pg.check("#cardsShare")
            pg.click("#fCards button[type=submit]")
            pg.wait_for_function("() => document.getElementById('cardsPill').textContent === 'Sharing'", timeout=5000)
            st = pg.evaluate("() => document.getElementById('cardsStatus').textContent")
            assert "Check your Garage 61 connection once" in st, st          # no cached name (no token)
            assert app.load_settings()["share_my_cards"] is True
            # the refused folder's 400 reply is the only console line allowed
            assert not [e for e in errs if "status of 400" not in e], errs
        finally:
            ctx.close()
            write_settings()


def test_window_result_dialog_incidents():
    with modules():
        r = _window()
        if not r:
            return
        pg, ctx, errs = r
        try:
            pg.click("#revlist .rev")
            pg.wait_for_function("() => !document.getElementById('fbResult').hidden", timeout=15000)
            pg.click("#fbResult")
            pg.wait_for_function("() => document.getElementById('dlgResult').open")
            sent = []
            pg.route("**/api/result", lambda rt: (sent.append(json.loads(rt.request.post_data)) if rt.request.method == "POST" else None,
                                                  rt.fulfill(status=200, content_type="application/json",
                                                             body=json.dumps({"ok": True, "result": {"finish_pos": 3}}))
                                                  if rt.request.method == "POST" else rt.continue_())[1])
            pg.fill("#r_finish_pos", "3")
            pg.fill("#r_pos_after_lap1", "5")
            pg.fill("#r_pit_stops", "1")
            pg.select_option("#r_finish_status", "running")
            for _ in range(31):
                pg.click("#incAdd") if not pg.evaluate("() => document.getElementById('incAdd').disabled") else None
            assert pg.evaluate("() => document.querySelectorAll('#incRows .incrow').length") == 30
            pg.evaluate("() => { const rows = [...document.querySelectorAll('#incRows .incrow')]; rows.slice(1).forEach(r => r.remove()) }")
            pg.fill("#incRows .inc-lap", "9")
            pg.select_option("#incRows .inc-kind", "contact")
            pg.select_option("#incRows .inc-pts", "4")
            pg.click("#resSave")
            pg.wait_for_function("() => !document.getElementById('dlgResult').open", timeout=5000)
            f = sent[0]["fields"]
            assert f["pos_after_lap1"] == 5 and f["pit_stops"] == 1 and f["finish_status"] == "running", f
            assert f["incident_laps"] == [{"lap": 9, "kind": "contact", "points": 4}], f
            assert not errs, errs
        finally:
            ctx.close()


if __name__ == "__main__":
    import faulthandler
    faulthandler.dump_traceback_later(600, exit=True)   # never hang CI (works on Windows too)
    failed = 0
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"ok    {name} ({time.time() - t0:.1f}s)")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    faulthandler.cancel_dump_traceback_later()
    if "s" in _SRV:
        _SRV["s"].shutdown()
    if "pw" in _PW:
        try:
            _PW["b"].close()
            _PW["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
