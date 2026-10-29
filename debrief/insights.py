"""Turn a review's numbers into a short coaching summary, sorted the way a driver reads it:

    verdict      2 lines: best lap, how consistent, how much time is realistically there
    fix          up to 3 corners worth working on now: what to change and roughly what it's worth
    bad          habits that repeat across laps (only listed when they clear a threshold)
    good         what you already do well

Uses only numbers the analysis already worked out (engine + analysis_plus), so old reviews can be
summarised too. Every item has plain words; numbers are rounded to what a driver can act on.
"""
from __future__ import annotations

import statistics as st

MIN_GAIN = 0.05          # s/lap: below this a corner isn't worth a "fix" item
VARY_BAD = 0.15          # s: corner time spread (IQR) that counts as "varies a lot"
VARY_GOOD = 0.06         # s: spread that counts as "locked in"
FADE_BAD = 0.3           # s: clean laps slower by this much from first to last
COAST_BAD = 0.5          # s/lap: extra coasting on typical laps vs your best lap
OVERLAP_BAD = 0.3        # s/lap: both pedals at once
LAP1_BAD = 1.0           # s: lap 1 slower than the median


def _f(v):
    return float(v) if isinstance(v, (int, float)) else None


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
    """Concrete differences between your typical lap and your best lap (or a comparison lap) in a corner."""
    if not tech:
        return []
    typ = tech.get("typical") or {}
    tgt = tech.get("ref") if against == "ref" else tech.get("best")
    if not tgt:
        return []
    who = "the comparison lap" if against == "ref" else "your best lap"
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


def build(data: dict) -> dict:
    corners = [c for c in data.get("corners") or [] if isinstance(c, dict) and c.get("id")]
    tech = {t["id"]: t for t in data.get("technique") or [] if isinstance(t, dict) and t.get("id")}
    laps = data.get("laps") or []
    counts = data.get("counts") or {}
    best = data.get("best") or {}
    median = _f(data.get("median"))
    con = data.get("consistency") or {}
    refs = data.get("refs") or []
    main_ref = next((r for r in refs if r.get("main")), refs[0] if refs else None)
    out = {"version": 1, "verdict": [], "fix": [], "bad": [], "good": []}

    # ---------- fix immediately: the corners with the most time to find
    ranked = []
    for c in corners:
        own = _f(c.get("avail")) or 0.0
        ref = _f(c.get("ref_gap"))
        ref = ref if (ref is not None and ref > 0 and main_ref) else 0.0
        gain = max(own, ref)
        if gain < MIN_GAIN:
            continue
        basis = "ref" if ref > own else "own"
        t = tech.get(c["id"])
        tips = corner_tips(t, "ref" if basis == "ref" and t and t.get("ref") else "best")
        if basis == "own":
            why = f"Your typical lap loses {own:.2f} s here compared with your better laps."
        else:
            why = f"{(main_ref or {}).get('name', 'The comparison driver').split(' ')[0]} is {ref:.2f} s quicker here on a good lap."
        ranked.append({"corner": c["id"], "gain": round(gain, 2), "basis": basis, "apex_m": c.get("apex_m"),
                       "start_m": c.get("start_m"), "end_m": c.get("end_m"), "best_lap": c.get("best_lap"),
                       "tips": tips[:3], "why": why,
                       "gear": (c.get("summary") or {}).get("gear"), "min_mph": (c.get("summary") or {}).get("min_mph")})
    ranked.sort(key=lambda x: -x["gain"])
    out["tips"] = {cid: corner_tips(t, "best")[:3] for cid, t in tech.items()}
    out["fix"] = ranked[:3]
    out["more_corners"] = len(ranked) - len(out["fix"])

    # ---------- bad habits (only when they clear a threshold)
    def bad(key, title, text, cost=None):
        out["bad"].append({"key": key, "title": title, "text": text, "cost": cost})

    def good(key, title, text):
        out["good"].append({"key": key, "title": title, "text": text})

    unclean = list(counts.get("unclean") or [])
    if len(unclean) >= 2:
        bad("unclean", "Incidents or track limits",
            f"Garage 61 flagged {_laps(unclean)}. Each one is a lap you can't use, and a safety-rating risk.")
    elif not unclean and len(laps) >= 5:
        good("clean", "Clean race", "No laps flagged for incidents or track limits.")

    lap1 = next((l for l in laps if l.get("n") == 1), None)
    if lap1 and median and _f(lap1.get("t")):
        d = lap1["t"] - median
        if d >= LAP1_BAD:
            bad("lap1", "Lap 1", f"Lap 1 was {d:.1f} s slower than your typical lap. Some of that is the standing or "
                "rolling start and traffic; the rest is time lost to caution or a moment.", round(d, 1))
        elif d <= 0.5 and len(laps) >= 5:
            good("lap1", "Strong first lap", f"Lap 1 was within {max(d, 0):.1f} s of your typical pace.")

    slow = list(counts.get("slow") or [])
    slow = [n for n in slow if n != 1]
    if len(slow) >= 2:
        bad("offpace", "Off-pace laps", f"{_laps(slow).capitalize()} were more than a second off your typical lap without "
            "an incident flag: likely mistakes or traffic. Open them in Compare to see where.")

    traffic = list(counts.get("traffic") or [])
    if traffic:
        bad("traffic", "Traffic (multi-class)", f"{_laps(traffic).capitalize()} lost time in one short stretch, most likely "
            "to traffic. They're left out of your pace numbers; plan where to let faster cars by.")

    trend = _f(data.get("trend_total"))
    n_clean = (counts.get("clean") or 0) if isinstance(counts.get("clean"), int) else 0
    if trend is not None and n_clean >= 8:
        if trend >= FADE_BAD:
            bad("fade", "Fading late in the race",
                f"Your clean laps got about {trend:.1f} s slower from start to finish. Tyres and fuel play a part; "
                "check the corners that slowed most under Stint.", round(trend, 1))
        elif abs(trend) < 0.15:
            good("steady", "Held pace all race", "Your clean laps held steady from start to finish.")

    vary = []
    locked = []
    for b in con.get("by_corner") or []:
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
            ". Pick one braking marker and one turn-in point there and repeat them.")
    if locked and len(locked) >= max(2, len(corners) // 2):
        good("locked", "Repeatable corners", "You hit the same time lap after lap in " +
             (", ".join(locked) if len(locked) < len(corners) else "every corner") + ".")

    score = _f(con.get("score"))
    if score is not None:
        if score >= 70:
            good("consistent", "Consistent laps", f"Consistency {score:.0f}/100: your clean laps stayed within "
                 f"about ±{(_f(con.get('std')) or 0):.2f} s.")
        elif score < 45:
            bad("scattered", "Lap times all over the place", f"Consistency {score:.0f}/100. Before chasing pace, aim to "
                "put five laps in a row within half a second of each other.")

    style = data.get("style") or {}
    bn = best.get("n")
    if style and bn is not None and str(bn) in style:
        clean_ns = [str(l["n"]) for l in laps if l.get("clean") and str(l.get("n")) in style]

        def med(k):
            vals = [_f(style[n].get(k)) for n in clean_ns]
            vals = [v for v in vals if v is not None]
            return st.median(vals) if vals else None
        b_st = style[str(bn)]
        mc, bc = med("coast_s"), _f(b_st.get("coast_s"))
        if mc is not None and bc is not None and mc - bc >= COAST_BAD:
            bad("coast", "Coasting", f"On a typical lap you spend {mc - bc:.1f} s more off both pedals than on your best "
                "lap. Brake later or get back on the throttle sooner.")
        mo = med("overlap_s")
        if mo is not None and mo >= OVERLAP_BAD:
            bad("overlap", "Brake and throttle together", f"About {mo:.1f} s a lap with both pedals pressed. Unless it's "
                "deliberate (trail-braking with throttle), it's scrubbing speed.")

    # ---------- verdict
    bt = _f(best.get("t"))
    if bt is not None:
        line = f"Best lap {fmt_t(bt)}"
        if main_ref and _f(main_ref.get("t")):
            line += f", {bt - main_ref['t']:+.2f} s against {main_ref.get('name', 'the comparison lap').split(' ')[0]}"
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
    out["time_to_find"] = round(sum(x["gain"] for x in out["fix"]), 2)
    return out
