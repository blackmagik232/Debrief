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


def view(sec: dict, summary: dict) -> dict:
    """The "From iRacing" section for one recorded section, as plain words and tables the review page and the session
    list both draw (headings, lines, tables; nothing hidden behind a click)."""
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
    blocks.append({"h": "Fuel", "lines": lines, "table": table if len(st) > 0 else None,
                   "note": "A stint ends at each of your own pit stops. Laps with a pit stop in them are left out of the per-lap figures."})
    pl = [x for x in fuel.get("per_lap") or []]
    if pl:
        blocks.append({"h": "Fuel lap by lap", "lines": [],
                       "table": {"cols": ["Lap", "Fuel used", ""], "rows": [[str(x["lap"]), _l(x["fuel_used_l"], 2), "pit lap" if x.get("pit") else ""] for x in pl]}})
    aids = sec.get("aids") or {}
    seen = aids.get("seen") or {}
    if not seen:
        blocks.append({"h": "Driver aids", "lines": ["iRacing didn't report brake bias, ABS or traction control for this car, so there is nothing to show."],
                       "table": None})
    else:
        ls = []
        for k in ("bb", "abs", "tc"):
            if k in seen:
                vals = seen[k]
                ls.append(f"{_AID[k]}: " + (f"{_num_s(vals[0])} the whole time." if len(vals) == 1 else "used " + ", ".join(_num_s(v) for v in vals) + "."))
        ch = aids.get("changes") or []
        for c in ch[:40]:
            ls.append(f"Lap {c.get('lap')}: you changed {_AID.get(c['aid'], c['aid']).lower()} from {_num_s(c['from'])} to {_num_s(c['to'])}"
                      + (f", {_where(c.get('d'))}." if c.get("d") is not None else "."))
        cols = [k for k in ("bb", "abs", "tc") if k in seen]
        blocks.append({"h": "Driver aids", "lines": ls,
                       "table": {"cols": ["Lap"] + [_AID[k] for k in cols],
                                 "rows": [[str(x["lap"])] + [_num_s(x.get(k)) for k in cols] for x in aids.get("per_lap") or []]},
                       "note": "The value shown for a lap is what it was when you finished that lap."})
    inc = sec.get("incidents") or {}
    il = []
    tot = inc.get("total")
    il.append("No incident points." if not tot else f"{tot} incident point{'' if tot == 1 else 's'} in this session"
              + (f" ({inc['team_total']} for the whole team)." if inc.get("team_total") not in (None, tot) else "."))
    ev = inc.get("events") or []
    blocks.append({"h": "Incidents", "lines": il,
                   "table": {"cols": ["Lap", "Points", "Where"],
                             "rows": [[str(e.get("lap")), f"+{e.get('gained')}", _where(e.get("d"))] for e in ev]} if ev else None})
    ps = sec.get("pit_stops") or []
    if ps:
        blocks.append({"h": "Your pit stops", "lines": [],
                       "table": {"cols": ["Lap", "In the pit lane", "Stopped", "Fuel added", "Tyres"],
                                 "rows": [[str(p.get("lap")), f"{p.get('lane_s')} s", f"{p.get('stall_s')} s",
                                           "-" if p.get("fuel_added_l") is None else _l(p["fuel_added_l"]),
                                           "changed" if p.get("tyres_changed") else "-"] for p in ps]}})
    ty, we = sec.get("tyres") or {}, sec.get("weather") or {}
    tl = []
    if ty.get("sets_used") is not None:
        tl.append(f"Tyre sets used: {ty['sets_used']}" + (f" of {ty['sets_available']}." if ty.get("sets_available") else "."))
    if ty.get("wear_end"):
        w = ty["wear_end"]
        if all(x is not None for x in w):
            tl.append("Tyre left at the end (front left, front right, rear left, rear right): "
                      + ", ".join(f"{round(x * 100)}%" for x in w) + ".")
    if we.get("track_c") is not None:
        tl.append(f"Track {we['track_c']:.1f} C ({we['track_c'] * 9 / 5 + 32:.0f} F)"
                  + (f", air {we['air_c']:.1f} C ({we['air_c'] * 9 / 5 + 32:.0f} F)." if we.get("air_c") is not None else "."))
    if tl:
        blocks.append({"h": "Tyres and conditions", "lines": tl, "table": None})
    race = sec.get("race")
    if race:
        pl = race.get("positions") or []
        fin = race.get("finish") or {}
        ls = []
        if fin.get("pos"):
            ls.append(f"You finished P{fin['pos']}" + (f" (P{fin['cpos']} in your class)." if fin.get("cpos") and fin["cpos"] != fin["pos"] else "."))
        blocks.append({"h": "Your position over the race", "lines": ls,
                       "table": {"cols": ["Lap", "Overall", "In class"], "rows": [[str(p["lap"]), f"P{p['pos']}", f"P{p['cpos']}" if p.get("cpos") else "-"] for p in pl]} if pl else None,
                       "note": "Your position when you crossed the line at the end of each lap."})
        riv = [r for r in race.get("rivals") or []]
        if riv:
            rows = []
            for r in sorted(riv, key=lambda r: (-len(r["stops"]), r["number"])):
                sp = r["stops"]
                rows.append([("#" + r["number"] if r["number"] else "-") + (" (you)" if r.get("is_me") else ""), r["name"] or "-",
                             str(len(sp)), ", ".join(str(x["lap"]) for x in sp) or "-",
                             ", ".join(f"{x['lane_s']:.0f} s" for x in sp) or "-"])
            blocks.append({"h": "Pit stops, every car", "lines": [],
                           "table": {"cols": ["Car", "Driver", "Stops", "On laps", "Time in the pit lane"], "rows": rows},
                           "note": "Worked out from each car's pit-road flag, once a second. A pass through the pit lane without stopping counts too."})
    notes = []
    if summary.get("disk_logging_note"):
        notes.append(summary["disk_logging_note"])
    if not summary.get("final"):
        notes.append("This recording is still being written, so the figures may grow.")
    elif summary.get("ended_by", "").startswith("recovered"):
        notes.append("Debrief or the PC stopped suddenly during this session. The recording was finished from what had been saved, so the last half minute may be missing.")
    notes.append("Lap numbers here are iRacing's own.")
    title = {1: "Practice", 2: "Qualifying", 3: "Race"}.get(kind, "Session")
    return {"title": "From iRacing", "kind": title, "recorded": True, "final": bool(summary.get("final")),
            "id": f"{summary.get('folder')}#{sec.get('num')}",
            "lede": "Everything in this section comes straight from iRacing, recorded live by Debrief while you drove. "
                    "Your lap times and everything else on this page are from Garage 61.",
            "blocks": blocks, "notes": notes}


def standalone_view(sec: dict, summary: dict) -> dict:
    """The same view for a recording with no Garage 61 session: it says so, and its lap times are iRacing's."""
    v = view(sec, summary)
    best = sec.get("best")
    head = [f"{sec.get('laps')} laps recorded" + (f", best {_lap_t(best)}" if best else "") + (f", typical {_lap_t(sec.get('typical'))}" if sec.get("typical") else "") + "."]
    v["lede"] = "Recorded by Debrief from iRacing while you drove. Garage 61 doesn't have this session (yet), so every number here is from iRacing."
    v["blocks"].insert(0, {"h": "Laps", "lines": head, "table": {"cols": ["Lap", "Time", ""], "rows": [[str(x["lap"]), _lap_t(x["time"]), "pit lap" if x.get("pit") else ""] for x in sec.get("lap_times") or []]}})
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
    row = {"start": meta.get("started_at"), "type": kind, "track": meta.get("track"), "car": meta.get("car"),
           "subsession": sub}
    got = assign([row], secs).get(0)
    if not got:
        return None
    sec, sm = find(library_folder, got["id"])
    return view(sec, sm) if sec else None


def attach_pending(library_folder, log=None, rewrite=None) -> int:
    """Reviews saved before their recording existed gain the "From iRacing" section: each saved review that matches a
    recorded section and doesn't have it yet is written again (rewrite(dir, data) is report.write by default). Returns
    how many. Never raises."""
    n = 0
    try:
        from . import library, report
        rewrite = rewrite or report.write
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
