"""The Race Room pit board (2.36 "Race Room"): the screen Swazi used on the 8 Nov 2026 weekend, worked out from iRacing's
own live numbers instead of Garage 61 live timing.

The weekend page read Garage 61's timing text every couple of minutes and estimated fuel from lap times. Here the same
board is fed by what this PC reads from iRacing (livedata), with the crew chief's real fuel and pit window and the Race
Room's stops, rivals and stints. Nothing here talks to the network or to iRacing; it is pure working-out, so every part
can be tested alone:

  session_kind / session_label  practice, qualifying, race, warm-up ... from iRacing's session type text
  LapBook          our own laps in ANY session (the Race Room keeps race laps itself): time, pit/slow flags, run number,
                   class position, fuel used
  pace             our reference pace (the last five clean laps), the cars around us and the class top 10
  stints           per stint (a driver's run between stops): laps, median, best, spread, trend a lap, time lost
  lap_chart        the bars of the lap chart, with the reference lines
  read_rivals      the weekend's "Read" tag on each rival (threat, catchable, on track together, ...)
  timesheet        practice and qualifying: the class order by best lap, around our car
  fuel_bar         the pit and fuel bar: laps done, fuel in the tank, the pit window and the flag, in laps
  strategy_crew    the four desks of the strategy crew (pit and fuel, rivals, pace and tyres, race engineer). On the
                   weekend these were Claude agents; here each desk is a set of written rules over the numbers above
  build            puts it together for the page

Rates follow the weekend page: a positive rate (seconds a lap) means the gap is shrinking.
"""
from __future__ import annotations

import math

from . import crewchief as cc

_num, _int, _at, _median = cc._num, cc._int, cc._at, cc._median

VERSION = 1
CLEAN_OVER_MEDIAN_S = 3.5      # a lap this much slower than the median is an in/out or traffic lap (weekend rule)
NEIGH_POSITIONS = 6            # "the cars around us": this many class places either side
NEIGH_OVER_REF_S = 6.0         # ... ignoring their laps this much slower than our pace (a stop, a spin)
TOP_N = 10
REF_LAPS = 5                   # our reference pace: the median of the last this many clean laps
TARGET_UNDER_REF_S = 0.3       # the weekend's target lap: a little under the reference pace
TOGETHER_S = 1.5               # a rival this close is "on track together"
THREAT_LAPS = 6.0              # a car behind that reaches us within this many laps is a threat (weekend rule)
FADE_PER_LAP = 0.05            # a stint getting slower by more than this a lap is fading (tyres, fuel load, traffic)
GAIN_PER_LAP = -0.02           # ... quicker by more than this a lap is improving
SPREAD_WIDE_S = 0.6            # laps further apart than this (standard deviation) are inconsistent
SAVE_SKIP_PCT = 8.0            # a fuel save up to this much is worth trying to skip a stop
MAX_LAPS = 400
MAX_CHART_LAPS = 120
RECENT_CALL_LAPS = 1          # the strip under the call lists crew chief calls from this lap and the last one only

KINDS = (("lone qualify", "qualify", "Qualifying (lone)"), ("open qualify", "qualify", "Qualifying (open)"),
         ("qualif", "qualify", "Qualifying"), ("warmup", "warmup", "Warm-up"), ("warm up", "warmup", "Warm-up"),
         ("heat", "race", "Heat race"), ("race", "race", "Race"), ("offline testing", "practice", "Test drive"),
         ("testing", "practice", "Test drive"), ("practice", "practice", "Practice"))


def session_kind(typ) -> str:
    """iRacing's SessionType ("Practice", "Open Qualify", "Lone Qualify", "Offline Testing", "Race", "Warmup") as one
    of practice / qualify / warmup / race, or "none" when there is no session."""
    t = str(typ or "").lower()
    for key, kind, _ in KINDS:
        if key in t:
            return kind
    return "practice" if t else "none"


def session_label(typ) -> str:
    """The words on the page for the session you are in."""
    t = str(typ or "").lower()
    for key, _, label in KINDS:
        if key in t:
            return label
    return str(typ).strip().title() if t else "No session"


def lap_text(s) -> str:
    return cc.lap_text(s) if s else "--"


def _sd(xs) -> float:
    if len(xs) < 2:
        return 0.0
    mu = sum(xs) / len(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def _slope(pts) -> float:
    """Least-squares slope of [(x, y)]: seconds a lap for (lap, time)."""
    if len(pts) < 3:
        return 0.0
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    num = sum((x - mx) * (y - my) for x, y in pts)
    den = sum((x - mx) ** 2 for x, _ in pts)
    return num / den if den else 0.0


def _signed(x, digits=1) -> str:
    return ("+" if x >= 0 else "-") + f"{abs(x):.{digits}f}"


# ---------------------------------------------------------------- our laps in any session
class LapBook:
    """Our car's laps, from the field arrays (CarIdxLapCompleted / CarIdxLastLapTime / CarIdxOnPitRoad of our CarIdx) and
    our own fuel. Works in practice and qualifying, where the Race Room records nothing. A run starts each time the car
    leaves the pits (or joins the session on track)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.key = None
        self.laps: list = []
        self.pending = None
        self.done = None
        self.run = 0
        self.in_pit = None
        self.flags = {"pit": False, "slow": False}
        self.fuel_at_line = None
        self.pos_hist: list = []          # [(lap, class position)]

    def feed(self, s: dict, field: dict | None, me, driver: str = ""):
        if not isinstance(s, dict) or not isinstance(field, dict) or me is None:
            return
        key = (_int(s.get("SessionNum")), _int(s.get("SessionUniqueID")))
        if key != self.key:
            self.reset()
            self.key = key
        lc = _num(_at(field.get("CarIdxLapCompleted"), me))
        pit = bool(_at(field.get("CarIdxOnPitRoad"), me))
        flags = int(_num(s.get("SessionFlags")) or 0)
        if self.in_pit is None or (self.in_pit and not pit):
            self.run += 1                  # first sight of the car, or it just left the pit lane
        self.in_pit = pit
        if pit:
            self.flags["pit"] = True
        if flags & cc.SLOW_FLAGS:
            self.flags["slow"] = True
        fuel = _num(s.get("FuelLevel"))
        if lc is None or lc < 0:
            return
        lc = int(lc)
        if self.done is None:
            self.done = lc
            self.fuel_at_line = fuel
            return
        if lc > self.done:
            used = (self.fuel_at_line - fuel) if (fuel is not None and self.fuel_at_line is not None) else None
            self.pending = {"lap": lc, "last0": self.pending_last(field, me), "flags": dict(self.flags), "run": self.run,
                            "driver": driver, "used": used if used is not None and 0 < used < 50 else None, "wait": 0}
            self.flags = {"pit": pit, "slow": bool(flags & cc.SLOW_FLAGS)}
            self.fuel_at_line = fuel
            cp = _int(_at(field.get("CarIdxClassPosition"), me))
            if cp and cp > 0:
                self.pos_hist.append((lc, cp))
                del self.pos_hist[:-MAX_LAPS]
            self.done = lc
        elif lc < self.done:              # a reset to the pits in a test session
            self.done = lc
        if self.pending is not None:
            self._settle(field, me)

    @staticmethod
    def pending_last(field, me):
        return _num(_at(field.get("CarIdxLastLapTime"), me))

    def _settle(self, field, me):
        p = self.pending
        ll = self.pending_last(field, me)
        p["wait"] += 1
        changed = ll is not None and ll > 0 and (p["last0"] is None or abs(ll - p["last0"]) > 1e-6)
        if not changed and p["wait"] < 16:                # iRacing writes the time a moment after the line
            return
        s = ll if ll is not None and ll > 0 else None      # waited long enough: the same time twice is possible
        self.laps.append({"lap": p["lap"], "s": s, "driver": p["driver"], "pit": p["flags"]["pit"], "slow": p["flags"]["slow"],
                          "stint": p["run"], "fuel_used": p["used"]})
        del self.laps[:-MAX_LAPS]
        self.pending = None

    def burn(self):
        """Median fuel used on clean laps (litres a lap), or None."""
        xs = [r["fuel_used"] for r in self.laps if r.get("fuel_used") and not r["pit"] and not r["slow"]]
        return _median(xs[-8:]) if len(xs) >= 2 else None


# ---------------------------------------------------------------- pace and stints
def timed(laps):
    return [r for r in laps if r.get("s")]


def clean(laps):
    """Laps that count for pace: timed, not a pit or caution lap, and not far slower than the median (traffic, a spin)."""
    t = [r for r in timed(laps) if not r.get("pit") and not r.get("slow")]
    if not t:
        return []
    med = _median([r["s"] for r in t])
    return [r for r in t if r["s"] < med + CLEAN_OVER_MEDIAN_S]


def class_cars(field, class_of, mine, skip=()):
    """{CarIdx: class position} of the cars in our class (all cars when classes are unknown)."""
    cp = (field or {}).get("CarIdxClassPosition") or []
    out = {}
    for i, p in enumerate(cp if isinstance(cp, (list, tuple)) else []):
        p = _int(p)
        if not p or p <= 0 or i in skip:
            continue
        if mine is not None and class_of and class_of.get(i) != mine:
            continue
        out[i] = p
    return out


def pace(laps, field, me, cars) -> dict:
    """{"ref", "median", "neigh", "top", "vs_neigh", "vs_top"}: our pace (the median of the last REF_LAPS clean laps), the
    median last lap of the cars within NEIGH_POSITIONS class places and of the class top 10."""
    c = clean(laps)
    t = [r["s"] for r in timed(laps)]
    ref = _median([r["s"] for r in c[-REF_LAPS:]]) if c else None
    med = _median(t) if t else None
    out = {"ref": ref, "median": med, "neigh": None, "top": None, "vs_neigh": None, "vs_top": None, "clean": len(c)}
    if not field or me is None or not cars:
        return out
    last = field.get("CarIdxLastLapTime") or []
    mypos = cars.get(me)
    lim = (ref or med or 1e9) + NEIGH_OVER_REF_S
    if mypos:
        ns = [_num(_at(last, i)) for i, p in cars.items() if i != me and abs(p - mypos) <= NEIGH_POSITIONS]
        ns = [x for x in ns if x and 0 < x < lim]
        out["neigh"] = _median(ns) if ns else None
    tops = [_num(_at(last, i)) for i, p in cars.items() if p <= TOP_N]
    tops = [x for x in tops if x and x > 0]
    out["top"] = _median(tops) if tops else None
    if ref and out["neigh"]:
        out["vs_neigh"] = round(ref - out["neigh"], 2)
    if ref and out["top"]:
        out["vs_top"] = round(ref - out["top"], 2)
    return out


def stints(laps) -> list:
    """One row per stint (a new one at each run number or driver change), with the weekend's columns."""
    groups: list = []
    for r in laps:
        g = groups[-1] if groups else None
        if g is None or r.get("stint") != g["stint"] or (r.get("driver") or "") != g["driver"]:
            g = {"stint": r.get("stint"), "driver": r.get("driver") or "", "laps": []}
            groups.append(g)
        g["laps"].append(r)
    out = []
    for n, g in enumerate(groups, 1):
        ls = g["laps"]
        c = clean(ls)
        ct = [r["s"] for r in c]
        best = min(ct) if ct else None
        out.append({"n": n, "driver": g["driver"], "laps": len(ls), "from": ls[0]["lap"], "to": ls[-1]["lap"],
                    "median": _median(ct) if ct else None, "best": best, "sd": round(_sd(ct), 3),
                    "trend": round(_slope([(r["lap"], r["s"]) for r in c]), 3),
                    "lost": round(sum(x - best for x in ct), 1) if best else 0.0, "clean": len(ct)})
    return out


def lap_chart(laps, p: dict) -> dict:
    t = timed(laps)[-MAX_CHART_LAPS:]
    med = _median([r["s"] for r in t]) if t else None
    drivers: list = []
    for r in t:
        d = r.get("driver") or ""
        if d not in drivers:
            drivers.append(d)
    return {"laps": [{"lap": r["lap"], "s": round(r["s"], 3), "driver": r.get("driver") or "",
                      "clean": not r.get("pit") and not r.get("slow") and med is not None and r["s"] < med + CLEAN_OVER_MEDIAN_S}
                     for r in t],
            "drivers": drivers, "median": med, "neigh": p.get("neigh"), "top": p.get("top"), "ref": p.get("ref")}


# ---------------------------------------------------------------- rivals
def read_tag(r: dict, laps_left=None) -> str:
    """The weekend's Read column for one rival row of the Race Room (role ahead/behind, gap_s, rate, laps_to_contact, tags)."""
    tags = r.get("tags") or []
    gap = _num(r.get("gap_s"))
    rate = _num(r.get("rate"))
    ltc = _num(r.get("laps_to_contact"))
    ahead = r.get("role") == "ahead"
    if "pitting" in tags or r.get("in_pit"):
        return "in the pits"
    if gap is not None and gap < TOGETHER_S:
        return "on track together"
    if not ahead and ltc is not None and ltc < THREAT_LAPS:
        return "threat"
    if ahead and ltc is not None and (laps_left is None or ltc < laps_left):
        return "catchable"
    if ahead:
        return "pulling away" if rate is not None and rate < -cc.AWAY_RATE else "holding"
    if rate is not None and rate > cc.RATE_MIN:
        return "closing slowly"
    return "safe"


def read_rivals(rivals, laps_left=None, cars_info=None) -> list:
    out = []
    for r in rivals or []:
        if r.get("role") not in ("ahead", "behind"):
            continue
        row = dict(r)
        row["read"] = read_tag(r, laps_left)
        if cars_info:
            row["car"] = (cars_info.get(r.get("idx")) or {}).get("car", "")
        out.append(row)
    return out


def timesheet(field, me, cars, drivers, around=4) -> list:
    """Practice and qualifying: the class by best lap, our car and `around` places either side of it, with the gap of each
    best lap to ours (negative: quicker than us)."""
    if not field or me is None:
        return []
    best = field.get("CarIdxBestLapTime") or []
    last = field.get("CarIdxLastLapTime") or []
    pit = field.get("CarIdxOnPitRoad") or []
    rows = []
    for i in cars:
        b = _num(_at(best, i))
        rows.append((i, b if b and b > 0 else None))
    rows.sort(key=lambda x: (x[1] is None, x[1] or 0.0, x[0]))
    pos = {i: n for n, (i, _) in enumerate(rows, 1)}
    if me not in pos:
        return []
    mine = dict(rows).get(me)
    lo, hi = max(1, pos[me] - around), pos[me] + around
    out = []
    for i, b in rows:
        p = pos[i]
        if not (lo <= p <= hi) and p != 1:
            continue
        d = drivers.get(i) or {}
        lt = _num(_at(last, i))
        gap = round(b - mine, 3) if (b and mine) else None
        out.append({"idx": i, "pos": p, "num": d.get("num", ""), "name": d.get("name", ""), "car": d.get("car", ""),
                    "best": b, "last": lt if lt and lt > 0 else None, "gap_s": gap, "me": i == me,
                    "read": "you" if i == me else ("in the pits" if _at(pit, i) else ("no time yet" if not b else
                                                                                     "quicker" if gap is not None and gap < 0 else "level" if gap == 0 else "slower"))})
    return out


# ---------------------------------------------------------------- pit and fuel
def fuel_bar(snap: dict | None, lap_now, laps_left_n, last_stop_lap, est: bool = False) -> dict:
    """The weekend's pit and fuel bar in laps, from the crew chief's numbers: laps done, the lap the tank runs dry, the
    window, the flag. Every number is None when it isn't known; ok is False until fuel and laps left both are."""
    snap = snap or {}
    fuel = snap.get("fuel") or {}
    box = snap.get("box") or {}
    fl = _num(fuel.get("laps"))
    out = {"ok": False, "now": lap_now, "last_stop": last_stop_lap, "finish": None, "run_out": None, "open": None,
           "close": None, "stops": int(box.get("stops") or 0), "need": bool(box.get("need")), "estimated": bool(est),
           "add_text": box.get("add_text") or "", "save_text": box.get("save_text") or "",
           "save_realistic": box.get("save_realistic"), "burn_text": fuel.get("burn_text") or "",
           "fuel_text": fuel.get("text") or "", "laps_of_fuel": fl, "text": "", "obs": ""}
    if lap_now is None:
        out["text"] = "Waiting for laps."
        return out
    if laps_left_n is not None:
        out["finish"] = int(lap_now + laps_left_n)
    if fl is not None:
        out["run_out"] = int(lap_now + math.floor(fl))
    if box.get("need"):
        out["close"] = _int(box.get("lap_latest"))
        out["open"] = _int(box.get("lap_earliest")) if box.get("lap_earliest") is not None else lap_now + 1
    out["ok"] = out["finish"] is not None and out["run_out"] is not None
    if not out["ok"]:
        out["text"] = ("Learning your fuel use: three clean laps." if fl is None else "Laps left aren't known yet.")
        return out
    pre = "Estimated: " if est else ""
    if out["need"] and out["close"] is not None:
        short = max(0, out["finish"] - out["run_out"])
        n = out["stops"]
        out["text"] = (f"{pre}{n} stop{'s' if n != 1 else ''} left. The tank runs dry around lap {out['run_out']} and the flag "
                       f"is around lap {out['finish']}, {short} lap{'s' if short != 1 else ''} short. "
                       + (f"Stop between lap {out['open']} and lap {out['close']}. " if out["open"] and out["open"] < out["close"]
                          else f"Stop by lap {out['close']}. ")
                       + (out["add_text"] + ". " if out["add_text"] else "")
                       + (out["save_text"] + "." if out["save_text"] else ""))
    else:
        m = out["run_out"] - out["finish"]
        out["text"] = (f"{pre}No more stops. The tank reaches about lap {out['run_out']} and the flag is around lap "
                       f"{out['finish']}, a margin of {m} lap{'s' if m != 1 else ''}.")
    return out


# ---------------------------------------------------------------- the strategy crew
def _desk(name, state, head, lines=()):
    return {"name": name, "state": state, "head": head, "lines": [x for x in lines if x]}


def strategy_crew(kind, *, bar=None, rivals=None, sheet=None, pace_=None, stint_rows=None, call=None, laps_left=None,
                  caution=False, wear=None, best=None, time_left=None, fuel_laps=None, lap_ref=None, estimated=False) -> list:
    """The four desks of the strategy crew, each a short headline and a few lines, with a state: "act" (do something
    now), "watch" (keep an eye on it) or "ok". Each desk is written rules over the board's numbers, the same questions the
    weekend's agents answered:

      Pit and fuel   race: stops left, the window, what to add, whether a fuel save drops a stop, a cheap stop under
                     caution, a car ahead pitting. Practice and qualifying: laps of fuel left in this run.
      Rivals         race: threats behind, catchable cars ahead, cars right with us. Practice and qualifying: where your
                     best lap sits and the gap to the next place up.
      Pace and tyres our pace against the cars around us and the top 10, the stint's trend (fade), consistency, tyre wear
                     when the pit stall has read it.
      Race engineer  the one thing to do next: the crew chief's call when there is one, else the most urgent desk.
    """
    bar, pace_ = bar or {}, pace_ or {}
    rivals, sheet, stint_rows = rivals or [], sheet or [], stint_rows or []
    desks = []
    race = kind == "race"
    est = "Estimated. " if estimated else ""

    # ---- pit and fuel
    if race:
        if not bar.get("ok"):
            desks.append(_desk("Pit and fuel", "ok", bar.get("text") or "Learning your fuel use.", ["A plan needs your fuel use and the laps left."]))
        elif bar.get("need") and bar.get("close") is not None:
            togo = bar["close"] - (bar.get("now") or 0)
            state = "act" if togo <= 3 or caution else "watch"
            lines = [f"{est}Window lap {bar['open']} to lap {bar['close']}." if bar.get("open") and bar["open"] < bar["close"]
                     else f"{est}Box by lap {bar['close']}.", bar.get("add_text") or ""]
            if bar.get("save_text"):
                lines.append(bar["save_text"] + ("." if bar.get("save_realistic") else ": more than you can really save."))
            if caution:
                lines.insert(0, "Caution: a stop now costs much less time. Box if you are in the window.")
            pitting_ahead = [r for r in rivals if r.get("role") == "ahead" and r.get("read") == "in the pits"]
            if pitting_ahead:
                r = pitting_ahead[0]
                lines.append(f"#{r.get('num')} ahead is in the pits: stopping on the same lap keeps the fight on track.")
            n = bar.get("stops") or 1
            desks.append(_desk("Pit and fuel", state, f"{n} stop{'s' if n != 1 else ''} left, box in {max(0, togo)} lap{'s' if togo != 1 else ''}", lines))
        else:
            m = (bar.get("run_out") or 0) - (bar.get("finish") or 0)
            desks.append(_desk("Pit and fuel", "watch" if m < 1 else "ok", "No more stops" if m >= 0 else "Fuel is short",
                               [f"{est}Fuel to lap {bar.get('run_out')}, flag around lap {bar.get('finish')}.", bar.get("save_text") or ""]))
    else:
        if fuel_laps is not None:
            laps_fit = int(fuel_laps)
            st = "act" if laps_fit <= 1 else "watch" if laps_fit <= 3 else "ok"
            lines = [f"Fuel for about {laps_fit} more lap{'s' if laps_fit != 1 else ''}."]
            if kind == "qualify" and laps_fit >= 2:
                lines.append("Enough for an out lap and a timed lap." if laps_fit < 4 else "Enough for a full run.")
            desks.append(_desk("Pit and fuel", st, "Box this lap" if st == "act" else f"{laps_fit} laps of fuel", lines))
        else:
            desks.append(_desk("Pit and fuel", "ok", "Learning your fuel use", ["Two clean laps and the fuel use shows here."]))

    # ---- rivals
    if race:
        threats = [r for r in rivals if r.get("read") == "threat"]
        together = [r for r in rivals if r.get("read") == "on track together"]
        catch = sorted([r for r in rivals if r.get("read") == "catchable"], key=lambda r: _num(r.get("laps_to_contact")) or 99)
        lines = []
        for r in threats[:2]:
            lines.append(f"#{r.get('num')} {r.get('name', '')} behind by {r.get('gap_s', 0):.1f} s, gaining {r.get('rate', 0):.1f} s a lap: "
                         f"with you in about {math.ceil(_num(r.get('laps_to_contact')) or 0)} laps.")
        for r in together[:2]:
            lines.append(f"#{r.get('num')} {r.get('name', '')} is right with you ({r.get('role')} by {r.get('gap_s', 0):.1f} s).")
        for r in catch[:1]:
            lines.append(f"#{r.get('num')} {r.get('name', '')} is {r.get('gap_s', 0):.1f} s ahead and you are {r.get('rate', 0):.1f} s a lap quicker: "
                         f"catch in about {math.ceil(_num(r.get('laps_to_contact')) or 0)} laps.")
        if not rivals:
            head, state = "No class rivals in sight yet", "ok"
        elif threats or together:
            head, state = f"{len(threats) + len(together)} car{'s' if len(threats) + len(together) != 1 else ''} in striking distance", "act" if threats else "watch"
        elif catch:
            head, state = f"{len(catch)} car{'s' if len(catch) != 1 else ''} catchable ahead", "watch"
        else:
            head, state = "Nobody closing, nobody catchable", "ok"
            lines.append("Gaps are steady: drive your own race.")
        desks.append(_desk("Rivals", state, head, lines))
    else:
        me = next((r for r in sheet if r.get("me")), None)
        if not me or not me.get("best"):
            desks.append(_desk("Rivals", "ok", "No lap time yet", ["Your place on the timesheet shows after a timed lap."]))
        else:
            up = next((r for r in sheet if r.get("pos") == me["pos"] - 1), None)
            p1 = next((r for r in sheet if r.get("pos") == 1), None)
            lines = []
            if up and up.get("gap_s") is not None:
                lines.append(f"P{up['pos']} (#{up.get('num')}) is " + (f"{abs(up['gap_s']):.3f} s quicker." if up["gap_s"] else "level on time."))
            if p1 and not p1.get("me") and p1.get("gap_s") and p1 is not up:
                lines.append(f"Class best is {abs(p1['gap_s']):.3f} s quicker than yours.")
            close = up and up.get("gap_s") is not None and abs(up["gap_s"]) < 0.2
            desks.append(_desk("Rivals", "watch" if close and kind == "qualify" else "ok",
                               f"P{me['pos']} on best lap", lines))

    # ---- pace and tyres
    ref = pace_.get("ref")
    lines = []
    state = "ok"
    if ref:
        if pace_.get("vs_neigh") is not None:
            v = pace_["vs_neigh"]
            lines.append(f"{abs(v):.1f} s a lap {'slower than' if v > 0 else 'quicker than'} the cars around you.")
            if v > 0.5:
                state = "watch"
        if pace_.get("vs_top") is not None:
            lines.append(f"{abs(pace_['vs_top']):.1f} s a lap {'off' if pace_['vs_top'] > 0 else 'ahead of'} the top 10.")
    cur = stint_rows[-1] if stint_rows else None
    if cur and cur.get("clean", 0) >= 4:
        tr = cur["trend"]
        if tr > FADE_PER_LAP:
            lines.append(f"This stint is fading by {tr:.2f} s a lap: tyres or fuel load.")
            state = "watch"
        elif tr < GAIN_PER_LAP:
            lines.append(f"This stint is getting quicker by {abs(tr):.2f} s a lap.")
        if cur["sd"] > SPREAD_WIDE_S:
            lines.append(f"Laps are spread by {cur['sd']:.2f} s: settle into a rhythm.")
            state = "watch"
    if isinstance(wear, dict) and wear:
        low = min(wear.items(), key=lambda kv: kv[1])
        lines.append(f"Tyre wear at the last stop: the {low[0]} at {low[1]:.0f}%.")
        if low[1] < 50:
            state = "watch"
    desks.append(_desk("Pace and tyres", state, f"Pace {lap_text(ref)}" if ref else "Learning your pace", lines or ["Clean laps show your pace here."]))

    # ---- race engineer: the one thing to do next
    if call:
        desks.append(_desk("Race engineer", "act" if (call.get("rank") or 9) <= 3 else "watch", call.get("text") or "", ["From the crew chief."]))
    else:
        urgent = next((d for d in desks if d["state"] == "act"), None) or next((d for d in desks if d["state"] == "watch"), None)
        if urgent:
            desks.append(_desk("Race engineer", urgent["state"], urgent["head"], [f"From the {urgent['name'].lower()} desk."] + urgent["lines"][:1]))
        elif race:
            desks.append(_desk("Race engineer", "ok", f"Target {lap_text(ref - TARGET_UNDER_REF_S)}" if ref else "All good",
                               ["Nothing needs you right now: hit your marks."]))
        elif kind == "qualify":
            desks.append(_desk("Race engineer", "ok", "Find the lap", [f"Best so far {lap_text(best)}." if best else "",
                                                                      f"{int(time_left // 60)} min left in the session." if time_left else ""]))
        else:
            tip = "Laps are steady: try one change and compare." if cur and cur.get("clean", 0) >= 4 and cur["sd"] <= 0.3 else \
                "Build up to a clean, steady run."
            desks.append(_desk("Race engineer", "ok", tip, [f"Best so far {lap_text(best)}." if best else ""]))
    return desks


# ---------------------------------------------------------------- the whole board
def _tile(key, label, big, sub="", state=""):
    return {"key": key, "label": label, "big": big if big not in (None, "") else "--", "sub": sub or "", "state": state}


def _rival_tile(key, label, r, idle):
    if not r:
        return _tile(key, label, "--", idle)
    rate = _num(r.get("rate"))
    bits = [("#" + str(r.get("num")) + " " if r.get("num") else "") + (r.get("name") or "")]
    if rate is not None and abs(rate) >= 0.05:
        bits.append(("closing " if rate > 0 else "opening ") + f"{abs(rate):.1f} s a lap")
    if r.get("laps_to_contact") is not None and rate and rate > 0:
        bits.append(f"contact in about {math.ceil(r['laps_to_contact'])} laps")
    if r.get("read") == "in the pits":
        bits.append("in the pits")
    state = "hot" if r.get("read") == "threat" else "good" if r.get("read") == "catchable" else ""
    gap = _num(r.get("gap_s"))
    return _tile(key, label, ("+" if r.get("role") == "behind" else "-") + f"{gap:.1f} s" if gap is not None else "--",
                 " · ".join(b for b in bits if b), state)


def waiting(text="Waiting for iRacing.", detail="") -> dict:
    """The board with nothing to show: every tile "--", so the page looks the same before a session."""
    tiles = [_tile("pos", "Position", "--"), _tile("left", "Laps left", "--"), _tile("box", "Box by", "--"),
             _tile("ahead", "Car ahead", "--"), _tile("behind", "Car behind", "--"), _tile("last", "Last lap", "--"),
             _tile("target", "Target", "--"), _tile("time", "Time left", "--")]
    return {"version": VERSION, "state": "waiting", "connected": False, "text": text, "detail": detail,
            "session": {"kind": "none", "label": "No session", "key": None, "track": "", "car": ""},
            "call": None, "calls": [], "tiles": tiles, "rivals": [], "rivals_kind": "race", "pace": {}, "stints": [],
            "lap_chart": {"laps": [], "drivers": []}, "fuel_bar": {"ok": False, "text": "Waiting for iRacing."},
            "positions": {"laps": [], "series": []}, "crew": [], "team": False}


def build(*, s: dict | None, field: dict | None, room=None, chief=None, book: LapBook | None = None, typ: str = "",
          session_key=None, track="", car="", units="mph") -> dict:
    """The board for this moment. room: raceroom.RaceRoom (rivals, stops, race laps, positions), chief: crewchief.CrewChief
    (fuel, the pit window, laps left, the calls), book: our laps in any session."""
    kind = session_kind(typ)
    st = waiting()
    st.update(state="live", connected=True, text="", session={"kind": kind, "label": session_label(typ), "key": session_key,
                                                              "track": track, "car": car})
    s = s if isinstance(s, dict) else {}
    snap = chief.snapshot() if chief is not None else {}
    me = room.team_idx if room is not None and room.team_idx is not None else (chief.me if chief is not None else None)
    class_of = chief.class_of if chief is not None else {}
    skip = chief.pace_idx if chief is not None else set()
    cars = class_cars(field, class_of, class_of.get(me) if class_of else None, skip)
    drivers = chief.drivers if chief is not None else {}
    race = kind == "race"
    team = bool(room is not None and room.info.get("team_racing"))
    st["team"] = team
    laps = [dict(r) for r in room.laps] if (race and room is not None and room.laps) else list(book.laps if book else [])
    for r in laps:
        r.setdefault("driver", "")
    p = pace(laps, field, me, cars)
    st["pace"] = p
    st["stints"] = stints(laps)
    st["lap_chart"] = lap_chart(laps, p)
    time_left = _num(s.get("SessionTimeRemain"))
    time_left = time_left if time_left is not None and 0 <= time_left < 600000 else None
    caution = bool(int(_num(s.get("SessionFlags")) or 0) & cc.FULL_COURSE)
    wear = {}
    for ch, name in (("LFwearM", "left front"), ("RFwearM", "right front"), ("LRwearM", "left rear"), ("RRwearM", "right rear")):
        w = _num(s.get(ch))
        if w is not None and 0 < w <= 1.0:
            wear[name] = w * 100.0
    est = bool(snap.get("fuel_estimated"))
    call = snap.get("call") if race else None
    st["call"] = call
    lap_now_ = chief.lap if chief is not None else None
    st["calls"] = [c for c in (snap.get("calls") or []) if (not call or c.get("id") != call.get("id"))
                   and (lap_now_ is None or c.get("lap") is None or c["lap"] >= lap_now_ - RECENT_CALL_LAPS)][:3] if race else []
    best = min((r["s"] for r in timed(laps)), default=None)
    last = laps[-1]["s"] if laps and laps[-1].get("s") else None
    target = (p["ref"] - TARGET_UNDER_REF_S) if p.get("ref") else None
    if race:
        ll = snap.get("laps_left") or {}
        n_left = ll.get("n")
        lap_now = chief.lap if chief is not None else None
        team_stops = (room.stops.stops.get(me) or []) if room is not None and me is not None else []
        last_stop = team_stops[-1]["lap"] if team_stops else 0
        bar = fuel_bar(snap, lap_now, n_left, last_stop, est)
        bar["obs"] = ("Stops seen: " + (", ".join(f"lap {x['lap']}" for x in team_stops[-6:]) or "none")
                      + (f". Each stop costs about {snap['pit_loss']['s']:.0f} s, already counted in laps left." if (snap.get("pit_loss") or {}).get("s") else "."))
        st["fuel_bar"] = bar
        rivals = read_rivals(room.rivals if room is not None else [], n_left, drivers)
        if me is not None and cars.get(me):
            d = drivers.get(me) or {}
            us = {"idx": me, "pos": cars[me], "num": d.get("num", ""), "name": d.get("name", ""), "car": d.get("car", ""),
                  "me": True, "role": "us", "read": "you", "gap_s": None, "rate": None, "laps_to_contact": None}
            st["rivals"] = sorted(rivals + [us], key=lambda r: r.get("pos") or 99)
        else:
            st["rivals"] = rivals
        st["rivals_kind"] = "race"
        ahead = next((r for r in rivals if r["role"] == "ahead" and r.get("pos") == (cars.get(me) or 0) - 1), None)
        behind = next((r for r in rivals if r["role"] == "behind" and r.get("pos") == (cars.get(me) or 0) + 1), None)
        P = snap.get("pos") or {}
        ph = room.pos_hist.get(me) if room is not None and me is not None else None
        change = (ph[0][1] - ph[-1][1]) if ph and len(ph) >= 2 else None
        box = snap.get("box") or {}
        if box.get("need"):
            box_big = "This lap" if box.get("in_laps") == 0 else f"Lap {box.get('lap_latest')}"
            box_sub = " · ".join(x for x in (f"in {box.get('in_laps')} laps" if box.get("in_laps") else "",
                                             box.get("window_text") or "", box.get("add_text") or "") if x)
            box_state = "hot" if (box.get("in_laps") or 0) <= 1 else ""
        else:
            box_big = "None" if box.get("text") else "--"
            box_sub = box.get("window_text") or ("Learning your fuel use." if not bar.get("ok") else "")
            box_state = "good" if box.get("text") else ""
        if est:
            box_sub = "Estimated fuel. " + box_sub
        st["tiles"] = [
            _tile("pos", "Position", f"P{P['class']}" if P.get("class") else "--",
                  " · ".join(x for x in ((f"of {P['of']} in class" if P.get("of") else ""), (f"P{P['overall']} overall" if P.get("overall") else ""),
                                         (("up " if change > 0 else "down ") + f"{abs(change)} since lap {ph[0][0]}" if change else "")) if x)),
            _tile("left", "Laps left", str(n_left) if n_left is not None else "--",
                  (f"flag about lap {bar['finish']}" if bar.get("finish") else "") + (" · timed race" if ll.get("kind") == "time" else "")),
            _tile("box", "Box by" + (f" · {box.get('stops')} stops" if (box.get("stops") or 0) > 1 else ""), box_big, box_sub, box_state),
            _rival_tile("ahead", "Car ahead", ahead, "No car ahead in your class"),
            _rival_tile("behind", "Car behind", behind, "No car behind in your class"),
            _tile("last", "Last lap", lap_text(last), (_signed(last - target, 2) + " s against the target" if (last and target) else "")
                  + (f" · best {lap_text(best)}" if best else ""), "good" if last and target and last <= target else ""),
            _tile("target", "Target", lap_text(target), f"cars around you {lap_text(p['neigh'])}" if p.get("neigh") else ""),
            _tile("time", "Time left", cc_clock(time_left), ("Caution. " if caution else "") + (cc.flags_text(s.get("SessionFlags")) if not caution else "")),
        ]
        st["positions"] = room._positions() if room is not None else {"laps": [], "series": []}
        crew_kw = dict(bar=bar, rivals=rivals, pace_=p, stint_rows=st["stints"], call=call, laps_left=n_left, caution=caution,
                       wear=wear, best=best, time_left=time_left, lap_ref=p.get("ref"), estimated=est)
    else:
        sheet = timesheet(field, me, cars, drivers)
        st["rivals"] = sheet
        st["rivals_kind"] = "timesheet"
        mine = next((r for r in sheet if r["me"]), None)
        p1 = next((r for r in sheet if r["pos"] == 1), None)
        if mine and mine.get("best"):
            best = mine["best"]                 # iRacing's own best (it only counts valid laps)
        fuel = _num(s.get("FuelLevel"))
        burn = book.burn() if book else None
        fuel_laps = (fuel / burn) if (fuel is not None and burn) else None
        st["fuel_bar"] = {"ok": False, "text": (f"Fuel {cc.fuel_text(fuel, units)}: about {fuel_laps:.1f} laps at {cc.fuel_text(burn, units, 2)} a lap."
                                                if fuel_laps is not None else "Fuel use shows after two clean laps.") if fuel is not None else "No fuel reading.",
                          "practice": True, "fuel_text": cc.fuel_text(fuel, units) if fuel is not None else "", "laps_of_fuel": fuel_laps}
        run_laps = [r for r in laps if r.get("stint") == (laps[-1]["stint"] if laps else None)]
        st["tiles"] = [
            _tile("session", "Session", session_label(typ), track),
            _tile("pos", "Position", f"P{mine['pos']}" if mine and mine.get("best") else "--", f"of {len(cars)} in class on best lap" if cars else ""),
            _tile("best", "Best lap", lap_text(best), (f"{_signed(mine['best'] - p1['best'], 3)} s to class best" if mine and p1 and p1 is not mine and mine.get("best") and p1.get("best") else
                   "class best" if mine and p1 is mine and mine.get("best") else "")),
            _tile("last", "Last lap", lap_text(last), (_signed(last - best, 3) + " s to your best") if last and best else "",
                  "good" if last and best and last <= best + 1e-6 else ""),
            _tile("laps", "Laps", str(len(laps)), f"{len(run_laps)} in this run" if laps else ""),
            _tile("fuel", "Fuel", f"{fuel_laps:.1f} laps" if fuel_laps is not None else (cc.fuel_text(fuel, units) if fuel is not None else "--"),
                  cc.fuel_text(fuel, units) if fuel_laps is not None else ""),
            _tile("ref", "Pace", lap_text(p.get("ref")), f"cars around you {lap_text(p['neigh'])}" if p.get("neigh") else ""),
            _tile("time", "Time left", cc_clock(time_left), "unlimited" if time_left is None else ""),
        ]
        ph = book.pos_hist if book else []
        st["positions"] = {"laps": [a for a, _ in ph][-MAX_CHART_LAPS:], "series": [{"idx": me, "num": "", "me": True, "pos": [b for _, b in ph][-MAX_CHART_LAPS:]}]} if ph else {"laps": [], "series": []}
        crew_kw = dict(sheet=sheet, pace_=p, stint_rows=st["stints"], wear=wear, best=best, time_left=time_left,
                       fuel_laps=fuel_laps, lap_ref=p.get("ref"))
    st["crew"] = strategy_crew(kind, **crew_kw)
    if not call:
        eng = st["crew"][-1]
        st["call"] = {"text": eng["head"], "rank": 1 if eng["state"] == "act" else 9, "from": "engineer"}
    return st


def cc_clock(s) -> str:
    if s is None:
        return "--"
    s = max(0, int(round(s)))
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    return (f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}")
