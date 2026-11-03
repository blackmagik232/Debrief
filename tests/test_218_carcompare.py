"""Debrief 2.18 Car vs car (debrief/carcompare.py and GET api/carcompare).

Synthetic fields: lap time = track base + car effect + iRating effect (0.8 s a lap per 1000 iRating) + race-day
offset + driver noise. Checks:
  - no car effect: a "quicker" sentence in at most 5 % of fields (family-wise, three cars);
  - no car effect but the faster drivers prefer car A: no false claim either (the iRating adjustment and bands);
  - a real 0.4 s car effect with enough drivers is found, and the sentence names the right car;
  - too few drivers: "not enough to tell", whatever the numbers;
  - the results file's car name and lap times are read; the window keeps the table beside the review;
  - where_faster on fake telemetry: a car that's faster at one corner shows there, two runs of the same car show
    nothing bigger than his own spread, one lap each is a rough look;
  - the server endpoint (cached, no paths, no long dashes) and the page in a browser (carcompare-light.png).

    python3 tests/test_218_carcompare.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-218-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import corner_laps as cl  # noqa: E402
from debrief import app, carcompare as CC, cornercoach, results_csv  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

TRACK = "Circuit Zolder (Grand Prix)"
CARS = ("McLaren 570S GT4", "Ford Mustang GT4", "BMW M4 G82 GT4 Evo")
LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
ENV, PW = {}, {}


# ================================================================ synthetic fields
def field(seed, n_drivers=60, effects=None, confound=False, races=4, cars=CARS[:3], me=None):
    """Races at one track. effects: {car: seconds}. confound: the higher a driver's iRating, the likelier the first car
    (also inside each iRating band, so the bands alone can't fix it)."""
    rng = np.random.default_rng(seed)
    effects = effects or {}
    people = []
    for k in range(n_drivers):
        ir = float(np.clip(rng.lognormal(np.log(2000), 0.45), 600, 7000))
        if confound:
            p_a = 1 / (1 + np.exp(-(ir - 2200) / 400))       # the higher the iRating, the likelier car A
            car = cars[0] if rng.random() < p_a else cars[1 + int(rng.integers(len(cars) - 1))]
        else:
            car = cars[k % len(cars)]
        people.append({"name": f"Driver {k}", "car": car, "ir": ir, "skill": rng.normal(0, 0.5)})
    if me:
        people.append({"name": me, "car": cars[0], "ir": 1800.0, "skill": -0.6})
    out = []
    for r in range(races):
        day = rng.normal(0, 0.6)
        ds = []
        for p in people:
            if rng.random() < 0.4 and p["name"] != me:
                continue
            pace = 100.0 + effects.get(p["car"], 0.0) - 0.8 * (p["ir"] - 2000) / 1000 + p["skill"] + day + rng.normal(0, 0.25)
            ds.append({"name": p["name"], "car": p["car"], "class": CC.class_key(p["car"]), "irating": p["ir"],
                       "pace": pace, "best": pace - 1.2, "laps": 14, "finished": True})
        out.append({"source": "results", "id": f"{seed}-{r}", "track": TRACK, "when": "", "drivers": ds})
    return out


def LIMIT(n):
    """The most false claims a test held at 5 % can show in n fields by chance (5 % plus two standard errors). Over
    1500 fields each (scratch runs for 2.18), the false-claim rate was 4.6 % with no car effect and 5.1 % with the
    hard confounding below: the test is held at 5 %, no better and no worse."""
    return 0.05 + 2 * (0.05 * 0.95 / n) ** 0.5


def claims(races, n_perm=200, seed=0):
    return CC.compare(races, TRACK, "GT4", n_perm=n_perm, seed=seed)["verdicts"]


def test_no_effect_false_claims_at_most_5pct():
    n, bad = 200, 0                 # every field and every shuffle seeded: the same answer every run
    for s in range(n):
        bad += bool(claims(field(40000 + s), seed=s))
    rate = bad / n
    ENV["null_rate"] = rate
    print(f"  null (no car effect): {bad}/{n} fields with a false claim ({rate:.1%})")
    assert rate <= LIMIT(n), rate


def test_confounded_field_no_false_claim():
    n, bad, raw_a_quicker = 120, 0, 0
    for s in range(n):
        races = field(5000 + s, confound=True)
        res = CC.compare(races, TRACK, "GT4", n_perm=200, seed=s)
        bad += bool(res["verdicts"])
        med = {c["car"]: c["median"] for c in res["cars"]}
        raw_a_quicker += med[CARS[0]] < min(v for k, v in med.items() if k != CARS[0])
    rate = bad / n
    ENV["confound_rate"] = rate
    print(f"  confounded (faster drivers pick the {CARS[0]}): raw numbers favour it in {raw_a_quicker}/{n}, "
          f"false claims {bad}/{n} ({rate:.1%})")
    assert raw_a_quicker >= n * 0.8, "the confounding should fool the raw numbers"
    assert rate <= LIMIT(n), rate


def test_real_effect_found():
    found, n = 0, 20
    for s in range(n):
        res = CC.compare(field(9000 + s, n_drivers=150, effects={CARS[1]: -0.4}), TRACK, "GT4", n_perm=200, seed=s)
        v = [x for x in res["verdicts"] if x["fast"] == CARS[1]]
        wrong = [x for x in res["verdicts"] if x["slow"] == CARS[1]]
        assert not wrong, wrong
        found += bool(v)
        if s == 0:
            assert v and abs(v[0]["gap"] - 0.4) < 0.25 and "a lap quicker than the" in v[0]["text"], res["verdicts"]
            assert "drivers of similar iRating" in v[0]["text"]
            row = next(c for c in res["cars"] if c["car"] == CARS[1])
            assert row["drivers"] >= 15 and row["with_irating"] >= 15
    ENV["power"] = found / n
    print(f"  real 0.4 s effect, 50 drivers a car: found in {found}/{n}")
    assert found >= n * 0.8, found


def test_too_few_drivers():
    res = CC.compare(field(7, n_drivers=18, effects={CARS[1]: -1.5}), TRACK, "GT4", n_perm=400)
    assert not res["verdicts"], res["verdicts"]
    assert "Not enough to tell" in res["summary"] and "at least 15 drivers" in res["summary"], res["summary"]
    assert all("not enough" in c["confidence"] or c["reference"] for c in res["cars"])


def test_scans_without_irating_and_you():
    races = field(11, me="Swazi Williams")
    res = CC.compare(races, TRACK, "GT4", me=["Swazi Williams"], n_perm=200)
    you = res["you"]["items"][0]
    assert you["car"] == CARS[0] and you["percentile"] is not None and "quicker than" in you["text"], you
    for r in races:
        for d in r["drivers"]:
            d["irating"] = None
        r["source"] = "scan"
    res = CC.compare(races, TRACK, "GT4", n_perm=200)
    assert not res["verdicts"] and "iRatings aren't known" in res["summary"], res["summary"]


def test_results_file_reads_car_and_laps():
    head = ('"Fin Pos","Car ID","Car","Car Class ID","Car Class","Cust ID","Name","Start Pos","Car #","Out","Interval",'
            '"Laps Led","Average Lap Time","Fastest Lap Time","Fast Lap#","Laps Comp","Inc","Old iRating","New iRating","Session Name"\n')
    rows = ['"1","135","McLaren 570S GT4","84","GT4 Class","1","A Fast","2","7","Running","-","3","1:39.512","1:38.004","5","15","0","2400","2450","RACE"',
            '"2","204","Ford Mustang GT4","84","GT4 Class","2","B Next","1","9","Disconnected","-1L","0","1:41.002","1:39.660","3","9","4","1700","1650","RACE"']
    p = results_csv.parse(head + "\n".join(rows) + "\n")
    a, b = p["drivers"]
    assert a["model"] == "McLaren 570S GT4" and a["car"] == "7" and a["lap"] == {"avg": 99.512, "best": 98.004, "laps": 15}, a
    assert b["fields"]["finish_status"] == "disconnected"
    race = CC._race_from_field({"track": TRACK, "drivers": p["drivers"]}, "x")
    assert race["drivers"][0]["class"] == "GT4" and race["drivers"][0]["irating"] == 2400
    assert race["drivers"][1]["finished"] is False
    ENV["csv"] = head + "\n".join(rows) + "\n"


# ================================================================ where each car is faster (fake telemetry)
def _laps(path):
    laps, _info = cornercoach.load_laps([str(path)])
    return laps


def tel_files():
    if "tel" in ENV:
        return ENV["tel"]
    TEL.mkdir(parents=True, exist_ok=True)
    a = cl.make(TEL / "mclaren570sgt4_zolder gp 2026-11-01 10-00-00.ibt", 9, seed=7, session="Race")
    keep = cl.VMIN
    try:
        cl.VMIN = tuple(v + 2.5 if k == 3 else v for k, v in enumerate(keep))      # car B: faster through T4
        b = cl.make(TEL / "fordmustanggt4_zolder gp 2026-11-02 10-00-00.ibt", 9, seed=8, session="Race",
                    car="Ford Mustang GT4", car_path="fordmustanggt4", car_id=204)
    finally:
        cl.VMIN = keep
    a2 = TMP / "same" / "mclaren570sgt4_zolder gp 2026-11-03 10-00-00.ibt"
    a2.parent.mkdir(parents=True, exist_ok=True)
    cl.make(a2, 9, seed=9, session="Race")
    ENV["tel"] = (TEL / a.name if hasattr(a, "name") else TEL / "mclaren570sgt4_zolder gp 2026-11-01 10-00-00.ibt",
                  TEL / "fordmustanggt4_zolder gp 2026-11-02 10-00-00.ibt", a2)
    return ENV["tel"]


def test_where_faster_on_fake_telemetry():
    pa, pb, pa2 = tel_files()
    la, lb, la2 = _laps(pa), _laps(pb), _laps(pa2)
    assert len(la) >= 3 and len(lb) >= 3, (len(la), len(lb))
    w = CC.where_faster(la, lb, "McLaren 570S GT4", "Ford Mustang GT4")
    assert not w["rough"]
    t4 = [r for r in w["rows"] if r["segment"] == "T4"]
    assert t4 and "min_kmh" in t4[0]["found"] and t4[0]["found"]["min_kmh"]["diff"] < 0, w["rows"]
    assert "the Ford Mustang GT4 is" in t4[0]["text"] and "km/h faster at the slowest point" in t4[0]["text"], t4[0]["text"]
    others = [r for r in w["rows"] if r["kind"] == "corner" and r["segment"] not in ("T4", "T5")]
    assert len(others) <= 1, [r["text"] for r in w["rows"]]
    same = CC.where_faster(la, la2, "Run 1", "Run 2")
    assert len([r for r in same["rows"] if r["kind"] == "corner"]) <= 1, [r["text"] for r in same["rows"]]
    rough = CC.where_faster(la[:1], lb[:1], "McLaren 570S GT4", "Ford Mustang GT4")
    assert rough["rough"]
    for r in w["rows"] + rough["rows"]:
        assert "—" not in r["text"] and "–" not in r["text"]
    ENV["where"] = [r["text"] for r in w["rows"]]


# ================================================================ server and window
def settings():
    LIB.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n", encoding="utf-8")


def srv():
    if "srv" not in ENV:
        settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, header=True, data=None):
    s = srv()
    h = {"X-Debrief": "1"} if header else {}
    if data is not None:
        h["Content-Type"] = "application/octet-stream"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=300) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def library():
    """Reviews with a results table each (made like the window does: the CSV through api/result-csv)."""
    if "lib" in ENV:
        return
    tel_files()
    races = field(21, n_drivers=70, effects={CARS[1]: -0.5}, me="Swazi Williams", cars=CARS[:2])
    head = ('"Fin Pos","Car ID","Car","Car Class ID","Car Class","Cust ID","Name","Start Pos","Car #","Out","Laps Led",'
            '"Average Lap Time","Fastest Lap Time","Laps Comp","Inc","Old iRating","New iRating","Session Name"\n')
    for k, r in enumerate(races):
        d = LIB / "reviews" / f"2026-10-{20 + k:02d}_circuit-zolder-grand-prix_mclaren-570s-gt4_{k}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "review.json").write_text(json.dumps({"meta": {"driver": "Swazi Williams", "track": TRACK, "car": CARS[0],
                                                            "started_at": f"2026-10-{20 + k:02d}T19:00:00+00:00"}}), encoding="utf-8")
        lines = []
        for i, x in enumerate(sorted(r["drivers"], key=lambda x: x["pace"]), 1):
            avg = x["pace"]
            lines.append(f'"{i}","1","{x["car"]}","84","GT4 Class","{i}","{x["name"]}","{i}","{i}","Running","0",'
                         f'"1:{avg - 60:06.3f}","1:{avg - 61.2:06.3f}","14","0","{int(x["irating"])}","{int(x["irating"])}","RACE"')
        st, j = call("api/result-csv?review=" + urllib.request.quote(d.name), data=(head + "\n".join(lines) + "\n").encode())
        assert st == 200 and j["ok"] and j["matched"]["name"] == "Swazi Williams", (st, j)
        assert (d / "field.json").is_file()
    ENV["lib"] = True


def no_dash_or_path(obj, where):
    t = json.dumps(obj, ensure_ascii=False)
    assert "—" not in t and "–" not in t, f"long dash in {where}"
    for p in (str(TMP), str(LIB), str(TEL)):
        assert p not in t, f"a path in {where}"


def test_endpoint():
    library()
    st, j = call("api/carcompare", header=False)
    assert st == 404
    st, j = call("api/carcompare?nope=1")
    assert st == 400
    t0 = time.time()
    st, j = call("api/carcompare")
    assert st == 200 and j["ok"], j
    first = time.time() - t0
    assert j["selected"] == {"track": TRACK, "class": "GT4"}, j["selected"]
    c = j["compare"]
    assert len(c["cars"]) == 2 and c["verdicts"] and c["verdicts"][0]["fast"] == CARS[1], c["summary"]
    assert c["you"]["items"][0]["car"] == CARS[0]
    w = j["where"]
    assert w and {w["a"], w["b"]} == {"Mclaren 570s GT4", "Ford Mustang GT4"}, (w, j["tel_cars"])
    no_dash_or_path(j, "api/carcompare")
    st, j2 = call("api/carcompare")
    assert j2["cached"] and j2["compare"] == j["compare"]
    st, j3 = call("api/carcompare?refresh=1")
    assert st == 200 and not j3["cached"]
    ENV["api"] = {"summary": c["summary"], "first_s": round(first, 1)}


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


def test_page_in_the_window():
    library()
    b = browser()
    if not b:
        return
    shots = os.environ.get("DEBRIEF_SHOTS")
    ctx = b.new_context(viewport={"width": 1320, "height": 900}, color_scheme="light", device_scale_factor=2 if shots else 1)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    for pat in ("**/api/sync", "**/api/update*"):
        pg.route(pat, lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    try:
        pg.goto(srv().url)
        pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=cars]')")
        pg.click(".navbtn[data-nav=cars]")
        pg.wait_for_selector("#vCars", state="visible")
        pg.wait_for_selector("#ccTable table", timeout=120000)
        t = pg.text_content("#vCars")
        assert "Car vs car" in t and "a lap quicker than the" in t and "You vs the field" in t, t[:800]
        assert "Where each car is faster" in t, t[:1500]
        assert "—" not in t and "–" not in t and str(TMP) not in t
        assert pg.locator("#ccTable tbody tr").count() == 2
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            pg.evaluate("() => { const b = document.getElementById('banner'); if (b) b.hidden = true }")
            h = pg.evaluate("() => document.getElementById('vCars').scrollHeight")
            pg.set_viewport_size({"width": 1320, "height": max(900, min(int(h) + 40, 9000))})
            time.sleep(0.3)
            pg.screenshot(path=str(Path(shots) / "carcompare-light.png"))
        assert not errs, errs
    finally:
        ctx.close()


def test_empty_state_words():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "cars:()=>openCars()" in js and "'vCars'" in js
    for words in ("Add the iRacing results file", "Scan this race", "teammate"):
        assert words in js, words


if __name__ == "__main__":
    fails = 0
    only = sys.argv[1:]
    try:
        for k, fn in list(globals().items()):
            if k.startswith("test_") and callable(fn) and (not only or k in only):
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
    print({k: v for k, v in ENV.items() if k in ("null_rate", "confound_rate", "power", "where", "api")})
    print("FAILED" if fails else "ALL OK", fails)
    sys.exit(1 if fails else 0)
