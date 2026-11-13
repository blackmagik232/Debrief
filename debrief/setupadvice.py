"""Automatic setup advice (2.23): turns the handling read (handling.py) into ONE ranked change at a time, with a plain
reason and what to expect. Pure logic; never raises. The advice is applied by hand in the iRacing garage.

Generic rules by setup area (anti-roll bars, springs, ride height and rake, wing, differential preload / coast /
power, brake bias, traction control, tyre pressures, dampers), the same for every car: a rule only offers a setting
the car's own setup actually has, and steps it from the value in the file. Reused from balance.py: the bar, bias,
traction control and wing settings and their stepping; and its rules that two ideas never move the same setting
opposite ways (the stronger one keeps it), and that an idea never just undoes his last change unless the laps showed
that change slower (balance._undo_of, setuplab.change_into).

Fixed-setup series (setuplab's own detection, passed in as series_setup="fixed"): only the in-car dials (brake bias,
traction control, ABS) where the car has them, otherwise driving advice.

Driving, not setup: an issue handling.py calls driving gets no setup change, only a pointer to the corner coach.

"How did the car feel?": his answer per issue (agree / not what I felt) moves the sureness one step for next time
(adjust_sureness). Setup history: each suggestion shown is kept per car and track (record_shown); once he has driven
a different setup, evaluate() says whether he did it and what Setup Lab's own comparison (setuplab.change_into: its
verdict and its slower flag) and the next handling read say. Nothing new is measured here.

advise(report, setup, series_setup=None, last_change=None, dials=None, feel=None) -> advice
"""
from __future__ import annotations

import math

from . import balance as bl
from . import handling as hd
from . import ibt

THEN = "Change only this, then drive at least 6 clean laps and compare again in Setup Lab."
DIFF_CAVEAT = ("Which way is more locking differs from car to car (a bigger ramp angle can mean less lock): check "
               "the garage's own help text for this car.")
DAMP_CAVEAT = "Which way is softer differs a little between cars: check the garage's own help text."
NUMBER_CAVEAT = bl.NUMBER_CAVEAT
COACH_NOTE = "The corner coach has the detail for {where}."

# ---- settings beyond balance.TARGETS: (leaf names, side, name, words, how it steps)
NEW = {
    "front_spring": (("SpringRate", "SpringRateFront"), "front", "Front springs",
                     {-1: "one step softer", 1: "one step stiffer"}, "pct"),
    "rear_spring": (("SpringRate", "SpringRateRear"), "rear", "Rear springs",
                    {-1: "one step softer", 1: "one step stiffer"}, "pct"),
    "front_ride": (("RideHeight",), "front", "Front ride height", {-1: "a little lower", 1: "a little higher"}, "mm"),
    "rear_ride": (("RideHeight",), "rear", "Rear ride height", {-1: "a little lower", 1: "a little higher"}, "mm"),
    "diff_preload": (("Preload", "DiffPreload", "PreLoad"), None, "Differential preload",
                     {-1: "a little less", 1: "a little more"}, "pct"),
    "diff_power": (("PowerRampAngle", "DiffPowerRampAngle", "PowerRamp", "DiffPowerRamp"), None,
                   "Differential power ramp", {-1: "one step toward less locking", 1: "one step toward more locking"},
                   "ramp"),
    "diff_coast": (("CoastRampAngle", "DiffCoastRampAngle", "CoastRamp", "DiffCoastRamp"), None,
                   "Differential coast ramp", {-1: "one step toward less locking", 1: "one step toward more locking"},
                   "ramp"),
    "front_press": (("StartingPressure", "ColdPressure"), "front", "Front tyre pressure",
                    {-1: "a little lower", 1: "a little higher"}, "press"),
    "rear_press": (("StartingPressure", "ColdPressure"), "rear", "Rear tyre pressure",
                   {-1: "a little lower", 1: "a little higher"}, "press"),
    "rear_damp": (("LsRbdDamping", "RbdDamping", "ReboundDamping"), "rear", "Rear dampers (rebound)",
                  {-1: "one click softer", 1: "one click stiffer"}, "click"),
}
LABEL = {"front_arb": "Front anti-roll bar", "rear_arb": "Rear anti-roll bar", "bias": "Brake bias",
         "tc": "Traction control", "wing": "Rear wing"}
LABEL.update({k: v[2] for k, v in NEW.items()})
MANUAL = {"ramp"}               # no number is shown: which way the angle goes differs per car

# (kind, phase, speed class or None) -> settings to try, in order; the first the car has (and that is allowed) is used
RULES = {
    ("understeer", "mid", "slow"): [("front_arb", -1), ("rear_arb", 1), ("front_spring", -1), ("rear_spring", 1),
                                    ("front_press", -1)],
    ("understeer", "mid", "medium"): [("front_arb", -1), ("rear_arb", 1), ("front_spring", -1), ("rear_spring", 1),
                                      ("front_press", -1)],
    ("understeer", "mid", "fast"): [("wing", -1), ("rear_ride", 1), ("front_arb", -1), ("rear_arb", 1)],
    ("oversteer", "mid", "slow"): [("rear_arb", -1), ("front_arb", 1), ("rear_spring", -1), ("front_spring", 1),
                                   ("rear_press", -1)],
    ("oversteer", "mid", "medium"): [("rear_arb", -1), ("front_arb", 1), ("rear_spring", -1), ("front_spring", 1),
                                     ("rear_press", -1)],
    ("oversteer", "mid", "fast"): [("wing", 1), ("rear_ride", -1), ("rear_arb", -1)],
    ("understeer", "entry", None): [("bias", -1), ("diff_coast", -1), ("front_arb", -1), ("rear_arb", 1)],
    ("oversteer", "entry", None): [("bias", 1), ("diff_coast", 1), ("rear_arb", -1), ("front_arb", 1)],
    ("understeer", "exit", None): [("diff_power", -1), ("front_arb", -1), ("rear_arb", 1), ("diff_preload", -1)],
    ("oversteer", "exit", None): [("tc", 1), ("diff_power", -1), ("rear_arb", -1), ("rear_spring", -1)],
    ("instability", "entry", None): [("bias", 1), ("diff_coast", 1), ("wing", 1), ("rear_damp", -1)],
    ("traction", "exit", None): [("tc", 1), ("diff_power", -1), ("rear_arb", -1), ("rear_spring", -1),
                                 ("rear_press", -1)],
}
# (setting, way) -> (why, what to expect, what to watch for)
TEXTS = {
    ("front_arb", -1): ("a softer front bar lets the front tyres grip more in the corner",
                        "the front to grip more", "more oversteer on the way out"),
    ("front_arb", 1): ("a stiffer front bar takes a little grip from the front, which calms a rear that slides first",
                       "the rear to settle", "more understeer"),
    ("rear_arb", -1): ("a softer rear bar lets the rear tyres grip more", "the rear to hold on better",
                       "the front pushing a little more"),
    ("rear_arb", 1): ("a stiffer rear bar frees the rear to turn, which helps a front that slides first",
                      "the car to turn in more", "more oversteer on the way out"),
    ("front_spring", -1): ("softer front springs let the front tyres follow the road and grip more",
                           "the front to grip more mid-corner", "a lazier turn-in and more roll"),
    ("front_spring", 1): ("stiffer front springs sharpen the front and take grip from it",
                          "the rear to settle", "more understeer"),
    ("rear_spring", -1): ("softer rear springs give the rear tyres more grip", "the rear to hold on better",
                          "the front pushing more"),
    ("rear_spring", 1): ("stiffer rear springs free the rear to rotate", "the car to turn in more",
                         "a loose rear on the way out"),
    ("rear_ride", 1): ("raising the rear (more rake) puts more load on the front at speed",
                       "the front to grip more in fast corners", "a twitchier rear"),
    ("rear_ride", -1): ("lowering the rear (less rake) gives the rear more grip at speed",
                        "the rear to settle in fast corners", "the front pushing more"),
    ("front_ride", -1): ("lowering the front loads the front tyres more", "more front grip", "bottoming on bumps"),
    ("front_ride", 1): ("raising the front takes load off the front tyres", "less front grip", "less front bite"),
    ("wing", -1): ("less rear wing lets the front do more of the turning at speed",
                   "the front to grip more in fast corners", "a lighter rear and a lower top-end hold"),
    ("wing", 1): ("more rear wing gives the rear more grip at speed", "the rear to settle at speed",
                  "a little less top speed and some front push"),
    ("bias", -1): ("more brake to the rear takes braking work off the front tyres, so they have more left to turn",
                   "the car to turn in better under braking", "a lighter rear and possible rear lock-ups"),
    ("bias", 1): ("more brake to the front keeps the rear steadier as you brake and turn in",
                  "a steadier rear on the way in", "a little more front push on entry"),
    ("tc", 1): ("more traction control holds the rear when you get on the throttle",
                "the rear to hold when you pick up the throttle", "a little slower exits"),
    ("diff_coast", 1): ("more coast locking steadies the rear when you lift and brake into the corner",
                        "a steadier rear on the way in", "more push on entry"),
    ("diff_coast", -1): ("less coast locking lets the car rotate more when you lift and brake",
                         "the car to turn in more", "a looser rear on entry"),
    ("diff_power", -1): ("less power locking lets the rear wheels turn more independently as you add throttle",
                         "the car to stay settled and point better on exit", "the inside wheel spinning more"),
    ("diff_power", 1): ("more power locking drives both rear wheels together", "less inside-wheel spin",
                        "more push on exit"),
    ("diff_preload", -1): ("less preload lets the diff open up earlier, so the car turns more freely",
                           "less push on the way out", "a little less stability"),
    ("rear_damp", -1): ("softer rear rebound lets the rear follow the road on turn-in",
                        "a calmer rear on the way in", "a floaty rear over bumps"),
    ("front_press", -1): ("slightly lower front pressure can add front grip when the tyres run over-inflated hot",
                          "a little more front grip", "a hotter, squishier front: check hot pressures in Setup Lab"),
    ("rear_press", -1): ("slightly lower rear pressure can add rear grip when the tyres run over-inflated hot",
                         "a little more rear grip", "a hotter, squishier rear: check hot pressures in Setup Lab"),
}
DRIVING = {
    ("instability", "entry"): "brake in a straight line, ease off the brake smoothly before you turn in, and make one "
                              "clean steering input instead of correcting it",
    ("traction", "exit"): "wait for the car to point at the exit and squeeze the throttle on in steps instead of all "
                          "at once",
}
FIXED = {("understeer", "entry"): [("bias", -1)], ("oversteer", "entry"): [("bias", 1)],
         ("instability", "entry"): [("bias", 1), ("abs", 1)], ("oversteer", "exit"): [("tc", 1)],
         ("traction", "exit"): [("tc", 1)]}
DIAL_WORDS = dict(bl.DIAL_WORDS)
DIAL_WORDS[("abs", 1)] = "turn ABS up one step on the in-car dial"
DIAL_LABEL = dict(bl.DIAL_LABEL)
DIAL_LABEL["abs"] = "ABS (in-car dial)"
DIAL_WHY = {("abs", 1): "more ABS stops the wheels locking when you brake hard, which keeps the car straight as you turn in"}


# ---------------------------------------------------------------- settings of this car
def _find_all(flat: dict, leaves, side) -> list:
    """[(key, value)] for every setting whose last name is one of `leaves` and (when `side`) that sits in a front or
    rear section (the left and right wheels both match)."""
    want = {x.lower() for x in leaves}
    out = []
    for k, v in flat.items():
        parts = str(k).lower().split(".")
        if parts[-1] not in want:
            continue
        if side and not any(side in p for p in parts[:-1]):
            continue
        out.append((k, v))
    return out


def _step_value(kind: str, text: str, way: int):
    """(old text, new text) one step on, or None when the value isn't a number or can't go that way."""
    from . import setuplab
    pre, s = bl._parts(text)
    if s is None:
        return None
    dec = setuplab._decimals(s)
    x = float(s)
    unit = ibt.unit(text)
    if kind == "pct":
        step = max(1.0, round(abs(x) * 0.05)) if dec == 0 else max(10 ** -dec, abs(x) * 0.05)
    elif kind == "mm":
        step = 0.1 if unit.lower().startswith("in") else 1.0
    elif kind == "press":
        step = 5.0 if "kpa" in unit.lower() else (0.5 if "psi" in unit.lower() else 1.0)
    else:
        step = 1.0 if dec == 0 else 10 ** -dec
    new = x + way * step
    if new <= 0:
        return None
    return pre + s, pre + (f"{new:.{dec}f}" if dec else f"{int(round(new))}")


def _option(flat: dict, target: str, way: int) -> dict | None:
    """One change on this car: {"target","way","key","label","from","to","text","keys","manual"} or None when the car
    has no such setting (or it can't go that way)."""
    if target in bl.TARGETS:
        o = bl._option(flat, target, way)
        if o is None or o.get("to") is None:
            return None
        o = dict(o)
        o["keys"] = [o["key"]]
        o["manual"] = False
        return o
    leaves, side, name, words, how = NEW[target]
    found = _find_all(flat, leaves, side)
    if not found:
        return None
    key, val = found[0]
    if how in MANUAL:
        return {"target": target, "way": way, "key": key, "keys": [k for k, _ in found], "label": name,
                "from": str(val), "to": None, "manual": True, "text": f"{name} {words[way]}"}
    st = _step_value(how, str(val), way)
    if st is None:
        return None
    old, new = st
    unit = ibt.unit(val)
    both = " (both sides)" if len(found) > 1 else ""
    return {"target": target, "way": way, "key": key, "keys": [k for k, _ in found], "label": name, "from": old,
            "to": new, "unit": unit, "manual": False,
            "text": f"{name} {old} → {new}{(' ' + unit) if unit and unit not in str(new) else ''}{both} "
                    f"({words[way]})"}


def _lower_first(t: str) -> str:
    return t[:1].lower() + t[1:] if t else t


def _short(o: dict) -> str:
    return bl._short(o)


def _sentence(o: dict) -> str:
    """'soften the front anti-roll bar one step' style: the change as text."""
    return _lower_first(o["text"])


SWING_DRIVE = ("pick one line and one turn-in speed for this corner and repeat it; the car is reacting to how it "
               "is driven from lap to lap")


def _touched(o, last) -> dict | None:
    """A change of his to any of the option's settings (for options with no number, like the diff ramp: which way it
    goes differs per car, so any change he made there counts as the one this would undo)."""
    if not o or not last:
        return None
    keys = set(o.get("keys") or [o.get("key")])
    for ch in last.get("changes") or []:
        if keys & set(ch.get("keys") or [ch.get("key")]):
            return ch
    return None


# ---------------------------------------------------------------- feel
def adjust_sureness(sure: str, answer: str | None) -> tuple:
    """(new sureness, note): "agree" raises it one step but never above A hint (a feeling is not a measurement, and
    it must not turn a noise-level read into a likely one), "disagree" lowers it one step (never below A guess)."""
    if sure not in hd.SURE_ORDER or answer not in ("agree", "disagree"):
        return sure, None
    i = hd.SURE_ORDER.index(sure)
    top = hd.SURE_ORDER.index("hint")
    if answer == "agree":
        j = i + 1 if i < top else i
        return hd.SURE_ORDER[j], ("You said this matches what you felt, so it is one step surer (never above A hint "
                                  "on a feeling alone)." if j > i else "You said this matches what you felt.")
    j = max(i - 1, 0)
    return hd.SURE_ORDER[j], ("You said this is not what you felt, so it is one step less sure." if j < i else
                              "You said this is not what you felt.")


FELT = ("understeer", "oversteer", "nervous entry", "no grip on exit", "fine")


def feel_key(issue_id: str) -> str:
    return str(issue_id)


# ---------------------------------------------------------------- the advice
def _strength(i: dict) -> tuple:
    return (hd.SURE_ORDER.index(i.get("sureness_raw") or i["sureness"]), i.get("share") or 0.0, len(i.get("corners") or []))


def advise(report: dict, setup: dict | None, series_setup=None, last_change: dict | None = None, dials=None,
           feel: dict | None = None) -> dict:
    """Ranked advice from a handling report. Never raises.

    {"ok", "empty": why there is nothing to try (or None), "items": [item], "top": item | None, "card": one line,
     "series_setup"}
    item: {"issue_id", "title", "verdict", "sureness", "sure_words", "corners", "laps_showing", "laps",
           "change": {"key","label","from","to","text"} | None, "dial": {...} | None, "driving": text | None,
           "coach": text | None, "why", "expect", "watch", "text", "then", "first", "rank", "feel", "feel_note"}"""
    out = {"ok": False, "empty": None, "items": [], "top": None, "card": "", "series_setup": series_setup,
           "early_advice": hd.early_advice_line()}
    try:
        feel = feel or {}
        if not report or not report.get("ok"):
            out["empty"] = (report or {}).get("message") or "The handling read has not run yet."
            out["card"] = "Car balance: " + out["empty"]
            return out
        out["ok"] = True
        issues = list(report.get("issues") or [])
        fixed = str(series_setup or "").lower() == "fixed"
        if not issues:
            n = report.get("laps") or 0
            out["empty"] = (f"The car looks balanced and stable on these {n} clean laps: nothing to change.")
            out["card"] = "Car balance: " + out["empty"]
            return out
        flat = ibt.flatten(setup or {})
        have = set(dials or ())
        if fixed:
            for target, leaves in (("bias", bl._BIAS), ("tc", bl._TC)):
                if bl._find_key(flat, leaves)[0] is not None:
                    have.add(target)
        items, taken = [], []                                   # taken: (issue, option)
        setup_issues = sorted([i for i in issues if i["verdict"] == "setup"], key=_strength, reverse=True)
        driving_issues = [i for i in issues if i["verdict"] == "driving"]
        for i in setup_issues + driving_issues:
            sure, fnote = adjust_sureness(i["sureness"], (feel.get(feel_key(i["id"])) or {}).get("answer"))
            base = {"issue_id": i["id"], "title": i["title"], "verdict": i["verdict"], "sureness": sure,
                    "sure_words": hd.SURE_WORDS[sure], "corners": i["corners"], "laps_showing": i["laps_showing"],
                    "laps": i["laps"], "change": None, "dial": None, "driving": None, "coach": None, "why": None,
                    "expect": None, "watch": None, "then": THEN, "first": False, "rank": None,
                    "feel": (feel.get(feel_key(i["id"])) or {}).get("answer"),
                    "felt": (feel.get(feel_key(i["id"])) or {}).get("felt"), "feel_note": fnote, "alternative": None}
            where = ", ".join(i["corners"])
            lead = f"{i['title']} on {i['laps_showing']} of {i['laps']} clean laps"
            if i["verdict"] == "driving":
                base["coach"] = COACH_NOTE.format(where=where)
                base["driving"] = (SWING_DRIVE if i["kind"] == "swing" else DRIVING.get((i["kind"], i["phase"]))
                                   or bl.DRIVING.get((i["phase"], "push" if i["kind"] == "understeer" else "rotate")))
                base["text"] = i["text"] + (f" Try: {base['driving']}." if base["driving"] else "")
                base["then"] = bl.THEN_DRIVE
                items.append(base)
                continue
            if fixed:
                tried = None
                for target, way in FIXED.get((i["kind"], i["phase"]), []):
                    if target not in have and target != "abs":
                        continue
                    if target == "abs" and "abs" not in have:
                        continue
                    if any(oo["target"] == target and oo["way"] != way for _g, oo in taken):
                        continue
                    tried = (target, way)
                    break
                if tried:
                    t, w = tried
                    why = DIAL_WHY.get((t, w)) or bl.WHY_WAY.get((t, w))
                    base["dial"] = {"target": t, "way": w, "label": DIAL_LABEL[t], "text": DIAL_WORDS[(t, w)]}
                    base["why"] = why
                    taken.append((i, {"target": t, "way": w, "key": f"dial:{t}"}))
                    base["text"] = (f"{lead}. The setup is locked, but this car has a {DIAL_LABEL[t].split(' (')[0].lower()} "
                                    f"dial: {DIAL_WORDS[(t, w)]}. Why: {why}. "
                                    f"{bl.BIAS_CAVEAT if t == 'bias' else bl.TC_CAVEAT if t == 'tc' else ''} "
                                    f"{bl.THEN_DIAL}").replace("  ", " ")
                    base["then"] = bl.THEN_DIAL
                else:
                    drive = DRIVING.get((i["kind"], i["phase"])) or bl.DRIVING.get(
                        (i["phase"], "push" if i["kind"] == "understeer" else "rotate"))
                    base["driving"] = drive
                    base["text"] = (f"{lead}. The setup is locked and this car has no in-car dial for it, so adapt "
                                    f"the driving: {drive}. {bl.THEN_DRIVE}")
                    base["then"] = bl.THEN_DRIVE
                items.append(base)
                continue
            rule = RULES.get((i["kind"], i["phase"], i["class"])) or RULES.get((i["kind"], i["phase"], None)) or []
            pick, undone, clash = None, None, None
            for target, way in rule:
                o = _option(flat, target, way)
                if o is None:
                    continue
                if last_change and not last_change.get("slower"):
                    ch = _touched(o, last_change) if o.get("manual") else bl._undo_of(o, last_change)
                    if ch:
                        undone = undone or (o, ch)
                        continue
                hit = next(((g, oo) for g, oo in taken if oo["key"] == o["key"] and oo["way"] != o["way"]), None)
                if hit:
                    clash = clash or (o, hit)
                    continue
                if pick is None:
                    pick = o
                    continue
                base["alternative"] = {"label": o["label"], "text": o["text"]}
                break
            if pick is None:
                if undone:
                    o, ch = undone
                    base["text"] = (f"{lead}. The usual change, {_sentence(o)}, would undo your change from "
                                    f"{last_change.get('from') or 'the last setup'} to {last_change.get('to') or 'this one'}, "
                                    f"and the laps don't show that change made the car slower, so it isn't suggested.")
                elif clash:
                    o, (g, oo) = clash
                    base["text"] = (f"{lead}. {_short(o)} would pull against the idea for {g['title'].lower()} "
                                    f"({_lower_first(_short(oo))}), which is surer: one would undo the other, so try that "
                                    f"one first and look here again after.")
                else:
                    base["text"] = (f"{lead}. The usual fixes are {', '.join(LABEL[t].lower() for t, _w in rule[:3])}, "
                                    f"but this car's setup shows nothing Debrief can step for this.")
                base["then"] = None
                items.append(base)
                continue
            taken.append((i, pick))
            why, expect, watch = TEXTS.get((pick["target"], pick["way"]), ("it moves the balance the way you need",
                                                                          "the balance to move", "the opposite problem"))
            base["change"] = {k: pick.get(k) for k in ("key", "label", "from", "to", "text", "target", "way")}
            base["why"], base["expect"], base["watch"] = why, expect, watch
            cav = (DIFF_CAVEAT if pick["target"].startswith("diff") else DAMP_CAVEAT if pick["target"].endswith("damp")
                   else bl.TC_CAVEAT if pick["target"] == "tc" else NUMBER_CAVEAT)
            base["text"] = (f"{lead}: {_sentence(pick)}. Why: {why}. Expect {expect}; watch for {watch}. {cav} {THEN}")
            if last_change and last_change.get("slower") and bl._undo_of(pick, last_change):
                base["text"] += (f" That goes back on your change from {last_change.get('from')} to "
                                 f"{last_change.get('to')}, and the laps showed {last_change.get('to')} slower, so going "
                                 f"back is reasonable.")
            items.append(base)
        n = 0
        for it in items:
            if it["verdict"] == "setup" and (it["change"] or it["dial"]):
                n += 1
                it["rank"] = n
        first = next((it for it in items if it["rank"] == 1), None)
        if first:
            first["first"] = True
        out["items"] = items
        out["top"] = first or next((it for it in items if it["driving"]), None) or (items[0] if items else None)
        out["card"] = card_text(out["top"], items)
        if first is None and out["top"] is not None and out["top"]["verdict"] == "driving":
            out["empty"] = "Nothing here points at the setup: it looks like driving."
        return out
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["empty"] = f"The advice could not be worked out ({str(e)[:120]})."
        out["card"] = "Car balance: " + out["empty"]
        return out


def card_text(top: dict | None, items: list) -> str:
    """'Car balance: mid-corner understeer in slow corners (Likely). Try: front anti-roll bar ...'."""
    if not top:
        return "Car balance: nothing to change."
    t = top["title"][:1].lower() + top["title"][1:]
    head = f"Car balance: {t} ({top['sure_words']})."
    if top["verdict"] == "driving":
        return head + " It looks like driving, not setup: " + (top.get("coach") or "see the corner coach.")
    if top.get("change"):
        return head + " Try: " + _lower_first(top["change"]["text"]) + "."
    if top.get("dial"):
        return head + " Try: " + top["dial"]["text"] + "."
    if top.get("driving"):
        return head + " Try: " + top["driving"] + "."
    return head


# ---------------------------------------------------------------- setup history (per car and track)
HISTORY_LIMIT = 30


def history_key(car, track) -> str:
    return f"{str(car or '').lower()}|{str(track or '').lower()}"


def record_shown(store: dict, key: str, item: dict | None, setup_fp: str | None, now: str) -> bool:
    """Keep the top suggestion that was shown (once per setup and issue). True when something was added."""
    if not item or not setup_fp or not (item.get("change") or item.get("dial") or item.get("driving")):
        return False
    if item.get("verdict") != "setup":
        return False
    lst = store.setdefault(key, [])
    if any(e.get("setup_fp") == setup_fp and e.get("issue_id") == item["issue_id"] for e in lst):
        return False
    ch = item.get("change") or {}
    lst.append({"at": now, "issue_id": item["issue_id"], "title": item["title"], "sureness": item["sureness"],
                "change_key": ch.get("key"), "change_text": ch.get("text") or (item.get("dial") or {}).get("text")
                or item.get("driving"), "from": ch.get("from"), "to": ch.get("to"), "setup_fp": setup_fp,
                "status": "shown", "result": None})
    del lst[:-HISTORY_LIMIT]
    return True


def _same_number(a, b) -> bool:
    from . import setuplab
    x, y = setuplab._num_text(str(a)), setuplab._num_text(str(b))
    try:
        return x is not None and y is not None and math.isclose(float(x), float(y), abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def evaluate(store: dict, key: str, current_fp: str | None, prev_fp: str | None, flat: dict,
             last_change: dict | None, issue_ids) -> bool:
    """Fill in what he did next for the entries of the setup he drove just before this one. Uses only Setup Lab's own
    comparison (last_change: its verdict and slower flag) and the new handling read. True when anything changed."""
    changed = False
    ids = set(issue_ids or ())
    for e in store.get(key, []):
        if e.get("status") != "shown" or not current_fp or e.get("setup_fp") == current_fp:
            continue
        if prev_fp is None or e.get("setup_fp") != prev_fp:
            e["status"], e["result"] = "moved on", "You drove other setups after this one, so it can't be judged on its own."
            changed = True
            continue
        did = bool(e.get("change_key")) and e["change_key"] in flat and e.get("to") is not None and \
            _same_number(flat[e["change_key"]], e["to"])
        diffs = [c for c in (last_change or {}).get("changes") or []]
        if e.get("change_key") and not did:
            e["status"] = "did something else" if diffs else "not done"
            e["result"] = ("You changed something else, not this." if diffs else "The setup is the same, so this "
                           "wasn't tried.")
            changed = True
            continue
        e["status"] = "followed" if did else "driven"
        gone = e["issue_id"] not in ids
        bits = []
        if last_change:
            if last_change.get("slower"):
                bits.append("Setup Lab's comparison says the laps got slower")
            elif last_change.get("verdict") == "faster":
                bits.append("Setup Lab's comparison says the laps got faster")
            elif last_change.get("verdict") == "no clear difference":
                bits.append("Setup Lab's comparison sees no clear pace difference")
            else:
                bits.append("Setup Lab can't tell yet whether the laps are faster")
        bits.append("the problem no longer shows" if gone else "the problem still shows")
        e["result"] = ("You did it. " if did else "") + "; ".join(bits).capitalize() + "."
        e["helped"] = bool(gone and not (last_change or {}).get("slower"))
        changed = True
    return changed


def history_view(store: dict, key: str, limit: int = 5) -> list:
    return [{"at": e.get("at"), "title": e.get("title"), "suggested": e.get("change_text"),
             "status": e.get("status"), "result": e.get("result")} for e in (store.get(key) or [])[-limit:]][::-1]
