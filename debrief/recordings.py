"""Recorded sessions in the library (2.27 "Live data"): listing them, joining them to Garage 61, and the plain-words
"From iRacing" section a review or a session row shows.

Where the numbers come from (and the page says so each time):
  from Garage 61   your laps, lap times, teammates, reference laps, history
  from iRacing     fuel, driver aids, pit service, tyres, incidents with their lap, positions, every car's pit stops;
                   recorded live by Debrief while you drove (liverec.py), so only for sessions it was running for.

Joining a recording to a Garage 61 session (never shown twice):
  Garage 61 sessions are grouped by its own event id, which is NOT iRacing's subsession id, and its laps carry no
  subsession (checked in sync.build_sessions); getting one needs a request per event (g61.event_for_subsession), which
  the session list never makes. So the join is: same kind of session (practice / qualifying / race), same track, same
  car, and the Garage 61 session's first lap starting within 10 minutes of the recorded section (before it started or
  after it ended). If a Garage 61 row ever does carry a "subsession", an exact subsession + kind match wins first.
  Each recording and each Garage 61 session is used once; the nearest start time is taken first.
A recorded section with no Garage 61 session shows as its own row, marked "Recorded by Debrief".
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import fsutil, liverec

WINDOW_S = 600.0
KINDS = {1: "Practice", 2: "Qualifying", 3: "Race"}
_CACHE: dict = {}
_LOCK = threading.Lock()


def kind_of(session_type: str) -> int | None:
    """1 practice, 2 qualifying, 3 race, from iRacing's session type text (the numbers Garage 61 uses)."""
    s = str(session_type or "").strip().lower()
    if not s:
        return None
    if "qualif" in s:
        return 2
    if "race" in s:
        return 3
    if "practice" in s or "warm" in s or "test" in s:
        return 1
    return None


def kind_of_label(label) -> int | None:
    return kind_of(label)


def epoch(iso) -> float | None:
    if not iso:
        return None
    s = str(iso).replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:
        s = m.group(1) + m.group(3)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def same_track(a, b) -> bool:
    from . import library
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or _norm(library.short_track_name(a)) == _norm(library.short_track_name(b)):
        return True
    return min(len(na), len(nb)) >= 6 and (na in nb or nb in na)


def same_car(a, b) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na == nb or (min(len(na), len(nb)) >= 4 and (na in nb or nb in na))


# ---------------------------------------------------------------- listing
def _summary(d: Path) -> dict | None:
    p = d / liverec.SUMMARY
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _CACHE.get(str(p))
        if hit and hit[0] == key:
            return hit[1]
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("final") and (doc.get("sv") or 1) < liverec.SUMMARY_V and (d / liverec.CAR).exists():
        # 2.35: worked out again from the recording's own files (green-flag stops, aids, gaps, AI count). Derived data only.
        try:
            new = liverec.summarise_dir(d, ended_by=doc.get("ended_by") or "", final=True)
            fsutil.write_json(p, new, default=str)
            doc = new
            st = p.stat()
            key = (str(p), st.st_mtime_ns, st.st_size)
        except Exception:  # noqa: BLE001 (the old summary still works)
            pass
    with _LOCK:
        _CACHE[str(p)] = (key, doc)
    return doc


def sections(library_folder) -> list:
    """One dict per recorded section (practice / qualifying / race of a recorded subsession), newest first. A folder
    whose summary isn't written yet has no rows yet."""
    from . import library
    root = liverec.recordings_root(library_folder)
    out = []
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for d in dirs:
        sm = _summary(d)
        if not sm:
            continue
        for s in sm.get("sessions") or []:
            k = kind_of(s.get("type"))
            if k is None or not s.get("started"):
                continue
            fuel = s.get("fuel") or {}
            inc = s.get("incidents") or {}
            track = (sm.get("track") or {})
            full = track.get("name") or ""
            if track.get("config") and track["config"] not in full:
                full = f"{full} ({track['config']})"
            out.append({"id": f"{d.name}#{s.get('num')}", "dir": d.name, "num": s.get("num"), "kind": k,
                        "session": KINDS[k], "type": s.get("type"), "track": full, "track_short": library.short_track_name(full),
                        "car": (sm.get("car") or {}).get("name") or "", "start": s.get("started"), "end": s.get("ended") or s.get("started"),
                        "subsession": sm.get("subsession"), "laps": s.get("laps") or 0, "best": s.get("best"),
                        "typical": s.get("typical"), "fuel_per_lap_l": fuel.get("per_lap_median_l"),
                        "incidents": inc.get("total"), "final": bool(sm.get("final"))})
    out.sort(key=lambda r: epoch(r["start"]) or 0.0, reverse=True)
    return out


def assign(rows: list, secs: list) -> dict:
    """{row index: section} joining Garage 61 session rows to recorded sections (module docstring has the rule). Each
    side is used once."""
    pairs = []
    ep = [(epoch(r.get("start")), i) for i, r in enumerate(rows)]
    ep = sorted((e, i) for e, i in ep if e is not None)
    import bisect
    keys = [e for e, _ in ep]
    for si, s in enumerate(secs):
        s0, s1 = epoch(s.get("start")), epoch(s.get("end")) or epoch(s.get("start"))
        if s0 is None:
            continue
        lo = bisect.bisect_left(keys, s0 - WINDOW_S)
        hi = bisect.bisect_right(keys, (s1 or s0) + WINDOW_S)
        for e, i in ep[lo:hi]:
            r = rows[i]
            if r.get("type") != s["kind"]:
                continue
            sub = r.get("subsession")
            exact = bool(sub and s.get("subsession") and str(sub) == str(s["subsession"]))
            if not exact and not (same_track(r.get("track"), s["track"]) and same_car(r.get("car"), s["car"])):
                continue
            pairs.append((-1.0 if exact else abs(e - s0), si, i))
    pairs.sort()
    used_s, used_r, out = set(), set(), {}
    for _score, si, i in pairs:
        if si in used_s or i in used_r:
            continue
        used_s.add(si)
        used_r.add(i)
        out[i] = secs[si]
    return out


def merge_rows(g61_rows: list, secs: list) -> list:
    """The session list: every Garage 61 row (a joined one gains "rec"), plus a row of its own for each recording that
    matched nothing ("recorded_only": True, marked "Recorded by Debrief"), newest first."""
    joined = assign(g61_rows, secs)
    taken = {s["id"] for s in joined.values()}
    out = []
    for i, r in enumerate(g61_rows):
        r = dict(r, source="garage61")
        s = joined.get(i)
        if s:
            r["rec"] = brief(s)
        out.append(r)
    for s in secs:
        if s["id"] in taken:
            continue
        out.append({"key": "rec:" + s["id"], "event": None, "track_id": None, "car_id": None, "track": s["track_short"] or s["track"],
                    "car": s["car"], "type": s["kind"], "session": s["session"], "start": s["start"], "laps": s["laps"],
                    "clean_laps": None, "best": s["best"], "best_clean": s["typical"], "review": None, "source": "recorded",
                    "recorded_only": True, "rec": brief(s)})
    out.sort(key=lambda r: epoch(r.get("start")) or 0.0, reverse=True)
    return out


def brief(s: dict) -> dict:
    bits = []
    if s.get("fuel_per_lap_l") is not None:
        bits.append(f"fuel {s['fuel_per_lap_l']:.1f} L a lap")
    if s.get("incidents") is not None:
        bits.append(f"{s['incidents']} incident point{'' if s['incidents'] == 1 else 's'}")
    return {"id": s["id"], "line": " · ".join(bits), "final": s.get("final", False)}


def find(library_folder, rec_id: str):
    """(section row, whole summary) for an id from sections(), or (None, None)."""
    m = re.match(r"^([\w.\- ]{1,120})#(\d{1,3})$", str(rec_id or ""))
    if not m:
        return None, None
    d = liverec.recordings_root(library_folder) / m.group(1)
    sm = _summary(d)
    if not sm:
        return None, None
    for s in sm.get("sessions") or []:
        if str(s.get("num")) == m.group(2):
            return s, sm
    return None, None


# ---------------------------------------------------------------- the words
def _l(x, nd=1):
    if x is None:
        return "no reading"
    return f"{x:.{nd}f} L ({x / 3.78541:.{max(1, nd)}f} gal)"


def _lap_t(t):
    if t is None:
        return "no time"
    m, s = divmod(float(t), 60.0)
    return f"{int(m)}:{s:06.3f}" if m else f"{s:.3f}"


def _where(d):
    return "" if d is None else f"{round(float(d) * 100):d}% of the way round"


_AID = {"bb": "Brake bias", "abs": "ABS", "tc": "Traction control"}


def _num_s(v):
    if v is None:
        return "-"
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def and_list(xs) -> str:
    xs = [str(x) for x in xs]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else ""


def finish_words(cls_pos=None, cls_size=None, all_pos=None, all_size=None) -> str:
    """2.35: the one way a finishing place is said on the review page (the race result and From iRacing both use it):
    "P1 in your class of 12, P4 of 30 overall", "P1 in your class", "P4 of 30". "" when nothing is known."""
    def ok(v):
        return isinstance(v, int) and not isinstance(v, bool) and v > 0
    if ok(cls_pos):
        out = f"P{cls_pos} in your class" + (f" of {cls_size}" if ok(cls_size) else "")
        if ok(all_pos):
            out += f", P{all_pos}" + (f" of {all_size}" if ok(all_size) else "") + " overall"
        return out
    if ok(all_pos):
        return f"P{all_pos}" + (f" of {all_size}" if ok(all_size) else "")
    return ""


LAP_KEYS = ("lap", "first_lap", "last_lap")
LAPS_SAME = "Lap numbers here are the same as in the rest of this review (checked against your lap times)."
LAPS_OWN = "Lap numbers here are iRacing's own."
LAPS_UNSURE = ("Lap numbers here are iRacing's own. They couldn't be matched to this review's laps by lap time, so they "
               "may be one off from the lap numbers elsewhere on this page.")


def laps_shifted_words(k: int) -> str:
    return (f"iRacing numbers your laps {abs(k)} {'higher' if k < 0 else 'lower'} than the rest of this review does. The laps "
            "here are renumbered to match the rest of the page (matched on your lap times).")


def shift_laps(obj, k: int):
    """A copy of obj with every lap number ("lap", "first_lap", "last_lap") moved by k."""
    if isinstance(obj, dict):
        return {key: (v + k if key in LAP_KEYS and isinstance(v, int) and not isinstance(v, bool) else shift_laps(v, k))
                for key, v in obj.items()}
    if isinstance(obj, list):
        return [shift_laps(x, k) for x in obj]
    return obj


def lap_offset(review_laps, lap_times, tol: float = 0.02):
    """k such that the review's lap n is the recording's lap n - k (review n = recorded lap + k), found by matching lap
    times; None when it can't be told reliably (fewer than 3 matching laps, or another shift matches nearly as well)."""
    rv = {}
    for l in review_laps or []:
        n, t = (l or {}).get("n"), (l or {}).get("t")
        if isinstance(n, int) and isinstance(t, (int, float)) and t > 0:
            rv[n] = float(t)
    rec = [(x["lap"], float(x["time"])) for x in lap_times or []
           if isinstance(x, dict) and isinstance(x.get("lap"), int) and isinstance(x.get("time"), (int, float)) and x["time"] > 0]
    if not rv or not rec:
        return None
    score = {}
    for k in range(-3, 4):
        both = [(t, rv[lap + k]) for lap, t in rec if lap + k in rv]
        score[k] = (sum(1 for a, b in both if abs(a - b) <= tol), len(both))
    best = max(score, key=lambda k: (score[k][0], -abs(k)))
    hit, n = score[best]
    rest = max(score[k][0] for k in score if k != best)
    if hit >= 3 and hit >= 0.6 * n and hit > 2 * rest:
        return best
    return None


def _field(race: dict):
    """(cars in the race, cars in your class or None in a one-class race, more than one class)"""
    riv = [r for r in race.get("rivals") or [] if isinstance(r, dict)]
    me = next((r for r in riv if r.get("is_me")), None)
    multi = len({r.get("class") or "" for r in riv}) > 1
    cls = len([r for r in riv if (r.get("class") or "") == (me.get("class") or "")]) if me and multi else None
    return (len(riv) or None), cls, multi


def view(sec: dict, summary: dict, laps_note: str | None = None) -> dict:
    """The "From iRacing" section for one recorded section, as plain words and tables the review page and the session
    list both draw (headings, lines, tables; nothing hidden behind a click). laps_note says how its lap numbers relate
    to the review's (for_review); "facts" holds the numbers other sections of the page are checked against."""
    blocks = []
    fuel = sec.get("fuel") or {}
    kind = kind_of(sec.get("type"))
    tank = summary.get("fuel_tank_l")
    lines = []
    if fuel.get("per_lap_median_l") is not None:
        lines.append(f"You used {_l(fuel['per_lap_median_l'], 2)} a lap (the middle value over {fuel.get('laps_counted')} "
                     f"full laps, not counting pit laps).")
    else:
        lines.append("Not enough full laps to work out fuel per lap.")
    if fuel.get("start_l") is not None:
        lines.append(f"You started with {_l(fuel['start_l'])}" + (f" in a {_l(tank, 0)} tank." if tank else "."))
    st = [x for x in fuel.get("stints") or [] if x.get("per_lap_l") is not None]
    table = {"cols": ["Stint", "Laps", "Fuel used", "Per lap"],
             "rows": [[f"{x['stint']}", f"{x['first_lap']} to {x['last_lap']}", _l(x.get("fuel_l")), _l(x.get("per_lap_l"), 2)] for x in st]}
    blocks.append({"key": "fuel", "h": "Fuel", "lines": lines, "table": table if len(st) > 1 else None, "chart": "fuel",
                   "note": "A stint ends at each of your own pit stops. Laps with a pit stop in them are left out of the per-lap figures."})
    pl = [x for x in fuel.get("per_lap") or []]
    if pl:
        blocks.append({"key": "fuel_laps", "h": "Fuel lap by lap", "lines": [], "numbers_of": "fuel",
                       "table": {"cols": ["Lap", "Fuel used", ""], "rows": [[str(x["lap"]), _l(x["fuel_used_l"], 2), "pit lap" if x.get("pit") else ""] for x in pl]}})
    aids = sec.get("aids") or {}
    seen = aids.get("seen") or {}
    if not seen:
        blocks.append({"key": "aids", "h": "Driver aids", "lines": ["iRacing didn't report brake bias, ABS or traction control for this car, so there is nothing to show."],
                       "table": None})
    else:
        ls = []
        ch = aids.get("changes") or []
        steady = [k for k in ("bb", "abs", "tc") if k in seen and len(seen[k]) == 1]
        if steady:
            parts = [f"{_AID[k].lower() if i else _AID[k]} {_num_s(seen[k][0])}" for i, k in enumerate(steady)]
            ls.append((", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]) + " the whole session.")
        for k in ("bb", "abs", "tc"):
            if k in seen and len(seen[k]) > 1:
                ls.append(f"{_AID[k]}: used " + ", ".join(_num_s(v) for v in seen[k]) + ".")
        for c in ch[:40]:
            ls.append(f"Lap {c.get('lap')}: you changed {_AID.get(c['aid'], c['aid']).lower()} from {_num_s(c['from'])} to {_num_s(c['to'])}"
                      + (f", {_where(c.get('d'))}." if c.get("d") is not None else "."))
        cols = [k for k in ("bb", "abs", "tc") if k in seen]
        blocks.append({"key": "aids", "h": "Driver aids", "lines": ls, "chart": "aids" if ch else None,
                       "table": {"cols": ["Lap"] + [_AID[k] for k in cols],
                                 "rows": [[str(x["lap"])] + [_num_s(x.get(k)) for k in cols] for x in aids.get("per_lap") or []]} if ch else None,
                       "note": "The value shown for a lap is what it was when you finished that lap." if ch else None})
    inc = sec.get("incidents") or {}
    il = []
    tot = inc.get("total")
    il.append("No incident points." if not tot else f"{tot} incident point{'' if tot == 1 else 's'} in this session"
              + (f" ({inc['team_total']} for the whole team)." if inc.get("team_total") not in (None, tot) else "."))
    ev = inc.get("events") or []
    blocks.append({"key": "incidents", "h": "Incidents", "lines": il, "chart": "incidents" if ev else None,
                   "table": {"cols": ["Lap", "Points", "Where"],
                             "rows": [[str(e.get("lap")), f"+{e.get('gained')}", _where(e.get("d"))] for e in ev]} if ev else None})
    ps = sec.get("pit_stops") or []
    if ps:
        blocks.append({"key": "pit_stops", "h": "Your pit stops", "lines": [],
                       "table": {"cols": ["Lap", "In the pit lane", "Stopped", "Fuel added", "Tyres"],
                                 "rows": [[str(p.get("lap")), f"{p.get('lane_s')} s", f"{p.get('stall_s')} s",
                                           "-" if p.get("fuel_added_l") is None else _l(p["fuel_added_l"]),
                                           "changed" if p.get("tyres_changed") else "-"] for p in ps]}})
    ty, we = sec.get("tyres") or {}, sec.get("weather") or {}
    tl = []
    if ty.get("sets_used"):
        tl.append(f"Tyre sets used: {ty['sets_used']}" + (f" of {ty['sets_available']}." if ty.get("sets_available") else "."))
    if ty.get("wear_end"):
        w = ty["wear_end"]
        if all(x is not None for x in w):
            tl.append("Tyre left at the end (front left, front right, rear left, rear right): "
                      + ", ".join(f"{round(x * 100)}%" for x in w) + ".")
    if we.get("track_c") is not None:
        tl.append(f"At the end of the session: track {we['track_c']:.1f} C ({we['track_c'] * 9 / 5 + 32:.0f} F)"
                  + (f", air {we['air_c']:.1f} C ({we['air_c'] * 9 / 5 + 32:.0f} F)." if we.get("air_c") is not None else "."))
    if tl:
        blocks.append({"key": "tyres", "h": "Tyres and conditions", "lines": tl, "table": None,
                       "chart": "tyres" if ty.get("wear_end") and all(x is not None for x in ty["wear_end"]) else None})
    race = sec.get("race")
    if race:
        pl = race.get("positions") or []
        fin = race.get("finish") or {}
        ls = []
        size, csize, multi = _field(race)
        words = finish_words(fin.get("cpos") if multi else None, csize, fin.get("pos"), size)
        if words:
            ls.append((f"When you last crossed the line you were {words}." if fin.get("from") == "last lap row"
                       else f"You finished {words}."))
        blocks.append({"key": "position", "h": "Your position over the race", "lines": ls, "chart": "position" if len(pl) > 1 else None,
                       "table": {"cols": ["Lap", "Overall", "In class"], "rows": [[str(p["lap"]), f"P{p['pos']}", f"P{p['cpos']}" if p.get("cpos") else "-"] for p in pl]} if pl else None,
                       "note": "Your position when you crossed the line at the end of each lap."})
        gp = race.get("gaps") or []
        if len(gp) > 1:
            gl = []
            ah = [g["ahead"] for g in gp if g.get("ahead") is not None]
            bh = [g["behind"] for g in gp if g.get("behind") is not None]
            if ah:
                gl.append(f"Car ahead: {ah[0]:.1f} s after lap {gp[0]['lap']}, {ah[-1]:.1f} s at the end.")
            if bh:
                gl.append(f"Car behind: {bh[0]:.1f} s after lap {gp[0]['lap']}, {bh[-1]:.1f} s at the end.")
            blocks.append({"key": "gaps", "h": "Gaps to the cars around you", "lines": gl, "chart": "gaps",
                           "table": {"cols": ["Lap", "Ahead", "Gap", "Behind", "Gap"],
                                     "rows": [[str(g["lap"]), g.get("ahead_name") or "-", "-" if g.get("ahead") is None else f"{g['ahead']:.1f} s",
                                               g.get("behind_name") or "-", "-" if g.get("behind") is None else f"{g['behind']:.1f} s"] for g in gp]},
                           "note": "In your class, when you crossed the line. Shrinking ahead means you were catching; shrinking behind means you were being caught."})
        riv = [r for r in race.get("rivals") or [] if r.get("stops")]
        quiet = len([r for r in race.get("rivals") or [] if not r.get("stops")])
        if riv:
            rows = []
            num_key = lambda n: (0, int(n)) if str(n).isdigit() else (1, str(n))
            for r in sorted(riv, key=lambda r: (-len(r["stops"]), num_key(r["number"]))):
                sp = r["stops"]
                rows.append([("#" + r["number"] if r["number"] else "-") + (" (you)" if r.get("is_me") else ""), r["name"] or "-",
                             str(len(sp)), ", ".join(str(x["lap"]) for x in sp) or "-",
                             ", ".join(f"{x['lane_s']:.0f} s" for x in sp) or "-"])
            blocks.append({"key": "stops", "h": "Pit stops, every car", "chart": "stops",
                           "lines": [f"{len(riv)} car{'' if len(riv) == 1 else 's'} stopped" + (f"; the other {quiet} didn't." if quiet else ".")],
                           "table": {"cols": ["Car", "Driver", "Stops", "On laps", "Time in the pit lane"], "rows": rows},
                           "note": "Worked out from each car's pit-road flag, once a second, from the green flag on. A pass through the pit lane without stopping counts too."})
    notes = []
    if summary.get("disk_logging_note"):
        notes.append(summary["disk_logging_note"])
    if not summary.get("final"):
        notes.append("This recording is still being written, so the figures may grow.")
    elif summary.get("ended_by", "").startswith("recovered"):
        notes.append("Debrief or the PC stopped suddenly during this session. The recording was finished from what had been saved, so the last half minute may be missing.")
    notes.append(laps_note or LAPS_OWN)
    title = {1: "Practice", 2: "Qualifying", 3: "Race"}.get(kind, "Session")
    charts = _charts(sec, summary)
    return {"title": "From iRacing", "kind": title, "recorded": True, "final": bool(summary.get("final")),
            "id": f"{summary.get('folder')}#{sec.get('num')}",
            "lede": "Everything in this section comes straight from iRacing, recorded live by Debrief while you drove.",
            "blocks": blocks, "notes": notes, "charts": charts, "facts": _facts(sec),
            "ai_cars": summary.get("ai_cars") or 0}


def _facts(sec: dict) -> dict:
    """2.35: the recording's figures that other sections of the review also show (agree() checks them)."""
    fuel = sec.get("fuel") or {}
    inc = sec.get("incidents") or {}
    race = sec.get("race") or {}
    fin = race.get("finish") or {}
    size, csize, multi = _field(race)
    out = {"fuel": {"median_l": fuel.get("per_lap_median_l"), "laps": fuel.get("laps_counted"), "start_l": fuel.get("start_l")},
           "incidents": {"total": inc.get("total"), "team_total": inc.get("team_total")},
           "pit_laps": [p.get("lap") for p in sec.get("pit_stops") or [] if isinstance(p.get("lap"), int)],
           # drive-throughs: laps where the car went through the pit lane without standing in its box
           "pass_laps": [p.get("lap") for p in sec.get("pit_stops") or []
                         if isinstance(p.get("lap"), int) and (p.get("stall_s") or 0) < 1],
           # 2.35: the driver aid values you drove with (the Setup section's dials are checked against a steady one)
           "aids": {k: list(v) for k, v in ((sec.get("aids") or {}).get("seen") or {}).items() if isinstance(v, list)},
           # stops where the car stood in its box (a pass through the pit lane, a drive-through, isn't one)
           "pit_stops_n": len([p for p in sec.get("pit_stops") or [] if (p.get("stall_s") or 0) >= 1])}
    if race:
        out["finish"] = {"pos": fin.get("pos"), "cpos": fin.get("cpos") if multi else None, "size": size, "class_size": csize,
                         "multi": multi, "last_row": fin.get("from") == "last lap row"}
    return out


def _charts(sec: dict, summary: dict) -> dict:
    """2.35 "See it": the numbers each chart on the review page draws (review.html draws them; words stay in blocks)."""
    out: dict = {}
    fuel = sec.get("fuel") or {}
    pl = [x for x in fuel.get("per_lap") or [] if x.get("fuel_used_l") is not None and (x.get("lap") or 0) >= 1]
    if pl:
        med = fuel.get("per_lap_median_l")
        tank = summary.get("fuel_tank_l")
        out["fuel"] = {"laps": [{"lap": x["lap"], "l": x["fuel_used_l"], "pit": bool(x.get("pit"))} for x in pl],
                       "median": med, "tank": tank,
                       "laps_per_tank": round(tank * (summary.get("fuel_max_pct") or 1) / med, 1) if tank and med else None}
    race = sec.get("race") or {}
    pos = race.get("positions") or []
    own = [p.get("lap") for p in sec.get("pit_stops") or [] if p.get("lap") is not None]
    inc = [{"lap": e.get("lap"), "n": e.get("gained"), "d": e.get("d")} for e in (sec.get("incidents") or {}).get("events") or []]
    if len(pos) > 1:
        multi = any(p.get("cpos") and p.get("cpos") != p.get("pos") for p in pos)
        out["position"] = {"laps": pos, "class": multi, "stops": own, "incidents": inc}
    if len(race.get("gaps") or []) > 1:
        out["gaps"] = race["gaps"]
    riv = [r for r in race.get("rivals") or [] if r.get("stops")]
    if riv:
        laps = max([p.get("lap") or 0 for p in pos] + [s.get("lap") or 0 for r in riv for s in r["stops"]] + [1])
        mine = next((r for r in race.get("rivals") or [] if r.get("is_me")), None)
        out["stops"] = {"laps": laps, "cars": [{"n": r["number"], "name": r["name"], "me": bool(r.get("is_me")), "cls": r.get("class") or "",
                                                "stops": [{"lap": s.get("lap"), "s": s.get("lane_s")} for s in r["stops"]]} for r in riv],
                        "me_class": (mine or {}).get("class") or ""}
    if inc:
        out["incidents"] = inc
    ty = sec.get("tyres") or {}
    if ty.get("wear_end") and all(x is not None for x in ty["wear_end"]):
        out["tyres"] = {"wear": ty["wear_end"]}
    aids = sec.get("aids") or {}
    if aids.get("changes"):
        out["aids"] = {"changes": aids["changes"], "per_lap": aids.get("per_lap") or []}
    return out


def standalone_view(sec: dict, summary: dict) -> dict:
    """The same view for a recording with no Garage 61 session: it says so, and its lap times are iRacing's."""
    v = view(sec, summary)
    best = sec.get("best")
    head = [f"{sec.get('laps')} laps recorded" + (f", best {_lap_t(best)}" if best else "") + (f", typical {_lap_t(sec.get('typical'))}" if sec.get("typical") else "") + "."]
    v["lede"] = "Recorded by Debrief from iRacing while you drove. Garage 61 doesn't have this session (yet), so every number here is from iRacing."
    v["blocks"].insert(0, {"key": "laps", "h": "Laps", "lines": head, "table": {"cols": ["Lap", "Time", ""], "rows": [[str(x["lap"]), _lap_t(x["time"]), "pit lap" if x.get("pit") else ""] for x in sec.get("lap_times") or []]}})
    return v


# ---------------------------------------------------------------- reviews
def for_review(data: dict, library_folder) -> dict | None:
    """The "From iRacing" view for a review dict (meta: track, car, started_at, session, event) when a recording matches
    it, else None."""
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    kind = kind_of_label(meta.get("session") or "Race")
    if kind is None or not meta.get("started_at"):
        return None
    secs = sections(library_folder)
    if not secs:
        return None
    sub = (data.get("racecap") or {}).get("subsession") if isinstance(data.get("racecap"), dict) else None
    sub = meta.get("subsession") or sub     # 2.34: a review built from this PC knows its iRacing session
    row = {"start": meta.get("started_at"), "type": kind, "track": meta.get("track"), "car": meta.get("car"),
           "subsession": sub}
    got = assign([row], secs).get(0)
    if not got:
        return None
    sec, sm = find(library_folder, got["id"])
    if not sec:
        return None
    k = lap_offset(data.get("laps"), sec.get("lap_times"))
    if k:
        sec = shift_laps(sec, k)
    v = view(sec, sm, laps_note=LAPS_UNSURE if k is None else LAPS_SAME if k == 0 else laps_shifted_words(k))
    v["facts"]["lap_check"] = "unknown" if k is None else "same" if k == 0 else "shifted"
    v["facts"]["lap_shift"] = k or 0
    n = (sm or {}).get("ai_cars")
    if isinstance(n, int) and not isinstance(n, bool) and n > 0 and "ai_cars" not in meta:   # 2.35: a race against AI
        meta["ai_cars"], meta["vs_ai"] = n, True
    return v


def attach_pending(library_folder, log=None, rewrite=None) -> int:
    """Reviews saved before their recording existed gain the "From iRacing" section: each saved review that matches a
    recorded section and doesn't have it yet is written again (rewrite(dir, data) is report.write by default). Returns
    how many. Never raises."""
    n = 0
    try:
        from . import library, report
        rewrite = rewrite or (lambda d, data, log=None: report.write(d, data, log=log, setup=False))
        lib = Path(library_folder)
        secs = sections(lib)
        if not secs:
            return 0
        rows = library.review_summaries(lib)
        cand = []
        for r in rows:
            k = kind_of_label(r.get("session") or "Race")
            if k and r.get("started_at"):
                cand.append({"start": r["started_at"], "type": k, "track": r.get("track"), "car": r.get("car"), "dir": r["dir"]})
        joined = assign(cand, secs)
        for i, s in joined.items():
            d = lib / "reviews" / cand[i]["dir"]
            p = d / "review.json"
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            have = data.get("from_iracing")
            if isinstance(have, dict) and have.get("id") == s["id"] and (have.get("final") or not s.get("final")):
                continue
            rewrite(d, data, log=log) if log else rewrite(d, data)
            n += 1
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"  Couldn't add recordings to saved reviews: {type(e).__name__}: {e}")
    return n


_OLD_KEYS = {"Fuel": "fuel", "Fuel lap by lap": "fuel_laps", "Driver aids": "aids", "Incidents": "incidents",
             "Your pit stops": "pit_stops", "Tyres and conditions": "tyres", "Your position over the race": "position",
             "Gaps to the cars around you": "gaps", "Pit stops, every car": "stops", "Laps": "laps"}


def upgrade_view(fi: dict) -> dict:
    """A From iRacing section saved before 2.35's checks (no "facts", blocks without "key") kept when its recording is
    gone: its blocks gain their keys and its figures are read back from its own words (all written by view() above), so
    agree() can check them. Lap numbers of such a view were never checked against the review."""
    if not isinstance(fi, dict) or isinstance(fi.get("facts"), dict) or not isinstance(fi.get("blocks"), list):
        return fi
    import re as _re
    facts: dict = {"lap_check": "unknown", "fuel": {}, "incidents": {}, "pit_laps": []}
    for b in fi["blocks"]:
        if not isinstance(b, dict):
            continue
        b.setdefault("key", _OLD_KEYS.get(str(b.get("h") or "")))
        text = " ".join(str(x) for x in b.get("lines") or [])
        if b["key"] == "fuel":
            m = _re.search(r"You used ([\d.]+) L .*? over (\d+) ", text)
            if m:
                facts["fuel"] = {"median_l": float(m.group(1)), "laps": int(m.group(2))}
            m = _re.search(r"You started with ([\d.]+) L", text)
            if m:
                facts["fuel"]["start_l"] = float(m.group(1))
        elif b["key"] == "incidents":
            m = _re.search(r"^(\d+) incident points? in this session(?: \((\d+) for the whole team\))?", text)
            facts["incidents"] = {"total": int(m.group(1)) if m else 0 if text.startswith("No incident") else None,
                                  "team_total": int(m.group(2)) if m and m.group(2) else None}
        elif b["key"] == "position":
            m = _re.search(r"You finished P(\d+)(?: \(P(\d+) in your class\))?", text)
            if m:
                facts["finish"] = {"pos": int(m.group(1)), "cpos": int(m.group(2)) if m.group(2) else None,
                                   "multi": bool(m.group(2)), "last_row": None}   # None: an old view never said if this was the final order
        elif b["key"] == "pit_stops":
            rows = (b.get("table") or {}).get("rows") or []
            facts["pit_stops_n"] = len(rows)
    fi["facts"] = facts
    fi["notes"] = [n for n in fi.get("notes") or [] if n != LAPS_OWN] + [LAPS_UNSURE]
    return fi


# ---------------------------------------------------------------- 2.35: one value for each fact on the review page
SOURCE_LABEL = {"iracing": "From iRacing (the results in your telemetry file)",
                "results_file": "From your iRacing results file",
                "recording": "From iRacing (recorded by Debrief when the race ended)", "you": "Entered by you",
                "scan": "From the replay scan"}
SOURCE_WHY = {"iracing": "it is iRacing's own result, from your telemetry file",
              "results_file": "it is from iRacing's results file",
              "recording": "it is what iRacing reported when the race ended",
              "you": "you entered it", "scan": "it is where you finished on the road in the replay scan"}


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _pts(n) -> str:
    return f"{n} incident point{'' if n == 1 else 's'}"


def agree(data: dict) -> dict | None:
    """2.35 "no contradictions": each fact the review shows in more than one place gets one owner, and this works out
    the words the other places use. The race result owns finish, start and incidents; Debrief's recording owns fuel
    per lap and the pit stop laps. Returns {"source_label", "multi", "finish", "blocks", "fuel", "pit_line", "notes"}
    (only what applies) or None. Every note is one plain sentence saying which figure the page uses and why; the page
    shows them once near the top. Reads the review only, so it is the same on every write."""
    if not isinstance(data, dict):
        return None
    res = data.get("result") if isinstance(data.get("result"), dict) else None
    if res and _int(res.get("finish_pos")) is None:
        res = None
    fi = data.get("from_iracing") if isinstance(data.get("from_iracing"), dict) else {}
    facts = fi.get("facts") if isinstance(fi.get("facts"), dict) else {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    out: dict = {"blocks": {}, "notes": []}
    src = (res or {}).get("source")
    why = SOURCE_WHY.get(src, "it is the result saved with this review")
    if res:
        out["source_label"] = SOURCE_LABEL.get(src, "Saved with this review")

    # ---- finishing place (the race result owns it)
    rf = facts.get("finish") if isinstance(facts.get("finish"), dict) else {}
    if rf and rf.get("last_row") is None:
        rf = {}      # an old saved view: whether its finish was final can't be known, so it isn't compared
    r_pos, r_cpos = _int(rf.get("pos")), _int(rf.get("cpos"))
    multi = bool(rf.get("multi")) or bool(meta.get("multiclass")) or bool(counts.get("multiclass")) or \
        bool(res and _int(res.get("overall_pos")))
    out["multi"] = multi
    if res:
        fp, fs = res["finish_pos"], _int(res.get("field_size"))
        # which place the result holds: in a multi-class race a result from iRacing (or one that has the overall place
        # beside it, or that the recording's class place confirms) is your class place; a typed one is not assumed to be
        if not multi:
            kind = "overall"
        elif src in ("iracing", "recording") or _int(res.get("overall_pos")) or (r_cpos and r_cpos == fp):
            kind = "class"
        else:
            kind = "unknown"
        cls = cs = al = als = None
        if kind == "overall":
            al, als = fp, fs
        elif kind == "class":
            cls, cs, al, als = fp, fs, _int(res.get("overall_pos")), _int(res.get("overall_size"))
            if al is None and r_cpos == fp and r_pos:
                al, als = r_pos, _int(rf.get("size"))                              # the recording adds the overall place
        words = finish_words(cls, cs, al, als) if kind != "unknown" else finish_words(None, None, fp, fs)
        first = f"P{fp}"
        out["finish"] = {"words": words, "value": first, "ctx": words[len(first):].lstrip(" ,"),
                         "class_pos": cls, "overall_pos": al, "kind": kind}
        rec = finish_words(r_cpos if multi else None, _int(rf.get("class_size")), r_pos, _int(rf.get("size")))
        if kind == "class":
            bad = bool(r_cpos) and r_cpos != cls
        elif kind == "overall":
            bad = bool(r_pos) and r_pos != al
        else:
            bad = bool(r_pos or r_cpos) and fp not in (r_pos, r_cpos)
        lead = "When you last crossed the line you were" if rf.get("last_row") else "You finished"
        if bad:
            said = "When you last crossed the line Debrief's recording had you" if rf.get("last_row") else "Debrief's recording of iRacing has you finishing"
            line = f"{said} {rec}, but the race result says {words}. This page uses the race result because {why}."
            out["blocks"]["position"] = {"lines": [line]}
            out["notes"].append({"fact": "finish", "text": line})
        elif rf and rec and kind == "unknown":
            out["blocks"]["position"] = {"lines": [f"{lead} {rec}. The race result says {words}."]}
        elif rf and rec:
            out["blocks"]["position"] = {"lines": [f"{lead} {words}, the same as the race result."]}

    # ---- incident points (the race result owns them)
    inc = facts.get("incidents") if isinstance(facts.get("incidents"), dict) else {}
    ft, tt = _int(inc.get("total")), _int(inc.get("team_total"))
    ri = _int((res or {}).get("incidents"))
    if ri is not None and ft is not None and ri != ft:
        if tt is not None and ri == tt:
            out["blocks"]["incidents"] = {"lines": [f"{ft} of your team's {_pts(tt)} were yours. The race result counts the "
                                                    f"whole team, so it says {tt}."]}
        else:
            line = (f"iRacing counted {_pts(ft)} for you while Debrief was recording, but the race result says {ri}. "
                    f"This page uses {ri} because {why}.")
            out["blocks"]["incidents"] = {"lines": [line]}
            out["notes"].append({"fact": "incidents", "text": line})

    # ---- fuel per lap (the recording owns it: it reads iRacing's fuel level every lap)
    fu = facts.get("fuel") if isinstance(facts.get("fuel"), dict) else {}
    med = fu.get("median_l")
    if isinstance(med, (int, float)) and med > 0:
        n = _int(fu.get("laps"))
        out["fuel"] = {"median_l": med, "laps": n, "start_l": fu.get("start_l") if isinstance(fu.get("start_l"), (int, float)) else None,
                       "src": "from iRacing, recorded by Debrief while you drove" + (f": the middle value over {n} full laps, not counting pit laps" if n else "")
                              + ". It is the same figure as in From iRacing below."}

    # ---- pit stop laps (the recording owns them, once its lap numbers are known to match the review's)
    passes = sorted({x for x in facts.get("pass_laps") or [] if _int(x)})
    stops = [x for x in facts.get("pit_laps") or [] if _int(x) and x not in passes]
    stops_all = stops + [x for x in passes if x not in stops]
    pits = sorted({x for x in counts.get("pit") or [] if _int(x)})
    if stops_all and facts.get("lap_check") in ("same", "shifted"):
        st = sorted(set(stops))
        line = (f"Pit stop{'s' if len(st) > 1 else ''} on lap{'s' if len(st) > 1 else ''} {and_list(st)} (from iRacing)."
                if st else "")
        if passes:
            line += (" " if line else "") + (f"You drove through the pit lane without stopping on lap{'s' if len(passes) > 1 else ''} "
                                             f"{and_list(passes)} (from iRacing).")
        st = sorted(set(stops))
        if pits:
            line += (f" Lap{'s' if len(pits) > 1 else ''} {and_list(pits)} {'include' if len(pits) > 1 else 'includes'} "
                     f"time in the pit lane, so {'they are' if len(pits) > 1 else 'it is'} left out of your pace numbers.")
        out["pit_line"] = line
        missing = [x for x in st if x not in pits]
        if pits and missing:
            out["notes"].append({"fact": "pit_laps", "text": (
                f"Debrief's recording has a pit stop on lap{'s' if len(missing) > 1 else ''} {and_list(missing)}, which "
                f"{'Garage 61' if meta.get('source') != 'pc' else 'your telemetry file'} didn't mark as a pit lap. The "
                "recording's lap is used for the stop; your pace numbers leave out only the laps marked as pit laps.")})
    if not out["blocks"]:
        out.pop("blocks")
    if not out["notes"]:
        out.pop("notes")
    return out if len(out) > 1 or res else None
