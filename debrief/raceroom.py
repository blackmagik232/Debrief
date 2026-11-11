"""Race Room (2.30 "Endurance Race Room"): the team-race page for the driver AND for the teammates who are not driving.

Nothing here talks to the network or to iRacing: it is fed the readings livedata.LiveReader gives (your own channels, the
CarIdx arrays of every car, the session text) and works out what the page shows. Every teammate's Debrief reads iRacing on
their own PC; there is no relay. What each PC can know:

  driver  (in the car)   everything the crew chief knows: real fuel, pit service, tyres, incidents
  crew    (in the session, not in the car)  iRacing sends every car's CarIdx data, but NOT the own-car channels (fuel,
          service) of a car you are not driving. So the crew PC sees position, laps, gaps, pit road for every car, and
          ESTIMATES the team car's fuel: fuel after the last stop (the plan's fill, or the tank) less the laps since
          that stop x the burn. That figure is always labelled "estimated", on screen and in anything spoken, and it
          never produces a spoken box or fuel call on a crew PC (text only).

Parts, each a small class or function you can test alone:
  detect_role        driving / crew / spectator, and the team (TeamID from DriverInfo, never a name)
  PlanView           the saved 2.26 stint plan for this track, as the live race needs it (stint at a stop, laps expected at
                     a time, the plan delta, the next driver, fair share)
  StopTracker        every car's pit stops from CarIdxOnPitRoad (never from a slow lap; a caution is only a note on a stop)
  CrewFuel           the estimated fuel
  swap_alert         "Your stint starts in about five minutes", in the driver's own time
  handover_card      what the next driver needs when the car pits for a swap
  RaceRoom           puts it together: pre() before the crew chief reads a sample (it gives the crew chief the sample to use),
                     post() after, state() for the page
"""
from __future__ import annotations

import math
import time
from collections import deque

from . import crewchief as cc
from . import endurance as E
from . import replan as RP
from . import units as _units
from . import voicepack as vp

_num, _int, _at, _median = cc._num, cc._int, cc._at, cc._median

VERSION = 1
ROLE_HOLD_S = 2.0
PLAN_RELOAD_S = 30.0
RESYNC_S = 20.0               # a new role must hold this long before the page switches (climbing in/out takes a second)
STOP_MIN_LANE_S = 20.0          # without the track-surface channel a pit road visit this long counts as a stop
ALERT_FAR_S = 15 * 60.0
ALERT_NEAR_S = 5 * 60.0
SWAP_STALL_FRAC = 0.8           # names unknown: a stop at the plan's stop counts as a swap only with a stall of at least this share of iRacing's swap timer
REPEAT_GUARD_S = 15 * 60.0      # after a "five minutes" alert, no new alert for the same driver for this long
ALERT_MIN_S = 60.0              # no swap alert is made with less than this to go
ALERT_SLACK_S = 30.0            # an alert is made a little early rather than missed between two readings
STATE_EVERY_S = 0.5             # the page's picture is rebuilt at most this often (and on events)
FRAME_EVERY_S = 1.0             # the race log: one frame a second
HANDOVER_KEEP_S = 300.0
MAX_LAP_ROWS = 600
MAX_STOP_ROWS = 200
CLASS_CHART = 10
CRASH_FLAGS = cc.SLOW_FLAGS
FUEL_KINDS = ("fuel_critical", "box_now", "box_soon", "save_or_box", "caution_box", "fuel_tight")
CREW_NO_VOICE = "crew: text only"

SWAP_FAR_SAY = "Your stint starts in about fifteen minutes."
SWAP_NEAR_SAY = "Your stint starts in about five minutes."


# ---------------------------------------------------------------- who this PC's user is in the plan
IAM_FILE = "_me.json"


def _iam_path(library_folder):
    from pathlib import Path
    return Path(library_folder) / E.FOLDER / IAM_FILE


def load_iam(library_folder) -> str:
    """The plan driver this PC's user said they are ("" when never said). Never raises."""
    import json
    try:
        d = json.loads(_iam_path(library_folder).read_text(encoding="utf-8"))
        return str(d.get("name") or "")[:40] if isinstance(d, dict) else ""
    except (OSError, ValueError):
        return ""


def save_iam(library_folder, name) -> str:
    import json
    from . import fsutil
    name = str(name or "").strip()[:40]
    fsutil.atomic_write_text(_iam_path(library_folder), json.dumps({"name": name}), mkdir=True)
    return name


# ---------------------------------------------------------------- names
def _words(s):
    import re
    return [w for w in re.split(r"[^a-z0-9]+", str(s or "").lower()) if w]


def match_name(names, who):
    """The entry of `names` (the plan's driver names) that is `who` (an iRacing user name), or None. Exact match first, then
    the same words, then a first name or a surname that only one entry has. Never guesses between two."""
    who_l = str(who or "").strip().lower()
    if not who_l:
        return None
    for n in names:
        if n.lower() == who_l:
            return n
    ww = set(_words(who))
    hits = [n for n in names if _words(n) and set(_words(n)) <= ww or ww and ww <= set(_words(n))]
    if len(hits) == 1:
        return hits[0]
    hits = [n for n in names if ww & set(w for w in _words(n) if len(w) > 2)]
    return hits[0] if len(hits) == 1 else None


def initials(name) -> str:
    w = _words(name)
    if not w:
        return "?"
    return (w[0][0] + (w[-1][0] if len(w) > 1 else "")).upper()


def clock_text(s) -> str:
    """Seconds as m:ss or h:mm:ss."""
    if s is None or s != s:
        return "--"
    s = max(0, int(round(s)))
    h, m = s // 3600, s % 3600 // 60
    return f"{h}:{m:02d}:{s % 60:02d}" if h else f"{m}:{s % 60:02d}"


def local_time(wall_ts, shift_h=0.0) -> str:
    """HH:MM on this PC's clock, moved by shift_h hours (another driver's clock)."""
    lt = time.localtime(wall_ts)
    off = getattr(lt, "tm_gmtoff", 0) or 0
    return time.strftime("%H:%M", time.gmtime(wall_ts + off + shift_h * 3600.0))


# ---------------------------------------------------------------- role
def parse_info(sections) -> dict:
    """The session text -> {"entries": {CarIdx: [entry...]}, "me": entry or None, "team_id", "team_name", "team_cars": [CarIdx],
    "car_idx", "class_of", "pace", "spectators", "tank", "track", "car", "team_racing", "session_len_s"}."""
    di = (sections or {}).get("DriverInfo") or {}
    wi = (sections or {}).get("WeekendInfo") or {}
    entries: dict = {}
    pace, spec, class_of = set(), set(), {}
    for d in di.get("Drivers") or []:
        i = _int(d.get("CarIdx")) if isinstance(d, dict) else None
        if i is None:
            continue
        entries.setdefault(i, []).append(d)
        class_of[i] = d.get("CarClassID")
        if d.get("CarIsPaceCar") in (1, True):
            pace.add(i)
        if d.get("IsSpectator") in (1, True):
            spec.add(i)
    my_car = _int(di.get("DriverCarIdx"))
    uid = _int(di.get("DriverUserID"))
    me = None
    if uid is not None:
        me = next((e for es in entries.values() for e in es if _int(e.get("UserID")) == uid), None)
    car_es = entries.get(my_car) or []
    if me is None and uid is None and car_es:
        me = car_es[0]              # no user id in the session text: the car's first entry (a solo driver's own)
    team_id = _int(me.get("TeamID")) if me else None
    if not team_id and car_es:
        # this PC's user is not listed (only the driver in the car is): the team is the car's team
        ids = {_int(e.get("TeamID")) for e in car_es}
        team_id = ids.pop() if len(ids) == 1 else 0
    team_id = team_id if team_id and team_id > 0 else 0
    team_cars = []
    if team_id:
        for i, es in sorted(entries.items()):
            if i in pace or i in spec:
                continue
            if any(_int(e.get("TeamID")) == team_id for e in es):
                team_cars.append(i)
    car_idx = my_car if my_car in team_cars or (not team_id and my_car is not None) else (team_cars[0] if team_cars else my_car)
    mx, pct = _num(di.get("DriverCarFuelMaxLtr")), _num(di.get("DriverCarMaxFuelPct"))
    sec_len = None
    try:
        sess = (sections.get("SessionInfo") or {}).get("Sessions") or []
        for s in sess:
            if "race" in str(s.get("SessionType") or s.get("SessionName") or "").lower():
                txt = str(s.get("SessionTime") or "")
                v = _num(txt.split()[0]) if txt and txt.split() and txt.split()[0] != "unlimited" else None
                sec_len = v if v and v > 0 else None
    except (AttributeError, TypeError, ValueError, IndexError):
        pass
    return {"entries": entries, "me": me, "team_id": team_id, "team_name": str((me or {}).get("TeamName") or ""),
            "team_cars": team_cars, "car_idx": car_idx, "class_of": class_of, "pace": pace, "spectators": spec,
            "tank": (mx * (pct if pct else 1.0)) if mx else None,
            "track": str(wi.get("TrackDisplayName") or wi.get("TrackName") or ""),
            "car": str((me or {}).get("CarScreenName") or ""), "team_racing": wi.get("TeamRacing") in (1, True),
            "session_len_s": sec_len, "sub": wi.get("SubSessionID"), "my_name": str((me or {}).get("UserName") or "")}


def detect_role(own, info) -> dict:
    """Who this PC is in the session right now. driving: IsOnTrack (in the car, on track or in the pits). Otherwise a
    member of a team in the session (TeamID from DriverInfo) is crew; someone with a car of their own who isn't in it is a
    driver who is waiting; anyone else is a spectator."""
    own = own if isinstance(own, dict) else {}
    info = info if isinstance(info, dict) else {}
    on = own.get("IsOnTrack")
    driving = bool(on) and on not in (0, "0")
    lap_status = _int(own.get("DCLapStatus"))
    base = {"team_id": info.get("team_id") or 0, "team_name": info.get("team_name") or "", "car_idx": info.get("car_idx"),
            "me": info.get("my_name") or "", "dc_lap_status": lap_status, "drivers_so_far": _int(own.get("DCDriversSoFar"))}
    if not info.get("entries"):
        return dict(base, role="none", driving=False, reason="no session text yet")
    if driving:
        return dict(base, role="driver", driving=True, reason="you are in the car")
    if info.get("team_id") and info.get("team_cars"):
        return dict(base, role="crew", driving=False, car_idx=info["car_idx"], reason="you are on a team and not in the car")
    me = info.get("me") or {}
    if me and _int(me.get("IsSpectator")) not in (1,):
        return dict(base, role="driver", driving=False, reason="you are not in the car yet")
    return dict(base, role="spectator", driving=False, reason="you are watching and not on a team")


# ---------------------------------------------------------------- the saved plan
class PlanView:
    """The 2.26 stint plan for this track in the shape the race needs. PlanView(saved) with endurance.load_inputs' answer;
    raises ValueError when the plan can't be read."""

    def __init__(self, saved: dict):
        self.inputs = saved["inputs"]
        self.title = saved.get("title") or ""
        self.saved = saved.get("saved") or ""
        self.cfg = E._read(self.inputs)
        self.sim = E.simulate(self.cfg)
        self.stints = self.sim["stints"]
        self.names = [d["name"] for d in self.cfg["drivers"]]
        self.by_name = {d["name"]: d for d in self.cfg["drivers"]}
        raw = self.inputs.get("drivers") or []
        self.tz = {}
        for i, d in enumerate(self.cfg["drivers"]):
            r = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
            self.tz[d["name"]] = _num(r.get("tz")) or 0.0
        self.total_laps = self.sim["laps"]
        self.total_s = self.cfg["minutes"] * 60.0 if self.cfg["minutes"] else self.sim["finish_s"]
        self.tank = self.cfg["tank"]

    def stint(self, k):
        return self.stints[k] if 0 <= k < len(self.stints) else None

    def expected_laps(self, elapsed_s) -> float:
        """Laps the plan has done at this race time (stops stand still)."""
        if not self.stints or elapsed_s <= self.stints[0]["start_s"]:
            return 0.0
        for st in self.stints:
            if elapsed_s <= st["end_s"]:
                span = max(1e-6, st["end_s"] - st["start_s"])
                return st["from_lap"] - 1 + st["laps"] * (elapsed_s - st["start_s"]) / span
            nxt = st.get("stop")
            if nxt is not None and elapsed_s < st["end_s"] + nxt["stop_s"]:
                return float(st["to_lap"])
        return float(self.total_laps)

    def time_at_laps(self, laps) -> float:
        """Race time at which the plan has done this many laps (inverse of expected_laps, on the driving part)."""
        for st in self.stints:
            if laps <= st["to_lap"]:
                if laps <= st["from_lap"] - 1:
                    return st["start_s"]
                return st["start_s"] + (st["end_s"] - st["start_s"]) * (laps - (st["from_lap"] - 1)) / max(1, st["laps"])
        return float(self.sim["finish_s"])

    def delta(self, progress_laps, elapsed_s) -> dict | None:
        """Ahead (+) or behind (-) the plan: laps, and seconds (how long ago/ahead the plan reached the laps done now)."""
        if progress_laps is None or elapsed_s is None or elapsed_s <= 0 or not self.stints:
            return None
        d_laps = progress_laps - self.expected_laps(elapsed_s)
        d_s = self.time_at_laps(progress_laps) - elapsed_s
        if abs(d_s) < 30 and abs(d_laps) < 0.5:
            state, txt = "on", "On plan"
        else:
            state = "ahead" if d_s > 0 else "behind"
            m, s = divmod(int(round(abs(d_s))), 60)
            span = f"{m} min {s:02d} s" if m else f"{s} s"
            txt = f"{'Ahead of' if d_s > 0 else 'Behind'} plan by {abs(d_laps):.1f} lap{'s' if abs(abs(d_laps) - 1) > 0.05 else ''} ({span})"
        return {"state": state, "laps": round(d_laps, 2), "seconds": round(d_s, 1), "text": txt}

    def fair_laps(self, total_laps=None) -> int:
        t = total_laps or self.total_laps
        n = max(1, len(self.names))
        return int(math.ceil(E.FAIR_SHARE * t / n - 1e-9)) if t else 0


# ---------------------------------------------------------------- every car's stops
class StopTracker:
    """Pit stops of every car from CarIdxOnPitRoad. A visit is a stop when the car sat in its stall (CarIdxTrackSurface == 1);
    without that channel, when it spent STOP_MIN_LANE_S on pit road. Driving through is a pass, not a stop. A slow lap never
    makes a stop; a caution is only a note on the stop (under_caution)."""

    def __init__(self, min_lane_s=STOP_MIN_LANE_S):
        self.min_lane_s = min_lane_s
        self.cars: dict = {}
        self.stops: dict = {}               # idx -> [stop dict]
        self.passes: dict = {}              # idx -> number of drive-throughs
        self.last_t = None

    def in_pit(self, idx) -> bool:
        c = self.cars.get(idx)
        return bool(c and c["in"])

    def feed(self, t, field, skip=(), caution=False) -> list:
        pit = field.get("CarIdxOnPitRoad")
        if not isinstance(pit, (list, tuple)):
            return []
        surf = field.get("CarIdxTrackSurface")
        has_surf = isinstance(surf, (list, tuple)) and len(surf) >= len(pit) and any(_int(x) == 1 or _int(x) == 3 for x in surf)
        lc = field.get("CarIdxLapCompleted") or []
        done = []
        for i, on in enumerate(pit):
            if i in skip:
                continue
            on = bool(on)
            c = self.cars.get(i)
            if c is None:
                self.cars[i] = c = {"in": on, "t_in": None, "lap_in": None, "stall": 0.0, "since": None, "seen": False,
                                    "caution": False}
                if on:
                    c["partial"] = True
                continue
            stalled = has_surf and _int(_at(surf, i)) == 1
            if on and not c["in"]:
                c.update({"in": True, "t_in": t, "lap_in": _int(_at(lc, i)), "stall": 0.0, "since": None, "seen": False,
                          "caution": bool(caution), "partial": False})
            if c["in"]:
                if stalled:
                    c["seen"] = True
                    if c["since"] is None:
                        c["since"] = t
                elif c["since"] is not None:
                    c["stall"] += max(0.0, t - c["since"])
                    c["since"] = None
            if not on and c["in"]:
                if c["since"] is not None:
                    c["stall"] += max(0.0, t - c["since"])
                lane = (t - c["t_in"]) if c["t_in"] is not None else None
                real = c["seen"] if has_surf else (lane is not None and lane >= self.min_lane_s)
                if real and not c.get("partial"):
                    row = {"idx": i, "lap": (c["lap_in"] + 1) if c["lap_in"] is not None else None, "t_in": round(c["t_in"], 1), "t_out": round(t, 1),
                           "lane_s": round(lane, 1), "stall_s": round(c["stall"], 1), "under_caution": c["caution"]}
                    lst = self.stops.setdefault(i, [])
                    lst.append(row)
                    del lst[:-MAX_STOP_ROWS]
                    done.append(row)
                elif lane is not None and not c.get("partial"):
                    self.passes[i] = self.passes.get(i, 0) + 1
                c.update({"in": False, "since": None, "partial": False})
        self.last_t = t
        return done

    def count(self, idx) -> int:
        return len(self.stops.get(idx) or [])


# ---------------------------------------------------------------- estimated fuel (crew)
class CrewFuel:
    """Fuel of a car this PC can't read: the fuel after the last stop less the progress since it x the burn.

    Hand check: tank 80 L, the stop at the end of lap 12 filled to the top, burn 3.0 L a lap, 4 laps and 0.5 of a lap later
    (progress 16.5 against 12.0): 80 - 4.5 x 3.0 = 66.5 L, estimated."""

    def __init__(self):
        self.anchor = None            # {"progress", "fuel", "how"}

    def set(self, progress, fuel, how):
        self.anchor = {"progress": float(progress), "fuel": float(fuel), "how": how}

    def estimate(self, progress, burn):
        a = self.anchor
        if a is None or burn is None or progress is None:
            return None
        return max(0.0, a["fuel"] - max(0.0, progress - a["progress"]) * burn)

    @property
    def how(self):
        return self.anchor["how"] if self.anchor else "unknown"


# ---------------------------------------------------------------- swap help
def swap_alert(eta_s, fired: set, key) -> dict | None:
    """Which spoken swap alert is due, if any. eta_s: seconds until the next driver's stint starts. fired: the set of alerts
    already made for this swap (updated). Two alerts, one for each time: about fifteen minutes (made while the ETA is between
    10 and 15 minutes) and about five (between 0 and 5). Joining late never makes the fifteen after the five."""
    if eta_s is None or eta_s <= ALERT_MIN_S:
        return None                                  # too late to be any use (the car is at the stop): never made
    if eta_s <= ALERT_NEAR_S + ALERT_SLACK_S and (key, "near") not in fired:
        fired.add((key, "near"))
        fired.add((key, "far"))
        return {"kind": "swap_5", "say": SWAP_NEAR_SAY, "minutes": 5}
    if 10 * 60.0 < eta_s <= ALERT_FAR_S + ALERT_SLACK_S and (key, "far") not in fired:
        fired.add((key, "far"))
        return {"kind": "swap_15", "say": SWAP_FAR_SAY, "minutes": 15}
    return None


def handover_card(*, stop: dict | None, planned: dict | None, driver_from, driver_to, fuel_before=None, fuel_after=None,
                  tyres_flags=None, repair_left=None, fast_repairs=0, incidents=None, next_lap_s=None, next_burn=None,
                  unit="mph", measured=False) -> dict:
    """What the next driver needs when the car pits for a swap. Everything measured on this PC says so; anything taken from the
    plan says "plan"; anything this PC can't know is None and the page says "not known on this PC"."""
    fuel_add = None
    fuel_how = "unknown"
    if fuel_before is not None and fuel_after is not None and fuel_after >= fuel_before:
        fuel_add, fuel_how = fuel_after - fuel_before, "measured"
    elif planned and planned.get("fuel_add_l") is not None:
        fuel_add, fuel_how = planned["fuel_add_l"], "plan"
    tyres, tyres_how = None, "unknown"
    if tyres_flags is not None:
        tyres, tyres_how = bool(int(tyres_flags) & 0x0F), "measured"
    elif planned and planned.get("tyres") is not None:
        tyres, tyres_how = bool(planned["tyres"]), "plan"
    card = {"from": driver_from, "to": driver_to,
            "fuel_added": None if fuel_add is None else cc.fuel_pair(fuel_add, unit), "fuel_how": fuel_how,
            "fuel_in_tank": None if fuel_after is None else cc.fuel_pair(fuel_after, unit),
            "tyres": tyres, "tyres_how": tyres_how,
            "repairs": ("fast repair used" if fast_repairs else None) if repair_left in (None, 0) else
                       f"repairs left: {repair_left:.0f} s",
            "incidents": incidents, "target_lap": cc.lap_text(next_lap_s) if next_lap_s else "",
            "target_lap_s": next_lap_s, "burn": cc.fuel_pair(next_burn, unit) if next_burn else None,
            "burn_text": (cc.fuel_text(next_burn, unit, 2) + " a lap") if next_burn else "",
            "lane_s": (stop or {}).get("lane_s"), "stall_s": (stop or {}).get("stall_s"),
            "plan_stop_s": (planned or {}).get("stop_s")}
    bits = []
    if fuel_add is not None:
        bits.append(f"fuel {'+' if fuel_how == 'plan' else 'added '}{cc.fuel_text(fuel_add, unit)}"
                    + (" (plan)" if fuel_how == "plan" else ""))
    if tyres is not None:
        bits.append(("new tyres" if tyres else "same tyres") + (" (plan)" if tyres_how == "plan" else ""))
    card["text"] = ", ".join(bits)
    return card


# ---------------------------------------------------------------- the room
class RaceRoom:
    """pre(sample, field, sections, version, now, wall) -> the sample the crew chief should read;
    post(chief, now) after the crew chief has read it; state() is the page's picture; take_alerts() / take_events() hand
    on what was made since the last call. plan_for(track) -> endurance.load_inputs' answer or None; iam() -> the plan
    driver name of this PC's user (the Race Room page asks once) or ""."""

    def __init__(self, units="mph", plan_for=None, iam=None, log=None, wall=time.time):
        self.units = _units.norm(units)
        self.plan_for = plan_for
        self.iam = iam or (lambda: "")
        self.wall = wall
        self.log = log                    # callable(row) or None: the race log's sink
        self.info_v = None
        self.info: dict = {}
        self.role = {"role": "none", "driving": False}
        self._role_new = None
        self._role_since = None
        self.plan: PlanView | None = None
        self.plan_key = None
        self.plan_err = ""
        self.stops = StopTracker()
        self.fuel = CrewFuel()
        self.t = None
        self.field = None
        self._field_key = None
        self.race = False
        self.state_num = None
        self.session_num = None
        self.uid = None
        self.team_laps = 0
        self.team_pct = 0.0
        self.team_idx = None
        self.team_stops = 0
        self._k0 = 0                       # the plan stint the car was in when this PC first saw the race (joining late)
        self._eff = 0                      # stops since then that were stint stops of the plan (a driver change or fuel)
        self._seeded = False
        self.stint = None
        self.stints_done: list = []
        self.laps: deque = deque(maxlen=MAX_LAP_ROWS)
        self._lap_pending = None
        self._lap_flags = {"pit": False, "slow": False}
        self.pos_hist: dict = {}
        self.pos_laps: list = []
        self.swaps: list = []
        self.handover = None
        self.alert = None
        self.alerts_out: list = []
        self.fired: set = set()
        self.events: list = []
        self.cur_driver = None
        self.cur_driver_how = ""
        self.own_burn = None
        self._rv = cc.Rivals()
        self.rivals: list = []
        self.caution = False
        self.flags = 0
        self.last_state_now = -1e9
        self._state = {"version": VERSION, "state": "waiting", "text": "Waiting for iRacing."}
        self.replan_res = None
        self.replan_reason = ""
        self._replan_last = None
        self._replan_now = -1e9
        self.replan_err = ""
        self.delays: dict = {}
        self.last_real = None
        self._frame_t = -1e9
        self._hdr_done = False
        self.elapsed = None
        self.time_left = None
        self._chief = None
        self.crew_note = ""
        self.stint_changes = 0
        self._real_before_stop = None
        self.prev_driving = None
        self._inc0 = None
        self.dirty = True

    def _ev(self, row):
        """An event for the window / the coach log and for the race log."""
        self.events.append(row)
        if self.log is not None and row.get("k") not in ("lap",):
            self.log(row)

    # ------------------------------------------------------------ outside settings
    def force_replan(self):
        self._replan_last = None
        self._replan_now = -1e9
        self.dirty = True

    def set_delay(self, name, minutes):
        n = match_name(self.plan.names, name) if self.plan else None
        if n:
            if minutes and minutes > 0:
                self.delays[n] = float(minutes)
            else:
                self.delays.pop(n, None)
            self._replan_last = None
            self.dirty = True
        return n

    # ------------------------------------------------------------ session text and the plan
    def _set_info(self, sections, version):
        self.info = parse_info(sections)
        self.info_v = version
        self.uid = self.info.get("sub")
        track = self.info.get("track")
        if track and track != self.plan_key and self.plan_for is not None:
            self.plan_key = track
            self.plan, self.plan_err = None, ""
            try:
                saved = self.plan_for(track)
                if saved:
                    self.plan = PlanView(saved)
            except (ValueError, KeyError, TypeError) as e:
                self.plan_err = str(e)[:140]
            except Exception as e:  # noqa: BLE001
                self.plan_err = f"{e.__class__.__name__}"
        self._current_driver()
        self.dirty = True

    def _reload_plan(self):
        """The saved plan is read again every PLAN_RELOAD_S of the race clock: a change made mid-race (a driver swapped, a new fuel
        figure) takes effect, and the rest of the race is worked out again from it."""
        if self.plan_for is None or not self.plan_key or self.t is None:
            return
        if self._plan_check_t is not None and abs(self.t - self._plan_check_t) < PLAN_RELOAD_S:
            return
        self._plan_check_t = self.t
        try:
            saved = self.plan_for(self.plan_key)
        except Exception:  # noqa: BLE001
            return
        old = self.plan.inputs if self.plan else None
        if not saved or saved.get("inputs") == old:
            return
        try:
            new = PlanView(saved)
        except Exception:  # noqa: BLE001
            return
        self.plan, self.plan_err = new, ""
        self._seeded = False                      # the stints may be different: find the car's place in them again
        self._eff = 0
        self.force_replan()
        self.dirty = True

    _plan_check_t = None

    # ------------------------------------------------------------ where the car is in the plan
    @property
    def plan_k(self) -> int:
        """Index of the plan's stint the car is in now. NOT the number of stops this PC has seen: an unplanned stop (a repair,
        a stop-and-go) does not move it, and a PC that joins mid-race (or after a restart) starts from the race progress."""
        return self._k0 + self._eff

    def _seed_cursor(self):
        """Once per race on this PC: which plan stint the car is in, from the laps done and who is in the car."""
        if self._seeded or not self.plan or not self.plan.stints or self.team_idx is None or not self.race or self.state_num != 4:
            return
        self._seeded = True
        prog = self._progress()
        sts = self.plan.stints
        k = len(sts) - 1
        for i, x in enumerate(sts):
            if prog <= x["to_lap"]:
                k = i
                break
        who = self.plan_name(self.cur_driver) if self.cur_driver and self.cur_driver_how in ("iRacing", "you") else None
        if who and sts[k]["driver"] != who:
            # the plan's driver for these laps is not the one in the car: the nearest stint of the one who is
            near = [i for i, x in enumerate(sts) if x["driver"] == who]
            if near:
                k = min(near, key=lambda i: abs(i - k))
        self._k0, self._eff = k, 0

    def _resync_cursor(self):
        """The driver iRacing (or this PC) says is in the car is not the plan's driver for stint plan_k and has not been for
        RESYNC_S: the team did something else than the plan (a skipped swap, a double stint). The cursor moves to the nearest
        stint of the driver who is in the car. Never during a stop or just after one (the name changes then)."""
        if not (self._seeded and self.plan and self.plan.stints and self.cur_driver and self.cur_driver_how in ("iRacing", "you")):
            self._mis_t = None
            return
        pn = self.plan_name(self.cur_driver)
        cur = self.plan.stint(self.plan_k)
        if pn is None or (cur is not None and cur["driver"] == pn):
            self._mis_t = None
            return
        recent = bool(self.swaps) and self.swaps[-1].get("t_out") is not None and self.t - self.swaps[-1]["t_out"] < 150.0
        if recent or (self.team_idx is not None and self.stops.in_pit(self.team_idx)):
            self._mis_t = None
            return
        if self._mis_t is None:
            self._mis_t = self.t
            return
        if self.t - self._mis_t < RESYNC_S:
            return
        prog = self._progress()
        sts = self.plan.stints
        cand = [i for i, x in enumerate(sts) if x["driver"] == pn]
        if not cand:
            return
        def gap(i):
            x = sts[i]
            return 0.0 if x["from_lap"] - 1 <= prog <= x["to_lap"] else min(abs(prog - (x["from_lap"] - 1)), abs(prog - x["to_lap"]))
        k = min(cand, key=lambda i: (gap(i), abs(i - self.plan_k)))
        self._k0 = k - self._eff
        self._mis_t = None
        self._ev({"k": "resync", "t": self.t, "driver": pn, "stint": k + 1})
        self.dirty = True

    _mis_t = None

    _mis2_t = None

    def _resync_by_role(self):
        """iRacing names nobody for the car, and the plan puts this PC's user in it, but this PC says he is NOT in the car (he is
        crew and stayed crew after the stop): the team did something else than the plan (a double stint). The stop that moved
        the cursor was a fuel stop. The cursor goes back to the nearest stint of someone else, and the stint that was opened
        for the plan's guess is joined to the one before it again."""
        me = self.me_plan() if self.plan else None
        if not (me and self.plan.stints and self._seeded and self.cur_driver_how == "plan" and self.role.get("role") == "crew"
                and not self.role.get("driving")):
            self._mis2_t = None
            return
        cur = self.plan.stint(self.plan_k)
        if cur is None or cur["driver"] != me or (self.team_idx is not None and self.stops.in_pit(self.team_idx)) or \
                (self.swaps and self.swaps[-1].get("t_out") is not None and self.t - self.swaps[-1]["t_out"] < 150.0):
            self._mis2_t = None
            return
        if self._mis2_t is None:
            self._mis2_t = self.t
            return
        if self.t - self._mis2_t < RESYNC_S:
            return
        self._mis2_t = None
        prog = self._progress()
        sts = self.plan.stints
        cand = [i for i, x in enumerate(sts) if x["driver"] != me]
        if not cand:
            return

        def gap(i):
            x = sts[i]
            return 0.0 if x["from_lap"] - 1 <= prog <= x["to_lap"] else min(abs(prog - (x["from_lap"] - 1)), abs(prog - x["to_lap"]))
        k = min(cand, key=lambda i: (gap(i), abs(i - self.plan_k)))
        self._k0 = k - self._eff
        st = self.stint
        if st is not None and st.get("how") == "plan" and self.plan_name(st.get("driver")) == me and self.stints_done \
                and self.stints_done[-1].get("why") == "swap (plan)" and self.plan_name(self.stints_done[-1].get("driver")) != me:
            prev = self.stints_done.pop()
            self.stint = {x: y for x, y in prev.items() if x not in ("end_t", "end_lap", "laps", "why")}
            self.swaps[-1]["counted"], self.swaps[-1]["swap"], self.swaps[-1]["to"] = False, None, None
        self._ev({"k": "resync", "t": self.t, "driver": (sts[k] or {}).get("driver"), "stint": k + 1, "why": "not in the car"})
        self.dirty = True

    def _near_planned_stop(self, k=None, prog=None) -> bool:
        """The car is at (or has passed a little) the plan's stop for stint k: within 30% of the stint or 3 laps."""
        st = self.plan.stint(self.plan_k if k is None else k) if self.plan else None
        if not st or not st.get("stop"):
            return False
        prog = self._progress() if prog is None else prog
        return abs(prog - st["to_lap"]) <= max(3.0, 0.3 * st["laps"])

    def _current_driver(self):
        """Who is in the team car: the one name iRacing lists for it, else (when this PC drives) this PC's user, else the
        plan's driver for the stint, labelled as planned."""
        idx = self.team_idx if self.team_idx is not None else self.info.get("car_idx")
        es = self.info.get("entries", {}).get(idx) or []
        names = sorted({str(e.get("UserName") or "") for e in es if e.get("UserName")})
        who, how = None, ""
        if self.role.get("driving"):
            who, how = self.info.get("my_name") or (names[0] if len(names) == 1 else None) or self._learned or None, "you"
            if len(names) == 1 and not self.info.get("my_name"):
                self._learned = names[0]
        elif len(names) == 1:
            who, how = names[0], "iRacing"
        if who is None and self.plan:
            st = self.plan.stint(self.plan_k)
            if st:
                who, how = st["driver"], "plan"
        if who != self.cur_driver or how != self.cur_driver_how:
            old = self.cur_driver
            self.cur_driver, self.cur_driver_how = who, how
            if old and who and old != who and how != "plan" and self.stint is not None and not self._same_driver(self.stint.get("driver"), who):
                self._new_stint("driver change")
                self._driver_changed_late()
            elif old and who and self.stint is not None and how != "plan" and self.stint.get("how") == "plan" and self._same_driver(self.stint.get("driver"), who):
                self.stint["driver"], self.stint["how"] = who, how

    def _same_driver(self, a, b) -> bool:
        if not a or not b:
            return False
        return a == b or (self.plan_name(a) is not None and self.plan_name(a) == self.plan_name(b))

    def _driver_changed_late(self):
        """The name in the car changed just after a stop that looked like a repair or a splash: it was a stint stop after all."""
        if self.swaps and self.t is not None and self.swaps[-1].get("t_out") is not None and self.t - self.swaps[-1]["t_out"] <= 120.0 \
                and not self.swaps[-1].get("counted"):
            last = self.swaps[-1]
            last["counted"], last["swap"], last["to"] = True, True, self.cur_driver
            self._eff += 1
            self._anchor_after_stop(last.get("_k", self.plan_k - 1), last)

    def plan_name(self, who):
        """The plan's name for an iRacing user name (or the plan name itself)."""
        if not self.plan or not who:
            return None
        return match_name(self.plan.names, who)

    def set_iam(self, name):
        self._iam_val, self._iam_t = str(name or ""), self.t
        self._alerts_reset()

    @property
    def _near_said(self) -> dict:
        d = self.__dict__.get("_near_said_d")
        if d is None:
            d = self.__dict__["_near_said_d"] = {}
        return d

    def _alerts_reset(self):
        self.fired = set()
        self.alert = None
        self.dirty = True

    _iam_val = None
    _iam_t = None

    def me_plan(self):
        """This PC's user as the plan names them: what they said (Race Room asks once), else their iRacing name matched to the
        plan's names. The saved answer is looked at again every five seconds of the race clock."""
        if self._iam_val is None or self.t is None or self._iam_t is None or self.t - self._iam_t >= 5.0:
            try:
                self._iam_val = self.iam() or ""
            except Exception:  # noqa: BLE001
                self._iam_val = ""
            self._iam_t = self.t if self.t is not None else 0.0
        n = self._iam_val
        if n and self.plan and n in self.plan.names:
            return n
        return self.plan_name(self.my_name()) if self.info else None

    def my_name(self):
        return (self.info.get("my_name") if self.info else "") or self._learned or ""

    # ------------------------------------------------------------ role
    def _role(self, own, now):
        r = detect_role(own, self.info)
        cur = self.role.get("role")
        if r["role"] == cur:
            self._role_new = None
            self.role = dict(r)
            return
        if cur == "none":
            self.role = dict(r)
            return
        if self._role_new != r["role"]:
            self._role_new, self._role_since = r["role"], now
            self.role = dict(self.role, **{k: v for k, v in r.items() if k not in ("role", "driving", "reason")})
            return
        if now - self._role_since >= ROLE_HOLD_S:
            self.role = dict(r)
            self._role_new = None
            self._on_role_change()

    def _on_role_change(self):
        self.dirty = True
        self._ev({"k": "role", "t": self.t, "role": self.role.get("role")})
        if self.role.get("driving"):
            self.alert = None
        st = self.stint
        if st is not None and bool(st.get("mine")) != bool(self.role.get("driving")):
            at_stop = self.team_idx is not None and self.stops.in_pit(self.team_idx)
            just_out = bool(self.swaps) and self.t is not None and self.swaps[-1].get("t_out") is not None and \
                self.t - self.swaps[-1]["t_out"] <= 90.0
            if self.t is not None and (self.t - st["start_t"] < 180.0 or at_stop or just_out):
                st["mine"] = bool(self.role.get("driving"))         # the swap that just happened (or is happening): the same stint
                if at_stop or just_out:
                    self._flip_t = self.t                           # a driver change at a stop: the stop opens the next stint
            else:
                self._new_stint("you got in" if self.role.get("driving") else "you got out")
        self._current_driver()
        if self.stint is not None and self.cur_driver and self.role.get("driving"):
            # only a PC that knows who drove (it was this PC's user) names a stint; a plan name never renames an open stint
            self.stint["driver"], self.stint["how"] = self.cur_driver, self.cur_driver_how

    # ------------------------------------------------------------ stints
    def _progress(self):
        return self.team_laps + self.team_pct

    def _inc_now(self, own):
        v = _int(own.get("PlayerCarTeamIncidentCount")) if isinstance(own, dict) else None
        return v

    def _open_stint(self, t, own=None, driver=None):
        n = len(self.stints_done) + 1
        self.stint = {"n": n, "driver": driver or self.cur_driver, "how": self.cur_driver_how, "start_t": t,
                      "start_lap": self.team_laps, "start_progress": self._progress(),
                      "inc0": self._inc_now(own) if own is not None else self._inc0, "mine": bool(self.role.get("driving")),
                      "fuel0": self.last_real["fuel"] if (self.last_real and self.role.get("driving")) else None}
        self.stint["partial"] = t is not None and self.team_laps > 0 and not self.stints_done
        self.stint_changes += 1
        if self.swaps and abs(t - self.swaps[-1]["t_out"]) <= 60.0 and self.swaps[-1].get("swap") is None:
            self.swaps[-1]["swap"] = True
            self.swaps[-1]["to"] = self.stint["driver"]
        self._ev({"k": "stint", "t": t, "n": n, "driver": self.stint["driver"], "lap": self.team_laps})

    def _new_stint(self, why, own=None, driver=None):
        self._close_stint(why)
        if self.race and self.t is not None:
            self._open_stint(self.t, own, driver)

    def _close_stint(self, why):
        s = self.stint
        if s is None:
            return
        s = dict(s, end_t=self.t, end_lap=self.team_laps, laps=self.team_laps - s["start_lap"], why=why)
        self.stints_done.append(s)
        del self.stints_done[:-60]
        self.stint = None
        self._ev({"k": "stint_end", "t": self.t, "n": s["n"], "laps": s["laps"], "why": why})

    # ------------------------------------------------------------ the sample
    def pre(self, s, field, sections, version, now, wall=None):
        """Called with every sample before the crew chief. Returns the sample the crew chief should read: this one when
        driving, else one made from the team car's CarIdx data and the estimated fuel."""
        if not isinstance(s, dict):
            return s
        if sections and version != self.info_v:
            self._set_info(sections, version)
        t = _num(s.get("SessionTime"))
        if t is None:
            return s
        self.t = t
        self._session(s)
        self._reload_plan()
        self._role(s, now)
        if field is not None and id(field) != self._field_key:
            self._field_key = id(field)
            self.field = field
            self._field(field, s, now)
        self.flags = int(_num(s.get("SessionFlags")) or 0)
        self.caution = bool(self.flags & cc.FULL_COURSE)
        if self.role.get("driving"):
            self._driving(s)
            self._est_fuel = None            # the gauge is real now: an old estimate must not be read as current
        crew = self.role.get("role") == "crew" and self.team_idx is not None and self.field is not None
        self.prev_driving = bool(self.role.get("driving"))
        if not crew:
            return s
        return self._crew_own(s)

    def _session(self, s):
        sn = _int(s.get("SessionNum"))
        uid = _int(s.get("SessionUniqueID"))
        if sn != self.session_num or (uid is not None and uid != self._uid_seen()):
            if self.session_num is not None:
                self._reset_session()
            self.session_num = sn
            self._uid = uid
        st = _int(s.get("SessionState"))
        typ = str(s.get("SessionType") or "")
        self.race = "race" in typ.lower()
        self.state_num = st
        rem = _num(s.get("SessionTimeRemain"))
        self.time_left = rem if rem is not None and 0 <= rem < 600000 else None
        total = self.info.get("session_len_s") or (self.plan.total_s if self.plan and self.plan.cfg["minutes"] else None)
        self.elapsed = (total - self.time_left) if (total and self.time_left is not None) else None

    _uid = None
    _learned = ""

    def _uid_seen(self):
        return self._uid

    def _reset_session(self):
        self.stops = StopTracker()
        self.fuel = CrewFuel()
        self.stint = None
        self.stints_done = []
        self.laps.clear()
        self.pos_hist, self.pos_laps = {}, []
        self.swaps, self.handover, self.alert = [], None, None
        self.fired = set()
        self.team_stops = 0
        self._k0, self._eff, self._seeded, self._mis2_t = 0, 0, False, None
        self.fuel_unsure = ""
        self.replan_res, self._replan_last = None, None
        self._hdr_done = False
        self._lap_pending = None
        self._rv.reset()
        self.delays = {}
        self.dirty = True

    def _driving(self, s):
        f = _num(s.get("FuelLevel"))
        if f is not None:
            self.last_real = {"fuel": f, "t": self.t, "progress": self._progress()}
        self._inc_last = self._inc_now(s)
        self._inc0 = self._inc_last if self._inc0 is None else self._inc0
        if self.team_idx is not None and self.stops.in_pit(self.team_idx):
            if self._real_before_stop is None and self._pit_fuel_mark is not None:
                self._real_before_stop = self._pit_fuel_mark
            self.ho_real = {"before": self._real_before_stop, "after": f, "t": self.t}
        else:
            self._pit_fuel_mark = f
            self._real_before_stop = None

    # ------------------------------------------------------------ the field (about 4 times a second)
    def _field(self, field, s, now):
        t = self.t
        skip = self.info.get("pace", set()) | self.info.get("spectators", set())
        idx = self.role.get("car_idx")
        if idx is None:
            idx = self.info.get("car_idx")
        self.team_idx = idx
        done = self.stops.feed(t, field, skip, bool(int(_num(s.get("SessionFlags")) or 0) & cc.FULL_COURSE))
        lc, pct = field.get("CarIdxLapCompleted"), field.get("CarIdxLapDistPct")
        lm, pm = _num(_at(lc, idx)), _num(_at(pct, idx))
        if lm is not None:
            lm = int(lm)
            prev = self.team_laps
            self.team_laps = lm
            self.team_pct = min(1.0, max(0.0, pm if pm is not None and pm >= 0 else 0.0))
            if self.race and lm > prev:
                self._lap_done(field, lm, now)
        if self._lap_pending is not None:
            self._lap_time(field)
        if self.stops.in_pit(idx):
            self._lap_flags["pit"] = True
        if self.flags & cc.SLOW_FLAGS or int(_num(s.get("SessionFlags")) or 0) & cc.SLOW_FLAGS:
            self._lap_flags["slow"] = True
        for row in done:
            if row["idx"] == idx:
                self._team_stop(row, s)
            if self.log is not None:
                self.log({"k": "stop", "t": t, **row})
        self._frame_log(field, s)
        if self.race and self.stint is None and self.state_num == 4 and self.team_idx is not None:
            self._open_stint(t, s)
        self._seed_cursor()
        self._current_driver()          # the plan's driver follows the cursor when iRacing names nobody
        self._resync_cursor()
        self._resync_by_role()
        self._fuel_anchor()

    def _lap_done(self, field, lap, now):
        self._lap_pending = {"lap": lap, "t": self.t, "last0": _num(_at(field.get("CarIdxLastLapTime"), self.team_idx)),
                             "flags": dict(self._lap_flags), "driver": self.cur_driver}
        self._lap_flags = {"pit": bool(self.stops.in_pit(self.team_idx)), "slow": False}
        cp = _int(_at(field.get("CarIdxClassPosition"), self.team_idx))
        self.pos_laps.append(lap)
        cars = self._class_cars(field)
        for i, p in cars.items():
            self.pos_hist.setdefault(i, []).append((lap, p))
        del self.pos_laps[:-400]
        for i in list(self.pos_hist):
            del self.pos_hist[i][:-400]
        self._lap_time(field, force=False)
        if cp:
            self._ev({"k": "lap", "t": self.t, "lap": lap, "cpos": cp})

    def _lap_time(self, field, force=True):
        p = self._lap_pending
        if p is None:
            return
        ll = _num(_at(field.get("CarIdxLastLapTime"), self.team_idx))
        changed = ll is not None and ll > 0 and (p["last0"] is None or abs(ll - p["last0"]) > 1e-6)
        if changed or (self.t - p["t"] > 3.0 and force):
            lt = ll if ll is not None and ll > 0 else None
            self.laps.append({"lap": p["lap"], "s": lt, "text": cc.lap_text(lt) if lt else "", "driver": p["driver"],
                              "initials": initials(p["driver"]), "pit": p["flags"]["pit"], "slow": p["flags"]["slow"],
                              "stint": len(self.stints_done) + 1 if self.stint else len(self.stints_done)})
            self._lap_pending = None
            if self.log is not None:
                self.log({"k": "teamlap", "t": self.t, "lap": p["lap"], "s": lt, "driver": p["driver"], "pit": p["flags"]["pit"],
                          "slow": p["flags"]["slow"]})

    def _class_cars(self, field):
        cp = field.get("CarIdxClassPosition") or []
        mine = self.info.get("class_of", {}).get(self.team_idx)
        pace = self.info.get("pace", set()) | self.info.get("spectators", set())
        out = {}
        for i, p in enumerate(cp if isinstance(cp, (list, tuple)) else []):
            p = _int(p)
            if not p or p <= 0 or i in pace:
                continue
            if mine is not None and self.info.get("class_of") and self.info["class_of"].get(i) != mine:
                continue
            out[i] = p
        return out

    # ------------------------------------------------------------ a stop of the team car
    def _driver_before(self):
        """The driver who brought the car in: the stint that closed within the last minute, else the one in the car."""
        if self.stints_done and self.t is not None and self.t - self.stints_done[-1]["end_t"] <= 60.0:
            return self.stints_done[-1]["driver"]
        return self.cur_driver

    def _team_stop(self, row, s):
        """The team car made a stop. It is a STINT STOP of the plan (the cursor moves, the fuel is filled) when the driver
        changed or fuel went in; an unplanned stop (a repair, a stop-and-go, a splash that was not needed) is shown as a stop
        and changes nothing else."""
        k = self.plan_k
        self.team_stops += 1
        planned = self.plan.stint(k) if self.plan else None
        pstop = (planned or {}).get("stop")
        nxt = self.plan.stint(k + 1) if self.plan else None
        prog = self._progress()
        names_known = self.cur_driver_how in ("iRacing", "you")
        t_in = row.get("t_in")
        flipped = self._flip_t is not None and self.t is not None and t_in is not None and t_in - 10.0 <= self._flip_t <= self.t + 1.0
        changed = bool(self.stints_done) and self.t is not None and self.t - self.stints_done[-1]["end_t"] <= 90.0 \
            and self.stints_done[-1].get("why") in ("driver change", "swap (plan)")
        hr = self.ho_real or {}
        measured = self.role.get("driving") and hr.get("before") is not None and hr.get("after") is not None
        tank = self._tank() or 0.0
        fuelled = bool(measured and hr["after"] - hr["before"] >= max(2.0, 0.05 * tank))
        near = self._near_planned_stop(k, prog)
        # 2.30 review: plan stop_s includes the pit lane, so a share of it sat above iRacing's own 30 s swap timer and a normal
        # swap was missed. The stall alone is compared, against a little under the swap timer.
        long_stall = bool(pstop and (row.get("stall_s") or 0.0) >= SWAP_STALL_FRAC * E.SWAP_S)
        if self.cur_driver_how == "iRacing":
            counted = changed                                  # iRacing names the driver: a stint stop only if he changed
        elif measured:
            counted = changed or flipped or fuelled
        else:
            counted = changed or flipped or (near and long_stall)   # no name to go by: this PC's own flip, or a stop near the plan's
                                                                    # stop that lasted as long as a swap does
        planned_swap = bool(pstop and pstop.get("swap"))
        rec = dict(row, n=self.team_stops, planned_swap=planned_swap, plan_stop_s=(pstop or {}).get("stop_s"),
                   plan_fuel_add=(pstop or {}).get("fuel_add_l"), swap=None, to=None, from_driver=self._driver_before(),
                   counted=counted, _k=k)
        self.swaps.append(rec)
        del self.swaps[:-60]
        if not counted and not self.role.get("driving"):
            self.fuel_unsure = f"after an unplanned stop on lap {self.team_laps}"   # fuel may have gone in: do not invent a fill
        if counted:
            self._eff += 1
            self._anchor_after_stop(k, rec)
            if (planned_swap or flipped) and self.cur_driver_how != "iRacing" and not changed:
                # iRacing doesn't name one driver for the car: the plan says this stop is a swap, and it is shown as planned
                self._new_stint("swap (plan)", s, (nxt or {}).get("driver"))
        self.dirty = True
        self._ev({"k": "team_stop", "t": self.t, **{x: y for x, y in rec.items() if x != "_k"}})

    def _anchor_after_stop(self, k, rec):
        """Fuel for the next stint after a stint stop: what the plan fills, else the full tank (estimated, and said so)."""
        planned = self.plan.stint(k) if self.plan else None
        pstop = (planned or {}).get("stop")
        prog = self._progress()
        est_before = self.fuel.estimate(prog, self._burn_for(self.cur_driver)) if self.fuel.anchor else None
        tank = self._tank()
        self.fuel_unsure = ""
        hr = self.ho_real or {}
        seen = hr.get("before") is not None and hr.get("after") is not None and hr.get("t") is not None and \
            rec.get("t_in") is not None and rec["t_in"] - 5.0 <= hr["t"] <= (rec.get("t_out") or hr["t"]) + 5.0
        if tank and seen and hr["after"] - hr["before"] >= max(2.0, 0.05 * tank):
            # this PC's user was in the car while the fuel went in: what the gauge last said is better than the plan's fill
            self.fuel.set(prog, min(tank, hr["after"]), "read at the stop")
        elif tank:
            if est_before is not None and pstop and pstop.get("fuel_add_l") is not None:
                self.fuel.set(prog, min(tank, est_before + pstop["fuel_add_l"]), "plan fill")
            else:
                self.fuel.set(prog, tank, "full tank")

    _pit_fuel_mark = None
    fuel_unsure = ""
    _flip_t = None
    ho_real = None

    # ------------------------------------------------------------ fuel
    def _tank(self):
        return self.info.get("tank") or (self._chief.tank if self._chief is not None and self._chief.tank else None) or \
            (self.plan.tank if self.plan else None)

    def _burn_for(self, who):
        """Burn a lap for a driver of the team: what this PC measured while its user drove (when that is who), else the plan's."""
        pn = self.plan_name(who) if who else None
        if self.role.get("driving") and self._chief is not None:
            b, how = self._chief.burn.burn()
            if b and how == "laps":
                self.own_burn = b
                return b
        if pn and self.plan:
            d = self.plan.by_name.get(pn)
            if d:
                return d["burn"] * (self._burn_ratio or 1.0)
        if self.own_burn:
            return self.own_burn
        if self.plan and self.plan.cfg["drivers"]:
            return self.plan.cfg["drivers"][0]["burn"]
        return None

    _burn_ratio = 1.0

    def _fuel_anchor(self):
        """The crew's fuel starts from the race start (plan or full tank), or from where the plan says the stint began."""
        if self.fuel.anchor is not None or self.team_idx is None or not self.race:
            return
        tank = self._tank()
        prog = self._progress()
        if self.plan and self.plan.stints:
            st = self.plan.stint(self.plan_k) or self.plan.stints[-1]
            if st is not None and self.state_num in (4, None) and self.plan_k == 0 and self.team_stops == 0 and prog < 1.0:
                self.fuel.set(prog, st["fuel_start_l"], "plan")
                return
            if st is not None:
                self.fuel.set(st["from_lap"] - 1, st["fuel_start_l"], "plan stint")
                return
        if tank and prog < 1.0:
            self.fuel.set(prog, tank, "full tank")

    # ------------------------------------------------------------ what the crew chief reads on a crew PC
    def _crew_own(self, s):
        f = self.field
        i = self.team_idx
        o = dict(s)
        pit = bool(_at(f.get("CarIdxOnPitRoad"), i))
        surf = _int(_at(f.get("CarIdxTrackSurface"), i))
        o.update({"PlayerCarIdx": i, "Lap": self.team_laps + 1, "LapDistPct": self.team_pct,
                  "LapLastLapTime": _num(_at(f.get("CarIdxLastLapTime"), i)) or 0.0, "OnPitRoad": pit,
                  "PlayerCarInPitStall": surf == 1, "IsOnTrack": True,
                  "PlayerCarClassPosition": _int(_at(f.get("CarIdxClassPosition"), i)) or 0,
                  "PlayerCarPosition": _int(_at(f.get("CarIdxPosition"), i)) or 0,
                  "CarLeftRight": None, "PlayerCarTowTime": 0.0, "Brake": 0.0, "Throttle": 0.0})
        burn = self._burn_for(self.cur_driver)
        est = self.fuel.estimate(self._progress(), burn)
        self._est_fuel = est
        o["FuelLevel"] = est if (est is not None and not self.fuel_unsure) else 9999.0       # unknown: no fuel call can be made from it
        return o

    _est_fuel = None

    # ------------------------------------------------------------ the race log (about once a second)
    def _frame_log(self, field, s):
        if self.log is None or not self.race or self.t - self._frame_t < FRAME_EVERY_S:
            return
        self._frame_t = self.t
        n = len(field.get("CarIdxLapCompleted") or [])
        if not self._hdr_done:
            self._hdr_done = True
            drv = []
            for i, es in sorted(self.info.get("entries", {}).items()):
                for e in es:
                    drv.append({"idx": i, "num": str(e.get("CarNumber") or ""), "name": str(e.get("UserName") or ""),
                                "class": e.get("CarClassID"), "team_id": _int(e.get("TeamID")) or 0,
                                "team": str(e.get("TeamName") or ""), "car": str(e.get("CarScreenName") or ""),
                                "pace": i in self.info.get("pace", set()) or i in self.info.get("spectators", set())})
            self.log({"k": "hdr", "v": 1, "track": self.info.get("track"), "sub": self.info.get("sub"),
                      "team_id": self.info.get("team_id"), "team_idx": self.team_idx, "role": self.role.get("role"),
                      "me": self.my_name(), "n": n, "drivers": drv, "wall": round(self.wall(), 1),
                      "plan": self.plan.inputs if self.plan else None, "plan_title": self.plan.title if self.plan else "",
                      "units": self.units})
        g = lambda k: field.get(k) or []        # noqa: E731
        r2 = lambda xs: [None if x is None else round(float(x), 3) for x in xs]       # noqa: E731
        pit = [i for i, v in enumerate(g("CarIdxOnPitRoad")) if v]
        stall = [i for i, v in enumerate(g("CarIdxTrackSurface")) if _int(v) == 1]
        self.log({"k": "f", "t": round(self.t, 1), "rem": self.time_left, "fl": self.flags,
                  "lc": [(_int(x) if _int(x) is not None else -1) for x in g("CarIdxLapCompleted")],
                  "lp": r2(g("CarIdxLapDistPct")), "po": [(_int(x) or 0) for x in g("CarIdxPosition")],
                  "cp": [(_int(x) or 0) for x in g("CarIdxClassPosition")], "ll": r2(g("CarIdxLastLapTime")),
                  "pit": pit, "sf": stall})
        if self.role.get("driving"):
            row = {"k": "o", "t": round(self.t, 1)}
            for key, name in (("fuel", "FuelLevel"), ("tow", "PlayerCarTowTime"), ("svfl", "PitSvFlags"),
                              ("inc", "PlayerCarTeamIncidentCount"), ("dcs", "DCDriversSoFar"), ("dcl", "DCLapStatus")):
                v = s.get(name)
                if v is not None:
                    row[key] = _num(v)
            for key, name in (("lf", "LFwearM"), ("rf", "RFwearM"), ("lr", "LRwearM"), ("rr", "RRwearM")):
                v = _num(s.get(name))
                if v is not None:
                    row[key] = round(v, 3)
            self.log(row)

    # ------------------------------------------------------------ after the crew chief
    def bind(self, chief):
        """On a crew PC the crew chief follows the TEAM car (its CarIdx), not the player's own slot."""
        self._chief = chief
        if chief is not None:
            est = self.role.get("role") == "crew"
            if getattr(chief, "fuel_estimated", False) != est:
                chief.fuel_estimated = est
                chief.fuel_source_changed()           # crew <-> driver: what was learned from the other kind of fuel is dropped
        if chief is not None and self.role.get("role") == "crew" and self.team_idx is not None:
            b = self._burn_for(self.cur_driver)
            if b:
                chief.burn.seed, chief.burn.seed_why = b, "estimated from the plan"       # the board's numbers, never learned
            chief.me = self.team_idx
            chief.my_class = chief.class_of.get(self.team_idx)

    def post(self, chief, now, s=None):
        """The crew chief has read the sample. Works out the rest of the page: rivals, plan delta, alerts, the re-plan."""
        self.bind(chief)
        if self.field is not None and chief is not None:
            self._rivals(chief, now)
        self._alerts(chief, s)
        self._maybe_replan(chief, now)
        if self.dirty or now - self.last_state_now >= STATE_EVERY_S:
            self.last_state_now = now
            self.dirty = False
            self._state = self._build(chief, now, s)
        return self._state

    def state(self):
        return self._state

    def take_alerts(self) -> list:
        out, self.alerts_out = self.alerts_out, []
        return out

    def take_events(self) -> list:
        out, self.events = self.events, []
        return out

    # ------------------------------------------------------------ rivals (the nearest cars of the class, with tags)
    def _rivals(self, chief, now):
        f, me = self.field, self.team_idx
        if me is None:
            return
        cars = self._class_cars(f)
        mypos = cars.get(me)
        if mypos is None:
            self.rivals = []
            return
        lc, pct = f.get("CarIdxLapCompleted"), f.get("CarIdxLapDistPct")
        lm, pm = _num(_at(lc, me)), _num(_at(pct, me))
        progress = (lm + pm) if lm is not None and pm is not None else None
        lap_ref = chief._lap_ref() if chief is not None else None
        if not lap_ref:
            lap_ref = (self.laps[-1]["s"] if self.laps and self.laps[-1]["s"] else None)
        by_pos = {p: i for i, p in cars.items()}
        pitf = f.get("CarIdxOnPitRoad") or []
        mypit = bool(_at(pitf, me))
        want = [(p, by_pos[p]) for p in range(max(1, mypos - 3), mypos + 4) if p in by_pos and p != mypos]
        if 1 in by_pos and by_pos[1] != me and (1, by_pos[1]) not in want:
            want.insert(0, (1, by_pos[1]))
        rows, keep = [], set()
        for p, idx in want:
            g = chief._gap(f, idx, me, lap_ref) if chief is not None else None
            if g is None:
                continue
            role = "ahead" if p < mypos else "behind"
            gap = abs(g)
            on_pit = bool(_at(pitf, idx))
            key = (idx, "x")
            keep.add(key)
            if progress is not None and not (self.caution or on_pit or mypit or self.flags & cc.SLOW_FLAGS):
                self._rv.sample(key, progress, gap)
            rate = self._rv.rate(key, progress) if progress is not None and not self.caution else None
            ltc = None
            if rate is not None and rate >= cc.RATE_MIN:
                ltc = max(0.0, (gap - cc.CONTACT_GAP_S) / rate)
            tags = []
            if not self.caution and not on_pit and not mypit:
                if gap < cc.ALONGSIDE_S:
                    tags.append("alongside")
                if role == "behind" and ltc is not None and ltc <= cc.CONTACT_LAPS:
                    tags.append("threat")
                if role == "ahead" and ltc is not None and ltc <= cc.CONTACT_LAPS:
                    tags.append("catchable")
                if rate is not None and rate <= -cc.AWAY_RATE:
                    tags.append("pulling away")
            if on_pit and not self.caution:
                tags.append("pitting")
            d = chief.drivers.get(idx) or {}
            rows.append({"idx": idx, "num": d.get("num", ""), "name": d.get("name", ""), "pos": p, "role": role,
                         "gap_s": round(gap, 2), "rate": None if rate is None else round(rate, 2),
                         "laps_to_contact": None if ltc is None else round(ltc, 1), "tags": tags,
                         "stops": self.stops.count(idx), "in_pit": on_pit,
                         "last": _num(_at(f.get("CarIdxLastLapTime"), idx)), "best": _num(_at(f.get("CarIdxBestLapTime"), idx))})
        self._rv.forget_except(keep)
        self.rivals = rows

    # ------------------------------------------------------------ alerts for the next driver
    def _rp_stints(self):
        """The rest of the race from the latest re-plan (it knows about late drivers, a caution, a different burn), as rows
        [{"driver", "laps", "start_in_s", "end_in_s", ...}] with the car's own stint first, or None to use the saved plan."""
        r = self.replan_res
        if not r or not r.get("from_state") or self._replan_t is None:
            return None
        rows = r.get("stints") or []
        return rows if len(rows) >= 2 else None

    _replan_t = None

    def next_stint(self):
        """(the stint after the one the car is in, whether a different driver takes it) for the stop the car makes next. Follows the
        latest re-plan when there is one (a driver marked late, a different burn), else the saved plan."""
        if not self.plan:
            return None, False
        rows = self._rp_stints()
        if rows is not None:
            cur, nxt = rows[0], rows[1]
            out = {"driver": nxt["driver"], "laps": nxt["laps"], "from_lap": nxt.get("from_lap"),
                   "start_s": (self._replan_el or 0.0) + nxt["start_in_s"], "src": "re-plan"}
            return out, nxt["driver"] != cur["driver"]
        cur = self.plan.stint(self.plan_k)
        nxt = self.plan.stint(self.plan_k + 1)
        if cur is None or nxt is None:
            return None, False
        return nxt, nxt["driver"] != cur["driver"]

    _replan_el = None

    def eta_to_stop(self, chief):
        """Seconds until the car is expected to pit for the next stop: the plan's stop lap if the car hasn't passed it, else the
        crew chief's latest box lap; at the pace of the last clean laps (the driver's plan pace until there are some)."""
        if not self.plan:
            return None
        rows = self._rp_stints()
        if rows is not None and self._replan_t is not None and self.t is not None:
            # the re-plan was made at race time _replan_t: its stint ends end_in_s after that
            return max(0.0, rows[0]["end_in_s"] - (self.t - self._replan_t))
        cur = self.plan.stint(self.plan_k)
        if cur is None or not cur.get("stop"):
            return None
        prog = self._progress()
        target = float(cur["to_lap"])
        if prog > target and chief is not None:
            lat = (chief.snap.get("box") or {}).get("lap_latest") if chief.snap else None
            target = float(lat) if lat else prog + 1.0
        clean = [r["s"] for r in list(self.laps)[-6:] if r["s"] and not r["pit"] and not r["slow"]]
        pace = _median(clean) if len(clean) >= 2 else None
        if pace is None:
            d = self.plan.by_name.get(cur["driver"])
            pace = d["lap_t"] if d else None
        if pace is None:
            return None
        return max(0.0, (target - prog) * pace)

    def _alerts(self, chief, s):
        nxt, swap = self.next_stint()
        me = self.me_plan()
        in_car = self.plan_name(self.cur_driver) if self.cur_driver and self.cur_driver_how in ("iRacing", "you") else None
        if not (nxt and swap and me and nxt["driver"] == me) or self.role.get("driving") or (in_car and in_car == me):
            self.alert = None
            self.eta_next = None
            return
        eta = self.eta_to_stop(chief)
        if eta is None:
            return
        key = (self.plan_k, nxt["driver"])       # one pair per driver per planned stint; never changes with stops seen
        last = self._near_said.get(nxt["driver"])
        if last is not None and self.t is not None and self.t - last < REPEAT_GUARD_S:
            for half in ("far", "near"):          # 2.30 review: the same takeover is never announced twice (a long repair
                self.fired.add((key, half))       # stall can move the cursor; his next real stint is a whole stint away)
        a = swap_alert(eta, self.fired, key)
        if a and a["kind"] == "swap_5" and self.t is not None:
            self._near_said[nxt["driver"]] = self.t
        wall = self.wall()
        at = local_time(wall + eta)
        self.eta_next = eta
        if a:
            a = dict(a, eta_s=round(eta, 0), at_local=at, driver=me, stop=self.team_stops + 1,
                     text=f"Your stint starts in about {a['minutes']} minutes (about {at} your time).")
            self.alert = a
            self.alerts_out.append(a)
            self._ev({"k": "swap_alert", "t": self.t, **{x: a[x] for x in ("kind", "eta_s", "driver")}})
            self.dirty = True
        elif self.alert is not None:
            self.alert = dict(self.alert, eta_s=round(eta, 0), at_local=at)

    eta_next = None

    # ------------------------------------------------------------ re-plan
    def _ratios(self, chief):
        """How the race is going against the plan for the driver in the car: lap time and fuel ratios (1.0 when not known)."""
        pn = self.plan_name(self.cur_driver) if self.cur_driver else None
        d = self.plan.by_name.get(pn) if pn and self.plan else None
        if d is None and self.plan:
            st = self.plan.stint(self.plan_k)
            d = self.plan.by_name.get(st["driver"]) if st else None
        pace = burn = 1.0
        if d is None:
            return pace, burn
        clean = [r["s"] for r in list(self.laps)[-8:] if r["s"] and not r["pit"] and not r["slow"]]
        if len(clean) >= 3:
            pace = min(1.5, max(0.7, _median(clean) / d["lap_t"]))
        if self.role.get("driving") and chief is not None:
            b, how = chief.burn.burn()
            if b and how == "laps":
                burn = min(2.0, max(0.5, b / d["burn"]))
        return pace, burn

    def _maybe_replan(self, chief, now):
        if not self.plan or not self.race or self.team_idx is None or chief is None or self.state_num != 4:
            return
        pace, burn = self._ratios(chief)
        self._burn_ratio = burn
        last = self._replan_last
        why = RP.drift({}, last, self.team_laps, self.caution, self.team_stops, pace, burn)
        if why is None or now - self._replan_now < 5.0:
            return
        self._replan_now = now
        st = chief.last_calc.get("ltg") if chief.last_calc else None
        fuel = chief.fuel if self.role.get("driving") else self._est_fuel
        sub = {"time_left_s": self.time_left, "crossings": st["crossings"] if st else None, "elapsed_s": self.elapsed or 0.0,
               "driver": self.plan_name(self.cur_driver) or ((self.plan.stint(self.plan_k) or {}).get("driver")),
               "fuel_l": fuel, "lap_f": self.team_pct, "pace_ratio": pace, "burn_ratio": burn,
               "stints_on_tyres": 0, "row_done": 0, "delays": dict(self.delays)}
        self._replan_last = {"lap": self.team_laps, "caution": self.caution, "stops": self.team_stops, "pace": pace, "burn": burn}
        try:
            res = RP.replan(self.plan.inputs, sub)
            res["why"], res["estimated_fuel"] = why, not self.role.get("driving")
            self.replan_res, self.replan_err = res, ""
            self._replan_t, self._replan_el = self.t, self.elapsed
        except ValueError as e:
            self.replan_res, self.replan_err = None, str(e)
        except Exception as e:  # noqa: BLE001
            self.replan_res, self.replan_err = None, e.__class__.__name__
        self.replan_reason = why
        self.dirty = True
        if self.log is not None and self.replan_res:
            self.log({"k": "replan", "t": self.t, "why": why, "laps": self.replan_res["laps"], "stops": self.replan_res["stops"],
                      "finish_s": self.replan_res["finish_s"]})

    # ------------------------------------------------------------ the page
    def _fair(self, chief):
        if not self.plan:
            return []
        done: dict = {}
        for s in self.stints_done + ([dict(self.stint, laps=self.team_laps - self.stint["start_lap"])] if self.stint else []):
            n = self.plan_name(s["driver"]) or s["driver"]
            done[n] = done.get(n, 0) + max(0, s.get("laps", 0))
        left = {}
        if self.replan_res:
            for st in self.replan_res["stints"]:
                left[st["driver"]] = left.get(st["driver"], 0) + st["laps"]
        st = chief.last_calc.get("ltg") if chief is not None and chief.last_calc else None
        total = self.team_laps + (st["crossings"] if st else 0) or self.plan.total_laps
        fair = self.plan.fair_laps(total)
        rows = []
        for n in self.plan.names:
            d, l = done.get(n, 0), left.get(n, 0)
            if not self.plan.cfg["fair_share"]:
                state, txt = "ok", ""
            elif d >= fair:
                state, txt = "ok", "fair share done"
            elif d + l >= fair:
                state, txt = "ok", f"needs {fair - d} more lap{'s' if fair - d != 1 else ''}"
            else:
                state, txt = "short", f"on course to be {fair - d - l} lap{'s' if fair - d - l != 1 else ''} short of the fair share"
            rows.append({"name": n, "laps": d, "planned_left": l, "fair": fair, "state": state, "text": txt})
        return rows

    def _build(self, chief, now, s):
        role = dict(self.role)
        snap = chief.snapshot() if chief is not None else {}
        crew = role.get("role") == "crew"
        sess = {"track": self.info.get("track"), "car": self.info.get("car"), "race": self.race, "time_left_s": self.time_left,
                "time_left_text": clock_text(self.time_left), "elapsed_s": self.elapsed, "team_laps": self.team_laps,
                "flag": cc.flags_text(self.flags), "caution": self.caution}
        st = {"version": VERSION, "state": "racing" if (self.race and self.state_num == 4) else "waiting",
              "role": role, "session": sess, "units": self.units}
        if role.get("role") in ("none",):
            st["state"], st["text"] = "waiting", "Waiting for iRacing."
        elif role.get("role") == "spectator":
            st["state"], st["text"] = "waiting", "You are watching and are not on a team in this session."
        elif not self.race:
            st["text"] = "No race is running. The engineer's page shows this session."
        else:
            st["text"] = ""
        board = self._board(snap, crew)
        st["board"] = board
        st["stint"] = self._stint_view(chief)
        st["plan"] = self._plan_view_state()
        st["fair"] = self._fair(chief)
        st["fuel"] = self._fuel_view(chief, crew)
        st["swap"] = {"alert": self.alert, "handover": self._handover_view(), "eta_s": self.eta_next}
        st["rivals"] = list(self.rivals)
        st["laps"] = [dict(r) for r in list(self.laps)[-40:]][::-1]
        st["positions"] = self._positions()
        st["stops"] = self._stops_view(chief)
        st["replan"] = self._replan_view()
        st["overlay"] = self._overlay(board, st["stint"])
        st["calls"] = (snap.get("calls") or [])[:6]
        return st

    def _board(self, snap, crew):
        keep = ("call", "call_text", "pos", "laps_left", "box", "ahead", "behind", "last_lap", "caution", "flag", "plan",
                "pit_loss", "state")
        b = {k: snap.get(k) for k in keep}
        b["estimated"] = bool(crew)
        if crew:
            f = self._est_fuel
            if f is None:
                b["box"] = None                 # nothing to base a box call on
                b["no_fuel"] = True
            elif self.fuel_unsure:
                b["box"] = None                 # fuel may have gone in at the unplanned stop: no box claim from a guess
                b["uncertain"] = "Fuel is uncertain " + self.fuel_unsure + ": a box call needs the driver's reading."
        b["perf_note"] = ""
        h = self.pos_hist.get(self.team_idx) if self.team_idx is not None else None
        if h and len(h) >= 2:
            b["pos_change"] = {"since_lap": h[0][0], "places": h[0][1] - h[-1][1]}       # + is places gained since the first lap seen
        return b

    def _stint_view(self, chief):
        s = self.stint
        pn = self.plan_name(self.cur_driver) if self.cur_driver else None
        nxt, swap = self.next_stint()
        me = self.me_plan()
        out = {"driver": self.cur_driver, "driver_how": self.cur_driver_how, "plan_driver": pn, "n": s["n"] if s else None,
               "timer_s": (self.t - s["start_t"]) if (s and self.t is not None) else None,
               "timer_text": clock_text((self.t - s["start_t"]) if (s and self.t is not None) else None),
               "laps": (self.team_laps - s["start_lap"]) if s else None,
               "planned_laps": (self.plan.stint(self.plan_k) or {}).get("laps") if self.plan else None,
               "next": None, "stops_done": self.team_stops, "me": me}
        if self.plan and nxt:
            tzs = self.plan.tz
            eta = self.eta_to_stop(chief)
            wall = self.wall()
            tz_me = tzs.get(me, 0.0) if me else 0.0
            tz_n = tzs.get(nxt["driver"], 0.0)
            out["next"] = {"driver": nxt["driver"], "swap": swap, "planned_start_s": nxt["start_s"],
                           "eta_s": eta, "eta_text": clock_text(eta) if eta is not None else "",
                           "their_time": local_time(wall + eta, tz_n - tz_me) if eta is not None else "",
                           "your_time": local_time(wall + eta) if eta is not None else "",
                           "tz_shift_h": tz_n - tz_me,
                           "is_me": bool(me and nxt["driver"] == me),
                           "laps": nxt["laps"], "from_lap": nxt["from_lap"]}
        elif self.plan and self.plan.stint(self.plan_k) and not self.plan.stint(self.plan_k).get("stop"):
            out["next"] = {"driver": None, "text": "The plan has no more stops."}
        return out

    def _plan_view_state(self):
        if not self.plan:
            return {"has_plan": False, "err": self.plan_err,
                    "text": "No saved stint plan for this track." if not self.plan_err else "The saved plan can't be read: " + self.plan_err,
                    "make": True}
        prog = self._progress()
        over = self.time_left is not None and self.time_left <= 0           # the clock has run out: no more 'against the plan'
        d = self.plan.delta(prog, self.elapsed) if (self.elapsed and not over) else None
        return {"has_plan": True, "title": self.plan.title, "saved": self.plan.saved, "stops": self.plan.sim["stops"],
                "laps": self.plan.total_laps, "delta": d, "drivers": self.plan.names,
                "stints": [{"n": x["n"], "driver": x["driver"], "from_lap": x["from_lap"], "to_lap": x["to_lap"],
                            "start": E.clock(max(0, x["start_s"])), "stop": x.get("stop")} for x in self.plan.stints],
                "make": False}

    def _fuel_view(self, chief, crew):
        unit = self.units
        if not crew:
            snap = chief.snapshot() if chief is not None else {}
            f = snap.get("fuel") or {}
            return {"estimated": False, **f} if f else {"estimated": False, "text": ""}
        est = self._est_fuel
        burn = self._burn_for(self.cur_driver)
        if est is None:
            return {"estimated": True, "text": "unknown", "basis": "Needs a saved plan or the car's tank to estimate."}
        pair = cc.fuel_pair(est, unit)
        laps = (est / burn) if burn else None
        return {"estimated": True, "uncertain": self.fuel_unsure or None, "text": "~" + pair["text"], "alt": pair.get("alt"), "l": est,
                "laps": None if laps is None else round(laps, 1),
                "laps_text": "" if laps is None else f"about {laps:.1f} laps of fuel",
                "basis": f"estimated from the {self.fuel.how} after the last stop, {burn:.2f} L a lap" if burn else "estimated"}

    def _handover_view(self):
        k = self.plan_k
        in_pit = self.team_idx is not None and self.stops.in_pit(self.team_idx)
        last = self.swaps[-1] if self.swaps else None
        show_last = (not in_pit) and last is not None and self.t is not None and self.t - last["t_out"] <= HANDOVER_KEEP_S \
            and bool(last.get("counted"))                 # a repair or a splash is a stop, not a handover
        if not (in_pit or show_last):
            return None
        if in_pit:
            cur, nxt = (self.plan.stint(k) if self.plan else None), (self.plan.stint(k + 1) if self.plan else None)
            stop, pstop = None, (cur or {}).get("stop")
        else:
            cur, nxt = (self.plan.stint(k - 1) if self.plan else None), (self.plan.stint(k) if self.plan else None)
            stop, pstop = last, (cur or {}).get("stop")
        swap = (bool(pstop and pstop.get("swap")) and (not in_pit or self._near_planned_stop(k))) or \
            bool(last and last.get("swap") and not in_pit)
        if not swap:
            return None
        frm = (cur or {}).get("driver") or (last or {}).get("from_driver") or self.cur_driver
        to = (nxt or {}).get("driver") or (pstop or {}).get("to") or (last or {}).get("to")
        nd = self.plan.by_name.get(to) if self.plan and to else None
        hr = self.ho_real or {}
        driving = self.role.get("driving")
        measured = bool(hr.get("before") is not None and hr.get("after") is not None)
        card = handover_card(stop=stop, planned=pstop, driver_from=frm, driver_to=to,
                             fuel_before=hr.get("before") if measured else None, fuel_after=hr.get("after") if measured else None,
                             incidents=self._stint_incidents(), unit=self.units,
                             next_lap_s=(nd or {}).get("lap_t"), next_burn=(nd or {}).get("burn"))
        card["in_pit"] = in_pit
        card["estimated"] = self.role.get("role") == "crew" and not measured
        card["measured_here"] = measured or bool(driving)
        return card

    def _stint_incidents(self):
        s = self.stint or (self.stints_done[-1] if self.stints_done else None)
        if not s or s.get("inc0") is None or self._inc_last is None:
            return None
        return max(0, self._inc_last - s["inc0"])

    _inc_last = None
    _real_before_stop = None

    def _positions(self):
        laps = self.pos_laps[-120:]
        if not laps:
            return {"laps": [], "series": []}
        n = len(laps)
        series = []
        me = self.team_idx
        order = sorted(self.pos_hist, key=lambda i: (i != me, (self.pos_hist[i][-1][1] if self.pos_hist[i] else 99)))
        for i in order[:CLASS_CHART]:
            d = dict(self.pos_hist[i])
            d_ = self.info_drv(i)
            series.append({"idx": i, "num": d_.get("num", ""), "me": i == me, "pos": [d.get(l) for l in laps]})
        return {"laps": laps, "series": series}

    def info_drv(self, i):
        es = self.info.get("entries", {}).get(i) or [{}]
        return {"num": str(es[0].get("CarNumber") or ""), "name": str(es[0].get("UserName") or "")}

    def _stops_view(self, chief):
        ours = [dict(x) for x in self.swaps[-12:]]
        cars = self._class_cars(self.field) if self.field else {}
        rivals = []
        for i, p in sorted(cars.items(), key=lambda kv: kv[1]):
            d = self.info_drv(i)
            lst = self.stops.stops.get(i) or []
            rivals.append({"idx": i, "num": d["num"], "name": d["name"], "pos": p, "me": i == self.team_idx,
                           "stops": len(lst), "passes": self.stops.passes.get(i, 0), "in_pit": self.stops.in_pit(i),
                           "last_lap": lst[-1]["lap"] if lst else None, "under_caution": sum(1 for x in lst if x["under_caution"]),
                           "list": [{"lap": x["lap"], "lane_s": x["lane_s"], "stall_s": x["stall_s"], "caution": x["under_caution"]}
                                    for x in lst[-6:]]})
        return {"ours": ours, "rivals": rivals[:40]}

    def _replan_view(self):
        if self.replan_res is None:
            return {"ok": False, "text": self.replan_err or "Made once the race is running and a plan is saved.",
                    "delays": dict(self.delays)}
        r = self.replan_res
        return {"ok": True, "why": r.get("why"), "laps": r["laps"], "finish_s": r["finish_s"],
                "finish_text": clock_text(r["finish_s"]), "stops": r["stops"], "stints": r["stints"][:12],
                "options": r["options"], "notes": r["notes"], "warnings": r["warnings"], "estimated": r.get("estimated_fuel"),
                "delays": dict(self.delays)}

    def _overlay(self, board, stint):
        bits = []
        if stint.get("timer_s") is not None:
            bits.append("Stint " + stint["timer_text"])
        nx = stint.get("next")
        if nx and nx.get("driver"):
            bits.append("next " + str(nx["driver"]).split()[0][:10] + (" " + nx["your_time"] if nx.get("is_me") and nx.get("your_time") else ""))
        box = board.get("box") if board else None
        if box and box.get("need"):
            ear, lat = box.get("lap_earliest"), box.get("lap_latest")
            tag = f"BOX L{ear}-{lat}" if ear and lat and ear < lat else f"BOX L{lat}"
            bits.append(tag + (" (est.)" if board.get("estimated") else ""))
        elif box and box.get("text"):
            bits.append("no stop" + (" (est.)" if board.get("estimated") else ""))
        return " · ".join(bits)
