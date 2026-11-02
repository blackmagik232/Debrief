"""Resolve markers for a review (2.7.5): an EDL DaVinci Resolve imports onto a timeline (Timelines > Import > Timeline Markers).

The timeline's start (01:00:00:00) is the race start, so line a race recording's green flag up with it. With a RaceCap
scan the markers are real moments (seconds from the green flag). Without one they're the laps the review flagged, placed
by adding up lap times, so they're close but not exact, and the file's title says so.
"""
from __future__ import annotations

import math

FPS = 60
COLOR = {"pass": "Yellow", "lost": "Red", "fight": "Blue", "incident": "Red", "pit": "Purple", "lap": "Green"}
TAG = {"best": "Best lap", "pit": "Pit lap", "unclean": "Garage 61 flagged this lap", "traffic": "Most likely traffic", "slow": "Slow lap"}


def tc(seconds: float, fps: int = FPS) -> str:
    f = int(round(max(0.0, seconds) * fps))
    s, fr = f // fps, f % fps
    return f"{1 + s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}:{fr:02d}"


def _clean(text) -> str:
    return " ".join(str(text).replace("|", "/").split())[:120]


def items(data: dict) -> tuple[list, bool]:
    """([(seconds from the race start, kind, text)], exact)."""
    rc = data.get("racecap") if isinstance(data.get("racecap"), dict) else None
    out = []
    if rc:
        g = rc.get("green") or 0.0
        at = lambda t: t - g  # noqa: E731
        for p in rc.get("passes") or []:
            out.append((at(p["t"]), "pass" if p["made"] else "lost",
                        (f"Passed {p['other']}" if p["made"] else f"Passed by {p['other']}") + f" for P{p['for_position']}"))
        for f in rc.get("fights") or []:
            if f.get("from") is not None:
                out.append((at(f["from"]), "fight", f"Fight with {f['other']} for P{f['for_position']} starts"))
        for i in rc.get("incidents") or []:
            others = " with " + ", ".join(i["others"]) if i.get("others") else ""
            out.append((at(i["t"]), "incident", (", ".join(i.get("what") or []) or "Incident") + others))
        for p in rc.get("pits") or []:
            if p.get("t") is not None:
                out.append((at(p["t"]), "pit", f"Pit stop ({p.get('seconds')} s in the lane)"))
        return [x for x in out if x[0] >= 0], True
    tags = {}
    rcr = data.get("racecraft") if isinstance(data.get("racecraft"), dict) else {}
    for r in rcr.get("timeline") or []:
        if isinstance(r, dict) and r.get("n") is not None:
            tags[int(r["n"])] = [TAG[t] for t in r.get("tags") or [] if t in TAG]
    run = 0.0
    for l in sorted((x for x in data.get("laps") or [] if isinstance(x, dict) and isinstance(x.get("t"), (int, float))), key=lambda x: x.get("n") or 0):
        n = int(l.get("n") or 0)
        if tags.get(n):
            out.append((run, "lap", f"Lap {n}: " + ", ".join(tags[n])))
        if math.isfinite(l["t"]):
            run += float(l["t"])
    return out, False


def edl(data: dict, fps: int = FPS) -> str:
    rows, exact = items(data)
    meta = data.get("meta") or {}
    title = _clean(f"Debrief {meta.get('track') or 'race'}" + ("" if exact else " (approximate, from lap times)"))
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    for k, (t, kind, text) in enumerate(sorted(rows, key=lambda x: x[0]), 1):
        a, b = tc(t, fps), tc(t + 1.0 / fps, fps)
        lines += [f"{k:03d}  001      V     C        {a} {b} {a} {b}  ",
                  f" |C:ResolveColor{COLOR.get(kind, 'Yellow')} |M:{_clean(text)} |D:1", ""]
    return "\n".join(lines)
