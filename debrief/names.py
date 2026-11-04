"""One display name per track and car (Debrief 2.18).

The same track reaches Debrief under several spellings: Garage 61 and the review pages say "Circuit Zolder
(Grand Prix)", iRacing's telemetry files give the name and the layout apart ("Circuit Zolder", "Grand Prix"),
some code paths joined them as "Circuit Zolder - Grand Prix", and a name that already held its layout could
get it twice ("Circuit Zolder (Grand Prix) - Grand Prix"). iRacing's own car names are lower case in places
("Mclaren 570s GT4") where Garage 61 and the results file say "McLaren 570S GT4".

track() and car() turn every spelling into one form, the one the results file and Garage 61 use:
"Base (Layout)" and the official car spelling. Both are idempotent (track(track(x)) == track(x)) and never
merge two different layouts: the layout is kept, only written the same way. Pure, no I/O.
"""
from __future__ import annotations

import re

# iRacing's base names written another way by some sources -> iRacing's own spelling. Only spellings seen in
# real files or Garage 61 go here: a guess could merge two different tracks.
TRACK_ALIASES = {
    "zolder circuit": "Circuit Zolder",
    "zolder": "Circuit Zolder",
}

# Words iRacing writes in lower case in its car screen names, with the official spelling.
CAR_WORDS = {
    "mclaren": "McLaren", "mx-5": "MX-5", "mx5": "MX-5", "bmw": "BMW", "amg": "AMG", "gt3": "GT3", "gt4": "GT4",
    "gte": "GTE", "gtp": "GTP", "lmp2": "LMP2", "lmp3": "LMP3", "tcr": "TCR", "gr86": "GR86", "evo": "EVO",
    "rs": "RS", "gti": "GTI", "lmdh": "LMDh", "m4": "M4", "m8": "M8", "r8": "R8", "lms": "LMS", "f3": "F3",
    "fr2.0": "FR2.0", "srx": "SRX", "nascar": "NASCAR", "arca": "ARCA", "cup": "Cup", "ir-01": "iR-01",
    "ir18": "IR18", "ir-18": "IR-18", "fia": "FIA", "vw": "VW", "usf": "USF",
}
_MODEL_S = re.compile(r"^(\d{3})s$")   # McLaren's "570s", "720s" -> "570S", "720S"


def _space(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def split_track(name) -> tuple:
    """('Circuit Zolder', 'Grand Prix') from 'Circuit Zolder (Grand Prix)', 'Circuit Zolder - Grand Prix' or
    'Circuit Zolder (Grand Prix) - Grand Prix'. The layout is '' when there is none."""
    full = _space(name)
    base, cfg = full, ""
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*(?:[-–]\s*(.*))?$", full)
    if m:
        base, cfg, more = m.group(1), m.group(2), m.group(3) or ""
        if more and _key(more) != _key(cfg):
            cfg = f"{cfg} {more}".strip()
    else:
        m = re.match(r"^(.*?)\s[-–]\s(.*)$", full)
        if m:
            base, cfg = m.group(1), m.group(2)
    return _space(base), _space(cfg)


def _key(s) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(s or "").lower()))


def track(name, config=None) -> str:
    """The display name: 'Base (Layout)', or 'Base' when there is no layout. config is iRacing's
    TrackConfigName when the name came without it; a layout already in the name isn't written twice."""
    base, cfg = split_track(name)
    extra = _space(config)
    if extra and _key(extra) != _key(cfg) and _key(extra) not in _key(cfg):
        cfg = f"{cfg} {extra}".strip() if cfg else extra
    if not base:
        return cfg
    base = TRACK_ALIASES.get(_key(base), base)
    return f"{base} ({cfg})" if cfg else base


def car(name) -> str:
    """The display name of a car: iRacing's lower-case words written the official way ('Mclaren 570s GT4' ->
    'McLaren 570S GT4'). Anything else is left as it is."""
    out = []
    for w in _space(name).split(" "):
        lw = w.lower()
        if lw in CAR_WORDS:
            out.append(CAR_WORDS[lw])
        elif _MODEL_S.match(lw):
            out.append(lw[:-1] + "S")
        else:
            out.append(w)
    return " ".join(out)
