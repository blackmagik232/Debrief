"""Turn a review's numbers into a short coaching summary, sorted the way a driver reads it:

    verdict      2 lines: best lap, how consistent, how much time is realistically there
    fix          up to 5 corners worth working on now: what to change, roughly what it's worth, and the
                 numbers behind it ("numbers": best / typical / comparison technique, "times": corner times)
    bad          habits that repeat across laps (only listed when they clear a threshold)
    good         what you already do well

Every bad/good item is {key, title, text, cost} plus, when known, "detail" (one plain sentence with the
measurement behind it), "laps" [int] and "corners" [str]. Every fix item adds "numbers"
{"best", "typical", "ref"} (each {brake_on, min_mph, exit_mph, full_thr, coast_s, gear} or None) and
"times" {"best_t", "typical_t", "p25_t", "ref_t"}. Readers must treat all of these as optional.

Thresholds in seconds are for a ~90 s lap and scale with your median lap time (a 30 s short oval and an
8-minute Nordschleife lap are judged alike). Wording stays correlational: "on your quicker laps you
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
OVERLAP_BAD = 0.3        # s/lap: both pedals at once
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
    """Threshold multiplier for this track: median lap / 90 s (clamped to 0.33..4)."""
    m = _f(median)
    return min(4.0, max(0.33, m / REF_LAP)) if m else 1.0


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


def build(data: dict) -> dict:
    corners = [c for c in data.get("corners") or [] if isinstance(c, dict) and c.get("id")]
    tech = {t["id"]: t for t in data.get("technique") or [] if isinstance(t, dict) and t.get("id")}
    laps = [l for l in data.get("laps") or [] if isinstance(l, dict)]
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    best = data.get("best") if isinstance(data.get("best"), dict) else {}
    median = _f(data.get("median"))
    con = data.get("consistency") if isinstance(data.get("consistency"), dict) else {}
    refs = [r for r in data.get("refs") or [] if isinstance(r, dict)]
    main_ref = next((r for r in refs if r.get("main")), refs[0] if refs else None)
    k = _scale(median)
    min_gain = MIN_GAIN * k
    clean_set = {str(l.get("n")) for l in laps if l.get("clean")}
    out = {"version": 2, "verdict": [], "fix": [], "bad": [], "good": []}

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
            why = f"Your typical lap loses {own:.2f} s here compared with your better laps."
            detail = (f"Typical {typ_t:.2f} s through here; your quicker quarter of laps {p25:.2f} s."
                      if typ_t is not None and p25 is not None else None)
        else:
            why = f"{who} is {ref:.2f} s quicker here on a good lap."
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
    ranked.sort(key=lambda x: -x["gain"])
    by_id = {c["id"]: c for c in corners}
    out["tips"] = {cid: corner_tips(_with_quick(t, by_id.get(cid), clean_set), "best")[:3] for cid, t in tech.items()}
    out["fix"] = ranked[:MAX_FIX]
    out["more_corners"] = len(ranked) - len(out["fix"])

    # ---------- bad habits (only when they clear a threshold)
    def bad(key, title, text, cost=None, detail=None, laps=None, corners=None):
        it = {"key": key, "title": title, "text": text, "cost": cost}
        if detail:
            it["detail"] = detail
        if laps:
            it["laps"] = _lap_list(laps)
        if corners:
            it["corners"] = [str(x) for x in corners]
        out["bad"].append(it)

    def good(key, title, text, detail=None, laps=None, corners=None):
        it = {"key": key, "title": title, "text": text, "cost": None}
        if detail:
            it["detail"] = detail
        if laps:
            it["laps"] = _lap_list(laps)
        if corners:
            it["corners"] = [str(x) for x in corners]
        out["good"].append(it)

    n_laps = counts.get("analysed") if isinstance(counts.get("analysed"), int) else len(laps)
    n_laps += counts.get("excluded") if isinstance(counts.get("excluded"), int) else 0
    unclean = _lap_list(counts.get("unclean"))
    pit = _lap_list(counts.get("pit"))
    # old reviews (no excluded_laps) dropped spin laps before counting incidents, so "clean race" can't be trusted
    hidden_spins = bool(counts.get("excluded")) and "excluded_laps" not in counts
    if len(unclean) >= 2:
        bad("unclean", "Incidents or track limits",
            f"Garage 61 flagged {_laps(unclean)}. Each one is a lap you can't use, and a safety-rating risk.",
            detail=f"{len(unclean)} of {n_laps + len(pit)} full laps flagged.", laps=unclean)
    elif not unclean and len(laps) >= 5 and not hidden_spins:
        good("clean", "Clean race", "No laps flagged for incidents or track limits.",
             detail=f"0 of {n_laps + len(pit)} full laps flagged by Garage 61.")

    lap1 = next((l for l in laps if l.get("n") == 1), None)
    if lap1 and median and _f(lap1.get("t")) and 1 not in pit:
        d = lap1["t"] - median
        if d >= LAP1_BAD * k:
            bad("lap1", "Lap 1", f"Lap 1 was {d:.1f} s slower than your typical lap. Some of that is the standing or "
                "rolling start and traffic; the rest is time lost to caution or a moment.", round(d, 1),
                detail=f"Lap 1 {fmt_t(lap1['t'])} against a typical {fmt_t(median)}.", laps=[1])
        elif d <= 0.5 * k and len(laps) >= 5:
            good("lap1", "Strong first lap", f"Lap 1 was within {max(d, 0):.1f} s of your typical pace.",
                 detail=f"Lap 1 {fmt_t(lap1['t'])} against a typical {fmt_t(median)}.", laps=[1])

    slow = [n for n in _lap_list(counts.get("slow")) if n != 1 and n not in pit]
    cut = _f(data.get("slow_cut")) or 1.0
    if len(slow) >= 2:
        lap_t = {l.get("n"): _f(l.get("t")) for l in laps}
        lost = [lap_t.get(n) - median for n in slow if lap_t.get(n) is not None and median]
        bad("offpace", "Off-pace laps", f"{_laps(slow).capitalize()} were more than {cut:.1f} s off your typical lap without "
            "an incident flag: likely a mistake or traffic. Open them in Compare to see where.",
            round(sum(lost), 1) if lost else None,
            detail=(f"Together they were {sum(lost):.1f} s slower than {len(lost)} typical laps." if lost else None),
            laps=slow)

    traffic = _lap_list(counts.get("traffic"))
    if traffic:
        td = [t for t in counts.get("traffic_detail") or [] if isinstance(t, dict)]
        bad("traffic", "Traffic (multi-class)", f"{_laps(traffic).capitalize()} lost time in one short stretch, most likely "
            "to traffic. They're left out of your pace numbers; plan where to let faster cars by.",
            detail=("; ".join(f"lap {t.get('n')}: ~{_f(t.get('lost')) or 0:.2f} s around {_f(t.get('at_m')) or 0:.0f} m"
                              for t in td[:4]) + ".") if td else None, laps=traffic)

    trend = _f(data.get("trend_total"))
    n_clean = (counts.get("clean") or 0) if isinstance(counts.get("clean"), int) else 0
    if trend is not None and n_clean >= 8:
        if trend >= FADE_BAD * k:
            bad("fade", "Fading late in the race",
                f"Your clean laps got about {trend:.1f} s slower from start to finish. Tyres and fuel play a part; "
                "check the corners that slowed most under Stint.", round(trend, 1),
                detail=f"Trend line over {n_clean} clean laps: +{trend:.2f} s from first to last.")
        elif abs(trend) < 0.15 * k:
            good("steady", "Held pace all race", "Your clean laps held steady from start to finish.",
                 detail=f"Trend line over {n_clean} clean laps: {trend:+.2f} s from first to last.")

    vary = []
    locked = []
    for b in con.get("by_corner") or []:
        if not isinstance(b, dict):
            continue
        iqr = _f(b.get("iqr"))
        if iqr is None:
            continue
        if iqr >= VARY_BAD:
            vary.append((b["id"], iqr))
        elif iqr <= VARY_GOOD:
            locked.append(b["id"])
    if vary:
        vary.sort(key=lambda x: -x[1])
        bad("vary", "Corners you drive differently every lap",
            " and ".join(f"{cid} (±{iqr / 2:.2f} s)" for cid, iqr in vary[:3]) +
            ". Pick one braking marker and one turn-in point there and repeat them.",
            detail="Middle half of your clean laps through " + ", ".join(f"{cid} spread over {iqr:.2f} s" for cid, iqr in vary[:3]) + ".",
            corners=[cid for cid, _ in vary[:3]])
    off_many = bool(isinstance(con.get("off_pace_n"), int) and isinstance(con.get("n"), int)
                    and con["off_pace_n"] * 5 > con["n"])   # over 20% of laps off-pace: not "repeatable"
    if locked and len(locked) >= max(2, len(corners) // 2) and not off_many:
        good("locked", "Repeatable corners", "You hit the same time lap after lap in " +
             (", ".join(locked) if len(locked) < len(corners) else "every corner") + ".",
             detail=f"Middle half of your clean laps within {VARY_GOOD:.2f} s in {len(locked)} corner(s).", corners=locked)

    # 2.3: brake-point repeatability (works on old reviews from corners[].per_lap)
    bs = _brake_spread(data)
    spread = [c for c in (bs or {}).get("corners") or [] if isinstance(c, dict) and c.get("id")
              and _f(c.get("iqr_m")) is not None and isinstance(c.get("n"), int)]
    loose = sorted([c for c in spread if c.get("label") in SPREAD_BAD_LABELS and c["n"] >= SPREAD_MIN_N],
                   key=lambda c: -c["iqr_m"])[:3]
    if loose:
        first = loose[0]
        names = [f"{first['id']} (about {first['iqr_m']:.0f} m between laps)"] + [c["id"] for c in loose[1:]]
        bad("brake_spread", "Brake point moves around",
            f"Your brake point moved around in {_and(names)}.",
            detail="Middle half of your clean laps' brake points spanned " +
                   ", ".join(f"{c['iqr_m']:.0f} m in {c['id']}" for c in loose) + ".",
            corners=[c["id"] for c in loose])
    locked_b = [c["id"] for c in spread if c.get("label") == "locked"]
    if len(spread) >= 2 and len(locked_b) * 2 >= len(spread):
        good("brake_markers", "Same brake marker every lap",
             f"You braked at the same marker lap after lap in {_and(locked_b)}.",
             detail=f"Middle half of your clean laps' brake points within {5} m in {len(locked_b)} of {len(spread)} braking corners.",
             corners=locked_b)

    # 2.3: which part of the corner the typical-vs-better gap sits in (skills, when it could score)
    sk = data.get("skills") if isinstance(data.get("skills"), dict) else None
    if sk and not sk.get("reason") and isinstance(sk.get("phases"), dict):
        tot = _f((sk.get("overall") or {}).get("loss_s")) if isinstance(sk.get("overall"), dict) else None
        cands = [(p, v) for p, v in sk["phases"].items() if p in PHASE_WORDS and isinstance(v, dict)
                 and _f(v.get("loss_pm")) is not None and _f(v.get("loss_s")) is not None]
        if cands and tot and tot > 0:
            p, v = max(cands, key=lambda pv: pv[1]["loss_pm"])
            if v["loss_pm"] >= PHASE_MIN_PM and v["loss_s"] >= PHASE_MIN_SHARE * tot:
                bad("phase", "Where in the corner",
                    f"Most of the gap between your typical and better laps was {PHASE_WORDS[p]} "
                    f"(about {v['loss_s']:.2f} s a lap).", round(v["loss_s"], 2),
                    detail=(f"{v['loss_s']:.2f} s of the {tot:.2f} s a lap between your typical lap (median of clean laps) "
                            "and your better laps (your quickest quarter of clean laps)."))

    score = _f(con.get("score"))
    off_n = con.get("off_pace_n") if isinstance(con.get("off_pace_n"), int) else None
    cn = con.get("n") if isinstance(con.get("n"), int) else None
    off_txt = f" {off_n} of {cn} laps were more than {cut:.1f} s off your typical lap." if off_n and cn else ""
    if score is not None:
        if score >= 70 and not (off_n and cn and off_n * 5 > cn):
            good("consistent", "Consistent laps", f"Consistency {score:.0f}/100: your clean laps stayed within "
                 f"about ±{(_f(con.get('std')) or 0):.2f} s.",
                 detail=(f"{_f(con.get('within_05')) or 0:.0f}% of {cn or 'your'} clean laps within 0.5 s of your best." + off_txt))
        elif score < 45:
            bad("scattered", "Lap times all over the place", f"Consistency {score:.0f}/100. Before chasing pace, aim to "
                "put five laps in a row within half a second of each other.",
                detail=(f"Spread ±{(_f(con.get('std')) or 0):.2f} s; {_f(con.get('within_05')) or 0:.0f}% of laps "
                        "within 0.5 s of your best." + off_txt), laps=con.get("off_pace") or None)

    style = data.get("style") if isinstance(data.get("style"), dict) else {}
    bn = best.get("n")
    if style and bn is not None and isinstance(style.get(str(bn)), dict):
        clean_ns = [str(l["n"]) for l in laps if l.get("clean") and isinstance(style.get(str(l.get("n"))), dict)]

        def med(key):
            vals = [_f(style[n].get(key)) for n in clean_ns]
            vals = [v for v in vals if v is not None]
            return st.median(vals) if vals else None
        b_st = style[str(bn)]
        mc, bc = med("coast_s"), _f(b_st.get("coast_s"))
        if mc is not None and bc is not None and mc - bc >= COAST_BAD * k:
            bad("coast", "Coasting", f"On a typical lap you spend {mc - bc:.1f} s more off both pedals than on your best "
                "lap. On your quicker laps you tended to go from brake to throttle with less of a gap.",
                round(mc - bc, 1), detail=f"Off both pedals: {mc:.1f} s on a typical clean lap, {bc:.1f} s on lap {bn}.",
                laps=[bn])
        mo = med("overlap_s")
        if mo is not None and mo >= OVERLAP_BAD * k:
            bad("overlap", "Brake and throttle together", f"About {mo:.1f} s a lap with both pedals pressed. Unless it's "
                "deliberate (trail-braking with throttle), it's time the car spends slowing and accelerating at once.",
                detail=f"Both pedals above 10%: {mo:.1f} s on a typical clean lap.")

    # ---------- verdict
    bt = _f(best.get("t"))
    if bt is not None:
        line = f"Best lap {fmt_t(bt)}"
        if main_ref and _f(main_ref.get("t")):
            line += f", {bt - main_ref['t']:+.2f} s against {(main_ref.get('name') or 'the comparison lap').split(' ')[0]}"
        label = con.get("label")
        if label:
            line += f". {label}" + (f" ({score:.0f}/100)" if score is not None else "")
        out["verdict"].append(line + ".")
    if out["fix"]:
        tot = sum(x["gain"] for x in out["fix"])
        out["verdict"].append(f"About {tot:.2f} s a lap is there, mostly in " +
                              " and ".join(x["corner"] for x in out["fix"][:2]) + ".")
    else:
        out["verdict"].append("No corner stands out: your typical laps are already close to your best. "
                              "More pace will come from new references (follow quicker drivers on Garage 61).")
    if pit:
        out["verdict"].append(f"Pit stop on {_laps(pit)}: left out of your pace numbers.")
    out["time_to_find"] = round(sum(x["gain"] for x in out["fix"]), 2)
    return out
