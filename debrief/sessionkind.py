"""What kind of session this is, and what the car is doing in it (2.31 "Race Engineer").

Pure functions of the session text (iRacing's YAML as livedata gives it) and the live channels. No clock, no iRacing, no
network: the Race Engineer asks these two questions on every reading, and the tests feed them stand-ins.

  classify(sections, own)   -> {"kind", "label", "family", "venue", "name", "raw", "phase", ...}
  CarState                  -> "in the garage", "in the pits", "out lap", "flying lap", "in lap", "cool-down lap",
                               "off track", "towing"

kind     what the session is: test, practice, open_practice, lone_qualify, open_qualify, qualify, warmup, race,
         time_trial, spectating, replay, or "session" (an unknown name, shown as iRacing wrote it)
family   how the Race Room page lays itself out: "practice" | "qualify" | "race" | "watch" | "other"
venue    "official" | "hosted" | "league" | "offline" (a test drive) | "replay"
phase    a race's state: gridding, formation, green, white, chequered, cool-down (else "")
"""
from __future__ import annotations

F_CHECKERED, F_WHITE = 0x0001, 0x0002

# SessionState (iRacing): 1 get in car, 2 warmup (the grid), 3 parade laps, 4 racing, 5 chequered, 6 cool-down
PHASES = {1: "gridding", 2: "gridding", 3: "formation", 4: "green", 5: "chequered", 6: "cool-down"}
PHASE_WORDS = {"gridding": "Gridding", "formation": "Formation lap", "green": "Green flag", "white": "White flag",
               "chequered": "Chequered flag", "cool-down": "Cool-down", "": ""}

KIND_WORDS = {"test": "Test drive", "practice": "Practice", "open_practice": "Open practice",
              "lone_qualify": "Lone qualifying", "open_qualify": "Open qualifying", "qualify": "Qualifying",
              "warmup": "Warmup", "race": "Race", "time_trial": "Time trial", "spectating": "Spectating",
              "replay": "Replay", "session": "Session"}
FAMILY = {"test": "practice", "practice": "practice", "open_practice": "practice", "lone_qualify": "qualify",
          "open_qualify": "qualify", "qualify": "qualify", "warmup": "practice", "race": "race",
          "time_trial": "practice", "spectating": "watch", "replay": "watch", "session": "other"}


def _int(v):
    try:
        if v is None or isinstance(v, bool):
            return int(v) if isinstance(v, bool) else None
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(_int(v))


def session_row(sections, session_num):
    """The Sessions[] entry for SessionNum (the last one when the number is unknown or missing)."""
    rows = [r for r in (((sections or {}).get("SessionInfo") or {}).get("Sessions") or []) if isinstance(r, dict)]
    if not rows:
        return {}
    for r in rows:
        if _int(r.get("SessionNum")) == session_num and session_num is not None:
            return r
    return rows[-1] if session_num is None else {}


def type_from_names(raw_type: str, raw_name: str, event: str) -> str:
    """The kind for iRacing's SessionType / SessionName / EventType words."""
    t = " ".join(str(raw_type or "").lower().replace("_", " ").split())
    n = " ".join(str(raw_name or "").lower().replace("_", " ").split())
    e = " ".join(str(event or "").lower().replace("_", " ").split())
    both = t + " | " + n
    if "time trial" in both or "time attack" in both or "time trial" in e:
        return "time_trial"
    if "offline" in t or t == "testing" or t == "test" or (not t and e == "test"):
        return "test"
    if "lone" in t and "qual" in t:
        return "lone_qualify"
    if "open" in t and "qual" in t:
        return "open_qualify"
    if "open" in t and "practice" in t:
        return "open_practice"
    if "warm" in both:
        return "warmup"
    if "race" in t or t == "heat" or "feature" in t:
        return "race"
    if "qual" in t:
        return "qualify"
    if "practice" in t:
        return "practice"
    if "race" in n:
        return "race"
    if "qual" in n:
        return "qualify"
    if "practice" in n:
        return "practice"
    if e == "race":
        return "race"
    if e in ("qualify", "qualifying"):
        return "qualify"
    if e == "practice":
        return "practice"
    return "session"


def classify(sections, own=None) -> dict:
    """The session kind from the session text and the live channels. Never raises; an unknown type is kind "session"
    with the raw name shown."""
    own = own if isinstance(own, dict) else {}
    sections = sections if isinstance(sections, dict) else {}
    wi = sections.get("WeekendInfo") if isinstance(sections.get("WeekendInfo"), dict) else {}
    di = sections.get("DriverInfo") if isinstance(sections.get("DriverInfo"), dict) else {}
    sn = _int(own.get("SessionNum"))
    row = session_row(sections, sn)
    raw_type = str(row.get("SessionType") or own.get("SessionType") or "")
    raw_name = str(row.get("SessionName") or "")
    event = str(wi.get("EventType") or "")
    kind = type_from_names(raw_type, raw_name, event)
    official = _truthy(wi.get("Official"))
    league = (_int(wi.get("LeagueID")) or 0) > 0
    series = _int(wi.get("SeriesID"))
    hosted = league or (not official and (series == 0 or series is None) and kind != "test" and bool(wi))
    if str(wi.get("SimMode") or "").lower() == "replay" or _truthy(own.get("IsReplayPlaying")):
        venue = "replay"
    elif kind == "test" or str(event).lower() == "test":
        venue = "offline"
    elif league:
        venue = "league"
    elif hosted:
        venue = "hosted"
    else:
        venue = "official"
    me = None
    for d in di.get("Drivers") or []:
        if isinstance(d, dict) and _int(d.get("CarIdx")) == _int(di.get("DriverCarIdx")):
            me = d
    spectating = bool(me) and _truthy(me.get("IsSpectator")) or (_int(di.get("DriverCarIdx")) in (-1,))
    if venue == "replay":
        kind = "replay"
    elif spectating and not _truthy(own.get("IsOnTrack")):
        kind = "spectating"
    label = KIND_WORDS.get(kind, "Session")
    if kind == "session":
        label = "Session" + (f" ({raw_type or raw_name})" if (raw_type or raw_name) else "")
    elif venue in ("hosted", "league") and kind not in ("spectating", "replay"):
        label = ("League " if venue == "league" else "Hosted ") + KIND_WORDS[kind].lower()
    elif kind in ("open_practice", "practice") and venue == "official":
        label = KIND_WORDS[kind]
    phase = ""
    if kind == "race":
        st = _int(own.get("SessionState"))
        phase = PHASES.get(st, "")
        fl = _int(own.get("SessionFlags")) or 0
        if phase == "green" and fl & F_WHITE:
            phase = "white"
        if phase in ("green", "white") and fl & F_CHECKERED:
            phase = "chequered"
    team = _truthy(wi.get("TeamRacing"))
    classes = _int(wi.get("NumCarClasses")) or 1
    return {"kind": kind, "label": label, "family": FAMILY.get(kind, "other"), "venue": venue,
            "name": raw_name or raw_type, "raw": raw_type or raw_name, "event": event, "official": official,
            "hosted": venue in ("hosted", "league"), "league": league, "team": team, "classes": classes,
            "phase": phase, "phase_text": PHASE_WORDS.get(phase, ""), "session_num": sn,
            "track": str(wi.get("TrackDisplayName") or wi.get("TrackName") or ""),
            "key": (sn, kind, str(wi.get("SubSessionID") or ""))}


# ---------------------------------------------------------------- the car
CAR_WORDS = {"none": "", "garage": "In the garage", "pits": "In the pits", "out": "Out lap", "flying": "Flying lap",
             "in": "In lap", "cooldown": "Cool-down lap", "off": "Off track", "towing": "Towing"}
SLOW_LAP = 1.07           # a lap this much slower than the session best is not a push lap


class CarState:
    """The car's state from the channels, one reading at a time. update() returns the state key (CAR_WORDS).

    garage    IsInGarage, or not in the world (PlayerTrackSurface -1) and not on track
    pits      on pit road or in the stall (PlayerTrackSurface 1 or 2, OnPitRoad)
    out       from leaving the pits to the next start/finish line
    flying    on track after that
    in        the lap after a push lap in qualifying (the car is on its way in), and the lap when the session ends
    cooldown  a slow lap after the flag (the chequered session state) or after a clearly slow lap in qualifying
    off       off the track (PlayerTrackSurface 0) for more than a moment
    towing    PlayerCarTowTime above zero
    Hand-check (qualifying, best 90.0 s): out lap 120 s -> flying; a 90.4 s lap -> still flying (push 2);
    a 100 s lap (111%) -> next lap is "in" (a lap after a slow one is the way in)."""
    OFF_HOLD_S = 1.5

    def __init__(self):
        self.state = "none"
        self.lap = None
        self.out_lap = False
        self.last_lap_s = None
        self.best_s = None
        self.slow_prev = False
        self.out_lap_prev = False
        self._off_since = None
        self._left_pits_lap = None

    def update(self, own, now, family="practice", finished=False) -> str:
        own = own if isinstance(own, dict) else {}
        surf = _int(own.get("PlayerTrackSurface"))
        garage = _truthy(own.get("IsInGarage")) or (surf == -1 and not _truthy(own.get("IsOnTrack")))
        pit = _truthy(own.get("OnPitRoad")) or surf in (1, 2) or _truthy(own.get("PlayerCarInPitStall"))
        tow = (own.get("PlayerCarTowTime") or 0) > 0 if isinstance(own.get("PlayerCarTowTime"), (int, float)) else False
        lap = _int(own.get("Lap"))
        prev = self.state
        if garage:
            self.state, self.out_lap, self._off_since = "garage", False, None
            self.lap = lap
            return self.state
        if tow:
            self.state = "towing"
            return self.state
        if pit:
            if prev not in ("pits", "garage", "none"):
                self.out_lap = False
            self.state, self._off_since = "pits", None
            self.lap = lap
            self._left_pits_lap = lap
            self.out_lap = True            # the next time the car is on track it is on an out lap
            return self.state
        # on track
        if lap is not None and self.lap is not None and lap != self.lap:
            was_out, self.out_lap = self.out_lap, False
            self.out_lap_prev = was_out
            self._lap_done(own, family, was_out)
        self.lap = lap
        if surf == 0 or surf == -1:
            if self._off_since is None:
                self._off_since = now
            if now - self._off_since >= self.OFF_HOLD_S:
                self.state = "off"
                return self.state
            return self.state if self.state not in ("none", "pits", "garage") else "flying"
        self._off_since = None
        if self.out_lap:
            self.state = "out"
        elif finished:
            self.state = "cooldown" if family != "race" else "cooldown"
        elif family == "qualify" and self.slow_prev:
            self.state = "in"
        else:
            self.state = "flying"
        return self.state

    def _lap_done(self, own, family, was_out=False):
        t = own.get("LapLastLapTime")
        t = float(t) if isinstance(t, (int, float)) and t > 0 else None
        self.last_lap_s = t
        if t is not None and (self.best_s is None or t < self.best_s) and not was_out:
            self.best_s = t
        # an out lap is slow by nature: it never makes the NEXT lap an "in lap"
        self.slow_prev = bool(t is not None and not was_out and self.best_s is not None and t > self.best_s * SLOW_LAP)

    def text(self) -> str:
        return CAR_WORDS.get(self.state, "")
