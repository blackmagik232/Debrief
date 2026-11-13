"""Race reviews straight from this PC (2.34 "No token needed").

Your race's telemetry file (.ibt) has every channel of your own car, lap by lap; Debrief's own recording of the race
(Settings > Live data) and the file's session info have the result. Together they make the same race review a Garage 61
review makes, through the same engine, page and library: only the comparison laps from teammates and followed drivers
need Garage 61, and those are added later with "Add comparison laps".

    races(folder=None, cache_path=None)         -> race sessions on this PC, newest first (sessionibt.sessions rows)
    newest(...)                                  -> the newest one, or None
    build(cfg, session, progress=None, info=None) -> (review dict, out dir) ready for report.write
    event_id(session)                            -> "ir<subsession>" (meta.event_id of a review made here)

Read only: never writes, moves or copies a telemetry file. Makes no network requests itself (the comparison laps
2.36 adds are fetched by app.cmd_review_upgrade and handed to build).
"""
from __future__ import annotations

import hashlib
import math
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import engine, ibt, vsai

SOURCE = "pc"                       # meta.source of a review built here ("garage61" is implied when absent)
SOURCE_LABEL = "From your PC"
REFS_FROM = "garage61"              # meta.refs_from of a PC review that has comparison laps from Garage 61 (2.36)
MIN_LAPS = 2                        # laps with telemetry a race needs to be reviewed (the Garage 61 rule)
COLUMNS = {"Speed": "Speed", "LapDistPct": "LapDistPct", "Lat": "Lat", "Lon": "Lon", "Brake": "Brake",
           "Throttle": "Throttle", "Gear": "Gear", "ABSActive": "BrakeABSactive", "LatAccel": "LatAccel",
           "LongAccel": "LongAccel", "SteeringWheelAngle": "SteeringWheelAngle", "RPM": "RPM"}
NO_RACE = ("Debrief found no race in your telemetry files. Turn on Settings > Live data > Make iRacing save my "
           "telemetry (or press Alt+L in the car), race, then press Review a session.")
NO_FOLDER = ("Debrief can't find your iRacing telemetry folder, so it can't review a race from this PC. Set the folder "
             "in Settings, or connect Garage 61.")


# ---------------------------------------------------------------- where the races are
def telemetry_folder() -> Path | None:
    try:
        from .desktop import settings_io
        raw, _err = settings_io._raw()
        return settings_io.telemetry_folder(raw or {})
    except Exception:  # noqa: BLE001
        return None


def default_cache() -> Path:
    from . import firstrun
    return firstrun._cache_dir() / firstrun.SETUPLAB_CACHE


def is_race(session: dict) -> bool:
    return any(str(t).strip().lower() == "race" for t in (session or {}).get("session_types") or [])


def races(folder=None, cache_path=None, progress=None, stop=None) -> list:
    """Race sessions in the telemetry folder (a session that has race laps), newest first. [] when there is no folder."""
    from . import sessionibt
    folder = Path(folder) if folder else telemetry_folder()
    if not folder or not Path(folder).is_dir():
        return []
    out = sessionibt.sessions(folder, cache_path=cache_path or default_cache(), progress=progress, stop=stop)
    return [s for s in out if is_race(s) and s.get("paths")]


def newest(folder=None, cache_path=None) -> dict | None:
    r = races(folder, cache_path)
    return r[0] if r else None


def event_id(session: dict) -> str:
    sub = (session or {}).get("subsession")
    if sub:
        return f"ir{int(sub)}"
    # 2.35: a hash of the whole session id (its last 12 characters were the time of day, so two days collided)
    return "ir" + hashlib.sha1(str((session or {}).get("id") or "session").encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------- the laps
def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _race_num(lapsets: list) -> int | None:
    """The race session's number: the race session with the most laps (a heat format has several)."""
    count = {}
    for _ib, lp in lapsets:
        for x in lp:
            if str(x.get("session") or "").strip().lower() == "race":
                count[x["session_num"]] = count.get(x["session_num"], 0) + 1
    return max(count, key=lambda k: (count[k], k)) if count else None


def _raw(ib, lap: dict) -> dict:
    a, b = lap["start"], lap["end"]
    cols = {}
    for csv_name, ch in COLUMNS.items():
        if ib.has(ch):
            cols[csv_name] = ib.col(ch)[a:b]
    return engine.raw_from_columns(cols, b - a)


def _wet_percent(v) -> float | None:
    """iRacing's TrackWetness channel (1 dry ... 7 extremely wet; 0 unknown) as a 0-100 figure, the scale Garage 61's
    trackWetness is compared on (depth.SIMILAR_WETNESS). 1 (dry) and 2 (mostly dry) are both 0: iRacing's "mostly dry" is
    not a wet track, and 16.7 % would read as different from dry."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v < 1:
        return None
    return 0.0 if v < 3 else round((min(7.0, v) - 1.0) / 6.0 * 100.0, 1)


def _lap_conditions(ib, a: int, b: int, track: dict) -> dict:
    """2.36: the track and air temperature (deg C) and wetness (0-100) of one lap, in the keys Garage 61's laps carry
    (trackTemp, airTemp, trackWetness) so one set of rules reads both. Live channels (the median over the lap) when the
    file has them; otherwise the session text's temperatures (a forecast at the start, the same for every lap). A figure
    the file doesn't have is left out, never guessed."""
    out = {}

    def med(name):
        if not ib.has(name):
            return None
        col = np.asarray(ib.col(name)[a:b], float)
        col = col[np.isfinite(col)]
        return float(np.median(col)) if len(col) else None
    tt = med("TrackTempCrew") if ib.has("TrackTempCrew") else med("TrackTemp")
    at = med("AirTemp")
    forecast = tt is None and track.get("surface_temp") is not None
    tt = tt if tt is not None else track.get("surface_temp")
    at = at if at is not None else track.get("air_temp")
    wet = _wet_percent(med("TrackWetness"))
    if isinstance(tt, (int, float)) and math.isfinite(tt):
        out["trackTemp"] = round(float(tt), 1)
        if forecast:
            out["trackTempForecast"] = True     # only the session text's forecast was known: never shown as measured
    if isinstance(at, (int, float)) and math.isfinite(at):
        out["airTemp"] = round(float(at), 1)
    if wet is not None:
        out["trackWetness"] = wet
    return out


def read_laps(session: dict, eid: str) -> tuple:
    """(laps in the Garage 61 shape the engine reads, {lap id: parsed telemetry}, facts) for the race of this session.
    facts: {"session_info", "player", "car", "track", "rate", "files"} from the file with the most race laps."""
    lapsets, opened = [], []
    try:
        for p in session.get("paths") or []:
            try:
                ib = ibt.open_ibt(p)
            except Exception:  # noqa: BLE001  (a damaged or half-written file is skipped, as Practice does)
                continue
            opened.append(ib)
            lapsets.append((ib, ibt.laps(ib)))
        num = _race_num(lapsets)
        if num is None:
            raise SystemExit(NO_RACE)
        laps, tel, best_file, most = [], {}, None, -1
        for ib, lp in lapsets:
            race = [x for x in lp if x["session_num"] == num and str(x.get("session") or "").lower() == "race"]
            if len(race) > most:
                most, best_file = len(race), ib
            rate = ib.tick_rate or 60
            t0 = float(ib.start_date or 0)
            for x in race:
                if x.get("time") is None:
                    continue
                lid = f"{Path(ib.path).name}#{x['lap']}"
                try:
                    tel[lid] = _raw(ib, x)
                except (ValueError, IndexError):
                    continue
                laps.append({**_lap_conditions(ib, x["start"], x["end"], ib.track()),
                             "id": lid, "lap_number": x["lap"], "lap_time": float(x["time"]), "clean": bool(x["clean"]),
                             "start_time": _iso(t0 + x["start"] / rate), "event": eid, "session_type": 3,
                             "incidents": x.get("incidents", 0), "pit": bool(x.get("pit")), "off": bool(x.get("off")),
                             "start_lap": bool(x.get("start_lap")), "timed_by": x.get("timed_by")})
        laps.sort(key=lambda l: l["lap_number"])
        ib = best_file
        facts = {"session_info": ib.session if ib else {}, "player": ib.player() if ib else {},
                 "car": ib.car() if ib else {}, "track": ib.track() if ib else {}, "race_num": num,
                 "files": [Path(x.path).name for x in opened]}
        return laps, tel, facts
    finally:
        for ib in opened:
            try:
                ib.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- the result, without typing it in
def _sof(iratings: list) -> int | None:
    """iRacing's strength-of-field formula (the published one) over the field's pre-race iRatings."""
    xs = [float(x) for x in iratings if isinstance(x, (int, float)) and x > 0]
    if not xs:
        return None
    k = 1600 / math.log(2)
    return int(round(k * math.log(len(xs) / sum(math.exp(-x / k) for x in xs))))


def result_from(facts: dict, recording: dict | None = None) -> dict:
    """Result fields (results.FIELDS names) from the file's session info, with the recording's finish when it has one.
    Every field is optional; what can't be known is left out rather than guessed."""
    si = facts.get("session_info") or {}
    di = si.get("DriverInfo") or {}
    me = di.get("DriverCarIdx")
    drivers = [d for d in di.get("Drivers") or [] if not d.get("CarIsPaceCar") and not d.get("IsSpectator")]
    my_class = next((d.get("CarClassID") for d in drivers if d.get("CarIdx") == me), None)
    classes = {d.get("CarClassID") for d in drivers}
    multiclass = len(classes) > 1
    field = [d for d in drivers if not multiclass or d.get("CarClassID") == my_class]
    ai = vsai.ai_count(si) > 0                                         # 2.35: no strength of field or iRating against AI
    out = {"field_size": len(field) or None, "sof": None if ai else _sof([d.get("IRating") for d in field])}
    mine = next((d for d in drivers if d.get("CarIdx") == me), {})
    if not ai and isinstance(mine.get("IRating"), (int, float)) and mine["IRating"] > 0:
        out["irating_before"] = int(mine["IRating"])
    sess = next((s for s in (si.get("SessionInfo") or {}).get("Sessions") or []
                 if s.get("SessionNum") == facts.get("race_num")), {})
    rp = next((r for r in sess.get("ResultsPositions") or [] if r.get("CarIdx") == me), None)
    out["source"] = "iracing"
    if rp:
        if multiclass and rp.get("ClassPosition") is not None:
            out["finish_pos"] = int(rp["ClassPosition"]) + 1          # iRacing counts class positions from 0
            if rp.get("Position"):                                    # 2.35: the overall place too, with the whole field
                out["overall_pos"], out["overall_size"] = int(rp["Position"]), len(drivers) or None
        elif rp.get("Position") and multiclass:
            # 2.35: no class place in the file: the overall place is kept as the overall place and the class finish is
            # left unset (a place from one base beside a field from another read "P8 of 10")
            out["overall_pos"], out["overall_size"] = int(rp["Position"]), len(drivers) or None
        elif rp.get("Position"):
            out["finish_pos"] = int(rp["Position"])
        if isinstance(rp.get("Incidents"), (int, float)):
            out["incidents"] = int(rp["Incidents"])
        if isinstance(rp.get("LapsLed"), (int, float)):
            out["laps_led"] = int(rp["LapsLed"])
        why = str(rp.get("ReasonOutStr") or "").strip().lower()
        if why:
            out["finish_status"] = ("running" if why == "running" else "disconnected" if "disconnect" in why else "dnf")
    q = [r for r in (si.get("QualifyResultsInfo") or {}).get("Results") or []]
    if q:
        base = 0 if min((r.get("Position") for r in q if isinstance(r.get("Position"), int)), default=1) == 0 else 1
        cls = [r for r in q if not multiclass or next((d.get("CarClassID") for d in drivers if d.get("CarIdx") == r.get("CarIdx")), None) == my_class]
        mine_q = next((r for r in q if r.get("CarIdx") == me), None)
        if mine_q is not None:
            if multiclass:
                order = sorted(cls, key=lambda r: r.get("Position", 999))
                out["start_pos"] = next(i for i, r in enumerate(order, 1) if r.get("CarIdx") == me)
            elif isinstance(mine_q.get("Position"), int):
                out["start_pos"] = mine_q["Position"] + (1 - base)
    fin = ((recording or {}).get("race") or {}).get("finish") or {}
    if fin.get("from") != "last lap row":
        pos = fin.get("cpos") if multiclass else fin.get("pos")
        if isinstance(pos, int) and pos > 0:
            out["finish_pos"] = pos                                   # the recording saw the session end
            out["source"] = "recording"
            if multiclass and isinstance(fin.get("pos"), int) and fin["pos"] > 0:
                out["overall_pos"], out["overall_size"] = fin["pos"], len(drivers) or None
        if isinstance(fin.get("incidents"), int):
            out["incidents"] = fin["incidents"]
    if "finish_pos" not in out:
        out.pop("source", None)
    return {k: v for k, v in out.items() if v is not None}


# 2.35: result fields results.validate doesn't take (it is the form's gate) that a result from iRacing keeps
EXTRA_RESULT = ("source", "overall_pos", "overall_size")


def _recording_for(library_folder, meta: dict) -> dict | None:
    """The recorded race section of this race (exact on the subsession), or None."""
    try:
        from . import recordings
        secs = recordings.sections(library_folder)
        row = {"start": meta.get("started_at"), "type": "race", "track": meta.get("track"), "car": meta.get("car"),
               "subsession": meta.get("subsession")}
        got = recordings.assign([row], secs).get(0)
        if not got:
            return None
        sec, _sm = recordings.find(library_folder, got["id"])
        return sec
    except Exception:  # noqa: BLE001  (a recording is extra; the review stands without it)
        return None


# ---------------------------------------------------------------- the review
def track_name(track: dict) -> str:
    """'Name (Config)', the shape Garage 61's names have, so PBs and progress line up with Garage 61 reviews."""
    nm = str(track.get("name") or "").strip()
    cfg = str(track.get("config") or "").strip()
    return f"{nm} ({cfg})" if cfg and cfg.lower() not in nm.lower() else nm


def build(cfg: dict, session: dict, progress=None, info=None, refs_for=None) -> tuple:
    """(review data, review folder) for a race session from races(). Raises SystemExit with a plain sentence when the
    race can't be reviewed.

    2.36 "Both sources": refs_for(laps, tel, meta) -> {"refs", "ref_tel", "on_track", "requests"} fetches comparison
    laps from Garage 61 once your own laps are read (app.cmd_review_upgrade). They go through the engine beside your
    own laps from this PC, which stay the review's laps, telemetry, result, From iRacing and Setup. meta.source stays
    "pc" (where your laps are from); meta.refs_from says "garage61" (where the comparison laps are from)."""
    from . import app, library, results
    step = app._stepper(progress)
    eid = event_id(session)
    step("Reading your race from the telemetry files on this PC...", 0.1)
    laps, tel, facts = read_laps(session, eid)
    if len(tel) < MIN_LAPS:
        raise SystemExit("That race has fewer than 2 timed laps in your telemetry files, so there's nothing to review yet.")
    car = facts.get("car") or {}
    track = facts.get("track") or {}
    player = facts.get("player") or {}
    tname, cname = track_name(track), str(car.get("name") or session.get("car") or "")
    di = (facts.get("session_info") or {}).get("DriverInfo") or {}
    multiclass = len({d.get("CarClassID") for d in di.get("Drivers") or []
                      if not d.get("CarIsPaceCar") and not d.get("IsSpectator")}) > 1
    starts = [app.lap_start_dt(l) for l in laps if app.lap_start_dt(l)]
    meta = {"driver": str(player.get("UserName") or ""), "event_id": eid, "car": cname, "track": tname,
            "started_at": min(starts).isoformat() if starts else "", "session": "Race",
            "salutation": app.normalise_salutation(cfg.get("salutation", "")), "multiclass": multiclass,
            "source": SOURCE, "source_label": SOURCE_LABEL, "subsession": session.get("subsession"),
            "files": facts.get("files"),
            "ai_cars": vsai.ai_count(facts.get("session_info")),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    meta["vs_ai"] = meta["ai_cars"] > 0
    got = {}
    if refs_for is not None:
        step("Looking for comparison laps on Garage 61...", 0.4)
        got = refs_for(laps, tel, meta) or {}
        if got.get("failed"):
            meta["refs_failed"] = REFS_FROM                   # Garage 61 couldn't be reached: not the same as "it had none"
        elif not got.get("unmatched"):
            meta["refs_checked"] = REFS_FROM                  # Garage 61 was asked (the page says so when it had none)
        if got.get("own_list"):
            meta["own_list"] = True                           # the track was matched by your recent lap list (no telemetry)
    refs = [r for r in got.get("refs") or [] if isinstance(r, dict)]
    ref_tel, on_track = got.get("ref_tel") or {}, got.get("on_track")
    if refs:
        meta["refs_from"] = REFS_FROM
    for l in laps:
        if app.lap_start_dt(l):
            l["_start"] = app.lap_start_dt(l).timestamp()
    step(f"{len(laps)} race laps read. Analysing...", 0.5)
    length_m = (track.get("length_km") or 0) * 1000 or None
    try:
        data = engine.analyze(meta, laps, tel, refs, ref_tel, track_length=length_m, log=app.say)
    except ValueError as e:
        raise SystemExit(f"Couldn't build the review: {e}")
    except IndexError:
        raise SystemExit(app.BROKEN_SPEED_MSG)
    if refs and not data.get("refs"):
        data["meta"].pop("refs_from", None)               # every comparison lap was left out: nothing came from Garage 61
    if isinstance(on_track, dict):
        data["on_track"] = dict(on_track, multiclass=bool(on_track.get("multiclass")) or multiclass)
    else:
        data["on_track"] = {"drivers": [], "multiclass": multiclass, "local": True}
    data["meta"]["requests"] = int(got.get("requests") or 0)
    out_dir = library.review_dir(cfg["library_folder"], laps, tname, cname)
    try:
        data["pb"] = library.pb_for(cfg["library_folder"], data, exclude_dir=out_dir.name)
    except Exception:  # noqa: BLE001
        pass
    saved = results.load(out_dir)
    if saved and saved.get("finish_pos") is not None:
        data["result"] = saved                                         # a result you typed always wins
    else:
        fields = result_from(facts, _recording_for(cfg["library_folder"], meta))
        if fields.get("finish_pos"):                                   # nothing to show without a finishing position
            try:
                res = results.validate(fields)
                res.update({k: fields[k] for k in EXTRA_RESULT if fields.get(k) is not None})
                res.setdefault("source", "iracing")
                res["saved_at"] = meta["generated_at"]
                data["result"] = res
            except ValueError as e:
                app.say(f"  The race result from iRacing couldn't be used: {e}")
    learn = app.learn_from_for(cfg["library_folder"], data)
    if learn:
        data["learn_from"] = learn
    return data, out_dir


def median_lap(laps: list) -> float | None:
    ts = [l["lap_time"] for l in laps if l.get("lap_time")]
    return st.median(ts) if ts else None
