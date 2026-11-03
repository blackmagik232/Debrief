"""Practice loop (2.14): "One thing this session".

From the corner coach's report (cornercoach.analyze_files / analyze_laps) it picks ONE thing to work on in the next
session, turns it into a number he can aim for, scores the session afterwards, and can give a short spoken progress
cue after the focus corner while he drives.

    make_plan(report, history=None, car=None, track=None) -> plan
    score_plan(plan, laps_phases, report=None)            -> score (how the session went, and what next)
    session_phases(laps, plan)                            -> laps_phases from cornercoach.load_laps laps
    score_files(plan, paths, report=None)                 -> score_plan over the clean laps of .ibt files
    progress_cue(plan, lap_phases)                        -> "Good, 84" / "Too much brake" / None
    LiveFocus(plan)                                       -> for livecoach.Coach.set_focus (live progress cues)
    load_plans / current_plan / save_plan / save_score    -> plans kept per car + track (JSON, atomic write)

A plan (JSON-ready):
    {"version", "kind": "focus" | "consistency" | "laps_first" | "none", "plan_id", "made" (ISO time),
     "car", "track", "corner": "T4", "id": "C4", "metric", "unit", "target", "tolerance",
     "now": {"typical", "best"}, "text", "voice", "how", "why", "check", "success_rule": {...},
     "zone": {"id", "apex_m", "start_m", "end_m"}, "length_m", "lost_s", "confidence", "evidence",
     "repeat": bool, "reason": why this corner was picked}
    metric: brake_point (m before the apex) | peak_brake (%) | trail (brake % at turn-in) | coast (s) |
            throttle_point (m after the apex; negative = before it) | min_speed (km/h) | corner_time (s)
    success_rule: {"key": phase key (cornercoach.phases), "op": "<=" | ">=" | "between", "value" | "lo"/"hi"}
        tested against that lap's phase number for the plan's corner.

Honesty rules (2.14, after the 2.13 corner coach review):
  - a "focus" plan only from a corner coach card, and only on a number that card's rule was tested on (the
    permutation test) and that itself passed the noise gate (z >= cornercoach.Z_LIKELY); never more confident
    than its card (a plan from a card's second rule is at most "likely").
  - a corner with no card ("no clear pattern yet") only gives a "consistency" plan, worded as an experiment
    ("Try this and see"), confidence "unclear", experiment True. A kept focus whose card is gone today is
    marked the same way.
  - a time difference between laps that hit and laps that missed is only talked about with >= MIN_EACH of each
    AND a shuffle test (hit / miss labels shuffled SHUFFLE_N times, fixed seed): p < SHUFFLE_P. It describes
    those laps; it never promises lap time. Otherwise "not enough laps to tell yet" / "no clear difference".
Nothing here raises for bad input; corrupt plan files are set aside (renamed .bad) and never stop anything.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np

from . import cornercoach as cc
from . import fsutil

VERSION = 2                  # 2.14: plans follow the 2.13 review corner coach (tested metrics, experiments)
KEEP_BELOW = 0.70            # hit on fewer than 70% of measured laps: keep the same focus next session
MIN_EACH = 3                 # hit laps and missed laps needed before a time difference is talked about
WANT_LAPS = 10               # "drive 10 clean laps first"
MAX_HISTORY = 50             # plans kept per car + track
SHUFFLE_N = 400              # score_plan's time verdict: hit / miss labels shuffled this many times ...
SHUFFLE_SEED = 214           # ... with a fixed seed (the same laps always give the same verdict)
SHUFFLE_P = 0.05             # ... and a difference this rare among the shuffles counts as real
MIN_GAIN_S = 0.03            # ... and at least this big (median through the corner) to be talked about

# tolerances (how close counts as hitting the target)
TOL_BRAKE_M = 5.0            # brake point: +/- 5 m
TOL_PEAK = 3.0               # peak brake: target + 3 points
TOL_TRAIL = 5.0              # trail: target - 5 points
TOL_COAST = 0.1              # coasting: target + 0.1 s
TOL_PICKUP_M = 5.0           # throttle pick-up: target + 5 m
TOL_MIN_KMH = 2.0            # minimum speed: target - 2 km/h

# how big the best-vs-typical gap must be before it is a target worth aiming at
NEED_BRAKE_M = 5.0
NEED_PEAK = 5.0
NEED_TRAIL = 5.0
NEED_COAST = 0.15
NEED_PICKUP_M = 10.0
NEED_MIN_KMH = 2.0

# which number each corner-coach rule is measured by (first that gives a usable target wins). Only numbers the corner
# coach tests that rule on (cornercoach.DIRECTION): a plan never aims at a number the card's test didn't look at.
RULE_METRICS = {"brake_later": ("brake_point",), "brake_earlier": ("brake_point",), "lighter": ("peak_brake",),
                "trail": ("trail", "coast"), "coast": ("coast",), "throttle": ("throttle_point",),
                "carry": ("min_speed",), "lock": (), "spin": (), "release": ()}
METRIC_KEY = {"brake_point": "brake_m", "peak_brake": "peak_pct", "trail": "trail_pct", "coast": "coast_s",
              "throttle_point": "pickup_m", "min_speed": "min_kmh", "corner_time": "t"}
CONF_ORDER = {"clear": 0, "likely": 0, "thin": 1}
CONF_RANK = {"unclear": 0, "thin": 1, "likely": 2, "clear": 3}
EXPERIMENT = "Try this and see"


# ---------------------------------------------------------------- helpers
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _five(x) -> int:
    return int(math.floor(float(x) / 5.0 + 0.5) * 5)


def _ri(x) -> int:
    """Rounded half up (136.5 -> 137), not Python's round-half-to-even."""
    return int(math.floor(float(x) + 0.5))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _nums(card, key):
    n = ((card or {}).get("numbers") or {}).get(key) or {}
    b, t = _num(n.get("best")), _num(n.get("typical"))
    return (b, t) if b is not None and t is not None else (None, None)


def _tested(card, key) -> bool:
    """The card's number passed the corner coach's noise gate (z >= Z_LIKELY). A card without z (old caches) is
    taken as it is."""
    n = ((card or {}).get("numbers") or {}).get(key) or {}
    z = _num(n.get("z"))
    return z is None or z >= cc.Z_LIKELY


def _cap(conf, most):
    """The lower of two confidences."""
    c = conf if conf in CONF_RANK else "thin"
    return c if CONF_RANK[c] <= CONF_RANK.get(most, 0) else most


def _where(m: int) -> str:
    """metres from the apex in words: -30 -> '30 m before the apex'."""
    if abs(m) <= 5:
        return "at the apex"
    return f"{abs(m)} m {'before' if m < 0 else 'after'} the apex"


def _check(key: str, value, rule: dict):
    """True / False: does this phase number meet the rule. None when there is no number."""
    v = _num(value)
    if v is None or not isinstance(rule, dict):
        return None
    op = rule.get("op")
    if op == "<=":
        return v <= float(rule["value"]) + 1e-9
    if op == ">=":
        return v >= float(rule["value"]) - 1e-9
    if op == "between":
        return float(rule["lo"]) - 1e-9 <= v <= float(rule["hi"]) + 1e-9
    return None


def measure(plan: dict, lap_phases: dict | None):
    """(value in the plan's own units, hit True/False) for one lap's phase numbers; (None, None) when not measured.
    brake_point is given as metres BEFORE the apex (a positive number), as the plan talks about it."""
    if not isinstance(plan, dict) or not isinstance(lap_phases, dict):
        return None, None
    rule = plan.get("success_rule") or {}
    key = rule.get("key")
    if not key:
        return None, None
    raw = _num(lap_phases.get(key))
    if raw is None:
        return None, None
    hit = _check(key, raw, rule)
    val = -raw if plan.get("metric") == "brake_point" else raw
    return val, hit


# ---------------------------------------------------------------- targets from one card
def _target(metric: str, card: dict, T: str):
    """The plan fields for one metric from a card's numbers, or None when they don't give a clear target (or the
    number didn't pass the corner coach's noise gate)."""
    if metric in METRIC_KEY and not _tested(card, METRIC_KEY[metric]):
        return None
    if metric == "brake_point":
        b, t = _nums(card, "brake_m")
        if b is None:
            return None
        tgt, typ = _five(-b), _five(-t)
        n = abs(typ - tgt)
        if n < NEED_BRAKE_M or tgt <= 0:
            return None
        later = tgt < typ
        return {"unit": "m before the apex", "target": tgt, "tolerance": TOL_BRAKE_M,
                "now": {"typical": typ, "best": _five(-b)},
                "text": f"{T}: start braking about {tgt} m before the apex, about {n} m "
                        f"{'later' if later else 'earlier'} than you usually do",
                "voice": f"{T}: brake {'later' if later else 'earlier'}",
                "check": f"We'll count laps where you start braking for {T} between {tgt - 5:g} and {tgt + 5:g} m "
                         f"before the apex",
                "success_rule": {"key": "brake_m", "op": "between", "lo": -(tgt + TOL_BRAKE_M),
                                 "hi": -(tgt - TOL_BRAKE_M)}}
    if metric == "peak_brake":
        b, t = _nums(card, "peak_pct")
        if b is None:
            return None
        tgt, typ = _five(b), _five(t)
        if t - b < NEED_PEAK or tgt >= typ:
            return None
        allway = t >= cc.FULL_BRAKE_PCT
        return {"unit": "%", "target": tgt, "tolerance": TOL_PEAK, "now": {"typical": typ, "best": _ri(b)},
                "text": f"{T}: press the brake to about {tgt}%" + (", not all the way" if allway else
                                                                     f", not {typ}%"),
                "voice": f"{T}: lighter brake",
                "check": f"We'll count laps where your peak brake in {T} stays at or below {tgt + TOL_PEAK:g}%",
                "success_rule": {"key": "peak_pct", "op": "<=", "value": tgt + TOL_PEAK}}
    if metric == "trail":
        b, t = _nums(card, "trail_pct")
        if b is None:
            return None
        tgt = _five(b)
        if b - t < NEED_TRAIL or tgt < 10:
            return None
        return {"unit": "% brake at turn-in", "target": tgt, "tolerance": TOL_TRAIL,
                "now": {"typical": _five(t), "best": _ri(b)},
                "text": f"{T}: keep about {tgt}% brake on as you turn in, then let it go by the apex",
                "voice": f"{T}: hold it in",
                "check": f"We'll count laps where you still have at least {tgt - TOL_TRAIL:g}% brake on as you turn "
                         f"in to {T}",
                "success_rule": {"key": "trail_pct", "op": ">=", "value": tgt - TOL_TRAIL}}
    if metric == "coast":
        b, t = _nums(card, "coast_s")
        if b is None:
            return None
        tgt = round(b, 1)
        if t - b < NEED_COAST:
            return None
        # coasting is measured on the 5 m grid: one cell at the slowest speed is the finest it can tell apart
        vmin = _nums(card, "min_kmh")[0]
        tol = max(TOL_COAST, round(cc.STEP / (vmin / 3.6), 1)) if vmin and vmin > 10 else TOL_COAST
        return {"unit": "s", "target": tgt, "tolerance": tol, "now": {"typical": round(t, 1), "best": tgt},
                "text": f"{T}: go from the brake to the throttle with no gap, at most {tgt:.1f} s with both feet off "
                        f"(you usually coast {t:.1f} s)",
                "voice": f"{T}: no coasting",
                "check": f"We'll count laps where you spend {tgt + tol:.1f} s or less with both feet off in {T}",
                "success_rule": {"key": "coast_s", "op": "<=", "value": round(tgt + tol, 2)}}
    if metric == "throttle_point":
        b, t = _nums(card, "pickup_m")
        if b is None:
            return None
        tgt, typ = _five(b), _five(t)
        n = typ - tgt
        if n < NEED_PICKUP_M:
            return None
        return {"unit": "m after the apex", "target": tgt, "tolerance": TOL_PICKUP_M,
                "now": {"typical": typ, "best": tgt},
                "text": f"{T}: start on the throttle {_where(tgt)}, about {n} m earlier than you usually do",
                "voice": f"{T}: throttle earlier",
                "check": f"We'll count laps where you pick up the throttle in {T} no later than "
                         f"{_where(int(tgt + TOL_PICKUP_M))}",
                "success_rule": {"key": "pickup_m", "op": "<=", "value": tgt + TOL_PICKUP_M}}
    if metric == "min_speed":
        b, t = _nums(card, "min_kmh")
        if b is None:
            return None
        tgt = _ri(b)
        if b - t < NEED_MIN_KMH:
            return None
        return {"unit": "km/h", "target": tgt, "tolerance": TOL_MIN_KMH,
                "now": {"typical": _ri(t), "best": tgt},
                "text": f"{T}: carry about {tgt} km/h at the slowest point (you usually have {_ri(t)} km/h)",
                "voice": f"{T}: carry more speed",
                "check": f"We'll count laps where your slowest speed in {T} is {tgt - TOL_MIN_KMH:g} km/h or more",
                "success_rule": {"key": "min_kmh", "op": ">=", "value": tgt - TOL_MIN_KMH}}
    return None


def _zone(report: dict, cid: str) -> dict | None:
    for c in (report or {}).get("corners") or []:
        if c.get("id") == cid and all(_num(c.get(k)) is not None for k in ("apex_m", "start_m", "end_m")):
            return {"id": cid, "apex_m": float(c["apex_m"]), "start_m": float(c["start_m"]),
                    "end_m": float(c["end_m"])}
    return None


def _base(report, kind, car=None, track=None) -> dict:
    info = (report or {}).get("info") or {}
    return {"version": VERSION, "kind": kind, "plan_id": uuid.uuid4().hex[:12], "made": _now(),
            "car": car or info.get("car"), "track": track or info.get("track"),
            "length_m": _num(info.get("length_m")), "from_laps": int((report or {}).get("laps") or 0),
            "corner": None, "id": None, "metric": None, "unit": None, "target": None, "tolerance": None,
            "now": None, "text": "", "voice": None, "how": None, "why": None, "check": None, "success_rule": None,
            "zone": None, "lost_s": None, "confidence": None, "evidence": None, "repeat": False, "reason": ""}


def _from_card(report, card, metric, car=None, track=None, second=False):
    T = card.get("corner") or ""
    tg = _target(metric, card, T)
    zone = _zone(report, card.get("id"))
    if tg is None or zone is None:
        return None
    p = _base(report, "focus", car, track)
    p.update(tg)
    conf = card.get("confidence") if card.get("confidence") in CONF_RANK else "thin"
    if second:                                      # the card's confidence is its main rule's
        conf = _cap(conf, "likely")
    p.update({"corner": T, "id": card.get("id"), "metric": metric, "how": card.get("how"), "why": card.get("why"),
              "zone": zone, "lost_s": _num(card.get("lost_s")), "gap_s": _num(card.get("gap_s")),
              "confidence": conf, "evidence": card.get("evidence"), "group": card.get("group"),
              "p_value": _num(card.get("p_value")), "experiment": False, "from_rule": "second" if second else "main"})
    if conf == "thin":
        p["text"] += " (from only a few laps so far)"
    p["reason"] = (f"In {T} your best laps were about {p['lost_s']:.2f} s quicker than your typical ones, and the "
                   f"corner coach found one clear thing they do differently."
                   if p["lost_s"] is not None else f"{T} shows the clearest difference in how you drive it.")
    return p


def _card_plans(report, card, car=None, track=None):
    """Plans from one card, its main rule first, then its second one."""
    out = []
    for k, rule in enumerate((card.get("rule"), card.get("secondary"))):
        for m in RULE_METRICS.get(rule or "", ()):
            p = _from_card(report, card, m, car, track, second=k == 1)
            if p is not None and all(q["metric"] != m for q in out):
                out.append(p)
    return out


def _consistency(report, car=None, track=None):
    """The corner where typical laps lose most to your best laps with no clear driving difference: aim to drive it
    the same way every lap."""
    pc = (report or {}).get("per_corner") or {}
    rows = sorted((report or {}).get("unclear") or [], key=lambda u: -(_num(u.get("lost_s")) or 0))
    for u in rows:
        cid = u.get("id")
        tn = ((pc.get(cid) or {}).get("numbers") or {}).get("t") or {}
        b, t = _num(tn.get("best")), _num(tn.get("typical"))
        lost = _num(u.get("lost_s"))
        zone = _zone(report, cid)
        if b is None or t is None or lost is None or zone is None or lost < cc.MIN_LOST_S:
            continue
        T = u.get("corner") or ""
        tgt = round(b + lost / 2.0, 2)
        p = _base(report, "consistency", car, track)
        grp = f" ({u['group']})" if u.get("group") else ""
        p.update({"corner": T, "id": cid, "metric": "corner_time", "unit": "s", "target": tgt, "tolerance": 0.0,
                  "now": {"typical": round(t, 2), "best": round(b, 2)},
                  "text": f"{EXPERIMENT}: drive {T} the same way every lap. Your best laps take {b:.2f} s through "
                          f"here, a typical lap {t:.2f} s",
                  "voice": f"{T}: same every lap",
                  "how": "Pick your braking marker, turn-in point and the place you go back to full throttle, and "
                         "use the same ones every lap. Don't try anything new here; just repeat.",
                  "why": ("The corner coach found no clear thing your best laps do differently here, so this is an "
                          "experiment, not a known fix: see whether doing it the same way each lap helps."),
                  "check": f"We'll count laps where {T} takes {tgt:.2f} s or less",
                  "success_rule": {"key": "t", "op": "<=", "value": tgt},
                  "zone": zone, "lost_s": lost, "gap_s": _num(u.get("gap_s")), "confidence": "unclear",
                  "experiment": True, "group": u.get("group"),
                  "evidence": f"{u.get('best_laps')} best laps vs {u.get('typical_laps')} typical laps{grp}.",
                  "reason": f"In {T} your best laps were about {lost:.2f} s quicker than your typical ones, but there's "
                            f"no clear pattern in what you do differently, so it may be lap-to-lap scatter."})
        return p
    return None


def _done_corners(history) -> set:
    """Corners he has already got (the latest score for that corner said move on)."""
    seen, done = set(), set()
    for h in history or []:
        if not isinstance(h, dict) or not h.get("id"):
            continue
        sc = h.get("score")
        if not isinstance(sc, dict) or h["id"] in seen:
            continue
        seen.add(h["id"])
        if ((sc.get("next") or {}).get("action")) == "next":
            done.add(h["id"])
    return done


def make_plan(corner_report: dict | None, history=None, car=None, track=None) -> dict:
    """The single focus for the next session (see the module text for the shape). Never raises.

    Order: the biggest-loss card that is clear or likely, then thin ones (only when nothing else), each turned into
    a measurable target (its main rule, else its second one). With history (plans newest first, as load_plans
    returns): when the last scored plan said keep, that focus is kept (with today's numbers when the card is still
    there); corners he has already got are skipped while others remain. No card: the most variable corner (drive it
    the same way every lap). Too few laps: drive WANT_LAPS clean laps first."""
    try:
        return _make_plan(corner_report or {}, list(history or []), car, track)
    except Exception as e:  # noqa: BLE001
        p = _base(corner_report if isinstance(corner_report, dict) else {}, "none", car, track)
        p["text"] = "Debrief couldn't pick a focus from these laps. Drive a few more clean laps and try again."
        p["reason"] = f"error: {str(e)[:120]}"
        return p


def _make_plan(report, history, car, track):
    laps = int(report.get("laps") or 0)
    if not report.get("ok") or laps < cc.MIN_LAPS:
        p = _base(report, "laps_first", car, track)
        groups = [g for g in report.get("groups") or [] if isinstance(g, dict)]
        n_all = sum(int(g.get("laps") or 0) for g in groups) if groups else laps
        mixed = len(groups) > 1 and n_all >= cc.MIN_LAPS
        p["from_laps"] = n_all
        p["text"] = (f"Drive {WANT_LAPS} clean laps first. Then Debrief can pick one thing for you to work on."
                     + (f" So far: {n_all} clean lap{'s' if n_all != 1 else ''}" if n_all else "")
                     + (", but not enough of the same kind on the same setup." if mixed else "." if n_all else ""))
        p["voice"] = None
        p["reason"] = f"the corner coach needs at least {cc.MIN_LAPS} clean laps"
        p["check"] = f"We'll count your clean laps (aim for {WANT_LAPS})."
        return p
    cards = [c for c in report.get("cards") or [] if isinstance(c, dict) and c.get("id")]
    cards.sort(key=lambda c: (CONF_ORDER.get(c.get("confidence"), 1), -(_num(c.get("lost_s")) or 0)))

    last = next((h for h in history if isinstance(h, dict) and isinstance(h.get("score"), dict)
                 and h.get("kind") in ("focus", "consistency")), None)
    if last is not None and ((last["score"].get("next") or {}).get("action")) == "keep":
        card = next((c for c in cards if c.get("id") == last.get("id")), None)
        p = _from_card(report, card, last.get("metric"), car, track) if card else None
        gone = p is None
        if gone:
            p = {k: v for k, v in last.items() if k != "score"}
            p.update({"plan_id": uuid.uuid4().hex[:12], "made": _now(), "from_laps": laps})
            if p.get("kind") == "focus":          # today's laps don't back it up: say so, never more sure than that
                p["confidence"] = "unclear"
                p["experiment"] = True
                txt = str(p.get("text") or "").replace(" (from only a few laps so far)", "")
                p["text"] = txt if txt.startswith(EXPERIMENT) else f"{EXPERIMENT}: {txt}"
        sc = last["score"]
        p["repeat"] = True
        p["reason"] = (f"Same focus as last time: you hit it on {sc.get('hits', 0)} of {sc.get('measured', 0)} "
                       f"laps, so it isn't a habit yet." if sc.get("measured") else
                       "Same focus as last time: there were no clean laps through this corner to score.")
        if gone and p.get("kind") == "focus":
            p["reason"] += " Your latest laps don't show a clear pattern here any more, so treat it as an experiment."
        return p

    done = _done_corners(history)
    plans = []
    for c in cards:
        plans += _card_plans(report, c, car, track)
    fresh = [p for p in plans if p["id"] not in done]
    pick = (fresh or plans or [None])[0]
    if pick is not None:
        if pick["id"] in done:
            pick["reason"] += " (You'd already got this one, but nothing else stands out yet.)"
        return pick
    p = _consistency(report, car, track)
    if p is not None:
        return p
    p = _base(report, "none", car, track)
    p["text"] = ("Every corner looks steady so far: no clear thing to change and no corner varies much. Drive more "
                 "laps and Debrief will look again.")
    p["reason"] = "no corner coach card and no corner varies much from lap to lap"
    return p


# ---------------------------------------------------------------- scoring a session
def session_phases(laps, plan) -> list:
    """[{"file", "lap", "time", "phases"}] for the plan's corner, from cornercoach.load_laps laps (clean laps; laps
    slower than cornercoach.OFFPACE_FRAC of the median are left out, as the corner coach does)."""
    out = []
    zone = (plan or {}).get("zone")
    if not zone:
        return out
    laps = [x for x in laps or [] if isinstance(x, dict) and isinstance(x.get("R"), dict) and _num(x.get("time"))]
    if laps:
        med = float(np.median([x["time"] for x in laps]))
        laps = [x for x in laps if x["time"] <= med * cc.OFFPACE_FRAC]
    for x in laps:
        out.append({"file": x.get("file"), "lap": x.get("lap"), "time": _num(x.get("time")),
                    "phases": cc.phases(x["R"], zone)})
    return out


def _fmt_val(plan, v):
    m = plan.get("metric")
    if v is None:
        return None
    if m in ("coast", "corner_time"):
        return round(v, 2)
    return _ri(v)


def score_plan(plan: dict, session_laps_phases, report: dict | None = None) -> dict:
    """How the session went against the plan. session_laps_phases: one item per clean lap, either a phase dict for
    the plan's corner (cornercoach.phases) or {"lap", "file", "time", "phases": phase dict} (session_phases). An item
    with phases None is a clean lap where the corner couldn't be read.

    {"plan_id", "corner", "metric", "laps", "measured", "hits", "misses", "hit_share", "per_lap": [{"lap", "file",
     "value", "hit", "t"}], "text", "time_hit", "time_miss", "gain_s", "z", "verdict": "worth" | "slower" | "unclear"
     | "too_few" | "never" | "n/a", "verdict_text", "p" (shuffle test), "shuffles", "next": {"action": "keep" |
     "next", "text"}, "next_plan" (with report), "scored"}
    "z" is kept as None for older readers (2.14 uses the shuffle test, shuffle_test)."""
    try:
        return _score(plan or {}, list(session_laps_phases or []), report)
    except Exception as e:  # noqa: BLE001
        return {"plan_id": (plan or {}).get("plan_id"), "laps": 0, "measured": 0, "hits": 0, "misses": 0,
                "hit_share": None, "per_lap": [], "verdict": "never",
                "text": "Debrief couldn't score this session.", "verdict_text": f"({str(e)[:120]})",
                "next": {"action": "keep", "text": "Keep the same focus next session."}, "scored": _now()}


def shuffle_test(hit_times, miss_times, n: int = SHUFFLE_N, seed: int = SHUFFLE_SEED) -> dict | None:
    """Are laps that hit the plan quicker through the corner than laps that missed? The hit / miss labels are shuffled
    n times (fixed seed, at least 200) and each time the share of (hit lap, missed lap) pairs where the hit lap was
    quicker is worked out (ties count half; ranks, so one slow lap can't swing it, and with 4 laps a side a 3-and-1
    split never looks as clear as a clean one, which a difference of medians would). p = share of shuffles at least
    as far from an even split either way (two-sided, counting the real split). gain = median of the missed laps
    minus median of the hit laps (positive = hits quicker). None with fewer than MIN_EACH of either."""
    a = np.asarray([v for v in (_num(x) for x in hit_times or []) if v is not None], float)
    b = np.asarray([v for v in (_num(x) for x in miss_times or []) if v is not None], float)
    if len(a) < MIN_EACH or len(b) < MIN_EACH:
        return None
    n = max(200, int(n))
    pool = np.concatenate([a, b])
    rng = np.random.default_rng(int(seed))
    idx = np.vstack([np.arange(len(pool))[None, :], rng.permuted(np.tile(np.arange(len(pool)), (n, 1)), axis=1)])
    H, M = pool[idx[:, :len(a)]], pool[idx[:, len(a):]]
    d = M[:, None, :] - H[:, :, None]                      # > 0: the hit lap was quicker
    auc = ((d > 0) + 0.5 * (d == 0)).mean(axis=(1, 2))
    far = np.abs(auc - 0.5)
    p = (1.0 + float(np.sum(far[1:] >= far[0] - 1e-12))) / (n + 1.0)
    return {"gain": float(np.median(b) - np.median(a)), "auc": float(auc[0]), "p": p, "n": n,
            "n_hit": int(len(a)), "n_miss": int(len(b))}


def _score(plan, items, report):
    T = plan.get("corner") or "that corner"
    rows = []
    for k, it in enumerate(items):
        if isinstance(it, dict) and "phases" in it:
            ph, lap, f = it.get("phases"), it.get("lap"), it.get("file")
        else:
            ph, f = it, None
            tag = it.get("_lap") if isinstance(it, dict) else None
            lap = tag[1] if isinstance(tag, (list, tuple)) and len(tag) == 2 else k + 1
        val, hit = measure(plan, ph) if isinstance(ph, dict) else (None, None)
        t = _num((ph or {}).get("t")) if isinstance(ph, dict) else None
        rows.append({"lap": lap, "file": f, "value": _fmt_val(plan, val), "hit": hit, "t": None if t is None else round(t, 3)})
    out = {"plan_id": plan.get("plan_id"), "corner": plan.get("corner"), "id": plan.get("id"),
           "metric": plan.get("metric"), "laps": len(rows), "per_lap": rows, "scored": _now(),
           "time_hit": None, "time_miss": None, "gain_s": None, "z": None, "p": None, "shuffles": None,
           "confidence": plan.get("confidence"), "experiment": bool(plan.get("experiment"))}
    measured = [r for r in rows if r["hit"] is not None]
    hits = [r for r in measured if r["hit"]]
    misses = [r for r in measured if not r["hit"]]
    out.update({"measured": len(measured), "hits": len(hits), "misses": len(misses),
                "hit_share": round(len(hits) / len(measured), 3) if measured else None})
    kind = plan.get("kind")
    if kind not in ("focus", "consistency"):
        n = len(rows)
        out["verdict"] = "n/a"
        out["text"] = f"You drove {n} clean lap{'s' if n != 1 else ''}."
        out["verdict_text"] = ""
        enough = n >= cc.MIN_LAPS
        out["next"] = {"action": "next" if enough else "keep",
                       "text": ("That's enough for the corner coach. Next session, Debrief picks one thing to work on."
                                if enough else f"Keep going: {WANT_LAPS} clean laps gives Debrief enough to work with.")}
        return _with_next_plan(out, plan, report)
    if not measured:
        out["verdict"] = "never"
        out["text"] = (f"You didn't get a clean lap through {T} this session that could be measured, so there's "
                       f"nothing to score yet." if rows else
                       f"You didn't get a clean lap through {T} this session, so there's nothing to score yet.")
        out["verdict_text"] = ""
        out["next"] = {"action": "keep", "text": f"Keep the same focus next session. {plan.get('text')}."}
        return _with_next_plan(out, plan, report)
    out["text"] = f"You hit it on {len(hits)} of {len(measured)} lap{'s' if len(measured) != 1 else ''}."
    th = [r["t"] for r in hits if r["t"] is not None]
    tm = [r["t"] for r in misses if r["t"] is not None]
    out["time_hit"] = round(float(np.median(th)), 3) if th else None
    out["time_miss"] = round(float(np.median(tm)), 3) if tm else None
    if plan.get("metric") == "corner_time":
        out["verdict"] = "n/a"
        out["verdict_text"] = (f"Through {T} your laps that hit it took {out['time_hit']:.2f} s (median)"
                               + (f", the others {out['time_miss']:.2f} s." if out["time_miss"] is not None else ".")
                               if out["time_hit"] is not None else "")
    elif len(th) < MIN_EACH or len(tm) < MIN_EACH:
        out["verdict"] = "too_few"
        out["verdict_text"] = (f"Not enough laps to tell yet whether it's quicker: Debrief needs at least {MIN_EACH} "
                               f"laps that hit it and {MIN_EACH} that didn't ({len(th)} and {len(tm)} this time).")
    else:
        d = shuffle_test(th, tm)
        gain = d["gain"]
        out["gain_s"] = round(gain, 3)
        out["p"] = round(d["p"], 4)
        out["shuffles"] = d["n"]
        if abs(gain) < MIN_GAIN_S:
            out["verdict"] = "unclear"
            out["verdict_text"] = (f"No real time difference between laps that hit it and laps that didn't "
                                   f"(under {MIN_GAIN_S:.2f} s through {T}, {len(th)} vs {len(tm)} laps); that is "
                                   f"lap-to-lap scatter.")
        elif d["p"] < SHUFFLE_P and gain > 0:
            out["verdict"] = "worth"
            out["verdict_text"] = (f"Your laps that hit it were about {gain:.2f} s quicker through {T} (median, "
                                   f"{len(th)} laps that hit it vs {len(tm)} that didn't).")
        elif d["p"] < SHUFFLE_P:
            out["verdict"] = "slower"
            out["verdict_text"] = (f"So far your laps that hit it were not quicker through {T} (about "
                                   f"{-gain:.2f} s slower, {len(th)} vs {len(tm)} laps). It can take a few "
                                   f"sessions to feel natural.")
        else:
            out["verdict"] = "unclear"
            out["verdict_text"] = (f"No clear time difference yet between laps that hit it and laps that didn't "
                                   f"({len(th)} vs {len(tm)} laps); the gap is within the lap-to-lap scatter.")
    share = len(hits) / len(measured)
    if share < KEEP_BELOW:
        out["next"] = {"action": "keep",
                       "text": f"Keep the same focus next session until it's a habit (7 laps out of 10). "
                               f"{plan.get('text')}."}
    else:
        out["next"] = {"action": "next",
                       "text": f"You've got {T}. Next session, move on to the next thing."}
    return _with_next_plan(out, plan, report)


def _with_next_plan(out, plan, report):
    if isinstance(report, dict):
        scored = dict(plan, score={k: v for k, v in out.items() if k != "next_plan"})
        out["next_plan"] = make_plan(report, history=[scored], car=plan.get("car"), track=plan.get("track"))
    return out


def score_files(plan: dict, paths, report: dict | None = None) -> dict:
    """score_plan over the clean laps of one session's .ibt files (cornercoach.load_laps). Never raises."""
    try:
        laps, _info = cc.load_laps(list(paths) if isinstance(paths, (list, tuple)) else [paths])
    except Exception:  # noqa: BLE001
        laps = []
    return score_plan(plan, session_phases(laps, plan), report)


# ---------------------------------------------------------------- live progress
PRAISE = {"brake_point": "Good brake point", "trail": "Good, held it in", "coast": "Good, no coasting",
          "throttle_point": "Good throttle", "min_speed": "Good speed", "corner_time": "Good, on pace"}


def progress_cue(plan: dict, lap_phases: dict | None) -> str | None:
    """A 2-4 word cue after the focus corner, or None (no plan, no reliable number for this pass). lap_phases may
    carry "_reliable": False (the live pass was not seen cleanly), then nothing is said."""
    if not isinstance(plan, dict) or plan.get("kind") not in ("focus", "consistency"):
        return None
    if not isinstance(lap_phases, dict) or lap_phases.get("_reliable") is False:
        return None
    val, hit = measure(plan, lap_phases)
    if hit is None:
        return None
    m = plan.get("metric")
    if hit:
        if m == "peak_brake":
            return f"Good, {_ri(val)}"
        return PRAISE.get(m)
    if m == "peak_brake":
        return "Too much brake"
    if m == "brake_point":
        tgt = _num(plan.get("target")) or 0
        return "Braked too early" if val > tgt else "Braked too late"
    if m == "trail":
        return "Hold the brake in"
    if m == "coast":
        return "Less coasting"
    if m == "throttle_point":
        return "Throttle earlier"
    if m == "min_speed":
        return "More speed"
    return None                                    # corner_time: nothing to say about a slower pass


def cue_vocabulary(plan: dict) -> list:
    """Every progress cue this plan can give (for the voice to render ahead of time)."""
    m = (plan or {}).get("metric")
    out = []
    if m == "peak_brake":
        out += [f"Good, {n}" for n in range(0, 101)] + ["Too much brake"]
    elif m == "brake_point":
        out += [PRAISE[m], "Braked too early", "Braked too late"]
    elif m in PRAISE:
        out.append(PRAISE[m])
        out += {"trail": ["Hold the brake in"], "coast": ["Less coasting"], "throttle_point": ["Throttle earlier"],
                "min_speed": ["More speed"]}.get(m, [])
    return out


class LiveFocus:
    """The plan's corner, watched live: feed(sample, ok, lap) for every sample (livecoach.Coach does this when given
    one with set_focus). It keeps the samples of each pass through the corner, and when the car leaves the corner
    forwards it works out the pass's number (livecoach.corner_phases: the corner coach's own phase rules) and
    returns progress_cue's text, or None.

    A pass counts only when every sample was ok (a clean, coachable lap, no car alongside), it started before the
    corner zone and covered it with no gap over MAX_GAP_M. Cues: always on a change (hit after a miss or the other
    way round), else at most every repeat_laps laps, so a run of good laps isn't praised every lap. The coach's own
    limits (gap between cues, per lap, per corner, silence rules) still apply on top."""
    MAX_GAP_M = 25.0
    MIN_SAMPLES = 10

    def __init__(self, plan: dict, length_m: float | None = None, repeat_laps: int = 3):
        self.plan = plan if isinstance(plan, dict) else {}
        self.zone = self.plan.get("zone") or None
        self.L = _num(length_m) or _num(self.plan.get("length_m"))
        self.corner_id = self.plan.get("id")
        self.repeat_laps = max(1, int(repeat_laps))
        self.active = bool(self.zone and self.L and self.plan.get("kind") in ("focus", "consistency"))
        self.passes: list = []          # [{"lap", "value", "hit"}] measured passes this session
        self._buf: list = []
        self._ok = True
        self._last_rel = None
        self._last_cue = None           # (lap, hit) of the last cue given
        if self.active:
            a = float(self.zone["apex_m"])
            self._s_rel = float(self.zone["start_m"]) - a
            self._e_rel = float(self.zone["end_m"]) - a

    def vocabulary(self) -> list:
        return cue_vocabulary(self.plan) if self.active else []

    def _rel(self, pct: float) -> float:
        L = self.L
        return ((pct * L - float(self.zone["apex_m"]) + L / 2.0) % L) - L / 2.0

    def feed(self, s: dict, ok: bool = True, lap=None) -> str | None:
        if not self.active or not isinstance(s, dict):
            return None
        pct = _num(s.get("LapDistPct"))
        if pct is None or pct < 0 or pct > 1.0001:
            self._reset()
            return None
        rel = self._rel(pct)
        lo = self._s_rel - 6 * cc.STEP                  # start listening 30 m before the zone
        inside = lo <= rel <= self._e_rel
        prev = self._last_rel
        self._last_rel = rel
        if inside:
            if prev is not None and (rel < prev - 1.0 or rel - prev > self.MAX_GAP_M):
                self._ok = False                         # went backwards (reset, tow) or a gap in the samples
            if not self._buf:
                self._ok = bool(ok) and rel <= self._s_rel
            self._ok = self._ok and bool(ok)
            self._buf.append(s)
            return None
        if not self._buf:
            return None
        buf, good = self._buf, self._ok
        self._reset()
        if not (self._e_rel < rel <= self._e_rel + 200.0) or not good or len(buf) < self.MIN_SAMPLES:
            return None
        from .livecoach import corner_phases
        ph = corner_phases(buf + [s], self.zone, self.L)
        val, hit = measure(self.plan, ph)
        if hit is None:
            return None
        self.passes.append({"lap": lap, "value": _fmt_val(self.plan, val), "hit": bool(hit)})
        del self.passes[:-200]
        text = progress_cue(self.plan, ph)
        if text is None:
            return None
        last = self._last_cue
        if last is not None and last[1] == bool(hit) and lap is not None and last[0] is not None \
                and lap - last[0] < self.repeat_laps:
            return None
        self._last_cue = (lap, bool(hit))
        return text

    def _reset(self):
        self._buf = []
        self._ok = True

    def status(self) -> dict:
        n = len(self.passes)
        h = sum(1 for p in self.passes if p["hit"])
        return {"corner": self.plan.get("corner"), "id": self.corner_id, "metric": self.plan.get("metric"),
                "target": self.plan.get("target"), "text": self.plan.get("text"), "active": self.active,
                "measured": n, "hits": h, "passes": self.passes[-20:],
                "progress": (f"Hit {h} of {n} so far" if n else "No clean pass through the corner yet")}


# ---------------------------------------------------------------- keeping plans per car + track
_SAFE = re.compile(r"[^a-z0-9]+")


def plan_path(folder, car, track) -> Path:
    """<folder>/<car>__<track>.json (lower case, letters and digits only)."""
    def slug(x):
        return _SAFE.sub("-", str(x or "unknown").lower()).strip("-")[:60] or "unknown"
    return Path(folder) / f"{slug(car)}__{slug(track)}.json"


def _read(path: Path):
    """(data, damaged): data {"version", "car", "track", "plans": [...]}; damaged True when the file was unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": VERSION, "plans": []}, False
    except (OSError, ValueError, UnicodeDecodeError):
        return {"version": VERSION, "plans": []}, True
    if not isinstance(raw, dict) or not isinstance(raw.get("plans"), list):
        return {"version": VERSION, "plans": []}, True
    raw["plans"] = [p for p in raw["plans"] if isinstance(p, dict)]
    return raw, False


def load_plans(folder, car, track) -> list:
    """Every saved plan for this car + track, newest first (each may carry its "score"). [] when there are none or
    the file is damaged. Never raises."""
    try:
        return _read(plan_path(folder, car, track))[0]["plans"]
    except Exception:  # noqa: BLE001
        return []


def current_plan(folder, car, track) -> dict | None:
    p = load_plans(folder, car, track)
    return p[0] if p else None


def _write(path: Path, data: dict, damaged: bool) -> bool:
    try:
        if damaged and path.exists():
            try:
                path.replace(path.with_name(path.name + ".bad"))      # kept for a look, never deleted
            except OSError:
                pass
        fsutil.write_json(path, data, mkdir=True, compact=False, indent=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def save_plan(folder, car, track, plan: dict) -> bool:
    """Put plan first (newest) in this car + track's file (a plan with the same plan_id is replaced). Keeps the last
    MAX_HISTORY. False when it can't be written. A damaged file is renamed <name>.bad and a new one started."""
    if not isinstance(plan, dict):
        return False
    path = plan_path(folder, car, track)
    data, damaged = _read(path)
    pid = plan.get("plan_id")
    plans = [p for p in data["plans"] if not pid or p.get("plan_id") != pid]
    data.update({"version": VERSION, "car": car, "track": track, "plans": ([plan] + plans)[:MAX_HISTORY]})
    return _write(path, data, damaged)


def save_score(folder, car, track, plan_id: str, score: dict) -> bool:
    """Attach a score to the saved plan with this plan_id. False when there is no such plan or it can't be written."""
    path = plan_path(folder, car, track)
    data, damaged = _read(path)
    if damaged:
        return False
    for p in data["plans"]:
        if p.get("plan_id") == plan_id:
            p["score"] = {k: v for k, v in (score or {}).items() if k != "next_plan"}
            return _write(path, data, False)
    return False
