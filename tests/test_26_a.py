"""Debrief 2.6.0, team A: on-track id fix, team membership, single team slug, review lookup from the sync
store, one list refresh per run, sync-on-open skipping unchanged combos, Garage 61 client errors/backoff,
plain-language messages.

    python3 tests/test_26_a.py

No network: fakes in this file, plus a tiny fake Garage 61 on 127.0.0.1 for the client and sync tests.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-26a-"))
for _k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR", "DEBRIEF_SCHEDULE_DIR", "DEBRIEF_UPDATE_STATE"):
    os.environ.setdefault(_k, str(TMP / _k.lower()))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

from debrief import app, brand, g61, leaderboards, prep, sync, team  # noqa: E402

QUIET = lambda m: None  # noqa: E731


# ================================================================ fakes
class CountingApi:
    """Records every call that would be a Garage 61 request."""
    _n = 0

    def __init__(self, teams=None, teams_raises=False, laps=None):
        self.calls = []
        self.requests_made = 0
        self._teams = [{"slug": "midfield-menace", "name": "Midfield Menace"}] if teams is None else teams
        self.teams_raises = teams_raises
        self._laps = laps or []
        CountingApi._n += 1
        self.cache = TMP / f"cache-{CountingApi._n}"

    def _req(self, name, **kw):
        self.calls.append((name, kw))
        self.requests_made += 1

    def me(self):
        return {"slug": "me", "firstName": "Alex", "lastName": "Driver"}

    def peek(self, key):
        return self.me() if key == "me" else None

    def teams(self, refresh=False):
        if self.teams_raises:
            raise g61.Garage61Error(g61.MSG_OFFLINE)
        if refresh:
            self._req("teams", refresh=True)
            if getattr(self, "joined", None):
                self._teams = list(self.joined)
        return list(self._teams)

    def cached_items(self, name):
        return list(self._teams) if name == "teams" else []

    def tracks(self, refresh=False):
        self._req("tracks", refresh=refresh) if refresh else None
        return []

    def cars(self, refresh=False):
        self._req("cars", refresh=refresh) if refresh else None
        return [{"id": 7, "name": "Car Seven"}]

    def has_csv(self, lid):
        return False

    def lap_csv(self, lid):
        return None

    def find_laps(self, fresh=False, **kw):
        self._req("find_laps", **kw)
        mk = lambda slug, t: {"id": f"{slug}{t}", "lapTime": t, "clean": True,  # noqa: E731
                              "startTime": "2026-10-01T20:00:00Z", "event": "EV1", "car": 7,
                              "driver": {"slug": slug, "firstName": slug.title(), "lastName": ""}}
        if kw.get("drivers") == ["me"] and not kw.get("teams"):
            return [mk("me", 90.2), mk("me", 92.0)]
        return [mk("jake", 89.0), mk("dom", 91.0), mk("me", 90.5)] + list(self._laps)

    def team_calls(self):
        return [kw for n, kw in self.calls if n == "find_laps" and kw.get("teams")]


def _lib_review(folder, name="a", best=90.0, started="2026-10-01T20:00:00+00:00", ids=(468, 190)):
    d = Path(folder) / "reviews" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.json").write_text(json.dumps({
        "meta": {"track": "Charlotte Motor Speedway (Roval 2019)", "car": "Ford Mustang GT4", "started_at": started},
        "best": {"n": 2, "t": best}, "median": 91.0, "combo_ids": {"track_id": ids[0], "car_id": ids[1]},
        "laps": [{"n": 1, "t": 91.0, "clean": True}, {"n": 2, "t": best, "clean": True}]}), encoding="utf-8")


def _newdir():
    return Path(tempfile.mkdtemp(dir=TMP))


# ================================================================ 1. on_track sends the track id
def test_on_track_sends_an_int_track_id():
    api = CountingApi()
    app.reset_refreshed()
    track = {"id": 468, "name": "Charlotte Motor Speedway", "variant": "Roval 2019"}
    app.on_track(api, track, "EV1", "me", 7)
    kw = api.calls[-1][1]
    assert kw["tracks"] == [468] and isinstance(kw["tracks"][0], int), kw
    # through the real request builder: the query string carries "468", never a dict
    fake = _FakeG61()
    srv, base = _serve(fake)
    try:
        c = _client(base, _newdir())
        app.on_track(c, track, "EV1", "me", 7)
        q = [q for p, q in fake.log if p == "laps"][-1]
        assert q["tracks"] == "468", q
    finally:
        srv.shutdown()


def test_cmd_review_passes_the_id_to_on_track():
    src = (ROOT / "debrief" / "app.py").read_text(encoding="utf-8")
    assert "on_track(api, _id(track)," in src


# ================================================================ 2. membership
def test_team_build_member_unchanged_and_keys():
    lib = _newdir()
    _lib_review(lib)
    api = CountingApi()
    d = team.build(api, lib, "midfield-menace", log=QUIET)
    assert d["membership"] == brand.MEMBER and d["team"] == {"slug": "midfield-menace", "name": "Midfield Menace"}
    assert len(api.team_calls()) >= 1
    assert {r["key"] for r in d["combos"][0]["board"]} >= {"jake", "dom", "me"}


def test_team_build_not_member_makes_no_team_requests():
    lib = _newdir()
    _lib_review(lib)
    api = CountingApi(teams=[{"slug": "other-team", "name": "Other"}])
    d = team.build(api, lib, "midfield-menace", log=QUIET)
    assert d["membership"] == brand.NOT_MEMBER
    assert d["team"] == {"slug": "midfield-menace", "name": brand.TEAM_NAME}
    assert api.team_calls() == [], api.calls
    board = d["combos"][0]["board"]
    assert [r["key"] for r in board] == ["me"], board        # your own best still shows
    assert not d["h2h"]


def test_team_build_unknown_membership_proceeds():
    lib = _newdir()
    _lib_review(lib)
    api = CountingApi(teams_raises=True)
    d = team.build(api, lib, "midfield-menace", log=QUIET)
    assert d["membership"] == brand.UNKNOWN and len(api.team_calls()) >= 1
    assert d["team"]["name"] == "Midfield Menace"


def test_render_saved_has_membership_and_team_name():
    lib = _newdir()
    p = team.render_saved(lib)
    data = json.loads((Path(lib) / "team" / "team.json").read_text(encoding="utf-8"))
    assert p.exists() and data["membership"] == brand.UNKNOWN
    assert data["team"] == {"slug": brand.TEAM_SLUG, "name": brand.TEAM_NAME}


def test_prep_membership():
    lib = _newdir()
    _lib_review(lib)
    api = CountingApi()
    d = prep.build(api, lib, 468, 190, None, log=QUIET)
    assert d["membership"] == brand.MEMBER and d["team"]["name"] == "Midfield Menace"
    assert [kw["teams"] for kw in api.team_calls()] == [["midfield-menace"]]   # always the slug, never "all"
    assert d["teammates"]["laps"]
    api = CountingApi(teams=[])
    d = prep.build(api, lib, 468, 190, "midfield-menace", log=QUIET)
    assert d["membership"] == brand.NOT_MEMBER and api.team_calls() == [] and d["teammates"]["laps"] == []
    assert d["team"] == {"slug": "midfield-menace", "name": "Midfield Menace"}
    assert d["history"]                                    # your own part still works
    api = CountingApi(teams_raises=True)
    d = prep.build(api, lib, 468, 190, None, log=QUIET)
    assert d["membership"] == brand.UNKNOWN and len(api.team_calls()) == 1
    off = prep.build(None, lib, 468, 190, None, log=QUIET)
    assert off["membership"] == brand.UNKNOWN and off["team"]["name"] == brand.TEAM_NAME


def test_prep_teammates_uses_brand_slug_when_none_given():
    api = CountingApi()
    prep._teammates(api, 468, 190, None, 90.0, QUIET)
    assert api.team_calls()[0]["teams"] == [brand.TEAM_SLUG]


def _sched():
    from datetime import date
    from debrief import schedule
    start = date.today() - timedelta(days=date.today().weekday())
    ser = {"id": schedule.slug("Mazda Cup"), "name": "Mazda Cup", "cars": ["Global Mazda MX-5 Cup"],
           "classes": ["MX-5"], "multiclass": False,
           "weeks": [{"week": 1, "start": start.isoformat(), "track": "Lime Rock Park"}]}
    return {"schema": 1, "season": "2026 Season 4", "weeks": [{"week": 1, "start": start.isoformat()}],
            "series": [ser]}


def test_leaderboards_not_member_no_requests():
    lib = _newdir()
    cfg = {"library_folder": str(lib), "team_slug": "midfield-menace"}
    sched = _sched()
    mine = {"chosen": True, "series": [{"id": sched["series"][0]["id"], "name": "Mazda Cup"}]}
    api = CountingApi(teams=[])
    res = leaderboards.refresh(api, cfg, sched, mine, log=QUIET)
    # only the one re-check of the teams list (a cached "not a member" may be a day old); no lap requests
    assert res["membership"] == brand.NOT_MEMBER and api.calls == [("teams", {"refresh": True})]
    assert res["requests"] == 1
    assert res["team"] == {"slug": "midfield-menace", "name": "Midfield Menace"}
    v = leaderboards.view(lib, sched)
    assert v["membership"] == brand.NOT_MEMBER and v["series"] == [] and v["team"]["name"] == "Midfield Menace"
    api.calls.clear()   # plan() reads the saved teams list only
    p = leaderboards.plan(cfg, api, sched, mine)
    assert p["membership"] == brand.NOT_MEMBER and p["team"]["name"] == "Midfield Menace" and api.calls == []
    # the team page picks the newest weekly check up
    team.render_saved(lib)
    data = json.loads((Path(lib) / "team" / "team.json").read_text(encoding="utf-8"))
    assert data["membership"] == brand.NOT_MEMBER


def test_leaderboards_member_and_unknown_keys():
    lib = _newdir()
    cfg = {"library_folder": str(lib), "team_slug": "midfield-menace"}
    sched = _sched()
    mine = {"chosen": True, "series": [{"id": sched["series"][0]["id"], "name": "Mazda Cup"}]}
    for api, want in ((CountingApi(), brand.MEMBER), (CountingApi(teams_raises=True), brand.UNKNOWN)):
        res = leaderboards.refresh(api, cfg, sched, mine, log=QUIET)
        assert res["membership"] == want and res["team"]["name"] == "Midfield Menace", res
    v = leaderboards.view(_newdir())
    assert v["membership"] == brand.UNKNOWN and v["team"] == {"slug": brand.TEAM_SLUG, "name": brand.TEAM_NAME}


# ================================================================ 3. single source
def test_team_slug_single_source():
    assert app.DEFAULT_TEAM == brand.TEAM_SLUG
    for f in ("leaderboards.py", "prep.py", "app.py", "team.py"):
        assert '"midfield-menace"' not in (ROOT / "debrief" / f).read_text(encoding="utf-8"), f


# ================================================================ 4. last race from the sync store
def _seed_store(api, last_success, sessions, stopped=False):
    store = sync.Store.for_api(api)
    store.root.mkdir(parents=True, exist_ok=True)
    store.save_state({"first_complete": True, "last_success": last_success, "stopped": stopped, "combos": {}})
    (store.root / "sessions.json").write_text(json.dumps({"schema": 1, "sessions": sessions}), encoding="utf-8")


class StatsApi(CountingApi):
    def my_statistics(self, start):
        self._req("my_statistics")
        return [{"track": {"id": 5}, "car": {"id": 7}, "day": "2026-10-29", "laps": 3}]

    def find_laps(self, fresh=False, **kw):
        self._req("find_laps", **kw)
        return [{"id": "L1", "event": "EVSEARCH", "track": {"id": 5}, "car": {"id": 7},
                 "startTime": "2026-10-29T20:00:00Z", "sessionType": 3}]


SESS = [{"event": "EVPRAC", "type": 1, "track_id": 9, "car_id": 8, "start": "2026-10-30T10:00:00Z"},
        {"event": "EVRACE", "type": 3, "track_id": 5, "car_id": 7, "start": "2026-10-30T09:00:00Z"}]


def test_last_event_from_fresh_store_makes_no_requests():
    api = StatsApi()
    now = datetime.now(timezone.utc)
    _seed_store(api, sync._iso(now - timedelta(minutes=3)), SESS)
    assert app.find_last_event(api, [3]) == ("EVRACE", 5, 7) and api.requests_made == 0
    # include_practice: the newest of any type, practice included
    assert app.find_last_event(api, [1, 2, 3]) == ("EVPRAC", 9, 8) and api.requests_made == 0


def test_last_event_old_store_searches_today():
    api = StatsApi()
    now = datetime.now(timezone.utc)
    _seed_store(api, sync._iso(now - timedelta(minutes=11)), SESS)
    assert app.find_last_event(api, [3]) == ("EVSEARCH", 5, 7)
    assert api.requests_made == 2, api.calls        # statistics + one lap search
    api2 = StatsApi()
    _seed_store(api2, sync._iso(now - timedelta(minutes=1)), SESS, stopped=True)   # unfinished sync: not trusted
    app.find_last_event(api2, [3])
    assert api2.requests_made == 2


def test_no_race_messages():
    class Empty(StatsApi):
        def my_statistics(self, start):
            self._req("my_statistics")
            return []
    for sessions, practice in (([3], True), ([1, 2, 3], False)):
        try:
            app.find_last_event(Empty(), sessions)
            raise AssertionError("no error")
        except SystemExit as e:
            msg = str(e.code)
            assert msg.startswith(app.NO_RACE_MSG), msg
            assert ("Include practice" in msg) == practice, msg
            assert "settings.toml" not in msg and "include_practice" not in msg


# ================================================================ 5. one refresh per run
def test_name_of_refreshes_each_list_once_per_run():
    app.reset_refreshed()
    api = CountingApi()
    for v in (101, 102, 103):
        assert app.name_of(api, "car", v) == f"car {v}"
        assert app.name_of(api, "track", v) == f"track {v}"
    refreshes = [(n, kw) for n, kw in api.calls if kw.get("refresh")]
    assert sorted(n for n, _ in refreshes) == ["cars", "tracks"], refreshes
    assert app.name_of(api, "car", 7) == "Car Seven"
    # on_track with unknown cars shares the same budget
    api2 = CountingApi(laps=[{"id": f"x{i}", "lapTime": 90, "event": "EV1", "car": 200 + i,
                              "driver": {"slug": f"d{i}"}} for i in range(4)])
    app.on_track(api2, 5, "EV1", "me", 7)
    app.name_of(api2, "car", 999)
    assert sum(1 for n, kw in api2.calls if n == "cars" and kw.get("refresh")) == 1, api2.calls
    # a new client (a new run) may refresh again
    api3 = CountingApi()
    app.name_of(api3, "car", 555)
    assert sum(1 for n, kw in api3.calls if n == "cars") == 1


# ================================================================ fake Garage 61 (client + sync)
class _FakeG61:
    def __init__(self):
        self.log = []
        self.status = {}          # path -> forced status code
        self.laps = []
        self.lock = threading.Lock()
        now = datetime.now(timezone.utc)
        for k in range(6):
            self.add(f"A{k}", 10, 20, "EVA", 3, now - timedelta(days=3, minutes=k), 90 + k * 0.1)
            self.add(f"B{k}", 11, 21, "EVB", 1, now - timedelta(days=2, minutes=k), 80 + k * 0.1)

    def add(self, lid, t, c, ev, st, when, lt):
        self.laps.append({"id": lid, "track": {"id": t}, "car": {"id": c}, "event": ev, "sessionType": st,
                          "startTime": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "lapTime": lt, "clean": True})

    def stats(self, start):
        rows = {}
        for l in self.laps:
            if l["startTime"] >= start:
                k = (l["track"]["id"], l["car"]["id"], l["startTime"][:10])
                r = rows.setdefault(k, {"track": {"id": k[0]}, "car": {"id": k[1]}, "day": k[2], "laps": 0,
                                        "timeDriven": 0.0})
                r["laps"] += 1
                r["timeDriven"] = round(r["timeDriven"] + l["lapTime"], 3)
        return {"drivingStatistics": list(rows.values())}


def _serve(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path[len("/api/v1/"):]
            with fake.lock:
                fake.log.append((path, q))
            code, body, ctype = fake.status.get(path, 200), None, "application/json"
            if code != 200:
                body = "secret body detail"
                ctype = "text/plain"
            elif path == "me/statistics":
                body = fake.stats(q["start"])
            elif path == "laps":
                t, c = int(q["tracks"]), int(q.get("cars") or 0)
                hit = [l for l in fake.laps if l["track"]["id"] == t and (not c or l["car"]["id"] == c)
                       and l["startTime"] > q.get("after", "")]
                body = {"items": hit, "total": len(hit)}
            elif path in ("tracks", "cars", "teams"):
                body = {"items": []}
            elif path.endswith("/csv"):
                body, ctype = "Speed,LapDistPct\n1,0.1\n", "text/csv"
            elif path == "me":
                body = {"slug": "me"}
            else:
                code = 404
            raw = b"" if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/api/v1/"


def _client(base, cache, logs=None):
    return g61.Garage61("test-token-123456", Path(cache), min_gap_s=0.0, base_url=base, backoff_base=0,
                        log=(logs.append if logs is not None else QUIET))


# ================================================================ 6. sync on open
def test_sync_no_change_open_is_one_request_and_progress_text():
    fake = _FakeG61()
    srv, base = _serve(fake)
    try:
        api = _client(base, _newdir())
        lines = []
        now = datetime.now(timezone.utc)
        r = sync.run(api, log=lines.append, telemetry="none", now=now)
        assert r["first"] and r["laps"] == 12
        st = sync.Store.for_api(api).state()
        assert set(st["stats"]) == {"10:20", "11:21"}, st.get("stats")
        prog = [l for l in lines if " so far" in l]
        assert prog and all("session" in l for l in prog), lines
        assert "2 sessions so far" in prog[-1] and ("left" not in prog[-1]), prog   # last combo: nothing left
        assert "1 session so far" in prog[0] and "left" in prog[0], prog[0]
        # reopen: statistics unchanged -> exactly 1 request, no /laps
        fake.log.clear()
        r2 = sync.run(api, log=QUIET, telemetry="none", now=now + timedelta(minutes=20))
        assert r2["requests"] == 1 and [p for p, _ in fake.log] == ["me/statistics"], fake.log
        # a new lap on one combo: only that combo is asked for
        fake.add("A9", 10, 20, "EVA2", 3, now + timedelta(minutes=25), 89.5)
        fake.log.clear()
        r3 = sync.run(api, log=QUIET, telemetry="none", now=now + timedelta(minutes=30))
        laps_q = [q for p, q in fake.log if p == "laps"]
        assert len(laps_q) == 1 and laps_q[0]["tracks"] == "10" and r3["new_laps"] == 1, fake.log
        # the weekly deep pass still asks every combo driven, unchanged or not
        fake.log.clear()
        sync.run(api, log=QUIET, telemetry="none", now=now + sync.DEEP_EVERY + timedelta(hours=1))
        assert {q["tracks"] for p, q in fake.log if p == "laps"} == {"10", "11"}, fake.log
    finally:
        srv.shutdown()


def test_sync_stats_without_numbers_always_asks():
    rows = [{"track": {"id": 1}, "car": {"id": 2}, "day": "2026-10-29"}]
    assert sync._stats_rows(rows) == {}
    assert not sync._stats_unchanged({}, {})


def test_eta_text():
    assert sync._eta_text(0, 1, 1) == ""
    assert sync._eta_text(10, 1, 0.01) == "under a minute left"     # never assumes faster than 1 s a request
    assert sync._eta_text(90, 2, 1.0) == "about 3 min left"


# ================================================================ 7. client: backoff + plain errors
def test_backoff_injectable_and_default():
    assert g61.Garage61.BACKOFF_BASE == 5
    c = g61.Garage61("tok-123456", _newdir(), base_url="http://127.0.0.1:9/api/v1/", log=QUIET)
    assert c.BACKOFF_BASE == 5
    c0 = g61.Garage61("tok-123456", _newdir(), base_url="http://127.0.0.1:9/api/v1/", log=QUIET, backoff_base=0)
    assert c0.BACKOFF_BASE == 0


def test_plain_errors_and_detail_in_log():
    fake = _FakeG61()
    srv, base = _serve(fake)
    try:
        for status, want in ((401, g61.MSG_TOKEN), (503, g61.MSG_SERVER.format(code=503)),
                             (418, g61.MSG_OTHER.format(code=418))):
            fake.status["laps"] = status
            logs = []
            c = _client(base, _newdir(), logs)
            try:
                c.find_laps(tracks=[5], cars=[7], drivers=["me"])
                raise AssertionError("no error")
            except g61.Garage61Error as e:
                msg = str(e)
                assert msg == want, msg
                assert e.status == status
                assert "/laps" not in msg and "tracks=" not in msg and "secret" not in msg, msg
            assert any("/laps?" in l for l in logs), logs
            if status == 418:
                assert any("secret body detail" in l for l in logs), logs
        assert len([1 for p, _ in fake.log if p == "laps"]) >= 5   # the 503 was retried (no sleeps: backoff 0)
    finally:
        srv.shutdown()
    logs = []
    c = g61.Garage61("tok-123456", _newdir(), min_gap_s=0, base_url="http://127.0.0.1:9/api/v1/", log=logs.append,
                     backoff_base=0)
    try:
        c.me()
        raise AssertionError("no error")
    except g61.Garage61Error as e:
        assert str(e) == g61.MSG_OFFLINE and e.status is None
    assert any("unreachable" in l and "/me" in l for l in logs), logs
    try:
        g61.Garage61("", _newdir())
        raise AssertionError("no error")
    except g61.Garage61Error as e:
        assert "settings.toml" not in str(e) and "Settings" in str(e)


def test_requests_imported_lazily():
    src = (ROOT / "debrief" / "g61.py").read_text(encoding="utf-8")
    head = src[:src.index("class Garage61Error")]
    assert "\nimport requests" not in head


# ================================================================ 8. app messages
def test_app_messages_have_no_settings_file_or_debug_text():
    src = (ROOT / "debrief" / "app.py").read_text(encoding="utf-8")
    assert "Run Setup.bat first" not in src and "(Lap fields seen" not in src
    assert "include_practice = true" not in src


def test_check_telemetry_line_is_info():
    class Api(StatsApi):
        def _cached(self, key, fn, max_age_s=None):
            return {"slug": "me"}

        def find_laps(self, fresh=False, **kw):
            return [{"id": "L1"}]

        def lap_csv(self, lid):
            return None
    real = app.make_api
    app.make_api = lambda cfg: Api()
    try:
        lines = app.cmd_check({"cache_dir": str(_newdir()), "library_folder": str(_newdir())})
    finally:
        app.make_api = real
    assert isinstance(lines, list) and app.TELEMETRY_INFO in lines
    assert not any("NOT available" in l for l in lines)
    rows = lines.rows()
    row = next(r for r in rows if r["text"] == app.TELEMETRY_INFO)
    assert row["level"] == "info" and all(r["level"] in ("ok", "info") for r in rows)
    assert lines.info == [app.TELEMETRY_INFO]


# ================================================================ 2.6.0 verifier fixes
def test_newest_synced_race_already_reviewed_searches_for_a_newer_one():
    """H1: a sync that finished before Garage 61 had your latest race must not make "Review last race"
    quietly review the previous race again: when the store's newest race already has a review, search."""
    api = StatsApi()
    now = datetime.now(timezone.utc)
    _seed_store(api, sync._iso(now - timedelta(minutes=3)), SESS)
    lib = TMP / "lib-h1"
    (lib / "reviews" / "2026-10-30_race_evrace").mkdir(parents=True)
    (lib / "reviews" / "2026-10-30_race_evrace" / "review.json").write_text("{}", encoding="utf-8")
    assert app.find_last_event(api, [3], library_folder=str(lib)) == ("EVSEARCH", 5, 7)
    assert api.requests_made == 2, api.calls
    api2 = StatsApi()      # not reviewed yet: the store answers with no requests
    _seed_store(api2, sync._iso(now - timedelta(minutes=3)), SESS)
    assert app.find_last_event(api2, [3], library_folder=str(TMP / "lib-empty")) == ("EVRACE", 5, 7)
    assert api2.requests_made == 0


def test_not_member_answer_is_rechecked_once_so_a_new_join_shows_up():
    """H2: the teams list is cached for a day; a cached "not a member" is checked once more against
    Garage 61 so someone who just joined sees the team straight away (1 request)."""
    api = CountingApi(teams=[])
    api.joined = [{"slug": "midfield-menace", "name": "Midfield Menace"}]
    mem, teams_list = team.membership_for(api, None, QUIET)
    assert mem == brand.MEMBER and api.calls == [("teams", {"refresh": True})]
    api2 = CountingApi(teams=[])     # still not on the team: exactly one extra request, still not a member
    assert team.membership_for(api2, None, QUIET)[0] == brand.NOT_MEMBER and api2.requests_made == 1
    api3 = CountingApi()             # already a member: no re-check
    assert team.membership_for(api3, None, QUIET)[0] == brand.MEMBER and api3.requests_made == 0


def test_real_client_teams_refresh_skips_the_day_cache():
    assert "refresh" in g61.Garage61.teams.__code__.co_varnames


def test_practice_reviewed_from_last_race_gets_the_practice_suffix():
    """2nd verifier M-b: "Review last race" with practice included named practice folders without the
    _practice suffix, so the already-reviewed check missed them."""
    src = (ROOT / "debrief" / "app.py").read_text(encoding="utf-8")
    assert "session_kind if session_kind in (1, 2, 3) else session_type(laps[0])" in src
    assert app.session_type({"sessionType": 1}) == 1 and app.session_type({"session_type": "practice"}) == 1


def test_store_pick_message_names_the_session():
    app._STORE_PICK.clear()
    app._STORE_PICK.update({"type": 3, "track": "Spa", "start": "2026-10-30T18:40:00Z"})
    t = app._store_pick_text()
    assert t.startswith("your race at Spa on ") and ":" in t


def test_teams_refresh_is_detected_by_signature_not_by_catching_type_errors():
    class Old:
        def teams(self):
            return []

    class Broken:
        def teams(self, refresh=False):
            raise TypeError("a real bug inside teams()")
    assert team.can_refresh_teams(CountingApi()) and not team.can_refresh_teams(Old())
    assert team.membership_for(Old(), None, QUIET)[0] == brand.NOT_MEMBER
    seen = []
    assert team.membership_for(Broken(), None, seen.append)[0] == brand.UNKNOWN   # raised and logged
    assert seen


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
