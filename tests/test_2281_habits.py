"""Debrief 2.28.1 "Clearer habits": every habit card says what happened / what it costs / a Try, the numbers sit in a fold,
cards are ranked by cost, the fade card shows the measured change and the fuel estimate apart, and a corner-by-corner
strip replaces the good-against-bad contradiction.

    python3 tests/ci_run.py tests/test_2281_habits.py

Synthetic data only, offline, utf-8. Browser tests use Playwright + Chromium and are skipped when either is missing.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import habits_cases_2281 as hc  # noqa: E402
from debrief import insights, report  # noqa: E402

TPL = ROOT / "debrief" / "templates" / "review.html"
GOLD = json.loads((ROOT / "tests" / "golden_2281_old.json").read_text(encoding="utf-8"))
NUM = re.compile(r"\d+(?:[.:]\d+)*")
_CASES = {}


def cases():
    if not _CASES:
        for k, d in hc.all_cases():
            _CASES[k] = d
    return {k: copy.deepcopy(v) for k, v in _CASES.items()}


def items(ins):
    return [(s, i) for s in ("bad", "good", "look") for i in ins[s]]


def card_sentences(it):
    parts = [it.get("text"), it.get("costs"), it.get("try")] + list(it.get("numbers") or [])
    out = []
    for p in parts:
        for sent in re.split(r"(?<=[.!?])\s+", p or ""):
            sent = sent.strip().rstrip(".").lower()
            if sent:
                out.append(sent)
    return out


# ---------------------------------------------------------------- words
def test_no_sentence_twice_in_a_card_and_every_card_has_three_parts():
    for name, d in cases().items():
        ins = insights.build(d)
        for s, it in items(ins):
            sents = card_sentences(it)
            assert len(sents) == len(set(sents)), (name, it["key"], sents)
            if s in ("bad", "look"):
                assert it["text"] and it["costs"] is not None or it["key"] in ("brake_spread", "vary") or it["badge"], (name, it)
                assert it["try"] and it["try"].startswith("Try:"), (name, it["key"], it["try"])
            assert "±" not in json.dumps(it, ensure_ascii=False) and "Middle half" not in json.dumps(it), (name, it["key"])
        for f in ins["fix"]:
            if "try" in f:
                assert f["try"].startswith("Try:") and f["corner"] in f["try"]


def test_no_number_lost_against_2_28_0():
    """Every number the 2.28.0 habits and fix cards printed is still on the card or in its fold (as printed there)."""
    for name, d in cases().items():
        ins = insights.build(d)
        pool = " ".join(" ".join(filter(None, [it.get("text"), it.get("costs"), it.get("try"), " ".join(it.get("numbers") or []),
                                                  it.get("detail")])) for _s, it in items(ins))
        pool += " " + " ".join((ins.get("strip") or {}).get("lines") or [])
        gold = GOLD[name]
        pool_nums = set(NUM.findall(pool))
        for h in gold["habits"]:
            old = (h["text"] or "") + " " + (h["detail"] or "")
            old = re.sub(r"\b\d+(?:\.\d+)?/100\b", "", old)
            old = old.replace("lap 1: ~0.90 s around 150 m", "")   # lap 1 is on its own Lap 1 card now, not twice
            old = old.replace("-0.00 s", "no change")      # 2.28.0 printed a negative zero; now it says "no change"
            for n in set(NUM.findall(old)):
                assert n in pool_nums, (name, h["key"], n, old)
        # the same fix corners with the same gains
        assert [(f["corner"], f["gain"]) for f in ins["fix"]] == [(f["corner"], f["gain"]) for f in gold["fix"]]


def test_plain_words_no_jargon():
    for name, d in cases().items():
        for _s, it in items(insights.build(d)):
            shown = " ".join(filter(None, [it["text"], it.get("costs"), it.get("try")]))
            assert "Middle half" not in shown and "middle half" not in shown and "±" not in shown, (name, it["key"], shown)
            assert not re.search(r"\d:\d\d\.\d\d\d", shown), (name, "lap time with thousandths on the card", shown)
            assert "shuffles" not in shown and "better laps" not in shown, (name, shown)
            assert not re.search(r"\d+ m\b", shown), (name, it["key"], "metres belong in the fold", shown)


def test_lap_one_is_not_shown_twice():
    ins = insights.build(cases()["rich_unknown_start"])
    assert [i["key"] for i in ins["look"]].count("lap1") == 1
    tr = next(i for i in ins["look"] if i["key"] == "traffic")
    assert tr["laps"] == [5] and "lap 1" not in tr["text"].lower() and "lap 1" not in " ".join(tr["numbers"])


def test_distances_become_corner_names():
    d = cases()["rich_unknown_start"]
    ins = insights.build(d)
    tr = next(i for i in ins["bad"] if i["key"] == "traffic")
    assert "just after Turn 7" in tr["try"] and "2500 m" in " ".join(tr["numbers"]) and "2500" not in tr["try"], tr
    # the place words: inside a corner, just after one, between two, and wrapping past the start line
    d2 = cases()["rich_unknown_start"]
    d2["counts"]["traffic_detail"] = [{"n": 7, "lost": 1.0, "at_m": 950.0}, {"n": 8, "lost": 1.0, "at_m": 1550.0},
                                      {"n": 9, "lost": 1.0, "at_m": 2950.0}]
    d2["counts"]["traffic"] = [7, 8, 9]
    t = next(i for i in insights.build(d2)["bad"] if i["key"] == "traffic")
    assert "in Turn 3" in t["try"] and "between Turn 4 and Turn 5" in t["try"] and "between Turn 8 and Turn 1" in t["try"], t["try"]


# ---------------------------------------------------------------- the fade card
def test_fade_shows_measured_change_and_fuel_estimate_apart_and_the_arithmetic_holds():
    d = cases()["rich_unknown_start"]
    tf = d["trend_fuel"]
    ins = insights.build(d)
    f = next(i for i in ins["bad"] if i["key"] == "fade")
    raw, fx, total = d["trend_total"], d["stint"]["fuel_s_per_lap"], tf["total"]
    span = round((total - raw) / fx)
    assert abs(raw + fx * span - total) < 0.02, (raw, fx, span, total)           # raw + fuel per lap x laps = corrected
    assert abs(span - raw / (d["trend_per10"] / 10.0)) < 0.6                       # the span is first-to-last, not the lap count
    assert f["text"].startswith(f"Measured: your clean laps got about {raw:.1f} s slower")
    assert f"the real slowdown is about {total:.1f} s" in f["text"] and "estimate" in f["text"]
    assert "Tyres play a part" not in json.dumps(f) and "tyres or in the driving" in f["costs"]
    fold = " ".join(f["numbers"])
    assert f"{raw:+.2f}" in fold and f"{fx:.3f}" in fold and f"{total:+.2f}" in fold and f"{span} laps" in fold
    assert f["corners"] == ["Turn 6", "Turn 3"] and "Turn 6 and Turn 3" in f["try"]   # from the Stint section, 0.05 s or more
    assert f["text"].count("2.5") == 1


def test_fade_only_over_the_line_because_of_the_estimate_says_so():
    d = cases()["rich_unknown_start"]
    d["trend_total"] = 0.05
    d["trend_per10"] = 0.05 / 13 * 10
    d["trend_fuel"] = {"total": 0.05 + 0.147 * 13, "fuel_s_per_lap": 0.147, "per10": 0.5, "corrected": True}
    f = next(i for i in insights.build(d)["bad"] if i["key"] == "fade")
    assert "most of that is the estimate, not measured" in f["text"], f["text"]
    # ranked on the measured part only: an estimate must not push the card to the top of Fix these
    n = d["counts"]["clean"]
    assert abs(f["badge"]["race_s"] - 0.05 * n / 2.0) < 0.01, f["badge"]
    assert "if the fuel estimate is right" in f["costs"], f["costs"]


def test_fade_without_fuel_data_says_so_plainly():
    f = next(i for i in insights.build(cases()["rich_nofuel"])["bad"] if i["key"] == "fade")
    assert "no fuel data" in f["text"] and "Measured" not in f["text"] and "(not fuel-corrected" in " ".join(f["numbers"])


def test_steady_pace_never_prints_a_negative_zero():
    g = next(i for i in insights.build(cases()["flat_fuel"])["good"] if i["key"] == "steady")
    assert "-0.00" not in json.dumps(g) and "no change" in g["numbers"][0]


# ---------------------------------------------------------------- cost badges and order
def test_cards_are_ranked_by_cost_and_badges_are_honest():
    for name, d in cases().items():
        ins = insights.build(d)
        costs = [(i["badge"].get("race_s") if i["badge"]["kind"] == "time" else None) for i in ins["bad"] if i["badge"]["kind"] != "safety"]
        known = [c for c in costs if c is not None]
        assert known == sorted(known, reverse=True), (name, costs)
        assert all(c is None for c in costs[len(known):]) or costs.index(None) >= len(known) if None in costs else True
        for _s, i in items(ins):
            b = i["badge"]
            assert b["kind"] in ("time", "safety", "none") and b["text"]
            if b["kind"] == "time":
                assert b["race_s"] is not None and b["race_s"] > 0
            else:
                assert b["race_s"] is None            # no invented seconds
        unclean = [i for i in ins["bad"] if i["key"] == "unclean"]
        for u in unclean:
            assert u["badge"]["kind"] == "safety" and u["badge"]["text"] == "safety"
    ins = insights.build(cases()["rich_unknown_start"])
    keys = [i["key"] for i in ins["bad"]]
    assert keys.index("unclean") == 1, keys                                   # 2 of 16 laps flagged: after the biggest cost
    d = cases()["rich_unknown_start"]
    d["counts"]["unclean"] = [3, 4, 12, 13]                                   # a quarter of the laps: first
    assert insights.build(d)["bad"][0]["key"] == "unclean"
    # per-lap costs are multiplied by the clean laps so they compare with race totals
    ph = next(i for i in ins["bad"] if i["key"] == "phase")
    assert abs(ph["badge"]["race_s"] - ph["badge"]["per_lap"] * 10) < 0.02 and "s/lap (~" in ph["badge"]["text"]


def test_phase_card_uses_the_real_base_and_names_the_corners():
    ph = next(i for i in insights.build(cases()["rich_unknown_start"])["bad"] if i["key"] == "phase")
    assert "mostly in Turn 2, Turn 6 and Turn 5" in ph["text"] and "of the 0.5 s a lap" in ph["costs"]
    assert ph["text"].startswith("Most of the time")                         # 0.28 of a 0.50 s gap is over half
    d = cases()["rich_unknown_start"]
    d["skills"]["overall"]["loss_s"] = 0.9
    ph = next(i for i in insights.build(d)["bad"] if i["key"] == "phase")
    assert ph["text"].startswith("The biggest part of the time") and "0.3 s of the 0.9 s" in ph["costs"]


def test_possibly_traffic_does_not_claim_off_pace():
    t = next(i for i in insights.build(cases()["rich_unknown_start"])["look"] if i["key"] == "traffic")
    assert "off-pace" not in json.dumps(t) and "stay in your pace numbers" in t["costs"]


# ---------------------------------------------------------------- the corner strip
def test_strip_never_shows_a_mark_without_data():
    for name, d in cases().items():
        strip = insights.build(d).get("strip")
        if not strip:
            continue
        for r in strip["rows"]:
            assert any(r[p] for p in ("braking", "mid", "exit")), (name, r)
            for p in ("braking", "mid", "exit"):
                c = r[p]
                assert c is None or (c["mark"] in ("ok", "watch", "cost") and c["note"]), (name, r["corner"], p, c)
    d = cases()["rich_unknown_start"]
    strip = insights.build(d)["strip"]
    row = {r["corner"]: r for r in strip["rows"]}
    assert row["Turn 3"]["braking"]["mark"] == "ok" and row["Turn 3"]["mid"]["mark"] == "cost"     # no more contradiction
    assert row["Turn 2"]["exit"]["mark"] == "cost" and row["Turn 4"]["braking"]["mark"] == "watch"
    assert row["Turn 7"]["exit"] is None and row["Turn 7"]["braking"] is None                       # no data: a dash, not a guess
    # take the numbers away and the marks go with them
    d["skills"] = {"reason": "not_enough_clean_laps"}
    d["brake_spread"] = {"corners": []}
    d["consistency"]["by_corner"] = [{"id": "Turn 3", "iqr": 0.35}]
    s2 = insights.build(d)["strip"]
    assert [r["corner"] for r in s2["rows"]] == ["Turn 3"] and s2["rows"][0]["braking"] is None and s2["rows"][0]["exit"] is None
    assert len(s2["no_data"]) == 7
    d["consistency"]["by_corner"] = []
    assert insights.build(d)["strip"] is None


def test_corner_specific_good_habits_are_marked_by_scope():
    ins = insights.build(cases()["rich_unknown_start"])
    assert {g["key"]: g["scope"] for g in ins["good"]} == {"locked": "corner", "brake_markers": "corner"}


# ---------------------------------------------------------------- the page
class Tmp:
    def __enter__(self):
        self.p = Path(tempfile.mkdtemp(prefix="debrief-2281-"))
        return self.p

    def __exit__(self, *a):
        shutil.rmtree(self.p, ignore_errors=True)


def _browser():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        return pw, pw.chromium.launch()
    except Exception:  # noqa: BLE001
        return None, None


def _page_data():
    import test_28_page as t28
    d = t28.base()
    r = cases()["rich_unknown_start"]
    for k in ("counts", "stint", "trend_total", "trend_per10", "trend_fuel", "consistency", "brake_spread", "skills", "median", "slow_cut"):
        d[k] = r[k]
    d["insights"] = insights.build(dict(d, corners=r["corners"], laps=r["laps"], racecraft=r["racecraft"], L=r["L"]))
    for row, c in zip(d["insights"]["strip"]["rows"], d["corners"]):
        row["corner"] = c["id"]        # the page's own corners, so the corner chips have somewhere to go
    return t28, d


def test_page_folds_open_by_keyboard_and_nothing_overflows():
    pw, br = _browser()
    if not br:
        return
    t28, d = _page_data()
    with Tmp() as f:
        path = t28.page_file(d, f)
        for width, scheme in ((1280, "light"), (1024, "dark"), (390, "light")):
            ctx = br.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
            pg.goto(path.as_uri())
            pg.wait_for_function("()=>document.querySelectorAll('#badList .hcard').length>0", timeout=15000)
            r = pg.evaluate("""()=>{const q=s=>[...document.querySelectorAll(s)];
                const cards=q('#badList .hcard').map(c=>({t:c.querySelector('b').textContent,badge:(c.querySelector('.hbadge')||{}).textContent,
                    parts:c.querySelectorAll('p').length,try:!!c.querySelector('p.try'),fold:!!c.querySelector('details.hfold')}));
                const box=document.querySelector('.hab2').getBoundingClientRect();
                const over=q('.hab2 *').filter(e=>e.getBoundingClientRect().right>box.right+1&&!e.closest('.tablewrap')).length;
                const rows=q('.hab2>*').map(e=>e.getBoundingClientRect().height);
                return {cards,over,docw:document.documentElement.scrollWidth,vw:innerWidth,chips:q('.hab2 .src button').map(b=>b.getBoundingClientRect().height),
                        strip:q('#stripTable tbody tr').length,marks:q('#stripTable td.m').map(x=>x.className),
                        titles:[...document.querySelectorAll('.hab2 h3')].map(h=>h.textContent.trim())}}""")
            assert r["cards"] and all(c["try"] and c["fold"] and c["badge"] for c in r["cards"]), r["cards"]
            assert r["over"] == 0 and r["docw"] <= r["vw"], (width, r)
            assert all(h >= 24 for h in r["chips"]), r["chips"]
            assert r["strip"] == 8 and any("cost" in m for m in r["marks"]) and any("none" in m for m in r["marks"]), r
            assert r["titles"][0].startswith("Fix these"), r["titles"]
            # a fold opens with the keyboard (Tab to the summary, Space opens it)
            pg.evaluate("document.querySelector('#badList .hcard summary').focus()")
            pg.keyboard.press("Space")
            assert pg.evaluate("document.querySelector('#badList .hcard details').open") is True
            assert pg.evaluate("document.querySelector('#badList .hcard details li')!==null")
            # a corner chip jumps to that corner and a lap chip to that lap (both are buttons)
            assert pg.evaluate("document.querySelectorAll('#stripTable td.c button').length") >= 1
            assert not errs, errs
            ctx.close()
    br.close()
    pw.stop()


if __name__ == "__main__":
    import traceback
    failed = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", n)
            except Exception:  # noqa: BLE001
                failed += 1
                print("FAIL", n)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
