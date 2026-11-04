"""One speed unit and one lap-time format everywhere (Debrief 2.18).

Settings > Units picks mph (the default) or km/h. Debrief works in its own units inside (reviews in mph, the
telemetry pages and the coach in km/h); everything a driver reads or hears goes through here first:

* convert_text(text, unit): every "N km/h" / "N mph" in a sentence, written in the chosen unit, with the same
  number of decimals. "101 vs 99 km/h" and "4 to 20 km/h" convert both numbers.
* convert(obj, unit): the same for every string inside a JSON-like value (dict keys are left alone).
* speed(v, frm, unit): one number.
* lap(t): the one lap-time format, m:ss.000 ("1:35.849"; under a minute "59.123").

Pure, no I/O.
"""
from __future__ import annotations

import math
import re

KMH_PER_MPH = 1.609344
UNITS = ("mph", "kmh")
DEFAULT = "mph"
LABEL = {"mph": "mph", "kmh": "km/h"}

_NUM = r"[+\-−]?\d+(?:\.\d+)?"
_JOIN = r"(?:\s+(?:vs\.?|to|and|or)\s+|[-\u2013/])"
_RX = re.compile(rf"(?<![\w.])({_NUM}(?:{_JOIN}{_NUM})*)(\s*)(km/h|kmh|kph|mph)(?![\w/])", re.I)
_ONE = re.compile(_NUM)


def norm(unit) -> str:
    """"kmh" for km/h, kmh, kph or kilometres; anything else is "mph"."""
    u = re.sub(r"[^a-z]", "", str(unit or "").lower())
    return "kmh" if u in ("kmh", "kph", "km", "kilometres", "kilometers", "metric") else "mph"


def label(unit) -> str:
    return LABEL[norm(unit)]


def speed(v, frm, unit):
    """v (in frm units) in unit; None stays None."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    a, b = norm(frm), norm(unit)
    if a == b:
        return x
    return x * KMH_PER_MPH if a == "mph" else x / KMH_PER_MPH


def _fmt(x: float, decimals: int, sign: str) -> str:
    s = f"{abs(x):.{decimals}f}"
    if sign:
        return sign + s
    return ("-" if x < 0 and float(s) != 0 else "") + s


def _conv_num(tok: str, frm: str, to: str) -> str:
    sign = tok[0] if tok[0] in "+-−" else ""
    body = tok[1:] if sign else tok
    dec = len(body.split(".")[1]) if "." in body else 0
    v = speed(float(body), frm, to)
    return _fmt(v, dec, sign)


def convert_text(text, unit):
    """text with every speed written in unit. Non-strings come back unchanged."""
    if not isinstance(text, str) or ("mph" not in text and "km" not in text and "kph" not in text):
        return text
    to = norm(unit)

    def one(m):
        frm = norm(m.group(3))
        if frm == to:
            return m.group(0) if m.group(3).lower() in ("mph", "km/h") else m.group(1) + m.group(2) + LABEL[to]
        nums = _ONE.sub(lambda n: _conv_num(n.group(0), frm, to), m.group(1))
        # a range like "1-2 km/h" can land on one number ("1-1 mph"): say "about 1 mph" instead
        rng = re.fullmatch(rf"({_NUM})\s*(?:[-\u2013]|to)\s*({_NUM})", nums.strip())
        if rng and rng.group(1) == rng.group(2):
            return "about " + rng.group(1) + m.group(2) + LABEL[to]
        return nums + m.group(2) + LABEL[to]
    return _RX.sub(one, text)


def convert(obj, unit):
    """A copy of a JSON-like value with every string run through convert_text."""
    if isinstance(obj, str):
        return convert_text(obj, unit)
    if isinstance(obj, dict):
        return {k: convert(v, unit) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [convert(v, unit) for v in obj]
    return obj


def lap(t) -> str:
    """The one lap-time format: m:ss.000, or ss.000 under a minute. '' for no time."""
    try:
        x = float(t)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(x) or x <= 0:
        return ""
    ms = int(round(x * 1000))
    m, rest = divmod(ms, 60000)
    s = rest / 1000
    return f"{m}:{s:06.3f}" if m else f"{s:.3f}"
