"""History sync tests: first download, catch-up syncs, pausing and resuming, and the window's API.

    python3 tests/test_sync.py        (or: python3 -m pytest tests/test_sync.py)

Runs against a small fake Garage 61 served on 127.0.0.1 (started here), so nothing leaves the PC.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

NOW = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ================================================================ fake Garage 61
class FakeG61:
    """Laps for two track/car combos: a big one (2,300 laps, so three pages) and a small one."""

    def __init__(self):
        self.laps = []
        self.log = []           # (path, params) of every request
        self.forbid_csv = set()
        self.csv_text = "Speed,LapDistPct\n10,0.1\n20,0.2\n"
        self.lock = threading.Lock()
        base = NOW - timedelta(days=400)
        n = 0
        # combo (10, 20): 23 practice events of 100 laps
        for e in range(23):
            for k in range(100):
                n += 1
                self.add(f"L{n}", 10, 20, f"EVPRAC{e:03d}", 1, base + timedelta(days=e * 10, minutes=k * 2),
                         90.0 + (k % 7) * 0.1 - e * 0.01, clean=(k % 5 != 0))
        # combo (11, 21): one race (event with practice + race sessions)
        for k in range(12):
            n += 1
            self.add(f"L{n}", 11, 21, "EVRACE001", 1, NOW - timedelta(days=3, minutes=60 - k * 2), 100.0 + k * 0.05)
        for k in range(20):
            n += 1
            self.add(f"L{n}", 11, 21, "EVRACE001", 3, NOW - timedelta(days=3, minutes=-k * 2), 99.5 + (k % 3) * 0.2,
                     clean=k != 4)
        self.n = n

    def add(self, lid, t, c, ev, st, when, lt, clean=True):
        self.laps.append({"id": lid, "track": {"id": t, "name": f"Track{t}"}, "car": {"id": c, "name": f"Car{c}"},
                          "event": ev, "sessionType": st, "startTime": iso(when), "lapTime": lt, "clean": clean,
                          "driver": {"slug": "me"}})

    def stats(self, start):
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        rows = {}
        for l in self.laps:
            when = datetime.fromisoformat(l["startTime"].replace("Z", "+00:00"))
            if when >= s:
                k = (l["track"]["id"], l["car"]["id"], l["startTime"][:10])
                rows[k] = {"track": {"id": k[0]}, "car": {"id": k[1]}, "day": k[2], "laps": 1}
        return {"drivingStatistics": list(rows.values())}

    def search(self, q):
        t = int(q["tracks"]); c = int(q["cars"])
        after = q.get("after", "2000-01-01T00:00:00Z")
        hit = [l for l in self.laps if l["track"]["id"] == t and l["car"]["id"] == c and l["startTime"] > after
               and (not q.get("event") or l["event"] == q["event"])]
        hit.sort(key=lambda l: l["startTime"], reverse=True)
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 1000))
        return {"items": hit[off:off + lim], "total": len(hit)}


def serve(fake: FakeG61):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path[len("/api/v1/"):]
            with fake.lock:
                fake.log.append((path, q))
            code, body, ctype = 200, None, "application/json"
            if path == "me":
                body = {"slug": "me", "firstName": "Test"}
            elif path == "me/statistics":
                body = fake.stats(q["start"])
            elif path == "laps":
                body = fake.search(q)
            elif path in ("tracks", "cars"):
                ids = sorted({l[path[:-1]]["id"] for l in fake.laps})
                body = {"items": [{"id": i, "name": f"{path[:-1].title()} {i}"} for i in ids]}
            elif path == "teams":
                body = {"items": []}
            elif path.startswith("laps/") and path.endswith("/csv"):
                lid = path.split("/")[1]
                if lid in fake.forbid_csv:
                    code = 403
                else:
                    body, ctype = fake.csv_text, "text/csv"
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


def client(base, cache):
    from debrief.g61 import Garage61
    api = Garage61("test-token-123456", Path(cache), min_gap_s=0.0, base_url=base, log=lambda m: None)
    _no_backoff(api)
    return api


def _no_backoff(api):
    """The fake server answers at once, so retries needn't wait 5+10+20+40 s. Garage61.BACKOFF_BASE (2.6)
    sets the first retry delay; before it existed, the client's waits are cut to 0 s (stop checks still run)."""
    if hasattr(type(api), "BACKOFF_BASE"):
        api.BACKOFF_BASE = 0
    else:
        real = api._sleep
        api._sleep = lambda secs: real(0)


# ================================================================ sync engine
def test_first_sync_downloads_everything_then_only_new_laps():
    from debrief import sync
    fake = FakeG61()
    srv, base = serve(fake)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fake.forbid_csv.add("L1")   # pretend one session's telemetry isn't available
            api = client(base, tmp)
            r = sync.run(api, log=lambda m: None, now=NOW)
            assert r["first"] and not r["stopped"], r
            assert r["laps"] == fake.n, (r["laps"], fake.n)
            store = sync.Store.for_api(api)
            sess = store.sessions()
            # 23 practice events + the race event's practice and race sessions
            assert len(sess) == 25, len(sess)
            assert sess[0]["event"] == "EVRACE001" and sess[0]["session"] == "Race", sess[0]
            assert sess[1]["event"] == "EVRACE001" and sess[1]["session"] == "Practice"
            race = sess[0]
            assert race["laps"] == 20 and race["clean_laps"] == 19 and abs(race["best_clean"] - 99.5) < 1e-9
            assert race["track"] == "Track11" and race["car"] == "Car21"
            # the big combo needed three pages (1000 + 1000 + 300)
            pages = [q for p, q in fake.log if p == "laps" and q["tracks"] == "10"]
            assert [int(q["offset"]) for q in pages] == [0, 1000, 2000], pages
            assert all(q["after"] == sync.EPOCH and q["group"] == "none" and q["drivers"] == "me" for q in pages)
            # telemetry: once per session's best lap
            csvs = [p for p, _ in fake.log if p.endswith("/csv")]
            assert len(csvs) == 25 and len(set(csvs)) == 25
            st = store.state()
            assert st["first_complete"] and st["last_success"] == iso(NOW)

            # nothing new: just one statistics call (no driving since the last sync), no telemetry again
            fake.log.clear()
            r2 = sync.run(api, log=lambda m: None, now=NOW + timedelta(hours=2))
            assert not r2["first"] and r2["new_laps"] == 0
            paths = [p for p, _ in fake.log]
            assert paths.count("me/statistics") == 1
            assert not any(p.endswith("/csv") for p in paths), paths   # L1 (403) isn't asked for again
            assert [q["start"] for p, q in fake.log if p == "me/statistics"] == [iso(NOW - sync.OVERLAP)]
            assert not [q for p, q in fake.log if p == "laps"]

            # a new practice session today shows up after the next sync
            for k in range(5):
                fake.add(f"N{k}", 10, 20, "EVNEW", 1, NOW + timedelta(hours=3, minutes=k * 2), 89.0 + k * 0.1)
            fake.log.clear()
            r3 = sync.run(api, log=lambda m: None, now=NOW + timedelta(hours=4))
            assert r3["new_laps"] == 5 and r3["laps"] == fake.n + 5, r3
            sess = store.sessions()
            assert sess[0]["event"] == "EVNEW" and sess[0]["laps"] == 5 and sess[0]["session"] == "Practice"
            assert [p for p, _ in fake.log if p.endswith("/csv")] == ["laps/N0/csv"]
            lap_calls = [q for p, q in fake.log if p == "laps"]
            assert len(lap_calls) == 1 and lap_calls[0]["tracks"] == "10", lap_calls   # only the combo driven
            assert lap_calls[0]["after"] == iso(NOW - sync.OVERLAP)
            assert r3["requests"] <= 6, r3   # stats + 2 lap pages + telemetry (+ names, saved for a week)
    finally:
        srv.shutdown()


def test_paused_sync_resumes_where_it_stopped():
    from debrief import sync
    fake = FakeG61()
    srv, base = serve(fake)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            api = client(base, tmp)
            # stop after the statistics call, the track and car names and the first combo's lap page
            r = sync.run(api, log=lambda m: None, now=NOW, should_stop=lambda: api.requests_made >= 4)
            assert r["stopped"] and not r["synced"], r
            st = sync.Store.for_api(api).state()
            assert st.get("stopped") and not st.get("first_complete")
            assert sync.due(sync.Store.for_api(api))
            done_before = {k for k, v in st["combos"].items() if v.get("done")}
            assert done_before == {"11:21"}, done_before   # newest combo first
            fake.log.clear()
            r2 = sync.run(api, log=lambda m: None)
            assert not r2["stopped"] and r2["synced"] and r2["laps"] == fake.n, r2
            # the finished combo wasn't downloaded again
            assert not any(p == "laps" and q["tracks"] == "11" for p, q in fake.log)
            assert not sync.due(sync.Store.for_api(api))
    finally:
        srv.shutdown()


def test_sync_without_telemetry_and_due_window():
    from debrief import sync
    fake = FakeG61()
    srv, base = serve(fake)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            api = client(base, tmp)
            r = sync.run(api, log=lambda m: None, telemetry="none")
            assert r["synced"] and not any(p.endswith("/csv") for p, _ in fake.log)
            store = sync.Store.for_api(api)
            assert not sync.due(store)                 # just synced
            st = store.state()
            st["last_success"] = iso(datetime.now(timezone.utc) - timedelta(minutes=11))
            store.save_state(st)
            assert sync.due(store)                     # over ten minutes ago
    finally:
        srv.shutdown()


def test_store_is_per_account():
    from debrief import sync
    with tempfile.TemporaryDirectory() as tmp:
        from debrief.g61 import Garage61
        a = Garage61("token-aaaaaaaa", Path(tmp), base_url="http://127.0.0.1:9/api/v1/")
        b = Garage61("token-bbbbbbbb", Path(tmp), base_url="http://127.0.0.1:9/api/v1/")
        assert sync.Store.for_api(a).root != sync.Store.for_api(b).root
        assert sync.Store.for_api(a).summary()["synced"] is False   # reading an empty store sends nothing


# ================================================================ settings + window
def _settings_env(tmp, base, extra=""):
    st = Path(tmp) / "settings.toml"
    lib = (Path(tmp) / "lib").as_posix()
    st.write_text(f'token = "test-token-123456"\nlibrary_folder = "{lib}"\napi_base = "{base}"\n{extra}', encoding="utf-8")
    os.environ["DEBRIEF_SETTINGS"] = str(st)
    os.environ["DEBRIEF_CACHE_DIR"] = str(Path(tmp) / "cache")
    return st


def _clear_env(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_settings_sync_options_round_trip():
    from debrief import app
    from debrief.desktop import settings_io
    old = {k: os.environ.get(k) for k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR")}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _settings_env(tmp, "http://127.0.0.1:9/api/v1/")
            v = settings_io.public_view()
            assert v["sync_on_open"] is True and v["sync_telemetry"] == "best"
            assert app.load_settings()["sync_on_open"] is True
            v = settings_io.save({"sync_on_open": False, "sync_telemetry": "none"})
            assert v["sync_on_open"] is False and v["sync_telemetry"] == "none"
            cfg = app.load_settings()
            assert cfg["sync_on_open"] is False and cfg["sync_telemetry"] == "none"
            for bad in ({"sync_on_open": "false"}, {"sync_telemetry": "all"}):
                try:
                    settings_io.save(bad)
                    raise AssertionError(f"accepted {bad}")
                except ValueError:
                    pass
    finally:
        _clear_env(old)


def test_review_params_validation():
    from debrief.desktop.server import clean_review_params
    assert clean_review_params({}) == {}
    ok = clean_review_params({"event": "EVRACE001", "session_type": 1, "track_id": 11, "car_id": 21})
    assert ok == {"event": "EVRACE001", "session_type": 1, "track_id": 11, "car_id": 21}
    for bad in ({"event": "../x", "session_type": 3, "track_id": 1, "car_id": 1},
                {"event": "E1", "session_type": 4, "track_id": 1, "car_id": 1},
                {"event": "E1", "session_type": True, "track_id": 1, "car_id": 1},
                {"event": "E1", "session_type": 3, "track_id": "x", "car_id": 1}):
        try:
            clean_review_params(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass


def test_a_review_pauses_a_running_sync():
    from debrief import app
    from debrief.desktop.server import Jobs
    real_sync, real_check, real_load = app.cmd_sync, app.cmd_check, app.load_settings
    seen = {}

    def slow_sync(cfg, progress=None, should_stop=None, open_after=False):
        while not should_stop():
            time.sleep(0.05)
        seen["stopped"] = True
        return {"stopped": True}

    app.cmd_sync = slow_sync
    app.cmd_check = lambda cfg, progress=None: ["ok"]
    app.load_settings = lambda: {}
    try:
        jobs = Jobs()
        assert jobs.start("sync")["ok"]
        time.sleep(0.2)
        assert jobs.kind == "sync"
        r = jobs.start("check")
        assert r["ok"], r
        assert seen.get("stopped")
        for _ in range(100):
            if not jobs.running:
                break
            time.sleep(0.02)
        assert jobs.snapshot()["kind"] == "check" and jobs.snapshot()["state"] == "done"
        # a sync doesn't push aside another sync or anything else that's running
        assert jobs.start("sync")["ok"]
        time.sleep(0.1)
        assert not jobs.start("sync")["ok"]
        jobs.pause_sync()
    finally:
        app.cmd_sync, app.cmd_check, app.load_settings = real_sync, real_check, real_load


def test_window_syncs_on_open_and_lists_sessions():
    from debrief import app
    from debrief.desktop.server import DebriefServer
    fake = FakeG61()
    srv, base = serve(fake)
    old = {k: os.environ.get(k) for k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR")}
    real_make = app.make_api
    app.make_api = lambda cfg: client(cfg["api_base"], cfg["cache_dir"])   # no 1 s pacing in tests
    ds = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _settings_env(tmp, base)
            ds = DebriefServer(heartbeat_timeout=3600)
            threading.Thread(target=ds.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def call(path, body=None):
                rq = urllib.request.Request(ds.url + path, headers={"X-Debrief": "1", "Content-Type": "application/json"},
                                            data=None if body is None else json.dumps(body).encode(),
                                            method="GET" if body is None else "POST")
                with opener.open(rq, timeout=20) as r:
                    return json.loads(r.read())

            j = call("api/sessions")
            assert j["ok"] and j["summary"]["synced"] is False and j["sessions"] == []
            r = call("api/sync", {"auto": True})
            assert r["ok"] and not r.get("skipped"), r
            for _ in range(300):
                s = call("api/status")
                if s["state"] != "running":
                    break
                time.sleep(0.05)
            assert s["state"] == "done" and s["kind"] == "sync", s
            assert s["result"]["sync"]["sessions"] == 25
            j = call("api/sessions")
            assert j["summary"]["synced"] and len(j["sessions"]) == 25
            first = j["sessions"][0]
            assert first["session"] == "Race" and first["review"] is None and first["track_id"] == 11
            # reopening the window straight away doesn't sync again
            assert call("api/sync", {"auto": True}).get("skipped") is True
            # ...but Sync now always does
            r = call("api/sync", {})
            assert r["ok"] and not r.get("skipped")
            for _ in range(300):
                if call("api/status")["state"] != "running":
                    break
                time.sleep(0.05)
            # a review of the race session is matched to the session row
            folder = Path(tmp) / "lib" / "reviews" / "2026-10-24_track11_car21_ace001"
            folder.mkdir(parents=True)
            (folder / "review.json").write_text("{}", encoding="utf-8")
            j = call("api/sessions")
            assert j["sessions"][0]["review"] == folder.name and j["sessions"][1]["review"] is None
            # switched off: opening the window doesn't sync
            from debrief.desktop import settings_io
            settings_io.save({"sync_on_open": False})
            assert call("api/sync", {"auto": True}).get("skipped") is True
    finally:
        app.make_api = real_make
        if ds is not None:
            ds.shutdown()
            ds.server_close()
        srv.shutdown()
        _clear_env(old)


def test_review_a_synced_practice_session():
    """Sessions page > Review on a practice session: reviewed on its own, in its own folder."""
    from debrief import app
    sys.path.insert(0, str(ROOT / "tests"))
    from test_desktop import _synthetic_csv
    fake = FakeG61()
    fake.csv_text = _synthetic_csv(99.5)
    srv, base = serve(fake)
    old = {k: os.environ.get(k) for k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR")}
    real_make = app.make_api
    app.make_api = lambda cfg: client(cfg["api_base"], cfg["cache_dir"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _settings_env(tmp, base, "max_reference_laps = 0\n")
            cfg = app.load_settings()
            app.cmd_sync(cfg)
            prac = app.cmd_review(cfg, "EVRACE001", open_after=False, session_kind=1, track_id=11, car_id=21)
            race = app.cmd_review(cfg, "EVRACE001", open_after=False, session_kind=3, track_id=11, car_id=21)
            assert prac.parent != race.parent and prac.parent.name.endswith("_practice")
            dp = json.loads((prac.parent / "review.json").read_text(encoding="utf-8"))
            dr = json.loads((race.parent / "review.json").read_text(encoding="utf-8"))
            assert dp["meta"]["session"] == "Practice" and len(dp["laps"]) == 12
            assert dr["meta"]["session"] == "Race" and len(dr["laps"]) == 20
    finally:
        app.make_api = real_make
        srv.shutdown()
        _clear_env(old)


if __name__ == "__main__":
    n = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            t0 = time.time()
            fn()
            n += 1
            print(f"ok    {k} ({time.time() - t0:.1f}s)")
    print(f"{n} tests passed")
