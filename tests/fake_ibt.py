"""Stand-in .ibt files for tests, shaped like the owner's real files (Nov 2026 check):
60 samples a second, LapLastLapTime updating about 2 s after the line (-1 for an untimed lap), tyre temps and wear
changing only in the pit stall, CarSetup values as text with units, the player's car at Drivers[DriverCarIdx].

    make(path, laps=[99.2, 98.9, ...], setup=SETUP_GT4, session="Practice", ...)
"""
from __future__ import annotations

import math

import numpy as np

from debrief import ibt

SETUP_GT4 = {
    "Tires": {
        "LeftFront": {"StartingPressure": "176 kPa", "LastHotPressure": "190 kPa", "LastTempsOMI": "78C, 82C, 88C", "TreadRemaining": "98%, 97%, 96%"},
        "LeftRear": {"StartingPressure": "176 kPa", "LastHotPressure": "188 kPa", "LastTempsOMI": "80C, 82C, 84C", "TreadRemaining": "98%, 98%, 97%"},
        "RightFront": {"StartingPressure": "176 kPa", "LastHotPressure": "191 kPa", "LastTempsIMO": "88C, 82C, 78C", "TreadRemaining": "96%, 97%, 98%"},
        "RightRear": {"StartingPressure": "176 kPa", "LastHotPressure": "189 kPa", "LastTempsIMO": "84C, 82C, 80C", "TreadRemaining": "97%, 98%, 98%"},
    },
    "Chassis": {
        "FrontBrakes": {"ArbSetting": "2", "ToeIn": "-1.3 mm", "BrakePads": "Endurance"},
        "LeftFront": {"CornerWeight": "3281 N", "RideHeight": "77.1 mm", "SpringRate": "140 N/mm", "BumpStiffness": "+37 clicks", "ReboundStiffness": "+3 clicks", "Camber": "-4.0 deg"},
        "LeftRear": {"CornerWeight": "4237 N", "RideHeight": "102.3 mm", "SpringRate": "228 N/mm", "BumpStiffness": "+25 clicks", "ReboundStiffness": "+25 clicks", "Camber": "-2.8 deg", "ToeIn": "+0.1 mm"},
        "InCarDials": {"AbsTcEscSetting": "Track", "CrossWeight": "50.0%", "NoseWeight": "43.6%"},
        "RightFront": {"CornerWeight": "3281 N", "RideHeight": "77.1 mm", "SpringRate": "140 N/mm", "BumpStiffness": "+37 clicks", "ReboundStiffness": "+3 clicks", "Camber": "-4.0 deg"},
        "RightRear": {"CornerWeight": "4237 N", "RideHeight": "102.3 mm", "SpringRate": "228 N/mm", "BumpStiffness": "+25 clicks", "ReboundStiffness": "+25 clicks", "Camber": "-2.8 deg", "ToeIn": "+0.1 mm"},
        "Rear": {"FuelLevel": "58.0 L", "ArbBlades": "1", "WingAngle": "2.5 deg"},
    },
}


def _yaml_block(d, ind=1):
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(" " * ind + f"{k}:")
            out.extend(_yaml_block(v, ind + 1))
        else:
            out.append(" " * ind + f"{k}: {v}")
    return out


def session_text(setup=None, session="Practice", car="Mclaren 570s GT4", car_path="mclaren570sgt4", car_id=135,
                 track="Zolder Circuit", track_code="zolder gp", track_id=200, surface_temp=22.2, wet=False,
                 name="Swazi Williams", setup_name="baseline.sto", other_cars=()):
    sessions = session if isinstance(session, (list, tuple)) else [session]
    lines = ["---", "WeekendInfo:", f" TrackName: {track_code}", f" TrackID: {track_id}", " TrackLength: 3.98 km",
             f" TrackDisplayName: {track}", " TrackConfigName: Grand Prix", f" TrackSurfaceTemp: {surface_temp:.2f} C",
             " TrackAirTemp: 18.00 C", " WeekendOptions:", f"  TrackWater: {'Wet' if wet else 'Dry'}",
             "SessionInfo:", " Sessions:"]
    for i, s in enumerate(sessions):
        lines += [f" - SessionNum: {i}", f"   SessionType: {s}", f"   SessionName: {s.upper()}"]
    lines += ["DriverInfo:", " DriverCarIdx: 3", f" DriverSetupName: {setup_name}", " Drivers:"]
    for idx, (nm, cn, cp, ci) in enumerate([("Other Driver: Jr", "Ligier JS P320", "ligierjsp320", 128)] + list(other_cars)):
        lines += [f" - CarIdx: {idx}", f"   UserName: {nm}", f"   CarScreenName: {cn}", f"   CarPath: {cp}", f"   CarID: {ci}",
                  "   CarClassShortName: LMP3"]
    lines += [" - CarIdx: 3", f"   UserName: {name}", f"   CarScreenName: {car}", f"   CarPath: {car_path}",
              f"   CarID: {car_id}", "   CarClassShortName: GT4 Class"]
    lines += ["CarSetup:", " UpdateCount: 3"] + _yaml_block(setup if setup is not None else SETUP_GT4)
    return "\n".join(lines) + "\n...\n"


def make(path, laps, setup=None, session="Practice", rate=60, lead=5.0, offs=(), pits=(), incidents=(), untimed=(),
         corner=None, brake_at=None, **kw):
    """A file of consecutive laps (seconds each) after a `lead`-second out lap. offs/pits/incidents/untimed are lap
    indexes (0 = first timed lap). corner: optional function (lap_index, pct) -> (speed m/s, brake 0..1) to shape a
    lap; default is a simple two-corner lap. Extra channels: tyre carcass temps change only in pits."""
    times = [lead] + list(laps)
    n = int(sum(times) * rate) + 4 * rate
    lap = np.zeros(n, np.int32)
    pct = np.zeros(n, np.float32)
    last = np.full(n, -1.0, np.float32)
    pit = np.zeros(n, bool)
    surf = np.full(n, ibt.ON_TRACK, np.int32)
    inc = np.zeros(n, np.int32)
    spd = np.zeros(n, np.float32)
    brk = np.zeros(n, np.float32)
    thr = np.zeros(n, np.float32)
    steer = np.zeros(n, np.float32)
    rh = np.full(n, 0.077, np.float32)
    t0 = 0
    shown = -1.0
    for i, T in enumerate(times):
        k = int(round(T * rate))
        seg = slice(t0, min(n, t0 + k))
        m = seg.stop - seg.start
        p = (np.arange(m) / k) if i > 0 else (0.6 + 0.4 * np.arange(m) / k)
        lap[seg] = i
        pct[seg] = p
        li = i - 1
        for j in range(m):
            if corner is not None and li >= 0:
                s, b = corner(li, float(p[j]))
            else:
                s = 55 - 30 * max(0.0, math.cos((p[j] - 0.3) * 20)) * (0.25 < p[j] < 0.36) - 25 * max(0.0, math.cos((p[j] - 0.7) * 20)) * (0.65 < p[j] < 0.76)
                b = 1.0 if (0.22 < p[j] < 0.27 or 0.62 < p[j] < 0.67) else 0.0
            spd[seg.start + j] = s
            brk[seg.start + j] = b
            thr[seg.start + j] = 0.0 if b > 0 else 1.0
        if li in offs:
            surf[seg.start + m // 2: seg.start + m // 2 + rate] = ibt.OFF_TRACK
        if li in pits:
            pit[seg.start + m - 3 * rate: seg.stop] = True
        if li in incidents:
            inc[seg.start + m // 2:] += 2
        last[seg] = shown
        if i > 0:
            shown = -1.0 if li in untimed else float(T)
            # iRacing updates LapLastLapTime about 2 s after the line
            last[seg.stop: seg.stop + 2 * rate] = last[seg.start]
            last[seg.stop + 2 * rate:] = shown
        t0 = seg.stop
    if t0 < n:
        lap[t0:] = len(times)
        pct[t0:] = np.arange(n - t0) / (rate * 100.0)
    sess = np.zeros(n, np.int32)
    cols = {"Lap": ("i", lap), "LapDistPct": ("f", pct), "LapLastLapTime": ("f", last), "OnPitRoad": ("b", pit),
            "PlayerTrackSurface": ("i", surf), "PlayerCarMyIncidentCount": ("i", inc), "IsOnTrack": ("b", np.ones(n, bool)),
            "SessionNum": ("i", sess), "SessionTime": ("d", np.arange(n) / rate), "Speed": ("f", spd),
            "Brake": ("f", brk), "Throttle": ("f", thr), "SteeringWheelAngle": ("f", steer),
            "LFrideHeight": ("f", rh), "RFrideHeight": ("f", rh), "LRrideHeight": ("f", rh + 0.025), "RRrideHeight": ("f", rh + 0.025),
            "CarLeftRight": ("i", np.ones(n, np.int32))}
    for c in ("LF", "RF", "LR", "RR"):
        for z in ("CL", "CM", "CR"):
            cols[f"{c}temp{z}"] = ("f", np.full(n, 70.0, np.float32))
        cols[f"{c}pressure"] = ("f", np.full(n, 188.0, np.float32))
    ibt.write_fake(path, session_text(setup=setup, session=session, **kw), cols, tick_rate=rate)
    return path
