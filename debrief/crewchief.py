"""The crew chief (2.29 "Crew chief"): a race engineer on the radio, from iRacing's own live numbers.

Pure logic first (no iRacing, no sound, no window: tests feed it dicts), then the small pieces that touch the outside:

  CrewChief     feed(own, field, now) -> [Call]. `own` is one reading of your car (the livedata.LiveReader channels:
                Lap, LapDistPct, FuelLevel, SessionFlags, SessionTimeRemain ...), `field` the CarIdx arrays (about 4 a
                second). snapshot() is what the Crew chief panel and the overlay show.
  CallSpeaker   the quiet zones: a call waits for a place where it may be said (never under braking, never with a car
                alongside unless it is the alongside or fuel critical call) and is dropped, not said late.
  CrewLog       every call, and the situation it came from, into the 2.27 recording of the session.

What it works out (units: litres inside; Settings > Units picks litres or gallons for what you read and hear):

  fuel          burn = median of the last BURN_LAPS green, clean laps (not a pit lap, not under yellow or caution, not
                after a tow, not lap 1, not a lap where fuel went up). Laps of fuel = fuel / burn. Tank =
                DriverCarFuelMaxLtr x DriverCarMaxFuelPct.
  laps to go    lap race: SessionLapsRemainEx. Timed race: the LEADER's recent lap time (your class leader in a
                multiclass race) and SessionTimeRemain, less the time the stops still to make will take, plus the one
                lap that finishes a timed race.   crossings = floor(time left / lap time + lap fraction) + 1
  pit window    the earliest and latest lap you can pit on (see pit_window), fuel to add, and the fuel saving that
                would skip the stop. Same reserve rule as the fuel planner (strategy.py): a spare lap of fuel.
  caution       a full-course caution (SessionFlags caution bits) with the window open or opening within 3 laps ->
                "Box now, cheap stop". A caution is never a pit cycle: caution laps are not fuel or pace laps, only a
                real stop in your stall counts as a stop, and nobody "pitting" is called while the field is under it.
  rivals        the cars directly ahead and behind IN YOUR CLASS: gap, closing rate over the last 3 laps, laps until
                contact, and tags threat / catchable / pulling away / pitting / alongside.
  calls         ranked by urgency (RANKS), each with a short spoken line and a short text line, never repeated within
                2 laps unless it gets more urgent.

Every number a rule uses is in a constant below, with the reason.
"""
from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field

from . import units as _units
from . import voicepack as vp

VERSION = 1

# ---- iRacing's SessionFlags bits (checked against pyirsdk's irsdk.Flags in tests/test_229_crewchief.py)
F_CHECKERED, F_WHITE, F_GREEN, F_YELLOW, F_RED = 0x1, 0x2, 0x4, 0x8, 0x10
F_YELLOW_WAVING, F_ONE_LAP_TO_GREEN = 0x100, 0x200
F_CAUTION, F_CAUTION_WAVING, F_FURLED = 0x4000, 0x8000, 0x80000
FULL_COURSE = F_CAUTION | F_CAUTION_WAVING                    # a full-course caution
SLOW_FLAGS = FULL_COURSE | F_YELLOW | F_YELLOW_WAVING | F_RED  # a lap under any of these is not a green lap
# CarLeftRight values with a car beside you (irsdk.CarLeftRight: 2 left, 3 right, 4 both, 5 two left, 6 two right)
ALONGSIDE_VALUES = (2, 3, 4, 5, 6)
STATE_RACING, STATE_CHECKERED = 4, 5                           # SessionState

# ---- fuel
BURN_LAPS = 8               # the burn is the median of this many latest green laps
MIN_BURN_LAPS = 3           # fewer than this: no figure yet (the same rule as strategy.MIN_BURN_LAPS)
MIN_BURN_L = 0.2            # a lap using less than this is a glitch (strategy.MIN_BURN_L)
RESERVE_LAPS = 1.0          # a spare lap of fuel at the flag and at every stop (strategy.plan's default)
CRITICAL_LAPS = 2.0         # fuel critical: under this many laps of fuel and not enough to finish
GAL_L = 3.785               # litres in a US gallon (strategy.GAL_L)
MARGIN_LAPS = 0.2           # the least spare fuel at the flag that still counts as "makes it"
TIGHT_PCT = 5.0            # "fuel is tight" when keeping the full spare lap would need at least this % saving
SAVE_REALISTIC = 0.10       # saving more than this share of the burn to skip a stop is not realistic
# ---- pit window
BOX_SOON_LAPS = 3           # "box within 3 laps"
CHEAP_STOP_LAPS = 3         # a caution stop is cheap when the window is open or opens within this many laps
DEFAULT_PIT_LOSS_S = 25.0   # used until a stop is measured (or a plan / strategy.json gives one), and said so
MAX_STOPS = 6
# ---- rivals
RATE_MIN = 0.1              # s a lap: a gap closing slower than this is not "closing"
AWAY_RATE = 0.1             # a gap growing faster than this is "pulling away"
CONTACT_LAPS = 5.0          # threat / catchable: contact within this many laps
CONTACT_GAP_S = 0.2         # "contact" = this close
ALONGSIDE_S = 0.1           # a rival this close by the clock is alongside (the CarLeftRight signal is the main source)
RATE_WINDOW_LAPS = 3.0      # closing rate smoothed over the last 3 laps
SAMPLE_EVERY = 0.25         # a gap sample every quarter lap
MIN_SPAN = 1.0              # at least a lap of samples before a rate is trusted (a gap reading wobbles by a few hundredths)
MIN_SAMPLES = 4
# ---- calls
REPEAT_LAPS = 2             # the same call type is not repeated within this many laps unless it escalates
TARGET_OFF_S = 0.3          # a clean lap this much slower than the target is called
FROZEN_S = 3.0              # no new session time for this long = data lost
LOST_REPEAT_S = 30.0
MAX_CALLS_KEPT = 30

RANKS = {"data_lost": 1, "data_back": 9, "fuel_critical": 2, "box_now": 3, "box_soon": 3, "caution_box": 4, "fuel_tight": 4,
         "threat": 5, "alongside": 6, "catchable": 7, "ahead_pits": 8, "plan": 9, "position": 9, "target": 10}
GROUP = {"box_soon": "box"}      # "box in three laps" ... "box next lap" share one limit; "box this lap" is limited per lap
REPEAT_OVERRIDE = {"fuel_tight": 5}       # laps between "fuel is tight" calls unless it gets worse
MUTE_OK = ("fuel_critical", "box_now", "box_soon")     # mute never silences these: a missed stop ends the race
RANK_WORDS = {1: "data", 2: "fuel", 3: "box", 4: "fuel or caution", 5: "threat", 6: "alongside", 7: "catchable",
              8: "pitting", 9: "plan", 10: "target"}
# how long a call may wait for a place to be said (seconds), and the sound priority (coaching cues are 1-3)
TTL_S = {"data_lost": 15.0, "fuel_critical": 15.0, "box_now": 14.0, "box_soon": 8.0, "caution_box": 8.0, "fuel_tight": 8.0, "threat": 6.0,
         "alongside": 2.5, "catchable": 6.0, "ahead_pits": 5.0, "plan": 8.0, "position": 8.0, "target": 8.0,
         "data_back": 4.0}
PRIORITY_FUEL, PRIORITY_CREW = 6, 4
LOUD = ("fuel_critical", "data_lost")       # sound priority 6, and not held back by the gap between calls
MIN_SPEAK_GAP_S = 2.5
QUIET_ALONGSIDE_OK = ("alongside", "fuel_critical")


# ---------------------------------------------------------------- small helpers
def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _int(v):
    """A whole number from a reading, or None (NaN, infinity, text and booleans are not numbers)."""
    x = _num(v)
    if x is None:
        return None
    try:
        return int(x)
    except (OverflowError, ValueError):
        return None


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _ceil1(x):
    return math.ceil(round(x * 10, 6)) / 10.0


def _at(arr, i):
    try:
        v = arr[i]
    except (TypeError, IndexError, KeyError):
        return None
    return v


def fuel_text(litres, unit="mph", digits=1) -> str:
    """'42.0 L' (km/h) or '11.1 gal' (mph): Settings > Units picks."""
    x = _num(litres)
    if x is None:
        return ""
    if _units.norm(unit) == "mph":
        return f"{x / GAL_L:.{digits}f} gal"
    return f"{x:.{digits}f} L"


def fuel_pair(litres, unit="mph") -> dict:
    """{'l': .., 'gal': .., 'text': the one in Settings > Units, 'alt': the other}"""
    x = _num(litres)
    if x is None:
        return {"l": None, "gal": None, "text": "", "alt": ""}
    gal = x / GAL_L
    mph = _units.norm(unit) == "mph"
    a, b = f"{x:.1f} L", f"{gal:.1f} gal"
    return {"l": round(x, 2), "gal": round(gal, 2), "text": b if mph else a, "alt": a if mph else b}


def lap_text(s) -> str:
    x = _num(s)
    return _units.lap(x) if x else ""


def say_fuel(litres, unit="mph") -> str:
    """What is said for an amount of fuel: 'Forty-two litres.' / 'Eleven gallons.' (rounded UP, so it is never short),
    or '' when the amount is outside what the voice packs have."""
    x = _num(litres)
    if x is None or x <= 0:
        return ""
    if _units.norm(unit) == "mph":
        n = int(math.ceil(x / GAL_L - 1e-9))
        lo, hi = vp.CREW_GALLONS
        return vp.crew_gallons_text(n) if lo <= n <= hi else ""
    n = int(math.ceil(x - 1e-9))
    lo, hi = vp.CREW_LITRES
    return vp.crew_litres_text(n) if lo <= n <= hi else ""


def say_rate(rate) -> str:
    """'three tenths a lap' for a closing rate in seconds a lap (a tenth .. two seconds)."""
    x = _num(rate)
    if x is None or x <= 0:
        return ""
    k = max(vp.CREW_RATE[0], min(vp.CREW_RATE[1], int(round(x * 10))))
    return vp.crew_rate_text(k)


def laps_phrase(x) -> str:
    """'under a lap', '1 lap', '4 laps'."""
    if x is None:
        return ""
    if x < 1.0:
        return "under a lap"
    n = int(round(x))
    return f"{n} lap{'s' if n != 1 else ''}"


def rate_text(rate) -> str:
    x = _num(rate)
    if x is None:
        return ""
    return f"{x:.1f} s a lap"


def vocabulary(unit: str | None = None) -> list:
    """Every sentence a crew chief call can be made of (for the voice pack coverage report)."""
    out = [f"{w}." for w in vp.CREW_WHOLES]
    out += [f"Box in {vp.number_words(n)} laps." for n in range(vp.CREW_BOX_IN[0], vp.CREW_BOX_IN[1] + 1)]
    out += [vp.crew_litres_text(n) for n in range(vp.CREW_LITRES[0], vp.CREW_LITRES[1] + 1)]
    out += [vp.crew_gallons_text(n) for n in range(vp.CREW_GALLONS[0], vp.CREW_GALLONS[1] + 1)]
    out += [vp.crew_laps_fuel_text(n) for n in range(vp.CREW_FUEL_LAPS[0], vp.CREW_FUEL_LAPS[1] + 1)]
    for k in range(vp.CREW_RATE[0], vp.CREW_RATE[1] + 1):
        out += [f"{lead}, {vp.crew_rate_text(k)}." for lead in vp.CREW_LEADS]
    out += [vp.crew_off_text(k) for k in range(vp.CREW_OFF[0], vp.CREW_OFF[1] + 1)]
    return out


# ---------------------------------------------------------------- the maths (pure)
def laps_to_go(laps_rem=None, time_rem=None, lap_f=0.0, leader_lap_s=None, stops=0, pit_loss_s=0.0,
               leader_f=None, my_lap_s=None) -> dict | None:
    """How far to the flag. lap_f = how far round the current lap you are (0..1).

    crossings: how many more times you cross the line (the lap you are on counts; so does the one that finishes a
    timed race). distance: laps of driving left, in laps (crossings less the part of this lap already done).

    Lap race: crossings = SessionLapsRemainEx.
    Timed race: when the clock reaches zero the LEADER takes the flag at his next crossing; you finish at your next
    crossing after that. With T seconds left, the leader's lap time p and his place on the lap fL (0..1), his k-th
    crossing from now is at (k - fL) * p, so he finishes at TL = (kL - fL) * p with kL = ceil(T / p + fL). Your k-th
    crossing is at (k - f) * p + s (s = the time of the stops still to make). You cross the line once for every
    crossing before TL, and once more after it:
        crossings = ceil((TL - s) / p + f)             (at least 1)
    Without his place (leader_f None) he is taken to be where you are. In a multi-class race the flag falls on the OVERALL
    leader (leader_lap_s, leader_f are his) while you lap at your class's pace (my_lap_s; the leader's when None):
        crossings = ceil((TL - s) / my_lap_s + f)
    Hand check A (the leader is where you are): 1800 s left, p = 100 s, half way round: kL = ceil(18.5) = 19,
    TL = 1850 s; your crossings are at 50, 150 ... 1750 s (18 of them) and 1850 s: 19 crossings, 18.5 laps.
    Hand check B (leader at fL = 0.8): his crossings are at 20, 120 ... 1820 s, so he finishes at 1820 s; yours at
    50 ... 1750 s (18) are before that and the one at 1850 s is after it: 19 crossings.
    Hand check C (leader at fL = 0.2): he finishes at 1880 s; yours at 50 ... 1850 s (19) are before it, one more at
    1950 s: 20 crossings. With one 60 s stop to make yours move to 110 ... 1910 s: 18 before 1880 s and the 1910 s
    one is the finish: 19 crossings."""
    f = min(1.0, max(0.0, _num(lap_f) or 0.0))
    lr, tr = _num(laps_rem), _num(time_rem)
    lap_n = tr_n = None
    if lr is not None and 0 <= lr < 30000:
        lap_n = int(lr)
    if tr is not None and 0 <= tr < 600000 and leader_lap_s and leader_lap_s > 5:
        p = float(leader_lap_s)
        fl = _num(leader_f)
        fl = f if fl is None or not (0.0 <= fl <= 1.0) else fl
        kl = max(1, int(math.ceil(tr / p + fl - 1e-9)))
        tl = (kl - fl) * p
        s_ = max(0, _int(stops) or 0) * max(0.0, _num(pit_loss_s) or 0.0)
        pm = _num(my_lap_s)
        pm = p if pm is None or pm <= 5 else pm
        tr_n = max(1, int(math.ceil((tl - s_) / pm + f - 1e-9)))
    if lap_n is None and tr_n is None:
        return None
    if lap_n is not None and tr_n is not None:
        kind, n = ("laps", lap_n) if lap_n <= tr_n else ("time", tr_n)
    elif lap_n is not None:
        kind, n = "laps", lap_n
    else:
        kind, n = "time", tr_n
    return {"crossings": n, "distance": max(0.0, n - f), "kind": kind}


LAST_LAPS = 2.0             # inside this many laps to the flag there is no spare-fuel margin: a stop is only for fuel that is short


def _margin(dist) -> float:
    """The spare fuel (in laps) wanted at the flag: 0.2 lap, none in the last two laps where it would be a large part of
    what is left and a stop costs more than it saves."""
    return 0.0 if dist is not None and dist <= LAST_LAPS else MARGIN_LAPS


def stop_plan(fuel, burn, tank, dist, lap_f=0.0, reserve=RESERVE_LAPS, stop_now=False):
    """The stops it takes to reach the flag, each as late as the fuel allows, as (crossings, ok).

    A stop happens at a line crossing: c = 0 is the end of this lap, 1 the end of the next one (that crossing is
    c + 1 - lap_f laps of driving from now). Each stop is the latest crossing that still arrives with the spare lap
    (or, when that is not possible, the spare 0.2 lap) and fills the tank. The fuel left over when you stop (up to
    a lap, because stops are at lap ends) is what the old "tank - reserve" sum ignored. Done when what is in the tank
    reaches the flag with MARGIN_LAPS to spare. stop_now: the first stop is at the end of this lap whatever.
    ok is False when a stop could not be reached with fuel to spare (the crossings are then a best effort)."""
    f = min(1.0, max(0.0, lap_f or 0.0))
    m = _margin(dist)
    stops: list = []
    ok = True
    fl, pos, cmin = float(fuel), 0.0, 0
    if stop_now:
        x = 1.0 - f
        if fl - x * burn < 0:
            return [0], False
        stops.append(0)
        fl, pos, cmin = float(tank), x, 1
    while True:
        if fl - (dist - pos) * burn >= m * burn - 1e-9:
            return stops, ok
        if len(stops) >= MAX_STOPS:
            return stops, False
        c = int(math.floor(pos + fl / burn - reserve + f - 1 + 1e-9))
        if c < cmin:
            c = int(math.floor(pos + fl / burn - m + f - 1 + 1e-9))
        if c < cmin:
            c, ok = cmin, False
        x = c + 1 - f
        if x >= dist - 1e-9:
            return stops, False
        stops.append(c)
        fl, pos, cmin = float(tank), x, c + 1


def stops_needed(fuel, burn, tank, dist, reserve=RESERVE_LAPS, lap_f=0.0) -> int:
    """Stops still to make to reach the flag (stop_plan's count; 0 when the fuel gets there with 0.2 lap to spare)."""
    if not burn or burn <= 0 or not tank or tank <= 0:
        return 0
    return len(stop_plan(fuel, burn, tank, dist, lap_f, reserve)[0])


def stops_if_stop_now(fuel, burn, tank, dist, lap_f=0.0, reserve=RESERVE_LAPS):
    """Stops to make in all if you stop at the end of this lap (that stop counts), or None if you cannot reach it."""
    if not burn or burn <= 0 or not tank or tank <= 0:
        return None
    plan, ok = stop_plan(fuel, burn, tank, dist, lap_f, reserve, stop_now=True)
    return len(plan) if ok or len(plan) > 1 else None


def pit_window(fuel, burn, tank, dist, lap_f=0.0, reserve=RESERVE_LAPS) -> dict:
    """The pit window with fuel in litres, burn in litres a lap, dist = laps of driving to the flag (laps_to_go
    'distance'), lap_f = how far round this lap you are.

    Does the fuel get you to the flag? It does when you reach it with MARGIN_LAPS (0.2 lap) to spare, or when the
    saving that would take is no more than SAVE_REALISTIC (10%) of the burn. The spare lap (reserve) is the planning
    cushion for WHERE to stop, not fuel you must have: with 0.95 lap spare there is no stop, only "fuel is tight"
    when keeping the whole spare lap would take a saving of TIGHT_PCT or more.

    k counts laps from now: k = 0 means pit at the end of THIS lap, k = 1 the end of the next one.
      latest   you must still arrive at the pits with the spare lap:   fuel - (k + 1 - lap_f) * burn >= reserve * burn
               -> k_latest = floor(fuel / burn - reserve + lap_f - 1)           (negative: even this lap is too late)
      earliest after one stop (filled to the top) you reach the flag with the spare lap:
               tank >= (dist - (k + 1 - lap_f) + reserve) * burn
               -> k_earliest = max(0, ceil(dist - tank / burn + reserve + lap_f - 1))
      stops    stop_plan: each stop as late as the fuel allows, counted with the fuel wasted at lap ends
      add      fuel to add to reach the flag with the spare lap = (dist + reserve) * burn - fuel. It is the same
               wherever in the window you stop (the planner's rule); `fill` is what fits in the tank at the latest stop.
      save     the burn that would reach the flag with MARGIN_LAPS to spare and no stop: burn - fuel / (dist + 0.2).
    Hand checks (tank 100 L, burn 3.0, reserve 1):
      60 L, 50 laps to go: 1 stop; k_latest = floor(20 - 1 + 0 - 1) = 18; k_earliest = ceil(50 - 33.33 + 1 - 1) = 17;
        add 153 - 60 = 93 L; to skip the stop 60 / 50.2 = 1.195 L a lap, a saving of 1.805 L a lap (60%: not realistic).
      60 L, 15 laps to go: 48.6 L needed <= 60 L: no stop; 12 L (4 laps) to spare over the spare lap.
      20 L, 30.5 laps to go, half way round the lap: k_latest = floor(6.67 - 1 + 0.5 - 1) = 5; k_earliest =
        max(0, ceil(30.5 - 33.33 + 1 + 0.5 - 1)) = 0; add 94.5 - 20 = 74.5 L.
      17.8 L, 5 laps to go on a 3 L lap: needs 5.2 laps = 15.6 L: no stop. 0.95 lap spare is not "fuel critical" and
        not even "tight" (keeping the full spare lap would take a 1% saving): the case the first version got wrong."""
    out = {"need_stop": False, "stops": 0, "k_latest": None, "k_earliest": None, "add_l": 0.0, "fill_l": None,
           "margin_l": None, "save_lpl": 0.0, "save_pct": 0.0, "feasible": True, "tight": False,
           "tight_save_lpl": 0.0, "stop_laps": [], "short": False, "reach_laps": None}
    if not burn or burn <= 0 or not tank or tank <= 0 or dist is None or fuel is None:
        out["feasible"] = False
        return out
    f = min(1.0, max(0.0, lap_f or 0.0))
    m = _margin(dist)
    need_res = (dist + reserve) * burn
    need_min = (dist + m) * burn
    out["margin_l"] = round(fuel - need_res, 2)
    out["reach_laps"] = round(fuel / burn - dist, 2)          # laps spare at the flag (negative: short of it)
    save_fin = burn - fuel / (dist + m)
    if fuel + 1e-9 >= need_min or save_fin <= SAVE_REALISTIC * burn:
        if fuel < dist * burn:                              # short of the flag: any saving needed is said
            out["tight"] = out["short"] = True
            out["tight_save_lpl"] = round(max(0.0, save_fin), 2)
        else:
            target = dist + (m if fuel < need_min else reserve)
            sv = burn - fuel / target
            if dist > LAST_LAPS and fuel < need_res and sv / burn * 100.0 >= TIGHT_PCT:
                out["tight"] = True
                out["tight_save_lpl"] = round(max(0.0, sv), 2)
        return out
    plan, ok = stop_plan(fuel, burn, tank, dist, f, reserve)
    n = max(1, len(plan))
    out["need_stop"] = True
    out["stops"] = n
    out["stop_laps"] = list(plan)
    k_late = int(math.floor(fuel / burn - reserve + f - 1 + 1e-9))
    out["k_latest"] = k_late
    if not ok:
        out["feasible"] = False
    if n == 1:
        ke = max(0, int(math.ceil(dist - tank / burn + reserve + f - 1 - 1e-9)))
        if ke > max(0, k_late):
            ke = max(0, int(math.ceil(dist - tank / burn + m + f - 1 - 1e-9)))
        out["k_earliest"] = ke
        if ke > max(0, k_late):
            out["feasible"] = False        # never show a window that isn't safe
    else:
        out["k_earliest"] = 0              # more than one stop: the next one can be whenever the tank takes it
    arrive = fuel - (max(0, k_late) + 1 - f) * burn
    out["fill_l"] = round(max(0.0, tank - max(0.0, arrive)), 2)
    out["add_l"] = round(min(_ceil1(max(0.0, need_res - fuel)), out["fill_l"]), 1)
    save = burn - fuel / (dist + m)
    out["save_lpl"] = round(max(0.0, save), 3)
    out["save_pct"] = round(max(0.0, save) / burn * 100.0, 1)
    return out


def flags_text(flags) -> str:
    f = _int(flags) or 0
    if f & FULL_COURSE:
        return "caution"
    if f & (F_YELLOW | F_YELLOW_WAVING):
        return "yellow"
    if f & F_CHECKERED:
        return "checkered"
    if f & F_WHITE:
        return "white flag"
    return "green"


# ---------------------------------------------------------------- one call
@dataclass
class Call:
    kind: str
    rank: int
    say: str                    # what is said ('Box this lap. Forty-two litres.')
    text: str                   # what is written
    level: int = 1              # escalation inside one kind (more urgent = higher)
    lap: int | None = None
    t: float = 0.0              # when it was made (the caller's clock)
    session_t: float | None = None
    priority: int = PRIORITY_CREW
    ttl: float = 6.0
    data: dict = field(default_factory=dict)
    id: int = 0
    spoken: bool | None = None  # None: not decided yet
    why: str = ""               # why it was not said

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "rank": self.rank, "word": RANK_WORDS.get(self.rank, ""),
                "say": self.say, "text": self.text, "level": self.level, "lap": self.lap, "spoken": self.spoken,
                "why": self.why, "session_t": self.session_t}


# ---------------------------------------------------------------- fuel burn
class BurnTracker:
    """Fuel used per lap, from clean green laps only."""

    def __init__(self):
        self.laps = deque(maxlen=BURN_LAPS)       # (lap number, litres)
        self.seed = None                          # from the last session or the plan, until MIN_BURN_LAPS laps are in
        self.seed_why = ""
        self.skipped: list = []                   # (lap, reason) of the last few laps that were left out

    def add(self, lap, used, dirty):
        if dirty:
            self.skipped.append((lap, dirty))
            del self.skipped[:-8]
            return False
        if used is None or used < MIN_BURN_L:
            self.skipped.append((lap, "no fuel used"))
            del self.skipped[:-8]
            return False
        self.laps.append((lap, float(used)))
        return True

    @property
    def n(self):
        return len(self.laps)

    def burn(self):
        """(litres a lap, how it is known) or (None, 'learning')."""
        if len(self.laps) >= MIN_BURN_LAPS:
            return _median([u for _l, u in self.laps]), "laps"
        if self.seed:
            return self.seed, self.seed_why or "earlier"
        return None, "learning"

    def reset(self):
        """A new session: keep the last figure as a starting guess."""
        b, how = self.burn()
        self.seed = b if b else self.seed
        self.seed_why = "from the last session" if b else self.seed_why
        self.laps.clear()
        self.skipped.clear()


# ---------------------------------------------------------------- rivals
class Rivals:
    """Gap samples to the car ahead and behind in your class, and what they say."""

    def __init__(self):
        self.hist: dict = {}        # car idx -> deque[(my progress in laps, gap seconds)]
        self.ahead = None
        self.behind = None

    def reset(self):
        self.hist.clear()
        self.ahead = self.behind = None

    @staticmethod
    def _slope(pts):
        n = len(pts)
        if n < 3:
            return None
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        den = sum((p[0] - mx) ** 2 for p in pts)
        if den < 1e-9:
            return None
        return sum((p[0] - mx) * (p[1] - my) for p in pts) / den

    def sample(self, idx, progress, gap):
        h = self.hist.setdefault(idx, deque(maxlen=24))
        if not h or progress - h[-1][0] >= SAMPLE_EVERY:
            h.append((progress, gap))
        elif progress < h[-1][0] - 0.5:            # the clock ran backwards (a reset): start over
            h.clear()
            h.append((progress, gap))

    def rate(self, idx, progress):
        """Closing rate in seconds a lap (positive: the gap is shrinking), smoothed over the last 3 laps."""
        h = self.hist.get(idx)
        if not h:
            return None
        pts = [p for p in h if progress - p[0] <= RATE_WINDOW_LAPS + 1e-9]
        if len(pts) < MIN_SAMPLES or pts[-1][0] - pts[0][0] < MIN_SPAN:
            return None
        s = self._slope(pts)
        return None if s is None else -s

    def forget_except(self, keep):
        for k in [k for k in self.hist if k not in keep]:
            del self.hist[k]


# ---------------------------------------------------------------- the crew chief
class CrewChief:
    """feed(own, field, now) -> the calls made by this reading. See the module text."""

    def __init__(self, units="mph", reserve_laps=RESERVE_LAPS, pit_loss_s=None, plan=None, clock=time.monotonic):
        self.units = _units.norm(units)
        self.reserve = float(reserve_laps)
        self.pit_loss_given = pit_loss_s
        self.clock = clock
        self.plan_stops: list = []
        self.plan_lap_s = None
        self.plan_burn = None
        self.plan_note = ""
        self.plan_loss = None
        self.set_plan(plan)
        # session facts
        self.me = None
        self.my_class = None
        self.class_of: dict = {}
        self.drivers: dict = {}
        self.pace_idx: set = set()
        self.tank = None
        self.car_name = ""
        self.track_name = ""
        self.est_lap_s = None
        self.info_version = None
        # state
        self.burn = BurnTracker()
        self.rivals = Rivals()
        self.session_num = None
        self.session_type = ""
        self.lap = None
        self.lap_start_fuel = None
        self._dirty: set = set()
        self.last_lap_s = None
        self.lap_times = deque(maxlen=8)
        self._pending_lt = None
        self.stops_done = 0
        self.pit_losses: list = []
        self._on_pit_prev = None
        self._stall_seen = False
        self._pit_in = None
        self._pit_stall_t = 0.0
        self.caution = False
        self.caution_laps = 0
        self.flags = 0
        self.fuel = None
        self.lap_f = 0.0
        self.cpos = None
        self.pos_overall = None
        self.class_size = None
        self.prev_pos = None
        self._last_frame = None
        self._last_session_t = None
        self._t_change = None
        self.lost = False
        self._lost_called = False
        self._lost_at = -1e9
        self.seen_rich = False
        self.leader_hist: dict = {}
        self._lap_done: dict = {}
        self._pit_seen: dict = {}
        self._last_caution_t = -1e9
        self._plan_state = None
        self.limits: dict = {}
        self.calls: deque = deque(maxlen=MAX_CALLS_KEPT)
        self._next_id = 1
        self.active = False
        self.race = False
        self._field_key = None
        self._last_field_t = None
        self.snap: dict = {}
        self.burn_note = ""
        self.last_calc: dict = {}
        self._rival_view = {"ahead": None, "behind": None}
        self.target_s = None
        self.target_given = None

    # ------------------------------------------------------------ setup
    def set_plan(self, plan):
        """plan: load_plan(...)'s dict (the 2.26 stint plan saved for this track), or None."""
        self.plan_stops, self.plan_lap_s, self.plan_burn, self.plan_note, self.plan_loss = [], None, None, "", None
        if not isinstance(plan, dict):
            return
        try:
            self.plan_stops = [i for i in (_int(x) for x in plan.get("stop_laps") or []) if i is not None]
            self.plan_lap_s = _num(plan.get("lap_s"))
            self.plan_burn = _num(plan.get("burn_l"))
            self.plan_loss = _num(plan.get("pit_loss_s"))
            n = len(self.plan_stops)
            self.plan_note = f"{n} stop{'s' if n != 1 else ''} planned" + (f" ({plan['title']})" if plan.get("title") else "")
        except (KeyError, TypeError, ValueError):
            self.plan_stops, self.plan_note = [], ""

    def set_info(self, sections: dict | None, version=None):
        """The session text (livedata sections): who you are, every driver's class, the tank."""
        if not isinstance(sections, dict) or version == self.info_version and version is not None:
            return
        self.info_version = version
        di = sections.get("DriverInfo") or {}
        wi = sections.get("WeekendInfo") or {}
        try:
            self.me = _int(di.get("DriverCarIdx")) if _int(di.get("DriverCarIdx")) is not None else self.me
        except (TypeError, ValueError, AttributeError):
            pass
        self.class_of, self.drivers, self.pace_idx = {}, {}, set()
        for d in di.get("Drivers") or []:
            try:
                i = _int(d.get("CarIdx"))
            except (TypeError, ValueError, AttributeError):
                continue
            if i is None:
                continue
            self.class_of[i] = d.get("CarClassID")
            self.drivers[i] = {"num": str(d.get("CarNumber") or ""), "name": str(d.get("UserName") or ""),
                               "car": str(d.get("CarScreenName") or "")}
            if d.get("CarIsPaceCar") in (1, True) or d.get("IsSpectator") in (1, True):
                self.pace_idx.add(i)
        self.my_class = self.class_of.get(self.me)
        mx, pct = _num(di.get("DriverCarFuelMaxLtr")), _num(di.get("DriverCarMaxFuelPct"))
        if mx:
            self.tank = mx * (pct if pct else 1.0)
        self.est_lap_s = _num(di.get("DriverCarEstLapTime"))
        self.car_name = (self.drivers.get(self.me) or {}).get("car") or self.car_name
        self.track_name = str(wi.get("TrackDisplayName") or wi.get("TrackName") or self.track_name)
        # another car (or a different tank) or another session: what was learned does not carry over
        car_key = (self.car_name, round(self.tank, 2) if self.tank else None)
        if self._prev_car is not None and car_key != self._prev_car:
            self.burn.seed, self.burn.seed_why = None, ""
            self.burn.laps.clear()
            self._force_new = True
        self._prev_car = car_key
        sub = wi.get("SubSessionID")
        sub = sub if sub not in (None, 0, "") else None
        if sub is not None and self._prev_sub is not None and sub != self._prev_sub:
            self._force_new = True
        if sub is not None:
            self._prev_sub = sub

    def multiclass(self) -> bool:
        return len({c for i, c in self.class_of.items() if i not in self.pace_idx}) > 1

    # ------------------------------------------------------------ calls
    def _make(self, kind, say, text, level, own, now, **data) -> Call:
        c = Call(kind=kind, rank=RANKS[kind], say=say, text=text, level=level, lap=self.lap, t=now,
                 session_t=_num(own.get("SessionTime")) if own else None,
                 priority=PRIORITY_FUEL if kind in LOUD else PRIORITY_CREW, ttl=TTL_S.get(kind, 6.0), data=data)
        return c

    def _allowed(self, kind, level, now, key=None) -> bool:
        """The same kind of call is not repeated within REPEAT_LAPS laps unless it escalates."""
        k = (GROUP.get(kind, kind), key)
        last = self.limits.get(k)
        if last is None:
            return True
        lap0, lvl0, t0 = last[:3]
        if level > lvl0:
            return True
        gap = REPEAT_OVERRIDE.get(kind, REPEAT_LAPS)
        if self.lap is not None and lap0 is not None:
            return self.lap - lap0 >= gap
        return now - t0 >= gap * (self.last_lap_s or 100.0)

    def _issue(self, c: Call, now, key=None, out=None):
        c.id = self._next_id
        self.limits[(GROUP.get(c.kind, c.kind), key)] = (self.lap, c.level, now, c.id)
        self._next_id += 1
        self.calls.append(c)
        if out is not None:
            out.append(c)
        return c

    def forgive(self, c: Call):
        """A call that was dropped without being said (no quiet place in time) does not use up its rate limit, so
        the next reading may make it again."""
        for k, v in list(self.limits.items()):
            if len(v) > 3 and v[3] == c.id:
                del self.limits[k]

    # ------------------------------------------------------------ the feed
    def no_data(self, now=None) -> list:
        """Called by the loop when iRacing answered nothing: after FROZEN_S of silence, 'Data lost'."""
        now = self.clock() if now is None else now
        out = []
        if not self.seen_rich or self._last_frame is None:
            return out
        if now - self._last_frame >= FROZEN_S:
            self._lose(now, {}, out)
        return out

    def _lose(self, now, own, out):
        if not self.race_live():
            return
        self.lost = True
        if not self._lost_called or now - self._lost_at >= LOST_REPEAT_S:
            self._lost_called = True
            self._lost_at = now
            c = self._make("data_lost", "Data lost.", "Lost the iRacing data. Calls are paused until it comes back.",
                           1, own, now)
            self._issue(c, now, out=out)

    def race_live(self) -> bool:
        return bool(self.race and self.seen_rich)

    def feed(self, own: dict, field: dict | None = None, now: float | None = None) -> list:
        """One reading in; the calls it makes out (already rate-limited). Never raises: odd values (NaN, text, a
        missing channel) are skipped, and anything that still goes wrong is kept in `errors` and the reading is left."""
        try:
            return self._feed(own, field, now)
        except Exception as e:  # noqa: BLE001
            self.errors = (self.errors + [f"{type(e).__name__}: {e}"])[-5:]
            self.error_count += 1
            return []

    errors: list = []
    error_count = 0

    def _feed(self, own, field, now) -> list:
        now = self.clock() if now is None else now
        now = _num(now) or 0.0
        out: list = []
        if not isinstance(own, dict):
            return out
        st = _num(own.get("SessionTime"))
        if st is None:
            return out
        self.seen_rich = self.seen_rich or own.get("FuelLevel") is not None
        sn = _int(own.get("SessionNum"))
        uid = _int(own.get("SessionUniqueID"))
        if sn != self.session_num or (uid is not None and uid != self._uid) or self._force_new:
            self._force_new = False
            self._uid = uid if uid is not None else self._uid
            self._new_session(sn)
        self.session_type = str(own.get("SessionType") or self.session_type or "")
        self.race = "race" in self.session_type.lower()
        # alive?
        if self._last_session_t is None or st != self._last_session_t:
            self._last_session_t, self._t_change = st, now
            if self.lost:
                self.lost = False
                if self._lost_called:
                    self._lost_called = False
                    if self.active:
                        self._issue(self._make("data_back", "Data back.", "The iRacing data is back.", 1, own, now),
                                    now, out=out)
        elif now - (self._t_change or now) >= FROZEN_S:
            self._last_frame = now
            self._lose(now, own, out)
            return out
        self._last_frame = now
        state = own.get("SessionState")
        state = _int(state)
        self.active = bool(self.race and self.seen_rich and (state is None or state == STATE_RACING))
        self._read_own(own, now)
        if field is not None:
            self._read_field(field, own, now)
        # a stop is the player's own: pit road in, stall, pit road out
        self._pit_logic(own, now)
        self._lap_logic(own, now, out)
        if self.lap is not None and self.seen_rich and (self.active or not self.race or now >= self._next_calc):
            self._calls(own, now, out, issue=self.active)
        return out

    # ------------------------------------------------------------ own car
    def _new_session(self, sn):
        self.session_num = sn
        self.burn.reset()
        self.rivals.reset()
        self.lap = None
        self._dirty = set()
        self.lap_times.clear()
        self.last_lap_s = None
        self.stops_done = 0
        self.pit_losses = []
        self._on_pit_prev = None
        self._stall_seen = False
        self._pit_in = None
        self.leader_hist.clear()
        self._lap_done.clear()
        self._pit_seen.clear()
        self.limits.clear()
        self._plan_state = None
        self._tight_ref = None
        self.prev_pos = None
        self.lost = self._lost_called = False
        self._last_session_t = None
        if self.burn.seed is None and self.plan_burn:
            self.burn.seed, self.burn.seed_why = self.plan_burn, "from your plan"

    def _read_own(self, own, now):
        self.flags = int(_num(own.get("SessionFlags")) or 0)
        self.caution = bool(self.flags & FULL_COURSE)
        if self.flags & SLOW_FLAGS:
            self._last_caution_t = _num(own.get("SessionTime")) or self._last_caution_t
        self.fuel = _num(own.get("FuelLevel"))
        self.lap_f = min(1.0, max(0.0, _num(own.get("LapDistPct")) or 0.0))
        if self.me is None:
            idx = own.get("PlayerCarIdx")
            self.me = _int(idx)
        cp = _int(own.get("PlayerCarClassPosition"))
        if cp and cp > 0:
            self.cpos = cp
        pp = _int(own.get("PlayerCarPosition"))
        if pp and pp > 0:
            self.pos_overall = pp

    def _observe_dirty(self, own):
        d = self._dirty
        if own.get("OnPitRoad") or own.get("PlayerCarInPitStall"):
            d.add("pit lap")
        if self.flags & SLOW_FLAGS:
            d.add("under yellow or caution")
        tow = _num(own.get("PlayerCarTowTime"))
        if (tow is not None and tow > 0) or own.get("IsOnTrack") is False or own.get("IsOnTrack") == 0:
            d.add("tow or off the world")

    def _pit_logic(self, own, now):
        on = bool(own.get("OnPitRoad"))
        stall = bool(own.get("PlayerCarInPitStall"))
        st = _num(own.get("SessionTime")) or 0.0
        if self._on_pit_prev is not None:
            if on and not self._on_pit_prev:
                self._pit_in = (st, self.lap_f)
                self._stall_seen = False
            if stall:
                self._stall_seen = True
            if self._on_pit_prev and not on and self._pit_in is not None:
                if self._stall_seen:                 # a real stop (not driving through, not a caution)
                    self.stops_done += 1
                    self._measure_loss(st)
                self._pit_in = None
                self._stall_seen = False
                self.rivals.reset()                  # the gaps changed under you
        self._on_pit_prev = on

    def _measure_loss(self, st):
        """Seconds a stop cost: time on pit road less the time that stretch takes at race pace."""
        t0, pct0 = self._pit_in
        frac = (self.lap_f - pct0) % 1.0
        lap_s = _median(list(self.lap_times)) or self.plan_lap_s or self.est_lap_s
        if lap_s:
            loss = (st - t0) - frac * lap_s
            if 3.0 <= loss <= 300.0:
                self.pit_losses.append(loss)
                del self.pit_losses[:-4]

    def pit_loss(self):
        """(seconds, how it is known)."""
        if self.pit_loss_given is not None:
            return float(self.pit_loss_given), "your plan"
        m = _median(self.pit_losses)
        if m is not None:
            return m, "measured"
        if self.plan_loss:
            return float(self.plan_loss), "your plan"
        return DEFAULT_PIT_LOSS_S, "assumed"

    def _lap_logic(self, own, now, out):
        lap = _int(own.get("Lap"))
        if lap is None:
            return
        self._observe_dirty(own)
        self._take_pending_lap_time(own)
        if self.lap is None:
            self._begin_lap(lap, own)
            return
        if lap == self.lap:
            return
        if lap == self.lap + 1:
            self._end_lap(own, now, out)
        else:
            self._dirty.add("lap jump")                  # a tow, a reset, a rejoin: nothing learned from that lap
        self._begin_lap(lap, own)

    def _begin_lap(self, lap, own):
        self.lap = lap
        self.lap_start_fuel = self.fuel
        self._dirty = set()
        self._observe_dirty(own)

    def _end_lap(self, own, now, out):
        dirty = set(self._dirty)
        if self.lap <= 1:
            dirty.add("lap 1")
        used = None
        if self.lap_start_fuel is not None and self.fuel is not None:
            used = self.lap_start_fuel - self.fuel
            if used < -0.05:
                dirty.add("fuel went up")
        if self.caution:
            self.caution_laps += 1
        self.burn.add(self.lap, used, ", ".join(sorted(dirty)) if dirty else "")
        ll = _num(own.get("LapLastLapTime"))
        if not dirty:
            self._pending_lt = (_num(own.get("SessionTime")) or 0.0, ll)    # taken once the clock's value changes
        else:
            self._pending_lt = None
        self._on_lap_end_extra(own, now, out, dirty)

    def _take_pending_lap_time(self, own):
        p = self._pending_lt
        if p is None:
            return
        st = _num(own.get("SessionTime")) or 0.0
        ll = _num(own.get("LapLastLapTime"))
        if ll and ll > 0 and (p[1] is None or abs(ll - p[1]) > 1e-6):
            self.lap_times.append(ll)
            self.last_lap_s = ll
            self._pending_lt = None
            self._lap_time_taken = True
        elif st - p[0] > 3.0:
            if ll and ll > 0 and p[1] is not None and abs(ll - p[1]) <= 1e-6:
                self.lap_times.append(ll)             # it really was the same time twice
                self.last_lap_s = ll
                self._lap_time_taken = True
            self._pending_lt = None

    def _on_lap_end_extra(self, own, now, out, dirty):
        self._edge = True                                # a lap just finished: position and target are looked at
        self._edge_dirty = bool(dirty) or self.caution

    # ------------------------------------------------------------ the field
    def _class_cars(self, field):
        cp = field.get("CarIdxClassPosition") or []
        res = {}
        for i, p in enumerate(cp if isinstance(cp, (list, tuple)) else []):
            p = _int(p)
            if not p or p <= 0 or i in self.pace_idx:
                continue
            if self.my_class is not None and self.class_of and self.class_of.get(i) != self.my_class:
                continue
            res[i] = p
        return res

    def _gap(self, field, a, me, lap_ref):
        """Seconds car `a` is ahead of you (negative: behind), from F2Time when it makes sense, else EstTime and the
        distance between you."""
        lc, pct = field.get("CarIdxLapCompleted"), field.get("CarIdxLapDistPct")
        la, lm = _num(_at(lc, a)), _num(_at(lc, me))
        pa, pm = _num(_at(pct, a)), _num(_at(pct, me))
        if None in (la, lm, pa, pm) or pa < 0 or pm < 0:
            return None
        prog = (la + pa) - (lm + pm)                      # laps ahead
        f2 = field.get("CarIdxF2Time")
        fa, fm = _num(_at(f2, a)), _num(_at(f2, me))
        if fa is not None and fm is not None and fa >= 0 and fm >= 0 and max(fa, fm) > 0:
            g = fm - fa                                    # I am further behind the leader than he is
            if (g >= -0.05) == (prog >= 0):
                return g
        est = field.get("CarIdxEstTime")
        ea, em = _num(_at(est, a)), _num(_at(est, me))
        if lap_ref:
            if ea is not None and em is not None and (ea > 0 or em > 0):
                whole = round(prog - (pa - pm))
                # EstTime runs on iRacing's own idea of a lap, not on the pace of the race: scale it to the pace,
                # or the gap jumps by the difference when a car is across the line and you are not
                full = self._est_full or self.est_lap_s
                scale = (lap_ref / full) if full and full > 5.0 else 1.0
                return whole * lap_ref + (ea - em) * scale
            return prog * lap_ref
        return None

    def _lap_ref(self):
        return _median(list(self.lap_times)) or self.plan_lap_s or self.est_lap_s

    def _read_field(self, field, own, now):
        if self.me is None:
            return
        key = id(field)
        st = _num(own.get("SessionTime")) or 0.0
        if key == self._field_key and self._last_field_t == st:
            return
        self._field_key, self._last_field_t = key, st
        cars = self._class_cars(field)
        me = self.me
        pos_cl = cars.get(me)
        if pos_cl is None:
            return
        self.cpos = pos_cl
        self.class_size = len(cars)
        op = _int(_at(field.get("CarIdxPosition"), me))
        if op and op > 0:
            self.pos_overall = op
        by_pos = {p: i for i, p in cars.items()}
        ahead, behind = by_pos.get(pos_cl - 1), by_pos.get(pos_cl + 1)
        self._leader_idx = by_pos.get(1)
        lp = _num(_at(field.get("CarIdxLapDistPct"), self._leader_idx)) if self._leader_idx is not None else None
        self._leader_f = lp if lp is not None and 0.0 <= lp <= 1.0 else None
        # the car that takes the flag when the clock runs out is the OVERALL leader (a faster class in multi-class)
        self._ov_idx = self._leader_idx
        posn = field.get("CarIdxPosition")
        if isinstance(posn, (list, tuple)):
            for i, v in enumerate(posn):
                if _int(v) == 1 and i not in self.pace_idx:
                    self._ov_idx = i
                    break
        ov = _num(_at(field.get("CarIdxLapDistPct"), self._ov_idx)) if self._ov_idx is not None else None
        self._ov_f = ov if ov is not None and 0.0 <= ov <= 1.0 else None
        self._ov_t = st
        est = field.get("CarIdxEstTime")
        if isinstance(est, (list, tuple)):
            # EstTime runs on iRacing's lap for a car, so the full lap is read from the cars of YOUR class only
            mx = max([e for e in (_num(_at(est, i)) for i in cars) if e is not None] or [0.0])
            if mx > 5.0:
                self._est_full = mx if self._est_full is None else max(self._est_full * 0.999, mx)
        lap_ref = self._lap_ref()
        self._track_leader(field, cars, st, now)
        pit = field.get("CarIdxOnPitRoad") or []
        mypit = bool(_at(pit, me)) or bool(self._on_pit_prev)
        lc, pct = field.get("CarIdxLapCompleted"), field.get("CarIdxLapDistPct")
        lm, pm = _num(_at(lc, me)), _num(_at(pct, me))
        progress = (lm + pm) if lm is not None and pm is not None else None
        view = {"ahead": None, "behind": None}
        keep = set()
        for role, idx in (("ahead", ahead), ("behind", behind)):
            if idx is None:
                continue
            keep.add(idx)
            g = self._gap(field, idx, me, lap_ref)
            if g is None:
                continue
            gap = abs(g)
            on_pit = bool(_at(pit, idx))
            if progress is not None and not (self.caution or on_pit or mypit or self.flags & SLOW_FLAGS):
                self.rivals.sample((idx, role), progress, gap)
            rate = self.rivals.rate((idx, role), progress) if progress is not None and not self.caution else None
            ltc = None
            if rate is not None and rate >= RATE_MIN:
                ltc = max(0.0, (gap - CONTACT_GAP_S) / rate)
            tags = []
            if not self.caution and not on_pit and not mypit:
                if gap < ALONGSIDE_S:
                    tags.append("alongside")
                if role == "behind" and ltc is not None and ltc <= CONTACT_LAPS:
                    tags.append("threat")
                if role == "ahead" and ltc is not None and ltc <= CONTACT_LAPS:
                    tags.append("catchable")
                if rate is not None and rate <= -AWAY_RATE:
                    tags.append("pulling away")
            if on_pit and not self.caution:
                tags.append("pitting")
            d = self.drivers.get(idx) or {}
            view[role] = {"idx": idx, "num": d.get("num", ""), "name": d.get("name", ""), "car": d.get("car", ""),
                          "gap_s": round(gap, 2), "rate": None if rate is None else round(rate, 2),
                          "laps_to_contact": None if ltc is None else round(ltc, 1), "tags": tags,
                          "class_pos": cars.get(idx)}
        self.rivals.forget_except({(i, r) for r, i in (("ahead", ahead), ("behind", behind)) if i is not None})
        self._rival_view = view

    def _track_leader(self, field, cars, st, now):
        """Lap times of the cars of your class (clean laps only): the leader's pace sets the length of a timed race."""
        lc, last, pit = field.get("CarIdxLapCompleted") or [], field.get("CarIdxLastLapTime") or [], \
            field.get("CarIdxOnPitRoad") or []
        watch = set(cars)
        if self._ov_idx is not None:
            watch.add(self._ov_idx)
        for i in watch:
            if _at(pit, i):
                self._pit_seen[i] = True
            n = _at(lc, i)
            if n is None:
                continue
            prev = self._lap_done.get(i)
            self._lap_done[i] = n
            if prev is None or n == prev:
                continue
            clean = not self._pit_seen.pop(i, False)
            lt = _num(_at(last, i))
            if clean and lt and lt > 0 and st - self._last_caution_t > lt:
                self.leader_hist.setdefault(i, deque(maxlen=3)).append(lt)

    def flag_lap(self):
        """(seconds, whose) the recent lap time of the OVERALL leader, who takes the flag; your class leader's when he is
        the same car or is not known."""
        h = self.leader_hist.get(self._ov_idx) if self._ov_idx is not None else None
        if h:
            return _median(list(h)), "overall leader"
        return self.leader_lap()

    def leader_lap(self, field_cars=None):
        """(seconds, whose) the recent lap time of your class leader: median of his last 3 clean laps; yours (or the
        plan's) when he is not known."""
        lead = self._leader_idx
        h = self.leader_hist.get(lead) if lead is not None else None
        if h:
            return _median(list(h)), "leader"
        mine = _median(list(self.lap_times))
        if mine:
            return mine, "yours"
        if self.plan_lap_s:
            return self.plan_lap_s, "plan"
        return self.est_lap_s, "estimate"

    _leader_idx = None
    _ov_idx = None
    _ov_f = None
    _ov_t = None
    _tight_ref = None
    _tight_stops = None
    _leader_f = None
    _uid = None
    _force_new = False
    _prev_car = None
    _prev_sub = None
    _est_full = None
    _edge = False
    _edge_dirty = False
    _lap_time_taken = False
    _next_calc = -1e9
    current = None

    # ------------------------------------------------------------ the numbers and the calls
    def _numbers(self, own):
        """Everything the panel and the rules need, worked out from the latest readings."""
        burn, how = self.burn.burn()
        raw_burn = burn
        if self._tight_stops != self.stops_done:
            self._tight_ref = None
        if burn and self._tight_ref and burn < self._tight_ref:
            burn = self._tight_ref          # a saving he was asked for must not make the shortage look solved
        F, tank = self.fuel, self.tank
        res = {"burn": burn, "burn_how": how, "fuel": F, "tank": tank, "fuel_laps": None, "ltg": None, "window": None,
               "stops": 0, "pit_loss": self.pit_loss(), "leader_lap": self.leader_lap(), "plan": None}
        if burn and F is not None:
            res["fuel_laps"] = F / burn
        lead_s = res["leader_lap"][0]
        flag_s = self.flag_lap()[0]
        # where the leader is NOW: the field arrays are up to a quarter of a second old
        lead_f = self._ov_f if self._ov_f is not None else self._leader_f
        now_t, age = _num(own.get("SessionTime")), None
        if lead_f is not None and self._ov_t is not None and now_t is not None and flag_s and flag_s > 5:
            age = now_t - self._ov_t
            if 0.0 <= age <= 2.0:
                lead_f = (lead_f + age / flag_s) % 1.0
        loss = res["pit_loss"][0]
        stops = 0
        ltg = None
        for _ in range(3 if self.race else 0):
            ltg = laps_to_go(own.get("SessionLapsRemainEx"), own.get("SessionTimeRemain"), self.lap_f,
                             flag_s or lead_s, stops, loss, lead_f, lead_s)
            if ltg is None or not burn or not tank or F is None:
                break
            n = stops_needed(F, burn, tank, ltg["distance"], self.reserve, self.lap_f)
            if n == stops:
                break
            stops = n
        res["ltg"], res["stops"] = ltg, stops
        if ltg is not None and burn and tank and F is not None:
            res["window"] = pit_window(F, burn, tank, ltg["distance"], self.lap_f, self.reserve)
            w = res["window"]
            if (w.get("tight") or w.get("short")) and not w["need_stop"]:
                if self._tight_ref is None:
                    self._tight_ref, self._tight_stops = raw_burn, self.stops_done
            elif not w["need_stop"]:
                self._tight_ref = None
        res["plan"] = self._plan_view(res["window"], ltg)
        return res

    def _plan_view(self, win, ltg):
        if not self.plan_stops or self.lap is None:
            return None
        i = self.stops_done
        if i >= len(self.plan_stops):
            return {"state": "on", "delta": 0, "text": "The plan has no more stops.", "short": "plan: no more stops"}
        planned = self.plan_stops[i]
        if win is None:
            return None
        if not win["need_stop"]:
            return {"state": "ahead", "delta": None, "planned": planned,
                    "text": f"Ahead of the plan: your fuel reaches the flag and the plan stops on lap {planned}.",
                    "short": "plan: no stop needed"}
        mine = self.lap + max(0, win["k_latest"])
        d = mine - planned
        state = "ahead" if d >= 1 else "behind" if d <= -1 else "on"
        word = {"ahead": f"Ahead of plan by {d} lap{'s' if d != 1 else ''}",
                "behind": f"Behind plan by {-d} lap{'s' if d != -1 else ''}", "on": "On plan"}[state]
        return {"state": state, "delta": d, "planned": planned,
                "text": f"{word}: the plan stops on lap {planned}, you can pit as late as lap {mine}.",
                "short": "plan " + ("on" if state == "on" else f"{d:+d}")}

    def _candidates(self, own, num, now) -> list:
        """Every call that is true right now: (kind, level, say, text, key, data)."""
        c = []
        win, ltg, burn, F = num["window"], num["ltg"], num["burn"], num["fuel"]
        unit = self.units
        on_pit = bool(own.get("OnPitRoad") or own.get("PlayerCarInPitStall"))
        fl = num["fuel_laps"]
        if win is not None and fl is not None and win["need_stop"] and fl < CRITICAL_LAPS and not on_pit:
            n = int(math.floor(fl + 1e-9))
            low = fl < 1.0
            say = "Fuel critical."
            if vp.CREW_FUEL_LAPS[0] <= n <= vp.CREW_FUEL_LAPS[1]:
                say += " " + vp.crew_laps_fuel_text(n)
            text = (f"Fuel critical: about {fl:.1f} laps of fuel and the flag is {ltg['distance']:.1f} laps away."
                    if ltg else f"Fuel critical: about {fl:.1f} laps of fuel.")
            c.append(("fuel_critical", 2 if low else 1, say, text, None, {"fuel_laps": round(fl, 2)}))
        if win is not None and win["need_stop"] and not on_pit and win["k_latest"] is not None:
            kl = win["k_latest"]
            add = win["add_l"]
            addt = f" Add {fuel_text(add, unit)}." if add else ""
            say_add = say_fuel(add, unit)
            if kl <= 0:
                c.append(("box_now", 4, "Box this lap." + (" " + say_add if say_add else ""),
                          f"Box this lap.{addt}", self.lap, {"add_l": add, "k": kl}))
            elif kl == 1:
                c.append(("box_soon", 3, "Box next lap.", f"Box next lap.{addt}", None, {"add_l": add, "k": kl}))
            elif kl <= BOX_SOON_LAPS:
                w = vp.number_words(kl)
                c.append(("box_soon", 2 if kl == 2 else 1, f"Box in {w} laps.",
                          f"Box in {kl} laps.{addt}", None, {"add_l": add, "k": kl}))
        if self.caution and win is not None and win["need_stop"] and not on_pit and burn and self.tank and ltg:
            # a cheap stop is worth calling when stopping now takes a stop off the count, or the one stop you have
            # to make is open now or soon and there is a real amount to add; never when it just adds a stop
            n = win["stops"]
            dist = ltg["distance"]
            now_n = stops_if_stop_now(F, burn, self.tank, dist, self.lap_f, self.reserve)
            need = (dist + self.reserve) * burn
            arrive = F - (1.0 - self.lap_f) * burn
            room = max(0.0, self.tank - max(0.0, arrive))
            add = round(min(_ceil1(max(0.0, need - F)), room), 1)
            meaningful = add >= max(2.0 * burn, 0.1 * self.tank)
            gains = now_n is not None and (now_n < n or (n == 1 and now_n == 1 and meaningful
                                                         and win["k_earliest"] is not None
                                                         and win["k_earliest"] <= CHEAP_STOP_LAPS))
            if gains:
                say_add = say_fuel(add, unit)
                full = " That fills the tank." if add >= room - 0.05 else ""
                c.append(("caution_box", 1, "Box now, cheap stop." + (" " + say_add if say_add else ""),
                          f"Caution: box now, cheap stop. Add {fuel_text(add, unit)}.{full}", None,
                          {"add_l": add, "stops_now": now_n, "stops_after": n}))
        if win is not None and not win["need_stop"] and win.get("tight") and not on_pit and self.race:
            sv = win["tight_save_lpl"]
            spare = win["reach_laps"] if win.get("reach_laps") is not None else 0.0     # laps spare at the flag
            if win.get("short"):
                c.append(("fuel_tight", 3, "Short of the flag.",
                          f"Short of the flag by {-spare:.1f} lap{'' if abs(spare) == 1 else 's'}: save "
                          f"{fuel_text(sv, unit, 2)} a lap to make it.", None,
                          {"save_lpl": sv, "reach_laps": spare, "short": True}))
            else:
                c.append(("fuel_tight", 2 if spare < MARGIN_LAPS else 1, "Fuel is tight.",
                          (f"Fuel is tight: {spare:.1f} laps to spare at the flag. Save {fuel_text(sv, unit, 2)} a lap to "
                           f"keep a spare lap."), None, {"save_lpl": sv, "reach_laps": spare, "short": False}))
        v = self._rival_view
        b, a = v.get("behind"), v.get("ahead")
        if b and "threat" in b["tags"]:
            ltc = b["laps_to_contact"]
            lvl = 3 if ltc <= 1.5 else 2 if ltc <= 3.0 else 1
            sr = say_rate(b["rate"])
            c.append(("threat", lvl, f"Car behind closing, {sr}.",
                      f"Car behind closing, {rate_text(b['rate'])}. Contact in {laps_phrase(ltc)}.", None,
                      {"gap": b["gap_s"], "rate": b["rate"], "idx": b["idx"]}))
        side = _num(own.get("CarLeftRight"))
        if (side is not None and int(side) in ALONGSIDE_VALUES) or \
                (b and "alongside" in b["tags"]) or (a and "alongside" in a["tags"]):
            c.append(("alongside", 1, "Car alongside.", "Car alongside.", None, {}))
        if a and "catchable" in a["tags"]:
            sr = say_rate(a["rate"])
            c.append(("catchable", 1, f"Gaining on the car ahead, {sr}.",
                      f"Gaining on the car ahead, {rate_text(a['rate'])}. Contact in {laps_phrase(a['laps_to_contact'])}.",
                      None, {"gap": a["gap_s"], "rate": a["rate"], "idx": a["idx"]}))
        if a and "pitting" in a["tags"] and not on_pit and not self.caution:
            c.append(("ahead_pits", 1, "Car ahead pitting.", "Car ahead is in the pits.", a["idx"], {"idx": a["idx"]}))
        pl = num["plan"]
        if pl is not None and self._plan_state is not None and pl["state"] != self._plan_state:
            say = {"on": "On plan.", "ahead": "Ahead of plan.", "behind": "Behind plan."}[pl["state"]]
            c.append(("plan", 1, say, pl["text"], None, {"state": pl["state"]}))
        return c

    def _edge_candidates(self, own, num) -> list:
        """Calls that are looked at once, when a lap has just finished: position and the lap against the target."""
        c = []
        cp = self.cpos
        if self._edge and not self._edge_dirty and cp and self.prev_pos and cp != self.prev_pos and not self.caution:
            up = cp < self.prev_pos
            c.append(("position", 1, "Up a place." if up else "Lost a place.",
                      f"{'Up' if up else 'Down'} to P{cp} in your class (was P{self.prev_pos}).", None,
                      {"pos": cp, "was": self.prev_pos}))
        if self._lap_time_taken and self.last_lap_s:
            tgt = self.target_for_display()
            if tgt and self.last_lap_s - tgt >= TARGET_OFF_S and self.last_lap_s - tgt <= 2.0:
                k = max(vp.CREW_OFF[0], min(vp.CREW_OFF[1], int(round((self.last_lap_s - tgt) * 10))))
                c.append(("target", 1, vp.crew_off_text(k),
                          f"Last lap {lap_text(self.last_lap_s)} was {self.last_lap_s - tgt:.1f} s off your target "
                          f"{lap_text(tgt)}.", None, {"delta": round(self.last_lap_s - tgt, 2)}))
        return c

    def target_for_display(self):
        if self.target_given:
            return self.target_given
        prev = list(self.lap_times)[:-1]
        if len(prev) >= 3:
            return _median(prev)
        return self.plan_lap_s

    def _calls(self, own, now, out, issue=True):
        edge = self._edge
        if now < self._next_calc and not edge and not self._lap_time_taken:
            return
        self._next_calc = now + 0.2
        num = self._numbers(own)
        self.last_calc = num
        win = num["window"]
        pl = num["plan"]
        cands = self._candidates(own, num, now)
        if edge or self._lap_time_taken:
            cands += self._edge_candidates(own, num)
        # plan state is remembered after the first look (so the first look is not a "change")
        if pl is not None:
            if self._plan_state is None:
                self._plan_state = pl["state"]
            elif any(k == "plan" for k, *_ in cands):
                self._plan_state = pl["state"]
        if edge:
            self.prev_pos = self.cpos if self.cpos else self.prev_pos
            self._edge = False
        self._lap_time_taken = False
        self.current = None
        best = None
        for kind, level, say, text, key, data in cands:
            cl = self._make(kind, say, text, level, own, now, **data)
            if best is None or (cl.rank, -cl.level) < (best.rank, -best.level):
                best = cl
            if issue and self._allowed(kind, level, now, key):
                self._issue(cl, now, key=key, out=out)
        self.current = best
        self.snap = self._build_snapshot(own, num)

    # ------------------------------------------------------------ what the window shows
    def still_valid(self, call: Call, own: dict | None) -> str | None:
        """Asked right before a call is spoken: None while it still holds, else why not."""
        if not isinstance(own, dict) or call.kind in ("data_lost",):
            return None
        if call.kind in ("box_now", "box_soon", "caution_box"):
            if own.get("OnPitRoad") or own.get("PlayerCarInPitStall"):
                return "already in the pits"
            if call.kind == "box_now" and call.lap is not None and _int(own.get("Lap")) is not None \
                    and _int(own.get("Lap")) != call.lap:
                return "that lap is over"
            if call.kind == "caution_box" and not ((_int(own.get("SessionFlags")) or 0) & FULL_COURSE):
                return "the caution is over"
        if call.kind == "fuel_critical" and (own.get("OnPitRoad") or own.get("PlayerCarInPitStall")):
            return "already in the pits"
        return None

    def snapshot(self) -> dict:
        s = dict(self.snap) if self.snap else self._idle_snapshot()
        s["calls"] = [c.to_dict() for c in list(self.calls)[-8:]][::-1]
        return s

    def _idle_snapshot(self) -> dict:
        return {"state": "waiting", "text": "Waiting for a race.", "race": self.race, "active": self.active,
                "units": self.units, "call": None, "overlay": ""}

    def _build_snapshot(self, own, num) -> dict:
        unit = self.units
        win, ltg, burn, F, fl = num["window"], num["ltg"], num["burn"], num["fuel"], num["fuel_laps"]
        s: dict = {"state": "lost" if self.lost else "racing" if self.active else "waiting", "race": self.race,
                   "active": self.active, "units": unit, "caution": self.caution, "flag": flags_text(self.flags),
                   "lap": self.lap, "stops_done": self.stops_done}
        cur = self.current
        s["call"] = cur.to_dict() if cur else None
        s["call_text"] = (cur.text if cur else ("Learning your fuel use: three clean laps." if burn is None and
                                                 self.race else "All good." if self.active else "Waiting for the race."))
        # position
        s["pos"] = {"class": self.cpos, "of": self.class_size, "overall": self.pos_overall,
                    "text": (f"P{self.cpos}" + (f" of {self.class_size}" if self.class_size else "") + " in class"
                             if self.cpos else "")}
        # laps to go
        if ltg:
            n = ltg["crossings"]
            s["laps_left"] = {"n": n, "kind": ltg["kind"], "distance": round(ltg["distance"], 2),
                              "text": f"{n} lap{'s' if n != 1 else ''} to go" + ("" if ltg["kind"] == "laps" else " (timed race)"),
                              "leader_lap": lap_text(num["leader_lap"][0]), "leader_how": num["leader_lap"][1]}
        else:
            s["laps_left"] = None
        # fuel
        fp = fuel_pair(F, unit)
        s["fuel"] = {**fp, "burn": fuel_pair(burn, unit) if burn else None,
                     "burn_text": (fuel_text(burn, unit, 2) + " a lap") if burn else "",
                     "burn_how": num["burn_how"], "burn_laps": self.burn.n,
                     "laps": None if fl is None else round(fl, 1),
                     "laps_text": "" if fl is None else f"{fl:.1f} laps of fuel",
                     "tank": fuel_pair(self.tank, unit),
                     "note": "Learning: needs three clean laps." if burn is None else
                             ("From " + num["burn_how"] + " until three clean laps are in." if num["burn_how"] != "laps" else "")}
        # box
        box = {"need": False, "text": "", "window_text": "", "in_laps": None, "add": None, "stops": 0}
        if win is not None:
            if not win["need_stop"]:
                spare = win["reach_laps"] if win.get("reach_laps") is not None else 0.0
                box.update(text="No stop needed.", window_text=f"Fuel reaches the flag, with {spare:.1f} laps to spare.",
                           stops=0)
                if win.get("short"):
                    box.update(text="Short of the flag: save fuel, no stop yet.", tight=True, short=True,
                               window_text=f"{-spare:.1f} lap{'' if abs(spare) == 1 else 's'} short of the flag.",
                               save_text=f"Save {fuel_text(win['tight_save_lpl'], unit, 2)} a lap to make it")
                elif win.get("tight"):
                    box.update(text="No stop needed, but fuel is tight.", tight=True,
                               save_text=f"Save {fuel_text(win['tight_save_lpl'], unit, 2)} a lap to keep a spare lap")
            else:
                kl, ke = win["k_latest"], win["k_earliest"]
                lap = self.lap
                lat = lap + max(0, kl)
                box.update(need=True, stops=win["stops"], in_laps=max(0, kl), lap_latest=lat,
                           add=fuel_pair(win["add_l"], unit), fill=fuel_pair(win["fill_l"], unit))
                if win["stops"] >= 2:
                    box["text"] = f"{win['stops']} stops needed. Box by lap {lat}."
                    box["window_text"] = f"Next stop: now to lap {lat}."
                else:
                    ear = lap + (ke or 0)
                    box["lap_earliest"] = ear
                    box["window_text"] = (f"Window: lap {ear} to lap {lat}" if ear < lat else f"Window: lap {lat}")
                    if ear > lat:
                        box["window_text"] = "No safe window with one stop: two stops."
                    box["text"] = ("Box this lap" if kl <= 0 else f"Box by lap {lat}, in {kl} lap{'s' if kl != 1 else ''}")
                box["add_text"] = f"Add {fp_text(win['add_l'], unit)}"
                sv = win["save_lpl"]
                if win["stops"] == 1:
                    real = win["save_pct"] <= SAVE_REALISTIC * 100
                    box["save_text"] = (f"Save {fuel_text(sv, unit, 2)} a lap to skip the stop"
                                        + ("" if real else " (not realistic)"))
                    box["save_realistic"] = real
        s["box"] = box
        s["plan"] = num["plan"]
        # rivals
        v = self._rival_view
        s["ahead"], s["behind"] = v.get("ahead"), v.get("behind")
        # last lap vs target
        tgt = self.target_for_display()
        ll = self.last_lap_s
        s["last_lap"] = {"s": ll, "text": lap_text(ll), "target": tgt, "target_text": lap_text(tgt),
                         "delta": None if not (ll and tgt) else round(ll - tgt, 2)}
        pit_loss = num["pit_loss"]
        s["pit_loss"] = {"s": round(pit_loss[0], 1), "how": pit_loss[1]}
        s["overlay"] = self._overlay_line(num, win)
        s["text"] = s["call_text"]
        return s

    def _overlay_line(self, num, win) -> str:
        bits = []
        fl = num["fuel_laps"]
        bits.append(f"Fuel {fl:.1f} laps" if fl is not None else "Fuel learning")
        if win is not None:
            if not win["need_stop"]:
                bits.append("fuel short" if win.get("short") else "fuel tight" if win.get("tight") else "no stop")
            else:
                lat = self.lap + max(0, win["k_latest"])
                ear = self.lap + (win["k_earliest"] or 0)
                bits.append(f"Box L{ear}-{lat}" if win["stops"] == 1 and ear < lat else f"Box L{lat}")
        pl = num["plan"]
        if pl:
            bits.append(pl["short"].replace("plan ", "Plan "))
        return " · ".join(bits)


def fp_text(litres, unit) -> str:
    """'93.0 L (24.6 gal)' with the unit from Settings > Units first."""
    p = fuel_pair(litres, unit)
    return f"{p['text']} ({p['alt']})" if p["text"] else ""


# ---------------------------------------------------------------- when a call may be said
class CallSpeaker:
    """The quiet zones. offer(calls) then pump(own, now) every reading: pump returns the one call that may be said now
    (most urgent first), or None. A call that finds no place within its time is dropped, never said late.

    Never under braking (the pedal, or a hard stop until the throttle is back), never with a car alongside (or for
    HOLD_S after it clears) unless the call IS the alongside or fuel critical call, and not within MIN_SPEAK_GAP_S of the
    last call (fuel critical and data lost are not held back by that gap)."""
    HOLD_S = 2.0
    HARD_BRAKE, HARD_THROTTLE = 0.5, 0.9
    HARD_MAX_S = 6.0

    def __init__(self, voice_on=True, braking_pedal=0.05):
        self.voice_on = voice_on
        self.braking_pedal = braking_pedal
        self.pending: list = []
        self.events: list = []            # dicts for the log: {"call": id, "what": "said"|"dropped", "why": ...}
        self._last_said = -1e9
        self._side_until = -1e9
        self._hard_since = None
        self.muted = False
        self.on_drop = None               # called with a call that found no quiet place in time (so it may be made again)

    def offer(self, calls):
        for c in calls or []:
            self.pending = [p for p in self.pending if p.kind != c.kind]       # the newest of a kind wins
            self.pending.append(c)

    def _zones(self, own, now):
        side = _int(own.get("CarLeftRight"))
        if side is not None and side in ALONGSIDE_VALUES:
            self._side_until = now + self.HOLD_S
        br, th = _num(own.get("Brake")) or 0.0, _num(own.get("Throttle")) or 0.0
        if br > self.HARD_BRAKE:
            self._hard_since = now if self._hard_since is None else self._hard_since
        elif self._hard_since is not None and (th > self.HARD_THROTTLE or now - self._hard_since > self.HARD_MAX_S):
            self._hard_since = None

    def quiet_reason(self, own, call, now) -> str | None:
        if (_num(own.get("Brake")) or 0.0) > self.braking_pedal:
            return "braking"
        if self._hard_since is not None:
            return "a hard braking zone"
        if now < self._side_until and call.kind not in QUIET_ALONGSIDE_OK:
            return "a car alongside"
        if call.kind not in LOUD and now - self._last_said < MIN_SPEAK_GAP_S:
            return "another call was just said"
        return None

    def pump(self, own, now):
        if not self.pending:
            self._zones(own or {}, now)
            return None
        own = own or {}
        self._zones(own, now)
        keep = []
        chosen = None
        for c in sorted(self.pending, key=lambda c: (c.rank, c.t)):
            if now - c.t > c.ttl:
                self._drop(c, getattr(c, "_waited", "") or "no quiet place in time")
                continue
            if not self.voice_on:
                self._drop(c, "Crew chief voice is off")
                continue
            if self.muted and c.kind not in MUTE_OK:
                self._drop(c, "muted")
                continue
            r = self.quiet_reason(own, c, now)
            if r:
                c._waited = f"waited for {r}"                          # noqa: SLF001
                keep.append(c)
                continue
            if chosen is None:
                chosen = c
            else:
                keep.append(c)
        self.pending = keep
        if chosen is not None:
            self._last_said = now
            self.events.append({"call": chosen.id, "what": "said"})
        return chosen

    def _drop(self, c, why):
        c.spoken, c.why = False, why
        self.events.append({"call": c.id, "what": "dropped", "why": why})
        if self.on_drop is not None and why not in ("Crew chief voice is off", "muted"):
            try:
                self.on_drop(c)
            except Exception:  # noqa: BLE001
                pass

    def take_events(self) -> list:
        ev, self.events = self.events, []
        return ev


# ---------------------------------------------------------------- the race log
class CrewLog:
    """Writes the crew chief's calls into the recording of the session (<Recorded>/<session>/crew.jsonl.gz, read back
    with liverec.read_jsonl). `folder()` gives that folder (or None while no recording is running); rows wait in memory
    meanwhile and go to `fallback` (a plain file next to the coach logs) if the session ends without one."""
    NAME = "crew.jsonl.gz"
    FLUSH_S = 10.0
    MAX_WAIT = 4000

    def __init__(self, folder_fn=None, fallback_dir=None, clock=time.monotonic, log=None):
        self.folder_fn = folder_fn
        self.fallback_dir = fallback_dir
        self.clock = clock
        self.log = log or (lambda m: None)
        self.rows: list = []
        self._last = clock()
        self.path = None

    def add(self, row: dict):
        self.rows.append(row)
        del self.rows[:-self.MAX_WAIT]
        if self.clock() - self._last >= self.FLUSH_S:
            self.flush()

    def _target(self):
        try:
            f = self.folder_fn() if self.folder_fn else None
        except Exception:  # noqa: BLE001
            f = None
        if f:
            from pathlib import Path
            return Path(f) / self.NAME
        return None

    def flush(self, final=False):
        import gzip
        from pathlib import Path
        self._last = self.clock()
        if not self.rows:
            return None
        p = self._target()
        if p is None:
            if not final:
                return None
            if not self.fallback_dir:
                return None
            Path(self.fallback_dir).mkdir(parents=True, exist_ok=True)
            p = Path(self.fallback_dir) / ("crew-" + time.strftime("%Y%m%d-%H%M%S") + ".jsonl.gz")
        data = ("\n".join(json.dumps(r, separators=(",", ":")) for r in self.rows) + "\n").encode("utf-8")
        try:
            with open(p, "ab") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
                    gz.write(data)
        except OSError as e:
            self.log(f"crewchief: could not write {p.name} ({e.__class__.__name__})")
            return None
        self.rows = []
        self.path = p
        return p

    def close(self):
        return self.flush(final=True)


def load_plan(library_folder, track) -> dict | None:
    """The 2.26 stint plan saved for this track (Endurance planner), as the crew chief needs it: the laps of its stops,
    the first driver's lap time and fuel use, the time a stop takes. None when there is none. Never raises."""
    try:
        from . import endurance
        saved = endurance.load_inputs(library_folder, track)
        if not saved:
            return None
        inp = saved["inputs"]
        p = endurance.plan(inp)
        stints = [x for x in p["stints"] if x.get("stop")]
        d = (inp.get("drivers") or [{}])[0]
        return {"stop_laps": [int(x["to_lap"]) for x in stints], "lap_s": d.get("lap_t"), "burn_l": d.get("burn_l"),
                "pit_loss_s": stints[0]["stop"]["stop_s"] if stints else None, "title": saved.get("title") or "",
                "saved": saved.get("saved") or ""}
    except Exception:  # noqa: BLE001
        return None


def recording_folder(library_folder):
    """The folder of the recording the recorder is writing right now (window or tray), or None."""
    try:
        from pathlib import Path
        from . import liverec
        doc = liverec.read_lock()
        if not doc or not doc.get("alive"):
            return None
        name = (doc.get("session") or {}).get("folder")
        if not name:
            return None
        p = liverec.recordings_root(library_folder) / name
        return p if p.is_dir() else None
    except Exception:  # noqa: BLE001
        return None


# the extra things the crew chief reads from your car (the rest is in livedata.COACH_CHANNELS)
CREW_OWN = ("FuelLevel", "PlayerCarInPitStall", "PlayerCarTowTime", "SessionTimeRemain", "SessionLapsRemainEx",
            "SessionFlags", "SessionState", "PlayerCarPosition", "PlayerCarClassPosition", "PlayerCarIdx", "SessionUniqueID")


def situation(chief: CrewChief) -> dict:
    """A short picture of the race for the log: what the call was made from."""
    s = chief.snap or {}
    f = s.get("fuel") or {}
    b = s.get("box") or {}
    ll = s.get("laps_left") or {}
    return {"lap": chief.lap, "lap_f": round(chief.lap_f, 3), "fuel_l": (f.get("l")), "fuel_laps": f.get("laps"),
            "burn_l": (f.get("burn") or {}).get("l"), "to_go": ll.get("n"), "stops_left": b.get("stops"),
            "box_in": b.get("in_laps"), "window": [b.get("lap_earliest"), b.get("lap_latest")],
            "add_l": (b.get("add") or {}).get("l"), "flag": s.get("flag"), "pos": (s.get("pos") or {}).get("class"),
            "ahead": _short_rival(s.get("ahead")), "behind": _short_rival(s.get("behind")),
            "stops_done": chief.stops_done}


def _short_rival(r):
    if not r:
        return None
    return {"num": r.get("num"), "gap": r.get("gap_s"), "rate": r.get("rate"), "tags": r.get("tags")}


def call_row(chief: CrewChief, c: Call, why: str = "") -> dict:
    """One log row: the situation -> the call."""
    return {"k": "call", "id": c.id, "t": c.session_t, "lap": c.lap, "kind": c.kind, "rank": c.rank, "level": c.level,
            "say": c.say, "text": c.text, "sit": situation(chief)}
