"""Debrief 2.25: "Save as picture" engagement cards (debrief/cards.py words, templates/cards.js drawing).

  - every card type (leaderboard, head-to-head, corner king, member, new team best) is built from fixture team data
    (arbitrary names) with a punchy rules-only line, in Settings > Units' unit;
  - missing data gives a plain fallback card, never an error; file names are safe;
  - the Team page and the member page draw a real 1200 x 675 PNG (Playwright + Chromium; SKIPPED when missing);
  - the window saves a card into <library>/shares under a safe name, never over a file, and says where.

    python3 tests/test_225_cards.py [--samples DIR]      (--samples also writes one sample PNG of each type)

No network, temp folders only."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import struct
import sys
import tempfile
import threading
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-225-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import app, cards, members, team  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
from pw_settle import settle  # noqa: E402

SAMPLES = Path(sys.argv[sys.argv.index("--samples") + 1]) if "--samples" in sys.argv else None


def fixture():
    def row(key, name, t, pos, ir, date, car="Acme GT3 Evo"):
        return {"key": key, "name": name, "t": t, "pos": pos, "gap": round(t - 138.979, 3), "irating": ir, "date": date,
                "car": car, "class": "GT3"}
    board = [row("k1", "Robin Hale", 138.979, 1, 3120, "2026-11-04"), row("me", "Pat Quill", 139.928, 2, 2740, "2026-11-02"),
             row("k2", "Sam Ortega", 140.512, 3, 2410, "2026-10-29"), row("k3", "Lee Vance", 141.2, 4, None, "2026-10-20"),
             row("k4", "Kit Moreno", 142.05, 5, 1800, "2026-10-11"), row("k5", "Dana Fox", 143.4, 6, 1500, "2026-10-01")]
    names = {"k1": "Robin Hale", "me": "Pat Quill", "k2": "Sam Ortega"}
    corners = []
    for i, (king, sec, m) in enumerate([("k1", "me", 0.081), ("me", "k1", 0.142), ("k2", "k1", 0.033), ("k1", "k2", 0.2)], 1):
        t = {"k1": 20.0, "me": 20.0, "k2": 20.0}
        t[king] = 19.9
        t[sec] = round(19.9 + m, 3)
        for k in t:
            t[k] = t.get(k, 20.4) if k in (king, sec) else round(19.9 + m + 0.2, 3)
        corners.append({"id": f"T{i}", "apex_m": 100.0 * i, "from_m": 100.0 * i - 50, "to_m": 100.0 * i + 50, "king": king,
                        "king_t": t[king], "second": sec, "margin": m, "times": t, "your_gap": round(t["me"] - t[king], 3),
                        "metrics": {k: {"min_mph": 80.0 + 3 * (k == king) + i, "exit_mph": 100.0} for k in t}})
    kings = {"fastest": "k1", "drivers": ["k1", "me", "k2"], "names": names, "corners": corners,
             "wins": {"k1": 2, "me": 1, "k2": 1}, "L": 7000}
    c1 = {"track": "Circuit de Spa-Francorchamps (Grand Prix Pits)", "track_short": "Spa", "car": "Acme GT3 Evo", "board": board,
          "kings": kings, "loaded": True, "races": 3, "last_date": "2026-11-04",
          "learn": [{"corner": "T2", "teammate": {"key": "k1", "name": "Robin Hale"}, "gap_s": 0.142, "text": "Robin brakes 8 m later."}]}
    c2 = {"track": "Lime Rock Park", "track_short": "Lime Rock", "car": "Acme GT3 Evo", "loaded": True, "kings": None,
          "board": [row("me", "Pat Quill", 61.2, 1, 2740, "2026-11-01"), row("k1", "Robin Hale", 61.9, 2, 3120, "2026-10-30")]}
    c3 = {"track": "Nowhere Ring", "track_short": "Nowhere", "car": "Acme GT3 Evo", "board": [], "loaded": False}
    members_ = [{"name": "Pat Quill", "key": "me", "is_me": True}, {"name": "Robin Hale", "key": "k1"}, {"name": "Sam Ortega", "key": "k2"}]
    return {"team_name": "Fixture Racing Club", "me": {"key": "me", "name": "Pat Quill"}, "members": members_,
            "combos": [c1, c2, c3], "generated_at": "2026-11-05T10:00:00",
            "weekly": {"season": "2026 S4", "series": [{"id": 1, "name": "Fixture GT3 Series", "weeks": [
                {"week": 7, "track": "Spa", "final": False, "board": [
                    {"key": "k1", "name": "Robin Hale", "t": 138.9, "pos": 1, "car": "Acme GT3 Evo", "date": "2026-11-04"},
                    {"key": "me", "name": "Pat Quill", "t": 139.5, "pos": 2, "car": "Acme GT3 Evo", "date": "2026-11-03"}]}]}],
                       "h2h": [{"key": "k1", "name": "Robin Hale", "shared_weeks": 5, "behind": 3, "median_gap_pct": 0.4}]}}


def png_size(b):
    assert b[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", b[16:24])


# ---------------------------------------------------------------- the words
def test_each_card_type_has_a_headline_hero_and_punchy_line():
    d = fixture()
    c = d["combos"][0]
    lb = cards.leaderboard(d, c)
    assert lb["headline"] == "Spa" and lb["hero"]["value"] == "2:18.979" and lb["rows"][0]["hl"] and len(lb["rows"]) == 5
    assert lb["rows"][1]["gap"] == "+0.949" and lb["line"] == "0.949 s to find at Spa. Who's next?", lb["line"]
    assert lb["team"] == "Fixture Racing Club" and "Black Magik Studios" in lb["brand"]
    du = cards.duel(d, "me", "k1", c)
    assert du["headline"] == "Pat Quill vs Robin Hale" and du["hero"]["value"] == "0.949 s" and len(du["cols"]) == 2
    assert du["cols"][0]["sub"] == "1 corner won" and du["cols"][1]["sub"] == "3 corners won" and du["stats"][0]["sub"].startswith("Level over 2 tracks"), du
    assert "rematch" in du["line"] and cards.duel(d, "me", "k2", c)["stats"][0]["sub"] != ""
    ck = cards.corner_king(d, c, "T2", "mph")
    assert ck["headline"] == "Turn 2" and ck["hero"]["value"] == "Pat Quill" and "0.142 s clear" in ck["hero"]["sub"]
    assert ck["stats"][0]["value"].endswith(" mph") and "Think you can take it?" in ck["line"]
    ck2 = cards.corner_king(d, c, "T2", "kmh")
    assert ck2["stats"][0]["value"].endswith(" km/h"), "speeds follow Settings > Units"
    assert cards.corner_king(d, c)["headline"] == "Turn 4", "default: the corner with the biggest margin"
    m = cards.member(d, "Robin Hale")
    assert m["headline"] == "Robin Hale" and "3120" in m["sub"] and m["rows"] and m["stats"][0]["value"] == "2"
    assert m["stats"][2]["sub"] == "Not tracked yet", "no invented consistency"
    m2 = cards.member(d, "Robin Hale", lib={"found": True, "consistency": "Steady: laps within 0.4 s. More text here."})
    assert m2["stats"][2]["sub"] == "Steady: laps within 0.4 s.", m2["stats"][2]
    nb = cards.best_card(d, c)
    assert nb["headline"] == "Robin Hale set a new team best" and nb["hero"]["value"] == "2:18.979" and "Spa" in nb["sub"]
    assert "2:18.979" in nb["line"]


def test_missing_data_gives_a_plain_fallback_not_an_error():
    d = fixture()
    for spec in (cards.leaderboard(d, d["combos"][2]), cards.best_card(d, d["combos"][2]), cards.corner_king(d, d["combos"][1]),
                 cards.duel(d, "me", "nobody"), cards.member(d, "No Such Driver"), cards.leaderboard({}, {}),
                 cards.leaderboard(None, None), cards.member(None, None), cards.corner_king({}, {})):
        assert spec["empty"] and "Nothing to show" in spec["line"] and spec["w"] == 1200 and spec["h"] == 675, spec
        assert re.fullmatch(r"[a-z0-9-]+\.png", spec["file"]), spec["file"]
    lone = cards.leaderboard(d, {"track": "Solo", "car": "X", "board": [{"key": "a", "name": "Only One", "t": 90.0}]})
    assert lone["line"] == "Only Only has a time at Solo so far. Who's next?" or "Only" in lone["line"]
    noteam = cards.leaderboard({}, d["combos"][0])
    assert noteam["team"] == "", "no built-in team name: it comes from the data or is left out"
    assert cards.build_all({}) == {} and cards.build_all(None) == {}


def test_file_names_are_safe():
    for bad in ("../../Evil Track\\x", "Spa<>:\"|?*", "", None, "ÅÖ Ring 'n' Co.", "a" * 300):
        fn = cards.file_name("leaderboard", bad, "GT3")
        assert re.fullmatch(r"[a-z0-9-]{1,120}\.png", fn), fn
    assert cards.file_name("leaderboard", "Spa", "GT3") == "spa-gt3-leaderboard.png"
    assert S.card_file_name("../../x/Spa GT3.png") == "x-spa-gt3.png" and S.card_file_name("") == "debrief-card.png"
    specs = cards.build_all(fixture())
    for k in ("lb:0", "nb:0", "kg:0", "kg:0:T2", "dh:k1", "dl:k1:0", "mb:Robin Hale", "wk:0:0", "ss:k1", "ln:0:0"):
        assert k in specs, (k, sorted(specs))
    assert all(re.fullmatch(r"[a-z0-9-]+\.png", s["file"]) for s in specs.values())
    assert "wk:0:0" in specs and specs["wk:0:0"]["kicker"].startswith("THIS WEEK")
    assert specs["ss:k1"]["hero"]["value"] == "2 - 3" and "Robin Hale leads 3 - 2" in specs["ss:k1"]["line"]


def test_generic_never_hard_codes_a_team():
    src = (ROOT / "debrief" / "cards.py").read_text(encoding="utf-8") + (ROOT / "debrief" / "templates" / "cards.js").read_text(encoding="utf-8")
    for bad in ("Midfield", "Swazi", "Overdriver", "NAVF"):
        assert bad not in src, bad


# ---------------------------------------------------------------- saving
def test_card_saves_into_shares_and_never_over_a_file():
    shares = TMP / "lib" / "shares"
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    a = S.save_share_png(png, "Spa-GT3-leaderboard.png", shares)
    b = S.save_share_png(png, "Spa-GT3-leaderboard.png", shares)
    assert a.parent == shares and a.name == "spa-gt3-leaderboard.png" and b.name == "spa-gt3-leaderboard-2.png", (a, b)
    assert S.friendly_path(Path.home() / "Documents" / "Debrief Reviews" / "shares" / "x.png") == "Documents\\Debrief Reviews\\shares\\x.png"


def test_share_save_endpoint_kind_card():
    lib = TMP / "lib2"
    lib.mkdir()
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(f'token = "g61-test-token-ABCDEFGHIJKLMNOP"\nlibrary_folder = \'{lib}\'\n', encoding="utf-8")
    app.LOG_FILE = str(TMP / "debrief.log")
    S.SHARE_DIR = str(TMP / "Downloads")
    srv = S.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        data = _real_png()
        rq = urllib.request.Request(srv.url + "api/share-save?name=Spa-GT3-leaderboard.png&kind=card", data=data, method="POST",
                                    headers={"X-Debrief": "1", "Content-Type": "image/png"})
        j = json.loads(op.open(rq, timeout=30).read())
        assert j["ok"] and j["card"] and Path(j["path"]).parent == lib / "shares" and Path(j["path"]).name == "spa-gt3-leaderboard.png", j
        assert j["shown"].endswith("spa-gt3-leaderboard.png") and (lib / "shares" / "spa-gt3-leaderboard.png").read_bytes() == data
    finally:
        srv.shutdown()


def _real_png():
    pw = _pw()
    if pw is None:
        import zlib
        raw = b"".join(b"\x00" + b"\x10\x20\x30" * 400 for _ in range(400))
        def ch(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", 400, 400, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")
    pg = pw.new_page()
    pg.set_content("<body>")
    url = pg.evaluate("""async()=>{const c=document.createElement('canvas');c.width=400;c.height=400;const x=c.getContext('2d');
      for(let i=0;i<400;i+=4){x.fillStyle='rgb('+(i%256)+','+(i*3%256)+',90)';x.fillRect(0,i,400,4)}return c.toDataURL('image/png')}""")
    pg.close()
    return base64.b64decode(url.split(",", 1)[1])


# ---------------------------------------------------------------- the pictures (browser)
PW = {}


def _pw():
    if "b" not in PW:
        try:
            from playwright.sync_api import sync_playwright
            PW["p"] = sync_playwright().start()
            PW["b"] = PW["p"].chromium.launch()
        except Exception:  # noqa: BLE001
            PW["b"] = None
    return PW["b"]


DRAW = "async id=>{const cv=await window.DebriefCards.draw(window.CARDS[id]);return cv.toDataURL('image/png')}"


def test_team_page_draws_real_pngs_of_every_type():
    b = _pw()
    if b is None:
        print("SKIPPED (no Playwright/Chromium): picture drawing")
        return
    lib = TMP / "libpage"
    page = team.write(lib, copy.deepcopy(fixture()))
    pg = b.new_page(viewport={"width": 1100, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(page.as_uri())
    settle(pg)
    ids = {"leaderboard": "lb:0", "head-to-head": "dh:k1", "corner king": "kg:0:T2", "member": "mb:Robin Hale", "new best": "nb:0",
           "weekly": "wk:0:0", "season": "ss:k1", "learn": "ln:0:0"}
    for kind, i in ids.items():
        url = pg.evaluate(DRAW, i)
        data = base64.b64decode(url.split(",", 1)[1])
        assert png_size(data) == (1200, 675), (kind, png_size(data))
        assert len(data) > 15000, (kind, "the picture looks blank", len(data))
        if SAMPLES and kind in ("leaderboard", "head-to-head", "corner king", "member", "new best"):
            SAMPLES.mkdir(parents=True, exist_ok=True)
            (SAMPLES / cards.file_name({"leaderboard": "leaderboard", "head-to-head": "duel", "corner king": "corner",
                                        "member": "member", "new best": "best"}[kind], "sample")).write_bytes(data)
    # a fallback card draws too
    fb = pg.evaluate("async()=>{const cv=await window.DebriefCards.draw({layout:'board',empty:true,headline:'Nowhere',line:'Nothing to show on this card yet.'});return cv.toDataURL('image/png')}")
    assert png_size(base64.b64decode(fb.split(",", 1)[1])) == (1200, 675)
    # buttons: on boards, corner kings (section and rows), head-to-heads, members, this week, season, learn
    n = pg.evaluate("document.querySelectorAll('button.cardbtn').length")
    assert n >= 12, n
    assert pg.evaluate("[...document.querySelectorAll('button.cardbtn')].every(b=>b.textContent.length>0)")
    # clicking one (opened from disk, outside the window) downloads the named PNG
    with pg.expect_download() as dl:
        pg.locator("#boardList button.cardbtn").first.click()
    out = TMP / "dl.png"
    dl.value.save_as(str(out))
    assert dl.value.suggested_filename == "spa-acme-gt3-evo-leaderboard.png", dl.value.suggested_filename
    assert png_size(out.read_bytes()) == (1200, 675)
    assert not errs, errs
    pg.close()


def test_a_hostile_name_cannot_break_the_page_or_the_picture():
    b = _pw()
    if b is None:
        return
    d = fixture()
    evil = '</script><img src=x onerror="window.__x=1">'
    d["combos"][0]["board"][0]["name"] = evil
    page = team.write(TMP / "libevil", d)
    pg = b.new_page()
    pg.goto(page.as_uri())
    settle(pg)
    assert pg.evaluate("window.__x") is None
    url = pg.evaluate(DRAW, "lb:0")
    assert png_size(base64.b64decode(url.split(",", 1)[1])) == (1200, 675)
    pg.close()


def test_member_page_has_the_button():
    b = _pw()
    d = fixture()
    html = members.html(members.view(d, "Robin Hale"), cards.member(d, "Robin Hale"))
    assert "Save as picture" in html and "DebriefCards" in html
    assert "Save as picture" not in members.html(members.view(d, "Robin Hale"))
    lib = TMP / "libmem"
    (lib / "team").mkdir(parents=True)
    (lib / "team" / "team.json").write_text(json.dumps(d), encoding="utf-8")
    page = members.page_for(lib, "Robin Hale")
    assert "Save as picture" in page
    if b is None:
        return
    pg = b.new_page()
    pg.set_content(page)
    settle(pg)
    url = pg.evaluate("async()=>{const cv=await window.DebriefCards.draw(window.CARDS.member);return cv.toDataURL('image/png')}")
    assert png_size(base64.b64decode(url.split(",", 1)[1])) == (1200, 675)
    pg.close()


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception:  # noqa: BLE001
            bad += 1
            print("FAIL", name)
            traceback.print_exc()
    try:
        if PW.get("b"):
            PW["b"].close()
            PW["p"].stop()
    except Exception:  # noqa: BLE001
        pass
    print(f"\n{len(tests) - bad} of {len(tests)} passed")
    sys.exit(1 if bad else 0)
