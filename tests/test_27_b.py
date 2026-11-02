"""Debrief 2.7.0 "Trust", Team B: fast offline detection (M1), the review's telemetry circuit breaker (M2),
a broken speed channel (M5), library backups before a re-render (M7), one atomic write (L1) and the test
runner's offline environment (6a).

    python3 tests/ci_run.py tests/test_27_b.py

Never contacts Garage 61 or GitHub: a fake Garage 61 on 127.0.0.1, fake clients, or patched requests.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-27b-"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
for _k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR", "DEBRIEF_SCHEDULE_DIR", "DEBRIEF_UPDATE_STATE"):
    os.environ.setdefault(_k, str(TMP / _k.lower()))

import numpy as np  # noqa: E402
import requests  # noqa: E402

from debrief import app, fsutil, g61, report, sync  # noqa: E402
from debrief.g61 import Garage61Error  # noqa: E402

QUIET = lambda m: None  # noqa: E731


def _newdir() -> Path:
    return Path(tempfile.mkdtemp(dir=TMP))


def _csv(lap_time=99.5, n=1200, zero_speed=False, slow_at=None):
    """A fake 3 km lap with three corners (as test_desktop's); zero_speed=True makes a broken Speed channel."""
    p = np.linspace(0, 0.999, n)
    v = np.zeros(n) if zero_speed else np.full(n, 60.0)
    if not zero_speed:
        for k, c in enumerate((0.2, 0.5, 0.8)):
            dip = 25.0 - (slow_at[1] if slow_at and slow_at[0] == k else 0.0)
            v -= dip * np.exp(-((p - c) / 0.02) ** 2)
    rows = ["Speed,LapDistPct,Brake,Throttle,Gear,LatAccel,LongAccel,SteeringWheelAngle,RPM"]
    rows += [f"{a:.3f},{b:.5f},0,1,4,0,0,0,7000" for a, b in zip(v, p)]
    return "\n".join(rows)


# ================================================================ fake Garage 61 (HTTP, for the client tests)
class _Fake:
    def __init__(self):
        self.log = []
        self.lock = threading.Lock()
        self.status = {}      # path -> forced status
        self.fail_csv = {}    # lap id -> status for its csv
        self.csv_text = "Speed,LapDistPct\n10,0.1\n20,0.2\n"

    def handle(self, path, q):
        code = self.status.get(path, 200)
        if code != 200:
            return code, "server detail", "text/plain"
        if path == "me":
            return 200, {"slug": "me", "firstName": "Test"}, "application/json"
        if path.startswith("laps/") and path.endswith("/csv"):
            lid = path.split("/")[1]
            if lid in self.fail_csv:
                return self.fail_csv[lid], "down", "text/plain"
            return 200, self.csv_text, "text/csv"
        if path in ("tracks", "cars", "teams"):
            return 200, {"items": []}, "application/json"
        return 404, None, "application/json"


def _serve(fake: _Fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path[len("/api/v1/"):]
            with fake.lock:
                fake.log.append((path, q))
            code, body, ctype = fake.handle(path, q)
            raw = b"" if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/api/v1/"


def _client(base, cache=None, logs=None, **kw):
    kw.setdefault("backoff_base", 0)
    return g61.Garage61("test-token-123456", Path(cache or _newdir()), min_gap_s=0.0, base_url=base,
                        log=(logs.append if logs is not None else QUIET), **kw)


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ================================================================ 1. fast offline detection (M1)
def test_connection_refused_fails_after_one_quick_retry():
    """Wi-Fi off (connection/DNS error): 2 attempts, not 5, and no 5/10/20/40 s backoff."""
    logs = []
    c = _client(f"http://127.0.0.1:{_free_port()}/api/v1/", logs=logs, backoff_base=5)
    c.OFFLINE_RETRY_S = 0   # the one quick retry needn't wait in the test
    t0 = time.monotonic()
    try:
        c.me()
        raise AssertionError("no error")
    except Garage61Error as e:
        assert str(e) == g61.MSG_OFFLINE and e.status is None
    took = time.monotonic() - t0
    assert c.requests_made == 2, c.requests_made
    assert took < 10, took   # Windows spends ~2 s per refused connect; the old path took 75 s
    assert any("unreachable" in l and "/me" in l for l in logs), logs


def test_offline_retry_wait_is_short_and_never_more_than_backoff_base():
    """With the default BACKOFF_BASE the one retry waits OFFLINE_RETRY_S (2 s), and with backoff 0 it waits 0."""
    waits = []
    c = _client(f"http://127.0.0.1:{_free_port()}/api/v1/", backoff_base=5)
    c._sleep = lambda s: waits.append(s)
    try:
        c.me()
    except Garage61Error:
        pass
    assert waits == [2], waits
    waits.clear()
    c0 = _client(f"http://127.0.0.1:{_free_port()}/api/v1/")
    c0._sleep = lambda s: waits.append(s)
    try:
        c0.me()
    except Garage61Error:
        pass
    assert waits == [0], waits
    assert g61.Garage61.BACKOFF_BASE == 5 and g61.Garage61.OFFLINE_RETRY_S == 2


def test_timeouts_and_5xx_keep_the_backoff():
    """A timeout (the host is there but slow) and a 5xx keep the 5-attempt backoff, the offline shortcut
    only applies to connection/DNS failures."""
    waits = []
    # a fake session, never a socket; the host is loopback so the runner's DEBRIEF_NO_NETWORK lets it through
    c = _client("http://localhost:9/api/v1/", backoff_base=5)
    c._sleep = lambda s: waits.append(s)
    calls = []

    class S:
        def get(self, url, timeout=30):
            calls.append(url)
            raise requests.ReadTimeout("read timed out")
    c.s = S()
    try:
        c.me()
        raise AssertionError("no error")
    except Garage61Error as e:
        assert str(e) == g61.MSG_OFFLINE
    assert len(calls) == 5 and waits == [5, 10, 20, 40], (len(calls), waits)
    # a ConnectionError that wraps a timeout (urllib3's ConnectTimeout) counts as a timeout too
    waits.clear(); calls.clear()

    class S2:
        def get(self, url, timeout=30):
            calls.append(url)
            raise requests.ConnectTimeout("HTTPSConnectionPool: connect timed out")
    c.s = S2()
    try:
        c.me()
    except Garage61Error:
        pass
    assert len(calls) == 5 and len(waits) == 4, (len(calls), waits)
    # a plain ConnectionError through the same fake session: two attempts, one short wait
    waits.clear(); calls.clear()

    class S3:
        def get(self, url, timeout=30):
            calls.append(url)
            raise requests.ConnectionError("[Errno 11001] getaddrinfo failed")
    c.s = S3()
    try:
        c.me()
    except Garage61Error as e:
        assert str(e) == g61.MSG_OFFLINE
    assert len(calls) == 2 and waits == [2], (len(calls), waits)
    # 5xx: five attempts (backoff 0 in the fake)
    fake = _Fake()
    fake.status["me"] = 503
    srv, base = _serve(fake)
    try:
        c = _client(base)
        try:
            c.me()
            raise AssertionError("no error")
        except Garage61Error as e:
            assert str(e) == g61.MSG_SERVER.format(code=503) and e.status == 503
        assert len([p for p, _ in fake.log if p == "me"]) == 5
    finally:
        srv.shutdown()


def test_is_timeout_helper():
    assert g61._is_timeout(requests.Timeout("x"))
    assert g61._is_timeout(requests.ConnectTimeout("x"))
    assert g61._is_timeout(requests.ConnectionError("Read timed out."))
    assert not g61._is_timeout(requests.ConnectionError("[Errno 111] Connection refused"))
    assert not g61._is_timeout(requests.ConnectionError("Name or service not known"))
    inner = OSError("timed out")
    outer = requests.ConnectionError("wrapped")
    outer.__cause__ = inner
    assert g61._is_timeout(outer)


# ================================================================ 2. telemetry circuit breaker (M2)
class _ReviewApi:
    """Enough of Garage61 for cmd_review: one race of laps, telemetry per lap id with optional failures."""

    def __init__(self, n=8, fail=None, none=(), csv=None):
        self.requests_made = 0
        self.fail = dict(fail or {})   # lap id -> Garage61Error to raise
        self.none = set(none)
        self.csv = csv or {}
        self.calls = []
        now = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        self.laps = [{"id": f"L{i}", "track": {"id": 468, "name": "Charlotte Motor Speedway", "variant": "Roval 2019"},
                      "car": {"id": 190, "name": "Ford Mustang GT4"}, "event": "EV1", "sessionType": 3,
                      "startTime": (now + timedelta(minutes=2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "lapTime": 99.5 + (i % 3) * 0.2, "clean": True, "driver": {"slug": "me"}}
                     for i in range(n)]

    def me(self):
        return {"slug": "me", "firstName": "Test", "lastName": "Driver"}

    def teams(self, refresh=False):
        return []

    def cars(self, refresh=False):
        return [{"id": 190, "name": "Ford Mustang GT4"}]

    def tracks(self, refresh=False):
        return [{"id": 468, "name": "Charlotte Motor Speedway", "variant": "Roval 2019", "lapLength": 3.0}]

    def find_laps(self, fresh=False, **params):
        self.requests_made += 1
        if params.get("drivers") == ["following"]:
            return []
        return list(self.laps)

    def lap_csv(self, lap_id):
        self.calls.append(lap_id)
        self.requests_made += 1
        if lap_id in self.fail:
            raise self.fail[lap_id]
        if lap_id in self.none:
            return None
        return self.csv.get(lap_id, _csv(99.5))


def _cfg(extra=None):
    lib = _newdir() / "lib"
    cfg = {"token": "test-token-123456", "library_folder": str(lib), "cache_dir": str(_newdir()),
           "include_practice": False, "max_reference_laps": 0, "salutation": ""}
    cfg.update(extra or {})
    return cfg


def _review(api, cfg=None):
    cfg = cfg or _cfg()
    real_make, real_say = app.make_api, app.say
    lines = []
    app.make_api = lambda c: api
    app.say = lines.append
    try:
        page = app.cmd_review(cfg, "EV1", open_after=False, session_kind=3, track_id=468, car_id=190)
        return page, lines, None
    except SystemExit as e:
        return None, lines, str(e)
    finally:
        app.make_api, app.say = real_make, real_say


def test_three_failures_in_a_row_stop_downloading_and_review_with_what_there_is():
    err = Garage61Error(g61.MSG_SERVER.format(code=502), status=502)
    api = _ReviewApi(n=10, fail={"L3": err, "L4": err, "L5": err, "L6": err})
    page, lines, exit_msg = _review(api)
    assert exit_msg is None and page and page.exists(), (exit_msg, lines)
    # L0-L2 downloaded, L3-L5 failed (3 in a row), L6-L9 never asked for
    assert api.calls == ["L0", "L1", "L2", "L3", "L4", "L5"], api.calls
    assert any(app.TEL_GIVING_UP in l for l in lines), lines
    prog = [l for l in lines if "skipped (Garage 61 errors)" in l]
    assert prog and "3 lap(s) with telemetry" in prog[0] and "7 skipped (Garage 61 errors)" in prog[0], prog
    data = json.loads((page.parent / "review.json").read_text(encoding="utf-8"))
    assert len([l for l in data["laps"] if l.get("t")]) >= 3
    assert app.TEL_FAIL_RUN == sync.TEL_FAIL_RUN == 3


def test_a_success_resets_the_run_counter():
    err = Garage61Error(g61.MSG_SERVER.format(code=500), status=500)
    api = _ReviewApi(n=8, fail={"L1": err, "L2": err, "L4": err, "L5": err})
    page, lines, exit_msg = _review(api)
    assert exit_msg is None and page, (exit_msg, lines)
    assert api.calls == [f"L{i}" for i in range(8)], api.calls     # never 3 in a row: every lap was asked for
    assert not any(app.TEL_GIVING_UP in l for l in lines)
    assert any("4 skipped (Garage 61 errors)" in l for l in lines), lines


def test_three_failures_with_nothing_downloaded_is_a_plain_error_with_a_next_step():
    err = Garage61Error(g61.MSG_BUSY, status=429)
    api = _ReviewApi(n=6, fail={f"L{i}": err for i in range(6)})
    page, lines, exit_msg = _review(api)
    assert page is None and exit_msg == app.TEL_NONE_MSG, (exit_msg, lines)
    assert api.calls == ["L0", "L1", "L2"], api.calls
    assert "Traceback" not in exit_msg and "Garage61Error" not in exit_msg
    assert "try the review again" in exit_msg and "Check connection" in exit_msg


def test_token_rejected_or_offline_still_stops_the_review_at_once():
    for err in (Garage61Error(g61.MSG_TOKEN, status=401), Garage61Error(g61.MSG_OFFLINE)):
        api = _ReviewApi(n=6, fail={"L0": err})
        real_make, real_say = app.make_api, app.say
        app.make_api = lambda c, api=api: api
        app.say = QUIET
        try:
            try:
                app.cmd_review(_cfg(), "EV1", open_after=False, session_kind=3, track_id=468, car_id=190)
                raise AssertionError("no error")
            except Garage61Error as e:
                assert str(e) == str(err)
        finally:
            app.make_api, app.say = real_make, real_say
        assert api.calls == ["L0"], api.calls


def test_laps_with_no_telemetry_are_still_counted_separately():
    api = _ReviewApi(n=6, none={"L1", "L4"})
    page, lines, exit_msg = _review(api)
    assert exit_msg is None and page, (exit_msg, lines)
    prog = [l for l in lines if "with none available" in l]
    assert prog and "4 lap(s) with telemetry, 2 with none available." in prog[0], lines
    assert not any("skipped (Garage 61 errors)" in l for l in lines)


# ================================================================ 3. broken speed channel (M5)
def test_all_zero_speed_channel_is_a_plain_error():
    api = _ReviewApi(n=6, csv={f"L{i}": _csv(zero_speed=True) for i in range(6)})
    page, lines, exit_msg = _review(api)
    assert page is None, lines
    assert exit_msg == app.BROKEN_SPEED_MSG, exit_msg
    assert "IndexError" not in exit_msg and "index 0 is out of bounds" not in exit_msg
    assert any("IndexError" in l for l in lines), lines     # the detail went to the log


def test_value_error_from_the_engine_is_still_a_plain_error():
    from debrief import engine
    real = engine.analyze

    def boom(*a, **k):
        raise ValueError("not enough full laps in this race.")
    engine.analyze = boom
    try:
        page, lines, exit_msg = _review(_ReviewApi(n=6))
    finally:
        engine.analyze = real
    assert page is None and exit_msg == "Couldn't build the review: not enough full laps in this race.", exit_msg


# ================================================================ 4. library backup before a re-render (M7)
def _lib_with_reviews(n=3, layout="2.6.0"):
    lib = _newdir() / "lib"
    for i in range(n):
        d = lib / "reviews" / f"2026-10-{10 + i:02d}_roval_gt4_{i}"
        d.mkdir(parents=True)
        (d / "review.json").write_text(json.dumps({
            "meta": {"track": "Charlotte Motor Speedway (Roval 2019)", "car": "Ford Mustang GT4",
                     "started_at": f"2026-10-{10 + i:02d}T20:00:00+00:00", "session": "Race"},
            "best": {"n": 2, "t": 90.0 + i}, "median": 91.0 + i,
            "laps": [{"n": 1, "t": 91.0 + i, "clean": True}, {"n": 2, "t": 90.0 + i, "clean": True}],
            "skills": {"old": True, "i": i}, "session_review": {"old": True}, "insights": {"old": True},
            "strategy": {"old": True}, "racecraft": {"old": True}, "i": i}), encoding="utf-8")
        (d / "index.html").write_text("<html>old</html>", encoding="utf-8")
    if layout is not None:
        (lib / ".layout").write_text(layout, encoding="utf-8")
    return lib


def test_refresh_all_backs_up_every_review_json_first():
    lib = _lib_with_reviews(3, layout="2.5.0")
    before = {p.parent.name: p.read_text(encoding="utf-8") for p in (lib / "reviews").glob("*/review.json")}
    real = report.LAYOUT
    report.LAYOUT = "9.9.9-test"
    try:
        n = report.refresh_all(lib, log=QUIET)
    finally:
        report.LAYOUT = real
    assert n == 3
    bk = lib / report.BACKUP_DIR / "2.5.0"
    assert bk.is_dir()
    for name, text in before.items():
        assert (bk / name / "review.json").read_text(encoding="utf-8") == text, name
    # the live copies were re-rendered (and changed), the backup wasn't
    for p in (lib / "reviews").glob("*/review.json"):
        assert (p.parent / "index.html").read_text(encoding="utf-8") != "<html>old</html>"
    assert report.list_backups(lib) == ["2.5.0"]
    # already in the new layout: nothing more is backed up or redone
    assert report.refresh_all(lib, log=QUIET) == 0 or (lib / ".layout").read_text() == real


def test_refresh_all_with_no_marker_backs_up_as_unknown():
    lib = _lib_with_reviews(2, layout=None)
    real = report.LAYOUT
    report.LAYOUT = "9.9.9-test"
    try:
        assert report.refresh_all(lib, log=QUIET) == 2
    finally:
        report.LAYOUT = real
    assert (lib / report.BACKUP_DIR / "unknown").is_dir()
    assert len(list((lib / report.BACKUP_DIR / "unknown").glob("*/review.json"))) == 2


def test_only_the_two_newest_backup_sets_are_kept():
    lib = _lib_with_reviews(1)
    for i, v in enumerate(("2.3.0", "2.4.0", "2.5.0", "2.6.0")):
        report.backup_reviews(lib, v, log=QUIET)
        d = lib / report.BACKUP_DIR / v
        t = 1_700_000_000 + i * 1000
        os.utime(d, (t, t))
    assert report.backup_reviews(lib, "2.6.1", log=QUIET) == 1
    kept = sorted(p.name for p in (lib / report.BACKUP_DIR).iterdir())
    assert kept == ["2.6.0", "2.6.1"], kept
    assert report.list_backups(lib) == ["2.6.1", "2.6.0"]


def test_backup_set_is_completed_not_redone():
    lib = _lib_with_reviews(2)
    assert report.backup_reviews(lib, "2.6.0", log=QUIET) == 2
    (lib / report.BACKUP_DIR / "2.6.0" / "2026-10-10_roval_gt4_0" / "review.json").write_text("{\"kept\":1}", encoding="utf-8")
    d = lib / "reviews" / "2026-10-12_roval_gt4_new"
    d.mkdir()
    (d / "review.json").write_text("{\"laps\": []}", encoding="utf-8")
    assert report.backup_reviews(lib, "2.6.0", log=QUIET) == 1    # only the new one
    assert json.loads((lib / report.BACKUP_DIR / "2.6.0" / "2026-10-10_roval_gt4_0" / "review.json").read_text()) == {"kept": 1}


def test_version_names_are_made_safe_for_folders():
    assert report._safe_version("2.6.0") == "2.6.0"
    assert report._safe_version("../x") == "x"
    assert report._safe_version("") == "unknown"
    assert report._safe_version("a b/c\\d:e") == "abcde"


def test_restore_backup_puts_review_json_back_and_rerenders():
    lib = _lib_with_reviews(2, layout="2.5.0")
    orig = {p.parent.name: p.read_text(encoding="utf-8") for p in (lib / "reviews").glob("*/review.json")}
    real = report.LAYOUT
    report.LAYOUT = "9.9.9-test"
    try:
        report.refresh_all(lib, log=QUIET)
    finally:
        report.LAYOUT = real
    for p in (lib / "reviews").glob("*/review.json"):
        assert p.read_text(encoding="utf-8") != orig[p.parent.name]
        (p.parent / "index.html").write_text("<html>broken</html>", encoding="utf-8")
    n = report.restore_backup(lib, "2.5.0", log=QUIET)
    assert n == 2
    for p in (lib / "reviews").glob("*/review.json"):
        assert p.read_text(encoding="utf-8") == orig[p.parent.name]
        html = (p.parent / "index.html").read_text(encoding="utf-8")
        assert html != "<html>broken</html>" and "/*DATA*/null" not in html
    assert not [x for x in (lib / "reviews").rglob(".*.tmp")]
    try:
        report.restore_backup(lib, "nope")
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
    assert (lib / "index.html").exists()   # the library page was rebuilt


def test_extras_keeps_saved_analysis_when_a_renderer_fails():
    import importlib
    data = {"meta": {}, "laps": [{"n": 1, "t": 90.0}], "skills": {"saved": True}, "session_review": {"saved": True},
            "strategy": {"saved": True}, "racecraft": {"saved": True}, "brake_spread": {"saved": True}}
    real_import = importlib.import_module

    class Boom:
        def __getattr__(self, name):
            def fn(d):
                raise RuntimeError("new code can't read this review")
            return fn

    def fake_import(name):
        if name.rsplit(".", 1)[-1] in ("skills", "session_review", "technique_plus", "strategy", "racecraft"):
            return Boom()
        return real_import(name)
    importlib.import_module = fake_import
    logs = []
    try:
        report._extras(data, log=logs.append)
    finally:
        importlib.import_module = real_import
    for k in ("skills", "session_review", "strategy", "racecraft", "brake_spread"):
        assert data[k] == {"saved": True}, (k, data.get(k))
    assert any("keeping the saved one" in l for l in logs), logs
    # a key that was absent before and fails now stays absent (nothing invented)
    data2 = {"meta": {}, "laps": [{"n": 1, "t": 90.0}]}
    importlib.import_module = fake_import
    try:
        report._extras(data2, log=QUIET)
    finally:
        importlib.import_module = real_import
    assert "skills" not in data2 and "session_review" not in data2


def test_extras_replaces_a_key_when_the_renderer_gives_a_value_and_drops_when_absent():
    import importlib
    real_import = importlib.import_module

    class Mod:
        def __init__(self, val):
            self.val = val

        def __getattr__(self, name):
            return lambda d: self.val

    def fake_import(name):
        tail = name.rsplit(".", 1)[-1]
        if tail == "skills":
            return Mod({"new": True})
        if tail in ("session_review", "technique_plus", "strategy", "racecraft"):
            return Mod(None)
        return real_import(name)
    importlib.import_module = fake_import
    try:
        d = {"meta": {}, "laps": [], "skills": {"old": True}, "session_review": {"old": True}}
        logs = []
        report._extras(d, log=logs.append)
        assert d["skills"] == {"new": True}
        assert d["session_review"] == {"old": True}      # None from a renderer never drops saved analysis
        assert any("session_review came back empty" in l for l in logs), logs
        d2 = {"meta": {}, "laps": []}
        report._extras(d2, log=QUIET)
        assert "session_review" not in d2 and "brake_spread" not in d2 and "skills" in d2
    finally:
        importlib.import_module = real_import


def test_write_never_drops_insights_from_a_saved_review_when_the_renderer_fails():
    """report.write on a review.json from an older layout keeps its derived keys when the new code fails."""
    lib = _lib_with_reviews(1)
    d = next((lib / "reviews").glob("*"))
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    from debrief import skills as skills_mod
    real = skills_mod.for_review

    def boom(d):
        raise KeyError("new_field")
    skills_mod.for_review = boom
    try:
        report.write(d, data, log=QUIET)
    finally:
        skills_mod.for_review = real
    saved = json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert saved["skills"] == {"old": True, "i": 0}, saved.get("skills")


# ================================================================ 5. one atomic write (L1)
def test_fsutil_write_json_and_write_text_are_atomic_and_fsync_when_asked():
    synced = []
    real = os.fsync
    os.fsync = lambda fd: synced.append(fd) or real(fd)
    try:
        p = TMP / "atomic" / "sub" / "x.json"
        fsutil.write_json(p, {"a": 1, "b": [1.5]}, fsync=True, mkdir=True)
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": [1.5]}
        assert p.read_text(encoding="utf-8") == '{"a":1,"b":[1.5]}'      # compact, as the sync store writes
        assert synced
        synced.clear()
        fsutil.write_text(p.with_name("y.txt"), "hello", fsync=False)
        assert p.with_name("y.txt").read_text(encoding="utf-8") == "hello" and not synced
        assert not [x for x in p.parent.iterdir() if x.name.startswith(".")], list(p.parent.iterdir())
    finally:
        os.fsync = real
    try:
        fsutil.write_json(TMP / "atomic" / "nan.json", {"x": float("nan")}, allow_nan=False)
        raise AssertionError("allow_nan=False not honoured")
    except ValueError:
        pass


def test_g61_sync_and_report_all_write_through_fsutil():
    """The three writers go through fsutil.atomic_write_text (one implementation), each flushed to disk."""
    seen = []
    real = fsutil.atomic_write_text

    def spy(path, text, fsync=False, tries=fsutil.RETRIES, mkdir=False):
        seen.append((Path(path).name, fsync))
        return real(path, text, fsync=fsync, tries=tries, mkdir=mkdir)
    fsutil.atomic_write_text = spy
    try:
        g61._atomic_write(TMP / "g.json", "{}")
        sync._write_json(TMP / "store" / "s.json", {"k": 1})
        report.atomic_write(TMP / "r.html", "<html></html>")
    finally:
        fsutil.atomic_write_text = real
    assert seen == [("g.json", True), ("s.json", True), ("r.html", True)], seen
    assert json.loads((TMP / "store" / "s.json").read_text()) == {"k": 1}
    for mod, name in ((g61, "tempfile"), (sync, "tempfile")):
        assert not hasattr(mod, name) or not callable(getattr(getattr(mod, name), "mkstemp", None)), \
            f"{mod.__name__} still has its own temp-file writer"
    src = (ROOT / "debrief" / "g61.py").read_text(encoding="utf-8") + (ROOT / "debrief" / "sync.py").read_text(encoding="utf-8")
    assert "mkstemp" not in src


def test_g61_cache_write_survives_a_briefly_locked_file():
    fake = _Fake()
    srv, base = _serve(fake)
    real = os.replace
    fails = {}

    def flaky(a, b):
        fails[str(b)] = fails.get(str(b), 0) + 1
        if fails[str(b)] <= 2:
            e = PermissionError(13, "in use")
            e.winerror = 32
            raise e
        return real(a, b)
    os.replace = flaky
    try:
        c = _client(base)
        assert c.me()["slug"] == "me"
        assert c.lap_csv("L1") == fake.csv_text
        assert not [x for x in c.cache.iterdir() if x.name.startswith(".")], list(c.cache.iterdir())
        assert all(v == 3 for v in fails.values()), fails
    finally:
        os.replace = real
        srv.shutdown()


# ================================================================ 6. the runner's offline environment
def test_no_network_blocks_real_hosts_but_not_loopback():
    old = os.environ.get("DEBRIEF_NO_NETWORK")
    os.environ["DEBRIEF_NO_NETWORK"] = "1"
    try:
        assert g61.no_network("https://garage61.net/api/v1/me")
        assert not g61.no_network("http://127.0.0.1:5/api/v1/me")
        assert not g61.no_network("http://localhost:5/api/v1/me")
        logs = []
        c = _client("https://garage61.invalid/api/v1/", logs=logs)
        calls = []

        class S:
            def get(self, url, timeout=30):
                calls.append(url)
                raise AssertionError("a real request was attempted")
        c.s = S()
        try:
            c.me()
            raise AssertionError("no error")
        except Garage61Error as e:
            assert str(e) == g61.MSG_OFFLINE and e.status is None
        assert not calls and any("DEBRIEF_NO_NETWORK" in l for l in logs), logs
        # a fake server on 127.0.0.1 still answers
        fake = _Fake()
        srv, base = _serve(fake)
        try:
            assert _client(base).me()["slug"] == "me"
        finally:
            srv.shutdown()
        os.environ["DEBRIEF_NO_NETWORK"] = "0"
        assert not g61.no_network("https://garage61.net/api/v1/me")
    finally:
        if old is None:
            os.environ.pop("DEBRIEF_NO_NETWORK", None)
        else:
            os.environ["DEBRIEF_NO_NETWORK"] = old


def test_ci_run_sets_the_offline_environment_and_keeps_update_json_out_of_the_repo():
    import ci_run
    env = ci_run.offline_env("/tmp/x")
    assert env["DEBRIEF_NO_NETWORK"] == "1" and env["DEBRIEF_UPDATE_STATE"].startswith("/tmp/x")
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == ci_run.DEAD_PROXY and env["NO_PROXY"] == "127.0.0.1,localhost"
    # this very process was started by ci_run (or run_all): the updater's state file is in the temp folder
    if os.environ.get("DEBRIEF_NO_NETWORK") == "1":
        st = Path(os.environ["DEBRIEF_UPDATE_STATE"])
        assert ROOT not in st.parents and st.name == "update.json", st
        # the updater would write there, never next to the code
        from debrief import updater
        assert updater._state_path() == st
    # a one-line child under ci_run.run_file sees it too, and a real request fails fast
    code = ("import os, sys, time; from debrief import updater; p = updater._state_path(); "
            "assert os.environ['DEBRIEF_NO_NETWORK'] == '1'; assert 'update.json' in str(p) and os.environ['TEMP'] in str(p), p; "
            "t = time.monotonic(); "
            "import requests\n"
            "try:\n    requests.get('https://api.github.com/repos/x/y/releases/latest', timeout=15)\n"
            "    raise SystemExit('a real request went out')\n"
            "except requests.RequestException:\n    pass\n"
            "assert time.monotonic() - t < 5\nprint('child ok')")
    script = TMP / "child.py"
    script.write_text(f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + code, encoding="utf-8")
    rc, out = ci_run.run_file(str(script))
    assert rc == 0 and "child ok" in out, out
    assert not (ROOT / "update.json").exists()


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
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
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
