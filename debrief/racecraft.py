"""Race craft (Debrief 2.4): how lap 1, traffic, flags and pit stops shaped the race.

build(data, raw_laps=None) works on a review dict. At engine time raw_laps (every Garage 61 lap of the
session) is passed too, so a lap 1 the analysis left out (a tow or a big moment, more than 25% off the
median) still gets its time. report.write calls build(data) for older reviews.

Everything is factual and correlational. Lap 1 "includes the start": we never say standing or rolling
(Debrief doesn't know which). Traffic is "most likely traffic", as the engine calls it.
Manual incidents (result.incident_laps) are merged by the page, never stored here.
"""
from __future__ import annotations

import math

import numpy as np

VERSION = 1
MIN_SEG_LOST_S = 0.2
MAX_SUMMARY = 3
LAP1_NOTE = "includes the start"


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int_list(v):
    out = []
    for x in v or [] if isinstance(v, (list, tuple)) else []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _r(v, d=2):
    return None if v is None else round(float(v), d) + 0.0   # + 0.0: never "-0.0"


def _and(a):
    a = list(a)
    if not a:
        return ""
    return a[0] if len(a) == 1 else ", ".join(a[:-1]) + " and " + a[-1]


def _raw_lap1_t(raw_laps):
    """Lap 1's time from the Garage 61 laps (lapNumber == 1), or None."""
    for l in raw_laps or []:
        if not isinstance(l, dict):
            continue
        n = l.get("lapNumber", l.get("lap_number"))
        if n == 1 and not isinstance(n, bool):
            t = _num(l.get("lapTime", l.get("lap_time")))
            if t and t > 0:
                return t
    return None


def _lap1_segments(data, clean_ns):
    seg = data.get("segments") if isinstance(data.get("segments"), dict) else None
    if not seg:
        return []
    names = seg.get("names") or []
    per = seg.get("laps") if isinstance(seg.get("laps"), dict) else {}
    l1 = per.get("1")
    if not isinstance(l1, list) or len(l1) != len(names):
        return []
    pool = [per[str(n)] for n in clean_ns if n != 1 and isinstance(per.get(str(n)), list)
            and len(per[str(n)]) == len(names)]
    if len(pool) < 2:
        return []
    out = []
    for j, nm in enumerate(names):
        vals = [_num(p[j]) for p in pool if _num(p[j]) is not None]
        if not vals or _num(l1[j]) is None:
            continue
        lost = float(l1[j]) - float(np.median(vals))
        if lost >= MIN_SEG_LOST_S:
            out.append({"id": str(nm), "lost_s": round(lost, 2)})
    out.sort(key=lambda s: -s["lost_s"])
    return out[:2]


def build(data: dict, raw_laps: list | None = None) -> dict | None:
    if not isinstance(data, dict):
        return None
    laps = [l for l in data.get("laps") or [] if isinstance(l, dict) and l.get("n") is not None
            and _num(l.get("t"))]
    if not laps:
        return None
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    median = _num(data.get("median")) or float(np.median([float(l["t"]) for l in laps]))
    best_n = (data.get("best") or {}).get("n") if isinstance(data.get("best"), dict) else None
    clean_ns = [int(l["n"]) for l in laps if l.get("clean")]
    by_n = {int(l["n"]): float(l["t"]) for l in laps}
    unclean = set(_int_list(counts.get("unclean")))
    slow = set(_int_list(counts.get("slow")))
    pit = set(_int_list(counts.get("pit")))
    excluded = set(_int_list(counts.get("excluded_laps")))
    traffic = {}
    for t in counts.get("traffic_detail") or []:
        if isinstance(t, dict) and t.get("n") is not None:
            try:
                traffic[int(t["n"])] = t
            except (TypeError, ValueError):
                pass
    for n in _int_list(counts.get("traffic")):
        traffic.setdefault(n, {"n": n})
    raw_t = {}
    if raw_laps:
        from .engine import _lap_numbers
        nums = _lap_numbers([l for l in raw_laps if isinstance(l, dict)])
        for l in raw_laps:
            if isinstance(l, dict) and nums.get(l.get("id")) is not None:
                t = _num(l.get("lapTime", l.get("lap_time")))
                if t and t > 0:
                    raw_t[int(nums[l.get("id")])] = t
                from .strategy import pit_kind
                if pit_kind(l):
                    pit.add(int(nums[l.get("id")]))

    # ---------- lap 1
    lap1 = None
    if 1 in by_n:
        segs = _lap1_segments(data, clean_ns)
        lap1 = {"n": 1, "t": _r(by_n[1], 3), "vs_median_s": _r(by_n[1] - median, 1), "in_analysis": True,
                "segments": segs, "note": LAP1_NOTE}
    else:
        t1 = raw_t.get(1) or _raw_lap1_t(raw_laps)
        if t1:
            lap1 = {"n": 1, "t": _r(t1, 3), "vs_median_s": _r(t1 - median, 1), "in_analysis": False,
                    "segments": [], "note": LAP1_NOTE}

    # ---------- timeline: one entry per lap with something to say
    rows = {}

    def tag(n, name, t=None):
        r = rows.get(n)
        if r is None:
            tt = by_n.get(n, raw_t.get(n) if t is None else t)
            r = rows[n] = {"n": n, "t": _r(tt, 3), "lost_s": None, "tags": []}
        if name not in r["tags"]:
            r["tags"].append(name)
        return r

    if lap1:
        tag(1, "lap1", lap1["t"])
    if best_n is not None:
        try:
            tag(int(best_n), "best")
        except (TypeError, ValueError):
            pass
    for n in sorted(unclean):
        tag(n, "unclean")
    for n in sorted(traffic):
        r = tag(n, "traffic")
        at = _num(traffic[n].get("at_m"))
        if at is not None:
            r["at_m"] = round(at, 1)
        lost = _num(traffic[n].get("lost"))
        if lost is not None:
            r["traffic_s"] = round(lost, 2)
    for n in sorted(slow):
        tag(n, "slow")
    for n in sorted(pit):
        tag(n, "pit")
    for n in sorted(excluded):
        tag(n, "excluded")
    for n, r in rows.items():
        if "pit" in r["tags"] or "excluded" in r["tags"] or n not in by_n:
            r["lost_s"] = None
        else:
            r["lost_s"] = _r(by_n[n] - median, 1)
    timeline = [rows[n] for n in sorted(rows)]

    # ---------- summary (factual, at most 3 sentences)
    summary = []
    if lap1 and lap1["vs_median_s"] is not None:
        d = lap1["vs_median_s"]
        if d >= 0.05:
            s = f"Lap 1 was {d:.1f} s slower than your typical lap"
            if lap1["segments"]:
                s += ", most of it in " + _and([x["id"] for x in lap1["segments"]])
            s += " (that includes the start)."
        else:
            s = "Lap 1 was as quick as your typical lap (that includes the start)." if d <= 0 else \
                f"Lap 1 was within {d:.1f} s of your typical lap (that includes the start)."
        summary.append(s)
    tr = [r for r in timeline if "traffic" in r["tags"] and r.get("traffic_s") is not None]
    if tr:
        if len(tr) == 1:
            summary.append(f"Most likely traffic cost about {tr[0]['traffic_s']:.1f} s on lap {tr[0]['n']}.")
        else:
            tot = sum(r["traffic_s"] for r in tr)
            summary.append(f"Most likely traffic cost about {tot:.1f} s over laps "
                           + _and([str(r["n"]) for r in tr]) + ".")
    elif traffic:
        summary.append("Lap" + ("s " if len(traffic) > 1 else " ") + _and([str(n) for n in sorted(traffic)])
                       + " lost time in one short stretch, most likely traffic.")
    if unclean and len(summary) < MAX_SUMMARY:
        u = sorted(unclean)
        lost = [by_n[n] - median for n in u if n in by_n]
        s = f"Garage 61 flagged {len(u)} lap{'s' if len(u) != 1 else ''} ({_and([str(n) for n in u])})"
        avg = sum(lost) / len(lost) if lost else None
        if avg is not None and avg >= 0.05:
            s += f"; on those laps you were {avg:.1f} s off your typical lap on average"
        summary.append(s + ".")
    if pit and len(summary) < MAX_SUMMARY:
        p = sorted(pit)
        summary.append(f"Pit lap{'s' if len(p) != 1 else ''}: {_and([str(n) for n in p])} "
                       "(left out of your pace numbers).")
    return {"version": VERSION, "lap1": lap1, "timeline": timeline, "summary": summary[:MAX_SUMMARY]}
