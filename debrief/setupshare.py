"""Setup share (2.15): a short "what changed and did it help" text for Discord, from a Setup Lab report.

    share(report, a=None, b=None)          -> {"ok", "text", "markdown", "pair": (a, b), "chars"}
    share_text(report, ...) / share_markdown(report, ...) -> just the text

Example:
    McLaren 570S GT4 at Zolder Circuit. Setup B vs A: rear wing 2.5 → 3.5 deg, front camber -4.0 → -3.6 deg
    (both sides). Result: no clear difference yet (6 and 7 clean laps; about 4 more on each would show a 0.1 s gap).

What it never contains: setup file names (they often carry a name), driver names, file names, folders, or any
setup file contents beyond the changed settings. Garage results (corner weights, ride heights) and pit readings
are left out; only what he set. The result is Setup Lab's own verdict (setuplab.compare), including its driving-
order caution, never re-judged here. Plain text is at most MAX_CHARS (Discord's limit is 2,000).
"""
from __future__ import annotations

import re

from . import setuplab

MAX_CHARS = 1500
MAX_CHANGES = 8             # more changes than this are counted, not listed
_SAFE = re.compile(r"[^\w\s.,:;()+\-/%°→'?!]", re.U)


def _clean(s: str) -> str:
    """Plain words only: no markdown/mention characters (@, #, *, `, <, >, links) and no long dashes."""
    s = str(s or "").replace("—", ", ").replace("–", " to ")
    s = _SAFE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _car_name(rep: dict) -> str:
    name = str(rep.get("car") or rep.get("car_path") or "the car")
    # iRacing's screen names are lower-case in places ("Mclaren 570s GT4"): tidy the common ones
    fixes = {"Mclaren": "McLaren", "570s": "570S", "720s": "720S", "Mx-5": "MX-5"}
    return " ".join(fixes.get(w, w) for w in name.split())


def _track_name(rep: dict) -> str:
    t = str(rep.get("track") or rep.get("track_code") or "the track")
    parts = [p.strip() for p in t.split(" - ") if p.strip()]
    if len(parts) >= 2:
        return f"{parts[0]} ({parts[1]})"
    return t


def _change_words(ch: dict) -> str:
    """'Rear wing 2.5 → 3.5 deg' -> 'rear wing 2.5 → 3.5 deg' (labels are plain words already)."""
    t = str(ch.get("text") or "")
    if t[:1].isupper() and not t[:2].isupper():
        t = t[0].lower() + t[1:]
    return t


def _pick(rep: dict, a, b):
    diffs = [d for d in rep.get("diffs") or [] if isinstance(d, dict)]
    if a and b:
        d = next((x for x in diffs if x.get("from") == a and x.get("to") == b), None)
        if d is None:
            d = next((x for x in diffs if x.get("from") == b and x.get("to") == a), None)
            if d is not None:
                d = {"from": a, "to": b, "changes": [_flip(c) for c in d.get("changes") or []]}
        if d is None:
            sa = next((s for s in rep.get("setups") or [] if s.get("id") == a), None)
            sb = next((s for s in rep.get("setups") or [] if s.get("id") == b), None)
            if sa is not None and sb is not None and isinstance(sa.get("setup"), dict) and isinstance(sb.get("setup"), dict):
                d = {"from": a, "to": b, "changes": setuplab.diff(sa["setup"], sb["setup"])}
        return d
    return diffs[-1] if diffs else None


def _flip(c: dict) -> dict:
    c = dict(c)
    c["old"], c["new"] = c.get("new"), c.get("old")
    o, n = setuplab._num_text(c["old"]) if c["old"] is not None else None, setuplab._num_text(c["new"]) if c["new"] is not None else None
    unit = c.get("unit") or ""
    if o is not None and n is not None:
        c["text"] = f"{c.get('label')} {o} → {n}" + (f" {unit}" if unit else "") + (" (both sides)" if c.get("both_sides") else "")
    return c


def _pair_for(rep: dict, a: str, b: str):
    for p in ((rep.get("compare") or {}).get("pairs") or []):
        if {p.get("a"), p.get("b")} == {a, b}:
            return p
    return None


def _pace(rep: dict, sid: str):
    for s in ((rep.get("compare") or {}).get("setups") or []):
        if s.get("id") == sid:
            return s.get("pace") or {}
    return {}


def _result(rep: dict, a: str, b: str) -> str:
    p = _pair_for(rep, a, b)
    if p is None:
        return "Result: not compared yet (Setup Lab compares setups driven one after the other)."
    na, nb = _pace(rep, a).get("clean_laps"), _pace(rep, b).get("clean_laps")
    laps = f"{na} and {nb} clean laps" if na is not None and nb is not None else ""
    v = p.get("verdict")
    if v == "faster":
        fast = p.get("faster")
        slow = b if fast == a else a
        d = abs(float(p.get("diff_s") or 0))
        s = f"Result: {fast} was faster than {slow} by about {setuplab._fmt_s(d)} s a lap ({laps})."
        if p.get("caution"):
            s += f" Caution: {fast} was also driven later, so part of it may be getting quicker; worth going back to {slow} for a few laps."
        return s
    if v == "no clear difference":
        more = p.get("more_laps")
        if more and more <= setuplab.MAX_MORE_LAPS:
            return (f"Result: no clear difference yet ({laps}; about {more} more on each would show a "
                    f"{setuplab.MIN_DIFF:.1f} s gap).")
        if p.get("diff_s") is not None and more is None:
            return f"Result: a gap smaller than {setuplab.MIN_DIFF:.1f} s a lap, which counts as no real difference ({laps})."
        return f"Result: no clear difference: too close to tell apart ({laps})."
    if v == "confounded":
        why = "; ".join(_clean(_strip_names(x, rep)) for x in (p.get("confounders") or [])[:2])
        return f"Result: can't be compared fairly yet ({why})." if why else "Result: can't be compared fairly yet."
    miss = [_clean(re.sub(r"\s*\(([^()]*)\)", r", \1", _strip_names(x, rep))) for x in (p.get("missing") or [])]
    return "Result: not enough laps to tell yet" + (f" ({'; '.join(miss)})." if miss else ".")


def _known_names(rep: dict) -> list:
    """Every setup name, label and file name the report knows (setup names often carry a person's name), longest
    first so a name containing another is removed whole."""
    out = set()
    for st in (rep.get("setups") or []) + ((rep.get("compare") or {}).get("setups") or []):
        if not isinstance(st, dict):
            continue
        for v in [st.get("name")] + list(st.get("names") or []) + list(st.get("files") or []):
            if isinstance(v, dict):
                v = v.get("file") or v.get("name")
            if isinstance(v, str) and v.strip():
                out.add(v.strip())
                out.add(v.strip().rsplit(".", 1)[0])
        lab = str(st.get("label") or "")
        if lab.startswith("Setup ") and lab.endswith(")") and " (" in lab:
            out.add(lab[lab.index(" (") + 2:-1])
    return sorted((x for x in out if len(x) >= 3), key=len, reverse=True)


def _strip_names(s: str, rep: dict | None = None) -> str:
    """'Setup B (Swazi Williams (v2) quali)' -> 'Setup B': every known setup name and label is removed literally
    (brackets inside a name can't leak part of it), then any "Setup B (...)" left keeps only "Setup B"."""
    s = str(s or "")
    for nm in _known_names(rep or {}):
        s = s.replace(f" ({nm})", "").replace(nm, "")
    out, i = [], 0
    for m in re.finditer(r"Setup [A-Z]{1,3} \(", s):
        if m.start() < i:
            continue
        depth, j = 0, m.end() - 1
        while j < len(s):
            depth += {"(": 1, ")": -1}.get(s[j], 0)
            if depth == 0:
                break
            j += 1
        out.append(s[i:m.end() - 2])                   # "...Setup B"
        i = j + 1 if j < len(s) else len(s)           # an unclosed bracket: drop the rest
    out.append(s[i:])
    return "".join(out)


def share(report: dict, a: str | None = None, b: str | None = None) -> dict:
    """The share text for setup b vs a (default: the most recent change he made). Never raises."""
    out = {"ok": False, "text": "", "markdown": "", "pair": None, "chars": 0}
    try:
        rep = report or {}
        d = _pick(rep, a, b)
        if not d:
            out["text"] = "Nothing to share yet: Setup Lab needs two different setups for this car and track."
            out["markdown"] = out["text"]
            return out
        a, b = d.get("from"), d.get("to")
        changes = [c for c in d.get("changes") or [] if c.get("kind") == "setting"]
        words = [_clean(_change_words(c)) for c in changes]
        more = len(words) - MAX_CHANGES
        shown = words[:MAX_CHANGES]
        head = f"{_clean(_car_name(rep))} at {_clean(_track_name(rep))}."
        if shown:
            chg = f"Setup {b} vs {a}: " + ", ".join(shown) + (f", and {more} more change{'s' if more != 1 else ''}" if more > 0 else "") + "."
        else:
            chg = f"Setup {b} vs {a}: only fuel or garage results changed, no settings."
        res = _strip_names(_result(rep, a, b), rep)
        text = " ".join([head, chg, res])
        while len(text) > MAX_CHARS and shown:
            shown = shown[:-1]
            more += 1
            chg = (f"Setup {b} vs {a}: " + ", ".join(shown) + f", and {more} more change{'s' if more != 1 else ''}.")
            text = " ".join([head, chg, res])
        text = text[:MAX_CHARS]
        md_lines = [f"**{_clean(_car_name(rep))}** at **{_clean(_track_name(rep))}**", f"Setup {b} vs {a}:"]
        md_lines += [f"- {w}" for w in shown] + ([f"- and {more} more change{'s' if more != 1 else ''}"] if more > 0 else [])
        if not shown:
            md_lines.append("- only fuel or garage results changed")
        md_lines.append(res.replace("Result:", "**Result:**", 1))
        md = "\n".join(md_lines)[:MAX_CHARS]
        out.update(ok=True, text=text, markdown=md, pair=(a, b), chars=len(text))
        return out
    except Exception as e:  # noqa: BLE001
        out["text"] = f"Could not make the share text ({str(e)[:80]})."
        return out


def share_text(report: dict, a: str | None = None, b: str | None = None) -> str:
    return share(report, a, b)["text"]


def share_markdown(report: dict, a: str | None = None, b: str | None = None) -> str:
    return share(report, a, b)["markdown"]
