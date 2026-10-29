"""Debrief 2.4.0 release review fixes (VERIFY-2.4.0.md): H1-H3, M1-M2, L1-L5.

    python3 tests/test_24_fixes.py

Windows-safe: no SIGALRM (faulthandler time guard), no /tmp (tempfile), utf-8 on every text open, symlink
tests skip where the OS refuses links. Never contacts Garage 61, iRacing or GitHub: the only HTTP is to the
app's own server on 127.0.0.1.
"""
from __future__ import annotations

import faulthandler
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
import warnings
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures_24_a"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
warnings.simplefilter("ignore")

TMP = Path(tempfile.mkdtemp(prefix="debrief-24fix-"))
LIB = TMP / "lib"
(LIB / "reviews").mkdir(parents=True)
ST = TMP / "settings.toml"
ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{LIB}\'\n', encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import requests  # noqa: E402


def _blocked(*a, **k):
    raise requests.ConnectionError("network blocked in tests")


requests.get = requests.post = requests.request = _blocked

from debrief import insights, intel, notes, prepare, results, strategy, xlsx  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

APP_JS = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
NS = ('xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


# ================================================================ H1: every lap in every window is safe
def _simulate(p, tank, pits) -> float:
    """Drive the plan: start with start_fuel_l, burn burn_l a lap, after each lap in pits add that amount
    (the car's tank caps it, as in the game). The lowest level seen (after each lap's burn)."""
    b = p["burn_l"]
    lvl, low = p["start_fuel_l"], float("inf")
    for lap in range(1, p["laps"] + 1):
        lvl -= b
        low = min(low, lvl)
        if lap in pits:
            lvl = min(tank, lvl + pits[lap])
    return low


def _random_inputs(rnd):
    return {"laps": rnd.randint(1, 400), "burn_l": round(rnd.uniform(0.05, 8), 2),
            "tank_l": round(rnd.uniform(1, 200), 1),
            "reserve_laps": rnd.choice([0, 0.5, 1, 1, 2, 5, round(rnd.uniform(0, 5), 2)])}


def test_h1_fuzz_every_stop_lap_in_every_window_is_safe():
    """Seeded fuzz (>= 5,000 plans with stops). For every window and every lap in it, with the other stops
    on their planned laps: fuel never goes below 0 before the next stop or the finish."""
    rnd = random.Random(24_0_0)
    plans = checked = 0
    while plans < 5000:
        inp = _random_inputs(rnd)
        try:
            p = strategy.plan(inp)
        except ValueError:
            continue
        if not p["windows"]:
            continue
        plans += 1
        planned = {w["at_lap"]: w["fuel_add_l"] for w in p["windows"]}
        for w in p["windows"]:
            assert 1 <= w["open"] <= w["at_lap"] <= w["close"] < p["laps"], (inp, w)
            assert w["fuel_add_l"] >= 0
            for lap in range(w["open"], w["close"] + 1):
                pits = dict(planned)
                del pits[w["at_lap"]]
                pits[lap] = w["fuel_add_l"]
                low = _simulate(p, inp["tank_l"], pits)
                assert low >= -1e-6, (inp, w, lap, low)
                checked += 1
        # the plan itself keeps the reserve
        assert _simulate(p, inp["tank_l"], planned) >= p["burn_l"] * inp["reserve_laps"] - 1e-6, inp
    assert checked > 5000


def test_h1_fuzz_any_mix_of_stop_laps_is_safe():
    """Stronger: nothing spills over the tank inside a window, so ANY mix of stop laps (each in its own
    window) is safe too."""
    rnd = random.Random(7)
    n = 0
    while n < 3000:
        inp = _random_inputs(rnd)
        try:
            p = strategy.plan(inp)
        except ValueError:
            continue
        if not p["windows"]:
            continue
        n += 1
        for _ in range(4):
            pits = {rnd.randint(w["open"], w["close"]): w["fuel_add_l"] for w in p["windows"]}
            if len(pits) < len(p["windows"]):   # two stops on one lap: not a real plan
                continue
            assert _simulate(p, inp["tank_l"], pits) >= -1e-6, (inp, pits)


def test_h1_review_example_window_edges():
    """The review's 40 laps / 2.5 L / 47 L / 1 spare lap: both ends of each window now stay above 0."""
    p = strategy.plan({"laps": 40, "burn_l": 2.5, "tank_l": 47, "reserve_laps": 1})
    w1, w2 = p["windows"]
    assert p["stops"] == 2 and w1["at_lap"] == 14 and w2["at_lap"] == 28
    # 2.4.0 offered "between laps 6 and 17" for stop 1: pitting after lap 6 ran dry (-17.5 L)
    assert w1["open"] > 6
    # the close of stop 2 comes from what stop 1 actually put in, not from "k x laps a tank"
    assert w2["close"] <= w1["at_lap"] + p["laps_per_tank"]
    for a in (w1["open"], w1["at_lap"], w1["close"]):
        for b in (w2["open"], w2["at_lap"], w2["close"]):
            low = _simulate(p, 47, {a: w1["fuel_add_l"], b: w2["fuel_add_l"]})
            assert low >= 0, (a, b, low)


def test_h1_close_does_not_assume_a_full_tank():
    """Stop k's latest lap is where the fuel from stop k-1 runs down to the reserve."""
    rnd = random.Random(3)
    for _ in range(2000):
        inp = _random_inputs(rnd)
        try:
            p = strategy.plan(inp)
        except ValueError:
            continue
        b, res = p["burn_l"], inp["reserve_laps"]
        for k, w in enumerate(p["windows"]):
            level_after_prev = p["stints"][k]["fuel_l"]      # stint k starts after stop k (k=0: the start)
            prev = 0 if k == 0 else p["windows"][k - 1]["at_lap"]
            # at the close you still have about the reserve (0.05 L for the rounding of fuel_l)
            assert level_after_prev - (w["close"] - prev) * b >= res * b - 0.06, (inp, w)


def test_h1_ui_shows_planned_lap_window_and_add():
    """The window shows the planned lap first: "Stop 1 after lap 14 (window 11–17): add 25.5 L"."""
    assert "' after lap '+at" in APP_JS and "' (window '+win+')'" in APP_JS and "': add '+fmtL(add)" in APP_JS
    assert "'between laps '" not in APP_JS
    assert "Pit on any lap in its window and add the same amount." in APP_JS


# ================================================================ H2: thirstier laps reach the planner
def test_h2_plan_accepts_p90_and_p90_l():
    base = {"laps": 40, "burn_l": 2.41, "use_p90": True, "tank_l": 47}
    for key in ("p90", "p90_l"):
        p = strategy.plan({**base, key: 2.52})
        assert p["burn_l"] == 2.52 and p["fuel_needed_l"] == 103.4, (key, p)      # 41 x 2.52 = 103.32, rounded up
    assert strategy.plan({**base, "p90": 2.52, "use_p90": False})["burn_l"] == 2.41
    assert strategy.plan(base)["fuel_needed_l"] == 98.9


def test_h2_app_js_sends_p90():
    m = re.search(r"function fuelInputs\(\)\{.*?return inp\}", APP_JS, re.S)
    assert m and "inp.p90=pb.p90" in m.group(0) and "p90_l" not in m.group(0), m and m.group(0)


_SRV = {}


def srv():
    if "s" not in _SRV:
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def _http(rel, body=None, raw=None, headers=None):
    h = {"X-Debrief": "1", "Content-Type": "application/json"} if headers is None else headers
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    rq = urllib.request.Request(srv().url + rel, data=data, method="POST" if data is not None else "GET", headers=h)
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_h2_api_strategy_over_http_uses_thirstier_laps():
    inputs = {"laps": 40, "burn_l": 2.41, "use_p90": True, "tank_l": 47}
    code, j = _http("api/strategy", {"action": "plan", "inputs": inputs})
    assert code == 200 and j["plan"]["fuel_needed_l"] == 98.9, j
    for key in ("p90", "p90_l"):            # what the window sends now, and what 2.4.0 sent
        code, j = _http("api/strategy", {"action": "plan", "inputs": {**inputs, key: 2.52}})
        assert code == 200 and j["ok"] and j["plan"]["burn_l"] == 2.52, (key, j)
        assert j["plan"]["fuel_needed_l"] == 103.4, j      # 41 x 2.52 = 103.32 (the review's "103.3"), rounded up
    code, j = _http("api/strategy", {"action": "plan", "inputs": {**inputs, "p90": 2.52, "use_p90": False}})
    assert code == 200 and j["plan"]["burn_l"] == 2.41
    code, j = _http("api/strategy", {"action": "plan", "inputs": {**inputs, "p90": "lots"}})
    assert code == 400 and j["error"].endswith("."), j


# ================================================================ H3 / M1: xlsx
def _book(sheets, shared=None, rels=None):
    """sheets: [(name, part path, xml bytes or None)]; parts with None are not written (shared parts)."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", f"<workbook {NS}><sheets>" + "".join(
            f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i}"/>' for i, (n, _, _) in enumerate(sheets)) +
            "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(
                       f'<Relationship Id="rId{i}" Target="{p.replace("xl/", "", 1)}" Type="x"/>'
                       for i, (_, p, _) in enumerate(sheets)) + "</Relationships>")
        written = set()
        for _, p, x in sheets:
            if x is not None and p not in written:
                z.writestr(p, x)
                written.add(p)
        if shared is not None:
            z.writestr("xl/sharedStrings.xml", shared)
    return b.getvalue()


def _grid(rows, cols, cell='<c t="s"><v>0</v></c>'):
    return (f"<worksheet {NS}><sheetData>" + "".join(f'<row r="{r}">' + cell * cols + "</row>"
                                                     for r in range(1, rows + 1)) + "</sheetData></worksheet>").encode()


def _measure(fn):
    tracemalloc.start()
    t = time.perf_counter()
    try:
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001
            out = e
        return out, time.perf_counter() - t, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_h3_review_repro_is_refused_fast_and_small():
    """One 2,401-character shared string, a 2,000 x 40 sheet of it, and 20 sheets all pointing at that
    part (11.9 KB): 2.4.0 took 55 s and 3.3 GB. Now: a plain XlsxError, well under 2 s and 100 MB."""
    sst = f'<sst {NS}><si><t xml:space="preserve"> {"Z" * 2400}</t></si></sst>'
    data = _book([(f"S{i}", "xl/worksheets/sheet1.xml", _grid(2000, 40) if i == 0 else None) for i in range(20)], sst)
    assert len(data) < 20_000
    out, secs, peak = _measure(lambda: intel.parse_upload(data, "x.xlsx"))
    assert isinstance(out, xlsx.XlsxError) and "more in it than Debrief can read" in str(out), out
    assert secs < 2.0 and peak < 100e6, (secs, peak)


def test_h3_two_sheets_cant_point_at_one_part():
    rows = (f"<worksheet {NS}><sheetData><row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>Track</t></is></c>"
            f"<c r=\"B1\" t=\"inlineStr\"><is><t>Notes</t></is></c></row></sheetData></worksheet>").encode()
    data = _book([("One", "xl/worksheets/sheet1.xml", rows), ("Two", "xl/worksheets/sheet1.xml", None),
                  ("Three", "xl/worksheets/SHEET1.xml", None)])
    book = xlsx.read(data)
    assert [s["name"] for s in book["sheets"]] == ["One"], book


def test_h3_cell_budget_across_sheets():
    """Distinct parts, short text: 3 sheets of 2,000 x 40 (240k cells) is over the 200k-cell budget."""
    sst = f'<sst {NS}><si><t>x</t></si></sst>'
    parts = [(f"S{i}", f"xl/worksheets/sheet{i}.xml", _grid(2000, 40)) for i in range(3)]
    out, secs, peak = _measure(lambda: xlsx.read(_book(parts, sst)))
    assert isinstance(out, xlsx.XlsxError) and str(out).endswith("."), out
    assert peak < 100e6, peak
    # two of them (160k cells) are fine
    book = xlsx.read(_book(parts[:2], sst))
    assert len(book["sheets"]) == 2 and len(book["sheets"][0]["rows"]) == 2000
    # and the budget counts the gaps that become "" (a cell at AN2000 makes a 2,000 x 40 grid)
    one = (f"<worksheet {NS}><sheetData><row r=\"1\"><c r=\"A1\" t=\"s\"><v>0</v></c></row>"
           f"<row r=\"2000\"><c r=\"AN2000\" t=\"s\"><v>0</v></c></row></sheetData></worksheet>").encode()
    parts = [(f"S{i}", f"xl/worksheets/sheet{i}.xml", one) for i in range(3)]
    out = _measure(lambda: xlsx.read(_book(parts, sst)))[0]
    assert isinstance(out, xlsx.XlsxError), out


def test_h3_text_budget_and_string_cap():
    long = "Y" * 9000
    sst = f'<sst {NS}><si><t>{long}</t></si></sst>'
    one = (f"<worksheet {NS}><sheetData><row r=\"1\"><c r=\"A1\" t=\"s\"><v>0</v></c>"
           f"<c r=\"B1\" t=\"inlineStr\"><is><t>{long}</t></is></c></row></sheetData></worksheet>").encode()
    row = xlsx.read(_book([("S", "xl/worksheets/sheet1.xml", one)], sst))["sheets"][0]["rows"][0]
    assert len(row[0]) == xlsx.MAX_STR and len(row[1]) == xlsx.MAX_STR
    # 2,000 x 40 cells of a 200-character string = 16 M characters: over the text budget
    sst = f'<sst {NS}><si><t>{"Q" * 200}</t></si></sst>'
    out = _measure(lambda: xlsx.read(_book([("S", "xl/worksheets/sheet1.xml", _grid(2000, 40))], sst)))[0]
    assert isinstance(out, xlsx.XlsxError) and "more in it" in str(out), out


def test_h3_server_parses_one_upload_at_a_time():
    up = S.IntelUploads()
    assert isinstance(up.parse_lock, type(threading.Lock()))
    assert "with self.server.intel.parse_lock:" in (ROOT / "debrief" / "desktop" / "server.py").read_text(encoding="utf-8")


def _sheet_xml(prolog: str, body_text="A1") -> str:
    return (f'{prolog}<worksheet {NS}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{body_text}</t></is></c>'
            f'<c r="B1" t="inlineStr"><is><t>b</t></is></c></row></sheetData></worksheet>')


def test_m1_doctype_entity_refused_in_any_encoding():
    ent = '<!DOCTYPE x [<!ENTITY a "ENTITY-EXPANDED">]>'
    cases = {
        "utf-16 with BOM": _sheet_xml('<?xml version="1.0" encoding="UTF-16"?>' + ent, "&a;").encode("utf-16"),
        "utf-16-le no BOM": _sheet_xml('<?xml version="1.0" encoding="UTF-16"?>' + ent, "&a;").encode("utf-16-le"),
        "utf-16-be no BOM": _sheet_xml('<?xml version="1.0" encoding="UTF-16"?>' + ent, "&a;").encode("utf-16-be"),
        "utf-32": _sheet_xml('<?xml version="1.0" encoding="UTF-32"?>' + ent, "&a;").encode("utf-32"),
        "utf-8 lower case": _sheet_xml('<?xml version="1.0"?><!doctype x [<!entity a "E">]>', "&a;").encode(),
        "latin-1 declared": _sheet_xml('<?xml version="1.0" encoding="ISO-8859-1"?>').encode("latin-1"),
        "ebcdic declared": _sheet_xml('<?xml version="1.0" encoding="cp037"?>' + ent, "&a;").encode("cp037"),
        "utf-16 no doctype": _sheet_xml('<?xml version="1.0" encoding="UTF-16"?>').encode("utf-16"),
    }
    for label, raw in cases.items():
        data = _book([("S", "xl/worksheets/sheet1.xml", raw)])
        try:
            r = xlsx.read(data)
        except xlsx.XlsxError as e:
            assert str(e).endswith("."), (label, e)
            continue
        raise AssertionError(f"{label}: read {r}")


def test_m1_expat_handlers_refuse_a_declaration():
    """The parser-side guard on its own (whatever the byte check thinks): a DOCTYPE or ENTITY raises."""
    from xml.parsers import expat
    for raw in (b'<!DOCTYPE x><x/>', b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
                b'<!DOCTYPE x [<!ENTITY e SYSTEM "http://127.0.0.1:9/x">]><x>&e;</x>'):
        p = expat.ParserCreate()
        p.StartDoctypeDeclHandler = xlsx._refuse_dtd
        p.EntityDeclHandler = xlsx._refuse_dtd
        try:
            p.Parse(raw, True)
        except xlsx.XlsxError:
            continue
        raise AssertionError(raw)


def test_m1_plain_utf8_still_reads():
    for prolog in ("", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "﻿<?xml version='1.0'?>"):
        r = xlsx.read(_book([("S", "xl/worksheets/sheet1.xml", _sheet_xml(prolog, "Café Roval").encode("utf-8"))]))
        assert r["sheets"][0]["rows"] == [["Café Roval", "b"]], (prolog, r)


# ================================================================ M2: publish never follows a link
def _try_symlink(src, dst, is_dir=True) -> bool:
    try:
        os.symlink(src, dst, target_is_directory=is_dir)
        return True
    except (OSError, NotImplementedError, AttributeError):
        print("SKIP  symlinks are refused on this OS/account")
        return False


def _share_cfg():
    base = Path(tempfile.mkdtemp(dir=TMP, prefix="m2-"))
    team, victim = base / "team", base / "victim"
    team.mkdir()
    victim.mkdir()
    return base, team, victim, {"team_cards_folder": str(team), "share_my_cards": True}


ME = {"slug": "swazi-williams", "name": "Swazi Williams"}
CARD = {"track": "Spa", "fields": {"lap1": "Tidy into La Source"}, "car_notes": {}}
EMPTY = {"track": "Spa", "fields": {}, "car_notes": {}}


def _raises_oserror(fn):
    try:
        fn()
    except OSError as e:
        assert "link" in str(e), e
        return
    raise AssertionError("no OSError")


def test_m2_symlinked_track_folder_refused_on_write_and_delete():
    base, team, victim, cfg = _share_cfg()
    (team / notes.SHARED_DIR).mkdir()
    if not _try_symlink(victim, team / notes.SHARED_DIR / notes.track_key("Spa")):
        return
    _raises_oserror(lambda: notes.publish(cfg, CARD, ME))
    assert os.listdir(victim) == []
    (victim / "swazi-williams.json").write_text("{}", encoding="utf-8")
    _raises_oserror(lambda: notes.publish(cfg, EMPTY, ME))
    assert os.listdir(victim) == ["swazi-williams.json"]          # never deleted through the link


def test_m2_symlinked_root_and_file_refused():
    base, team, victim, cfg = _share_cfg()
    if not _try_symlink(victim, team / notes.SHARED_DIR):
        return
    _raises_oserror(lambda: notes.publish(cfg, CARD, ME))
    assert list(victim.rglob("*")) == []
    # the file itself a link to a file outside the root
    base, team, victim, cfg = _share_cfg()
    d = team / notes.SHARED_DIR / notes.track_key("Spa")
    d.mkdir(parents=True)
    outside = victim / "keep.json"
    outside.write_text('{"keep": 1}', encoding="utf-8")
    if not _try_symlink(outside, d / "swazi-williams.json", is_dir=False):
        return
    _raises_oserror(lambda: notes.publish(cfg, CARD, ME))
    _raises_oserror(lambda: notes.publish(cfg, EMPTY, ME))
    assert outside.read_text(encoding="utf-8") == '{"keep": 1}'
    assert (d / "swazi-williams.json").is_symlink()


def test_m2_resolved_path_outside_root_refused():
    """Whatever kind of link (a symlink, or a junction on Windows), the real path must be under the root."""
    base, team, victim, cfg = _share_cfg()
    root = team / notes.SHARED_DIR
    d = root / notes.track_key("Spa")
    p = d / "swazi-williams.json"
    real = os.path.realpath
    try:
        os.path.realpath = lambda x, *a, **k: str(victim / "x") if str(x) == str(d) else real(x, *a, **k)
        _raises_oserror(lambda: notes.publish(cfg, CARD, ME))
    finally:
        os.path.realpath = real
    assert not p.exists()


def test_m2_normal_publish_and_delete_still_work():
    base, team, victim, cfg = _share_cfg()
    assert notes.publish(cfg, CARD, ME) is True
    p = team / notes.SHARED_DIR / notes.track_key("Spa") / "swazi-williams.json"
    assert json.loads(p.read_text(encoding="utf-8"))["fields"]["lap1"] == "Tidy into La Source"
    assert notes.publish(cfg, EMPTY, ME) is True and not p.exists()
    assert notes.publish(cfg, EMPTY, ME) is True                    # nothing there: still fine


# ================================================================ L1: one set of result rules
def test_l1_server_uses_results_validate():
    lenient = {"finish_pos": "5", "start_pos": 9, "field_size": 20, "finish_status": " DNF ",
               "incident_laps": [{"lap": 3, "kind": " Spin ", "points": None}, {"lap": 1, "kind": "off", "points": ""}],
               "pit_stops": "1", "sr_before": "2,41", "notes": " ok "}
    want = results.validate(lenient)
    for k in S.RESULT_DERIVED:
        want.pop(k, None)
    assert S.clean_result_fields(lenient) == want
    assert want["finish_status"] == "dnf" and want["incident_laps"][1] == {"lap": 3, "kind": "spin", "points": 0}
    bad = [{}, {"finish_pos": 0}, {"finish_pos": 1.5}, {"finish_pos": 30, "field_size": 22}, {"finish_pos": True},
           {"finish_pos": 3, "notes": "x" * 501}, {"finish_pos": "abc"}, {"finish_pos": 3, "sr_after": 5.5},
           {"finish_pos": 3, "finish_status": "crashed"}, {"finish_pos": 3, "pos_after_lap1": 201},
           {"finish_pos": 3, "incident_laps": [{"lap": 3, "kind": "bump"}]},
           {"finish_pos": 3, "incident_laps": [{"lap": 3, "kind": "off", "points": 3}]},
           {"finish_pos": 3, "incident_laps": [{"lap": 1, "kind": "off"}] * 31}]
    for b in bad:
        for fn in (results.validate, S.clean_result_fields):
            try:
                fn(b)
            except ValueError:
                continue
            raise AssertionError(f"{fn.__name__} accepted {b}")
    # the local fallback (results module not loadable) follows the same lenient rules for the 2.4 fields
    out = S._clean_result_fields_local(lenient)
    assert out["finish_status"] == "dnf" and out["incident_laps"][1]["points"] == 0


# ================================================================ L2: tiny CSVs refused, short real CSVs accepted
def test_l2_csv_minimum():
    hdr = {"X-Debrief": "1", "Content-Type": "application/octet-stream"}
    code, j = _http("api/intel-upload?name=intel.csv", raw=b"a,b\n", headers=hdr)
    assert code == 400 and "too small" in j["error"], j
    small = "Track,Notes\nRoval,T1 brake early\nSpa,Eau Rouge flat\n".encode("utf-8")
    code, j = _http("api/intel-upload?name=intel.csv", raw=small, headers=hdr)
    assert code == 200 and j["ok"] and j["sheets"][0]["header"] == ["Track", "Notes"], j


# ================================================================ L3: preview matching stays linear
def test_l3_resolver_matches_resolve():
    known = ["Charlotte Motor Speedway (Roval 2019)", "Spa-Francorchamps - Grand Prix Pits", "Road America - Full Course",
             "Watkins Glen International - Boot", "Watkins Glen International - Classic Boot", "Okayama - Full Course",
             "Lime Rock Park - Grand Prix", "Sebring International Raceway - International", "", None, 5]
    typed = ["Charlotte Roval", "charlotte motor speedway roval 2019", "Spa", "Spa-Francorchamps", "Road America",
             "Watkins Glen Boot", "Watkins Glen Classic Boot", "Okayama", "Okayama Short", "Lime Rock", "Sebring",
             "Made Up Ring", "Made-Up Ring", "made up ring", "Nürburgring", "Nurburgring", "Spa"]
    rnd = random.Random(5)
    for _ in range(30):
        rows = [rnd.choice(typed) for _ in range(40)]
        r, seen = intel._Resolver([k for k in known if isinstance(k, str) and k.strip()]), []
        for t in rows:
            want = intel._resolve(t, known, seen)
            assert r(t) == want, (t, want)
            if want[0] not in seen:
                seen.append(want[0])
            r.add_seen(want[0])


def test_l3_preview_2000_distinct_rows_is_quick():
    known = [f"Venue {i} Raceway - Layout {i % 7}" for i in range(450)]
    def word(i):   # letters only: track_key drops digits, so "Track 5" and "Track 6" would be one card
        return "".join(chr(97 + (i // 26 ** k) % 26) for k in range(3))
    rows = [[f"Made up {word(i)}", f"Hotspot {i}"] for i in range(2000)]
    parsed = {"sheets": [{"name": "S", "header": ["Track", "Crash spots"], "rows": rows, "suggest": {}}]}
    cfg = {"library_folder": str(Path(tempfile.mkdtemp(dir=TMP)))}
    t = time.perf_counter()
    out = intel.preview(parsed, 0, {"0": "track", "1": "hotspots"}, "fill_empty", cfg, known)
    secs = time.perf_counter() - t
    assert len(out["tracks"]) == 2000 and secs < 30, secs        # 2.4.0: over 120 s


# ================================================================ L4: the week box doesn't run a full prefill
def test_l4_week_box_uses_race_length_not_prefill():
    from debrief import schedule
    calls = []
    real = strategy.prefill
    strategy.prefill = lambda *a, **k: calls.append(a) or real(*a, **k)
    try:
        cfg = {"library_folder": str(LIB), "cache_dir": str(TMP / "cache")}
        ser = {"id": "s", "name": "S", "cars": ["Ford Mustang GT4"],
               "weeks": [{"week": 1, "start": "2026-11-17", "track": "Charlotte Motor Speedway (Roval 2019)",
                          "length": "40 mins"}]}
        w = prepare._week_view(ser, 1, [], str(LIB), None, schedule.Matcher(), cfg=cfg, sched={})
    finally:
        strategy.prefill = real
    assert calls == [] and w["strategy"] == strategy.race_length(ser["weeks"][0])
    assert w["strategy"]["minutes"] == 40


# ================================================================ L5: lap 1 stays correlational
def test_l5_lap1_insight_wording():
    import samples
    d = json.loads(json.dumps(samples.engine_review(), default=float))
    for data in (d, {k: v for k, v in d.items() if k != "racecraft"}):
        it = next(i for i in insights.build(data)["bad"] if i["key"] == "lap1")
        assert re.fullmatch(r"Lap 1 was \d+\.\d s slower than your typical lap \(that includes the start\)\.", it["text"]), it
        for word in ("standing", "rolling", "caution", "moment", "because"):
            assert word not in it["text"].lower(), it["text"]
    assert "Some of that is the standing" not in (ROOT / "debrief" / "insights.py").read_text(encoding="utf-8")


# ================================================================ CI
def test_ci_runs_this_file():
    yml = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert "python tests/test_24_fixes.py" in yml
    assert yml.index("tests/test_24_d.py") < yml.index("tests/test_24_fixes.py") < yml.index("Build with PyInstaller")


if __name__ == "__main__":
    faulthandler.dump_traceback_later(600, exit=True)      # Windows-safe time guard (no SIGALRM)
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        t = time.perf_counter()
        try:
            fn()
            print(f"ok    {name} ({time.perf_counter() - t:.1f}s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {name}: {e}")
    faulthandler.cancel_dump_traceback_later()
    if "s" in _SRV:
        _SRV["s"].shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
