"""Turn a review's numbers into a short coaching summary, sorted the way a driver reads it:

    verdict      one sentence in words: what to work on (the figures are on the summary tiles)
    fix          up to 5 corners worth working on now: what to change, roughly what it's worth, and the
                 numbers behind it ("numbers": best / typical / comparison technique, "times": corner times)
    bad          habits that repeat across laps (only listed when they clear a threshold)
    good         what you already do well
    look         (2.7) neutral "worth a look" notes: things that cost time but that the data can't call a
                 habit, e.g. a slow lap 1 when the start type (standing / rolling) isn't known

Every bad/good item is {key, title, text, cost} plus, when known, "detail" (one plain sentence with the
measurement behind it), "laps" [int] and "corners" [str]. Every fix item adds "numbers"
{"best", "typical", "ref"} (each {brake_on, min_mph, exit_mph, full_thr, coast_s, gear} or None) and
"times" {"best_t", "typical_t", "p25_t", "ref_t"}. Readers must treat all of these as optional.

Thresholds in seconds are for a ~90 s lap and scale with your median lap time (a 30 s short oval and an
8-minute Nordschleife lap are judged alike): every constant below with an "s" unit is multiplied by
_scale(median) (2.7: VARY_BAD / VARY_GOOD too, matching analysis_plus.iqr_label). Wording stays correlational: "on your quicker laps you
tended to...", never "do X and you'll gain Y".

Uses only numbers the analysis already worked out (engine + analysis_plus), so old reviews can be
summarised too. Every item has plain words; numbers are rounded to what a driver can act on.
"""
from __future__ import annotations

import statistics as st

MAX_FIX = 5              # corners in "fix immediately"
MIN_GAIN = 0.05          # s/lap: below this a corner isn't worth a "fix" item
VARY_BAD = 0.15          # s: corner time spread (IQR) that counts as "varies a lot"
VARY_GOOD = 0.06         # s: spread that counts as "locked in"
FADE_BAD = 0.3           # s: clean laps slower by this much from first to last
COAST_BAD = 0.5          # s/lap: extra coasting on typical laps vs your best lap
OVERLAP_BAD = 0.3        # s/lap: both pedals at once, more on a typical lap than on your best lap (2.18)
LUCK_MIN_LAPS = 8        # clean laps with pedal readings before coasting / overlap can be called a habit (2.19)
LUCK_ALPHA = 0.05        # ... and the quick laps must beat 95% of random shuffles of the laps (2.19)
LUCK_SHUFFLES = 2000
LAP1_GOOD = 0.05         # s: lap 1 at most this much slower than typical is "strong" (2.18: never praise a slower lap 1)
LAP1_BAD = 1.0           # s: lap 1 slower than the median
REF_LAP = 90.0           # the lap time the seconds above are written for
TECH_KEYS = ("brake_on", "min_mph", "exit_mph", "full_thr", "coast_s", "gear")
MAX_TIPS = 3
# 2.3 (technique_plus / brake_spread / skills)
SPREAD_BAD_LABELS = ("varies", "all over")
SPREAD_MIN_N = 8            # clean laps with a brake point before a brake-spread item is shown
PHASE_MIN_PM = 2.0          # per-mille of lap time: a phase must lose at least this much ...
PHASE_MIN_SHARE = 0.4       # ... and at least this share of the total typical-vs-better gap
PHASE_WORDS = {"braking": "in the braking zones", "entry": "between turning in and the slowest point",
               "mid": "between the slowest point and getting back on the throttle", "exit": "on corner exits"}
_TP_SKIP_NOTES = ("Not enough clean laps", "look the same on your quicker and slower laps")


def _scale(median) -> float:
    """Threshold multiplier for this track: median lap / 90 s (clamped to 0.25..6, as analysis_plus.time_scale)."""
    m = _f(median)
    return min(6.0, max(0.25, m / REF_LAP)) if m else 1.0


def luck_p(times, values):
    """2.19 luck check: how often would the quickest laps look this much better on a pedal habit by pure chance?
    times / values: one clean lap each (lap time, seconds of coasting or of both-pedals). The quick group is the
    fastest quarter of the laps (at least 3); the measure is how much lower its average is than the median of all the
    laps. The values are shuffled between laps LUCK_SHUFFLES times (fixed seed, so the same laps always give the same
    answer); p is the share of shuffles that look at least this good, counting the real one.
    Returns None when there are fewer than LUCK_MIN_LAPS laps."""
    import numpy as np
    pairs = [(float(t), float(v)) for t, v in zip(times, values) if t is not None and v is not None]
    if len(pairs) < LUCK_MIN_LAPS:
        return None
    pairs.sort(key=lambda tv: tv[0])
    v = np.array([x[1] for x in pairs])
    q = max(3, int(round(len(v) / 4)))
    overall = float(np.median(v))
    obs = overall - float(np.mean(v[:q]))
    rng = np.random.default_rng(219)
    order = rng.random((LUCK_SHUFFLES, len(v))).argsort(axis=1)[:, :q]       # the q laps a shuffle gives the quick group
    hits = int(np.sum(overall - np.mean(v[order], axis=1) >= obs - 1e-12))
    return (1 + hits) / (1 + LUCK_SHUFFLES)


def _f(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _nums(m) -> dict | None:
    """The technique numbers a fix item shows, copied from technique (None when absent)."""
    if not isinstance(m, dict):
        return None
    out = {}
    for k in TECH_KEYS:
        v = m.get(k)
        out[k] = (int(v) if k == "gear" and _f(v) is not None else (round(_f(v), 3) if _f(v) is not None else None))
    return out if any(v is not None for v in out.values()) else None


def fmt_t(s) -> str:
    if s is None:
        return "–"
    m = int(s // 60)
    return f"{m}:{s - m * 60:06.3f}"


def fmt_t1(s) -> str:
    """A lap time to tenths for the words on the page: 2:26.7 (the fold keeps thousandths with fmt_t)."""
    if s is None:
        return "–"
    tenths = int(round(s * 10))
    return f"{tenths // 600}:{(tenths % 600) / 10:04.1f}"


def _s1(x) -> str:
    """Seconds on the face of a card: one decimal, or "under 0.1 s" below 0.05 (a smaller figure is noise)."""
    return f"{x:.1f} s" if x >= 0.05 else "under 0.1 s"


def _chg(x) -> str:
    """"got about 0.6 s slower" / "got about 0.3 s quicker" / "did not change" for a first-to-last change in seconds."""
    if x is None or abs(x) < 0.05:
        return "did not change"
    return f"got about {abs(x):.1f} s {'slower' if x > 0 else 'quicker'}"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _laps(ns) -> str:
    ns = sorted(int(n) for n in ns)
    return ("lap " if len(ns) == 1 else "laps ") + ", ".join(map(str, ns))


def corner_tips(tech: dict | None, against: str = "best") -> list[str]:
    """Concrete differences between your typical lap and a target in a corner.

    against="ref": the comparison lap. Otherwise your own quicker laps through this corner (the quickest
    quarter, technique.quick), or your best lap when an older review has no "quick" and the best lap was
    actually quicker than usual there. No tips when neither fits (copying a lap that was slow in this
    corner would be the wrong advice)."""
    if not tech:
        return []
    typ = tech.get("typical") or {}
    if against == "ref":
        tgt, who = tech.get("ref"), "the comparison lap"
    elif tech.get("quick"):
        tgt, who = tech.get("quick"), "your quicker laps here"
    elif tech.get("best_quick", True) is not False:
        tgt, who = tech.get("best"), "your best lap"
    else:
        tgt, who = None, ""
    if not tgt:
        return []
    tips = []
    bt, bb = _f(typ.get("brake_on")), _f(tgt.get("brake_on"))
    if bt is not None and bb is not None and abs(bb - bt) >= 5:
        d = round(abs(bb - bt) / 5) * 5
        tips.append(f"brake {d:.0f} m {'later' if bb > bt else 'earlier'}, like {who}")
    mt, mb = _f(typ.get("min_mph")), _f(tgt.get("min_mph"))
    if mt is not None and mb is not None and abs(mb - mt) >= 1.5:
        tips.append(f"{'carry' if mb > mt else 'slow to'} {abs(mb - mt):.0f} mph {'more' if mb > mt else 'less'} at the slowest point "
                    f"({mb:.0f} mph)")
    ft, fb = _f(typ.get("full_thr")), _f(tgt.get("full_thr"))
    if ft is not None and fb is not None and abs(fb - ft) >= 10:
        d = round(abs(fb - ft) / 5) * 5
        tips.append(f"back to full throttle {d:.0f} m {'sooner' if fb < ft else 'later'}")
    ct, cb = _f(typ.get("coast_s")), _f(tgt.get("coast_s"))
    if ct is not None and cb is not None and ct - cb >= 0.15:
        tips.append(f"cut {ct - cb:.1f} s of coasting (off both pedals)")
    gt, gb = typ.get("gear"), tgt.get("gear")
    if isinstance(gt, int) and isinstance(gb, int) and gt != gb:
        tips.append(f"try gear {gb} (you usually use {gt})")
    return tips


def _with_quick(t: dict | None, corner: dict | None, clean_set: set) -> dict | None:
    """Reviews made before 2.2.2 have no technique.quick: work it out from the corner's per-lap numbers
    (median of your clean laps in the quickest quarter through this corner)."""
    if not t or t.get("quick") or not corner:
        return t
    per = corner.get("per_lap") if isinstance(corner.get("per_lap"), dict) else {}
    rows = [m for n, m in per.items() if isinstance(m, dict) and _f(m.get("t")) is not None
            and (not clean_set or str(n) in clean_set)]
    if len(rows) < 4:
        return {**t, "best_quick": _best_was_quick(t, rows)}
    ts = sorted(m["t"] for m in rows)
    p25 = _pctl(ts, 25)
    q = [m for m in rows if m["t"] <= p25 + 1e-9]
    if len(q) < 2:
        return t
    quick = {}
    for key in TECH_KEYS:
        vals = [m.get(key) for m in q if _f(m.get(key)) is not None]
        if not vals:
            quick[key] = None
        elif key == "gear":
            quick[key] = int(st.mode(vals))
        else:
            quick[key] = float(st.median(vals))
    return {**t, "quick": quick}


def _pctl(sorted_vals, q):
    """numpy-style linear percentile of an already sorted list."""
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _best_was_quick(t, rows):
    b = _f(((t or {}).get("best") or {}).get("t"))
    ts = sorted(m["t"] for m in rows)
    return True if b is None or not ts else b < st.median(ts)


def _ref_corner_t(c: dict, main_ref: dict | None):
    if not main_ref:
        return None
    rc = (c.get("refs") or {}).get(main_ref.get("name"))
    return _f(rc.get("t")) if isinstance(rc, dict) else None


def _lap_list(v) -> list:
    out = []
    for n in v or []:
        try:
            out.append(int(n))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _and(a):
    a = [str(x) for x in a]
    return a[0] if len(a) == 1 else ", ".join(a[:-1]) + " and " + a[-1] if a else ""


def _tp_notes(data: dict) -> dict:
    """corner id -> first useful technique_plus note (2.3 reviews only). Steering notes only when the
    telemetry really had steering."""
    tp = data.get("technique_plus") if isinstance(data.get("technique_plus"), dict) else {}
    steer = bool((tp.get("channels") or {}).get("steering")) if isinstance(tp.get("channels"), dict) else False
    out = {}
    for c in tp.get("corners") or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        for note in c.get("notes") or []:
            if not isinstance(note, str) or any(k in note for k in _TP_SKIP_NOTES):
                continue
            if not steer and "steering" in note.lower():
                continue
            out[c["id"]] = note
            break
    return out


def _brake_spread(data: dict):
    bs = data.get("brake_spread")
    if isinstance(bs, dict):
        return bs
    try:   # report.write sets it (2.3); work it out here too so insights alone gives the same answer
        from . import technique_plus
        return technique_plus.spread_for_review(data)
    except Exception:
        return None


def _start_type(data: dict) -> str | None:
    """"standing" / "rolling" when race craft (2.7, racecraft.lap1.start_type) knows it, else None."""
    rc = data.get("racecraft") if isinstance(data.get("racecraft"), dict) else None
    l1 = rc.get("lap1") if rc and isinstance(rc.get("lap1"), dict) else None
    v = (l1 or {}).get("start_type")
    return v if isinstance(v, str) and v in ("standing", "rolling") else None


def _lap1_segments(data: dict) -> list:
    """racecraft.lap1.segments ([{id, lost_s}]) when the review has them (2.4), else []."""
    rc = data.get("racecraft") if isinstance(data.get("racecraft"), dict) else None
    l1 = rc.get("lap1") if rc and isinstance(rc.get("lap1"), dict) else None
    if not l1 or not l1.get("in_analysis"):
        return []
    return [s for s in l1.get("segments") or [] if isinstance(s, dict) and s.get("id") and _f(s.get("lost_s")) is not None][:2]


STEADY_WORDS = {"Very consistent": "Your laps are very steady", "Consistent": "Your laps are steady",
                "Up and down": "Your laps come and go", "Scattered": "Your laps vary a lot"}
VERDICT_PHASES = {"braking": "under braking", "entry": "on the way into the corners",
               "mid": "through the middle of the corners", "exit": "on the way out of the corners"}


def _verdict_sentence(fix, con, skills) -> str:
    """What to work on, as one sentence with no figures in it."""
    if not fix:
        return ("No corner stands out: your typical laps are already close to your best, so more pace will "
                "come from new references (follow quicker drivers on Garage 61).")
    where = " and ".join(str(x["corner"]) for x in fix[:2])
    lead = STEADY_WORDS.get(con.get("label"))
    text = f"{lead}. The quickest gain is in {where}" if lead else f"The quickest gain is in {where}"
    phases = skills.get("phases") if isinstance(skills, dict) else None
    if isinstance(phases, dict):
        loss = {k: _f(v.get("loss_s")) or 0.0 for k, v in phases.items() if k in VERDICT_PHASES and isinstance(v, dict)}
        total = sum(v for v in loss.values() if v > 0)
        if total > 0:
            top = max(loss, key=loss.get)
            if loss[top] / total >= 0.5:
                text += f", and across the lap most of the time goes {VERDICT_PHASES[top]}"
    return text + "."


def try_text(it: dict) -> str:
    """2.28.1: the concrete "Try:" for a corner where no single measure stands out."""
    cid = it.get("corner")
    lap = it.get("best_lap")
    if it.get("basis") == "ref":
        return (f"Try: put the comparison lap beside yours in Compare laps, zoomed in on {cid}, and match where it turns in.")
    vs = f"your best lap there (lap {lap})" if lap is not None else "your best lap there"
    return (f"Try: next race, use one turn-in point in {cid} every lap, and compare {vs} with a typical lap in Compare laps.")


def watch_text(it: dict) -> str:
    """2.18: what to look at next time in a corner where no single measure stands out (never an empty "fix").
    2.28.1: the shared explanation first (the page says it once above the cards), then this corner's own Try."""
    cid = it.get("corner")
    if it.get("basis") == "ref":
        first = (f"Your brake point, slowest speed, throttle and gear in {cid} don't differ much from the comparison "
                 "lap's, so the time is in something these numbers don't show.")
    else:
        first = (f"Your brake point, slowest speed, throttle and gear in {cid} look about the same on quicker and slower "
                 "laps, so no single number explains the loss.")
    return first + " " + try_text(it)


STRIP_COST = 0.05        # s a lap (scaled): a phase that loses this much against your quicker laps "costs time" (2.28.1)
STRIP_WATCH = 0.02       # ... and this much is "worth a look"
_MARK = {0: "ok", 1: "watch", 2: "cost"}


def _loss_level(x, k):
    x = _f(x)
    if x is None:
        return None
    return 2 if x >= STRIP_COST * k else 1 if x >= STRIP_WATCH * k else 0


def _strip(data: dict, corners: list, spread: list, con: dict, sk, k: float, fix_gain: dict | None = None) -> dict | None:
    """2.28.1: one row per corner with braking / mid-corner / exit marked ok (nothing flagged), watch (worth a look) or
    cost (flagged AND the typical laps lose time there against the quicker laps). Built only from numbers the analysis
    already has: brake-point spread, corner-time spread (consistency.by_corner) and the per-corner phase losses
    (skills.corners). A phase with no data is None (shown as a dash), never a guess.
    {"rows": [{"corner", "braking", "mid", "exit", "cost_s"}], "no_data": [ids], "counts": {...}, "lines": [str]};
    a cell is {"mark": ok|watch|cost, "note": str} or None. "cost_s" is the corner's own gain from Fix first (your typical
    lap loses that much there against your quicker laps), so the strip never says nothing costs time while a card does."""
    sk_c = {c["id"]: c for c in (sk or {}).get("corners") or [] if isinstance(c, dict) and c.get("id")
            and isinstance(c.get("loss"), dict)} if isinstance(sk, dict) and not sk.get("reason") else {}
    sp = {c["id"]: c for c in spread if _f(c.get("iqr_m")) is not None and c.get("n", 0) >= SPREAD_MIN_N}
    cons = {b["id"]: _f(b.get("iqr")) for b in con.get("by_corner") or [] if isinstance(b, dict) and b.get("id")}
    order = sorted(corners, key=lambda c: (_f(c.get("apex_m")) is None, _f(c.get("apex_m")) or 0.0))
    rows, no_data = [], []
    for c in order:
        cid = c["id"]
        loss = sk_c.get(cid, {}).get("loss", {})
        cells = {}
        # braking: how much the brake point moves, and the time lost in the braking zone
        lv, notes = [], []
        s_ = sp.get(cid)
        if s_:
            lab = s_.get("label")
            lv.append(0 if lab in ("locked", "steady") else 1)
            notes.append("brake point the same every lap" if lab == "locked" else
                         f"brake point spread {s_['iqr_m']:.0f} m ({lab})")
        b_loss = _f(loss.get("braking"))
        if b_loss is not None:
            lv.append(_loss_level(b_loss, k))
            notes.append(f"braking zone loses {b_loss:.2f} s a lap")
        cells["braking"] = {"mark": _MARK[max(lv)], "note": "; ".join(notes)} if lv else None
        # mid-corner: how much the time through the corner changes, and the time lost between turn-in and throttle
        lv, notes = [], []
        iqr = cons.get(cid)
        if iqr is not None:
            lv.append(1 if iqr >= VARY_BAD * k else 0)
            notes.append(f"time through the corner changes by about {iqr / 2:.1f} s lap to lap" if iqr >= 0.1 else "time through the corner is steady")
        mids = [_f(loss.get(p)) for p in ("entry", "mid") if _f(loss.get(p)) is not None]
        if mids:
            lv.append(_loss_level(sum(mids), k))
            notes.append(f"turn-in to throttle loses {sum(mids):.2f} s a lap")
        cells["mid"] = {"mark": _MARK[max(lv)], "note": "; ".join(notes)} if lv else None
        # exit
        x_loss = _f(loss.get("exit"))
        cells["exit"] = ({"mark": _MARK[_loss_level(x_loss, k)], "note": f"exit loses {x_loss:.2f} s a lap"}
                         if x_loss is not None else None)
        cost_s = (fix_gain or {}).get(cid)
        if not any(cells.values()) and cost_s is None:
            no_data.append(cid)
            continue
        rows.append({"corner": cid, **cells, "cost_s": cost_s})
    if not rows:
        return None
    cnt = {m: sum(1 for r in rows for ph in ("braking", "mid", "exit") if r[ph] and r[ph]["mark"] == m) for m in _MARK.values()}
    cnt["cost_corners"] = sum(1 for r in rows if r["cost_s"] is not None or any(r[ph] and r[ph]["mark"] == "cost" for ph in ("braking", "mid", "exit")))
    lines = []
    for r in rows:
        bits = [f"{lab}: {r[ph]['note']}" for ph, lab in (("braking", "braking"), ("mid", "mid-corner"), ("exit", "exit")) if r[ph]]
        if r["cost_s"] is not None:
            bits.append(f"your typical lap loses {r['cost_s']:.2f} s a lap here against your quicker laps")
        lines.append(f"{r['corner']}: " + "; ".join(bits) + ".")
    return {"rows": rows, "no_data": no_data, "counts": cnt, "lines": lines}


def build(data: dict) -> dict:
    corners = [c for c in data.get("corners") or [] if isinstance(c, dict) and c.get("id")]
    tech = {t["id"]: t for t in data.get("technique") or [] if isinstance(t, dict) and t.get("id")}
    laps = [l for l in data.get("laps") or [] if isinstance(l, dict)]
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    best = data.get("best") if isinstance(data.get("best"), dict) else {}
    median = _f(data.get("median"))
    con = data.get("consistency") if isinstance(data.get("consistency"), dict) else {}
    refs = [r for r in data.get("refs") or [] if isinstance(r, dict) and not r.get("rival")]
    main_ref = next((r for r in refs if r.get("main")), refs[0] if refs else None)
    k = _scale(median)
    min_gain = MIN_GAIN * k
    clean_set = {str(l.get("n")) for l in laps if l.get("clean")}
    out = {"version": 2, "verdict": [], "fix": [], "bad": [], "good": [], "look": []}

    # ---------- fix immediately: the corners with the most time to find
    ranked = []
    for c in corners:
        own = _f(c.get("avail")) or 0.0
        ref = _f(c.get("ref_gap"))
        ref = ref if (ref is not None and ref > 0 and main_ref) else 0.0
        gain = max(own, ref)
        if gain < min_gain:
            continue
        basis = "ref" if ref > own else "own"
        t = _with_quick(tech.get(c["id"]), c, clean_set)
        tips = corner_tips(t, "ref" if basis == "ref" and t and t.get("ref") else "best")
        typ_t = _f(c.get("typical_t")) or _f(c.get("median_t"))
        p25 = _f(c.get("p25_t"))
        ref_t = _ref_corner_t(c, main_ref)
        who = (main_ref or {}).get("name") or "The comparison driver"
        who = who.split(" ")[0]
        if basis == "own":
            why = f"Your typical lap loses {_s1(own)} here compared with your quicker laps."
            detail = (f"Typical {typ_t:.2f} s through here; your quicker laps {p25:.2f} s."
                      if typ_t is not None and p25 is not None else None)
        else:
            why = f"{who} is {_s1(ref)} quicker here on a good lap."
            detail = (f"Your quicker laps {p25:.2f} s through here; {who} {ref_t:.2f} s."
                      if p25 is not None and ref_t is not None else None)
        summ = c.get("summary") if isinstance(c.get("summary"), dict) else {}
        ranked.append({"corner": c["id"], "gain": round(gain, 2), "basis": basis, "apex_m": c.get("apex_m"),
                       "start_m": c.get("start_m"), "end_m": c.get("end_m"), "best_lap": c.get("best_lap"),
                       "tips": tips[:3], "why": why, "detail": detail,
                       "gear": summ.get("gear"), "min_mph": summ.get("min_mph"),
                       "numbers": {"best": _nums((t or {}).get("best")), "typical": _nums((t or {}).get("typical")),
                                   "ref": _nums((t or {}).get("ref")), "quick": _nums((t or {}).get("quick"))},
                       "times": {"best_t": _f(c.get("best_t")), "typical_t": typ_t, "p25_t": p25, "ref_t": ref_t},
                       "corners": [c["id"]], "laps": _lap_list([c.get("best_lap")] if c.get("best_lap") is not None else [])})
    tp_notes = _tp_notes(data)
    for it in ranked:   # 2.3: the corner's first technique note goes with its tips (3 at most)
        note = tp_notes.get(it["corner"])
        if note and note not in it["tips"]:
            it["tips"] = it["tips"][:MAX_TIPS - 1] + [note]
    # 2.18: corners with a measured cause (a tip) come first, biggest first; then the rest, each with what to watch
    ranked.sort(key=lambda x: (not x["tips"], -x["gain"]))
    for it in ranked:
        if not it["tips"]:
            it["watch"] = watch_text(it)
            it["try"] = try_text(it)
    by_id = {c["id"]: c for c in corners}
    out["tips"] = {cid: corner_tips(_with_quick(t, by_id.get(cid), clean_set), "best")[:3] for cid, t in tech.items()}
    out["fix"] = ranked[:MAX_FIX]
    out["more_corners"] = len(ranked) - len(out["fix"])

    # ---------- habits (2.28.1): every card says what happened, what it costs and a concrete "Try:"; the measured
    # numbers go in "numbers" (shown in a fold). "detail" is the same numbers as one string, for older readers.
    n_clean_laps = len([l for l in laps if l.get("clean")]) or 1
    race_laps = max(1, n_clean_laps)
    in_fix = {f["corner"]: f for f in out["fix"]}
    cmap = {c["id"]: c for c in corners}
    Lm = _f(data.get("L")) or max([_f(c.get("end_m")) or 0.0 for c in corners] + [0.0])

    def place(m):
        """A lap distance in words, from the corner map: "in Turn 3", "just after Turn 9", "between Turn 5 and Turn 6"."""
        m = _f(m)
        pts = sorted([(_f(c.get("start_m")), _f(c.get("end_m")), c["id"]) for c in corners
                      if _f(c.get("start_m")) is not None and _f(c.get("end_m")) is not None])
        if m is None or not pts:
            return None
        if Lm > 0:
            m = m % Lm
        for s, e, cid in pts:
            if s <= m <= e:
                return f"in {cid}"
        prev = [p for p in pts if p[1] < m]
        prev = prev[-1] if prev else pts[-1]
        nxt = [p for p in pts if p[0] > m]
        nxt = nxt[0] if nxt else pts[0]
        if prev[2] == nxt[2]:
            return f"after {prev[2]}"
        back = (m - prev[1]) % Lm if Lm > 0 else m - prev[1]
        ahead = (nxt[0] - m) % Lm if Lm > 0 else nxt[0] - m
        return f"just after {prev[2]}" if back <= 120 and ahead > 120 else f"between {prev[2]} and {nxt[2]}"

    def b_lap(per_lap, label=""):
        race = per_lap * race_laps
        lap_txt = f"{per_lap:.1f} s/lap" if per_lap >= 0.05 else "under 0.1 s/lap"
        return {"kind": "time", "per_lap": round(per_lap, 4), "race_s": round(race, 4),
                "text": f"~{lap_txt}{label} (~{race:.1f} s over the race)"}

    def b_race(s, text=None):
        return {"kind": "time", "per_lap": None, "race_s": round(s, 4), "text": text or f"~{s:.1f} s over the race"}

    def b_none(text="no time cost measured"):
        return {"kind": "none", "per_lap": None, "race_s": None, "text": text}

    SAFETY = {"kind": "safety", "per_lap": None, "race_s": None, "text": "safety"}

    def add(bucket, key, title, text, costs=None, try_=None, numbers=None, cost=None, badge=None, laps=None,
            corners=None, scope="race"):
        numbers = [n for n in (numbers or []) if n]
        it = {"key": key, "title": title, "text": text, "cost": cost, "costs": costs, "try": try_, "numbers": numbers,
              "badge": badge or b_none(), "scope": scope}
        if numbers:
            it["detail"] = " ".join(numbers)
        if laps:
            it["laps"] = _lap_list(laps)
        if corners:
            it["corners"] = [str(x) for x in corners]
        out[bucket].append(it)

    n_laps = counts.get("analysed") if isinstance(counts.get("analysed"), int) else len(laps)
    n_laps += counts.get("excluded") if isinstance(counts.get("excluded"), int) else 0
    unclean = _lap_list(counts.get("unclean"))
    pit = _lap_list(counts.get("pit"))
    total_full = n_laps + len(pit)
    # old reviews (no excluded_laps) dropped spin laps before counting incidents, so "clean race" can't be trusted
    hidden_spins = bool(counts.get("excluded")) and "excluded_laps" not in counts
    flagged_share = 0.0
    if len(unclean) >= 2:
        flagged_share = len(unclean) / total_full if total_full else 0.0
        add("bad", "unclean", "Incidents or track limits", f"Garage 61 flagged {_laps(unclean)}.",
            "Each one is a lap you can't use, and a safety-rating risk.",
            f"Try: next race, open {_laps(unclean)} in Compare laps, find where it went wrong, and take that spot a touch "
            "more carefully until the lap is clean.",
            [f"{len(unclean)} of {total_full} full laps flagged."], badge=SAFETY, laps=unclean)
    elif not unclean and len(laps) >= 5 and not hidden_spins:
        add("good", "clean", "Clean race", "No laps flagged for incidents or track limits.",
            numbers=[f"0 of {total_full} full laps flagged by Garage 61."])

    lap1_item = False
    lap1 = next((l for l in laps if l.get("n") == 1), None)
    if lap1 and median and _f(lap1.get("t")) and 1 not in pit:
        d = lap1["t"] - median
        if d >= LAP1_BAD * k:
            nums = [f"Lap 1 {fmt_t(lap1['t'])} against a typical {fmt_t(median)}."]
            l1_corners = None
            segs = _lap1_segments(data)   # 2.4: where lap 1 lost its time (racecraft), when known
            if segs:
                nums.append("Most of it was in " + _and([f"{s['id']} ({s['lost_s']:.1f} s)" for s in segs]) +
                            " (that includes the start).")
                l1_corners = [s["id"] for s in segs]
            # correlational only (2.4.0 review L5): we don't say why. 2.7: it is only a bad habit when race craft
            # knows what kind of start it was (racecraft.lap1.start_type); otherwise lap 1 includes a start we
            # know nothing about, so it goes to the neutral "worth a look" list
            start = _start_type(data)
            text = (f"Lap 1 was {fmt_t1(lap1['t'])} against your typical {fmt_t1(median)}, "
                    f"with the {start} start included." if start else
                    f"Lap 1 was {fmt_t1(lap1['t'])} against your typical {fmt_t1(median)}, start included.")
            costs = f"That one lap cost about {d:.1f} s." + ("" if start else
                    " Debrief doesn't know what kind of start it was, so this isn't counted as a habit.")
            try_ = (f"Try: next race, plan lap 1 through {_and(l1_corners)}, where most of the time went, and watch it in "
                    "Compare laps first." if l1_corners else
                    "Try: replay the first corners of lap 1 in Compare laps and find where the time went.")
            add("bad" if start else "look", "lap1", "Lap 1", text, costs, try_, nums, cost=round(d, 1),
                badge=b_race(d), laps=[1], corners=l1_corners)
            lap1_item = True
        elif d <= LAP1_GOOD * k and len(laps) >= 5:
            # 2.18: praise only a lap 1 that was really as quick as a typical lap or quicker, and say which
            said = (f"Lap 1 was {-d:.1f} s quicker than your typical lap, start included." if d <= -0.05 else
                    "Lap 1 was as quick as your typical lap, start included.")
            add("good", "lap1", "Strong first lap", said,
                numbers=[f"Lap 1 {fmt_t(lap1['t'])} against a typical {fmt_t(median)}."], laps=[1])

    slow = [n for n in _lap_list(counts.get("slow")) if n != 1 and n not in pit]
    cut = _f(data.get("slow_cut")) or 1.0
    poss_in_slow = [n for n in _lap_list(counts.get("traffic_possible")) if n in slow]
    poss_td = {t.get("n"): t for t in counts.get("traffic_possible_detail") or [] if isinstance(t, dict)}
    if len(slow) >= 2:
        lap_t = {l.get("n"): _f(l.get("t")) for l in laps}
        lost = [lap_t.get(n) - median for n in slow if lap_t.get(n) is not None and median]
        why = ("likely a mistake or traffic" if not poss_in_slow else
               f"{_laps(poss_in_slow)} lost it in one short stretch, possibly traffic; the rest look like mistakes"
               if len(poss_in_slow) < len(slow) else "each lost it in one short stretch: possibly traffic, possibly a mistake")
        where = [f"lap {n} {place(poss_td[n].get('at_m'))}" for n in poss_in_slow
                 if n in poss_td and place(poss_td[n].get("at_m"))]
        tot_lost = sum(lost) if lost else None
        add("bad", "offpace", "Off-pace laps",
            f"{_laps(slow).capitalize()} were more than {cut:.1f} s off your typical lap without an incident flag: {why}.",
            f"Together those laps cost about {tot_lost:.1f} s." if tot_lost is not None else None,
            f"Try: open {_laps(slow)} in Compare laps and find where each one lost its time"
            + (f" (the short stretches were {_and(where)})" if where else "") + ".",
            ([f"{len(lost)} laps more than {cut:.1f} s over: " + ", ".join(f"lap {n} +{x:.1f} s" for n, x in zip(slow, lost)) + "."]
             if lost else None),
            cost=round(tot_lost, 1) if lost else None, badge=b_race(tot_lost) if lost else None, laps=slow)

    def traffic_item(bucket, key, title, text, ns, td, costs_tail, look=False):
        td = [t for t in td if isinstance(t, dict) and t.get("n") in ns]
        lost_sum = sum(_f(t.get("lost")) or 0.0 for t in td)
        places = [f"lap {t.get('n')} {place(t.get('at_m'))}" for t in td if place(t.get("at_m"))]
        num = ("; ".join(f"lap {t.get('n')}: ~{_f(t.get('lost')) or 0:.2f} s around {_f(t.get('at_m')) or 0:.0f} m"
                         for t in td[:4]) + ".") if td else None
        costs = (f"About {lost_sum:.1f} s lost in all." if td else "Debrief didn't measure how much time that was.")
        stay = "they stay in your pace numbers" if look else "not counted against you"
        add(bucket, key, title, text, costs + costs_tail,
            ("Try: if it was traffic, plan where to let faster cars by" if look else "Try: plan where to let faster cars by")
            + (f"; the stretch{'es were' if len(places) > 1 else ' was'} {_and(places)}." if places else "."),
            [num], badge=b_race(lost_sum, f"~{lost_sum:.1f} s lost ({stay})") if td else None, laps=ns)

    traffic = [n for n in _lap_list(counts.get("traffic")) if not (n == 1 and lap1_item)]
    if traffic:
        traffic_item("bad", "traffic", "Traffic (multi-class)",
                     f"{_laps(traffic).capitalize()} lost time in one short stretch, most likely to traffic. The laps are "
                     "left out of your pace numbers.", traffic, [t for t in counts.get("traffic_detail") or []], " Not counted against you.")
    poss = [n for n in _lap_list(counts.get("traffic_possible")) if not (n == 1 and lap1_item)]
    if poss:   # 2.7: single-class, the stricter detector: possibly traffic, possibly a mistake
        traffic_item("look", "traffic", "Possibly traffic",
                     f"{_laps(poss).capitalize()} lost time in one short stretch that your other laps didn't: possibly a "
                     "lapped or slower car, possibly a mistake.", poss, [t for t in counts.get("traffic_possible_detail") or []],
                     " Debrief can't tell which, so these laps stay in your pace numbers.", look=True)

    # 2.7: the fuel-corrected trend when the stint summary has one (fuel burning off hides tyre fade), and the
    # text says when it isn't corrected. 2.28.1: the measured change and the fuel estimate are shown separately.
    tf = data.get("trend_fuel") if isinstance(data.get("trend_fuel"), dict) else None
    fuel_fixed = tf is not None and _f(tf.get("total")) is not None
    trend = _f(tf.get("total")) if fuel_fixed else _f(data.get("trend_total"))
    raw_trend = _f(data.get("trend_total"))
    n_clean = (counts.get("clean") or 0) if isinstance(counts.get("clean"), int) else 0
    if trend is not None and n_clean >= 8:
        fx = _f(tf.get("fuel_s_per_lap")) if fuel_fixed else None
        corr = (f"fuel-corrected: about {fx or 0:.3f} s a lap of fuel-weight estimate added back"
                if fuel_fixed else "not fuel-corrected: these laps have no fuel data")
        if trend >= FADE_BAD * k:
            drift = sorted([z for z in ((data.get("stint") or {}).get("corner_drift") or [])
                            if isinstance(z, dict) and z.get("id") and (_f(z.get("delta_t")) or 0) >= 0.05],
                           key=lambda z: -z["delta_t"])[:3]
            names = [z["id"] for z in drift]
            if fuel_fixed and raw_trend is not None:
                if raw_trend < FADE_BAD * k:   # only the fuel estimate pushes it over the line: say so
                    text = (f"Including the fuel estimate, your clean laps got about {trend:.1f} s slower "
                            f"from first to last, but most of that is the estimate, not measured: the laps themselves {_chg(raw_trend)}.")
                else:
                    text = (f"Measured: your clean laps {_chg(raw_trend)} from first to last. Including the fuel burning "
                            f"off (an estimate), the slowdown is about {trend:.1f} s.")
            elif fuel_fixed:
                text = f"Including the fuel estimate, your clean laps got about {trend:.1f} s slower from first to last."
            else:
                text = (f"Your clean laps got about {trend:.1f} s slower from first to last. These laps have no fuel data, so "
                        "this is the measured change; the car getting lighter hides some of the fade.")
            nums = []
            if fuel_fixed:
                if raw_trend is not None:
                    nums.append(f"Trend line over {n_clean} clean laps: {raw_trend:+.2f} s from first to last (measured).")
                    if fx:
                        # the span the trend is measured over: first to last lap number (engine: max(nums) - min(nums))
                        ln = [l.get("n") for l in laps if isinstance(l.get("n"), int)]
                        t10 = _f(data.get("trend_per10"))
                        span = (max(ln) - min(ln)) if ln else (round(raw_trend / (t10 / 10.0)) if t10 else None)
                        nums.append(f"Fuel-weight estimate added back: about {fx:.3f} s a lap"
                                    + (f" over {span} laps (from the first lap to the last)" if span else "")
                                    + f" = {trend - raw_trend:.2f} s. This is an estimate, not a measurement.")
                nums.append(f"Fuel-corrected: {trend:+.2f} s from first to last.")
            else:
                nums.append(f"Trend line over {n_clean} clean laps: {trend:+.2f} s from first to last ({corr}).")
            nums += [f"{z['id']}: {_f(z.get('early_t')) or 0:.2f} s early, {_f(z.get('late_t')) or 0:.2f} s late "
                     f"({z['delta_t']:+.2f} s)." for z in drift]
            # 2.28.1 (lead): rank on what was measured, not on the fuel estimate, so a mostly-estimated fade
            # can't jump to the top of Fix these; the estimate is still said in plain words
            measured = raw_trend if (fuel_fixed and raw_trend is not None) else trend
            race_s = max(measured, 0.0) * n_clean / 2.0
            est_s = trend * n_clean / 2.0
            cost_line = (f"If the slowdown was steady, the measured part costs about {race_s:.1f} s over the {n_clean} clean "
                         f"laps (about {est_s:.1f} s if the fuel estimate is right). "
                         if fuel_fixed and raw_trend is not None and abs(est_s - race_s) >= 0.05 else
                         f"If the slowdown was steady, that costs about {race_s:.1f} s over the {n_clean} clean laps. ")
            add("bad", "fade", "Fading late in the race", text,
                cost_line + "The loss may be in the tyres or in the driving.",
                (f"Try: next race, watch {_and(names)} late in the stint, where you slowed most, and compare your first and "
                 "last clean laps there in Compare laps." if names else
                 "Try: open Stint and compare your first and last clean laps to see which corners slow down."),
                nums, cost=round(trend, 1), badge=b_race(race_s), corners=names)
        elif abs(trend) < 0.15 * k:
            chg = "no change" if abs(trend) < 0.005 else f"{trend:+.2f} s"
            add("good", "steady", "Held pace all race",
                "Your clean laps held steady from start to finish" +
                (", even after allowing for the fuel burning off." if fuel_fixed else ", though these laps have no fuel data."),
                numbers=[f"Trend line over {n_clean} clean laps: {chg} from first to last ({corr})."])

    vary = []
    locked = []
    for b in con.get("by_corner") or []:
        if not isinstance(b, dict):
            continue
        iqr = _f(b.get("iqr"))
        if iqr is None:
            continue
        if iqr >= VARY_BAD * k:
            vary.append((b["id"], iqr))
        elif iqr <= VARY_GOOD * k:
            locked.append(b["id"])
    # 2.3: brake-point repeatability (works on old reviews from corners[].per_lap)
    bs = _brake_spread(data)
    spread = [c for c in (bs or {}).get("corners") or [] if isinstance(c, dict) and c.get("id")
              and _f(c.get("iqr_m")) is not None and isinstance(c.get("n"), int)]
    locked_b = [c["id"] for c in spread if c.get("label") == "locked"]

    def corner_cost(ids):
        """Seconds a lap that the named corners lose (typical against quicker laps): only corners that are in
        Fix first, because that is where it was measured. (sum or None, [ids measured])"""
        got = [(cid, _f(in_fix[cid].get("gain"))) for cid in ids if cid in in_fix and _f(in_fix[cid].get("gain"))]
        return (sum(g for _, g in got), [cid for cid, _ in got]) if got else (None, [])

    if vary:
        vary.sort(key=lambda x: -x[1])
        top = [cid for cid, _ in vary[:3]]
        same_b = [cid for cid in top if cid in locked_b]
        per = [f"{cid} (about {iqr / 2:.1f} s)" for cid, iqr in vary[:3]]
        if same_b and len(same_b) == len(top):
            tr = (f"Try: next race, keep your brake marker in {_and(top)} and pick one turn-in point and one place to get back "
                  "on the throttle, and repeat them.")
        elif same_b:
            tr = (f"Try: next race, in {_and(same_b)} keep your brake marker and fix the turn-in and throttle points; in "
                  f"{_and([c for c in top if c not in same_b])} pick one braking marker and one turn-in point as well.")
        else:
            tr = f"Try: next race, pick one braking marker and one turn-in point in {_and(top)} and repeat them."
        gain, got = corner_cost(top)
        add("bad", "vary", "Corners where your time changes lap to lap",
            f"Your time through {_and(per)} changes from lap to lap.",
            (f"{_and(got)} lose{'s' if len(got) == 1 else ''} about {_s1(gain)} a lap on a typical lap against your quicker laps."
             if gain is not None else None), tr,
            [f"{cid}: most of your clean laps spread over {iqr:.2f} s ({iqr / 2:.2f} s either side of the middle)." for cid, iqr in vary[:3]]
            + ["This is the time through the whole corner, not the brake point."],
            badge=b_lap(gain) if gain is not None else None, corners=top, scope="corner")
    off_many = bool(isinstance(con.get("off_pace_n"), int) and isinstance(con.get("n"), int)
                    and con["off_pace_n"] * 5 > con["n"])   # over 20% of laps off-pace: not "repeatable"
    if locked and len(locked) >= max(2, len(corners) // 2) and not off_many:
        add("good", "locked", "Repeatable corners",
            f"Your time through {_and(locked)} stays within about {max(0.1, VARY_GOOD * k):.1f} s from lap to lap.",
            numbers=[f"Most of your clean laps were within {VARY_GOOD * k:.2f} s of each other in {len(locked)} corner(s)."],
            corners=locked, scope="corner")

    loose = sorted([c for c in spread if c.get("label") in SPREAD_BAD_LABELS and c["n"] >= SPREAD_MIN_N],
                   key=lambda c: -c["iqr_m"])[:3]
    if loose:
        ids = [c["id"] for c in loose]
        gain, got = corner_cost(ids)
        add("bad", "brake_spread", "Brake point moves around", f"Your brake point moves from lap to lap in {_and(ids)}.",
            (f"{_and(got)} lose{'s' if len(got) == 1 else ''} about {_s1(gain)} a lap on a typical lap against your quicker laps."
             if gain is not None else None),
            f"Try: next race, pick one braking marker (a sign, a bridge, a distance board) in {_and(ids)} and brake at it every lap.",
            ["Most of your clean laps' brake points spanned " + ", ".join(f"{c['iqr_m']:.0f} m in {c['id']}" for c in loose) + "."],
            badge=b_lap(gain) if gain is not None else None, corners=ids, scope="corner")
    if len(spread) >= 2 and len(locked_b) * 2 >= len(spread):
        still = [cid for cid, _ in vary if cid in locked_b]
        add("good", "brake_markers", "You brake at the same point every lap",
            f"You braked at the same point lap after lap in {_and(locked_b)}."
            + (f" The time still changes after the braking point in {_and(still)}." if still else ""),
            numbers=[f"Most of your clean laps' brake points were within {5} m in {len(locked_b)} of {len(spread)} braking corners."],
            corners=locked_b, scope="corner")

    # 2.3: which part of the corner the typical-vs-better gap sits in (skills, when it could score)
    sk = data.get("skills") if isinstance(data.get("skills"), dict) else None
    if sk and not sk.get("reason") and isinstance(sk.get("phases"), dict):
        ov = sk.get("overall") if isinstance(sk.get("overall"), dict) else {}
        gap = _f(ov.get("loss_s")) if ov.get("basis") == "zone_gap" else None
        cands = [(p, v) for p, v in sk["phases"].items() if p in PHASE_WORDS and isinstance(v, dict)
                 and _f(v.get("loss_pm")) is not None and _f(v.get("loss_s")) is not None
                 and not (isinstance(v.get("corners"), int) and v["corners"] <= 0)]
        tot = sum(max(0.0, v["loss_s"]) for _, v in cands)
        if cands and tot > 0:
            p, v = max(cands, key=lambda pv: pv[1]["loss_pm"])
            share = v["loss_s"] / tot
            if v["loss_pm"] >= PHASE_MIN_PM and share >= PHASE_MIN_SHARE:
                by_c = sorted([(c["id"], _f((c.get("loss") or {}).get(p))) for c in sk.get("corners") or []
                               if isinstance(c, dict) and c.get("id") and isinstance(c.get("loss"), dict)
                               and (_f(c["loss"].get(p)) or 0) >= 0.03 * k], key=lambda x: -x[1])[:3]
                pn = [cid for cid, _ in by_c]
                share_gap = v["loss_s"] / gap if gap is not None and gap >= v["loss_s"] and gap > 0 else None
                most = share_gap is not None and share_gap > 0.5
                lead = "Most of the time" if most else "The biggest part of the time"
                text = f"{lead} you lose to your quicker laps in the corners is {PHASE_WORDS[p]}" + \
                       (f", mostly in {_and(pn)}." if pn else ".")
                how = ("nearly all" if share_gap >= 0.85 else "most" if share_gap > 0.55 else "about half" if share_gap >= 0.45
                       else "about a third" if share_gap >= 0.28 else "a smaller part") if share_gap is not None else None
                costs = (f"About {_s1(v['loss_s'])} of the {_s1(gap)} a lap your typical laps lose to your quicker laps in the corners "
                         f"({how})." if share_gap is not None else
                         f"About {_s1(v['loss_s'])} a lap, against your quicker laps in the corners.")
                where = _and(pn) if pn else _and([f["corner"] for f in out["fix"][:2]]) or "your slowest corners"
                tries = {"braking": f"brake at the same marker and ease off the brake the same way every lap in {where}",
                         "entry": f"pick one turn-in point in {where} and use it every lap",
                         "mid": f"hold one line and the same speed at the slowest point in {where}",
                         "exit": f"get back on the throttle at the same point every lap out of {where}, a touch earlier if the car allows"}
                nums = [f"This part is {v['loss_s']:.2f} s a lap against your quicker laps (the fastest quarter of clean laps)."]
                if gap is not None:
                    nums.append(f"Through the corners your typical lap was {gap:.2f} s slower than your quicker laps"
                                + (f"; this part is {share_gap * 100:.0f}% of that." if share_gap is not None else "."))
                else:
                    nums.append(f"That is {share * 100:.0f}% of the {tot:.2f} s a lap the four parts lose added up corner by corner.")
                nums += [f"{cid}: {x:.2f} s a lap." for cid, x in by_c]
                add("bad", "phase", "Where in the corner", text, costs, f"Try: next race, {tries[p]}.", nums,
                    cost=round(v["loss_s"], 2), badge=b_lap(v["loss_s"]), corners=pn, scope="corner")

    score = _f(con.get("score"))
    off_n = con.get("off_pace_n") if isinstance(con.get("off_pace_n"), int) else None
    cn = con.get("n") if isinstance(con.get("n"), int) else None
    off_txt = f"{off_n} of {cn} laps were more than {cut:.1f} s off your typical lap." if off_n and cn else None
    within_s = _f(con.get("within_s")) or 0.5   # 2.7: the window scales with lap length (analysis_plus)
    std = _f(con.get("std")) or 0
    cl_t = [_f(l.get("t")) for l in laps if l.get("clean") and _f(l.get("t")) is not None]
    near_typ = (100.0 * sum(1 for t in cl_t if abs(t - median) <= within_s) / len(cl_t)) if cl_t and median else None
    if score is not None:
        if score >= 70 and not (off_n and cn and off_n * 5 > cn):
            add("good", "consistent", "Consistent laps",
                f"Consistency {score:.0f}/100: your clean laps stayed within about {std:.1f} s of your typical lap.",
                numbers=[f"Your clean laps typically sit {std:.2f} s from your typical lap"
                         + (f"; {near_typ:.0f}% of them were within {within_s:.1f} s of it." if near_typ is not None else "."),
                         f"{_f(con.get('within_05')) or 0:.0f}% of {cn or 'your'} clean laps were within {within_s:.1f} s of your best lap.",
                         off_txt])
        elif score < 45:
            add("bad", "scattered", "Lap times all over the place", f"Consistency {score:.0f}/100: your clean laps are all over the place.",
                "Until the laps are steadier it is hard to tell what any other change does.",
                f"Try: next race, aim for five laps in a row within {within_s:.1f} s of each other before chasing pace.",
                [f"Your clean laps typically sit {std:.2f} s from your typical lap"
                 + (f"; {near_typ:.0f}% of them were within {within_s:.1f} s of it." if near_typ is not None else "."),
                 f"{_f(con.get('within_05')) or 0:.0f}% of laps were within {within_s:.1f} s of your best lap.", off_txt],
                laps=con.get("off_pace") or None)

    style = data.get("style") if isinstance(data.get("style"), dict) else {}
    bn = best.get("n")
    if style and bn is not None and isinstance(style.get(str(bn)), dict):
        clean_ns = [str(l["n"]) for l in laps if l.get("clean") and isinstance(style.get(str(l.get("n"))), dict)]

        def med(key):
            vals = [_f(style[n].get(key)) for n in clean_ns]
            vals = [v for v in vals if v is not None]
            return st.median(vals) if vals else None
        b_st = style[str(bn)]

        def luck(key):
            ts = [_f(l.get("t")) for l in laps if l.get("clean") and isinstance(style.get(str(l.get("n"))), dict)]
            vs = [_f(style[str(l.get("n"))].get(key)) for l in laps if l.get("clean") and isinstance(style.get(str(l.get("n"))), dict)]
            ok = [(t, v) for t, v in zip(ts, vs) if t is not None and v is not None]
            return luck_p([t for t, _v in ok], [v for _t, v in ok])

        def luck_words(p, what, amt):
            return (f"Luck check: your best lap {what} {amt:.1f} s less than your typical lap; with your {len(clean_ns)} clean laps "
                    f"mixed up at random, your quick laps look this good only about {p * 100:.1f}% of the time, so this is unlikely to be luck.")
        mc, bc = med("coast_s"), _f(b_st.get("coast_s"))
        pc = luck("coast_s") if mc is not None and bc is not None and mc - bc >= COAST_BAD * k else None
        if pc is not None and pc <= LUCK_ALPHA:
            cc = [f["corner"] for f in out["fix"] if any("coasting" in t for t in f.get("tips") or [])]
            add("bad", "coast", "Coasting", f"On a typical lap you spend {mc - bc:.1f} s more off both pedals than on your best lap.",
                "On your quicker laps you tended to go from brake to throttle with less of a gap.",
                f"Try: next race, go straight from the brake to the throttle" + (f" in {_and(cc)}." if cc else "; open lap "
                f"{bn} in Compare laps to see where it did."),
                [f"Off both pedals (both under 5%, anywhere on the lap, lifts included): {mc:.1f} s on a typical clean lap, "
                 f"{bc:.1f} s on lap {bn}. Trail braking is a different thing: brake still on as you turn in. Coasting starts "
                 "once the brake is fully off.", luck_words(pc, "coasted", mc - bc)],
                cost=round(mc - bc, 1), badge=b_lap(mc - bc, " off the pedals"), laps=[bn], corners=cc)
        # 2.18: both pedals at once can be deliberate: it is only a habit when your typical laps do more of it
        # than your best lap, i.e. the laps show it costing time
        mo, bo = med("overlap_s"), _f(b_st.get("overlap_s"))
        po = luck("overlap_s") if mo is not None and bo is not None and mo - bo >= OVERLAP_BAD * k else None
        if po is not None and po <= LUCK_ALPHA:
            add("bad", "overlap", "Brake and throttle together",
                f"On a typical lap you have both pedals pressed for {mo - bo:.1f} s more than on your best lap.",
                "Some overlap can be deliberate, but your quicker lap did less of it.",
                f"Try: next race, finish braking before you press the throttle; open lap {bn} in Compare laps to see how it did it.",
                [f"Both pedals above 10%: {mo:.1f} s on a typical clean lap, {bo:.1f} s on lap {bn}.", luck_words(po, "had both pedals down for", mo - bo)],
                cost=round(mo - bo, 1), badge=b_lap(mo - bo, " with both pedals down"), laps=[bn])

    # ---------- the corner-by-corner strip: braking / mid-corner / exit for each turn, from data already worked out
    out["strip"] = _strip(data, corners, spread, con, sk, k, {cid: f.get("gain") for cid, f in in_fix.items() if _f(f.get("gain"))})

    # ---------- order: biggest cost first (per-lap costs times the clean laps, so they compare with race totals);
    # a safety card goes to the top when a quarter of the laps were flagged, otherwise right after the first time card
    def race_cost(it):
        v = (it.get("badge") or {}).get("race_s")
        return v if isinstance(v, (int, float)) else -1.0
    for bucket in ("bad", "look"):
        out[bucket].sort(key=lambda it: -race_cost(it))
    safety = [it for it in out["bad"] if (it.get("badge") or {}).get("kind") == "safety"]
    if safety:
        rest = [it for it in out["bad"] if it not in safety]
        at = 0 if flagged_share >= 0.25 or not rest or race_cost(rest[0]) < 0 else 1
        out["bad"] = rest[:at] + safety + rest[at:]

    # ---------- verdict: one sentence in words (the numbers are on the tiles right above it)
    out["verdict"].append(_verdict_sentence(out["fix"], con, data.get("skills")))
    out["time_to_find"] = round(sum(x["gain"] for x in out["fix"]), 2)
    return out
