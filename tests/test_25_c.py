"""Workstream 25-C (Debrief 2.5 pages): team.html "This week" boards, season head-to-heads and "Who to learn
from"; review.html "Who to learn from" lines in the Fix-first cards and the share responder (postMessage).

    python3 tests/test_25_c.py

Fixtures: tests/fixtures_25_c/ (rebuild with tests/fixtures_25_c/make_samples.py, SPEC §3.1 shapes). When
25-A's samples exist (tests/fixtures_25_a/ in this copy or in ../25-A) they are rendered too. Pages go through
the real render paths (team.write, report.write). Browser checks use Playwright + Chromium and print SKIPPED
when it isn't installed; the static checks always run. Nothing here talks to Garage 61 or Discord.
"""
from __future__ import annotations

import base64
import copy
import faulthandler
import json
import re
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from debrief import report, team  # noqa: E402
from pw_settle import settle  # noqa: E402

TPL = ROOT / "debrief" / "templates"
HERE = Path(__file__).resolve().parent
FX = HERE / "fixtures_25_c"
FX23 = HERE / "fixtures_23_c"
FX24 = HERE / "fixtures_24_c"
A_DIRS = [HERE / "fixtures_25_a", ROOT.parent / "25-A" / "tests" / "fixtures_25_a"]
XSS = '<img src=x onerror="window.__x=(window.__x||0)+1">'
BAD_WORDS = re.compile(r"\b(will gain|costing|because|you should|the reason)\b", re.I)


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def fx(name):
    return load(FX / name)


class Tmp:
    def __enter__(self):
        self.p = Path(tempfile.mkdtemp(prefix="debrief-25c-"))
        return self.p

    def __exit__(self, *a):
        shutil.rmtree(self.p, ignore_errors=True)


def _team_page(data, lib: Path) -> Path:
    return team.write(lib, copy.deepcopy(data))                     # <lib>/team/index.html (real render path)


def _review_page(data, lib: Path, name="2026-10-27_roval") -> Path:
    out = lib / "reviews" / name
    out.mkdir(parents=True, exist_ok=True)
    return report.write(out, copy.deepcopy(data))                   # <lib>/reviews/<name>/index.html


def review_25():
    d = load(FX24 / "sample_review_24.json")
    d["learn_from"] = fx("sample_learn_from.json")
    return d


# ---------------------------------------------------------------- browser helpers
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


def _skip():
    return _browser() is None


def _open(path, scheme="light", width=1280, init=None, wait=450):
    b = _browser()
    ctx = b.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error"
          and "fonts.g" not in (m.location or {}).get("url", "") and "ERR_" not in m.text else None)
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    if init:
        pg.add_init_script(init)
    pg.goto(Path(path).resolve().as_uri())
    settle(pg, cap_ms=max(1500, 3 * wait))     # loaded, painted and quiet (was a fixed `wait` ms)
    return pg, ctx, errs


TEAM_STATE = """()=>{const vis=id=>!document.getElementById(id).hidden;
  return {weekly:vis('weekly'),season:vis('season'),learn:vis('learn'),boards:vis('boards'),kings:vis('kings'),h2h:vis('h2h'),
  jump:[...document.querySelectorAll('#jump a')].filter(a=>!a.hidden).map(a=>a.textContent),
  bad:/NaN|undefined|Infinity|Invalid Date|\\[object/.test(document.body.innerText),
  fail:[...document.querySelectorAll('.empty')].some(e=>/couldn.t be drawn/.test(e.textContent))}}"""


# ================================================================ static checks (always run)
def _between(s, a, b):
    i = s.index(a)
    j = s.index(b, i + len(a))
    return s[i:j]


def _team25(s):
    return _between(s, "// ---------- 2.5: this week's boards", "</script>")


def _review25(s):
    return (_between(s, "// 2.5: who to learn from. D.learn_from", "function fixCard(")
            + _between(s, "// ---------- 2.5: share responder", "// ---------- 2.2.2: what to work on"))


def test_static_review_never_posts_anything_itself():
    s = (TPL / "review.html").read_text(encoding="utf-8")
    assert "connect-src 'none'" in s
    for w in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource"):
        assert w not in s, w
    blk = _review25(s)
    # the only postMessage calls are the two replies in the responder, both to the parent, plus (2.6) the
    # keyboard forwarder, which sends the parent nothing but a fixed shortcut name
    assert s.count("postMessage(") == 3 and blk.count("window.parent.postMessage(") == 2, s.count("postMessage(")
    assert s.count("parent.postMessage({type:'debrief-key',key:m},'*')") == 1
    assert "window.parent===window||e.source!==window.parent)return" in blk
    assert "m.type!=='debrief:share-request'" in blk and "type:'debrief:share-card'" in blk
    assert "Post to Discord" not in s          # the Post button lives in the app's frame bar (25-D)
    assert "innerHTML" not in blk


def test_static_team_no_innerhtml_tokens_only_words():
    s = (TPL / "team.html").read_text(encoding="utf-8")
    assert "connect-src 'none'" in s
    blk = _team25(s)
    assert "innerHTML" not in blk
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", blk)
    css = _between(s, "/* 2.5: this week's boards", "</style>")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", css)
    html = _between(s, '<section id="weekly"', '<section id="boards">') + _between(s, '<section id="learn"', '<section id="h2h">')
    for seg in (blk, html, _review25((TPL / "review.html").read_text(encoding="utf-8"))):
        assert not BAD_WORDS.search(seg), BAD_WORDS.search(seg)
    assert "Both are best laps: this compares your best driving, not your typical lap." in html   # 2.5 fixes L4: no causal "so"


def test_static_fixture_shapes():
    """our fixtures carry the SPEC §3.1 keys the pages read"""
    d = fx("sample_team_25.json")
    w = d["weekly"]
    assert set(w) >= {"season", "series", "h2h"}
    wk = w["series"][0]["weeks"][0]
    assert set(wk) >= {"week", "track", "final", "board"}
    assert set(wk["board"][0]) >= {"key", "name", "t", "car", "class", "date", "pos", "class_pos", "gap", "gap_pct", "is_me"}
    assert set(w["h2h"][0]) >= {"key", "name", "series", "shared_weeks", "ahead", "behind", "median_gap_pct", "verdict"}
    lf = fx("sample_learn_from.json")[0]
    assert set(lf) >= {"corner", "apex_m", "teammate", "gap_s", "how", "text", "alt", "similar_pace", "lap_gap_pct"}
    assert "weekly" not in fx("sample_team_24.json") and all("learn" not in c for c in fx("sample_team_24.json")["combos"])


# ================================================================ team.html in the browser
def test_team_24_hides_new_sections():
    if _skip():
        return
    with Tmp() as lib:
        pg, ctx, errs = _open(_team_page(fx("sample_team_24.json"), lib))
        st = pg.evaluate(TEAM_STATE)
        ctx.close()
        assert not errs and not st["bad"] and not st["fail"], (errs, st)
        assert not st["weekly"] and not st["season"] and not st["learn"], st
        assert st["boards"] and st["kings"] and st["h2h"], st
        assert st["jump"] == ["Leaderboards", "Corner kings", "Head-to-heads"], st


def test_team_25_this_week_boards():
    if _skip():
        return
    with Tmp() as lib:
        pg, ctx, errs = _open(_team_page(fx("sample_team_25.json"), lib))
        st = pg.evaluate(TEAM_STATE)
        panels = pg.query_selector_all("#wkList > .panel")
        mx5, imsa = panels[0], panels[1]
        opts = mx5.eval_on_selector_all("select option", "o=>o.map(x=>x.textContent)")
        rows = lambda p: p.eval_on_selector_all("tbody tr", "t=>t.map(r=>({me:r.classList.contains('me'),c:[...r.cells].map(c=>c.textContent)}))")
        r7 = rows(mx5)
        heads = mx5.eval_on_selector_all("thead th", "t=>t.map(x=>x.textContent)")
        mx5.query_selector("select").select_option("1")
        pg.wait_for_timeout(50)
        r6 = rows(mx5)
        state6 = mx5.query_selector(".state").text_content()
        chips = imsa.eval_on_selector_all(".clsrow button", "b=>b.map(x=>x.textContent)")
        all_rows = rows(imsa)
        imsa.query_selector(".clsrow button:nth-child(3)").click()             # GT4
        pg.wait_for_timeout(50)
        gt4 = rows(imsa)
        pressed = imsa.eval_on_selector_all(".clsrow button", "b=>b.map(x=>x.getAttribute('aria-pressed'))")
        imsa.query_selector("select").select_option("1")                        # week 5: empty board + note
        pg.wait_for_timeout(50)
        w5 = imsa.inner_text()
        ctx.close()
        assert not errs and not st["bad"] and not st["fail"], (errs, st)
        assert st["weekly"] and st["jump"][:2] == ["This week", "Season"] and "Who to learn from" in st["jump"], st
        assert opts == ["Week 7 · Okayama · in progress", "Week 6 · Lime Rock Park · final"], opts
        assert heads == ["Pos", "Driver", "Best lap", "Gap", "Gap %", "Car", "Date"], heads
        assert [r["c"][1] for r in r7] == ["Jordan X", "Test DriverYou", "Sam Y"], r7
        assert [r["me"] for r in r7] == [False, True, False]
        assert r7[0]["c"][2] == "1:38.412" and r7[0]["c"][3] == "Leader" and r7[1]["c"][3] == "+0.491 s", r7
        assert r7[1]["c"][4] == "+0.50 %" and r7[1]["c"][6] == "Oct 29", r7
        assert [r["c"][1] for r in r6] == ["Test DriverYou", "Jordan X"] and state6 == "Final", (r6, state6)
        assert chips == ["All classes", "GT3", "GT4"], chips
        assert [r["c"][0] for r in all_rows] == ["1", "2", "3", "4"], all_rows
        assert all_rows[3]["c"][5].endswith("GT4 P2") and all_rows[3]["c"][3] == "+0.380 s", all_rows   # gap within class
        assert all_rows[2]["c"][3] == "Class leader", all_rows
        assert [r["c"][0] for r in gt4] == ["1", "2"] and gt4[1]["me"] and pressed == ["false", "false", "true"], (gt4, pressed)
        assert "No team laps this week." in w5 and "final" in w5.lower(), w5


def test_team_25_season_head_to_heads():
    if _skip():
        return
    with Tmp() as lib:
        pg, ctx, errs = _open(_team_page(fx("sample_team_25.json"), lib))
        cards = pg.eval_on_selector_all("#seasonList > .panel", """c=>c.map(p=>({line:p.querySelector('.sline').textContent,
          notes:[...p.querySelectorAll('.note')].map(n=>n.textContent),dots:p.querySelectorAll('svg circle').length,
          fills:[...p.querySelectorAll('svg circle')].map(x=>x.getAttribute('fill')),
          tips:[...p.querySelectorAll('svg circle title')].map(x=>x.textContent)}))""")
        ctx.close()
        assert not errs, errs
        j, s = cards
        assert j["line"] == "Jordan: ahead of you in 3 of 5 weeks · median 0.42 % quicker", j
        assert "Jordan is slightly quicker than you over the season so far." in j["notes"], j
        assert j["dots"] == 5 and j["fills"][0] == "var(--b)" and j["fills"][1] == "var(--a)", j   # plus = they were quicker
        assert j["tips"][0] == "Week 3: you 1:38.900, Jordan 1:38.500 · Jordan was 0.41 % quicker", j
        assert s["line"] == "Sam: ahead of you in 0 of 1 week · median 0.41 % slower", s
        assert "Too few shared weeks to call it yet." in s["notes"] and s["dots"] == 1, s   # no verdict with 1 week


def test_team_25_who_to_learn_from():
    if _skip():
        return
    with Tmp() as lib:
        pg, ctx, errs = _open(_team_page(fx("sample_team_25.json"), lib))
        r = pg.evaluate("""()=>({panels:document.querySelectorAll('#learnList > .panel').length,
          rows:[...document.querySelectorAll('#learnList .lrow')].map(x=>({cid:x.querySelector('.cid').textContent,who:x.querySelector('.who').textContent,
            gap:x.querySelector('.gapv').textContent,facts:[...x.querySelectorAll('.fact')].map(f=>f.textContent),
            text:(x.querySelector('.ltext')||{}).textContent,alt:(x.querySelector('.alt')||{}).textContent})),
          basis:document.getElementById('learnBasis').textContent})""")
        ctx.close()
        assert not errs, errs
        assert r["panels"] == 1 and len(r["rows"]) == 3, r            # Okayama has learn: [] and no panel
        a, b, c = r["rows"]
        assert (a["cid"], a["who"], a["gap"]) == ("C4", "Jordan X", "0.21 s quicker"), a
        assert a["facts"] == ["Braked 11 m later", "+3.0 mph at the slowest point", "Full throttle 22 m sooner after the apex"], a
        assert a["text"].startswith("Jordan's best lap was 0.21 s quicker") and a["alt"] == "Also quicker here: Sam Y (0.16 s).", a
        assert b["facts"] == ["Gear 2 (you: 3)"] and b["alt"] is None, b
        assert c["alt"] == "Their best lap overall is 1.9 % quicker than yours.", c
        assert "Both are best laps" in r["basis"]
        for x in r["rows"]:
            assert not BAD_WORDS.search(" ".join([x["text"] or "", x["alt"] or ""] + x["facts"])), x


def test_team_weekly_only_and_empty_learn():
    """a team.json holding only `weekly` (no combos) still shows the boards; `learn` present but empty shows a note"""
    if _skip():
        return
    with Tmp() as lib:
        d = fx("sample_team_25.json")
        only = {"version": 1, "weekly": d["weekly"]}
        pg, ctx, errs = _open(_team_page(only, lib))
        st = pg.evaluate(TEAM_STATE)
        ctx.close()
        assert not errs and not st["bad"] and not st["fail"], (errs, st)
        assert st["weekly"] and st["season"] and not st["learn"] and not st["boards"], st
        assert st["jump"] == ["This week", "Season"], st
        d2 = fx("sample_team_24.json")
        for c in d2["combos"]:
            c["learn"] = []
        d2["weekly"] = {"season": "2026 Season 4", "series": [], "h2h": []}
        pg, ctx, errs = _open(_team_page(d2, lib))
        st = pg.evaluate(TEAM_STATE)
        txt = pg.inner_text("#learn") + pg.inner_text("#weekly") + pg.inner_text("#season")
        ctx.close()
        assert not errs and not st["bad"] and st["learn"] and st["weekly"] and st["season"], (errs, st)
        assert "No corner stands out yet." in txt and "No weekly leaderboards yet." in txt and "No season head-to-heads yet." in txt


def test_team_garbage_2_5_keys_degrade():
    if _skip():
        return
    with Tmp() as lib:
        d = fx("sample_team_24.json")
        d["weekly"] = {"series": [None, 3, {"name": "X", "weeks": [None, {"week": "x"}, {"week": 3, "board": [None, {"t": "a"}, {"t": 90.0}]}]},
                                  {"id": "y", "weeks": "nope"}], "h2h": [None, {}, {"key": "z", "series": [None, {"weeks": [None, {"week": 1}]}]}]}
        d["combos"][0]["learn"] = [None, {}, {"corner": "C9", "how": {"gear": [3], "min_mph": ["a", 2]}, "teammate": "nope"}]
        pg, ctx, errs = _open(_team_page(d, lib))
        st = pg.evaluate(TEAM_STATE)
        ctx.close()
        assert not errs and not st["bad"] and not st["fail"], (errs, st)
        assert st["weekly"] and st["learn"] and st["season"], st
        d["weekly"] = "nope"
        pg, ctx, errs = _open(_team_page(d, lib))
        st = pg.evaluate(TEAM_STATE)
        ctx.close()
        assert not errs and not st["weekly"] and not st["season"], (errs, st)


def test_team_escaping_dark_and_narrow():
    if _skip():
        return
    with Tmp() as lib:
        d = fx("sample_team_25.json")
        w = d["weekly"]
        w["season"] = XSS
        w["series"][0]["name"] = XSS
        w["series"][0]["weeks"][0]["track"] = XSS
        w["series"][0]["weeks"][0]["note"] = XSS
        w["series"][1]["weeks"][0]["board"][0].update(name=XSS, car=XSS, **{"class": XSS})
        w["h2h"][0]["name"] = XSS
        w["h2h"][0]["verdict"] = XSS
        lr = d["combos"][0]["learn"][0]
        lr["text"] = XSS
        lr["corner"] = XSS
        lr["teammate"]["name"] = XSS
        lr["alt"]["name"] = XSS
        pg, ctx, errs = _open(_team_page(d, lib), scheme="dark", width=390, init="window.__x=0")
        r = pg.evaluate("({x:window.__x||0,imgs:document.querySelectorAll('img').length,sw:document.documentElement.scrollWidth,"
                        "shown:document.body.innerText.includes('<img src=x')})")
        ctx.close()
        assert not errs and r["x"] == 0 and r["imgs"] == 0 and r["shown"], (errs, r)
        assert r["sw"] <= 390, r


def test_team_sibling_samples_if_present():
    """25-A's samples (when they exist) render through team.write with no errors"""
    if _skip():
        return
    src = next((p for p in A_DIRS if p.is_dir()), None)
    if not src:
        print("  (25-A samples not there yet: skipped)")
        return
    with Tmp() as lib:
        tw = src / "sample_team_with_weekly.json"
        if tw.exists():
            d = load(tw)
            pg, ctx, errs = _open(_team_page(d, lib))
            st = pg.evaluate(TEAM_STATE)
            lines = pg.eval_on_selector_all("#seasonList .sline", "x=>x.map(e=>e.textContent)")
            first_opt = pg.eval_on_selector_all("#wkList > .panel", "x=>x.map(p=>{const o=p.querySelector('select option');return o?o.textContent:p.querySelector('.wkhead .note').textContent})")
            ctx.close()
            assert not errs and not st["bad"] and not st["fail"] and st["weekly"], (tw, errs, st)
            # "{First}: ahead of you in N of M": N is `behind` (the weeks THEY were ahead), as team.head_to_heads
            want = [f"{p['name'].split()[0]}: ahead of you in {p['behind']} of {p['shared_weeks']}" for p in d["weekly"]["h2h"]]
            assert [x[:len(w)] for x, w in zip(lines, want)] == want, (lines, want)
            # the picker opens on the newest week of each series
            newest = [max(w["week"] for w in s["weeks"]) for s in d["weekly"]["series"] if s.get("weeks")]
            assert [o.startswith(f"Week {n} ") for o, n in zip(first_opt, newest)] == [True] * len(newest), (first_opt, newest)
        wv = src / "sample_weekly_view.json"
        if wv.exists():
            d = fx("sample_team_24.json")
            d["weekly"] = load(wv)
            pg, ctx, errs = _open(_team_page(d, lib))
            st = pg.evaluate(TEAM_STATE)
            ctx.close()
            assert not errs and not st["bad"] and not st["fail"] and st["weekly"], (wv, errs, st)
        lf = src / "sample_learn_from.json"
        if lf.exists():
            rows = load(lf)
            rows = rows if isinstance(rows, list) else rows.get("learn_from") or rows.get("rows") or []
            d = fx("sample_team_24.json")
            d["combos"][0]["learn"] = rows
            pg, ctx, errs = _open(_team_page(d, lib))
            st = pg.evaluate(TEAM_STATE)
            ctx.close()
            assert not errs and not st["bad"] and not st["fail"] and st["learn"], (lf, errs, st)
            r = review_25()
            r["learn_from"] = rows
            pg, ctx, errs = _open(_review_page(r, lib))
            ctx.close()
            assert not errs, (lf, errs)


# ================================================================ review.html in the browser
FIX_STATE = """()=>({fail:!document.getElementById('failNote').hidden,
  cards:[...document.querySelectorAll('#fixList .fix')].map(f=>({id:f.querySelector('h3').textContent.split(' ')[0],
    learn:(f.querySelector('.learn')||{}).textContent||null,href:(f.querySelector('.learn a')||{href:null}).getAttribute&&f.querySelector('.learn a')?f.querySelector('.learn a').getAttribute('href'):null})),
  bad:/NaN|undefined|Infinity|Invalid Date|\\[object/.test(document.body.innerText)})"""


def test_review_learn_from_in_fix_cards():
    if _skip():
        return
    with Tmp() as lib:
        pg, ctx, errs = _open(_review_page(review_25(), lib))
        st = pg.evaluate(FIX_STATE)
        ctx.close()
        assert not errs and not st["fail"] and not st["bad"], (errs, st)
        got = {c["id"]: c for c in st["cards"]}
        assert set(got) == {"C1", "C2", "C3", "C4", "C5"}, got
        # matched by apex: kings' C4 (2010 m) is this review's C3, kings' C5 (2735 m) is C4; nothing for C1/C5
        assert got["C3"]["learn"].startswith("Who to learn from: Jordan's best lap was 0.21 s"), got["C3"]
        assert got["C4"]["learn"].startswith("Who to learn from: Jordan's best lap was 0.09 s"), got["C4"]
        assert got["C2"]["learn"].startswith("Who to learn from: Sam's best lap"), got["C2"]
        assert got["C1"]["learn"] is None and got["C5"]["learn"] is None, got
        assert got["C3"]["learn"].endswith("Team page") and got["C3"]["href"] == "../../team/index.html", got["C3"]


def test_review_learn_from_by_id_without_apex_and_escaping():
    if _skip():
        return
    with Tmp() as lib:
        d = review_25()
        d["learn_from"] = [{"corner": "C1", "teammate": {"key": "x", "name": XSS}, "gap_s": 0.12, "text": XSS},
                           {"corner": "C5", "apex_m": 9999.0, "teammate": {"name": "Far"}, "gap_s": 0.3},
                           {"corner": "C2", "teammate": {"name": "Sam Y"}, "gap_s": 0.07},
                           {"corner": "C1", "apex_m": 2012.0, "teammate": {"name": "Apex"}, "gap_s": 0.2, "text": "By apex."}, None, "x"]
        pg, ctx, errs = _open(_review_page(d, lib), init="window.__x=0")
        st = pg.evaluate(FIX_STATE)
        x = pg.evaluate("({x:window.__x||0,imgs:document.querySelectorAll('#fixList img').length})")
        ctx.close()
        assert not errs and not st["fail"] and not st["bad"] and x == {"x": 0, "imgs": 0}, (errs, st, x)
        got = {c["id"]: c["learn"] for c in st["cards"]}
        assert got["C1"] == "Who to learn from: " + XSS + "Team page", got
        assert got["C5"] is None, got                           # apex far from every corner: not attached
        assert got["C3"] == "Who to learn from: By apex.Team page", got   # the apex wins over a stale corner id
        assert got["C2"].startswith("Who to learn from: Sam’s best lap was 0.07 s quicker than yours through C2."), got
        assert "Both are best laps" in got["C2"] and not BAD_WORDS.search(got["C2"]), got


def _matrix():
    """SPEC §4.1 old reviews (2.2.1 / pre-v2 / no GPS / no traces / practice / quali / result only)"""
    out = [(n, load(FX23 / n)) for n in ("sample_review_old.json", "sample_review_new.json", "sample_review_nomap.json",
                                         "sample_review_practice.json", "sample_review_quali.json", "sample_review_reason.json")]
    out.append(("sample_review_old_result.json", load(FX24 / "sample_review_old_result.json")))
    out.append(("sample_review_24.json", load(FX24 / "sample_review_24.json")))
    return out


def test_review_old_reviews_render_without_learn_from():
    if _skip():
        return
    import make_fixture_library
    with Tmp() as lib:
        flib = lib / "fixture-lib"
        make_fixture_library.main(str(flib))
        lib_reviews = sorted((flib / "reviews").glob("*/review.json"))
        cases = _matrix() + [(p.parent.name, load(p)) for p in (lib_reviews[0], lib_reviews[-1])]   # an old-format and a v2 row
        for name, d in cases:
            assert "learn_from" not in d
            pg, ctx, errs = _open(_review_page(d, lib, "r-" + re.sub(r"\W+", "-", name)))
            st = pg.evaluate(FIX_STATE)
            ctx.close()
            assert not errs and not st["fail"] and not st["bad"], (name, errs, st)
            assert all(c["learn"] is None for c in st["cards"]), (name, st)
        # an old review that gains learn_from (app.cmd_review attaches it) still renders
        d = load(FX23 / "sample_review_old.json")
        d["learn_from"] = fx("sample_learn_from.json")
        pg, ctx, errs = _open(_review_page(d, lib, "old-lf"))
        st = pg.evaluate(FIX_STATE)
        ctx.close()
        assert not errs and not st["fail"] and not st["bad"], (errs, st)


# ---------------------------------------------------------------- share responder (postMessage)
PARENT = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<iframe id="rv" src="REVIEW" width="1000" height="700"></iframe>
<iframe id="other" srcdoc="<script>window.ask=id=>parent.frames[0].postMessage({type:'debrief:share-request',id},'*')</script>"></iframe>
<script>window.got=[];addEventListener('message',e=>{if(e.source===document.getElementById('rv').contentWindow)window.got.push(e.data)});</script>
</body></html>"""


def _parent(lib, data):
    page = _review_page(data, lib)
    p = lib / "parent.html"
    p.write_text(PARENT.replace("REVIEW", page.resolve().as_uri()), encoding="utf-8")
    return p


def _png_size(url):
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR"
    return struct.unpack(">II", raw[16:24])


def test_share_round_trip_from_parent():
    if _skip():
        return
    with Tmp() as lib:
        pg, ctx, errs = _open(_parent(lib, review_25()), wait=900)
        pg.evaluate("document.getElementById('rv').contentWindow.postMessage({type:'debrief:share-request',id:'abc123'},'*')")
        pg.wait_for_function("window.got.length>0", timeout=15000)
        got = pg.evaluate("window.got")
        ctx.close()
        assert not errs, errs
        assert len(got) == 1, got
        m = got[0]
        assert m["type"] == "debrief:share-card" and m["id"] == "abc123" and "error" not in m, {k: v for k, v in m.items() if k != "png"}
        assert m["png"].startswith("data:image/png;base64,") and _png_size(m["png"]) == (1200, 630)
        assert m["text"] == "Test Driver · Roval · Mustang GT4: best 1:37.018, P7 from P11", m["text"]


def test_share_text_new_pb_and_no_result():
    if _skip():
        return
    with Tmp() as lib:
        d = review_25()
        d["pb"] = {"is_pb": True, "delta": 0.21}
        d["result"] = {"finish_pos": 3}
        d["meta"]["driver"] = ""
        pg, ctx, errs = _open(_parent(lib, d), wait=900)
        pg.evaluate("document.getElementById('rv').contentWindow.postMessage({type:'debrief:share-request',id:7},'*')")
        pg.wait_for_function("window.got.length>0", timeout=15000)
        m = pg.evaluate("window.got[0]")
        ctx.close()
        assert not errs, errs
        assert m["id"] == 7 and m["text"] == "Roval · Mustang GT4: best 1:37.018, P3, new PB", m["text"]


def test_share_no_reply_to_non_parent_or_wrong_type():
    if _skip():
        return
    with Tmp() as lib:
        pg, ctx, errs = _open(_parent(lib, review_25()), wait=900)
        pg.evaluate("document.getElementById('other').contentWindow.ask('sibling')")          # a sibling frame asks
        pg.evaluate("""(()=>{const w=document.getElementById('rv').contentWindow;
          w.postMessage({type:'debrief:share-card',id:'wrong-type'},'*');
          w.postMessage('debrief:share-request','*');
          w.postMessage({type:'debrief:share-request',id:{}},'*');
          w.postMessage({type:'debrief:share-request',id:'x'.repeat(500)},'*')})()""")
        # instead of a fixed 3 s wait: the valid request below is answered after the bad ones were handled,
        # and waiting as long again as its reply took covers a reply to a bad one that started earlier
        none = pg.evaluate("window.got")
        t0 = time.perf_counter()
        pg.evaluate("document.getElementById('rv').contentWindow.postMessage({type:'debrief:share-request',id:'ok'},'*')")
        pg.wait_for_function("window.got.length>0", timeout=15000)
        reply_ms = int((time.perf_counter() - t0) * 1000)
        pg.wait_for_timeout(max(300, reply_ms))
        after = pg.evaluate("window.got.map(m=>m.id)")
        ctx.close()
        assert not errs, errs
        assert none == [], none
        assert after == ["ok"], after                      # the responder still works for its parent
    with Tmp() as lib:
        # opened on its own (no parent frame): a message to itself gets no reply
        pg, ctx, errs = _open(_review_page(review_25(), lib),
                              init="window.__got=[];addEventListener('message',e=>{if(e.data&&e.data.type==='debrief:share-card')window.__got.push(e.data)})")
        pg.evaluate("window.postMessage({type:'debrief:share-request',id:'self'},'*')")
        pg.wait_for_timeout(max(500, 2 * reply_ms))      # twice what a real reply took above (was a fixed 2.5 s)
        got = pg.evaluate("window.__got")
        ctx.close()
        assert not errs and got == [], (errs, got)


def test_share_responder_on_old_review():
    if _skip():
        return
    with Tmp() as lib:
        pg, ctx, errs = _open(_parent(lib, load(FX23 / "sample_review_old.json")), wait=900)
        pg.evaluate("document.getElementById('rv').contentWindow.postMessage({type:'debrief:share-request',id:'old'},'*')")
        pg.wait_for_function("window.got.length>0", timeout=15000)
        m = pg.evaluate("window.got[0]")
        ctx.close()
        assert not errs, errs
        assert m["id"] == "old" and m["png"].startswith("data:image/png;base64,") and ": best " in m["text"], m.get("text")


if __name__ == "__main__":
    faulthandler.dump_traceback_later(600, exit=True)
    fails = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            print("ok  ", name)
        except Exception as e:  # noqa: BLE001
            fails += 1
            import traceback
            traceback.print_exc()
            print("FAIL", name, e)
    if "pw" in _PW:
        try:
            _PW["b"].close()
            _PW["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
    faulthandler.cancel_dump_traceback_later()
    print(f"{fails} failed" if fails else "all passed")
    sys.exit(1 if fails else 0)
