"""Debrief 2.16 window: the Racing page ("Your starts" and "Your incidents") through the real server (port 0, the
session secret and the X-Debrief header).

Fake files (the builders in tests/test_216_start.py): four races at Circuit Zolder over two weeks. Three have a replay
scan and a saved review (one with the official result), all four have telemetry with iRacing's incident count and
positions; plus a practice file with an off track. GET api/racing works both reports out once and caches them.

    python3 tests/test_216_page.py

No network, temp folders from tempfile, utf-8 everywhere. Browser checks use Playwright + Chromium and are skipped
when either is missing; DEBRIEF_SHOTS=<folder> also saves racing-light.png and racing-dark.png at scale 2.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)       # Windows CI has pyirsdk: nothing here may talk to iRacing
TMP = Path(tempfile.mkdtemp(prefix="debrief-216p-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import fake_ibt  # noqa: E402
import test_216_start as fx  # noqa: E402
from debrief import app  # noqa: E402
from debrief.desktop import carsetup as CS, racing as RC, server as S  # noqa: E402

LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
EMPTY = TMP / "empty"
ENV, PW = {}, {}
TRACK = "Circuit Zolder (Grand Prix)"
GREEN = fx.GREEN
DAY = 86400


def settings(lib=LIB, iracing=IRACING, racecap=None):
    lib.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{lib}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{iracing}'\n"
        + (f"racecap_library = '{racecap}'\n" if racecap else ""), encoding="utf-8")


def _pos_at(raw, t, car=fx.ME):
    for row in raw["samples"]:
        if row[0] >= t:
            return row[5][car]
    return raw["samples"][-1][5][car]


# (day, pace, scanned, review (official start, after lap 1, field), incidents [(seconds after the green, points, off)])
RACES = [
    (0, fx.PACE, True, None, [("pass", 4, False), (300.0, 1, True)]),
    (3, {1: [(GREEN + 2, GREEN + 12, 0.3)], 2: [(GREEN + 2, GREEN + 12, 0.3)]}, True, None, [(40.0, 2, False)]),
    (7, fx.PACE, True, {"start_pos": 4, "pos_after_lap1": 5, "field_size": 10, "finish_pos": 6}, [("pass", 4, False)]),
    (10, None, False, None, [(170.0, 1, True)]),
]


def make_files():
    if "files" in ENV:
        return ENV["files"]
    TEL.mkdir(parents=True, exist_ok=True)
    fx.TRACK = TRACK
    out = {"reviews": [], "ibt": []}
    for i, (day, pace, scanned, result, incs) in enumerate(RACES):
        fx.SUB = 91000101 + i
        prog, times = fx.progress(pace=pace)
        raw = fx.raw_samples(prog, times)
        scan = fx.scan_of(raw)
        end1 = float(fx.me_car(scan)["line_crossings"]["1"])
        start, lap1 = _pos_at(raw, GREEN + 0.6), _pos_at(raw, end1 + 2.5)
        when = 1_792_000_000 + day * DAY
        ev = f"EV-{i}"
        if scanned:
            fx.save_scan(LIB, raw, scan, event_id=ev)
            d = fx.save_review(LIB, f"2026-10-{20 + day:02d}_circuit-zolder_mclaren-570s-gt4_{i}",
                               time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(when + 60)), result=result, event_id=ev,
                               track=TRACK)
            (d / "index.html").write_text("<!doctype html><meta charset=utf-8><title>Review</title><p>Review", encoding="utf-8")
            out["reviews"].append(d.name)
        steps, offs = [], []
        for t, pts, off in incs:
            if t == "pass":
                t = next(tt for k, tt in enumerate(times) if tt > GREEN and prog[4][k] >= prog[fx.ME][k])
            else:
                t = GREEN + t
            steps.append((t, pts))
            if off:
                offs.append((t - 1.0, t))
        p = TEL / f"mclaren570sgt4_zolder gp 2026-10-{20 + day:02d} 19-00-00.ibt"
        fx.make_ibt(p, fx.prog_fn(prog, times), times[-1], incidents=steps, offs=offs, subsession=fx.SUB,
                    positions=[(GREEN + 0.5, start, start), (end1 + 2, lap1, lap1)], start_date=when, track=TRACK)
        out["ibt"].append(p)
    pr = TEL / "mclaren570sgt4_zolder gp 2026-10-31 18-00-00.ibt"
    fake_ibt.make(pr, [99.0, 98.5, 98.7, 98.9], incidents=(1,), track=TRACK, subsession=91000099,
                  start_date=1_792_000_000 + 11 * DAY)
    out["ibt"].append(pr)
    ENV["files"] = out
    return out


def srv():
    if "srv" not in ENV:
        settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, header=True):
    s = srv()
    h = {"X-Debrief": "1"} if header else {}
    rq = urllib.request.Request(s.url + rel, headers=h)
    try:
        with ENV["opener"].open(rq, timeout=180) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def ready():
    srv()
    make_files()
    settings()


def no_long_dash(obj, where):
    t = json.dumps(obj, ensure_ascii=False)
    assert "—" not in t and "–" not in t, f"long dash in {where}"


def no_paths(obj, where):
    t = json.dumps(obj, ensure_ascii=False)
    for p in (str(TMP), str(LIB), str(TEL), "/tmp/", "\\\\"):
        assert p not in t, f"{p} in {where}"


# ================================================================ the endpoint
def test_racing_endpoint():
    ready()
    code, j = call("api/racing")
    assert code == 200 and j["ok"], j
    assert j["counts"] == {"scans": 3, "reviews": 3, "telemetry": 5}, j["counts"]
    assert j["found"] == {"scans": 3, "telemetry": 5, "reviews": 3}, j["found"]
    assert j["where"]["library"] and j["where"]["telemetry"] and not j["where"]["racecap"], j["where"]
    assert j["where"]["telemetry_name"] == "telemetry"
    no_paths(j, "api/racing")
    no_long_dash(j, "api/racing")
    St, M = j["starts"], j["incidents"]
    assert St["races"] == 4 and [t["track"] for t in St["tracks"]] == [TRACK], (St["races"], St["tracks"])
    rows = St["list"]
    assert [r["start_from"] for r in rows] == ["telemetry", "telemetry", "official", "telemetry"], [r["start_from"] for r in rows]
    assert rows[2]["start_words"] == "from the official results" and rows[0]["lap1_words"].startswith("iRacing's position after lap 1")
    assert (rows[0]["start"], rows[0]["lap1_pos"], rows[0]["change"]) == (4, 5, -1), rows[0]
    assert rows[1]["change"] == 2 and rows[1]["contact"] == "no_4x", rows[1]
    assert rows[0]["contact"] == "contact_4x" and rows[0]["incident_points"] == 4, rows[0]
    assert j["pending"] is None, "every telemetry file read"
    assert [bool(r["review"]) for r in rows] == [True, True, True, False], [r["review"] for r in rows]
    assert set(r["review"] for r in rows if r["review"]) == set(make_files()["reviews"])
    assert rows[0]["corners"] and not rows[3]["corners"], "corner by corner only for scanned races"
    c1 = next(s for s in rows[0]["corners"] if s["key"] == "C1")
    assert c1["label"] == "the start to Turn 1" and c1["lost"] == 2, c1
    g = St["tracks"][0]
    assert any("only says what happened" in s or "habit rather than chance" in s for s in g["sentences"]), g["sentences"]
    assert not any("looks like a habit" in s for s in g["sentences"]), "four races: no habit claimed"
    # incidents
    assert M["sessions"] == 5 and M["events"] == 6, (M["sessions"], M["events"])
    t = M["tracks"][0]
    assert t["by_session"] == {"race": 5, "qualifying": 0, "practice": 1}, t["by_session"]
    assert t["points"] == 14 and t["points_known"] and t["corner_names"] == "corners", t
    kinds = {(r["kind_of_session"], r["points"], r["type"]) for r in t["list"]}
    assert ("race", 4, "contact") in kinds and ("race", 1, "own") in kinds, kinds
    hit = next(r for r in t["list"] if r["points"] == 4 and r["review"])
    assert hit["where"] == "Turn 1" and hit["closest"].startswith("Driver E, ") and hit["lap1"], hit
    assert "1x, 2x, 4x" in M["steps_note"], M["steps_note"]
    assert not t["patterns"], t["patterns"]


def test_cache_reuse_and_invalidation():
    ready()
    s = srv()
    st = CS.state(s)
    with st.lock:
        st.mem.clear()
    shutil.rmtree(TMP / "cache" / RC.CACHE, ignore_errors=True)
    made = st.made
    code, j = call("api/racing")
    assert code == 200 and not j["cached"] and st.made == made + 1
    code, j2 = call("api/racing")
    assert j2["cached"] and st.made == made + 1 and j2["starts"] == j["starts"], "asked again: from memory"
    with st.lock:
        st.mem.clear()
    code, j3 = call("api/racing")
    assert j3["cached"] and st.made == made + 1, "Debrief restarted: from the cache folder"
    assert list((TMP / "cache" / RC.CACHE).glob("racing-*.json")), "saved under the cache folder"
    for f in list((TMP / "cache" / RC.CACHE).glob("*.json")) + list((TMP / "cache" / RC.FILES_CACHE).glob("*.json")):
        no_paths(json.loads(f.read_text(encoding="utf-8")), f.name)
    assert list((TMP / "cache" / RC.FILES_CACHE).glob("files.json")), "each telemetry file's reading is kept"
    # a changed telemetry file
    f = make_files()["ibt"][-1]
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000_000))
    code, j4 = call("api/racing")
    assert not j4["cached"] and st.made == made + 2
    # a changed review (a result added)
    rv = LIB / "reviews" / make_files()["reviews"][0] / "result.json"
    rv.write_text(json.dumps({"start_pos": 4, "pos_after_lap1": 5, "field_size": 10}), encoding="utf-8")
    try:
        code, j5 = call("api/racing")
        assert not j5["cached"] and st.made == made + 3
        assert j5["starts"]["list"][0]["start_from"] == "official", j5["starts"]["list"][0]
    finally:
        rv.unlink()
    # a new module version
    old = RC.VERSION
    RC.VERSION = old + 100
    try:
        code, j6 = call("api/racing")
        assert not j6["cached"]
    finally:
        RC.VERSION = old
    # Look again with refresh=1: worked out again even when nothing changed
    call("api/racing")
    n = st.made
    code, j7 = call("api/racing?refresh=1")
    assert code == 200 and not j7["cached"] and st.made == n + 1
    # two requests at once: worked out once
    with st.lock:
        st.mem.clear()
    shutil.rmtree(TMP / "cache" / RC.CACHE, ignore_errors=True)
    n = st.made
    res = []
    ts = [threading.Thread(target=lambda: res.append(call("api/racing"))) for _ in range(3)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    assert all(c == 200 for c, _ in res) and st.made == n + 1, (st.made - n)


def test_cold_open_reads_no_telemetry_file_again():
    """Debrief restarted with a new answer to work out (say a new review): each telemetry file's reading comes from
    the cache folder, none is read again."""
    ready()
    s = srv()
    call("api/racing")
    st = CS.state(s)
    with st.lock:
        st.mem.clear()
    shutil.rmtree(TMP / "cache" / RC.CACHE, ignore_errors=True)
    s._racing_readings = None                       # as after a restart: only the cache folder is left
    reads, real = [], RC.read_one
    RC.read_one = lambda f: reads.append(f.name) or real(f)
    try:
        t0 = time.monotonic()
        code, j = call("api/racing")
        took = time.monotonic() - t0
    finally:
        RC.read_one = real
    assert code == 200 and j["ok"] and j["pending"] is None and reads == [], reads
    assert j["starts"]["races"] == 4 and j["incidents"]["events"] == 6
    print(f"  cold open with every telemetry reading cached: {took:.2f} s")


def test_slow_first_read_answers_early_and_finishes_in_the_background():
    """Over BUDGET_S of new files: the answer comes back early with "pending" (never cached), the rest is read on
    a background thread, and asking again gives the whole answer."""
    ready()
    s = srv()
    st = CS.state(s)
    with st.lock:
        st.mem.clear()
    shutil.rmtree(TMP / "cache" / RC.CACHE, ignore_errors=True)
    shutil.rmtree(TMP / "cache" / RC.FILES_CACHE, ignore_errors=True)
    s._racing_readings = None
    old, real = RC.BUDGET_S, RC.read_one
    RC.BUDGET_S = 0.0

    def slow(f):
        time.sleep(0.3)
        return real(f)
    RC.read_one = slow
    try:
        made = st.made
        code, j = call("api/racing")
        assert code == 200 and j["ok"] and j["pending"] and j["pending"]["left"] >= 1, j.get("pending")
        assert st.made == made and not list((TMP / "cache" / RC.CACHE).glob("racing-*.json")), "a partial answer is never cached"
        no_paths(j, "pending")
        for _ in range(100):
            code, j = call("api/racing")
            if not j["pending"]:
                break
            time.sleep(0.2)
        assert not j["pending"] and j["starts"]["races"] == 4 and j["incidents"]["events"] == 6, j.get("pending")
        assert st.made == made + 1
    finally:
        RC.BUDGET_S, RC.read_one = old, real
    # closing Debrief stops the reader between two files; it never holds the close up
    R = RC.readings(s, TMP / "cache")
    s.stopping.set()
    try:
        R.recs = {}
        RC.start_reader(s, R, make_files()["ibt"])
        t = R.thread
        if t is not None:
            t.join(5)
            assert not t.is_alive()
        assert len(R.recs) <= 1, "stopped before reading the files"
    finally:
        s.stopping.clear()
        R.recs = None


def test_empty_states():
    srv()
    EMPTY.mkdir(parents=True, exist_ok=True)
    settings(lib=EMPTY, iracing=TMP / "no such folder")
    try:
        code, j = call("api/racing")
        assert code == 200 and j["ok"], j
        assert j["counts"] == {"scans": 0, "reviews": 0, "telemetry": 0}, j["counts"]
        assert not j["where"]["telemetry"] and j["where"]["library"], j["where"]
        assert j["starts"]["races"] == 0 and j["incidents"]["sessions"] == 0
        no_paths(j, "empty")
        # a RaceCap folder that isn't there: said, never a crash, nothing written
        settings(lib=EMPTY, iracing=TMP / "no such folder", racecap=TMP / "no racecap")
        code, j = call("api/racing")
        assert code == 200 and not j["where"]["racecap"] and j["where"]["racecap_set"], j["where"]
        assert not (TMP / "no racecap").exists()
    finally:
        settings()


def test_racecap_folder_is_read_only():
    ready()
    rc = TMP / "RaceCap" / "Races"
    rc.mkdir(parents=True, exist_ok=True)
    src = next((LIB / "replay-scans" / "Races").iterdir()) if (LIB / "replay-scans").is_dir() else None
    if src is None:
        from debrief import replay_scan
        src = next(replay_scan.scans_root(LIB).iterdir())
    shutil.copytree(src, rc / src.name, dirs_exist_ok=True)
    before = sorted((p.relative_to(rc), p.stat().st_mtime_ns) for p in rc.rglob("*"))
    settings(racecap=TMP / "RaceCap")
    try:
        code, j = call("api/racing")
        assert code == 200 and j["where"]["racecap"], j["where"]
        assert j["found"]["scans"] == 3, "the same race in both folders counts once"
        after = sorted((p.relative_to(rc), p.stat().st_mtime_ns) for p in rc.rglob("*"))
        assert before == after, "nothing written in RaceCap's folder"
        no_paths(j, "with RaceCap")
    finally:
        settings()
        shutil.rmtree(TMP / "RaceCap", ignore_errors=True)


def test_bad_inputs():
    ready()
    for rel in ("api/racing?refresh=abc", "api/racing?track=..%2F..", "api/racing?refresh=1&x=1"):
        code, j = call(rel)
        assert code == 400 and not j["ok"] and j["error"] == "Bad request.", (rel, j)
    code, _ = call("api/racing", header=False)
    assert code == 404, "no X-Debrief header: not found"
    # broken files never break the page: a junk telemetry file, a junk scan and a junk review
    junk = [TEL / "junk 2026-10-01 10-00-00.ibt", LIB / "reviews" / "2026-10-01_junk" / "review.json"]
    junk[0].write_bytes(b"\0" * 4096)
    junk[1].parent.mkdir(parents=True, exist_ok=True)
    junk[1].write_text("{not json", encoding="utf-8")
    from debrief import replay_scan
    jd = replay_scan.scans_root(LIB) / "91999999_junk" / "scan"
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "91999999_junk.scan.json").write_text("[1, 2", encoding="utf-8")
    try:
        code, j = call("api/racing")
        assert code == 200 and j["ok"] and j["starts"]["races"] == 4, j.get("error")
        assert any(n.startswith("junk 2026-10-01 10-00-00.ibt:") for n in j["incidents"]["notes"]), j["incidents"]["notes"]
        no_paths(j, "with junk")
    finally:
        junk[0].unlink()
        shutil.rmtree(junk[1].parent, ignore_errors=True)
        shutil.rmtree(jd.parent, ignore_errors=True)


def test_registered_in_the_window():
    html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    nav = re.findall(r'data-nav="([a-z]+)"', html[html.index('<nav'):html.index('</nav>')]) if "<nav" in html else []
    if nav:
        assert nav.index("racing") == nav.index("practice") + 1, nav
    assert 'id="vRacing"' in html
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "'vRacing'" in js and "racing:()=>openRacing()" in js


# ================================================================ the window
def browser():
    if "b" not in PW:
        try:
            from playwright.sync_api import sync_playwright
            PW["pw"] = sync_playwright().start()
            PW["b"] = PW["pw"].chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: Playwright/Chromium not available")
            PW["b"] = None
    return PW["b"]


def page(dark=False, width=1320, height=900, scale=1):
    b = browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": width, "height": height}, color_scheme="dark" if dark else "light",
                        device_scale_factor=scale)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.route("**/api/sync", lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    pg.route("**/api/update*", lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    return ctx, pg, errs


def snap(pg, path):
    pg.evaluate("() => { const b = document.getElementById('banner'); if (b) b.hidden = true }")
    h = pg.evaluate("() => Math.max(900, ...[...document.querySelectorAll('.view')].filter(v => !v.hidden).map(v => v.scrollHeight))")
    size = pg.viewport_size
    pg.set_viewport_size({"width": size["width"], "height": min(int(h), 9000)})
    time.sleep(0.3)
    pg.screenshot(path=str(path))
    pg.set_viewport_size(size)


def shots_dir():
    d = os.environ.get("DEBRIEF_SHOTS")
    if not d:
        return None
    Path(d).mkdir(parents=True, exist_ok=True)
    return Path(d)


def open_racing(pg, s):
    pg.goto(s.url)
    pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=racing]')")
    pg.click(".navbtn[data-nav=racing]")
    pg.wait_for_selector("#vRacing", state="visible")
    pg.wait_for_selector("#rcStartSec .rctbl", timeout=60000)
    pg.wait_for_selector("#rcIncSec .rcgrid", timeout=60000)


def test_racing_in_the_window():
    ready()
    s = srv()
    shots = shots_dir()
    for dark in (False, True):
        r = page(dark=dark, scale=2 if shots else 1)
        if not r:
            return
        ctx, pg, errs = r
        try:
            open_racing(pg, s)
            assert pg.get_attribute(".navbtn[data-nav=racing]", "aria-current") == "page"
            st = pg.text_content("#rcStarts")
            assert "Your starts" in st and "P4 → P5" in st and "from the official results" in st, st[:600]
            assert "The start to Turn 1" in st and "Only cars the replay saw near you are counted (3 scanned races)" in st, st[:900]
            assert "No replay scan" in st and "Open review" in st, st[:900]
            assert not re.search(r"\b[CT]\d+\b", st), "corners read Turn N"
            rows = pg.locator("#rcStartSec .rctbl tbody tr:not(.rcsub)")
            assert rows.count() == 4, rows.count()
            assert pg.locator("#rcStartSec .rcbar").count() >= 5
            inc = pg.text_content("#rcInc")
            assert "Your incidents" in inc and "iRacing counts 1x off track, 2x loss of control, 4x contact" in inc, inc[:400]
            assert "Turn 1" in inc and "Contact" in inc and "Caught up" in inc and "Your own" in inc
            assert not re.search(r"\b[CT]\d+\b", inc), "corners read Turn N"
            assert pg.locator("#rcIncSec .rcgrid tbody tr").count() >= 5
            for sel in ("#rcStarts", "#rcInc", "#rcFound"):
                t = pg.text_content(sel)
                assert "—" not in t and "–" not in t, f"long dash in {sel}"
                assert str(TMP) not in t, f"a path in {sel}"
            pg.click("#rcIncSec details.rcincs summary")
            assert pg.locator("#rcIncSec .rclist tbody tr").count() == 6
            if shots:
                snap(pg, shots / ("racing-dark.png" if dark else "racing-light.png"))
            # a race row opens its saved review
            pg.click("#rcStartSec .rctbl .linkbtn >> nth=0")
            pg.wait_for_function("() => !document.getElementById('vFrame').hidden")
            assert not errs, errs
        finally:
            ctx.close()


def test_racing_empty_in_the_window():
    srv()
    EMPTY.mkdir(parents=True, exist_ok=True)
    settings(lib=EMPTY, iracing=TMP / "no such folder")
    try:
        r = page()
        if not r:
            return
        ctx, pg, errs = r
        try:
            pg.goto(srv().url)
            pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=racing]')")
            pg.click(".navbtn[data-nav=racing]")
            pg.wait_for_selector("#rcStarts .pvnone", timeout=60000)
            t = pg.text_content("#vRacing")
            assert "Scan this race" in t and "Alt+L" in t and "telemetry folder wasn't found" in t, t[:800]
            assert pg.is_visible("#rcStarts button[data-nav=settings]")
            assert not errs, errs
        finally:
            ctx.close()
    finally:
        settings()


if __name__ == "__main__":
    fails = 0
    try:
        for k, fn in list(globals().items()):
            if k.startswith("test_") and callable(fn):
                try:
                    fn()
                    print("ok", k)
                except (Exception, SystemExit):  # noqa: BLE001
                    fails += 1
                    print("FAIL", k)
                    traceback.print_exc()
    finally:
        if PW.get("b"):
            PW["b"].close()
            PW["pw"].stop()
        if "srv" in ENV:
            ENV["srv"].shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
        shutil.rmtree(fx.TMP, ignore_errors=True)
    print("FAILED" if fails else "ALL OK", fails)
    sys.exit(1 if fails else 0)
