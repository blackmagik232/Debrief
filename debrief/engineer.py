"""The Race Engineer (2.31): one radio voice for every session. Practice, test drives, qualifying, races, hosted and league.

Nothing here talks to iRacing, the network or an audio device. It is fed the readings livedata.LiveReader gives (your own
channels, the CarIdx arrays of every car, the session text) and works out

  * what kind of session and what the car is doing (sessionkind),
  * the page's picture: header, practice numbers, qualifying traffic, the radio transcript and the pinned urgent message,
  * the engineer's own messages (briefing, practice notes, qualifying calls, debrief) as crewchief.Call objects, so they go
    through the SAME quiet-zone queue as the crew chief's calls (never under braking, never with a car alongside),
  * answers to the driver's questions (intents.parse -> answer), spoken and written into the transcript.

What each PC can know (the 2.30 rules stay): on a crew PC (not in the car) fuel is an estimate, every number is labelled
"(est.)" and the engineer is text only; on a spectator PC there are no own-car numbers at all.

Sector maths, hand-checked (equal thirds, lap 90.0 s): the car crosses 0.333 between two readings, pct 0.329 at t = 10.00 and
0.335 at t = 10.05. fraction = (0.333 - 0.329) / (0.335 - 0.329) = 0.667, so the crossing was at t = 10.0333.
Traffic gap, hand-checked: me at 0.500, a car at 0.530, my reference lap 90 s -> it is 0.03 x 90 = 2.7 s ahead; a car at
0.480 is 0.02 x 90 = 1.8 s behind (the distance behind is 1 - ((0.480 - 0.500) mod 1) = 0.02).
"""
from __future__ import annotations

import math
import time
from collections import deque

from . import crewchief as cc
from . import intents
from . import raceroom as rr
from . import sessionkind as sk
from . import units as _units
from . import voicepack as vp

BIG = 1e9            # no real reading is this large: a bigger one (or infinity) is a broken reading, not a number


def _num(v):
    x = cc._num(v)
    return x if x is not None and abs(x) < BIG else None


def _int(v):
    x = _num(v)
    return int(x) if x is not None else None

VERSION = 1
MAX_TRANSCRIPT = 250
SHOW_TRANSCRIPT = 80
STATE_EVERY_S = 0.5
NOTE_GAP_S = 25.0               # practice notes: at least this long apart (session time)
NOTE_SAME_LAPS = 3              # ... and the same kind of note not within this many laps
SECTOR_LOSS_S = 0.3             # "lost more than 0.3 s in a sector against your best"
CONSISTENCY_EVERY = 5           # a consistency note after every 5 clean laps
CLEAR_GAP_S = 6.0               # nobody within this many seconds of you on track = clear
TRAFFIC_AHEAD_S = 5.0           # a car on track this close ahead (on a push lap) is traffic
GAP_WINDOW_S = 12.0             # the cars nearer than this are shown
MIN_LAP_S, MAX_LAP_S = 20.0, 1200.0
FIELD_RATE_KEEP = 3.0           # seconds of gap history kept for a closing rate
UNLIMITED = 400000
OFF_LAP_S = 0.5                 # off track this long in a lap makes it not clean
WET_WORDS = {0: "", 1: "Dry", 2: "Mostly dry", 3: "Very lightly wet", 4: "Lightly wet", 5: "Moderately wet", 6: "Very wet",
             7: "Extremely wet"}
CORNERS = (("LF", "left front"), ("RF", "right front"), ("LR", "left rear"), ("RR", "right rear"))

# message kinds -> (rank for the quiet-zone queue, sound priority, how long it may wait, in seconds). Sound priority: fuel
# critical 6 > box 5 > safety / traffic 4 > race info 3 > coaching 2 > chatter 1 (the crew chief and the coach keep theirs).
# Review fix: every engineer message ranks AFTER every crew chief call (crew ranks are 1 to 10) and sounds strictly below the
# crew chief's priority 4, so a box call is never made to wait behind an answer and interrupts it mid-sentence.
KINDS = {"eng_answer": (11, 3, 12.0), "eng_traffic": (12, 3, 3.0), "eng_quali": (13, 3, 8.0), "eng_brief": (14, 3, 15.0),
         "eng_debrief": (14, 3, 25.0), "eng_note": (15, 2, 10.0), "eng_chatter": (16, 1, 8.0)}
REPEAT_OK = ("eng_answer", "eng_brief", "eng_debrief", "eng_note", "eng_chatter")


def _caps(say):
    """Each sentence of a spoken message starts with a capital ('zero point five' -> 'Zero point five')."""
    if not say:
        return say
    import re
    return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), say)


def _t(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def clock(t) -> str:
    """A session clock: 12:34, or 1:02:03 after the first hour."""
    t = _t(t)
    if t is None:
        return "--:--"
    t = max(0, int(t))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def lap_text(sec) -> str:
    sec = _t(sec)
    if sec is None or sec <= 0:
        return "--"
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:06.3f}" if m else f"{s:.3f}"


def nw(n) -> str:
    """A whole number in words (0..150 have clips)."""
    k = int(round(n))
    return vp.number_words(max(0, min(999, k)))


def dec(x) -> str:
    """1.2 -> 'one point two'; 12.4 -> 'twelve'; 0.8 -> 'zero point eight' (tenths read out under ten)."""
    x = abs(float(x))
    if x >= 10:
        return nw(min(150, round(x)))
    whole = int(x * 10 + 0.5) // 10
    tenth = int(x * 10 + 0.5) % 10
    return f"{nw(whole)} point {nw(tenth)}" if tenth else nw(whole)


def lap_say(sec):
    """A lap time the way the coach says it ('one thirty-eight nine'), or None outside what the clips cover."""
    sec = _t(sec)
    if sec is None or not (vp.LAP_TENTHS[0] / 10 <= sec < vp.LAP_TENTHS[1] / 10 + 1):
        return None
    return vp.lap_time_words(sec)


def tenths_say(d) -> str:
    """0.4 -> 'four tenths down'; 1.34 -> 'one point three seconds down'."""
    k = int(round(abs(d) * 10))
    if k < 10:
        return f"{nw(max(k, 2))} tenths down"
    return f"{dec(k / 10)} seconds down"


def _temp(c, unit):
    c = _num(c)
    if c is None:
        return None
    return c * 9.0 / 5.0 + 32.0 if unit == "mph" else c


def _med(xs):
    return cc._median(xs) if xs else None


class Lap:
    __slots__ = ("n", "s", "sectors", "valid", "why", "out", "fuel", "t", "pit")

    def __init__(self, n, s, sectors, valid, why, out, fuel, t, pit=False):
        self.n, self.s, self.sectors, self.valid, self.why, self.out, self.fuel, self.t, self.pit = \
            n, s, sectors, valid, why, out, fuel, t, pit

    def to_dict(self):
        return {"lap": self.n, "s": self.s, "text": lap_text(self.s), "sectors": [round(x, 3) if x is not None else None
                                                                              for x in (self.sectors or [])],
                "valid": self.valid, "why": self.why, "out": self.out}


def traffic(field, me_idx, my_pct, ref_lap_s, skip=(), onroad_only=True) -> dict:
    """The cars on track near you, by TRACK POSITION (any class): [{"idx", "gap_s" (+ ahead, - behind), "pct"}...].
    Distance ahead = (their pct - mine) mod 1, behind = 1 - that; seconds = distance x the reference lap."""
    out = []
    if not isinstance(field, dict) or my_pct is None or not ref_lap_s:
        return {"cars": out}
    pcts = field.get("CarIdxLapDistPct") or []
    surf = field.get("CarIdxTrackSurface") or []
    pit = field.get("CarIdxOnPitRoad") or []
    for i, p in enumerate(pcts):
        if i == me_idx or i in skip or not isinstance(p, (int, float)) or p < 0:
            continue
        if onroad_only:
            sf = surf[i] if i < len(surf) else 3
            if sf != 3:                                   # 3 = on track; -1 not in the world, 1/2 pit stall / pit lane, 0 off
                continue
            if i < len(pit) and pit[i]:
                continue
        d = (p - my_pct) % 1.0
        ahead = d <= 0.5
        dist = d if ahead else 1.0 - d
        out.append({"idx": i, "gap_s": dist * ref_lap_s if ahead else -dist * ref_lap_s, "pct": p})
    out.sort(key=lambda c: abs(c["gap_s"]))
    return {"cars": out}


def nearest(cars, ahead: bool):
    best = None
    for c in cars:
        if (c["gap_s"] >= 0) == ahead and (best is None or abs(c["gap_s"]) < abs(best["gap_s"])):
            best = c
    return best


def class_rank(field, me_idx, class_of, my_class=None):
    """My rank by best lap among the cars with a best lap (same class when the session has classes): (rank, count, ahead_gap,
    pole_gap, pole_time). ahead_gap = my best - the next quicker car's best (seconds), pole_gap = my best - the quickest."""
    bests = (field or {}).get("CarIdxBestLapTime") or []
    mine = bests[me_idx] if 0 <= me_idx < len(bests) else None
    if not isinstance(mine, (int, float)) or mine <= 0:
        return None
    mine_c = (class_of or {}).get(me_idx) if class_of else None
    rows = []
    for i, b in enumerate(bests):
        if not isinstance(b, (int, float)) or b <= 0:
            continue
        if class_of and my_class is not False and (class_of.get(i) != mine_c):
            continue
        rows.append((b, i))
    rows.sort()
    rank = 1 + sum(1 for b, i in rows if b < mine - 1e-9)
    quicker = [b for b, i in rows if b < mine - 1e-9]
    ahead_gap = (mine - max(quicker)) if quicker else None
    return {"rank": rank, "of": len(rows), "ahead_gap": ahead_gap, "pole_gap": mine - rows[0][0] if rows else None,
            "pole": rows[0][0] if rows else None, "mine": mine}


class Engineer:
    """feed() on every reading; take_calls() for what to say; ask() for a question; state() for the page."""

    def __init__(self, units="mph", target_fn=None, clock=time.monotonic):
        self.units = _units.norm(units)
        self.target_fn = target_fn
        self.clock = clock
        self.enabled = True
        self.urgent_only = False
        self.transcript: deque = deque(maxlen=MAX_TRANSCRIPT)
        self._next_id = 1
        self.calls_out: list = []
        self.say_log: list = []
        self.info_v = None
        self.info: dict = {}
        self.sections: dict = {}
        self.sess = sk.classify({}, {})
        self.car = sk.CarState()
        self.role = "none"
        self.crew_pc = False
        self.connected = False
        self.last_state_now = -1e9
        self._state = {"version": VERSION, "connected": False, "session": {}, "transcript": [], "pinned": None}
        self.s: dict = {}
        self.field = None
        self._field_key = None
        self.board: dict = {}
        self.talk: dict = {}
        self.last_said = None
        self.last_kind = None
        self.last_box = False
        self.coach_says_laps = False     # set by the live runner while the coach gives lap results (no double "new best")
        self._reset_session(None)

    # ------------------------------------------------------------ session
    def _reset_session(self, key):
        self.key = key
        self.car = sk.CarState()        # a new session starts with a fresh car state (no 'in lap' left over from practice)
        self.laps: list = []
        self.cur_sectors: list = []
        self.bounds = [1.0]
        self.bounds_how = "thirds"
        self.sec_idx = 0
        self.sec_t0 = None
        self.prev_pct = None
        self.prev_t = None
        self.prev_lap = None
        self.lap_dirty = ""
        self.off_s = 0.0
        self.lap_fuel0 = None
        self.fuel_burns: list = []
        self.fuel_last = None
        self.pending = None
        self.last_raw = None
        self.best = None
        self.best_sectors: list = []
        self.best_lap_sectors: list = []
        self.sector_best_sum = None
        self.noted_at = {}
        self.last_note_t = -1e9
        self.fuel_noted = False
        self.cons_noted = 0
        self.prev_car = "none"
        self.briefed = False
        self.brief_n = 0
        self.stint_laps = 0
        self.stint_fuel0 = None
        self.debriefed_at = None
        self.last_lap_flag = False
        self.out_said = None
        self.push_said = None
        self.traffic_said = None
        self.quali_said_lap = None
        self.cool_said = False
        self.chequered_said = False
        self.rates: dict = {}
        self.traffic_now: dict = {}
        self.tyres = None
        self.tyres_lap = None
        self.inc0 = None
        self.start_pos = None
        self._last_quali = None
        self.clear_in = None

    # ------------------------------------------------------------ transcript
    def add(self, who, text, kind="info", urgent=False, spoken=None, t=None, est=False, say=None):
        """One transcript line. who: 'engineer' | 'you' | 'coach'."""
        t = _t(t) if t is not None else _t(self.s.get("SessionTime"))
        row = {"id": self._next_id, "t": t, "clock": clock(t), "who": who, "kind": kind, "text": text, "urgent": bool(urgent),
               "spoken": spoken, "est": bool(est)}
        self._next_id += 1
        self.transcript.append(row)
        return row

    def note_spoken(self, source, text, kind="", urgent=False, spoken=True, session_t=None, est=False):
        """The coach's cues and the crew chief's calls go into the same transcript (one radio)."""
        self.add("coach" if source == "coach" else "engineer", text, kind or source, urgent, spoken, t=session_t, est=est)

    def _make(self, kind, text, say, now, lap=None, data=None):
        rank, prio, ttl = KINDS[kind]
        say = _caps(say)
        c = cc.Call(kind=kind, rank=rank, say=say or "", text=text, level=1, lap=lap, t=now,
                    session_t=_t(self.s.get("SessionTime")), priority=prio, ttl=ttl, data=data or {})
        c.id = 100000 + self._next_id
        self._next_id += 1
        row = self.add("engineer", text, kind, urgent=False, spoken=None, est=self.crew_pc)
        c.data["row"] = row["id"]
        if self.crew_pc or not say or self.urgent_only and kind not in ("eng_answer", "eng_traffic"):
            row["spoken"] = False
            if self.urgent_only and not self.crew_pc:
                row["why"] = "urgent calls only"
            return None
        self.calls_out.append(c)
        return c

    def take_calls(self) -> list:
        out, self.calls_out = self.calls_out, []
        return out

    def add_note(self, text):
        """A plain note on the radio that is never spoken (talk-back problems, for one)."""
        return self.add("engineer", text, "info", spoken=False)

    def add_call(self, c):
        """A crew chief call is a line in the transcript too (one radio). Urgent when it is fuel / box."""
        row = self.add("engineer", c.text, c.kind, urgent=c.rank <= 4, spoken=None, t=c.session_t, est=self.crew_pc)
        c.data["row"] = row["id"]
        return row

    def still_valid(self, call, own):
        """Asked right before an engineer message is said: None while it still holds, else why not."""
        if not isinstance(own, dict):
            return None
        if call.kind in ("eng_note", "eng_quali", "eng_chatter") and self.car.state in ("pits", "garage"):
            if call.say not in ("Last lap of the session.",):
                return "you are in the pits"
        if call.kind == "eng_quali" and call.say.startswith("Clear track"):
            a = (self.traffic_now or {}).get("ahead")
            if a and abs(a["gap_s"]) < CLEAR_GAP_S:
                return "traffic appeared"
        if call.kind == "eng_traffic" and call.lap is not None and _int(own.get("Lap")) not in (None, call.lap):
            return "that lap is over"
        return None

    def latest(self):
        """The newest engineer line for the overlay: {"text", "est", "urgent"} or None."""
        for r in reversed(self.transcript):
            if r["who"] == "engineer":
                return {"text": r["text"], "est": r["est"], "urgent": r["urgent"], "clock": r["clock"]}
        return None

    def mark(self, call, spoken: bool, why=""):
        """The speaker said the call (True) or dropped it (False, why): the transcript line shows which."""
        rid = (call.data or {}).get("row")
        for r in reversed(self.transcript):
            if r["id"] == rid:
                r["spoken"] = bool(spoken)
                if why:
                    r["why"] = why
                break
        if spoken:
            self.last_said = call.say or call.text
            self.last_kind = call.kind
            self.last_box = bool((call.data or {}).get("box_answer"))

    # ------------------------------------------------------------ the feed
    def feed(self, s, field, sections, version, now, role="driver", board=None):
        """One reading. role: the Race Room's role for this PC (driver / crew / spectator / none)."""
        if not isinstance(s, dict):
            return
        t = _num(s.get("SessionTime"))
        if t is None:
            return
        self.s = s
        self.board = board or {}
        self.connected = True
        self.role = role or "none"
        self.crew_pc = self.role in ("crew", "spectator")
        if sections and version != self.info_v:
            self.info_v = version
            self.sections = sections
            self.info = rr.parse_info(sections)
            self._bounds_from(sections)
        key = (_int(s.get("SessionNum")), _int(s.get("SessionUniqueID")))
        if key != self.key:
            new = self.key is not None
            self._reset_session(key)
            self._bounds_from(self.sections)
            self.sess = sk.classify(self.sections, s)
            if new:
                self.add("engineer", f"New session: {self.sess['label']}.", "session", t=t)
        else:
            self.sess = sk.classify(self.sections, s)
        fam = self.sess["family"]
        finished = (_int(s.get("SessionState")) or 0) >= 5 or bool((_int(s.get("SessionFlags")) or 0) & sk.F_CHECKERED)
        prev = self.car.state
        cs = self.car.update(s, now, fam, finished)
        self.prev_car = prev
        if field is not None and id(field) != self._field_key:
            self._field_key = id(field)
            self.field = field
            self._traffic(now)
        if self.role == "driver" or self.s.get("IsOnTrack"):
            self._laps(s, t, now)
        self._messages(s, t, now, prev, cs, finished)
        if now - self.last_state_now >= STATE_EVERY_S:
            self.last_state_now = now
            self._state = self._build(now)

    def _bounds_from(self, sections):
        """Sector boundaries: SplitTimeInfo.Sectors[].SectorStartPct, else equal thirds (and the page says which)."""
        secs = ((sections or {}).get("SplitTimeInfo") or {}).get("Sectors") or []
        pcts = []
        for x in secs:
            v = _num(x.get("SectorStartPct")) if isinstance(x, dict) else None
            if v is not None:
                pcts.append(v)
        pcts = sorted(set(round(p, 6) for p in pcts))
        if len(pcts) >= 2 and pcts[0] <= 0.001:
            self.bounds = [p for p in pcts[1:] if 0.0 < p < 1.0] + [1.0]
            self.bounds_how = "iRacing's sector lines"
        else:
            self.bounds = [1.0 / 3, 2.0 / 3, 1.0]
            self.bounds_how = "equal thirds"

    def _ref_lap(self):
        b = self.best.s if self.best else None
        if b:
            return b
        t = self._target()
        return t[0] if t else 100.0

    def _target(self):
        """(seconds, how): the saved target (Prepare) when there is one, else your best this session."""
        if self.target_fn is not None:
            try:
                v = self.target_fn()
            except Exception:  # noqa: BLE001
                v = None
            v = _t(v)
            if v and v > 0:
                return v, "your target"
        if self.best:
            return self.best.s, "your best"
        return None

    # ------------------------------------------------------------ laps and sectors
    def _laps(self, s, t, now):
        pct = _num(s.get("LapDistPct"))
        lap = _int(s.get("Lap"))
        if pct is None or lap is None:
            return
        sf = _int(s.get("PlayerTrackSurface"))
        if self.prev_t is not None and sf == 0:
            self.off_s += max(0.0, min(1.0, t - self.prev_t))
        if (_num(s.get("PlayerCarTowTime")) or 0) > 0:
            self.lap_dirty = self.lap_dirty or "towed"
        if _truthy(s.get("OnPitRoad")) or self.car.state in ("pits", "garage"):
            self.lap_dirty = self.lap_dirty or "pit"
        inc = _int(s.get("PlayerCarMyIncidentCount"))
        if inc is not None:
            if self.inc0 is None:
                self.inc0 = inc
            elif inc > self.inc0:
                self.lap_dirty = self.lap_dirty or "incident"
                self.inc0 = inc
        fuel = _num(s.get("FuelLevel"))
        if fuel is not None:
            if self.lap_fuel0 is None:
                self.lap_fuel0 = fuel
            if self.stint_fuel0 is None:
                self.stint_fuel0 = fuel
        # pending lap time: wait for LapLastLapTime to change
        if self.pending is not None:
            raw = _num(s.get("LapLastLapTime"))
            if (raw and raw > 0 and raw != self.pending["before"]) or t - self.pending["t"] > 1.0:
                self._finish_lap(raw if raw and raw > 0 and raw != self.pending["before"] else None, t, now)
        if self.prev_lap is not None and lap != self.prev_lap and self.prev_pct is not None:
            if lap == self.prev_lap + 1 and self.prev_pct > 0.5 and pct < 0.5:
                x = self._cross(self.prev_pct, 1.0 + pct, 1.0, self.prev_t, t)
                self._sector_end(x)
                secs = list(self.cur_sectors)
                if self.pending is not None:
                    self._finish_lap(None, t, now)
                self.pending = {"lap": self.prev_lap, "t": t, "before": self.last_raw, "sectors": secs,
                                "out": self.car.out_lap_prev, "dirty": self.lap_dirty or ("off track" if self.off_s > OFF_LAP_S else ""),
                                "fuel_used": None if (fuel is None or self.lap_fuel0 is None) else self.lap_fuel0 - fuel,
                                "fuel_up": fuel is not None and self.lap_fuel0 is not None and fuel > self.lap_fuel0 + 0.05}
                self.cur_sectors, self.sec_idx, self.sec_t0 = [], 0, x
                self.lap_dirty, self.off_s, self.lap_fuel0 = "", 0.0, fuel
            else:
                self.lap_dirty = self.lap_dirty or "jump"        # a tow or a reset: not a normal lap change
                self.cur_sectors, self.sec_idx, self.sec_t0 = [], 0, None
        elif self.prev_pct is not None and self.prev_t is not None:
            if abs(pct - self.prev_pct) > 0.4:
                self.lap_dirty = self.lap_dirty or "jump"
                self.cur_sectors, self.sec_idx, self.sec_t0 = [], 0, None
            else:
                while self.sec_idx < len(self.bounds) - 1 and self.prev_pct < self.bounds[self.sec_idx] <= pct:
                    x = self._cross(self.prev_pct, pct, self.bounds[self.sec_idx], self.prev_t, t)
                    self._sector_end(x)
                    self.sec_t0 = x
                    self.sec_idx += 1
        if self.sec_t0 is None and self.prev_lap is None:
            self.sec_t0 = None
        self.prev_pct, self.prev_t, self.prev_lap = pct, t, lap

    @staticmethod
    def _cross(p0, p1, b, t0, t1):
        """When a line at b was crossed between two readings (linear). Hand-check: p0 .329 @10.00, p1 .335 @10.05, b .333 -> 10.0333."""
        if p1 <= p0:
            return t1
        f = (b - p0) / (p1 - p0)
        return t0 + max(0.0, min(1.0, f)) * (t1 - t0)

    def _sector_end(self, x):
        if self.sec_t0 is not None and self.sec_idx <= len(self.bounds) - 1:
            self.cur_sectors.append(x - self.sec_t0)
        elif self.sec_t0 is None:
            self.cur_sectors.append(None)

    def _finish_lap(self, raw, t, now):
        p, self.pending = self.pending, None
        secs = p["sectors"]
        ok_secs = bool(secs) and len(secs) == len(self.bounds) and all(x is not None for x in secs)
        sec_sum = sum(secs) if ok_secs else None
        s = raw if raw else sec_sum
        if raw:
            self.last_raw = raw
        why = p["dirty"]
        if p["out"] and not why:
            why = "out lap"
        if s is None or not (MIN_LAP_S <= s <= MAX_LAP_S):
            why = why or "no time"
        elif sec_sum and abs(sec_sum - s) > 1.5:
            secs = None                                        # the sector lines didn't match the lap: don't trust the splits
        if self.laps and why == "" and self.best is None:
            pass
        valid = not why
        lap = Lap(p["lap"], s, secs if ok_secs else None, valid, why, bool(p["out"]), p["fuel_used"], t, pit=(why == "pit"))
        self.laps.append(lap)
        del self.laps[:-200]
        self.stint_laps += 1
        new_best = False
        if valid and s:
            if self.best is None or s < self.best.s - 1e-9:
                new_best = self.best is not None
                self.best = lap
            if lap.sectors:
                if not self.best_sectors:
                    self.best_sectors = list(lap.sectors)
                else:
                    self.best_sectors = [min(a, b) for a, b in zip(self.best_sectors, lap.sectors)]
        if valid and p["fuel_used"] is not None and not p["fuel_up"] and 0.05 < p["fuel_used"] < 80:
            self.fuel_burns.append(p["fuel_used"])
            del self.fuel_burns[:-8]
        self._after_lap(lap, new_best, t, now)

    # ------------------------------------------------------------ notes after a lap
    def _clean_laps(self):
        return [x for x in self.laps if x.valid]

    def consistency(self):
        """(spread of the last 5 clean laps, spread of the 5 before) in seconds, or None."""
        c = [x.s for x in self._clean_laps()]
        if len(c) < 3:
            return None
        last = c[-5:]
        prev = c[-10:-5]
        return (max(last) - min(last), (max(prev) - min(prev)) if len(prev) >= 3 else None, len(last))

    def sector_loss(self, lap):
        """(sector index, seconds lost) of the biggest loss of this lap against your best sectors, or None."""
        if not lap.sectors or not self.best_lap_secs():
            return None
        worst = None
        for i, (a, b) in enumerate(zip(lap.sectors, self.best_lap_secs())):
            d = a - b
            if worst is None or d > worst[1]:
                worst = (i, d)
        return worst

    def best_lap_secs(self):
        return self.best.sectors if self.best and self.best.sectors else None

    def _after_lap(self, lap, new_best, t, now):
        fam = self.sess["family"]
        if self.crew_pc:
            return
        if fam == "race":
            return                                              # the crew chief talks in a race
        if fam == "qualify":
            self._quali_after(lap, new_best, t, now)
            return
        if fam != "practice" or not lap.valid:
            return
        n = len(self._clean_laps())
        if t - self.last_note_t < NOTE_GAP_S:
            return
        note = None
        if new_best and n >= 2:
            ls = lap_say(lap.s)
            if ls:
                note = ("best", f"New best, {lap_text(lap.s)}.", f"New best. {ls}.")
        if note is None and not self.fuel_noted and len(self.fuel_burns) >= 2 and self.role == "driver":
            burn = _med(self.fuel_burns)
            f = _num(self.s.get("FuelLevel"))
            if burn and f is not None:
                self.fuel_noted = True
                laps_left = int(f // burn)
                u = "gallons" if self.units == "mph" else "litres"
                b = burn / cc.GAL_L if self.units == "mph" else burn
                note = ("fuel", f"Fuel is now measured: {b:.2f} {u[:-1] if False else u} a lap, {laps_left} laps in the tank.",
                        f"Fuel is now measured. {dec(b)} {u} a lap. {nw(min(150, laps_left))} laps in the tank.")
        if note is None:
            c = self.consistency()
            if c and n % CONSISTENCY_EVERY == 0 and n != self.cons_noted and c[1] is not None:
                self.cons_noted = n                      # judged once for every five clean laps
                if c[0] < c[1] - 0.1:
                    note = ("cons", "Consistency is improving.", "Consistency is improving.")
                elif c[0] > c[1] + 0.2:
                    note = ("cons", "Laps are getting scattered, settle down.", "Laps are getting scattered, settle down.")
        if note is None and lap.sectors and self.best_lap_secs() and lap is not self.best:
            w = self.sector_loss(lap)
            if w and w[1] > SECTOR_LOSS_S and (lap.s - (self.best.s if self.best else lap.s)) > 0.0:
                i, d = w
                if self._same_ok("sector"):
                    note = ("sector", f"Lost {d:.1f} s in sector {i + 1} against your best.",
                            f"Sector {nw(i + 1)}, {tenths_say(d)}.")
        if note:
            kind, text, say = note
            if kind != "best" and not self._same_ok(kind):
                return
            self.noted_at[kind] = lap.n
            self.last_note_t = t
            if kind == "best" and self.coach_says_laps:
                # the coach already says "New best, up four tenths" for this lap: one voice, so the engineer writes it only
                self.add("engineer", text, "eng_note", spoken=False)
                return
            self._make("eng_note", text, say, now, lap=lap.n)

    def _same_ok(self, kind):
        last = self.noted_at.get(kind)
        return last is None or (self.laps[-1].n - last) >= NOTE_SAME_LAPS if self.laps else True

    def _quali_after(self, lap, new_best, t, now):
        if not lap.valid or self.car.state not in ("flying", "in", "cooldown", "out"):
            return
        r = self._rank()
        if r is None:
            return
        if self._last_quali == (lap.n, r["rank"]):
            return
        self._last_quali = (lap.n, r["rank"])
        n = r["rank"]
        if n == 1:
            text = f"That's P1, {lap_text(r['mine'])}. You're on pole."
            say = "That's position one."
            if r["of"] > 1:
                pass
        else:
            g = r["ahead_gap"]
            k = max(1, int(round(g * 10))) if g is not None else None
            if k is not None and k < 10:
                say = f"That's position {nw(n)}, " + (f"a tenth off position {nw(n - 1)}." if k == 1 else f"{nw(k)} tenths off position {nw(n - 1)}.")
                text = f"That's P{n}, {g:.1f} s off P{n - 1}."
            elif k is not None:
                say = f"That's position {nw(n)}, {dec(g)} seconds off position {nw(n - 1)}." if g < 10 else f"That's position {nw(n)}."
                text = f"That's P{n}, {g:.1f} s off P{n - 1}."
            else:
                say, text = f"That's position {nw(n)}.", f"That's P{n}."
        self._make("eng_quali", text, say, now, lap=lap.n)

    def _rank(self):
        if self.field is None or self.info.get("car_idx") is None:
            return None
        multi = (self.info.get("class_of") and len(set(self.info["class_of"].values())) > 1)
        return class_rank(self.field, self.info["car_idx"], self.info.get("class_of") if multi else None)

    # ------------------------------------------------------------ traffic (qualifying and practice)
    def _traffic(self, now):
        s = self.s
        idx = self.info.get("car_idx")
        pct = _num(s.get("LapDistPct"))
        if idx is None or pct is None:
            self.traffic_now = {}
            return
        ref = self._ref_lap()
        pace = set(self.info.get("pace") or ()) | set(self.info.get("spectators") or ())
        tr = traffic(self.field, idx, pct, ref, skip=pace)
        cars = tr["cars"]
        t = _num(s.get("SessionTime")) or 0.0
        for c in cars:
            h = self.rates.setdefault(c["idx"], deque(maxlen=12))
            h.append((t, c["gap_s"]))
        keep = {c["idx"] for c in cars}
        for k in list(self.rates):
            if k not in keep:
                del self.rates[k]
        near = [c for c in cars if abs(c["gap_s"]) <= GAP_WINDOW_S]
        for c in near:
            c["rate"] = self._rate(c["idx"], t)
            e = (self.info.get("entries") or {}).get(c["idx"]) or [{}]
            c["num"] = str(e[0].get("CarNumber") or "")
            c["name"] = str(e[0].get("UserName") or "")
        a, b = nearest(cars, True), nearest(cars, False)
        self.traffic_now = {"cars": near[:8], "ahead": next((c for c in near if a and c["idx"] == a["idx"]), None),
                            "behind": next((c for c in near if b and c["idx"] == b["idx"]), None), "ref_lap": ref}
        self.clear_in = self._clear_estimate(a, b)

    def _rate(self, idx, t):
        """Seconds of gap change per second of session time over the last FIELD_RATE_KEEP seconds (+ means opening)."""
        h = self.rates.get(idx)
        if not h or len(h) < 3:
            return None
        pts = [(x, g) for x, g in h if t - x <= FIELD_RATE_KEEP]
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 1.0:
            return None
        g0, g1 = pts[0][1], pts[-1][1]
        if abs(g1 - g0) > 20 or (g0 >= 0) != (g1 >= 0):
            return None                                       # passed each other: no rate
        return (abs(g1) - abs(g0)) / (pts[-1][0] - pts[0][0])

    def _clear_estimate(self, a, b):
        """Seconds until there is nobody within CLEAR_GAP_S ahead: the car ahead is pulling away. 0 when already clear,
        None when it is not pulling away (or unknown). Hand-check: gap 3 s, opening 0.5 s per s -> (6 - 3) / 0.5 = 6 s."""
        if a is None or abs(a["gap_s"]) >= CLEAR_GAP_S:
            return 0.0
        r = self._rate(a["idx"], _num(self.s.get("SessionTime")) or 0.0)
        if r is None or r < 0.05:
            return None
        return min(90.0, (CLEAR_GAP_S - abs(a["gap_s"])) / r)

    # ------------------------------------------------------------ messages that come from the car's state
    def _messages(self, s, t, now, prev, cs, finished):
        if self.crew_pc and self.role != "crew":
            return
        fam = self.sess["family"]
        left = _num(s.get("SessionTimeRemain"))
        lap_now = _int(s.get("Lap"))
        # leaving the pits: a short briefing
        if cs == "out" and prev in ("pits", "garage") and not self.crew_pc:
            self.stint_laps, self.stint_fuel0, self.briefed = 0, _num(s.get("FuelLevel")), True
            self._brief(s, t, now)
        if fam == "qualify" and not self.crew_pc:
            self._quali_calls(s, t, now, cs, prev, left, lap_now)
        # pit entry: a short debrief (practice, test, qualifying)
        if prev in ("flying", "in", "cooldown", "out", "off") and cs == "pits" and fam in ("practice", "qualify") \
                and not self.crew_pc and self.stint_laps >= 1:
            self._debrief(s, t, now, "pit")
        if finished and not self.chequered_said and fam != "other":
            self.chequered_said = True
            if fam == "race":
                self._chequered_race(s, t, now)
            elif not self.crew_pc and self.stint_laps >= 1:
                self._debrief(s, t, now, "flag")
        if fam in ("practice", "qualify") and left is not None and 0 < left < UNLIMITED and not self.crew_pc:
            ref = self._ref_lap()
            pct = _num(s.get("LapDistPct"))
            # the clock runs out during THIS lap when the time left is less than what is left of the lap: then this is the
            # last one (the flag falls at the line). Earlier than that, a whole lap is still to come.
            ends_this_lap = pct is not None and 0.0 <= pct <= 1.0 and left <= (1.0 - pct) * ref
            if ends_this_lap and cs in ("out", "flying") and not self.last_lap_flag and (self.sess["kind"] not in ("test",)):
                self.last_lap_flag = True
                self._make("eng_quali", "Last lap of the session.", "Last lap of the session.", now)

    def _brief(self, s, t, now):
        fam = self.sess["family"]
        try:
            self._brief_inner(s, t, now, fam)
        finally:
            self.brief_n += 1

    def _brief_inner(self, s, t, now, fam):
        parts_t, parts_s = ["Copy."], ["Copy."]
        tg = self._target()
        if fam in ("practice", "qualify") and tg:
            ls = lap_say(tg[0])
            if ls:
                parts_t.append(f"Target {lap_text(tg[0])} ({tg[1]}).")
                parts_s.append(f"Target is {ls}.")
        fuel = _num(s.get("FuelLevel"))
        burn = _med(self.fuel_burns)
        n = None
        bl = (self.board.get("fuel") or {}).get("laps") if fam == "race" else None
        if bl is not None and _num(bl) is not None and 0 <= bl < 1000:
            n = int(bl)                          # the race's own fuel laps (its burn, after a stop too)
        elif fuel is not None and burn:
            n = int(fuel // burn)
        if n is not None:
            parts_t.append(f"Fuel for {n} laps.")
            parts_s.append(f"Fuel for {nw(min(150, n))} laps.")
        if fam == "race":
            pos = _int(s.get("PlayerCarClassPosition")) or _int(s.get("PlayerCarPosition"))
            if pos and pos > 0:
                grid = self.brief_n == 0 and (self.sess.get("phase") in ("gridding", "formation") or (_int(s.get("Lap")) or 0) <= 1)
                if grid:                          # the real start; after a stop (or joining late) it is where he is running
                    parts_t.append(f"Start position {pos}.")
                    parts_s.append(f"Start position {nw(min(150, pos))}.")
                else:
                    parts_t.append(f"You are position {pos}.")
                    parts_s.append(f"You are position {nw(min(150, pos))}.")
            box = (self.board.get("box") or {})
            if box.get("need") and box.get("in_laps") is not None and 2 <= box["in_laps"] <= 120:
                parts_t.append(f"Box in {box['in_laps']} laps.")
                parts_s.append(f"Box in {nw(box['in_laps'])} laps.")
        unit = self.units
        tt = _temp(s.get("TrackTempCrew"), unit)
        if tt is not None and 0 <= tt < 150 and fam != "race":
            parts_t.append(f"Track temp {tt:.0f}.")
            parts_s.append(f"Track temp {nw(tt)}.")
        if fam == "qualify":
            parts_t = ["Out lap, warm the tyres."] + parts_t[1:]
            parts_s = ["Out lap, warm the tyres."] + parts_s[1:]
        if len(parts_t) > 1:
            self._make("eng_brief", " ".join(parts_t), " ".join(parts_s), now)

    def _quali_calls(self, s, t, now, cs, prev, left, lap_now):
        lap = lap_now
        if cs == "out":
            pct = _num(s.get("LapDistPct")) or 0.0
            if pct > 0.85 and self.push_said != lap:
                self.push_said = lap
                a = self.traffic_now.get("ahead") if self.traffic_now else None
                if a and abs(a["gap_s"]) < CLEAR_GAP_S:
                    n = max(1, int(round(abs(a["gap_s"]))))
                    self._make("eng_quali", f"Traffic ahead, {abs(a['gap_s']):.1f} s. Back off for a gap.",
                               f"Traffic ahead in {nw(n)} seconds, back off for a gap.", now, lap=lap)
                elif self.field is not None:
                    self._make("eng_quali", "Clear track ahead, push this lap.", "Clear track ahead, push this lap.", now, lap=lap)
        if cs == "flying":
            a = self.traffic_now.get("ahead") if self.traffic_now else None
            if a and 0 < a["gap_s"] <= TRAFFIC_AHEAD_S and (a.get("rate") is not None and a["rate"] < -0.1) \
                    and self.traffic_said != lap:
                self.traffic_said = lap
                n = max(1, int(round(a["gap_s"])))
                self._make("eng_traffic", f"Traffic ahead in {n} s.", f"Traffic ahead in {nw(n)} seconds.", now, lap=lap)
        if cs in ("in", "cooldown") and prev not in ("in", "cooldown") and not self.cool_said:
            self.cool_said = True
            self._make("eng_quali", "Cool-down lap, bring it in.", "Cool-down lap, bring it in.", now, lap=lap)
        if cs == "flying":
            self.cool_said = False

    def _debrief(self, s, t, now, why):
        """A short debrief: best lap, laps done, fuel used and the one thing to work on."""
        if self.debriefed_at == (self.stint_laps, why) or self.stint_laps < 1:
            return
        self.debriefed_at = (self.stint_laps, why)
        tx, sy = ["Debrief."], ["Debrief."]
        if self.best:
            ls = lap_say(self.best.s)
            tx.append(f"Best lap {lap_text(self.best.s)}.")
            if ls:
                sy.append(f"Best lap {ls}.")
        n = self.stint_laps
        tx.append(f"{n} lap{'s' if n != 1 else ''}.")
        sy.append(f"{nw(min(150, n))} laps." if n != 1 else "One lap.")
        fuel = _num(s.get("FuelLevel"))
        if self.role == "driver" and fuel is not None and self.stint_fuel0 is not None and self.stint_fuel0 - fuel > 0.2:
            used = self.stint_fuel0 - fuel
            if self.units == "mph":
                tx.append(f"Used {used / cc.GAL_L:.1f} gallons.")
                sy.append(f"Used {dec(used / cc.GAL_L)} gallons.")
            else:
                tx.append(f"Used {used:.1f} litres.")
                sy.append(f"Used {dec(used)} litres.")
        w = self.work_on()
        if w:
            tx.append(w[0])
            sy.append(w[1])
        self.stint_laps, self.stint_fuel0 = 0, fuel
        self._make("eng_debrief", " ".join(tx), " ".join(x for x in sy if x), now)

    def work_on(self):
        """The one thing to work on: the sector where your clean laps lose most against your best sector, else
        consistency. (text, say) or None."""
        laps = [x for x in self._clean_laps() if x.sectors][-8:]
        if len(laps) >= 2 and self.best_sectors:
            loss = []
            for i in range(len(self.best_sectors)):
                vals = [x.sectors[i] for x in laps]
                loss.append(_med(vals) - self.best_sectors[i])
            i = max(range(len(loss)), key=lambda k: loss[k])
            if loss[i] >= 0.15:
                return (f"Work on sector {i + 1}, you lose {loss[i]:.1f} s there.", f"Work on sector {nw(i + 1)}.")
        c = self.consistency()
        if c and c[0] > 0.6:
            return ("Work on consistency.", "Work on consistency.")
        if len(laps) >= 2:
            return ("Nothing stands out.", "Nothing stands out.")
        return None

    def _chequered_race(self, s, t, now):
        pos = _int(s.get("PlayerCarClassPosition")) or _int(s.get("PlayerCarPosition"))
        tx, sy = ["Chequered flag."], ["Chequered flag."]
        if pos and pos > 0:
            tx.append(f"You finish P{pos}.")
            sy.append(f"That's position {nw(min(150, pos))}.")
        if self.best:
            ls = lap_say(self.best.s)
            tx.append(f"Best lap {lap_text(self.best.s)}.")
            if ls:
                sy.append(f"Best lap {ls}.")
        self._make("eng_debrief", " ".join(tx), " ".join(sy), now)

    # ------------------------------------------------------------ what the page shows
    def _build(self, now):
        s, fam = self.s, self.sess["family"]
        unit = self.units
        left = _num(s.get("SessionTimeRemain"))
        laps_left = _int(s.get("SessionLapsRemainEx"))
        tl = clock(left) if left is not None and 0 <= left < UNLIMITED else ""
        ll = laps_left if laps_left is not None and 0 <= laps_left < 30000 else None
        me = self.info.get("car_idx")
        cls_name = ""
        if self.info.get("me"):
            cls_name = str(self.info["me"].get("CarClassShortName") or "")
        pos = _int(s.get("PlayerCarPosition")) or None
        cpos = _int(s.get("PlayerCarClassPosition")) or None
        multi = (self.sess.get("classes") or 1) > 1
        wet = WET_WORDS.get(_int(s.get("TrackWetness")) or 0, "")
        at, tt = _temp(s.get("AirTemp"), unit), _temp(s.get("TrackTempCrew"), unit)
        u = "°F" if unit == "mph" else "°C"
        weather = {"air": None if at is None else round(at), "track": None if tt is None else round(tt), "unit": u, "wet": wet,
                   "text": " · ".join(x for x in ((f"Air {at:.0f}{u}" if at is not None else ""),
                                                   (f"Track {tt:.0f}{u}" if tt is not None else ""), wet) if x)}
        sess = dict(self.sess)
        sess.update({"track": self.info.get("track") or sess.get("track"), "car": self.info.get("car"),
                     "class": cls_name, "time_left_text": tl, "time_left_s": left if tl else None, "laps_left": ll,
                     "role": self.role, "crew_pc": self.crew_pc, "weather": weather, "units": unit,
                     "pos": {"overall": pos if (fam in ("qualify", "race") or (pos and pos > 0 and self._multi())) else None,
                             "class": cpos if (multi and fam in ("qualify", "race")) else None}})
        st = {"version": VERSION, "connected": True, "session": sess, "car": {"state": self.car.state, "text": self.car.text()},
              "transcript": [dict(r) for r in list(self.transcript)[-SHOW_TRANSCRIPT:]][::-1],
              "pinned": self._pinned(), "enabled": self.enabled, "urgent_only": self.urgent_only, "family": fam,
              "estimated": self.crew_pc}
        if fam in ("practice", "race", "other") or fam == "qualify":
            st["practice"] = self._practice_view()
        if fam == "qualify" or fam == "practice":
            st["traffic"] = self._traffic_view()
        if fam == "qualify":
            st["quali"] = self._quali_view()
        return st

    def _multi(self):
        return len(self.info.get("entries") or {}) > 1

    def _pinned(self):
        c = (self.board or {}).get("call")
        if c and c.get("rank", 99) <= 4 and not self.crew_pc:
            txt = str(c.get("text") or c.get("say") or "")
            return {"text": txt.upper().rstrip("."), "kind": c.get("kind"), "rank": c.get("rank"), "lap": c.get("lap")}
        a = None
        if self.board.get("estimated") and (self.board.get("box") or {}).get("need"):
            a = None
        return a

    def _practice_view(self):
        last = self.laps[-1] if self.laps else None
        tg = self._target()
        best = self.best
        delta = (last.s - tg[0]) if (last and last.valid and tg) else None
        secs = []
        bl = self.best_lap_secs()
        for i in range(len(self.bounds)):
            lastv = last.sectors[i] if last and last.sectors else None
            bestv = bl[i] if bl else None
            secs.append({"n": i + 1, "last": lastv, "best": bestv,
                         "delta": None if (lastv is None or bestv is None) else round(lastv - bestv, 3),
                         "best_ever": self.best_sectors[i] if i < len(self.best_sectors) else None})
        cons = self.consistency()
        burn = _med(self.fuel_burns)
        fuel = _num(self.s.get("FuelLevel"))
        u = "gal" if self.units == "mph" else "L"
        k = (1 / cc.GAL_L) if self.units == "mph" else 1.0
        fv = {"burn": None if burn is None else round(burn * k, 2), "unit": u, "level": None if fuel is None else round(fuel * k, 1),
              "laps": None if (burn is None or fuel is None) else round(fuel / burn, 1), "n": len(self.fuel_burns),
              "estimated": self.crew_pc}
        return {"last": None if not last else last.to_dict(), "best": None if not best else best.to_dict(),
                "target": None if not tg else {"s": tg[0], "text": lap_text(tg[0]), "how": tg[1]},
                "delta": None if delta is None else round(delta, 3), "sectors": secs, "sector_how": self.bounds_how,
                "consistency": None if not cons else {"spread": round(cons[0], 3), "prev": None if cons[1] is None else round(cons[1], 3),
                                                      "n": cons[2]},
                "fuel": fv, "tyres": self._tyres(), "laps": [x.to_dict() for x in self.laps[-30:]][::-1],
                "clean": len(self._clean_laps()), "ref_lap": self._ref_lap()}

    def _tyres(self):
        s = self.s
        out = []
        unit = self.units
        for pre, nm in CORNERS:
            tc = [_num(s.get(f"{pre}tempC{x}")) for x in ("L", "M", "R")]
            wr = [_num(s.get(f"{pre}wear{x}")) for x in ("L", "M", "R")]
            if any(v is not None for v in tc) or any(v is not None for v in wr):
                tv = [x for x in tc if x is not None]
                wv = [x for x in wr if x is not None]
                out.append({"corner": nm, "temp": None if not tv else round(_temp(sum(tv) / len(tv), unit)),
                            "wear_pct": None if not wv else round(100.0 * (1.0 - min(wv)))})
        if out:
            self.tyres, self.tyres_lap = out, _int(s.get("Lap"))
        if self.tyres is None:
            return None
        return {"corners": self.tyres, "lap": self.tyres_lap, "unit": "°F" if unit == "mph" else "°C",
                "note": "Read in the pits: the numbers are from lap %s." % self.tyres_lap}

    def _traffic_view(self):
        t = self.traffic_now or {}
        def one(c):
            if not c:
                return None
            return {"gap_s": round(abs(c["gap_s"]), 1), "num": c.get("num"), "name": c.get("name"),
                    "rate": None if c.get("rate") is None else round(c["rate"], 2)}
        return {"ahead": one(t.get("ahead")), "behind": one(t.get("behind")), "cars": len(t.get("cars") or []),
                "clear_in_s": None if self.clear_in is None else round(self.clear_in), "ref_lap": t.get("ref_lap")}

    def _quali_view(self):
        r = self._rank()
        return {"rank": None if not r else r["rank"], "of": None if not r else r["of"],
                "ahead_gap": None if not r or r["ahead_gap"] is None else round(r["ahead_gap"], 3),
                "pole_gap": None if not r or r["pole_gap"] is None else round(r["pole_gap"], 3),
                "pole": None if not r else r["pole"], "pole_text": "" if not r else lap_text(r["pole"]),
                "state": self.car.state, "state_text": self.car.text()}

    def state(self) -> dict:
        st = dict(self._state)
        if st.get("connected"):
            # the radio list is read fresh: an answer to a question lands between two readings
            st["transcript"] = [dict(r) for r in list(self.transcript)[-SHOW_TRANSCRIPT:]][::-1]
            st["pinned"] = self._pinned()
        return st

    def idle_state(self, text="Waiting for iRacing."):
        return {"version": VERSION, "connected": False, "text": text, "session": {}, "transcript":
                [dict(r) for r in list(self.transcript)[-SHOW_TRANSCRIPT:]][::-1], "pinned": None, "enabled": self.enabled}

    # ------------------------------------------------------------ questions
    def ask(self, text, now=None, source="typed") -> dict:
        """The driver's question (typed, or recognised from the microphone): the answer is written in the transcript and
        returned as {"intent", "text", "say"}. say is None when it must stay text (a crew PC's fuel numbers)."""
        now = self.clock() if now is None else now
        q = intents.parse(text)
        you = str(text or "").strip()[:120]
        if you:
            self.add("you", you, "question")
        it = q["intent"]
        ans = self.answer(it)
        text_a, say = ans
        say = _caps(say)
        if it == "quiet":
            self.urgent_only = True
        elif it == "talk":
            self.urgent_only = False
        if it == "repeat" and self.last_said:
            if self.last_kind in REPEAT_OK and not self.last_box:
                text_a, say = self.last_said, self.last_said
            else:
                # a crew call (box, fuel, car behind) or a traffic / qualifying call goes stale in seconds: never replay it
                text_a, say = "Nothing urgent pending.", "Nothing urgent pending."
        if self.crew_pc and (it in ("fuel", "box") or (say and ("fuel" in say.lower() or "litre" in say.lower() or "gallon" in say.lower()))):
            say = None
        elif self.crew_pc:
            say = None
        c = None
        if say:
            rank, prio, ttl = KINDS["eng_answer"]
            c = cc.Call(kind="eng_answer", rank=rank, say=_caps(say), text=text_a, level=1, t=now, session_t=_t(self.s.get("SessionTime")),
                        priority=prio, ttl=ttl, data={})
            if it == "box" and say.lower().startswith("box"):
                # 2.31 review round 2: a spoken "Box this lap" answer obeys the crew chief's own box rules right before it is
                # said (past pit entry, final lap, already in the pits, that lap is over), and is never replayed by "say again".
                c.data["box_answer"] = True
                c.lap = _int((self.s or {}).get("Lap")) if isinstance(getattr(self, "s", None), dict) else None
            c.id = 100000 + self._next_id
            self._next_id += 1
            row = self.add("engineer", text_a, "eng_answer", spoken=None, est=self.crew_pc)
            c.data["row"] = row["id"]
            self.calls_out.append(c)
        else:
            self.add("engineer", text_a, "eng_answer", spoken=False, est=self.crew_pc)
        return {"intent": it, "text": text_a, "say": say, "source": source, "call": c}

    def answer(self, it):
        """(text, say) for an intent from the same live numbers as the page."""
        fn = getattr(self, "_a_" + it, None)
        if fn is None:
            return "Say again?", "Say again."
        return fn()

    def _est(self, txt):
        return txt + " (est.)" if self.crew_pc else txt

    def _a_unknown(self):
        return "Say again?", "Say again."

    def _a_radio_check(self):
        return "Loud and clear.", "Loud and clear."

    def _a_quiet(self):
        return "Copy, urgent calls only.", "Copy. Urgent calls only."

    def _a_talk(self):
        return "Copy, I'll keep you posted.", "Copy. I'll keep you posted."

    def _a_repeat(self):
        return "Nothing to repeat yet.", "I don't have that yet."

    def _a_fuel(self):
        fam = self.sess["family"]
        if fam == "race" and (self.board.get("fuel") or {}).get("laps") is not None:
            f = self.board["fuel"]
            n = int(f["laps"])
            txt = self._est(f"{f['laps']:.1f} laps of fuel.")
            return txt, f"{nw(min(150, n))} laps of fuel."
        burn, fuel = _med(self.fuel_burns), _num(self.s.get("FuelLevel"))
        if fuel is None:
            return "I don't have a fuel reading yet.", "I don't have that yet."
        if not burn:
            return "Still learning your fuel use: two clean laps.", "I don't have that yet."
        n = int(fuel // burn)
        txt = self._est(f"{n} laps of fuel at {burn / (cc.GAL_L if self.units == 'mph' else 1):.2f} "
                        f"{'gal' if self.units == 'mph' else 'L'} a lap.")
        return txt, f"{nw(min(150, n))} laps of fuel."

    def _gap_line(self, side):
        word = "ahead" if side == "ahead" else "behind"
        if self.sess["family"] == "race" and self.board.get(side):
            r = self.board[side]
            g = r.get("gap_s")
            if g is not None:
                rate = r.get("rate")
                clos = "He's closing." if (rate is not None and rate >= cc.RATE_MIN and side == "behind") else \
                    "You're gaining on him." if (rate is not None and rate >= cc.RATE_MIN and side == "ahead") else \
                    "He's pulling away." if "pulling away" in (r.get("tags") or []) and side == "behind" else \
                    "He's not closing." if side == "behind" else ""
                txt = self._est(f"Gap {word} is {g:.1f} s." + (" " + clos if clos else ""))
                say = f"Gap {word} is {dec(g)} seconds." + (" " + clos if clos else "")
                return txt, say
        c = (self.traffic_now or {}).get(side)
        if c:
            g = abs(c["gap_s"])
            rate = c.get("rate")
            clos = ""
            if rate is not None:
                if side == "behind":
                    clos = "He's closing." if rate < -0.05 else "He's pulling away." if rate > 0.05 else "He's not closing."
                else:
                    clos = "You're gaining on him." if rate < -0.05 else ""
            txt = self._est(f"Gap {word} is {g:.1f} s." + (" " + clos if clos else ""))
            return txt, f"Gap {word} is {dec(g)} seconds." + (" " + clos if clos else "")
        return (f"Nobody {word} you on track.", f"Nobody {word} of you." if side == "ahead" else "Nobody behind you.")

    def _a_gap_behind(self):
        return self._gap_line("behind")

    def _a_gap_ahead(self):
        return self._gap_line("ahead")

    def _a_gaps(self):
        a, b = self._gap_line("ahead"), self._gap_line("behind")
        return a[0] + " " + b[0], a[1] + " " + b[1]

    def _a_position(self):
        s = self.s
        pos = _int(s.get("PlayerCarClassPosition")) or _int(s.get("PlayerCarPosition"))
        if self.sess["family"] == "qualify":
            r = self._rank()
            if r:
                pos = r["rank"]
        if not pos or pos <= 0:
            return "No position yet.", "I don't have that yet."
        of = ""
        return f"You are P{pos}.", f"You are position {nw(min(150, pos))}."

    def _a_pace(self):
        last, best = (self.laps[-1] if self.laps else None), self.best
        if not last and not best:
            return "No clean lap yet.", "I don't have that yet."
        tx, sy = [], []
        ref = last if last and last.s else best
        ls = lap_say(ref.s)
        tx.append(f"Last lap {lap_text(ref.s)}." if ref is last else f"Best lap {lap_text(ref.s)}.")
        if ls:
            sy.append(("Last lap " if ref is last else "Best lap ") + ls + ".")
        if best and ref is last and best.s != ref.s:
            d = ref.s - best.s
            tx.append(f"{d:.1f} s off your best.")
            if 0 < d < 10:
                sy.append(f"{dec(d)} seconds off your best.")
        elif best and ref is last:
            tx.append("That is your best.")
            sy.append("New best.")
        return " ".join(tx), " ".join(sy)

    def _a_losing_time(self):
        laps = [x for x in self._clean_laps() if x.sectors][-8:]
        if len(laps) < 2 or not self.best_sectors:
            return "Not enough clean laps yet to say.", "I don't have that yet."
        w = self.work_on()
        loss = []
        for i in range(len(self.best_sectors)):
            loss.append(_med([x.sectors[i] for x in laps]) - self.best_sectors[i])
        i = max(range(len(loss)), key=lambda k: loss[k])
        if loss[i] < 0.1:
            return "Nothing stands out, your sectors are close to your best.", "Nothing stands out."
        return (f"You're losing most in sector {i + 1}: about {loss[i]:.1f} s against your best (sectors from {self.bounds_how}).",
                f"You're losing most in sector {nw(i + 1)}.")

    def _a_box(self):
        b = (self.board or {}).get("box") or {}
        if self.sess["family"] == "race" and b:
            if b.get("need"):
                n = b.get("in_laps")
                if n == 0:
                    return self._est("Box this lap."), "Box this lap."
                if n is not None and 1 <= n <= 120:
                    return self._est(f"Box in {n} lap{'s' if n != 1 else ''}. {b.get('window_text', '')}".strip()), f"Box in {nw(n)} laps."
            if b.get("text"):
                return self._est(b["text"]), "No stop needed."
        return "No stop to plan in this session.", "No stop needed." if self.sess["family"] != "race" else "I don't have that yet."

    def _a_tyres(self):
        tv = self._tyres()
        if not tv or not tv["corners"]:
            return "No tyre readings for this car.", "I don't have that yet."
        hot = [c for c in tv["corners"] if c["temp"] is not None]
        if not hot:
            return "No tyre temperatures for this car.", "I don't have that yet."
        h = max(hot, key=lambda c: c["temp"])
        txt = ", ".join(f"{c['corner']} {c['temp']}{tv['unit']}" + (f" ({c['wear_pct']}% worn)" if c["wear_pct"] is not None else "")
                        for c in tv["corners"])
        return txt + ". " + tv["note"], (f"Hottest tyre is the {h['corner']} at {nw(h['temp'])} degrees." if 0 <= h["temp"] < 1000 else None)

    def _a_weather(self):
        w = self._build_weather()
        if not w:
            return "No weather reading yet.", "I don't have that yet."
        return w

    def _build_weather(self):
        s = self.s
        at, tt = _temp(s.get("AirTemp"), self.units), _temp(s.get("TrackTempCrew"), self.units)
        u = "°F" if self.units == "mph" else "°C"
        if tt is None and at is None:
            return None
        tx, sy = [], []
        # a temperature below zero (or past 150) has no clips: it is shown, and left out of what is said
        if tt is not None:
            tx.append(f"Track {tt:.0f}{u}")
            if 0 <= tt < 150:
                sy.append(f"Track temp {nw(tt)}.")
        if at is not None:
            tx.append(f"air {at:.0f}{u}")
            if 0 <= at < 150:
                sy.append(f"Air temp {nw(at)}.")
        wet = WET_WORDS.get(_int(s.get("TrackWetness")) or 0, "")
        if wet:
            tx.append(wet.lower())
        return ", ".join(tx) + ".", (" ".join(sy) or None)

    def _a_time_left(self):
        s = self.s
        left = _num(s.get("SessionTimeRemain"))
        laps = _int(s.get("SessionLapsRemainEx"))
        if laps is not None and 0 < laps < 30000:
            return f"{laps} lap{'s' if laps != 1 else ''} to go.", f"{nw(min(150, laps))} laps to go." if laps != 1 else "One lap to go."
        if left is not None and 0 <= left < UNLIMITED:
            m = int(left // 60)
            if m >= 1 and m <= 150:
                return f"{m} minute{'s' if m != 1 else ''} left.", f"{nw(m)} minutes left." if m != 1 else "One minute left."
            pct, ref = _num(s.get("LapDistPct")), self._ref_lap()
            ends = pct is not None and 0.0 <= pct <= 1.0 and left <= (1.0 - pct) * ref
            # under a minute: only "last lap" when the clock really does run out before the line (else text only)
            return f"{int(left)} seconds left.", ("Last lap of the session." if ends and self.sess["family"] != "race" else None)
        return "No time limit in this session.", "I don't have that yet."


def _truthy(v):
    return bool(v) and v not in (0, "0", False)
