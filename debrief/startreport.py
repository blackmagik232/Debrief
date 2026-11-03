"""Start report (2.16 "Racing"): what happens to you from the green flag to the end of lap 1, race by race and across
your races.

Per race
  - Start position: the official results (the results CSV saved with the review, in class) when there are any, else
    RaceCap's results file, else iRacing's own position at the green from your telemetry or the replay scan; the
    scan's worked-out grid is used last and labelled as a guess. Which one was used is always said.
  - Position at the end of lap 1: iRacing's position a few seconds after the line (it settles about SETTLE_S after
    it), from telemetry or the scan, in class in a multi-class race.
  - Corner by corner on lap 1 (replay scan only): every car of your class in view at two checkpoints (the moment you
    leave each corner) is ahead of you or behind; a car that changed sides between two checkpoints passed you or was
    passed there. Only cars in view of the replay camera can be counted, so these are "places seen changing", and the
    report says when they don't add up to the official change.
  - Incidents on lap 1 and contact: "contact (4x)" only when iRacing counted it, with the closest car named only as
    "closest car: X, N m away" (incidentmap.py); "possibly" when another car was close; not known without telemetry.

Across races (per track and overall): average places gained or lost on lap 1 by grid band (thirds of your class's
field, or P1 to P5 / P6 to P15 / P16 and back when the field size isn't known), which corners the changes happen at,
and the trend over time (only with TREND_MIN races or more). Facts ("in 4 of 5 races you lost places at Turn 1") are
always given with their counts; a PATTERN ("you lose places at Turn 1") needs at least MIN_RACES races showing it and
a sign-flip test across races (every sign flip up to EXACT_MAX races, else PERM_N with a fixed seed, family-wise over
every corner or band tested) below ALPHA, and must go against what the grid alone gives (_gate). A trend isn't called
one when the grid places moved the same way. With 1 or 2 races it only says what happened.

Never raises on bad or missing data; JSON-ready dicts; read only.
"""
from __future__ import annotations

import bisect
import math

from . import incidentmap as im

VERSION = 2                 # 2: careful wording; no habit in the direction the grid alone gives
SETTLE_S = im.SETTLE_S
SWAP_MAX = 0.25             # two cars further apart than this share of the lap can't have swapped places
VIEW_TOL_S = 1.0            # a checkpoint uses the replay reading this close in time
BAND_FIXED = ((1, 5, "front", "P1 to P5"), (6, 15, "middle", "P6 to P15"), (16, 10 ** 6, "back", "P16 and back"))
MIN_FIELD_FOR_THIRDS = 6
GRID_TREND_P = 0.2          # your grid places moving this clearly over time: no lap 1 trend in the same direction
REL_SIDE = 0.0              # races started on average behind (ahead of) the middle of the grid: no "gain" ("lose") habit
MIN_RACES, ALPHA, TREND_MIN = im.MIN_RACES, im.ALPHA, im.TREND_MIN
LINE_KEY = "line"

START_WORDS = {"official": "from the official results", "racecap_results": "from RaceCap's results file",
               "telemetry": "iRacing's position at the green, from your telemetry",
               "replay": "iRacing's position at the green, from the replay",
               "estimate": "a guess from the replay at the green"}
LAP1_WORDS = {"saved": "from your saved result", "telemetry": "iRacing's position after lap 1, from your telemetry",
              "replay": "iRacing's position after lap 1, from the replay"}


def _num(v):
    return im._num(v)


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 else None


def _plural(n, one, many=None):
    return im._plural(n, one, many)


def _places(n) -> str:
    return _plural(abs(int(n)), "place")


# ---------------------------------------------------------------- positions from the replay scan
def class_rank(row_pos, idxs, my_idx, classes) -> tuple:
    """(position, in_class) from one row of iRacing's positions: in class when every car's class is known."""
    if my_idx not in idxs or not row_pos:
        return None, False
    me = idxs.index(my_idx)
    mine = row_pos[me] if me < len(row_pos) else None
    if not isinstance(mine, int) or mine <= 0:
        return None, False
    cls = (classes or {}).get(str(my_idx))
    if not cls:
        return mine, False
    ahead = sum(1 for j, p in enumerate(row_pos) if j != me and isinstance(p, int) and 0 < p < mine
                and (classes or {}).get(str(idxs[j])) == cls)
    return ahead + 1, True


def scan_pos_at(samples, my_idx, t, classes=None, settle=0.0) -> tuple:
    """iRacing's position (in class when known) at the first reading at or after t + settle that has one."""
    rows = im.sample_rows(samples)
    idxs = (samples or {}).get("idxs") or []
    t = _num(t)
    if not rows or t is None or my_idx not in idxs:
        return None, False
    times = [r[0] for r in rows]
    k = bisect.bisect_left(times, t + settle)
    for j in range(k, min(len(rows), k + 20)):
        pos = rows[j][im.POS] if len(rows[j]) > im.POS else None
        p, in_class = class_rank(pos, idxs, my_idx, classes)
        if p:
            return p, in_class
    return None, False


def _rel_at(rows, times, me, j, t):
    """(your lap position, their position relative to you: + ahead, - behind), from the same replay reading near t."""
    k = bisect.bisect_left(times, t)
    for d in range(0, 6):
        for kk in (k - d, k + d - 1) if d else (k,):
            if not 0 <= kk < len(rows) or abs(times[kk] - t) > VIEW_TOL_S:
                continue
            p = rows[kk][im.PCT]
            pit = rows[kk][im.PIT] if len(rows[kk]) > im.PIT else None
            if not p or me >= len(p) or j >= len(p) or not im._valid(p[me]) or not im._valid(p[j]):
                continue
            if pit and j < len(pit) and pit[j]:
                return None
            return im._wrapped(p[j], p[me])
    return None


def lap1_scan(scan, samples, me_car, zs, classes=None) -> dict | None:
    """Lap 1 corner by corner from the replay: places seen changing between checkpoints. None when it can't be read."""
    rows = im.sample_rows(samples)
    idxs = (samples or {}).get("idxs") or []
    idx = (me_car or {}).get("car_idx")
    green = _num((scan or {}).get("green"))
    try:
        end = float(((me_car or {}).get("line_crossings") or {}).get("1"))
    except (TypeError, ValueError):
        end = None
    if not rows or idx not in idxs or green is None or end is None or end <= green:
        return None
    me = idxs.index(idx)
    times = [r[0] for r in rows]
    prog, last = [], None
    for k in range(bisect.bisect_left(times, green), min(len(rows), bisect.bisect_right(times, end + 2))):
        p = rows[k][im.PCT]
        v = p[me] if p and me < len(p) else None
        if not im._valid(v):
            continue
        cur = (v - 1.0 if v > 0.5 else v) if last is None else v + round(last - v)
        prog.append((times[k], cur))
        last = cur
    if len(prog) < 5:
        return None
    start_prog = prog[0][1]

    def time_at(target):
        for (ta, pa), (tb, pb) in zip(prog, prog[1:]):
            if pa < target <= pb:
                return ta + (tb - ta) * (target - pa) / max(pb - pa, 1e-9)
        return None
    cps = []
    for z in zs or []:
        e = z["hi"] % 1.0
        if e <= max(start_prog, 0.0) or e >= 1.0:
            continue
        t = time_at(e)
        if t is not None and green < t < end:
            cps.append((t, z))
    cps.sort(key=lambda x: x[0])
    marks = [(green, None)] + cps + [(end, None)]
    my_cls = (classes or {}).get(str(idx))
    others = [j for j in range(len(idxs)) if j != me and (not my_cls or (classes or {}).get(str(idxs[j])) == my_cls)]
    segs = []
    for i in range(1, len(marks)):
        ta, tb, z = marks[i - 1][0], marks[i][0], marks[i][1]
        if z is not None:
            key, corner = z["key"], z["key"]
            label = f"the start to {z['label']}" if i == 1 else z["label"]
        else:
            key, corner = LINE_KEY, None
            label = "the start to the line" if i == 1 else (f"after {marks[i - 1][1]['label']} to the line" if marks[i - 1][1] else "the run to the line")
        gained = lost = seen = 0
        for j in others:
            a = _rel_at(rows, times, me, j, ta)
            b = _rel_at(rows, times, me, j, tb)
            if a is None or b is None or abs(a) > SWAP_MAX or abs(b) > SWAP_MAX:
                continue
            seen += 1
            if a < 0 < b:
                lost += 1
            elif a > 0 > b:
                gained += 1
        segs.append({"key": key, "corner": corner, "label": label, "from_t": round(float(ta), 2), "to_t": round(float(tb), 2),
                     "gained": gained, "lost": lost, "net": gained - lost, "cars_seen": seen})       # places SEEN changing
    return {"segments": segs, "seen_net": sum(s["net"] for s in segs), "in_class": bool(my_cls),
            "first_corner": cps[0][1]["label"] if cps else None}


# ---------------------------------------------------------------- one race
def race_row(o: dict, zs=None) -> dict:
    """One race's start and lap 1, from what was found for it (incidentmap.outings). Never raises."""
    try:
        return _race_row(o, zs if zs is not None else (o.get("_zones") or im.zones(None, None)))
    except Exception as e:  # noqa: BLE001
        return {"id": o.get("id"), "track": o.get("track"), "ok": False, "why": f"This race couldn't be read ({type(e).__name__})."}


def _race_row(o, zs) -> dict:
    s = o.get("scan") or {}
    scan, samples, me = s.get("scan"), s.get("samples"), o.get("me_car")
    classes = o.get("classes") or {}
    multiclass = bool(o.get("multiclass")) or len({v for v in classes.values() if v}) > 1
    rev = o.get("review") or {}
    res = rev.get("result") if isinstance(rev.get("result"), dict) else {}
    idx = (me or {}).get("car_idx") if me else o.get("me_idx")
    # ---- start
    start, start_src, in_class = None, None, multiclass
    if _int(res.get("start_pos")):
        start, start_src = _int(res["start_pos"]), "official"
    elif s.get("official") and me:
        st = (s["official"].get("start") or {})
        mine = st.get(str(me.get("number")))
        if isinstance(mine, int) and mine > 0:
            cls = classes.get(str(idx))
            if cls:
                nums = {str(c.get("number")): classes.get(str(c.get("car_idx"))) for c in scan.get("cars") or [] if isinstance(c, dict)}
                mine = 1 + sum(1 for n, p in st.items() if isinstance(p, int) and 0 < p < st[str(me.get("number"))] and nums.get(str(n)) == cls)
            start, start_src = mine, "racecap_results"
    if start is None and (o.get("start_class_pos") or o.get("start_pos")):
        start = o.get("start_class_pos") if multiclass and o.get("start_class_pos") else (o.get("start_pos") if not multiclass else None)
        start_src = "telemetry" if start else None
    if start is None and scan and me:
        p, cl = scan_pos_at(samples, idx, scan.get("green"), classes, settle=0.3)
        if p and (cl or not multiclass):
            start, start_src = p, "replay"
    if start is None and me and _int(me.get("grid")) and not multiclass:
        start, start_src = _int(me["grid"]), "estimate"
    # ---- end of lap 1
    lap1, lap1_src = None, None
    if _int(res.get("pos_after_lap1")):
        lap1, lap1_src = _int(res["pos_after_lap1"]), "saved"
    elif o.get("lap1_class_pos") or o.get("lap1_pos"):
        lap1 = o.get("lap1_class_pos") if multiclass else o.get("lap1_pos")
        lap1_src = "telemetry" if lap1 else None
    if lap1 is None and scan and me:
        try:
            t1 = float((me.get("line_crossings") or {}).get("1"))
        except (TypeError, ValueError):
            t1 = None
        p, cl = scan_pos_at(samples, idx, t1, classes, settle=SETTLE_S) if t1 is not None else (None, False)
        if p and (cl or not multiclass):
            lap1, lap1_src = p, "replay"
    change = start - lap1 if start and lap1 else None
    field = _int(res.get("field_size")) or _int(o.get("class_size"))
    if field is None and scan:
        cls = classes.get(str(idx))
        racing = [c for c in scan.get("cars") or [] if isinstance(c, dict) and not c.get("never_on_track")
                  and (not cls or classes.get(str(c.get("car_idx"))) == cls)]
        field = len(racing) or None
    # ---- corners
    corners = lap1_scan(scan, samples, me, zs, classes if multiclass else None) if scan and me else None
    # ---- incidents and contact on lap 1
    evs = [e for e in o.get("_events") or [] if e.get("lap1")]
    pts = sum(e.get("points") or 0 for e in evs)
    contact, contact_why = None, "Not known: there's no telemetry of lap 1 with iRacing's incident count."
    four = [e for e in evs if im.POINTS_CONTACT in (e.get("parts") or [])]
    if four:
        cl = next((e.get("class") or {} for e in four if (e.get("class") or {}).get("closest")), {})
        contact, contact_why = "contact_4x", "iRacing counted contact (4x) on lap 1." + (
            " " + im.closest_words(cl["closest"]) if cl.get("closest") else "")
    elif any((e.get("class") or {}).get("type") in ("contact", "caught_up") for e in evs):
        e = next(e for e in evs if (e.get("class") or {}).get("type") in ("contact", "caught_up"))
        contact, contact_why = "possibly", (e.get("class") or {}).get("why")
    elif o.get("files") and o.get("lap1_end") is not None and o.get("green") is not None:
        contact, contact_why = "no_4x", "iRacing counted no contact (4x) on lap 1. Light contact counts 0x and can't be seen."
    elif scan and me:
        contact, contact_why = None, "Not known: without telemetry only the replay is there, and it shows where cars were, not contact."
    row = {"id": o.get("id"), "ok": True, "track": o.get("track"), "subsession": o.get("subsession"), "when": o.get("when"),
           "review": str(rev.get("dir") or "") or None,
           "multiclass": multiclass, "in_class": multiclass, "field": field,
           "start": start, "start_from": start_src, "start_words": START_WORDS.get(start_src),
           "lap1_pos": lap1, "lap1_from": lap1_src, "lap1_words": LAP1_WORDS.get(lap1_src), "change": change,
           "band": band(start, field), "rel_start": rel_start(start, field), "start_type": (scan or {}).get("start_type"),
           "corners": corners["segments"] if corners else [], "seen_net": corners["seen_net"] if corners else None,
           "incidents": [{"where": e.get("where"), "zone": e.get("zone"), "points": e.get("points"), "parts": e.get("parts") or [],
                          "type": (e.get("class") or {}).get("type"), "sure": (e.get("class") or {}).get("sure"),
                          "closest": im.closest_words((e.get("class") or {}).get("closest")) or None,
                          "why": (e.get("class") or {}).get("why"), "t": e.get("t")} for e in evs],
           "incident_points": pts if o.get("files") else None, "contact": contact, "contact_why": contact_why,
           "sources": [x for x, ok in (("telemetry", bool(o.get("files"))), ("replay scan", bool(scan)), ("results", bool(res))) if ok]}
    row["sentences"] = race_sentences(row)
    return row


def rel_start(start, field) -> float | None:
    """Where in the grid you started, -0.5 (pole) to +0.5 (last): from the field's size, else the fixed bands' middles."""
    start, f = _int(start), _int(field)
    if not start:
        return None
    if f and f > 1 and start <= f:
        return round((start - 1) / (f - 1) - 0.5, 3)
    return -0.3 if start <= 5 else (0.0 if start <= 15 else 0.3)


def band(start, field) -> dict | None:
    """Which part of the grid: thirds of the field when its size is known, else P1 to P5 / P6 to P15 / P16 and back."""
    start = _int(start)
    if not start:
        return None
    f = _int(field)
    if f and f >= MIN_FIELD_FOR_THIRDS and start <= f:
        a, b = math.ceil(f / 3), math.ceil(2 * f / 3)
        key = "front" if start <= a else ("middle" if start <= b else "back")
        return {"key": key, "rule": "thirds", "label": f"{key} third of the grid"}
    for lo, hi, key, label in BAND_FIXED:
        if lo <= start <= hi:
            return {"key": key, "rule": "fixed", "label": label}
    return None


def race_sentences(r) -> list:
    out = []
    cls = " in class" if r["in_class"] else ""
    if r["start"] and r["lap1_pos"]:
        c = r["change"]
        how = "gained " + _places(c) if c > 0 else ("lost " + _places(c) if c < 0 else "kept your place")
        out.append(f"Started P{r['start']}{cls} ({r['start_words']}) and were P{r['lap1_pos']} after lap 1: you {how}.")
    elif r["start"]:
        out.append(f"Started P{r['start']}{cls} ({r['start_words']}). Your position after lap 1 isn't known.")
    elif r["lap1_pos"]:
        out.append(f"P{r['lap1_pos']}{cls} after lap 1. Your start position isn't known.")
    segs = [s for s in r["corners"] if s["gained"] or s["lost"]]
    if segs:
        bits = []
        for s in segs:
            g, lo = s["gained"], s["lost"]
            part = " and ".join(x for x in ((f"gained {g}" if g else ""), (f"lost {lo}" if lo else "")) if x)
            bits.append(f"{part} {_where(s['label'])}")
        out.append("Places seen changing on lap 1: " + "; ".join(bits) + ".")
        if r["change"] is not None and r["seen_net"] is not None and r["seen_net"] != r["change"]:
            out.append(f"These are only the cars the replay saw near you: they add up to {r['seen_net']:+d}, while your "
                       f"position changed by {r['change']:+d} ({_short_src(r)}).")
    elif r["corners"]:
        out.append("The replay saw no place change near you on lap 1.")
    if r["incidents"]:
        pts = r["incident_points"]
        out.append(f"{_plural(len(r['incidents']), 'incident')} on lap 1" + (f" ({pts}x)" if pts else "") + ": "
                   + "; ".join(_inc_words(i) for i in r["incidents"]) + ".")
    if r["contact"] == "contact_4x":
        out.append(r["contact_why"])
    elif r["contact"] == "possibly":
        out.append("You may have been in contact on lap 1 (another car was close; the replay can't show who touched whom).")
    return out


def _inc_words(i) -> str:
    if i.get("type") == "unknown" or not i.get("type"):
        return f"{i['where']}, kind not known"
    return f"{i['where']}, {im.TYPE_WORDS.get(i['type'], 'incident')} ({i['sure']})"


def _short_src(r) -> str:
    a = {"official": "official start", "racecap_results": "start from RaceCap's results", "telemetry": "telemetry",
         "replay": "replay", "estimate": "guessed start"}.get(r.get("start_from"), "")
    b = {"saved": "your saved result", "telemetry": "telemetry", "replay": "replay"}.get(r.get("lap1_from"), "")
    return a if a == b or not b else f"{a}, then {b}" if a else b


# ---------------------------------------------------------------- across races
def report(rows, tracks_zones=None) -> dict:
    """Across races: per track and overall. rows: race_row() results, any order (sorted oldest first here)."""
    rows = sorted([r for r in rows or [] if isinstance(r, dict) and r.get("ok")], key=lambda r: (r.get("when") or 0, str(r.get("id"))))
    by_track = {}
    for r in rows:
        by_track.setdefault(r.get("track") or "Unknown track", []).append(r)
    tracks = [_group(t, rs, (tracks_zones or {}).get(t)) for t, rs in by_track.items()]
    tracks.sort(key=lambda x: -x["races"])
    overall = _group("all your races", rows, None, corners=False)
    return {"version": VERSION, "races": len(rows), "tracks": tracks, "overall": overall, "list": rows,
            "thresholds": thresholds(), "how": HOW}


HOW = (f"Places gained or lost on lap 1 are your start position minus your position after lap 1 (in class in a multi-class "
       f"race), each from the source the race names. Corner by corner only counts the cars the replay saw near you. A habit "
       f"is only called one when at least {MIN_RACES} races show it, shuffling which races went which way (every "
       f"combination, or {im.PERM_N} with a fixed seed) makes it look that strong less than {ALPHA:.0%} of the time across "
       f"everything tested, and it goes against what your grid places alone would give (from the back anyone tends to "
       f"gain places on lap 1, from the front to lose them). A trend over time needs {TREND_MIN} races, and isn't called "
       f"one when your grid places moved the same way.")


def thresholds() -> dict:
    return {"min_races": MIN_RACES, "alpha": ALPHA, "trend_min": TREND_MIN, "perm_n": im.PERM_N, "perm_seed": im.PERM_SEED,
            "exact_max": im.EXACT_MAX, "swap_max_lap": SWAP_MAX, "rel_side": REL_SIDE, "grid_trend_p": GRID_TREND_P, "settle_s": SETTLE_S, "bands_fixed": [b[3] for b in BAND_FIXED]}


def _facts(values) -> dict:
    v = [x for x in values if x is not None]
    return {"n": len(v), "gained": sum(1 for x in v if x > 0), "lost": sum(1 for x in v if x < 0), "same": sum(1 for x in v if x == 0),
            "avg": round(sum(v) / len(v), 2) if v else None, "total": sum(v) if v else 0}


def _gate(test, facts, rel=None) -> str | None:
    """"gain" / "lose" when the claim passes every gate, else None. rel: the races' average place on the grid
    (rel_start). From the back anyone gains places on lap 1 more often than not, and from the front anyone loses them
    (cars ahead have trouble, there's only one way to go): a null simulation (24 equal cars, a few lap 1 incidents)
    called "you usually gain from the back" a habit in 30% of datasets. So a claim in the direction the grid alone
    would give is never made (REL_SIDE), whatever the test says."""
    if test["p"] >= ALPHA or facts["n"] < MIN_RACES:
        return None
    rel = _num(rel)
    if facts["lost"] >= MIN_RACES and facts["lost"] * 2 > facts["n"] and test["sum"] < 0:
        return None if rel is None or rel < -REL_SIDE else "lose"
    if facts["gained"] >= MIN_RACES and facts["gained"] * 2 > facts["n"] and test["sum"] > 0:
        return None if rel is None or rel > REL_SIDE else "gain"
    return None


def _mean_rel(rows) -> float | None:
    v = [x for x in (_num(r.get("rel_start")) for r in rows) if x is not None]
    return sum(v) / len(v) if len(v) * 2 > len(rows) and v else None


def _group(name, rs, zs, corners=True) -> dict:
    with_change = [r for r in rs if r.get("change") is not None]
    out = {"track": name, "races": len(rs), "with_change": len(with_change), "lap1": _facts([r["change"] for r in with_change])}
    # bands, family-wise with "all races"
    keys = ["front", "middle", "back"]
    cols = [[r["change"] if (r.get("band") or {}).get("key") == k else None for r in with_change] for k in keys]
    cols.append([r["change"] for r in with_change])
    tests = im.sign_flip(cols)
    bands = []
    for k, col, t in zip(keys + ["all"], cols, tests):
        f = _facts(col)
        if not f["n"]:
            continue
        rel = _mean_rel([r for r, v in zip(with_change, col) if v is not None])
        bands.append({"band": k, "label": "all races" if k == "all" else f"started in the {k}", **f, "p": t["p"],
                      "pattern": _gate(t, f, rel)})
    out["bands"] = bands
    out["band_rule"] = sorted({(r.get("band") or {}).get("rule") for r in with_change if r.get("band")})
    # corners on lap 1
    seg_rows = [r for r in rs if r.get("corners")]
    out["corners"] = []
    if corners and seg_rows:
        order = []
        labels = {}
        for r in seg_rows:
            for s in r["corners"]:
                if s["key"] not in order:
                    order.append(s["key"])
                labels.setdefault(s["key"], s["label"])
        first = {}
        cols = []
        for k in order:
            col = []
            for r in seg_rows:
                s = next((x for x in r["corners"] if x["key"] == k), None)
                col.append(s["net"] if s else None)
                if s and s["label"].startswith("the start"):
                    first[k] = s["label"]
            cols.append(col)
        tests = im.sign_flip(cols)
        for k, col, t in zip(order, cols, tests):
            f = _facts(col)
            if not f["n"]:
                continue
            rel = _mean_rel([r for r, v in zip(seg_rows, col) if v is not None])
            out["corners"].append({"key": k, "label": first.get(k) or labels[k], **f, "p": t["p"], "pattern": _gate(t, f, rel)})
    # trend over time
    tr = im.kendall_trend([r["change"] for r in with_change])
    tr["direction"], tr["grid_moved"] = None, False
    if tr["p"] is not None and tr["p"] < ALPHA and tr["tau"]:
        tr["direction"] = "better" if tr["tau"] > 0 else "worse"
        # starting further back over time gains places by itself (and further forward loses them): a trend the
        # grid alone could give isn't called one (null simulation: 7.5% false trends with a drifting grid, 20 races)
        rels = [r.get("rel_start") for r in with_change]
        if all(x is not None for x in rels):
            g = im.kendall_trend(rels)
            if g["p"] is not None and g["p"] < GRID_TREND_P and g["tau"] and (g["tau"] > 0) == (tr["tau"] > 0):
                tr["direction"], tr["grid_moved"] = None, True
        else:
            tr["direction"], tr["grid_moved"] = None, True
    out["trend"] = tr
    out["sentences"] = group_sentences(out)
    return out


def _avg_words(avg, n) -> str:
    if not avg:
        return "kept your place on average" if n > 1 else "kept your place"
    verb = "gained" if avg > 0 else "lost"
    if n == 1:
        return f"{verb} {_places(round(avg))}"
    return f"{verb} {abs(avg):.1f} places on average"


def _where(label) -> str:
    """"the start to Turn 1" -> "between the start and Turn 1"; "Turn 3" -> "at Turn 3"."""
    label = str(label or "")
    if label.startswith("the start to "):
        return "between the start and " + label[len("the start to "):]
    if label.startswith("after ") and label.endswith(" to the line"):
        return "between " + label[len("after "):-len(" to the line")] + " and the line"
    if label == "the run to the line":
        return "on the run to the line"
    return "at " + label


def group_sentences(g) -> list:
    out = []
    at = "" if g["track"] == "all your races" else f" at {g['track']}"
    f = g["lap1"]
    if not g["races"]:
        return [f"No races{at} with a start to report yet."]
    if not f["n"]:
        out.append(f"{_plural(g['races'], 'race')}{at}, but none with both a start position and a position after lap 1.")
    else:
        out.append(f"On lap 1{at} you {_avg_words(f['avg'], f['n'])} over {_plural(f['n'], 'race')} "
                   f"(gained in {f['gained']}, lost in {f['lost']}, same in {f['same']}).")
    for c in sorted(g["corners"], key=lambda c: -abs(c["total"]))[:2]:
        if not (c["gained"] or c["lost"]):
            continue
        verb, k = ("lost", c["lost"]) if c["lost"] >= c["gained"] else ("gained", c["gained"])
        if c["n"] == 1:
            out.append(f"In your one scanned race{at} the replay saw you {'lose' if verb == 'lost' else 'gain'} places {_where(c['label'])} on lap 1.")
        else:
            out.append(f"In {k} of {_plural(c['n'], 'scanned race')}{at} the replay saw you {'lose' if verb == 'lost' else 'gain'} places {_where(c['label'])} on lap 1.")
    for b in g["bands"]:
        if b["band"] == "all" or not b["n"]:
            continue
        out.append(f"Starting in the {b['band']} ({_plural(b['n'], 'race')}) you {_avg_words(b['avg'], b['n'])} on lap 1.")
    pats = [c for c in g["corners"] if c["pattern"]] + [b for b in g["bands"] if b["pattern"]]
    for c in pats:
        verb = "lose" if c["pattern"] == "lose" else "gain"
        if "key" in c:
            out.append(f"This looks like a habit, not chance: the replay usually sees you {verb} places {_where(c['label'])} on lap 1.")
        elif c["band"] == "all":
            out.append(f"This looks like a habit, not chance: you usually {verb} places on lap 1{at}.")
        else:
            out.append(f"This looks like a habit, not chance: starting in the {c['band']}, you usually {verb} places on lap 1.")
    if not pats:
        if g["with_change"] < MIN_RACES and not any(c["n"] >= MIN_RACES for c in g["corners"]):
            out.append(f"With {_plural(g['races'], 'race')} this only says what happened, not a pattern.")
        else:
            out.append("Not enough races yet to call any of this a habit rather than chance.")
    tr = g["trend"]
    if tr["n"] >= TREND_MIN:
        if tr["direction"]:
            out.append(f"Over time your lap 1 is getting {tr['direction']} (over {tr['n']} races).")
        elif tr.get("grid_moved"):
            out.append(f"Your lap 1 changed over time ({tr['n']} races), but so did where you start, so it can't be "
                       "told apart from your grid places.")
        else:
            out.append(f"No clear trend over time in your lap 1 yet ({tr['n']} races).")
    return out


# ---------------------------------------------------------------- everything
def build(src=None, **find) -> dict:
    """The start report across every race found. src: incidentmap.collect()'s result (or pass collect's arguments).
    Never raises."""
    try:
        src = src if src is not None else im.collect(**find)
        outs = [o for o in im.outings(src) if o["kind"] == "race"]
        rows, tz = [], {}
        for o in outs:
            o["_zones"] = im.zones_for(src, o["track"])
            tz[o["track"]] = o["_zones"]
            o["_events"] = im.outing_events(o)
            rows.append(race_row(o))
        out = report(rows, tz)
        out["notes"] = list(src.get("notes") or [])
        return out
    except Exception as e:  # noqa: BLE001
        return {"version": VERSION, "races": 0, "tracks": [], "overall": None, "list": [], "thresholds": thresholds(), "how": HOW,
                "notes": [f"The start report couldn't be worked out ({type(e).__name__})."]}
