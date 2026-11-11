"""C2, the fact sheet (2.30.1): what Debrief hands Siyanqaba to narrate. See CONTRACT.md.

Every value in `clauses` is a finished, unit-bearing phrase written here from Debrief's own numbers; a model may only
put the clauses in order. Decisions (`required`, `intent`) come from Debrief's logic. Anything Debrief's shuffle test
or lap counts don't back goes in `not_proven_yet` with what is missing, never in `clauses`.

    build(review, cards=None, session_id=None, speed_unit="mph", fuel_unit="L", feel=None, version=None) -> dict
    check(sheet) -> [problems]     [] for a sheet that keeps the contract

Pure: no files, no network. Units follow Settings > Units (mph by default; litres or gallons).
"""
from __future__ import annotations

import math
import re

from . import cornercoach, units

FACTSHEET_VERSION = "1"
CLAUSE_KEYS = ("summary", "best", "consistency", "fuel", "focus", "feel", "trend")
INTENTS = ("focus", None)          # pit / stay / attack / defend are live calls: never in a post-session sheet
RESERVED_INTENTS = ("pit", "stay", "attack", "defend")
SAME_S = 0.05                      # a smaller change against the last session is "about the same"
GAL_L = 3.785
MAX_FEEL = 3                       # feel notes in the clause; the rest stay in the feel file
FEEL_TAGS = ("loose", "push", "brakes_long", "lockup", "bottoming", "snap")
FEEL_PHASES = ("entry", "mid", "exit")
_PHASE_WORDS = {"entry": " on entry", "mid": " mid-corner", "exit": " on exit", None: ""}
_FEEL_WORDS = {
    "loose": "{t} felt loose{p}",
    "push": "{t} pushed wide{p}",
    "brakes_long": "braking ran long into {t}",
    "lockup": "you locked up in {t}{p}",
    "bottoming": "the car bottomed out in {t}{p}",
    "snap": "the rear snapped in {t}{p}",
}


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def turn_label(cid) -> str:
    """"C6" / "T6" -> "Turn 6"; anything else as it is."""
    m = re.fullmatch(r"[CT](\d+)", str(cid or ""))
    return f"Turn {m.group(1)}" if m else str(cid or "")


def session_kind(types) -> str:
    t = [str(x).lower() for x in (types or [])]
    if any("race" in x for x in t):
        return "race"
    if any("qual" in x for x in t):
        return "qualify"
    return "practice"


def _corners(cards: dict | None) -> list:
    info = (cards or {}).get("info") or {}
    L = _num(info.get("length_m"))
    out = []
    for c in (cards or {}).get("corners") or []:
        a = _num(c.get("apex_m"))
        out.append({"id": c.get("id"), "label": turn_label(c.get("id")),
                    "apex_pct": round(a / L, 4) if a is not None and L else None,
                    "name": None, "name_source": "detected"})
    return out


def _feel_clause(feel: dict | None, corners: list, length_m) -> str | None:
    from . import c1
    taps = [t for t in ((feel or {}).get("taps") or []) if t.get("tag") in FEEL_TAGS]
    if not taps:
        return None
    phrases, seen = [], set()
    for t in sorted(taps, key=lambda t: str(t.get("at") or ""), reverse=True):
        cid = c1.corner_key(t.get("apex_pct"), corners, length_m) if t.get("apex_pct") is not None else None
        label = turn_label(cid or t.get("corner"))
        ph = t.get("phase") if t.get("phase") in FEEL_PHASES else None
        words = _FEEL_WORDS[t["tag"]].format(t=label, p=_PHASE_WORDS[ph])
        if words in seen:
            continue
        seen.add(words)
        phrases.append(words)
        if len(phrases) == MAX_FEEL:
            break
    return "you noted " + (", ".join(phrases[:-1]) + " and " + phrases[-1] if len(phrases) > 1 else phrases[0])


def build(review: dict, cards: dict | None = None, session_id: str | None = None, speed_unit: str = "mph",
          fuel_unit: str = "L", feel: dict | None = None, version: str | None = None) -> dict:
    """The C2 v1 fact sheet for one session review (c1.review_session) and its corner cards (c1.corner_cards)."""
    if version is None:
        from . import __version__ as version
    speed_unit = units.norm(speed_unit)
    gallons = str(fuel_unit or "").lower().startswith("gal")
    clauses, not_proven = {}, []
    s = (review or {}).get("summary") or {}
    corners = _corners(cards)
    length_m = _num(((cards or {}).get("info") or {}).get("length_m"))
    sheet = {
        "factsheet_version": FACTSHEET_VERSION,
        "producer": {"app": "debrief", "version": str(version), "c1": _c1_version()},
        "session": {"id": session_id, "track": review.get("track") or "", "track_key": review.get("track_code") or "",
                    "car": review.get("car") or "", "car_path": review.get("car_path") or "",
                    "kind": session_kind(review.get("session_types")), "date": str(review.get("date") or "")[:10],
                    "units": {"speed": units.label(speed_unit), "fuel": "gal" if gallons else "L"}},
        "corners": corners,
        "required": [], "intent": None, "clauses": clauses, "proven": {}, "not_proven_yet": not_proven,
    }
    best = _num(s.get("best"))
    if best is None:
        clauses["summary"] = "no valid laps to analyze in this session"
        sheet["proven"] = {"summary": True}
        return sheet

    # best lap and best possible lap
    th = review.get("theoretical") or {}
    text = f"best lap {units.lap(best)}"
    if th.get("enough") and _num(th.get("possible")) is not None:
        text += f", best corners added up {units.lap(th['possible'])}"
    else:
        need = max(0, int((review.get("thresholds") or {}).get("min_laps") or 5)
                   - int(th.get("laps_counted") or th.get("valid_laps") or 0))
        not_proven.append("best possible lap" + (f" (needs {_plural(need, 'more lap')})" if need else ""))
    clauses["best"] = text

    # consistency
    con = review.get("consistency") or {}
    n = int(con.get("clean_laps") or 0)
    if con.get("enough") and con.get("near_best") is not None:
        clauses["consistency"] = f"{con['near_best']} of {_plural(n, 'clean lap')} within half a second of your best"
    else:
        need = max(0, int((review.get("thresholds") or {}).get("min_laps") or 5) - n)
        not_proven.append(f"consistency (needs {_plural(need, 'more clean lap')})" if need else "consistency")

    # fuel
    fu = review.get("fuel") or {}
    per = _num(fu.get("per_lap_l"))
    if per and per > 0:
        if gallons:
            text = f"fuel use {per / GAL_L:.2f} gallons a lap"
        else:
            text = f"fuel use {per:.2f} liters a lap"
        tank = _num(fu.get("tank_l"))
        if tank and str(fu.get("tank_source") or "").startswith("session"):
            text += f", about {int(tank // per)} laps a tank"
        clauses["fuel"] = text

    # focus: the biggest card that passed the shuffle test with enough laps
    cc = cards or {}
    focus_card = None
    for c in cc.get("cards") or []:
        label = turn_label(c.get("id") or c.get("corner"))
        if c.get("confidence") in ("likely", "clear") and _num(c.get("lost_s")) is not None:
            if focus_card is None:
                focus_card = c
        else:
            laps = int(c.get("best_laps") or 0) + int(c.get("typical_laps") or 0)
            need = max(0, cornercoach.THIN_LAPS - laps)
            not_proven.append(f"{label} pattern" + (f" (needs {_plural(need, 'more clean lap')})" if need else " (thin)"))
    if focus_card is not None:
        label = turn_label(focus_card.get("id") or focus_card.get("corner"))
        text = f"in {label} your best laps were about {float(focus_card['lost_s']):.2f} seconds quicker"
        how = str(focus_card.get("how") or "").strip().rstrip(".")
        if how:
            how = re.sub(r"\bT(\d+)\b", r"Turn \1", units.convert_text(how, speed_unit))
            text += "; " + how[:1].lower() + how[1:]
        clauses["focus"] = text
        sheet["required"] = ["focus"]
        sheet["intent"] = "focus"
    for u in cc.get("unclear") or []:
        not_proven.append(f"{turn_label(u.get('id') or u.get('corner'))}: no clear pattern yet")
    if not cc.get("cards") and not cc.get("unclear") and cc.get("message"):
        not_proven.append("corner tips (" + str(cc["message"]).strip().rstrip(".")[:120] + ")")

    # trend against the last session here
    hist = review.get("history") or {}
    d = _num(((hist.get("vs_last") or {}).get("best_s")))
    if d is not None:
        if abs(d) < SAME_S:
            clauses["trend"] = "best lap about the same as your last session here"
        else:
            clauses["trend"] = f"best lap {abs(d):.2f} seconds {'quicker' if d < 0 else 'slower'} than your last session here"

    fc = _feel_clause(feel, corners, length_m)
    if fc:
        clauses["feel"] = fc
    sheet["proven"] = {k: True for k in clauses}
    return sheet


def _c1_version() -> str:
    from . import c1
    return c1.C1_VERSION


def check(sheet: dict) -> list:
    """Problems with a sheet against C2 v1 ([] = fine)."""
    p = []
    if not isinstance(sheet, dict):
        return ["not a JSON object"]
    if sheet.get("factsheet_version") != FACTSHEET_VERSION:
        p.append(f"factsheet_version is not {FACTSHEET_VERSION}")
    cl = sheet.get("clauses")
    if not isinstance(cl, dict) or not cl:
        p.append("no clauses")
        cl = {}
    for k, v in cl.items():
        if k not in CLAUSE_KEYS:
            p.append(f"unknown clause {k}")
        if not isinstance(v, str) or not v.strip():
            p.append(f"clause {k} is empty")
        elif re.search(r"\{|\}", v):
            p.append(f"clause {k} has a brace")
    for r in sheet.get("required") or []:
        if r not in cl:
            p.append(f"required {r} is not a clause")
    if sheet.get("intent") not in INTENTS:
        p.append(f"intent {sheet.get('intent')!r} is not allowed after a session")
    for k in ("session", "producer", "corners", "proven", "not_proven_yet"):
        if k not in sheet:
            p.append(f"missing {k}")
    for c in sheet.get("corners") or []:
        if "apex_pct" not in c or "id" not in c:
            p.append("a corner without id or apex_pct")
            break
    for x in sheet.get("not_proven_yet") or []:
        if not isinstance(x, str):
            p.append("not_proven_yet holds a non-text item")
            break
    # numbers live in clauses only: nothing outside clauses / session / producer / corners may be a number phrase
    for k, v in sheet.items():
        if k in ("clauses", "session", "producer", "corners", "proven", "not_proven_yet", "factsheet_version"):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            p.append(f"bare number in {k}")
    return p
