"""Session review (2.13): an instant review of a practice, qualifying or race session straight from iRacing's
telemetry files (.ibt), without waiting for Garage 61. Read only: never writes, moves or copies a telemetry file.

    sessions(folder | index | records)  the sessions on this PC, newest first (cheap: uses Setup Lab's index cache)
    review(files, history=None, ...)    one session reviewed, as a JSON-ready dict for the window

What a "session" is (real files, Nov 2026): iRacing writes a new file every time he gets in the car, so one evening is
many files. A session is files in a row of the same car + track with no more than GAP_HOURS between one file's end and
the next file's start, and the same iRacing SubSessionID when the files say which one they belong to.

Words used everywhere here (thresholds are stated, never hidden):
  valid lap  a complete, timed lap with no pit lane, no off track, no new incident and not a race start lap (ibt.laps)
  clean lap  a valid lap that also shows pace (Setup Lab's pace_laps): not the out lap or first flying lap of a run
             (tyres warming up), not a lap alongside another car, and not more than 3 x MAD slower than its run's
             median (107% when the MAD is 0)
The best lap is the fastest valid lap. Median, consistency and segment spread use clean laps. The best possible lap
(best segments added up) uses valid laps, and is only given with MIN_LAPS of them.

What the files can't tell (real files): CarLeftRight (the spotter) is not written to .ibt files, so traffic laps are
only caught by the slow-lap rule; caution periods are not detected; tyre temperatures only change at pit stops.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

from debrief import engine, ibt
from debrief import setuplab as sl

GAP_HOURS = 3.0                 # more than this between files starts a new session
MIN_LAPS = sl.MIN_LAPS          # 5: laps needed before consistency, segment spread or the best possible lap is given
NEAR_BEST_S = 0.5               # "laps within half a second of your best"
SAME_S = 0.05                   # a smaller difference to an earlier session is "about the same"
FUEL_MARGIN_LAPS = 1            # fuel for the race plus this many laps
MAX_FUEL_LAP_L = 20.0           # a lap "using" more than this is a refuel or a reset, not consumption
MIN_COVERAGE = 0.95             # a lap's distance trace must cover this much of the lap for segment times
GAIN_MIN_S = 0.01               # segment gains below this are not listed

_SUB_RE = re.compile(rb"^\s*SubSessionID:\s*(\d+)", re.M)
_ids_memo: dict = {}

REASON_WORDS = {
    "untimed": "no lap time (the file starts or ends during this lap)",
    "pit": "pit lane",
    "start": "race start or formation lap",
    "off": "off track",
    "incident": "incident",
    "warmup": "warm-up (out lap or first flying lap of a run)",
    "traffic": "car alongside for more than 10% of the lap",
    "slow": "slow lap (more than 3 x MAD over its run's median)",
}


# ================================================================ small helpers

def _r(x, nd=3):
    return None if x is None or not math.isfinite(float(x)) else round(float(x), nd)


def _fmt(t) -> str:
    if t is None:
        return "-"
    m, s = divmod(float(t), 60.0)
    return f"{int(m)}:{s:06.3f}" if m >= 1 else f"{s:.3f}"


def _plural(n, word, many=None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _mad(xs) -> float:
    return sl._mad(list(xs))


def _subsession(path) -> int | None:
    """The file's SubSessionID from its session text (no YAML parse). None when missing or 0. Never raises."""
    try:
        p = Path(path)
        st = p.stat()
        key = (str(p), st.st_size, st.st_mtime_ns)
        if key in _ids_memo:
            return _ids_memo[key]
        with ibt.open_ibt(p) as ib:
            m = _SUB_RE.search(ib.session_text.encode("latin-1", "replace"))
        val = int(m.group(1)) if m and int(m.group(1)) > 0 else None
        _ids_memo[key] = val
        return val
    except Exception:  # noqa: BLE001
        return None


# ================================================================ sessions

def _key(rec) -> tuple:
    return (((rec.get("car") or {}).get("path") or "").lower(), ((rec.get("track") or {}).get("code") or "").lower())


def group_records(records: list, gap_hours: float = GAP_HOURS, ids: dict | None = None) -> list:
    """Index records -> lists of records, one per session, oldest first. ids: {file name: SubSessionID or None}."""
    recs = sorted((r for r in records if r.get("ok", True)), key=lambda r: (r.get("sort") or 0, r.get("file") or ""))
    out, cur, cur_end = [], None, 0.0
    for r in recs:
        start = float(r.get("sort") or 0)
        end = start + float(((r.get("detail") or {}).get("duration_s")) or 0)
        sub = (ids or {}).get(r.get("file"))
        if cur:
            prev = cur[-1]
            psub = (ids or {}).get(prev.get("file"))
            same = (_key(prev) == _key(r) and start - cur_end <= gap_hours * 3600
                    and not (sub and psub and sub != psub))
            if same:
                cur.append(r)
                cur_end = max(cur_end, end)
                continue
        cur = [r]
        cur_end = end
        out.append(cur)
    return out


def _session_info(recs: list, folder=None, ids: dict | None = None) -> dict:
    last = recs[-1]
    c, t = last.get("car") or {}, last.get("track") or {}
    types = []
    for r in recs:
        for s in r.get("sessions") or []:
            if s not in types:
                types.append(s)
    keep, _d = sl.pace_laps(recs)
    times = [x["time"] for _f, x in keep]
    bests = [r.get("best") for r in recs if r.get("best")]
    temps = [(r.get("detail") or {}).get("surface_temp") for r in recs]
    temps = [x for x in temps if x is not None]
    sub = next(((ids or {}).get(r.get("file")) for r in recs if (ids or {}).get(r.get("file"))), None)
    first = recs[0]
    out = {"kind": "session", "id": f"{c.get('path') or ''}|{t.get('code') or ''}|{sub or first.get('file')}",
           "car": c.get("name") or c.get("path") or "", "car_path": c.get("path") or "",
           "track": " - ".join(x for x in (t.get("name"), t.get("config")) if x) or (t.get("code") or ""),
           "track_code": t.get("code") or "", "date": first.get("when") or "", "last": last.get("when") or "",
           "sort": float(first.get("sort") or 0), "subsession": sub, "files": [r["file"] for r in recs],
           "session_types": types, "laps": sum(int(r.get("laps") or 0) for r in recs),
           "valid_laps": sum(int(r.get("clean_laps") or 0) for r in recs), "clean_laps": len(times),
           "best": _r(min(bests)) if bests else None, "median": _r(float(np.median(times))) if times else None,
           "surface_temp": _r(float(np.median(temps)), 1) if temps else None}
    if folder:
        out["paths"] = [str(Path(folder) / r["file"]) for r in recs]
    return out


def sessions(src, cache_path=None, gap_hours: float = GAP_HOURS, progress=None, stop=None) -> list:
    """The sessions in a telemetry folder, newest first.

    src: the folder (read with setuplab.index, cached in cache_path), an index dict from setuplab.index, or a list
    of index records. Files of one session must be consecutive in time, the same car + track, within gap_hours, and
    the same SubSessionID when both files have one (read from the folder; not available from bare records).
    Returns [{"kind": "session", "id", "car", "car_path", "track", "track_code", "date", "last", "sort",
    "subsession", "files": [names], "paths": [full paths, when the folder is known], "session_types", "laps",
    "valid_laps", "clean_laps", "best" (fastest valid lap), "median" (of clean laps), "surface_temp"}]."""
    folder = None
    if isinstance(src, (str, Path)):
        folder = Path(src)
        idx = sl.index(folder, cache_path=cache_path, progress=progress, stop=stop)
        records = idx.get("files") or []
    elif isinstance(src, dict):
        folder = src.get("folder")
        records = src.get("files") or []
    else:
        records = list(src or [])
    ids = {r["file"]: _subsession(Path(folder) / r["file"]) for r in records if folder and r.get("file")}
    groups = group_records(records, gap_hours=gap_hours, ids=ids)
    out = [_session_info(g, folder, ids) for g in groups]
    out.sort(key=lambda s: -s["sort"])
    return out


# ================================================================ reading one file for the review

def _race_def(ib) -> dict | None:
    for s in (ib.session.get("SessionInfo") or {}).get("Sessions") or []:
        if str(s.get("SessionType") or "").lower() == "race":
            secs = None if "unlimited" in str(s.get("SessionTime")) else ibt.number(s.get("SessionTime"))
            lp = s.get("SessionLaps")
            laps = int(lp) if isinstance(lp, (int, float)) or str(lp).isdigit() else None
            if secs or laps:
                return {"minutes": round(secs / 60, 1) if secs else None, "laps": laps}
    return None


def _tank(ib) -> float | None:
    di = ib.session.get("DriverInfo") or {}
    mx = ibt.number(di.get("DriverCarFuelMaxLtr"))
    pct = ibt.number(di.get("DriverCarMaxFuelPct"))
    if mx and mx > 0:
        if pct and 0 < pct <= 1:
            mx *= pct
        return float(mx)
    return None


def _sectors(ib) -> list:
    out = []
    for s in (ib.session.get("SplitTimeInfo") or {}).get("Sectors") or []:
        v = ibt.number(s.get("SectorStartPct"))
        if v is not None and 0 <= v < 1:
            out.append(float(v))
    return sorted(set(out))


def _first_last(ib, name, on):
    if not ib.has(name):
        return None, None
    a = ib.col(name).astype(float)
    ok = np.isfinite(a) & on
    if not ok.any():
        ok = np.isfinite(a)
    if not ok.any():
        return None, None
    i = np.nonzero(ok)[0]
    return float(a[i[0]]), float(a[i[-1]])


def _read(path) -> dict:
    """Everything the review needs from one file, in one open. Never raises (returns {"ok": False, "error"})."""
    path = Path(path)
    try:
        with ibt.open_ibt(path) as ib:
            return _read_open(ib, path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "file": path.name, "error": str(e)[:200]}


def _read_open(ib, path: Path) -> dict:
    from debrief import livecoach                # resampling helpers (imported here: keeps this module light)
    laps = ibt.laps(ib)
    det = sl._detail_of(ib)
    when, sort = sl._when(path.name, ib.start_date)
    n = ib.record_count
    on = ib.col("IsOnTrack").astype(bool) if ib.has("IsOnTrack") else np.ones(n, bool)
    types = ib.session_types()
    sess = ib.col("SessionNum").astype(int) if ib.has("SessionNum") else np.zeros(n, int)
    out = {"ok": True, "file": path.name, "path": str(path), "when": when, "sort": sort, "car": ib.car(),
           "track": ib.track(), "setup": ib.setup, "detail": det, "laps": laps,
           "subsession": None, "race": _race_def(ib), "tank": _tank(ib), "sectors": _sectors(ib),
           "length_m": livecoach._ibt_length_m(ib), "rate": ib.tick_rate or 60, "notes": []}
    wi = ib.session.get("WeekendInfo") or {}
    try:
        sub = int(wi.get("SubSessionID") or 0)
        out["subsession"] = sub or None
    except (TypeError, ValueError):
        pass
    out["track_temp"] = _first_last(ib, "TrackTempCrew", on) if ib.has("TrackTempCrew") else _first_last(ib, "TrackTemp", on)
    out["air_temp"] = _first_last(ib, "AirTemp", on)
    out["has_lapdist"] = ib.has("LapDistPct")
    out["has_spotter"] = ib.has("CarLeftRight")
    # fuel at the start of a race (the first sample of a race session in this file)
    out["race_start_fuel"] = None
    out["fuel_max_seen"] = None
    if ib.has("FuelLevel"):
        fl = ib.col("FuelLevel").astype(float)
        good = np.isfinite(fl)
        if good.any():
            out["fuel_max_seen"] = float(fl[good].max())
        race_nums = [k for k, v in types.items() if str(v).lower() == "race"]
        if race_nums:
            idx = np.nonzero(np.isin(sess, race_nums) & good)[0]
            lapc = ib.col("Lap").astype(int) if ib.has("Lap") else None
            # only a file that holds the start (lap 0 or 1); a file that rejoins mid-race is not the start
            if len(idx) and lapc is not None and int(lapc[idx[0]]) <= 1:
                out["race_start_fuel"] = float(fl[idx[0]])
    # pit stops: onto pit road and back out again, counted per session type
    out["pit_stops"] = {}
    if ib.has("OnPitRoad"):
        pit = ib.col("OnPitRoad").astype(np.int8)
        ins = np.nonzero(np.diff(pit) == 1)[0] + 1
        outs = np.nonzero(np.diff(pit) == -1)[0] + 1
        for i in ins:
            if (outs > i).any():
                st = str(types.get(int(sess[i]), "") or "")
                out["pit_stops"][st] = out["pit_stops"].get(st, 0) + 1
    # each valid lap on the review's distance grid (for segment times)
    out["R"] = {}
    L = out["length_m"]
    if L and out["has_lapdist"]:
        for k, x in enumerate(laps):
            if not (x["clean"] and x["time"]):
                continue
            try:
                raw = livecoach._ibt_lap_raw(ib, x)
                if len(raw["v"]) < 50 or engine.coverage(raw) < MIN_COVERAGE:
                    continue
                out["R"][k] = engine.resample(raw, float(x["time"]), float(L))
            except Exception:  # noqa: BLE001
                continue
    return out


# ================================================================ the review

def _laps_table(files: list) -> tuple[list, dict]:
    recs = [{"file": f["file"], "detail": f["detail"]} for f in files]
    keep, dropped = sl.pace_laps(recs)
    kept = {id(x) for _f, x in keep}
    rows = []
    for fi, f in enumerate(files):
        det_rows = f["detail"].get("laps") or []
        for k, (x, d) in enumerate(zip(f["laps"], det_rows)):
            reasons = []
            if x["time"] is None:
                reasons.append("untimed")
            if x["pit"]:
                reasons.append("pit")
            if x["start_lap"]:
                reasons.append("start")
            if x["off"]:
                reasons.append("off")
            if x["incidents"] > 0:
                reasons.append("incident")
            valid = bool(x["clean"] and x["time"])
            if valid and not reasons:
                if id(d) not in kept:
                    reasons.append("warmup" if d.get("warm") else "traffic" if d.get("traffic") else "slow")
            elif not valid and not reasons:
                reasons.append("untimed")
            words = [REASON_WORDS[r] for r in reasons]
            if "incident" in reasons:
                words[reasons.index("incident")] = f"incident (+{x['incidents']}x)"
            rows.append({"n": len(rows) + 1, "file": f["file"], "file_index": fi, "lap": int(x["lap"]),
                         "session": x["session"] or "", "time": _r(x["time"]), "timed_by": x["timed_by"],
                         "valid": valid, "clean": valid and not reasons, "reasons": reasons,
                         "reason": "; ".join(words) if words else None,
                         "fuel_used": d.get("fuel"), "fuel_level": d.get("fuel_level"),
                         "stint": f"{fi}.{d.get('stint', 0)}", "setup": f.get("setup_label"),
                         "_k": k})
    return rows, dropped


def _consistency(clean: list, best) -> dict:
    n = len(clean)
    out = {"clean_laps": n, "enough": n >= MIN_LAPS, "median": None, "mad": None, "iqr": None, "std": None,
           "near_best": None, "near_best_pct": None, "near_best_s": NEAR_BEST_S, "text": ""}
    if not n:
        out["text"] = "No clean laps, so consistency can't be measured."
        return out
    a = np.asarray(clean, float)
    out["median"] = _r(np.median(a))
    out["mad"] = _r(_mad(a))
    out["iqr"] = _r(np.percentile(a, 75) - np.percentile(a, 25)) if n >= 2 else None
    out["std"] = _r(np.std(a, ddof=1)) if n >= 2 else None
    if best is not None:
        near = int(np.sum(a <= best + NEAR_BEST_S + 1e-9))
        out["near_best"] = near
        out["near_best_pct"] = round(100.0 * near / n)
        out["text"] = f"{near} of {_plural(n, 'clean lap')} within half a second of your best."
    if n >= MIN_LAPS:
        out["text"] += (f" Half your clean laps were within {out['mad']:.2f} s of your median ({_fmt(out['median'])})"
                        f"; the middle half spread over {out['iqr']:.2f} s.")
    else:
        out["text"] += f" Only {_plural(n, 'clean lap')}: {MIN_LAPS} are needed before calling it consistent or not."
    return out.copy()


def _segments(files: list, rows: list, best_row) -> tuple[dict, dict]:
    """Best possible lap and per-segment spread. Segments are the reviews' corner segments (engine corners, cut
    halfway between one corner's end and the next one's start), else iRacing's sectors, else equal thirds."""
    theo = {"basis": None, "segments": [], "cuts_m": [], "valid_laps": 0, "possible": None, "best": None,
            "gap_s": None, "gains": [], "enough": False, "text": ""}
    spread = {"segments": [], "clean_laps": 0, "enough": False, "top": None, "text": ""}
    seg_laps = []
    for r in rows:
        f = files[r["file_index"]]
        R = f["R"].get(r["_k"])
        if r["valid"] and R is not None:
            seg_laps.append({"n": r["n"], "R": R, "t": r["time"], "clean": bool(r["clean"])})
    theo["valid_laps"] = len(seg_laps)
    # Real files (Nov 2026): with every valid lap counted, segment bests came from warm-up and slow laps and the best
    # possible lap was 0.65-0.85 s under the real best. With MIN_LAPS clean laps, only clean laps (and the best lap
    # itself) count; with fewer, valid laps do, and the text says so.
    n_clean_seg = sum(1 for x in seg_laps if x["clean"])
    theo["laps_used"] = "valid"
    if n_clean_seg >= MIN_LAPS:
        best_n = best_row["n"] if best_row else None
        seg_laps = [x for x in seg_laps if x["clean"] or x["n"] == best_n]
        theo["laps_used"] = "clean"
    theo["laps_counted"] = len(seg_laps)
    n_valid = sum(1 for r in rows if r["valid"])
    if not seg_laps:
        theo["text"] = ("No valid lap has a full distance trace, so the lap can't be split into segments."
                        if n_valid else "No valid laps, so there is no best possible lap.")
        spread["text"] = "Not enough laps to see where your lap times vary."
        return theo, spread
    ref_file = files[best_row["file_index"]] if best_row else files[0]
    L = float(ref_file.get("length_m") or files[0].get("length_m"))
    ref = next((x for x in seg_laps if best_row and x["n"] == best_row["n"]), seg_laps[0])
    corners = []
    try:
        corners = engine.detect_corners(ref["R"], L=L) or []
    except Exception:  # noqa: BLE001
        corners = []
    seg = None
    if corners:
        seg = engine.build_segments(corners, seg_laps, [], L, eligible_ns=[x["n"] for x in seg_laps])
        theo["basis"] = "corners"
    if seg is None:
        starts = ref_file.get("sectors") or []
        inner = [p * L for p in starts if p > 0]
        if inner:
            names = [f"S{i + 1}" for i in range(len(inner) + 1)]
            theo["basis"] = "iRacing sectors"
        else:
            inner, names = [L / 3, 2 * L / 3], ["S1", "S2", "S3"]
            theo["basis"] = "equal thirds"
        seg = engine.build_segments(None, seg_laps, [], L, eligible_ns=[x["n"] for x in seg_laps],
                                    names=names, cuts=inner)
    names = seg["names"]
    theo["segments"] = names
    theo["cuts_m"] = [_r(c, 1) for c in seg["cuts"]]
    per = seg["laps"]
    for r in rows:
        if str(r["n"]) in per:
            r["segments"] = per[str(r["n"])]
    if best_row is None or str(best_row["n"]) not in per:
        best_row = min((r for r in rows if str(r["n"]) in per), key=lambda r: r["time"])
    bl = per[str(best_row["n"])]
    theo["best"] = _r(best_row["time"])
    by_n = {r["n"]: r for r in rows}
    theo["best_segments"] = [{"segment": nm, "t": b["t"], "lap": by_n[b["lap"]]["lap"], "file": by_n[b["lap"]]["file"],
                              "session": by_n[b["lap"]]["session"]} for nm, b in zip(names, seg["best"])]
    if len(seg_laps) < MIN_LAPS:
        miss = MIN_LAPS - len(seg_laps)
        theo["text"] = (f"Best possible lap needs {MIN_LAPS} valid laps with a full trace; this session has "
                        f"{len(seg_laps)} ({_plural(miss, 'more lap')} needed).")
    else:
        theo["enough"] = True
        possible = round(sum(b["t"] for b in seg["best"]), 3)
        theo["possible"] = possible
        theo["gap_s"] = _r(best_row["time"] - possible)
        gains = []
        for j, (nm, b) in enumerate(zip(names, seg["best"])):
            g = bl[j] - b["t"]
            if g >= GAIN_MIN_S:
                src = by_n[b["lap"]]
                gains.append({"segment": nm, "gain_s": _r(g), "from_lap": src["lap"], "from_file": src["file"]})
        gains.sort(key=lambda x: -x["gain_s"])
        theo["gains"] = gains
        text = (f"Your best {('corners' if theo['basis'] == 'corners' else 'sectors')} added up make "
                f"{_fmt(possible)}, {theo['gap_s']:.2f} s under your best lap ({_fmt(best_row['time'])}).")
        if gains:
            parts = [f"{g['segment']} ({g['gain_s']:.2f} s, best on lap {g['from_lap']})" for g in gains[:3]]
            text += " Most of it is in " + ", ".join(parts) + "."
        if theo["laps_used"] == "clean":
            text += f" Counted from your {len(seg_laps)} clean laps."
        else:
            text += (f" Counted from all {len(seg_laps)} valid laps, because fewer than {MIN_LAPS} were clean, so it "
                     "may be too optimistic.")
        text += " Each segment's best came from a different lap, so this is a guide, not a lap you can count on."
        theo["text"] = text
    # spread per segment over clean laps
    clean_ns = [r["n"] for r in rows if r["clean"] and str(r["n"]) in per]
    spread["clean_laps"] = len(clean_ns)
    if len(clean_ns) >= MIN_LAPS:
        spread["enough"] = True
        m = np.array([per[str(n)] for n in clean_ns], float)
        sd = m.std(axis=0, ddof=1)
        var = sd ** 2
        tot = float(var.sum())
        items = [{"segment": nm, "spread_s": _r(s), "share_pct": round(100 * v / tot) if tot > 0 else 0,
                  "median_s": _r(float(np.median(m[:, j])))} for j, (nm, s, v) in enumerate(zip(names, sd, var))]
        spread["segments"] = items
        top = max(items, key=lambda x: x["spread_s"])
        spread["top"] = top["segment"]
        spread["text"] = (f"Most of your lap-to-lap variation is in {top['segment']} (±{top['spread_s']:.2f} s, "
                          f"one standard deviation over {len(clean_ns)} clean laps).")
    else:
        spread["text"] = (f"Where your lap times vary needs {MIN_LAPS} clean laps with a full trace; "
                          f"this session has {len(clean_ns)}.")
    return theo, spread


def _fuel(files: list, rows: list, pace_med, race_minutes=None, race_laps=None) -> dict:
    used = [r["fuel_used"] for r in rows if r["valid"] and r["fuel_used"] and 0 < r["fuel_used"] < MAX_FUEL_LAP_L]
    out = {"per_lap_l": None, "laps_measured": len(used), "tank_l": None, "tank_source": None,
           "laps_per_tank": None, "race": None, "pit_stops": 0, "pit_visits": 0, "text": ""}
    stops = {}
    for f in files:
        for k, v in (f.get("pit_stops") or {}).items():
            stops[k] = stops.get(k, 0) + v
    out["pit_stops"] = sum(v for k, v in stops.items() if k.lower() == "race")
    out["pit_visits"] = sum(stops.values())
    tank = next((f["tank"] for f in files if f.get("tank")), None)
    if tank:
        out["tank_l"], out["tank_source"] = _r(tank, 1), "session (car's tank size)"
    else:
        seen = [f["fuel_max_seen"] for f in files if f.get("fuel_max_seen")]
        if seen:
            out["tank_l"], out["tank_source"] = _r(max(seen), 1), "most fuel seen (the tank may hold more)"
    if not used:
        out["text"] = "No fuel readings on valid laps, so fuel use can't be measured."
        return out
    per = float(np.median(used))
    out["per_lap_l"] = _r(per)
    text = f"You used {per:.2f} L a lap (median of {_plural(len(used), 'valid lap')})."
    if out["tank_l"]:
        out["laps_per_tank"] = round(out["tank_l"] / per, 1)
        text += f" A {'full ' if tank else ''}{out['tank_l']:.0f} L tank lasts about {int(out['tank_l'] // per)} laps."
    src = "argument"
    if race_minutes is None and race_laps is None:
        rd = next((f["race"] for f in files if f.get("race")), None)
        if rd:
            race_minutes, race_laps, src = rd.get("minutes"), rd.get("laps"), "session"
    lap_s = pace_med or (float(np.median([r["time"] for r in rows if r["valid"]])) if any(r["valid"] for r in rows) else None)
    if (race_laps or race_minutes) and (race_laps or lap_s):
        # same rule as Setup Lab's fuel idea, so the two never disagree
        n = int(race_laps) if race_laps else int(math.ceil(race_minutes * 60 / lap_s)) + 1
        need = (n + FUEL_MARGIN_LAPS) * per
        carried, csrc = None, None
        rs = [f["race_start_fuel"] for f in files if f.get("race_start_fuel") is not None]
        if rs:
            carried, csrc = rs[0], "fuel at the race start"
        else:
            for f in files:
                for k, v in ibt.flatten(f.get("setup") or {}).items():
                    if k.rsplit(".", 1)[-1] == "FuelLevel" and ibt.number(v) is not None:
                        carried, csrc = ibt.number(v), "the setup's fuel"
                if carried is not None:
                    break
        race = {"minutes": race_minutes, "laps": race_laps, "source": src, "est_laps": n, "lap_s": _r(lap_s),
                "margin_laps": FUEL_MARGIN_LAPS, "need_l": _r(need, 1), "carried_l": _r(carried, 1),
                "carried_source": csrc, "spare_l": _r(carried - need, 1) if carried is not None else None,
                "spare_laps": _r((carried - need) / per, 1) if carried is not None else None,
                "stops_needed": None}
        length = (f"The {int(race_laps)}-lap race" if race_laps else
                  f"The {race_minutes:.0f}-minute race is about {n} laps at {_fmt(lap_s)} a lap and")
        text += f" {length} needs about {need:.1f} L with {_plural(FUEL_MARGIN_LAPS, 'lap')} spare."
        if out["tank_l"] and need > out["tank_l"]:
            race["stops_needed"] = int(math.ceil(need / out["tank_l"])) - 1
            text += f" That is more than the tank holds: plan {_plural(race['stops_needed'], 'stop')}."
        if carried is not None:
            d = carried - need
            if d >= 0:
                text += (f" You {'started with' if csrc.startswith('fuel') else 'set'} {carried:.1f} L "
                         f"({csrc}): about {d:.1f} L ({d / per:.1f} laps) more than needed.")
            else:
                text += (f" You {'started with' if csrc.startswith('fuel') else 'set'} {carried:.1f} L "
                         f"({csrc}): about {-d:.1f} L short; add fuel or plan a stop.")
        out["race"] = race
    if out["pit_stops"]:
        text += f" You made {_plural(out['pit_stops'], 'pit stop')} in the race."
    out["text"] = text
    return out


def _conditions(files: list) -> dict:
    def pick(key, i):
        for f in (files if i == 0 else reversed(files)):
            v = (f.get(key) or (None, None))[i]
            if v is not None:
                return _r(v, 1)
        return None
    out = {"track_temp_start": pick("track_temp", 0), "track_temp_end": pick("track_temp", 1),
           "air_temp_start": pick("air_temp", 0), "air_temp_end": pick("air_temp", 1), "source": "live channels"}
    if out["track_temp_start"] is None:
        out["track_temp_start"] = _r(files[0]["track"].get("surface_temp"), 1)
        out["track_temp_end"] = _r(files[-1]["track"].get("surface_temp"), 1)
        out["source"] = "session text (the forecast at the session start, not a live reading)"
    if out["air_temp_start"] is None:
        out["air_temp_start"] = _r(files[0]["track"].get("air_temp"), 1)
        out["air_temp_end"] = _r(files[-1]["track"].get("air_temp"), 1)
    states = [(f["detail"].get("track_state") or "dry") for f in files]
    out["state"] = "wet" if "wet" in states else "damp" if "damp" in states else "dry"
    out["wet"] = out["state"] != "dry"
    t0, t1 = out["track_temp_start"], out["track_temp_end"]
    text = f"Track {t0:.0f}°C" if t0 is not None else "Track temperature not recorded"
    if t0 is not None and t1 is not None and abs(t1 - t0) >= 1:
        text += f" at the start, {t1:.0f}°C at the end"
    if out["air_temp_start"] is not None:
        text += f", air {out['air_temp_start']:.0f}°C"
    out["text"] = text + f", {out['state']}."
    return out


def _setups(files: list, idx: dict | None) -> tuple[list, bool]:
    """Setup label per file, the same letters as Setup Lab when its index is given."""
    mine = []
    for f in files:
        mine.append({"file": f["file"], "sort": f["sort"], "when": f["when"], "setup": f["setup"],
                     "detail": f["detail"], "clean_laps": 0, "sessions": [], "car": f["car"], "track": f["track"],
                     "setup_name": f["detail"].get("setup_name") or ""})
    shared = False
    pool = mine
    if idx and idx.get("files"):
        key = _key(files[0])
        others = [dict(r) for r in idx["files"] if r.get("ok", True) and _key(r) == key]
        names = {r["file"] for r in others}
        if all(m["file"] in names for m in mine):
            pool, shared = others, True
    g = sl.group(pool)
    by_file = {r["file"]: r.get("setup_id") for r in pool}
    by_id = {s["id"]: s for s in g["setups"]}
    for f in files:
        s = by_id.get(by_file.get(f["file"])) or {}
        f["setup_label"] = s.get("label") or "No setup recorded"
        f["setup_fp"] = s.get("fingerprint") or ""
        f["setup_id"] = s.get("id")
    return g["setups"], shared


def _norm_history(history, car_path, track_code) -> list:
    out, recs = [], []
    for h in history or []:
        if not isinstance(h, dict):
            continue
        if h.get("kind") == "session_review":
            s = h.get("summary") or {}
            out.append({"date": h.get("date"), "sort": h.get("sort") or 0, "best": s.get("best"),
                        "median": s.get("median"), "files": h.get("files") or [], "car_path": h.get("car_path"),
                        "track_code": h.get("track_code"), "session_types": h.get("session_types") or [],
                        "surface_temp": (h.get("conditions") or {}).get("track_temp_start")})
        elif h.get("kind") == "session":
            out.append({k: h.get(k) for k in ("date", "sort", "best", "median", "files", "car_path", "track_code",
                                              "session_types", "surface_temp")})
        elif h.get("file") and h.get("ok", True):
            recs.append(h)
    if recs:
        out += [{k: s.get(k) for k in ("date", "sort", "best", "median", "files", "car_path", "track_code",
                                       "session_types", "surface_temp")} for s in sessions(recs)]
    return [h for h in out if str(h.get("car_path") or "").lower() == str(car_path).lower()
            and str(h.get("track_code") or "").lower() == str(track_code).lower()]


def _history(history, me: dict) -> dict | None:
    if history is None:
        return None
    prev = [h for h in _norm_history(history, me["car_path"], me["track_code"])
            if (h.get("sort") or 0) < me["sort"] - 1 and not set(h.get("files") or []) & set(me["files"])]
    out = {"sessions": len(prev), "last": None, "best_before": None, "vs_last": None, "vs_best_before": None,
           "text": ""}
    if not prev:
        out["text"] = "No earlier sessions with this car at this track to compare with."
        return out
    prev.sort(key=lambda h: h.get("sort") or 0)
    timed = [h for h in prev if h.get("best") or h.get("median")]
    if not timed:
        out["text"] = "The earlier sessions here have no valid laps to compare with."
        return out
    last = timed[-1]                                  # the last session here that has a lap time
    out["last"] = {k: last.get(k) for k in ("date", "best", "median", "session_types", "surface_temp")}
    bests = [h for h in prev if h.get("best")]
    if bests:
        b = min(bests, key=lambda h: h["best"])
        out["best_before"] = {"date": b.get("date"), "best": b["best"]}
    s = me["summary"]
    parts = []

    def words(d, what):
        if abs(d) < SAME_S:
            return f"{what} about the same as"
        return f"{what} {abs(d):.2f} s {'faster' if d < 0 else 'slower'} than"

    if s.get("best") is not None and last.get("best"):
        d = s["best"] - last["best"]
        out["vs_last"] = {"best_s": _r(d)}
        parts.append(f"{words(d, 'Best lap')} your last session here ({last.get('date')}, {_fmt(last['best'])})")
    if s.get("median") is not None and last.get("median"):
        d = s["median"] - last["median"]
        out["vs_last"] = dict(out["vs_last"] or {}, median_s=_r(d))
        parts.append(f"{words(d, 'median clean lap')} then ({_fmt(last['median'])})")
    if s.get("best") is not None and out["best_before"]:
        d = s["best"] - out["best_before"]["best"]
        out["vs_best_before"] = _r(d)
        if out["best_before"]["date"] == last.get("date") and last.get("best") == out["best_before"]["best"]:
            if d < 0:
                parts.append("a new best here")
        elif d < 0:
            parts.append(f"a new personal best here by {-d:.2f} s")
        else:
            parts.append(f"{d:.2f} s off your best here ({_fmt(out['best_before']['best'])}, {out['best_before']['date']})")
    text = "; ".join(parts)
    text = (text[:1].upper() + text[1:] + ".") if text else "This session has no valid lap to compare."
    t_me = (me.get("conditions") or {}).get("track_temp_start")
    t_last = last.get("surface_temp")
    if t_me is not None and t_last is not None and abs(t_me - t_last) > sl.MAX_TEMP_GAP:
        text += f" The track was {abs(t_me - t_last):.0f}°C {'hotter' if t_me > t_last else 'cooler'} today, which moves lap times too."
    if parts and set(last.get("session_types") or []) != set(me.get("session_types") or []):
        text += " The sessions were not the same kind (" + ", ".join(last.get("session_types") or ["?"]) + \
                " then, " + ", ".join(me.get("session_types") or ["?"]) + " now)."
    out["text"] = text
    return out


def review(files, history=None, race_minutes=None, race_laps=None, idx: dict | None = None) -> dict:
    """One session reviewed from its .ibt files. Never raises for a bad file.

    files: .ibt paths (any order), or a session dict from sessions() (its "paths").
    history: earlier review() dicts, sessions() dicts or setuplab index records (other cars/tracks are ignored).
    race_minutes / race_laps: the series' race length (default: the race in the session text).
    idx: setuplab.index() result, so setup letters match Setup Lab's.
    Returns the dict described in the 2.13 notes (kind "session_review")."""
    if isinstance(files, dict):
        files = files.get("paths") or []
    paths = [Path(p) for p in (files or [])]
    out = {"kind": "session_review", "version": 1, "car": "", "car_path": "", "track": "", "track_code": "",
           "date": "", "sort": 0, "files": [], "session_types": [], "subsession": None,
           "summary": {"best": None, "best_lap": None, "median": None, "laps": 0, "valid_laps": 0, "clean_laps": 0,
                       "left_out": {}},
           "laps": [], "consistency": None, "theoretical": None, "segment_spread": None, "fuel": None,
           "conditions": None, "setups": [], "stints": [], "history": None,
           "thresholds": {"min_laps": MIN_LAPS, "near_best_s": NEAR_BEST_S, "slow_mad_k": sl.MAD_K,
                          "slow_fallback_pct": round((sl.TRAFFIC - 1) * 100), "traffic_side_pct": round(sl.TRAFFIC_SIDE * 100),
                          "gap_hours": GAP_HOURS, "fuel_margin_laps": FUEL_MARGIN_LAPS, "same_s": SAME_S},
           "notes": [], "bad_files": []}
    read = []
    for p in paths:
        f = _read(p)
        if f.get("ok"):
            read.append(f)
        else:
            out["bad_files"].append({"file": f["file"], "error": f.get("error") or "unreadable"})
    if out["bad_files"]:
        out["notes"].append(f"{_plural(len(out['bad_files']), 'file')} could not be read (damaged or still being "
                            "written) and {} skipped.".format("was" if len(out["bad_files"]) == 1 else "were"))
    if not read:
        out["notes"].append("No readable telemetry in this session.")
        return out
    read.sort(key=lambda f: (f["sort"], f["file"]))
    key = _key(read[-1])
    other = [f for f in read if _key(f) != key]
    if other:
        out["notes"].append(f"{_plural(len(other), 'file')} for another car or track left out of this review.")
        read = [f for f in read if _key(f) == key]
    c, t = read[-1]["car"], read[-1]["track"]
    out.update(car=c.get("name") or c.get("path") or "", car_path=c.get("path") or "",
               track=" - ".join(x for x in (t.get("name"), t.get("config")) if x) or (t.get("code") or ""),
               track_code=t.get("code") or "", date=read[0]["when"], sort=read[0]["sort"],
               files=[f["file"] for f in read])
    subs = sorted({f["subsession"] for f in read if f["subsession"]})
    out["subsession"] = subs[0] if len(subs) == 1 else (subs or None)
    if len(subs) > 1:
        out["notes"].append("These files come from more than one iRacing session.")

    setups, shared = _setups(read, idx)
    rows, dropped = _laps_table(read)
    types = []
    for r in rows:
        if r["session"] and r["session"] not in types:
            types.append(r["session"])
    out["session_types"] = types

    valid = [r for r in rows if r["valid"]]
    clean = [r for r in rows if r["clean"]]
    best_row = min(valid, key=lambda r: r["time"]) if valid else None
    s = out["summary"]
    s.update(laps=len(rows), valid_laps=len(valid), clean_laps=len(clean), left_out=dropped,
             best=_r(best_row["time"]) if best_row else None,
             best_lap={"lap": best_row["lap"], "file": best_row["file"], "session": best_row["session"],
                       "timed_by": best_row["timed_by"]} if best_row else None,
             median=_r(float(np.median([r["time"] for r in clean]))) if clean else None)
    if best_row:
        s["text"] = (f"Best lap {_fmt(best_row['time'])} (lap {best_row['lap']}, {best_row['session'] or 'session'})"
                     + (f"; median clean lap {_fmt(s['median'])}" if s["median"] else "")
                     + f". {len(clean)} clean of {_plural(len(rows), 'lap')}.")
    else:
        s["text"] = f"No valid lap in {_plural(len(rows), 'lap')}."

    out["consistency"] = _consistency([r["time"] for r in clean], s["best"])
    out["theoretical"], out["segment_spread"] = _segments(read, rows, best_row)
    out["fuel"] = _fuel(read, rows, s["median"], race_minutes=race_minutes, race_laps=race_laps)
    out["conditions"] = _conditions(read)

    # stints: laps in a row in one file (a pit stop or a new session starts a new one) with their setup
    stints = {}
    for r in rows:
        st = stints.setdefault(r["stint"], {"stint": len(stints) + 1, "file": r["file"], "session": r["session"],
                                            "first_lap": r["lap"], "last_lap": r["lap"], "laps": 0, "clean_laps": 0,
                                            "best": None, "setup": r["setup"]})
        st["last_lap"] = r["lap"]
        st["laps"] += 1
        if r["clean"]:
            st["clean_laps"] += 1
        if r["valid"] and (st["best"] is None or r["time"] < st["best"]):
            st["best"] = r["time"]
    by_file = {f["file"]: f for f in read}
    for r in rows:
        r["stint"] = stints[r["stint"]]["stint"]
    for st in stints.values():
        f = by_file[st["file"]]
        st["fingerprint"] = f.get("setup_fp")
        st["setup_name"] = f["detail"].get("setup_name") or ""
    out["stints"] = list(stints.values())
    used = {f.get("setup_id") for f in read}
    out["setups"] = [{"id": x["id"], "label": x["label"], "name": x["name"], "fingerprint": x["fingerprint"],
                      "files": [fn for fn in x["files"] if fn in by_file]} for x in setups if x["id"] in used]
    if len(out["setups"]) > 1:
        out["notes"].append(f"You drove {len(out['setups'])} different setups in this session; lap times mix them.")
    if not shared:
        out["notes"].append("Setup letters here count only this session's files (Setup Lab's letters cover all files).")

    for r in rows:
        r.pop("_k", None)
        r.pop("file_index", None)
    out["laps"] = rows
    out["history"] = _history(history, out)

    # what couldn't be read
    if not any(f["has_spotter"] for f in read):
        out["notes"].append("iRacing doesn't write the spotter (car alongside) to telemetry files, so traffic laps are "
                            "only caught by the slow-lap rule.")
    if not all(f["length_m"] and f["has_lapdist"] for f in read):
        out["notes"].append("Some files have no track length or lap distance, so their laps can't be split into segments.")
    n_samples = sum(1 for r in rows if r["timed_by"] == "samples" and r["time"] is not None)
    if n_samples:
        out["notes"].append(f"{_plural(n_samples, 'lap')} had no time from iRacing and {'was' if n_samples == 1 else 'were'} "
                            "timed from the telemetry samples (to about 1/60 s).")
    out["notes"].append("Caution periods aren't detected; tyre temperatures in telemetry only change at pit stops.")
    return out
