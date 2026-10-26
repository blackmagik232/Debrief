"""Desktop app + team hub tests.

    python3 tests/test_desktop.py        (or: python3 -m pytest tests/test_desktop.py)

The math tests run anywhere. The server tests start the desktop server in-process against the mock
Garage 61 API (DEBRIEF_MOCK, default /home/claude/devtools/mock_g61.py) and copy an old review
(DEBRIEF_OLD_REVIEWS, default /home/claude/devtools/oldlib/reviews) into a temporary library;
they are skipped if those aren't there. A full run takes about a minute (1 s request pacing).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MOCK = Path(os.environ.get("DEBRIEF_MOCK", "/home/claude/devtools/mock_g61.py"))
OLD = Path(os.environ.get("DEBRIEF_OLD_REVIEWS", "/home/claude/devtools/oldlib/reviews"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


# ================================================================ pure logic (no server)
def test_head_to_head_sign():
    from debrief import team
    combos = [{"track": "T", "track_short": "T", "car": "C", "kings": None,
               "board": [{"key": "me", "name": "Me", "t": 90.0}, {"key": "a", "name": "A", "t": 90.5},
                         {"key": "b", "name": "B", "t": 89.0}]},
              {"track": "U", "track_short": "U", "car": "C", "kings": None,
               "board": [{"key": "me", "name": "Me", "t": 80.0}, {"key": "a", "name": "A", "t": 79.8}]}]
    h = {p["key"]: p for p in team.head_to_heads(combos)}
    # gap = your best - theirs: negative means you are quicker
    assert h["a"]["combos"][0]["gap"] == -0.5 and h["a"]["combos"][1]["gap"] == 0.2
    assert h["a"]["ahead"] == 1 and h["a"]["behind"] == 1 and h["a"]["shared"] == 2
    assert abs(h["a"]["avg_gap"] - (-0.15)) < 1e-9
    assert h["b"]["combos"][0]["gap"] == 1.0 and h["b"]["behind"] == 1 and h["b"]["ahead"] == 0


def _synthetic_csv(lap_time, slow_at=None, n=1200):
    """A fake 3 km lap with three corners; slow_at=(index, mph) makes one corner slower."""
    import numpy as np
    p = np.linspace(0, 0.999, n)
    v = np.full(n, 60.0)  # m/s
    for k, c in enumerate((0.2, 0.5, 0.8)):
        dip = 25.0 - (slow_at[1] if slow_at and slow_at[0] == k else 0.0)
        v -= dip * np.exp(-((p - c) / 0.02) ** 2)
    rows = ["Speed,LapDistPct,Brake,Throttle,Gear,LatAccel,LongAccel,SteeringWheelAngle,RPM"]
    rows += [f"{a:.3f},{b:.5f},0,1,4,0,0,0,7000" for a, b in zip(v, p)]
    return "\n".join(rows)


def test_corner_kings_sign_and_reuse():
    from debrief import team
    # B is slower in corner 2 only (lower minimum speed there) -> A owns C2, B's gap there is positive
    entries = [{"key": "me", "name": "Me", "t": 60.0, "csv": _synthetic_csv(60.0), "is_me": True},
               {"key": "b", "name": "B", "t": 60.6, "csv": _synthetic_csv(60.6, slow_at=(1, 8.0)), "is_me": False}]
    k = team.corner_kings(entries, track_length=None)
    assert k is not None and len(k["corners"]) == 3, k and [c["id"] for c in k["corners"]]
    for z in k["corners"]:
        assert z["margin"] >= 0
        assert abs(z["king_t"] - min(z["times"].values())) < 1e-9
        assert abs(z["your_gap"] - (z["times"]["me"] - z["king_t"])) < 1e-9  # + = you slower
    assert sum(z["times"]["me"] for z in k["corners"]) - 60.0 < 0.01  # segments add up to the lap
    c2 = k["corners"][1]
    assert c2["king"] == "me" and c2["times"]["b"] > c2["times"]["me"]


def test_result_and_goal_validation():
    from debrief.desktop.server import clean_result_fields, clean_goal
    assert clean_result_fields({"finish_pos": "7", "field_size": 22, "notes": "ok", "sr_before": "2.41",
                                "start_pos": ""}) == {"finish_pos": 7, "field_size": 22, "notes": "ok", "sr_before": 2.41}
    for bad in ({}, {"finish_pos": 0}, {"finish_pos": 1.5}, {"finish_pos": 30, "field_size": 22},
                {"finish_pos": True}, {"finish_pos": 3, "notes": "x" * 501}, {"finish_pos": "abc"},
                {"finish_pos": 3, "sr_after": 11}):
        try:
            clean_result_fields(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    g = clean_goal({"track": "T", "car": "C", "target_s": 83.5, "note": "n", "id": "abc_1"})
    assert g == {"track": "T", "car": "C", "target_s": 83.5, "note": "n", "id": "abc_1"}
    for bad in ({"track": "T", "car": "C", "target_s": "83"}, {"track": "", "car": "C", "target_s": 83},
                {"track": "T", "car": "C", "target_s": 5}, {"track": "T", "car": "C", "target_s": 83, "id": "../x"}):
        try:
            clean_goal(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


# ================================================================ server against the mock API
ENV = {}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _setup():
    if ENV:
        return ENV
    if not MOCK.exists() or not OLD.is_dir():
        ENV["skip"] = f"mock API ({MOCK}) or old reviews ({OLD}) not found"
        return ENV
    port = _free_port()
    ENV["mock"] = subprocess.Popen([sys.executable, str(MOCK), str(port)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    tmp = Path(tempfile.mkdtemp(prefix="debrief-test-"))
    lib = tmp / "lib"
    (lib / "reviews").mkdir(parents=True)
    for d in OLD.iterdir():  # reviews written by the original Debrief (no new review.json keys)
        if (d / "review.json").exists():
            shutil.copytree(d, lib / "reviews" / d.name)
    ENV["review"] = sorted(p.name for p in (lib / "reviews").iterdir())[0]
    st = tmp / "settings.toml"
    st.write_text(f'token = "mock-token-123"\napi_base = "http://127.0.0.1:{port}/api/v1/"\n'
                  f"library_folder = '{lib}'\nmax_reference_laps = 3\ninclude_practice = false\n", encoding="utf-8")
    os.environ["DEBRIEF_SETTINGS"] = str(st)
    os.environ["DEBRIEF_CACHE_DIR"] = str(tmp / "cache")
    from debrief.desktop.server import DebriefServer
    srv = DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    ENV.update(tmp=tmp, lib=lib, settings=st, srv=srv, url=srv.url, port=port)
    return ENV


def _req(path, body=None, header=True, base=None, method=None):
    url = (base or ENV["url"]) + path
    data = None if body is None else json.dumps(body).encode()
    h = {"Content-Type": "application/json"} if data is not None else {}
    if header:
        h["X-Debrief"] = "1"
    rq = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(rq, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")


def _wait_job(timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = _req("api/status")
        if s.get("state") in ("done", "error"):
            return s
        time.sleep(0.3)
    raise AssertionError("job didn't finish")


def _need_server():
    e = _setup()
    if "skip" in e:
        print("  SKIPPED:", e["skip"])
        return False
    return True


def test_auth_required():
    if not _need_server():
        return
    wrong = f"http://127.0.0.1:{ENV['srv'].port}/not-the-secret/"
    for path in ("api/combos", "api/teams", "api/goals", "api/pages", f"api/result?review={ENV['review']}"):
        assert _req(path, base=wrong)[0] == 404, path           # no secret
        assert _req(path, header=False)[0] == 404, path         # secret but no X-Debrief header
    for path, body in (("api/prep", {"track_id": 1, "car_id": 1}), ("api/team", {}), ("api/goals", {"action": "save"}),
                       ("api/result", {"review": ENV["review"], "action": "delete"})):
        assert _req(path, body, base=wrong)[0] == 404, path
        assert _req(path, body, header=False)[0] == 404, path
    # wrong Host header (DNS rebinding) is refused too
    rq = urllib.request.Request(ENV["url"] + "api/goals", headers={"X-Debrief": "1", "Host": "evil.example"})
    try:
        urllib.request.urlopen(rq, timeout=10)
        raise AssertionError("bad host accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    # oversized body
    big = {"action": "save", "goal": {"note": "x" * 70000}}
    assert _req("api/goals", big)[0] == 400


def test_combos_teams_settings():
    if not _need_server():
        return
    code, j = _req("api/combos")
    assert code == 200 and j["ok"] and j["requests"] == 0, j        # cache only: never contacts Garage 61
    assert j["tracks"] is None and j["cars"] is None                # nothing cached yet
    assert len(j["combos"]) >= 1 and j["combos"][0]["track_id"] is None   # old review: no stored ids
    code, j = _req("api/combos?fetch=1")
    assert code == 200 and 1 <= j["requests"] <= 2 and len(j["tracks"]) > 100, (j["requests"], j.get("error"))
    assert j["combos"][0]["track_id"] == 468 and j["combos"][0]["car_id"] == 190   # matched by name
    code, j = _req("api/combos?fetch=1")
    assert j["requests"] == 0                                        # now cached
    code, j = _req("api/teams")
    assert code == 200 and j["teams"] is None and j["requests"] == 0 and j["team_slug"] == "midfield-menace"
    code, j = _req("api/teams?fetch=1")
    assert j["requests"] == 1 and {t["slug"] for t in j["teams"]} >= {"midfield-menace", "overdriveracademy"}
    # settings: team picker + optional iRacing credentials (never shown back)
    code, j = _req("api/settings", {"team_slug": "OverdriverAcademy"})
    assert code == 200 and j["settings"]["team_slug"] == "overdriveracademy"
    assert _req("api/settings", {"team_slug": "bad slug!"})[0] == 400
    code, j = _req("api/settings", {"iracing_client_id": "client-abc", "iracing_client_secret": "very-secret-xyz"})
    assert code == 200 and j["settings"]["iracing_client_id_saved"] and j["settings"]["iracing_client_secret_saved"]
    _, view = _req("api/settings")
    blob = json.dumps(view)
    assert "very-secret-xyz" not in blob and "client-abc" not in blob and "mock-token-123" not in blob
    assert "very-secret-xyz" in ENV["settings"].read_text()
    assert _req("api/settings", {"iracing_client_secret": "has space"})[0] == 400
    code, j = _req("api/settings", {"clear_iracing": True})
    assert not j["settings"]["iracing_client_id_saved"] and not j["settings"]["iracing_client_secret_saved"]
    code, j = _req("api/settings", {"team_slug": "midfield-menace"})
    assert j["settings"]["team_slug"] == "midfield-menace"
    from debrief import app
    assert app.load_settings()["team_slug"] == "midfield-menace"


def test_goals_endpoints():
    if not _need_server():
        return
    _, j = _req("api/goals")
    assert j["ok"] and j["goals"] == []
    g = {"track": "Charlotte Motor Speedway (Roval 2019)", "car": "Ford Mustang GT4", "target_s": 83.0, "note": "beat Jake"}
    code, j = _req("api/goals", {"action": "save", "goal": g})
    assert code == 200 and j["ok"], j
    gid = j["goal"]["id"]
    code, j = _req("api/goals", {"action": "save", "goal": {**g, "id": gid, "target_s": 84.0}})
    assert j["ok"]
    _, j = _req("api/goals")
    assert len(j["goals"]) == 1 and j["goals"][0]["target_s"] == 84.0
    assert j["progress"][0]["achieved"] is True                     # old review best 1:23.654 < 1:24.000
    assert _req("api/goals", {"action": "save", "goal": {**g, "target_s": "fast"}})[0] == 400
    assert _req("api/goals", {"action": "nuke"})[0] == 400
    assert _req("api/goals", {"action": "delete", "id": "../../x"})[0] == 400
    code, j = _req("api/goals", {"action": "delete", "id": gid})
    assert code == 200 and j["deleted"] is True
    _, j = _req("api/goals")
    assert j["goals"] == []


def test_result_endpoints():
    if not _need_server():
        return
    rv = ENV["review"]
    code, j = _req(f"api/result?review={rv}")
    assert code == 200 and j["result"] is None
    assert _req("api/result?review=..%2F..%2Fetc")[0] == 400
    assert _req("api/result?review=nope")[0] == 400
    assert _req("api/result", {"review": rv, "fields": {"finish_pos": 25, "field_size": 20}})[0] == 400
    assert _req("api/result", {"review": rv, "fields": {"start_pos": 3}})[0] == 400       # finish needed
    code, j = _req("api/result", {"review": rv, "action": "save",
                                  "fields": {"finish_pos": 6, "start_pos": 12, "field_size": 22, "incidents": 4,
                                             "notes": "<script>x</script>"}})
    assert code == 200 and j["ok"] and j["result"]["finish_pos"] == 6, j
    data = json.loads((ENV["lib"] / "reviews" / rv / "review.json").read_text())
    assert data["result"]["finish_pos"] == 6
    assert "<script>x</script>" not in (ENV["lib"] / "reviews" / rv / "index.html").read_text()  # embedded safely
    _, j = _req(f"api/result?review={rv}")
    assert j["result"]["incidents"] == 4
    code, j = _req("api/result", {"review": rv, "action": "delete"})
    assert code == 200 and j["deleted"] is True
    _, j = _req(f"api/result?review={rv}")
    assert j["result"] is None


def test_prep_and_team_jobs():
    if not _need_server():
        return
    assert _req("api/prep", {"track_id": "abc", "car_id": 190})[0] == 400
    assert _req("api/prep", {"track_id": -1, "car_id": 190})[0] == 400
    assert _req("api/prep", {"track_id": True, "car_id": 190})[0] == 400
    code, j = _req("api/prep", {"track_id": 468, "car_id": 190})
    assert code == 200 and j["ok"], j
    busy = _req("api/team", {})[1]                                   # one job at a time
    assert busy["ok"] is False and "busy" in busy["error"]
    s = _wait_job()
    assert s["state"] == "done" and s["kind"] == "prep", s
    page = ENV["lib"] / s["result"]["page"]
    assert page.is_file() and s["result"]["page"].startswith("prep/")
    code, j = _req("api/team", {})
    assert j["ok"], j
    s = _wait_job()
    assert s["state"] == "done" and s["kind"] == "team", s
    assert s["result"]["page"] == "team/index.html"
    data = json.loads((ENV["lib"] / "team" / "team.json").read_text())
    assert 0 < data["requests"] <= 58, data["requests"]             # stated worst case
    html = (ENV["lib"] / "team" / "index.html").read_text()
    assert "/*TEAM*/null" not in html and "mock-token-123" not in html
    c = data["combos"][0]
    board = c["board"]
    assert [r["pos"] for r in board] == list(range(1, len(board) + 1))
    assert all(r["gap"] >= 0 for r in board) and board[0]["gap"] == 0
    assert sum(r["is_me"] for r in board) == 1 and {r["name"] for r in board} >= {"Jake Morrow", "Tyler Reyes", "Dom Castillo"}
    k = c["kings"]
    assert k and k["corners"] and "me" in k["drivers"]
    for z in k["corners"]:
        assert z["margin"] >= 0 and abs(z["your_gap"] - (z["times"]["me"] - z["king_t"])) < 0.002
    for p in data["h2h"]:
        for e in p["combos"]:
            assert abs(e["gap"] - (e["you"] - e["them"])) < 0.002
    _, pages = _req("api/pages")
    assert pages["team"] == "team/index.html" and pages["prep"]
    # a second refresh straight away reuses the cache: no requests
    _req("api/team", {})
    _wait_job()
    assert json.loads((ENV["lib"] / "team" / "team.json").read_text())["requests"] == 0
    # pages are served inside the window
    with urllib.request.urlopen(ENV["url"] + "lib/team/index.html", timeout=10) as r:
        assert r.status == 200


def test_heartbeat_never_stops_mid_job():
    if not _need_server():
        return
    from debrief.desktop.server import DebriefServer
    os.environ["DEBRIEF_CACHE_DIR"] = str(ENV["tmp"] / "cache-cold")   # cold cache = a job that takes a while
    try:
        srv = DebriefServer(heartbeat_timeout=1.5, startup_grace=0)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        threading.Thread(target=srv.watchdog, daemon=True).start()
        code, j = _req("api/team", {}, base=srv.url)
        assert j["ok"], j
        time.sleep(4)                                                # well past the heartbeat timeout
        assert not srv.stopping.is_set() and srv.jobs.running, "stopped mid-job"
        t0 = time.time()
        while srv.jobs.running and time.time() - t0 < 240:
            time.sleep(0.3)
        assert srv.jobs.snapshot()["state"] == "done", srv.jobs.snapshot()
        t0 = time.time()
        while not srv.stopping.is_set() and time.time() - t0 < 10:
            time.sleep(0.2)
        assert srv.stopping.is_set(), "didn't stop once idle"
        srv.server_close()
    finally:
        os.environ["DEBRIEF_CACHE_DIR"] = str(ENV["tmp"] / "cache")


def test_bye_stops_idle_server_quickly():
    if not _need_server():
        return
    from debrief.desktop.server import DebriefServer
    srv = DebriefServer(heartbeat_timeout=90)       # the real defaults: 90 s timeout, 90 s start-up grace
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    threading.Thread(target=srv.watchdog, daemon=True).start()
    assert _req("api/ping", {}, base=srv.url)[0] == 200
    assert _req("api/bye", {}, header=False, base=srv.url)[0] == 404   # needs the header too
    t0 = time.time()
    assert _req("api/bye", {}, base=srv.url)[0] == 200                  # what the window sends on close
    while not srv.stopping.is_set() and time.time() - t0 < 15:
        time.sleep(0.2)
    assert srv.stopping.is_set() and time.time() - t0 < 8, "didn't stop after the window closed"
    srv.server_close()


# ================================================================ regression tests (integration fixes)
class _FakeTeamApi:
    """Just enough of the Garage 61 client for team.build, without any network."""
    requests_made = 0

    def me(self):
        return {"slug": "me", "firstName": "Swazi", "lastName": "Williams"}

    def teams(self):
        return [{"slug": "midfield-menace", "name": "Midfield Menace"}]

    def cached_items(self, name):
        return []

    def tracks(self):
        return []

    def cars(self):
        return []

    def has_csv(self, lid):
        return False

    def lap_csv(self, lid):
        return None

    def find_laps(self, **kw):
        mk = lambda slug, t, **x: {"id": slug + str(t), "lapTime": t, "clean": True, "startTime": "2026-10-01T20:00:00Z",
                                   "driver": {"slug": slug, "firstName": slug.title(), "lastName": ""}, **x}
        if kw.get("drivers") == ["me"]:
            return [mk("me", 90.2), mk("me", 92.0), mk("me", 7.0, incomplete=True)]
        laps = [mk("jake", 70.0), mk("tyler", 6.0, incomplete=True), mk("dom", 91.0)]
        if kw.get("group") == "none":
            laps += [mk("tyler", 92.5), mk("tyler", 11.0)]
        return laps


def _lib_review(folder, name, best, started, clean=None, ids=(468, 190)):
    d = Path(folder) / "reviews" / name
    d.mkdir(parents=True, exist_ok=True)
    b = {"n": 2, "t": best}
    if clean is not None:
        b["clean"] = clean
    (d / "review.json").write_text(json.dumps({
        "meta": {"track": "Charlotte Motor Speedway (Roval 2019)", "car": "Ford Mustang GT4", "started_at": started},
        "best": b, "median": 85.0, "combo_ids": {"track_id": ids[0], "car_id": ids[1]},
        "laps": [{"n": 1, "t": 85.0, "clean": True}, {"n": 2, "t": best, "clean": True}]}), encoding="utf-8")


def test_team_board_keeps_much_quicker_teammate_drops_partial_laps():
    from debrief import team
    tmp = Path(tempfile.mkdtemp(prefix="debrief-test-"))
    try:
        _lib_review(tmp, "a", 90.0, "2026-10-01T20:00:00+00:00")
        data = team.build(_FakeTeamApi(), tmp, "midfield-menace", log=lambda m: None)
        board = data["combos"][0]["board"]
        got = {r["key"]: r["t"] for r in board}
        assert got.get("jake") == 70.0, got               # 22% quicker than you: kept
        assert got.get("tyler") == 92.5, got              # partial 6 s / 11 s laps dropped, full lap kept
        assert got.get("me") == 90.0 and got.get("dom") == 91.0
        assert team.plausible(70.0, team.anchor_for([70.0, 91.0], 90.0)) and not team.plausible(11.0, 90.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_team_library_combos_ignore_unclean_best():
    from debrief import team
    tmp = Path(tempfile.mkdtemp(prefix="debrief-test-"))
    try:
        _lib_review(tmp, "a", 84.5, "2026-09-01T20:00:00+00:00")
        _lib_review(tmp, "b", 83.0, "2026-09-08T20:00:00+00:00", clean=False)   # every lap flagged
        _lib_review(tmp, "c", 6.9, "2026-09-10T20:00:00+00:00")                 # old review, partial "best"
        c = team.library_combos(tmp)
        assert len(c) == 1 and c[0]["best"] == 84.5 and c[0]["races"] == 3, c
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_team_page_names_cant_break_the_script():
    from debrief import team
    nasty = "/*TEAM*/null <!--<script> </script> & Co"
    tmp = Path(tempfile.mkdtemp(prefix="debrief-test-"))
    try:
        data = {"version": 1, "team": {"slug": "midfield-menace", "name": nasty}, "me": {"name": nasty, "key": "me"},
                "generated_at": "2026-10-26T00:00:00+00:00", "requests": 0, "notes": [nasty],
                "combos": [{"track": nasty, "track_short": nasty, "car": nasty, "track_id": 468, "car_id": 190,
                            "races": 1, "last_date": "2026-10-01", "kings": None, "note": None,
                            "board": [{"key": "me", "name": nasty, "t": 90.0, "is_me": True, "pos": 1, "gap": 0.0,
                                       "date": "2026-10-01"}]}], "h2h": []}
        page = team.write(tmp, data)
        html = page.read_text(encoding="utf-8")
        assert "<!--<script>" not in html and nasty not in html
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("  SKIPPED (browser part): playwright not installed")
            return
        errs = []
        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch()
            except Exception:  # noqa: BLE001  (Playwright installed without its browser, e.g. on CI)
                print("  SKIPPED (browser part): no Chromium for Playwright")
                return
            pg = b.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(page.resolve().as_uri())
            pg.wait_for_timeout(300)
            name = pg.evaluate("window.TEAM && window.TEAM.team.name")
            b.close()
        assert not errs and name == nasty, (errs, name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_view_greets_from_cache_only():
    """first_name comes from the saved `me` answer only: no request, no folders created, no token shown."""
    import hashlib
    from debrief import app
    from debrief.desktop import settings_io
    tmp = Path(tempfile.mkdtemp(prefix="debrief-test-"))
    keep = {k: os.environ.get(k) for k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR")}
    try:
        st = tmp / "settings.toml"
        base = f"http://127.0.0.1:{_free_port()}/api/v1/"   # nothing listens here: any request would fail
        st.write_text(f'token = "greet-token-123"\napi_base = "{base}"\nlibrary_folder = \'{tmp / "lib"}\'\n',
                      encoding="utf-8")
        os.environ["DEBRIEF_SETTINGS"] = str(st)
        os.environ["DEBRIEF_CACHE_DIR"] = str(tmp / "cache")
        v = settings_io.public_view()
        assert v["first_name"] is None and not (tmp / "cache").exists(), v
        assert v["version"] and v["installed"] is app.FROZEN and v["data_folder"] == str(app.HERE)
        ns = hashlib.sha256((base + "|greet-token-123").encode()).hexdigest()[:10]   # Garage61's cache layout
        d = tmp / "cache" / ns
        d.mkdir(parents=True)
        (d / (hashlib.sha1(b"me").hexdigest()[:20] + ".json")).write_text(
            json.dumps({"slug": "x", "firstName": "Jake", "lastName": "Morrow"}), encoding="utf-8")
        v = settings_io.public_view()
        assert v["first_name"] == "Jake", v
        assert "greet-token-123" not in json.dumps(v) and "Morrow" not in json.dumps(v)
        (d / (hashlib.sha1(b"me").hexdigest()[:20] + ".json")).write_text(
            json.dumps({"firstName": "<img src=x>"}), encoding="utf-8")
        assert settings_io.public_view()["first_name"] is None      # odd names are dropped, not shown
    finally:
        for k, val in keep.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        shutil.rmtree(tmp, ignore_errors=True)


def test_frozen_paths_and_first_start():
    """As the installed app (sys.frozen): data home in LOCALAPPDATA (or ~/.local/share off Windows),
    resources from sys._MEIPASS, settings.toml created from the example on first start."""
    tmp = Path(tempfile.mkdtemp(prefix="debrief-test-"))
    try:
        res = tmp / "bundle"
        res.mkdir()
        shutil.copy(ROOT / "settings.example.toml", res / "settings.example.toml")
        code = ("import sys, json; sys.frozen = True; sys._MEIPASS = sys.argv[1]\n"
                "from debrief import app\n"
                "created = app.ensure_settings()\n"
                "cfg = app.load_settings()\n"
                "from debrief.desktop import settings_io\n"
                "v = settings_io.public_view()\n"
                "print(json.dumps({'here': str(app.HERE), 'res': str(app.RES), 'created': created,\n"
                "  'again': app.ensure_settings(), 'cache': cfg['cache_dir'], 'installed': v['installed'],\n"
                "  'token_saved': v['token_saved'], 'label': app.data_folder_label()}))")
        env = {k: v for k, v in os.environ.items() if k not in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR")}
        env.update(HOME=str(tmp / "home"), USERPROFILE=str(tmp / "home"), LOCALAPPDATA=str(tmp / "lad"),
                   PYTHONPATH=str(ROOT))
        out = subprocess.run([sys.executable, "-c", code, str(res)], env=env, capture_output=True, text=True,
                             timeout=60, cwd=str(tmp))
        assert out.returncode == 0, out.stderr
        r = json.loads(out.stdout.strip().splitlines()[-1])
        want = (tmp / "lad" / "Debrief") if os.name == "nt" else (tmp / "home" / ".local" / "share" / "Debrief")
        assert Path(r["here"]) == want and Path(r["res"]) == res, r
        assert r["created"] is True and r["again"] is False and (want / "settings.toml").is_file(), r
        assert Path(r["cache"]) == want / ".cache" and r["installed"] is True and r["token_saved"] is False, r
        assert str(want) in r["label"] and "Setup.bat" not in (want / "settings.toml").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clean_goal_keeps_garage61_ids():
    from debrief.desktop.server import clean_goal
    g = clean_goal({"track": "T", "car": "C", "target_s": 83.5, "track_id": 468, "car_id": 190})
    assert g["track_id"] == 468 and g["car_id"] == 190
    assert "track_id" not in clean_goal({"track": "T", "car": "C", "target_s": 83.5})
    for bad in ({"track_id": "abc"}, {"track_id": -3}, {"car_id": 1.5}, {"car_id": True}, {"track_id": float("inf")},
                {"track_id": float("nan")}, {"target_s": float("nan")}, {"target_s": float("inf")}, {"target_s": 10 ** 400}):
        try:
            clean_goal({"track": "T", "car": "C", "target_s": 83.5, **bad})
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def _raw_http(raw: bytes, timeout=4.0):
    """Send raw bytes to the test server. Returns (response bytes, closed_by_server)."""
    s = socket.create_connection(("127.0.0.1", ENV["srv"].port), timeout)
    s.sendall(raw)
    data, closed = b"", False
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                closed = True
                break
            data += chunk
    except socket.timeout:
        pass
    finally:
        s.close()
    return data, closed


def _post_raw(path, body: bytes, length=None):
    port, sec = ENV["srv"].port, ENV["srv"].secret
    head = (f"POST /{sec}/{path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Debrief: 1\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body) if length is None else length}\r\n\r\n")
    return _raw_http(head.encode() + body)


def test_bad_content_length_and_oversize_bodies():
    if not _need_server():
        return
    for length in ("-1", "abc", "1e3", ""):
        data, closed = _post_raw("api/goals", b'{"action":"x"}', length=length)
        assert data.startswith(b"HTTP/1.1 400"), (length, data[:80])
        assert closed, f"connection left open after Content-Length {length!r}"
    big = b'{"action":"save","goal":{"note":"' + b"x" * 70000 + b'"}}'
    data, closed = _post_raw("api/goals", big)
    assert data.startswith(b"HTTP/1.1 400") and data.count(b"HTTP/1.1 ") == 1 and closed, data[:200]
    # a normal request still works afterwards
    assert _req("api/goals")[0] == 200


def test_non_finite_and_huge_numbers_get_a_friendly_400():
    if not _need_server():
        return
    rv = ENV["review"]
    for v in ("1e400", "NaN", "Infinity", "-Infinity", "1" + "0" * 400):
        body = ('{"review":"%s","action":"save","fields":{"finish_pos":%s}}' % (rv, v)).encode()
        code, j = _req_raw("api/result", body)
        assert code == 400 and ("number" in j["error"] or "between" in j["error"]), (v, code, j)
        for path, b in (("api/prep", '{"track_id":%s,"car_id":190}' % v),
                        ("api/goals", '{"action":"save","goal":{"track":"T","car":"C","target_s":%s}}' % v),
                        ("api/goals", '{"action":"save","goal":{"track":"T","car":"C","target_s":83,"track_id":%s}}' % v)):
            code, j = _req_raw(path, b.encode())
            assert code == 400 and j["ok"] is False and "went wrong" not in j["error"], (path, v, code, j)


def _req_raw(path, body: bytes):
    rq = urllib.request.Request(ENV["url"] + path, data=body, headers={"X-Debrief": "1", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_goals_show_on_library_page_and_keep_ids():
    if not _need_server():
        return
    lib = ENV["lib"]
    with urllib.request.urlopen(ENV["url"] + "lib/index.html", timeout=30) as r:
        assert r.status == 200
    g = {"track": "Charlotte Motor Speedway (Roval 2019)", "car": "Ford Mustang GT4", "target_s": 83.25,
         "track_id": 468, "car_id": 190}
    code, j = _req("api/goals", {"action": "save", "goal": g})
    assert code == 200 and j["ok"], j
    gid = j["goal"]["id"]
    assert j["goal"]["track_id"] == 468 and j["goal"]["car_id"] == 190
    assert gid in (lib / "index.html").read_text(encoding="utf-8")       # rebuilt right after the save
    code, j = _req("api/goals", {"action": "delete", "id": gid})
    assert j["deleted"] and gid not in (lib / "index.html").read_text(encoding="utf-8")
    # goals.json changed some other way (or a rebuild failed): the next page load rebuilds it
    from debrief import goals
    time.sleep(0.05)
    g2 = goals.save_goal(lib, {**g, "target_s": 83.5})
    os.utime(lib / "goals.json", (time.time() + 5, time.time() + 5))
    with urllib.request.urlopen(ENV["url"] + "lib/index.html", timeout=30) as r:
        assert g2["id"] in r.read().decode("utf-8")
    goals.delete_goal(lib, g2["id"])


def test_ui_team_load_shows_request_count():
    if not _need_server():
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIPPED: playwright not installed")
        return
    errs = []
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception:  # noqa: BLE001  (Playwright installed without its browser, e.g. on CI)
            print("  SKIPPED: no Chromium for Playwright")
            return
        try:
            pg = b.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(ENV["url"])
            pg.evaluate("document.querySelector('.navbtn[data-nav=settings]').click()")
            pg.wait_for_function("document.getElementById('teamStatus').textContent.length > 0", timeout=30000)
            pg.wait_for_timeout(500)
            # the button only shows before the team list is saved on this PC; press it either way
            pg.evaluate("document.getElementById('btnTeams').click()")
            pg.wait_for_function("document.getElementById('teamSetMsg').textContent.includes('loaded')", timeout=30000)
            msg = pg.text_content("#teamSetMsg")
        finally:
            b.close()
    assert "Garage 61 request" in msg, msg
    assert not errs, errs


def test_rereview_keeps_result_and_survives_locked_library():
    if not _need_server():
        return
    from debrief import library
    code, j = _req("api/review", {})
    assert j["ok"], j
    s = _wait_job(300)
    assert s["state"] == "done", s
    rv = s["result"]["review"]
    code, j = _req("api/result", {"review": rv, "action": "save", "fields": {"finish_pos": 4, "field_size": 18}})
    assert code == 200 and j["ok"], j
    # 1) re-review the same race: the result typed in earlier stays on the page
    _req("api/review", {})
    s = _wait_job(300)
    assert s["state"] == "done", s
    data = json.loads((ENV["lib"] / "reviews" / rv / "review.json").read_text(encoding="utf-8"))
    assert data.get("result", {}).get("finish_pos") == 4, data.get("result")
    # 2) the library page can't be written (locked) after the review is saved: still a success
    real = library.rebuild

    def locked(folder):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")
    library.rebuild = locked          # e.g. antivirus holding index.html right after the review is saved
    try:
        _req("api/review", {})
        s = _wait_job(300)
    finally:
        library.rebuild = real
    assert s["state"] == "done", s    # the review itself was saved, so this isn't a failure
    data = json.loads((ENV["lib"] / "reviews" / rv / "review.json").read_text(encoding="utf-8"))
    assert data.get("result", {}).get("finish_pos") == 4, data.get("result")
    html = (ENV["lib"] / "reviews" / rv / "index.html").read_text(encoding="utf-8")
    assert '"finish_pos":4' in html


def _teardown():
    if ENV.get("srv"):
        ENV["srv"].shutdown()
        ENV["srv"].server_close()
    if ENV.get("mock"):
        ENV["mock"].terminate()
    if ENV.get("tmp"):
        shutil.rmtree(ENV["tmp"], ignore_errors=True)


def teardown_module(module=None):  # pytest hook
    _teardown()


if __name__ == "__main__":
    fails = 0
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t0 = time.time()
            try:
                t()
                print(f"ok    {t.__name__} ({time.time() - t0:.1f}s)")
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                print(f"FAIL  {t.__name__}: {e}")
                traceback.print_exc()
    finally:
        _teardown()
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
