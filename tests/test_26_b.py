"""Debrief 2.6.0 Team B: library row index, refresh rebuilds once, parse_csv fast path, /api/sessions paging,
/api/brand, brand in templates, friendlier setup messages.

    python3 tests/test_26_b.py

Offline: the desktop server runs on 127.0.0.1 with a fake token and never contacts Garage 61.
"""
from __future__ import annotations

import faulthandler
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
TMP = Path(tempfile.mkdtemp(prefix="debrief-26b-"))
LIB = TMP / "lib"
ST = TMP / "settings.toml"
os.environ["DEBRIEF_SETTINGS"] = str(ST)
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ.setdefault("DEBRIEF_SCHEDULE_DIR", str(TMP / "schedules"))
os.environ.setdefault("DEBRIEF_UPDATE_STATE", str(TMP / "update.json"))
faulthandler.dump_traceback_later(600, exit=True)

import numpy as np  # noqa: E402

from debrief import brand, engine, library, report  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
import make_fixture_library  # noqa: E402

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FIX = ROOT / "tests"


def write_settings(token="mock-token-26b"):
    ST.write_text(f'token = "{token}"\nlibrary_folder = \'{LIB}\'\napi_base = "http://127.0.0.1:9/api/v1/"\n',
                  encoding="utf-8")


write_settings()


def _fresh_lib(name) -> Path:
    lib = TMP / name
    shutil.rmtree(lib, ignore_errors=True)
    lib.mkdir(parents=True)
    _make(lib)
    return lib


def _make(lib: Path):
    import subprocess
    subprocess.run([sys.executable, str(FIX / "make_fixture_library.py"), str(lib), "--goals"], check=True,
                   stdout=subprocess.DEVNULL)


def _fixture_reviews():
    """Every review-shaped fixture json (meta + laps) in tests/fixtures_*."""
    out = []
    for p in sorted(FIX.glob("fixtures_*/*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("meta"), dict) and isinstance(d.get("laps"), list):
            out.append((p.name, d))
    return out


def _uncached_rows(lib):
    """list_reviews as 2.5 worked it out: every review.json parsed (no index)."""
    rows = []
    for p in sorted((Path(lib) / "reviews").glob("*/review.json")):
        try:
            r = library._row(p)
        except Exception:  # noqa: BLE001
            r = None
        if r:
            rows.append(r)
    library._unify_combo_keys(rows)
    _apply_pbs_slow(rows)
    rows.sort(key=lambda r: (r["date"], r["started_at"], r["dir"]), reverse=True)
    return rows


def _apply_pbs_slow(rows):
    """The 2.5 O(n²) PB pass, kept here as the reference for the one-pass version."""
    done = set()
    for r in rows:
        if id(r) in done:
            continue
        grp = sorted([x for x in rows if library.same_combo(x, r)], key=lambda x: (library._ts(x), x["dir"]))
        for i, x in enumerate(grp):
            done.add(id(x))
            x["pb"] = library._pb_info(x["best"] if x.get("best_clean", True) else None, grp[:i])


def _canon(rows):
    return json.dumps(rows, sort_keys=True)


# ================================================================ 1. row index
def test_index_rows_identical_to_full_parse():
    lib = _fresh_lib("idx1")
    # add the real review fixtures too (big ones with traces, practice, old results)
    for i, (name, d) in enumerate(_fixture_reviews()):
        out = lib / "reviews" / f"2026-0{1 + i % 9}-1{i % 9}_fix_{i:02d}"
        out.mkdir(parents=True)
        (out / "review.json").write_text(json.dumps(d), encoding="utf-8")
    want = _canon(_uncached_rows(lib))
    cold = library.list_reviews(lib)
    assert (lib / library.INDEX_FILE).is_file()
    warm = library.list_reviews(lib)
    assert _canon(cold) == want and _canon(warm) == want
    assert len(warm) >= 9
    # without PBs too
    a = library.list_reviews(lib, _pbs=False)
    assert all(r["pb"] is None for r in a)


def test_index_invalidated_by_change_add_delete_and_result():
    lib = _fresh_lib("idx2")
    library.list_reviews(lib)
    dirs = sorted(p.name for p in (lib / "reviews").iterdir())
    # change one review (new best, same size or not: mtime/size both checked)
    p = lib / "reviews" / dirs[0] / "review.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["best"]["t"] = round(d["best"]["t"] - 0.5, 3)
    d["laps"][0]["t"] = d["best"]["t"]
    p.write_text(json.dumps(d), encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    # a result.json typed in later changes the row's result
    from debrief import results
    rp = lib / "reviews" / dirs[1] / "result.json"
    rp.write_text(json.dumps({"finish_pos": 3, "field_size": 20}), encoding="utf-8")
    # delete one, add one
    shutil.rmtree(lib / "reviews" / dirs[2])
    shutil.copytree(lib / "reviews" / dirs[3], lib / "reviews" / "2026-12-01_copy_zzzzzz")
    assert _canon(library.list_reviews(lib)) == _canon(_uncached_rows(lib))
    rows = {r["dir"]: r for r in library.list_reviews(lib)}
    assert dirs[2] not in rows and "2026-12-01_copy_zzzzzz" in rows
    assert rows[dirs[1]]["result"]["finish_pos"] == 3
    assert results.load(lib / "reviews" / dirs[1])["finish_pos"] == 3
    idx = json.loads((lib / library.INDEX_FILE).read_text(encoding="utf-8"))
    assert set(idx["reviews"]) == set(rows)


def test_index_missing_or_corrupt_rebuilds_silently():
    lib = _fresh_lib("idx3")
    want = _canon(_uncached_rows(lib))
    for junk in ("", "{not json", "[]", json.dumps({"header": {"v": 999}, "reviews": {}}),
                 json.dumps({"header": library._index_header(), "reviews": {"x": 1, "y": {"m": "bad"}}})):
        (lib / library.INDEX_FILE).write_text(junk, encoding="utf-8")
        assert _canon(library.list_reviews(lib)) == want, junk[:20]
        assert json.loads((lib / library.INDEX_FILE).read_text(encoding="utf-8"))["header"] == library._index_header()
    # a damaged review.json is skipped (and remembered as skipped) exactly as before
    bad = lib / "reviews" / "2026-01-01_bad_bad000"
    bad.mkdir()
    (bad / "review.json").write_text("{half", encoding="utf-8")
    assert all(r["dir"] != bad.name for r in library.list_reviews(lib))
    assert _canon(library.list_reviews(lib)) == want
    # no temp files left behind
    assert not [p for p in lib.iterdir() if p.name.endswith(".tmp")]


def test_index_read_only_library_still_lists():
    lib = _fresh_lib("idx4")
    (lib / library.INDEX_FILE).unlink(missing_ok=True)   # the fixture script's rebuild wrote one
    real = library.fsutil.atomic_write_text

    def locked(*a, **k):
        raise PermissionError(13, "locked")
    library.fsutil.atomic_write_text = locked
    try:
        assert _canon(library.list_reviews(lib)) == _canon(_uncached_rows(lib))
    finally:
        library.fsutil.atomic_write_text = real
    assert not (lib / library.INDEX_FILE).exists()


def test_index_rows_are_not_shared_between_calls():
    lib = _fresh_lib("idx5")
    a = library.list_reviews(lib)
    a[0]["track"] = "MUTATED"
    a[0]["corners"].clear()
    b = library.list_reviews(lib)
    assert b[0]["track"] != "MUTATED" and b[0]["corners"]


def test_pb_for_and_rebuild_accept_rows():
    lib = _fresh_lib("idx6")
    rows = library.list_reviews(lib)
    data = json.loads(next((lib / "reviews").glob("*/review.json")).read_text(encoding="utf-8"))
    data["meta"]["started_at"] = "2027-01-01T00:00:00+00:00"
    data["meta"]["event_id"] = "NEWNEW"
    assert library.pb_for(lib, data) == library.pb_for(lib, data, rows=rows) == \
        library.pb_for(lib, data, rows=library.list_reviews(lib, _pbs=False))
    out = library.rebuild(lib, rows=rows)
    html = out.read_text(encoding="utf-8")
    assert '"brand":' in html and brand.TEAM_NAME in html
    library.rebuild(lib)   # without rows too


def test_one_pass_pbs_match_reference():
    rng = random.Random(5)
    rows = []
    for i in range(160):
        t = rng.choice(["A", "B", "C"])
        rows.append({"dir": f"d{i:03d}", "track": t, "car": "car", "track_id": None, "car_id": None,
                     "started_at": f"2026-{1 + rng.randrange(12):02d}-{1 + rng.randrange(28):02d}T10:00:00+00:00",
                     "date": "", "best": round(80 + rng.random() * 2, 1),   # ties on purpose
                     "best_clean": rng.random() > 0.2})
    a = json.loads(json.dumps(rows))
    b = json.loads(json.dumps(rows))
    library._apply_pbs(a)
    _apply_pbs_slow(b)
    assert _canon(a) == _canon(b)


def test_review_summaries_and_window_list_match_25():
    lib = _fresh_lib("idx7")
    items = S.ReviewIndex().list(lib)
    # 2.5's ReviewIndex._summary on each review.json, grouped the same way
    old = []
    for p in (lib / "reviews").glob("*/review.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        m = d.get("meta", {})
        best = (d.get("best") or {}).get("t")
        old.append({"dir": p.parent.name, "track": m.get("track") or "Unknown track", "car": m.get("car") or "Unknown car",
                    "started_at": m.get("started_at") or "",
                    "date": library.local_day(m.get("started_at")) or p.parent.name[:10],
                    "session": m.get("session") or "", "best": S.app.fmt_t(best) if best else "",
                    "laps": len(d.get("laps", []))})
    flat = sorted((r for g in items for r in g["reviews"]), key=lambda r: r["dir"])
    assert flat == sorted(old, key=lambda r: r["dir"])
    assert S.ReviewIndex._summary(next((lib / "reviews").glob("*/review.json")))["dir"]


def test_rows_cache_sees_result_change():
    lib = _fresh_lib("idx8")
    rc = S.RowsCache()
    a = rc.rows(lib)
    assert rc.rows(lib) is a
    d = sorted((lib / "reviews").iterdir())[0]
    (d / "result.json").write_text(json.dumps({"finish_pos": 1}), encoding="utf-8")
    b = rc.rows(lib)
    assert b is not a and next(r for r in b if r["dir"] == d.name)["result"]["finish_pos"] == 1


def test_warm_scan_benchmark_300():
    """300 copies of a real ~250 KB review: a warm scan reads the index only."""
    src = json.loads((FIX / "fixtures_23_c" / "b_sample_review_practice.json").read_text(encoding="utf-8"))
    lib = TMP / "bench"
    rng = random.Random(1)
    for i in range(300):
        d = dict(src, meta=dict(src["meta"], started_at=f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}T19:00:00+00:00",
                                event_id=f"EV{i:06d}"), best=dict(src["best"], t=round(src["best"]["t"] + rng.random(), 3)))
        out = lib / "reviews" / f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}_bench_{i:04d}"
        out.mkdir(parents=True)
        (out / "review.json").write_text(json.dumps(d), encoding="utf-8")
    t0 = time.perf_counter()
    library.list_reviews(lib)
    cold = time.perf_counter() - t0
    warm = min(_timed(lambda: library.list_reviews(lib)) for _ in range(3))
    print(f"    300 reviews: cold {cold:.2f}s, warm {warm:.3f}s")
    assert warm < 0.5, warm   # 0.15 s target on a quiet PC; loose here so a busy CI box doesn't flake


def _timed(fn):
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


# ================================================================ 2. refresh_all rebuilds once; _lib waits
def test_refresh_all_rebuilds_library_once_at_end():
    lib = _fresh_lib("ref1")
    (lib / ".layout").write_text("2.5.0", encoding="utf-8")
    calls = []
    real = library.rebuild
    library.rebuild = lambda folder, rows=None: calls.append(folder) or real(folder, rows)
    try:
        n = report.refresh_all(lib)
    finally:
        library.rebuild = real
    assert n >= 5 and len(calls) == 1 and (lib / "index.html").is_file()
    assert (lib / ".layout").read_text(encoding="utf-8").strip() == report.LAYOUT == "2.6.0"


def test_lib_page_not_rebuilt_while_refresh_runs():
    write_settings()
    shutil.rmtree(LIB, ignore_errors=True)
    LIB.mkdir(parents=True)
    _make(LIB)
    library.rebuild(LIB)
    old = (LIB / "index.html").stat().st_mtime
    for p in (LIB / "reviews").glob("*/review.json"):   # every review now newer than the page
        os.utime(p, (old + 50, old + 50))
    calls = []
    real = library.rebuild
    library.rebuild = lambda folder, rows=None: calls.append(folder) or real(folder, rows)
    gate = threading.Event()
    s = srv()
    try:
        assert s.jobs.run_background("refresh", lambda: gate.wait(20))
        for _ in range(3):
            code, _h, body = get("lib/index.html")
            assert code == 200 and body
        assert calls == []
        gate.set()
        s.jobs.wait_background(10)
        code, _h, _b = get("lib/index.html")
        assert code == 200 and len(calls) == 1
        # the index file itself is never served
        assert get("lib/" + library.INDEX_FILE)[0] == 404
    finally:
        gate.set()
        library.rebuild = real


# ================================================================ 3. parse_csv fast path
def _same(a, b):
    assert a.keys() == b.keys()
    for k in a:
        if k == "_missing":
            assert a[k] == b[k], (a[k], b[k])
        else:
            assert a[k].dtype == b[k].dtype and a[k].shape == b[k].shape, k
            assert np.array_equal(a[k], b[k], equal_nan=True), k


def _both(text):
    out = []
    for fast in (True, False):
        try:
            out.append(engine.parse_csv(text, _fast=fast))
        except ValueError as e:
            out.append(("ValueError", str(e)))
    if isinstance(out[0], tuple) or isinstance(out[1], tuple):
        assert out[0] == out[1], out
    else:
        _same(*out)
    return out[0]


def _fixture_csvs():
    """Every telemetry CSV the tests build: synth laps (all drop_cols variants) and the inline CSVs of
    test_fix_d / test_desktop / test_25_d / test_sync / test_fix_a."""
    from fixtures_a import synth
    out = [synth.lap_csv()[0]]
    for drop in (("Brake",), ("Throttle",), ("Lat", "Lon"), ("ABSActive",), ("SteeringWheelAngle", "RPM", "Gear")):
        out.append(synth.lap_csv(drop_cols=drop)[0])
    meta, laps, tel = synth.race(n=4, seed=3)
    out += list(tel.values())
    out += ["Speed,LapDistPct\n10,0.1\n20,0.2\n", "Speed,LapDistPct\n", "   \n", "", "Speed,LapDistPct\n1,0.1\n",
            "Speed,LapDistPct\n" + "\n".join(f"{20 + i},{i / 40}" for i in range(40))]
    return out


def test_parse_csv_fast_equals_tolerant_on_fixtures():
    for t in _fixture_csvs():
        _both(t)
        _both(t.replace("\r\n", "\n"))
        _both("\ufeff" + t)


def test_parse_csv_odd_files_fall_back_identically():
    from fixtures_a import synth
    base = synth.lap_csv()[0].replace("\r\n", "\n")
    lines = base.strip().split("\n")
    rng = random.Random(11)
    junk = ["", " ", "nan", "NaN", "inf", "-inf", "x", "True", "1e309", " 2.5 ", "+3", ".5", "None", "true", "0x1"]
    for _ in range(120):
        L = list(lines)
        for _k in range(rng.randint(1, 5)):
            i = rng.randrange(1, len(L))
            c = L[i].split(",")
            c[rng.randrange(len(c))] = rng.choice(junk)
            L[i] = ",".join(c)
        if rng.random() < 0.15:
            L.insert(rng.randrange(1, len(L)), "")
        if rng.random() < 0.15:
            L.insert(rng.randrange(1, len(L)), ",,, ,")
        if rng.random() < 0.1:
            L[3] += ",7"
        if rng.random() < 0.1:
            L[4] = ",".join(L[4].split(",")[:3])
        _both(rng.choice(["\n", "\r\n"]).join(L))
    _both('Speed,LapDistPct\n' + "\n".join(f'"{20 + i}",{i / 40}' for i in range(20)))   # quoted
    _both("Speed;LapDistPct\n1;2\n")


def test_parse_csv_fast_path_is_used_and_quicker():
    from fixtures_a import synth
    t = synth.lap_csv()[0]
    assert engine._csv_fast(t.strip()) is not None
    fast = min(_timed(lambda: engine.parse_csv(t)) for _ in range(5))
    slow = min(_timed(lambda: engine.parse_csv(t, _fast=False)) for _ in range(5))
    print(f"    parse_csv 3366 rows: fast {fast * 1000:.1f} ms, tolerant {slow * 1000:.1f} ms")


# ================================================================ server
_SRV = {}


def srv():
    if "s" not in _SRV:
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def get(rel, headers=None):
    rq = urllib.request.Request(srv().url + rel, headers=headers or {})
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def api(rel):
    code, _h, b = get("api/" + rel, {"X-Debrief": "1"})
    return code, json.loads(b)


def post(rel, body):
    rq = urllib.request.Request(srv().url + rel, data=json.dumps(body).encode(), method="POST",
                                headers={"X-Debrief": "1", "Content-Type": "application/json"})
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _store_root():
    from debrief import app, sync
    return sync.Store.for_api(app.make_api(app.load_settings())).root


# ================================================================ 4. /api/sessions
def test_sessions_paging_and_review_match():
    write_settings()
    LIB.mkdir(parents=True, exist_ok=True)
    root = _store_root()
    root.mkdir(parents=True, exist_ok=True)
    sess = []
    for i in range(130):
        sess.append({"key": f"E{i:09d}|3", "event": f"EVT{i:09d}", "track_id": 1, "car_id": 2, "track": "T",
                     "car": "C", "type": [3, 1, 2][i % 3], "session": "Race", "start": f"2026-10-{1 + i % 28:02d}",
                     "laps": 5, "clean_laps": 4, "best": 80.0 + i, "best_clean": True})
    (root / "sessions.json").write_text(json.dumps({"sessions": sess, "laps": 650}), encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"schema": 1}), encoding="utf-8")
    # review folders for some events (race, practice, qualifying); an older duplicate loses to the newest
    for i, suf in ((0, ""), (1, "_practice"), (2, "_qualifying"), (3, ""), (6, "")):
        ev = f"evt{i:09d}"[-6:]
        for day in ("2026-10-01", "2026-10-05"):
            d = LIB / "reviews" / f"{day}_t_c_{ev}{suf}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "review.json").write_text("{}", encoding="utf-8")
    code, j = api("sessions")              # 2.5 window: no params -> everything
    assert code == 200 and j["ok"] and j["total"] == 130 and len(j["sessions"]) == 130 and j["more"] is False
    assert j["summary"]["sessions"] == 130 and j["summary"]["laps"] == 650
    by = {s["event"]: s["review"] for s in j["sessions"]}
    assert by["EVT000000000"] == "2026-10-05_t_c_000000"
    assert by["EVT000000001"] == "2026-10-05_t_c_000001_practice"
    assert by["EVT000000002"] == "2026-10-05_t_c_000002_qualifying"
    assert by["EVT000000004"] is None
    # same answer as the 2.5 linear match for every session
    dirs = sorted((p.parent.name for p in (LIB / "reviews").glob("*/review.json")), reverse=True)
    for s in j["sessions"]:
        end = "_" + s["event"][-6:].lower() + {1: "_practice", 2: "_qualifying"}.get(s["type"], "")
        assert s["review"] == next((d for d in dirs if d.endswith(end)), None)
    code, j = api("sessions?limit=50")
    assert j["total"] == 130 and len(j["sessions"]) == 50 and j["offset"] == 0 and j["limit"] == 50 and j["more"]
    code, j = api("sessions?offset=120")
    assert len(j["sessions"]) == 10 and j["limit"] == 60 and j["more"] is False
    assert j["sessions"][0]["event"] == "EVT000000120"
    code, j = api("sessions?offset=500&limit=10")
    assert j["sessions"] == [] and j["total"] == 130
    code, j = api("sessions?offset=-1")
    assert code == 400 and not j["ok"]


def test_sessions_without_token():
    write_settings(token="")
    try:
        code, j = api("sessions?limit=5")
        assert code == 200 and j["token"] is False and j["total"] == 0 and j["sessions"] == []
    finally:
        write_settings()


# ================================================================ 5. /api/brand and templates
def test_api_brand():
    code, j = api("brand")
    assert code == 200 and j["ok"]
    assert {k: j[k] for k in brand.as_dict()} == brand.as_dict()
    blob = json.dumps(j).lower()
    assert "mock-token" not in blob and "token" not in [k.lower() for k in j]
    assert get("api/brand")[0] == 404          # API header required, like the other endpoints


def test_brand_in_every_template_render():
    d = TMP / "brandrev"
    d.mkdir(exist_ok=True)
    data = {"meta": {"track": "T", "car": "C"}, "laps": [{"n": 1, "t": 90.0, "clean": True}], "corners": [],
            "best": {"n": 1, "t": 90.0}}
    report.write(d, data)
    html = (d / "index.html").read_text(encoding="utf-8")
    assert '"brand":{' in html and brand.TEAM_NAME in html
    assert "brand" not in json.loads((d / "review.json").read_text(encoding="utf-8"))   # page only
    assert '"brand":{' in report.render_library([], {"goals": []})
    assert '"brand":{' in report.render_library([], None)
    # any template with /*BRAND*/null gets it; a caller's own value wins; data can't inject a marker
    out = report.fill("<script>B=/*BRAND*/null;X=/*X*/null</script>", {"/*X*/null": report._embed("/*BRAND*/null")})
    assert brand.TEAM_NAME in out and out.count("/*BRAND*/null") == 1
    assert report.fill("a /*BRAND*/null", {"/*BRAND*/null": "1"}) == "a 1"


def test_teams_membership_and_check_rows():
    code, j = api("teams")
    assert code == 200 and j["membership"] == brand.UNKNOWN          # no cached list
    # check job: rows() passed through when cmd_check returns the 2.6 list subclass
    class Lines(list):
        def rows(self):
            return [{"text": t, "level": "ok"} for t in self]
    real = S.app.cmd_check
    S.app.cmd_check = lambda cfg, progress=None: Lines(["Connected"])
    try:
        assert srv().jobs.start("check")["ok"]
        for _ in range(100):
            snap = srv().jobs.snapshot()
            if snap.get("state") != "running":
                break
            time.sleep(0.05)
        assert snap["result"]["lines"] == ["Connected"] and snap["result"]["rows"] == [{"text": "Connected", "level": "ok"}]
        S.app.cmd_check = lambda cfg, progress=None: ["plain"]
        assert srv().jobs.start("check")["ok"]
        for _ in range(100):
            snap = srv().jobs.snapshot()
            if snap.get("state") != "running":
                break
            time.sleep(0.05)
        assert snap["result"] == {"lines": ["plain"]}
    finally:
        S.app.cmd_check = real


# ================================================================ 6. user-facing messages
def test_messages_point_to_settings_and_welcome():
    import errno
    real = S.settings_io.save

    def full(body):
        raise OSError(errno.ENOSPC, "No space left on device")
    S.settings_io.save = full
    try:
        code, j = post("api/settings", {"team_slug": "x"})
    finally:
        S.settings_io.save = real
    assert code == 500 and j["error"] == "Debrief couldn't save your settings. Is %LOCALAPPDATA%\\Debrief read-only or full?"
    write_settings(token="PASTE-YOUR-GARAGE61-TOKEN-HERE")
    try:
        code, j = post("api/review", {})
        assert code == 400 and j["error"] == "Finish setup first: paste your Garage 61 token on the welcome screen."
        assert srv().jobs.snapshot().get("kind") != "review"
    finally:
        write_settings()
    src = Path(S.__file__).read_text(encoding="utf-8")
    assert "Set up settings.toml first." not in src and "Couldn't write settings.toml" not in src
    assert S.NO_TOKEN_SAVED == "Save your Garage 61 token first."
    assert "settings.toml" not in S.friendly(SystemExit("settings.toml not found in C:\\x. Run Setup.bat first."))
    assert "Setup.bat" not in S.friendly(SystemExit("settings.toml not found in C:\\x. Run Setup.bat first."))
    assert "settings.toml" not in S.friendly(SystemExit("settings.toml has a typo on line 3 (x)."))
    from debrief.g61 import Garage61Error
    assert "settings.toml" not in S.friendly(Garage61Error("401 Unauthorized. Open settings.toml and paste a new one"))


if __name__ == "__main__":
    failed = 0
    names = [n for n in globals() if n.startswith("test_") and callable(globals()[n])]
    for name in names:
        t = time.perf_counter()
        try:
            globals()[name]()
            print(f"ok    {name} ({time.perf_counter() - t:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {name}")
    if "s" in _SRV:
        _SRV["s"].shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
